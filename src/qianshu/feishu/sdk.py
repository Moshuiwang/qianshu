"""飞书官方 SDK 适配。只通过本应用长连接分发审批事件。"""

import asyncio
import base64
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import ContextVar
from http import HTTPStatus
from pathlib import Path
from secrets import token_urlsafe
from threading import Lock
from urllib.parse import parse_qs, urlparse
from uuid import NAMESPACE_OID, uuid5

import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageReactionRequest, CreateMessageReactionRequestBody,
    CreateMessageRequest, CreateMessageRequestBody, Emoji,
    GetChatMembersRequest, GetMessageResourceRequest, ListMessageRequest,
    ReplyMessageRequest, ReplyMessageRequestBody,
)
from lark_oapi.core.http import Transport as SDKTransport
from lark_oapi.core.token import (
    AccessTokenResponse, CreateSelfTenantTokenRequest, CreateSelfTenantTokenRequestBuilder,
    CreateTokenRequestBody, CreateTokenRequestBodyBuilder, TokenManager,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger, P2CardActionTriggerResponse,
)
from lark_oapi.ws import client as ws

from .cards import Report, approval_card, decided_approval_card
from .entrance import Application, Attachment, Entrance, Message
from .settings import Settings, Window
from .state import State

_LOG = logging.getLogger(__name__)
_HTTP_TIMEOUT = 2
_CALLBACK_BUDGET = 3
_CALLBACK_RESPONSE_MARGIN = 0.5
_CALLBACK_TIMEOUT_NOTICE = "处理超时，请重新点击"


class _EndpointUnavailable(ws.ServerException):
    """取址暂时失败，可以在窗口内重试。"""


def _checked(response):
    if not response.success():
        raise RuntimeError("飞书 API 请求失败。")
    return response


def _approval_warning(content: str) -> P2CardActionTriggerResponse:
    return P2CardActionTriggerResponse({"toast": {"type": "warning", "content": content}})


def _message(message, sender_id: str, kind: str, content: str, bot_keys, bot_open_id: str) -> Message:
    keys = tuple(bot_keys)
    if kind == "post" and keys:
        keys += (bot_open_id,)
    return Message(
        message.message_id or "", message.chat_id or "", sender_id or "",
        int(message.create_time), kind, content, keys,
    )


class _Transport:
    def __init__(self, settings: Settings, window: Window, clock: Callable[[], float]):
        self.settings, self.window, self.clock = settings, window, clock
        self.im = (
            lark.Client.builder().app_id(settings.app_id).app_secret(settings.app_secret)
            .log_level(lark.LogLevel.ERROR).timeout(_HTTP_TIMEOUT).enable_set_token(True).build().im.v1
        )

    def _require_window(self) -> None:
        if not self.window.contains(self.clock()):
            raise RuntimeError("飞书验收窗口已关闭。")

    def react(self, message_id: str) -> None:
        body = CreateMessageReactionRequestBody.builder().reaction_type(
            Emoji.builder().emoji_type("OK").build(),
        ).build()
        request = CreateMessageReactionRequest.builder().message_id(message_id).request_body(body)
        self._require_window()
        _checked(self.im.message_reaction.create(request.build()))

    def reply(self, message_id: str, text: str, *, deadline: float) -> None:
        body = ReplyMessageRequestBody.builder().msg_type("text").content(
            json.dumps({"text": text}, ensure_ascii=False),
        ).uuid(
            str(uuid5(NAMESPACE_OID, message_id)),
        ).build()
        request = ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()

        async def send():
            self._require_window()
            remaining = min(deadline - time.monotonic(), self.window.end.timestamp() - self.clock())
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                token = await self._tenant_token()
                self._require_window()
                if time.monotonic() >= deadline:
                    raise TimeoutError
                option = lark.RequestOption.builder().tenant_access_token(token).build()
                _checked(await self.im.message.areply(request, option))

        asyncio.run(send())

    async def _tenant_token(self) -> str:
        # 复用 SDK 1.8 的令牌缓存和请求模型，用可取消的异步传输处理缓存未命中。
        config = self.im.message.config
        cache_key = f"self_tenant_token:{config.app_id}"
        token = TokenManager.cache.get(cache_key)
        if token:
            return token
        # 该版本的构建器默认共用模型实例，给并发请求各自传入新实例。
        body = (
            CreateTokenRequestBodyBuilder(CreateTokenRequestBody())
            .app_id(config.app_id).app_secret(config.app_secret).build()
        )
        request = (
            CreateSelfTenantTokenRequestBuilder(CreateSelfTenantTokenRequest()).request_body(body).build()
        )
        raw = await SDKTransport.aexecute(config, request)
        if raw.status_code != HTTPStatus.OK:
            raise RuntimeError("飞书租户令牌请求失败。")
        response = _checked(lark.JSON.unmarshal(raw.content.decode(), AccessTokenResponse))
        if not response.tenant_access_token or type(response.expire) is not int or response.expire <= 0:
            raise RuntimeError("飞书未返回有效租户令牌。")
        token = response.tenant_access_token
        TokenManager.cache.set(cache_key, token, int(time.time() + response.expire - 600))
        return token

    async def is_member(self, operator_id: str) -> bool:
        self._require_window()
        option = lark.RequestOption.builder().tenant_access_token(await self._tenant_token()).build()
        seen_pages = set()
        page = None
        while True:
            self._require_window()
            request = (
                GetChatMembersRequest.builder().chat_id(self.settings.chat_id)
                .member_id_type("open_id").page_size(100)
            )
            if page:
                request.page_token(page)
            response = await self.im.chat_members.aget(request.build(), option)
            data = _checked(response).data
            if data.trigger_security_conf_limit or type(data.has_more) is not bool:
                return False
            if any(member.member_id_type == "open_id" and member.member_id == operator_id
                   for member in data.items or ()):
                return True
            if not data.has_more:
                return False
            page = data.page_token
            if not isinstance(page, str) or not page.strip() or page in seen_pages:
                return False
            seen_pages.add(page)

    def send_card(self, card: dict) -> str:
        body = CreateMessageRequestBody.builder().receive_id(self.settings.chat_id).msg_type(
            "interactive",
        ).content(json.dumps(card, ensure_ascii=False)).build()
        request = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body)
        self._require_window()
        response = _checked(self.im.message.create(request.build()))
        if not response.data or not response.data.message_id:
            raise RuntimeError("飞书未返回卡片消息标识。")
        return response.data.message_id

    async def notify_callback_timeout(self) -> None:
        async with asyncio.timeout(_HTTP_TIMEOUT):
            self._require_window()
            option = lark.RequestOption.builder().tenant_access_token(await self._tenant_token()).build()
            body = CreateMessageRequestBody.builder().receive_id(self.settings.chat_id).msg_type(
                "text",
            ).content(json.dumps({"text": _CALLBACK_TIMEOUT_NOTICE}, ensure_ascii=False)).build()
            request = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
            self._require_window()
            _checked(await self.im.message.acreate(request, option))

    def history(self, start: float, end: float) -> Iterator[Message]:
        page = None
        seen_pages = set()
        while True:
            self._require_window()
            request = (
                ListMessageRequest.builder().container_id_type("chat").container_id(self.settings.chat_id)
                .start_time(str(math.floor(start))).end_time(str(math.ceil(end)))
                .sort_type("ByCreateTimeAsc").page_size(50)
            )
            if page:
                request.page_token(page)
            data = _checked(self.im.message.list(request.build())).data
            for item in data.items or ():
                sender = item.sender
                if (
                    item.deleted or item.chat_id != self.settings.chat_id or not sender
                    or sender.sender_type != "user" or not item.body
                ):
                    continue
                keys = (
                    mention.key for mention in item.mentions or ()
                    if mention.key and mention.id_type == "open_id"
                    and mention.id == self.settings.bot_open_id
                )
                try:
                    message = _message(
                        item, sender.id if sender.id_type == "open_id" else "",
                        item.msg_type, item.body.content, keys, self.settings.bot_open_id,
                    )
                except (TypeError, ValueError):
                    continue
                if start <= message.created_ms / 1000 <= end:
                    yield message
            if not data.has_more:
                return
            page = data.page_token
            if not page or page in seen_pages:
                raise RuntimeError("飞书历史分页游标无效。")
            seen_pages.add(page)

    def download(self, message_id: str, attachment: Attachment) -> bytes:
        request = GetMessageResourceRequest.builder().message_id(message_id).file_key(
            attachment.key,
        ).type(attachment.kind)
        self._require_window()
        response = _checked(self.im.message_resource.get(request.build()))
        with response.file as stream:
            return stream.read()


class _Connection(ws.Client):
    """绑定 SDK 帧与当前连接。SDK 升级时需复核这些私有生命周期方法。"""

    def __init__(self, service: "FeishuService"):
        self.service = service
        self._dispatch = ContextVar("feishu_dispatch", default=(None, 0.0, 0.0))
        self.receiver = None
        self.frames = set()
        self._generation = 0
        dispatcher = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._receive)
            .register_p2_card_action_trigger(self._approve)
            .build()
        )
        super().__init__(
            service.settings.app_id, service.settings.app_secret,
            event_handler=dispatcher, log_level=lark.LogLevel.ERROR,
        )

    async def _connect(self):
        self.service.transport._require_window()
        async with asyncio.timeout(self.service._remaining()):
            async with self._lock:
                if self._conn is not None:
                    return
                conn_url = await self._discover_endpoint()
                query = parse_qs(urlparse(conn_url).query)
                conn_id, service_id = query[ws.DEVICE_ID][0], query[ws.SERVICE_ID][0]
                self.service.transport._require_window()
                try:
                    self._conn = await ws.websockets.connect(
                        conn_url, close_timeout=1, **ws._ws_connect_kwargs(),
                    )
                except ws.InvalidHandshake as error:
                    ws._parse_ws_conn_exception(error)
                self._conn_url, self._conn_id, self._service_id = conn_url, conn_id, service_id
                self.service.transport._require_window()
                self._generation += 1
                self.service._connected(self._conn)
                self.receiver = asyncio.create_task(self._receive_message_loop())

    async def _discover_endpoint(self) -> str:
        # 固定版本的 SDK 在这里直接调用无超时的 requests.post。
        headers = {"locale": "zh", ws.USER_AGENT: self._user_agent}
        body = {"AppID": self._app_id, "AppSecret": self._app_secret}
        limit = min(_HTTP_TIMEOUT, self.service._remaining())
        try:
            async with asyncio.timeout(limit):
                async with httpx.AsyncClient(timeout=httpx.Timeout(limit, connect=limit)) as client:
                    response = await client.post(self._domain + ws.GEN_ENDPOINT_URI, headers=headers, json=body)
        except (TimeoutError, httpx.TransportError):
            raise _EndpointUnavailable(0, "飞书长连接地址暂不可用。") from None
        if response.status_code in (HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.TOO_MANY_REQUESTS) or (
            500 <= response.status_code < 600
        ):
            raise _EndpointUnavailable(response.status_code, "飞书长连接地址暂不可用。")
        if response.status_code != HTTPStatus.OK:
            raise ws.ClientException(response.status_code, "飞书长连接地址请求失败。")
        endpoint = ws.JSON.unmarshal(response.text, ws.EndpointResp)
        if endpoint.code in (ws.SYSTEM_BUSY, ws.INTERNAL_ERROR):
            raise _EndpointUnavailable(endpoint.code, "飞书长连接地址暂不可用。")
        if endpoint.code != ws.OK:
            raise ws.ClientException(endpoint.code, "飞书长连接地址请求被拒绝。")
        if endpoint.data.ClientConfig is not None:
            self._configure(endpoint.data.ClientConfig)
        return endpoint.data.URL

    async def _disconnect(self):
        try:
            self.service._disconnected()
        finally:
            await super()._disconnect()

    async def _receive_message_loop(self):
        self.receiver = asyncio.current_task()
        try:
            while self._conn is not None:
                origin = self._conn
                msg = await origin.recv()
                dispatch = self._frame_timing(origin)
                task = asyncio.create_task(self._handle_message(msg, dispatch))
                self.frames.add(task)
                task.add_done_callback(self.frames.discard)
        except Exception:
            try:
                await self._disconnect()
                if self._auto_reconnect:
                    await self._reconnect()
                else:
                    _LOG.error("feishu_connection_receive_failed")
            except Exception:
                _LOG.error("feishu_connection_receive_failed")

    async def _ping_loop(self):
        while True:
            try:
                if self._conn is not None:
                    frame = ws._new_ping_frame(int(self._service_id))
                    await self._write_message(frame.SerializeToString())
            except Exception:
                _LOG.error("feishu_connection_ping_failed")
            await asyncio.sleep(self._ping_interval)

    def _frame_timing(self, origin):
        received = time.monotonic()
        return origin, received, received + _CALLBACK_BUDGET - self.service._callback_response_margin

    async def _handle_message(self, msg, dispatch=None):
        if dispatch is None:
            dispatch = self._frame_timing(self._conn)
        origin, _, _ = dispatch
        if origin is None:
            return
        task = asyncio.current_task()
        self.frames.add(task)
        token = self._dispatch.set(dispatch)
        try:
            await super()._handle_message(msg)
        finally:
            self._dispatch.reset(token)
            self.frames.discard(task)

    async def _handle_data_frame(self, frame):
        # 沿用 SDK 帧格式和分发器，在编码应答前等待审批回调。
        origin, started, _ = self._dispatch.get()
        headers = frame.headers
        frame_type = ws._get_by_key(headers, ws.HEADER_TYPE)
        if frame_type not in (ws.MessageType.EVENT.value, ws.MessageType.CARD.value):
            return
        payload = frame.payload
        count = int(ws._get_by_key(headers, ws.HEADER_SUM))
        if count > 1:
            payload = self._combine(
                ws._get_by_key(headers, ws.HEADER_MESSAGE_ID), count,
                int(ws._get_by_key(headers, ws.HEADER_SEQ)), payload,
            )
            if payload is None:
                return

        response = ws.Response(code=HTTPStatus.OK)
        try:
            if frame_type == ws.MessageType.CARD.value:
                header = json.loads(payload).get("header")
                if not isinstance(header, dict) or header.get("event_type") != "card.action.trigger":
                    raise ValueError("卡片帧缺少卡片回调类型。")
            result = self._event_handler._do_without_validation(payload)
            if result is not None:
                response.data = base64.b64encode(ws.JSON.marshal(await result).encode())
        except Exception:
            _LOG.error("feishu_event_dispatch_failed")
            if frame_type == ws.MessageType.CARD.value:
                warning = _approval_warning("卡片回调格式无效")
                response.data = base64.b64encode(ws.JSON.marshal(warning).encode())
            else:
                response = ws.Response(code=HTTPStatus.INTERNAL_SERVER_ERROR)
        header = headers.add()
        header.key = ws.HEADER_BIZ_RT
        header.value = str(round((time.monotonic() - started) * 1000))
        frame.payload = ws.JSON.marshal(response).encode()
        remaining = started + _CALLBACK_BUDGET - time.monotonic()
        if remaining <= 0:
            return
        async with asyncio.timeout(remaining):
            async with self._lock:
                if origin is not None and origin is self._conn and origin.close_code is None:
                    await origin.send(frame.SerializeToString())

    def _combine(self, msg_id, count, sequence, data):
        # SDK 分片缓存跨重连保留，给每次连接独立的缓存键。
        return super()._combine(f"{self._generation}:{msg_id}", count, sequence, data)

    def _from_current_app(self, data) -> bool:
        origin, _, _ = self._dispatch.get()
        tenant_key = self.service.settings.tenant_key
        return (
            origin is not None and origin is self._conn and origin is self.service._origin
            and origin.close_code is None and data.header is not None
            and data.header.app_id == self.service.settings.app_id
            and (tenant_key is None or data.header.tenant_key == tenant_key)
        )

    def _receive(self, data) -> None:
        if not self._from_current_app(data) or not data.event:
            return
        message, sender = data.event.message, data.event.sender
        if (
            not message or message.chat_type != "group" or not sender
            or sender.sender_type != "user" or not sender.sender_id
        ):
            return
        keys = (
            mention.key for mention in message.mentions or ()
            if mention.key and mention.id and mention.id.open_id == self.service.settings.bot_open_id
        )
        try:
            incoming = _message(
                message, sender.sender_id.open_id, message.message_type, message.content,
                keys, self.service.settings.bot_open_id,
            )
        except (TypeError, ValueError):
            return
        self.service.entrance.inject_message(incoming)

    async def _approve(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        if not self._from_current_app(data):
            return _approval_warning("审批来源与当前飞书连接配置不符")
        origin, _, deadline = self._dispatch.get()
        try:
            return await self.service._record_action(data, origin, deadline)
        except TimeoutError:
            self.service._notify_callback_timeout()
            return _approval_warning(_CALLBACK_TIMEOUT_NOTICE)
        except Exception:
            _LOG.error("feishu_approval_record_failed")
            return _approval_warning("暂时无法记录审批决定")


class FeishuService:
    """供编排者提供业务处理函数，从主线程启动单个 SDK 长连接。"""

    def __init__(
        self, settings: Settings, window: Window, state_path: Path,
        process: Callable[[Application], Report], *, clock: Callable[[], float] = time.time,
        membership_check: Callable[[str], Awaitable[bool]] | None = None,
        callback_response_margin: float = _CALLBACK_RESPONSE_MARGIN,
    ):
        if not 0 < callback_response_margin < _CALLBACK_BUDGET:
            raise ValueError("飞书卡片回调的应答预留时间无效。")
        self.settings, self.window, self.clock = settings, window, clock
        self._lock = Lock()
        self._origin = None
        self._connected_at: float | None = None
        self._membership_check = membership_check
        self._callback_response_margin = callback_response_margin
        self._callback_notices = set()
        self._callback_notice_failures = 0
        self._state = State(state_path, settings)
        self._state.close_approvals()
        self._callback_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="feishu-callback")
        # SDK 和 HTTP 请求日志可能包含响应原文或消息标识。
        logging.getLogger("Lark").disabled = True
        logging.getLogger("httpx").disabled = True
        self.transport = _Transport(settings, window, clock)
        self.entrance = Entrance(settings, window, self._state, self.transport, process, clock=clock)
        self._client = _Connection(self)

    def _connected(self, origin) -> None:
        with self._lock:
            self._connected_at = self.clock()
            self._state.close_approvals()
            self._origin = origin
        _LOG.info("feishu_connected")
        self.entrance.recover()

    def _disconnected(self) -> None:
        with self._lock:
            was_connected = self._origin is not None
            self._origin = None
            self._connected_at = None
            self._state.close_approvals()
        if was_connected:
            _LOG.info("feishu_disconnected")

    def publish_approval(self, record_id: str, summary: str) -> str:
        """返回一次性审批标识。调用方须先核对台账与当次验收授权。"""
        if not record_id.strip():
            raise ValueError("缺少台账记录标识。")
        with self._lock:
            origin = self._origin
            if origin is None or origin.close_code is not None or not self.window.contains(self.clock()):
                raise RuntimeError("审批长连接不可用。")
        approval_id = token_urlsafe(32)
        message_id = self.transport.send_card(approval_card(summary, approval_id, record_id))
        with self._lock:
            if (
                self._origin is not origin or origin.close_code is not None
                or not self.window.contains(self.clock())
            ):
                raise RuntimeError("发送审批卡片期间审批已关闭。")
            self._state.bind(approval_id, message_id, record_id, self.window.end.timestamp())
        return approval_id

    async def _record_action(
        self, data: P2CardActionTrigger, origin, deadline: float,
    ) -> P2CardActionTriggerResponse:
        if time.monotonic() >= deadline:
            raise TimeoutError
        event = data.event
        if not event or not event.context or not event.action or not event.operator:
            return _approval_warning("审批回调缺少必要信息")
        context, value = event.context, event.action.value
        if context.open_chat_id != self.settings.chat_id or not isinstance(value, dict):
            return _approval_warning("审批群或卡片信息不符")
        approval_id, record_id, decision = (value.get(key) for key in ("approval_id", "record_id", "decision"))
        event_id, operator_id = data.header.event_id, event.operator.open_id
        if decision not in ("approve", "reject") or not all(
            isinstance(item, str) and item.strip()
            for item in (context.open_message_id, approval_id, record_id, event_id, operator_id)
        ):
            return _approval_warning("审批回调缺少有效标识或决定")
        raw_time = data.header.create_time
        if isinstance(raw_time, str) and re.fullmatch(r"[0-9]+", raw_time):
            try:
                raw_time = int(raw_time)
            except ValueError:
                return _approval_warning("审批回调时间缺失或格式无效")
        if type(raw_time) is not int:
            return _approval_warning("审批回调时间缺失或格式无效")
        now = self.clock()
        oldest = now - self.settings.callback_max_age
        candidates = [
            raw_time / scale for scale in (1, 1_000, 1_000_000)
            if oldest * scale <= raw_time <= now * scale
        ]
        created_at = candidates[0] if len(candidates) == 1 else None
        connected_at = self._connected_at

        def current_time() -> float | None:
            now = self.clock()
            if (
                origin is not None and origin is self._origin and origin.close_code is None
                and connected_at is not None and connected_at == self._connected_at
                and created_at is not None and self.window.contains(now) and self.window.contains(created_at)
                and connected_at <= created_at <= now
                and now - created_at <= self.settings.callback_max_age
            ):
                return now
            return None

        if current_time() is None:
            if origin is self._origin and not self.window.contains(self.clock()):
                await self._write_callback(deadline, self._state.close_approvals)
            return _approval_warning("审批回调已过期或与当前连接时间不符")
        if not await self._write_callback(deadline, self._state.claim_callback, event_id):
            return _approval_warning("该审批回调已处理")
        remaining = min(
            _HTTP_TIMEOUT, deadline - time.monotonic(),
            self.window.end.timestamp() - self.clock(),
        )
        if time.monotonic() >= deadline:
            raise TimeoutError
        try:
            async with asyncio.timeout(remaining):
                check = self._membership_check or self.transport.is_member
                is_member = await check(operator_id)
        except Exception:
            is_member = False
        if time.monotonic() >= deadline:
            raise TimeoutError
        if is_member is not True:
            _LOG.warning("feishu_approval_membership_rejected")
            return _approval_warning("未能确认点击人是汇报群成员")
        if not self.window.contains(self.clock()):
            await self._write_callback(deadline, self._state.close_approvals)
            return _approval_warning("审批验收窗口已关闭")
        if current_time() is None:
            return _approval_warning("审批回调已过期或连接已断开")
        response = P2CardActionTriggerResponse({
            "toast": {"type": "success", "content": "已记录审批决定"},
            "card": {"type": "raw", "data": decided_approval_card(decision)},
        })
        if await self._write_callback(
            deadline, self._state.decide, approval_id, context.open_message_id, record_id,
            decision, event_id, operator_id, current_time,
        ):
            return response
        return _approval_warning("卡片绑定不符、已过期或已处理")

    async def _write_callback(self, deadline, operation, *args):
        if time.monotonic() >= deadline:
            raise TimeoutError
        commit = Future()

        def before_commit():
            if not commit.set_running_or_notify_cancel():
                raise TimeoutError

        write = asyncio.wrap_future(self._callback_writer.submit(
            operation, *args, deadline=deadline, before_commit=before_commit,
        ))
        try:
            done, _ = await asyncio.wait((write,), timeout=max(0, deadline - time.monotonic()))
            # 先取消提交资格才能拒绝。已进入提交阶段时，以 SQLite 的实际结果为准。
            if not done and commit.cancel():
                raise TimeoutError
            return await write
        finally:
            if not write.done() and commit.cancel():
                write.cancel()

    def _notify_callback_timeout(self) -> None:
        async def notify():
            try:
                await self.transport.notify_callback_timeout()
            except Exception:
                self._callback_notice_failures += 1

        task = asyncio.create_task(notify())
        self._callback_notices.add(task)
        task.add_done_callback(self._callback_notices.discard)

    def _remaining(self) -> float:
        return self.window.end.timestamp() - self.clock()

    async def _serve(self) -> None:
        try:
            async with asyncio.timeout(self._remaining()):
                delay = 0.25
                while self._remaining() > 0:
                    try:
                        await self._client._connect()
                    except _EndpointUnavailable:
                        await asyncio.sleep(min(delay, max(0, self._remaining())))
                        delay = min(2, delay * 2)
                    else:
                        break
                if self._remaining() <= 0:
                    return
                ping = asyncio.create_task(self._client._ping_loop())
                try:
                    await asyncio.sleep(max(0, self._remaining()))
                finally:
                    ping.cancel()
                    await asyncio.gather(ping, return_exceptions=True)
        except TimeoutError:
            if self._remaining() > 0:
                raise

    async def _stop(self) -> None:
        if self._client.receiver is not None:
            self._client.receiver.cancel()
            await asyncio.gather(self._client.receiver, return_exceptions=True)
        frames = tuple(self._client.frames)
        for frame in frames:
            frame.cancel()
        await asyncio.gather(*frames, return_exceptions=True)
        self._callback_writer.shutdown(wait=False, cancel_futures=True)
        notices = tuple(self._callback_notices)
        for notice in notices:
            notice.cancel()
        await asyncio.gather(*notices, return_exceptions=True)
        cron = self._client._cache._cron
        cron.cancel()
        try:
            if self._client._conn is not None or self._origin is not None:
                await self._client._disconnect()
        finally:
            await asyncio.gather(cron, return_exceptions=True)

    def run(self) -> None:
        """阻塞到窗口关闭；进程内离线注入使用 Entrance.inject_message。"""
        serving = ws.loop.create_task(self._serve())
        try:
            ws.loop.run_until_complete(serving)
        except KeyboardInterrupt:
            pass
        except Exception:
            raise RuntimeError("飞书长连接运行失败。") from None
        finally:
            serving.cancel()
            ws.loop.run_until_complete(asyncio.gather(serving, return_exceptions=True))
            try:
                ws.loop.run_until_complete(self._stop())
            finally:
                try:
                    self.entrance.close()
                finally:
                    self._state.close()
