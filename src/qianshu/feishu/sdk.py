"""飞书官方 SDK 适配。只通过本应用长连接分发审批事件。"""

import asyncio
import base64
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Iterator
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

from .cards import Report, approval_card
from .entrance import Application, Attachment, Entrance, Message
from .settings import Settings, Window
from .state import State

_LOG = logging.getLogger(__name__)
_HTTP_TIMEOUT = 2


class _EndpointUnavailable(ws.ServerException):
    """取址暂时失败，可以在窗口内重试。"""


def _checked(response):
    if not response.success():
        raise RuntimeError("飞书 API 请求失败。")
    return response


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
                token = await self._reply_token()
                self._require_window()
                if time.monotonic() >= deadline:
                    raise TimeoutError
                option = lark.RequestOption.builder().tenant_access_token(token).build()
                _checked(await self.im.message.areply(request, option))

        asyncio.run(send())

    async def _reply_token(self) -> str:
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
            # SDK 的异步 REST 方法也会同步获取租户令牌。
            response = await asyncio.to_thread(self.im.chat_members.get, request.build())
            data = _checked(response).data
            if data.trigger_security_conf_limit or type(data.has_more) is not bool:
                return False
            if any(member.member_id_type == "open_id" and member.member_id == operator_id
                   for member in data.items or ()):
                return True
            if not data.has_more:
                return False
            page = data.page_token
            if not page or page in seen_pages:
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
        self.dispatch_origin = ContextVar("feishu_dispatch_origin", default=None)
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
            await super()._receive_message_loop()
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

    async def _handle_message(self, msg):
        # SDK 在接收循环的同一事件循环中调度帧。
        origin = self._conn
        if origin is None:
            return
        task = asyncio.current_task()
        self.frames.add(task)
        token = self.dispatch_origin.set(origin)
        try:
            await super()._handle_message(msg)
        finally:
            self.dispatch_origin.reset(token)
            self.frames.discard(task)

    async def _handle_data_frame(self, frame):
        # 沿用 SDK 帧格式和分发器，在编码应答前等待审批回调。
        headers = frame.headers
        if ws._get_by_key(headers, ws.HEADER_TYPE) != ws.MessageType.EVENT.value:
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

        started = time.monotonic()
        response = ws.Response(code=HTTPStatus.OK)
        try:
            result = self._event_handler._do_without_validation(payload)
            if result is not None:
                response.data = base64.b64encode(ws.JSON.marshal(await result).encode())
        except Exception:
            _LOG.error("feishu_event_dispatch_failed")
            response = ws.Response(code=HTTPStatus.INTERNAL_SERVER_ERROR)
        header = headers.add()
        header.key = ws.HEADER_BIZ_RT
        header.value = str(round((time.monotonic() - started) * 1000))
        frame.payload = ws.JSON.marshal(response).encode()
        origin = self.dispatch_origin.get()
        async with self._lock:
            if origin is not None and origin is self._conn and origin.close_code is None:
                await origin.send(frame.SerializeToString())

    def _combine(self, msg_id, count, sequence, data):
        # SDK 分片缓存跨重连保留，给每次连接独立的缓存键。
        return super()._combine(f"{self._generation}:{msg_id}", count, sequence, data)

    def _from_current_app(self, data) -> bool:
        origin = self.dispatch_origin.get()
        return (
            origin is not None and origin is self._conn and origin.close_code is None
            and data.header is not None and data.header.app_id == self.service.settings.app_id
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
        accepted = False
        try:
            if self._from_current_app(data):
                accepted = await self.service._record_action(data, self.dispatch_origin.get())
        except Exception:
            _LOG.error("feishu_approval_record_failed")
        return P2CardActionTriggerResponse({
            "toast": {
                "type": "success" if accepted else "warning",
                "content": "已记录审批决定" if accepted else "审批无效或已处理",
            },
        })


class FeishuService:
    """供编排者提供业务处理函数，从主线程启动单个 SDK 长连接。"""

    def __init__(
        self, settings: Settings, window: Window, state_path: Path,
        process: Callable[[Application], Report], *, clock: Callable[[], float] = time.time,
        membership_check: Callable[[str], Awaitable[bool]] | None = None,
    ):
        self.settings, self.window, self.clock = settings, window, clock
        self._lock = Lock()
        self._origin = None
        self._membership_check = membership_check
        self._state = State(state_path, settings)
        self._state.close_approvals()
        # SDK 和 HTTP 请求日志可能包含响应原文或消息标识。
        logging.getLogger("Lark").disabled = True
        logging.getLogger("httpx").disabled = True
        self.transport = _Transport(settings, window, clock)
        self.entrance = Entrance(settings, window, self._state, self.transport, process, clock=clock)
        self._client = _Connection(self)

    def _connected(self, origin) -> None:
        with self._lock:
            self._state.close_approvals()
            self._origin = origin
        _LOG.info("feishu_connected")
        self.entrance.recover()

    def _disconnected(self) -> None:
        with self._lock:
            was_connected = self._origin is not None
            self._origin = None
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

    async def _record_action(self, data, origin) -> bool:
        event = data.event
        if not event or not event.context or not event.action or not event.operator:
            return False
        context, value = event.context, event.action.value
        if context.open_chat_id != self.settings.chat_id or not isinstance(value, dict):
            return False
        approval_id, record_id, decision = (value.get(key) for key in ("approval_id", "record_id", "decision"))
        event_id, operator_id = data.header.event_id, event.operator.open_id
        if decision not in ("approve", "reject") or not all(
            isinstance(item, str) and item.strip()
            for item in (context.open_message_id, approval_id, record_id, event_id, operator_id)
        ):
            return False
        with self._lock:
            now = self.clock()
            if origin is None or origin is not self._origin or origin.close_code is not None:
                return False
            if not self.window.contains(now):
                self._state.close_approvals()
                return False
        try:
            remaining = self.window.end.timestamp() - now
            async with asyncio.timeout(min(_HTTP_TIMEOUT, remaining)):
                check = self._membership_check or self.transport.is_member
                is_member = await check(operator_id)
        except Exception:
            is_member = False
        if is_member is not True:
            _LOG.warning("feishu_approval_membership_rejected")
            return False
        with self._lock:
            now = self.clock()
            if origin is None or origin is not self._origin or origin.close_code is not None:
                return False
            if not self.window.contains(now):
                self._state.close_approvals()
                return False
            return self._state.decide(
                approval_id, context.open_message_id, record_id, decision, event_id, operator_id, now,
            )

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
        cron = self._client._cache._cron
        cron.cancel()
        try:
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
                self.entrance.close()
