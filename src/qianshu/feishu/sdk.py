"""飞书官方 SDK 适配。只通过本应用长连接分发审批事件。"""

import asyncio
import base64
import fcntl
import json
import logging
import math
import re
import socket
import struct
import termios
import time
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from contextvars import ContextVar
from http import HTTPStatus
from pathlib import Path
from secrets import token_urlsafe
from threading import Event, Lock
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlparse
from uuid import NAMESPACE_OID, uuid5

import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageResponse, GetChatMembersResponse,
    CreateMessageReactionRequest, CreateMessageReactionRequestBody,
    CreateMessageRequest, CreateMessageRequestBody, Emoji,
    GetChatMembersRequest, GetMessageResourceRequest, ListMessageRequest,
    ReplyMessageRequest, ReplyMessageRequestBody, ReplyMessageResponse,
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
from websockets.uri import parse_uri

from .frame_budget import FrameBudget, MESSAGE, bounded_wire, current_app
from .cards import Report, approval_card, decided_approval_card
from .entrance import Application, Attachment, Entrance, Message
from .settings import Settings, Window
from .state import CALLBACK_BUDGET as _CALLBACK_BUDGET, CALLBACK_RESPONSE_MARGIN as _CALLBACK_RESPONSE_MARGIN
from .state import ProofReceipt, State, callback_receipt

_LOG = logging.getLogger(__name__)
_HTTP_TIMEOUT = 2
_CALLBACK_TIMEOUT_NOTICE = "处理超时，请重新点击"
_CREATE_SEND_MARGIN = 10


class ApprovalClosed(RuntimeError):
    """审批无法再用于建单。"""


class _DecisionRecorded(Exception):
    """证明已落盘，等待方尚未取得资格状态更新的结果。"""


class _UnconfirmedDecision(Exception):
    """候选已持久化，原回调无法确认决定。"""


class _EndpointUnavailable(ws.ServerException):
    """取址暂时失败，可以在窗口内重试。"""


class _ReplyNotSent(RuntimeError):
    """原消息的回复请求还未交给飞书发送接口。"""


def _checked(response):
    if not response.success():
        raise RuntimeError("飞书 API 请求失败。")
    return response


def _approval_warning(content: str) -> P2CardActionTriggerResponse:
    return P2CardActionTriggerResponse({"toast": {"type": "warning", "content": content}})


def _message(
    message, sender_id: str, kind: str, content: str, bot_keys, bot_open_id: str,
    *, sender_type: str = "user",
) -> Message:
    keys = tuple(bot_keys)
    if kind == "post" and keys:
        keys += (bot_open_id,)
    return Message(
        message.message_id or "", message.chat_id or "", sender_id or "",
        int(message.create_time), kind, content, keys, sender_type,
    )


def _before_send(target, outlet, value):
    check = getattr(target, "before_send", None)
    if check is not None:
        check(outlet, value)


class CheckedHTTPTransport(httpx.BaseTransport, httpx.AsyncBaseTransport):
    """在 HTTPX 交付最终请求时执行检查。仅用于无代理、无 mounts 的建单客户端。"""

    def __init__(self, client, check):
        # HTTPX 没有公开的已创建客户端传输装饰器。保留客户端原有的发送和关闭行为。
        if any(transport is not None for transport in client._mounts.values()):
            raise RuntimeError("建单传输不能使用额外路由。")
        self.transport = client._transport
        self.check = check
        client._transport = self

    def handle_request(self, request):
        self.check(request)
        return self.transport.handle_request(request)

    async def handle_async_request(self, request):
        self.check(request)
        return await self.transport.handle_async_request(request)

    def close(self):
        self.transport.close()

    async def aclose(self):
        await self.transport.aclose()

    def __exit__(self, *exc):
        return self.transport.__exit__(*exc)

    async def __aexit__(self, *exc):
        return await self.transport.__aexit__(*exc)


class _OrderHTTP:
    """建单专用 SDK 请求适配，只开放审批所需的消息、成员和令牌接口。"""

    def __init__(self, config, check):
        self.config = config
        self.check = check
        self.cached_token = (None, 0.0)

    def check_request(self, request):
        purpose, token = request.extensions.pop("order_approval")
        self.check("http", (request, purpose, token))

    def request(self, client, model, purpose, token=None):
        path = model.uri
        for key, value in (model.paths or {}).items():
            path = path.replace(":" + key, quote(value, safe=""))
        body = None if model.body is None else json.loads(lark.JSON.marshal(model.body))
        request = client.build_request(model.http_method.name, self.config.domain + path,
                                       params=model.queries, json=body,
                                       headers={"Authorization": "Bearer " + token} if token else {})
        request.extensions["order_approval"] = purpose, token
        return request

    def token_request(self):
        body = CreateTokenRequestBodyBuilder(CreateTokenRequestBody()).app_id(
            self.config.app_id).app_secret(self.config.app_secret).build()
        return CreateSelfTenantTokenRequestBuilder(CreateSelfTenantTokenRequest()).request_body(body).build()

    @staticmethod
    def result(response, model):
        response.raise_for_status()
        return _checked(lark.JSON.unmarshal(response.text, model))

    def token(self, response):
        result = self.result(response, AccessTokenResponse)
        token = result.tenant_access_token
        if not token or type(result.expire) is not int or result.expire <= 0:
            raise RuntimeError("飞书未返回有效租户令牌。")
        self.check("tenant_credential", token)
        self.cached_token = token, time.monotonic() + result.expire - 600
        return token

    def valid_token(self):
        token, deadline = self.cached_token
        return token if time.monotonic() < deadline else None

    def create(self, model):
        with httpx.Client(timeout=_HTTP_TIMEOUT, trust_env=False) as client:
            CheckedHTTPTransport(client, self.check_request)
            token = self.valid_token() or self.token(client.send(self.request(client, self.token_request(), "token")))
            return self.result(client.send(self.request(client, model, "message", token)), CreateMessageResponse)

    async def tenant_token(self):
        token = self.valid_token()
        if token:
            return token
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, trust_env=False) as client:
            CheckedHTTPTransport(client, self.check_request)
            return self.token(await client.send(self.request(client, self.token_request(), "token")))

    async def async_request(self, model, option, purpose, response_type):
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, trust_env=False) as client:
            CheckedHTTPTransport(client, self.check_request)
            request = self.request(client, model, purpose, option.tenant_access_token)
            return self.result(await client.send(request), response_type)

    async def acreate(self, model, option):
        return await self.async_request(model, option, "message", CreateMessageResponse)

    async def aget(self, model, option):
        return await self.async_request(model, option, "members", GetChatMembersResponse)


class _Transport:
    def __init__(self, settings: Settings, window: Window | None, clock: Callable[[], float],
                 http_check=None):
        self.settings, self.window, self.clock = settings, window, clock
        self.im = (
            lark.Client.builder().app_id(settings.app_id).app_secret(settings.app_secret)
            .log_level(lark.LogLevel.ERROR).timeout(_HTTP_TIMEOUT).enable_set_token(True).build().im.v1
        )

        self.order_http = _OrderHTTP(self.im.message.config, http_check) if http_check else None
        if self.order_http:
            self.im = SimpleNamespace(message=self.order_http, chat_members=self.order_http)

    def _require_window(self) -> None:
        if self.window is not None and not self.window.contains(self.clock()):
            raise RuntimeError("飞书验收窗口已关闭。")

    def react(self, message_id: str, *, deadline: float | None = None) -> None:
        body = CreateMessageReactionRequestBody.builder().reaction_type(
            Emoji.builder().emoji_type("OK").build(),
        ).build()
        request = CreateMessageReactionRequest.builder().message_id(message_id).request_body(body).build()
        self._require_window()
        if deadline is None:
            _checked(self.im.message_reaction.create(request))
            return

        async def send():
            async with asyncio.timeout(deadline - time.monotonic()):
                token = await self._tenant_token()
                if time.monotonic() >= deadline:
                    raise TimeoutError
                option = lark.RequestOption.builder().tenant_access_token(token).build()
                _checked(await self.im.message_reaction.acreate(request, option))

        asyncio.run(send())

    def reply(self, message_id: str, text: str, *, deadline: float, purpose: str = "") -> None:
        dedup_key = f"qianshu-handoff:{purpose}:{message_id}" if purpose else message_id
        body = ReplyMessageRequestBody.builder().msg_type("text").content(
            json.dumps({"text": text}, ensure_ascii=False),
        ).uuid(
            str(uuid5(NAMESPACE_OID, dedup_key)),
        ).build()
        request = ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
        attempted = False

        async def trace(event: str, info: dict) -> None:
            nonlocal attempted
            # 代理 CONNECT 也会写请求头，只有实际回复的 POST 才占用幂等期限。
            if (event in ("http11.send_request_headers.started", "http2.send_request_headers.started")
                and info["request"].method == b"POST"):
                attempted = True

        async def send():
            self._require_window()
            remaining = deadline - time.monotonic()
            if self.window is not None:
                remaining = min(remaining, self.window.end.timestamp() - self.clock())
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                token = await self._tenant_token()
                self._require_window()
                if time.monotonic() >= deadline:
                    raise TimeoutError
                if purpose:
                    response = _checked(await self._handoff_reply(request, token, trace))
                    if not response.data or not response.data.message_id:
                        raise RuntimeError("飞书未确认转交回复发送成功。")
                else:
                    option = lark.RequestOption.builder().tenant_access_token(token).build()
                    _checked(await self.im.message.areply(request, option))

        try:
            asyncio.run(send())
        except Exception:
            if purpose and not attempted:
                raise _ReplyNotSent("飞书回复请求尚未发出。") from None
            raise

    async def _handoff_reply(
        self, request: ReplyMessageRequest, token: str, trace: Callable[[str, dict], Awaitable[None]],
    ) -> ReplyMessageResponse:
        # SDK 的异步回复接口不接收 HTTPX 请求扩展。
        if getattr(self, "order_http", None):
            raise RuntimeError("建单入口只接受审批操作。")
        path = request.uri.replace(":message_id", quote(request.message_id, safe=""))
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = await client.post(
                self.im.message.config.domain + path,
                headers={"Authorization": f"Bearer {token}"},
                json=json.loads(lark.JSON.marshal(request.request_body)),
                extensions={"trace": trace},
            )
        response.raise_for_status()
        return lark.JSON.unmarshal(response.text, ReplyMessageResponse)

    async def _tenant_token(self) -> str:
        # 复用 SDK 1.8 的令牌缓存和请求模型，用可取消的异步传输处理缓存未命中。
        if getattr(self, "order_http", None):
            return await self.order_http.tenant_token()
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
        _before_send(self, "token", {"app_id": body.app_id, "app_secret": body.app_secret})
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
            _before_send(self, "members", {"chat_id": self.settings.chat_id, "page_token": page})
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
        _before_send(self, "message", vars(body))
        response = _checked(self.im.message.create(request.build()))
        if not response.data or not response.data.message_id:
            raise RuntimeError("飞书未返回卡片消息标识。")
        return response.data.message_id

    def announce(self, text: str) -> None:
        # 到期关闭和 D15 的读回结果也须发往同一汇报群。
        body = CreateMessageRequestBody.builder().receive_id(self.settings.chat_id).msg_type(
            "text",
        ).content(json.dumps({"text": text}, ensure_ascii=False)).build()
        request = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
        _before_send(self, "message", vars(body))
        _checked(self.im.message.create(request))

    async def notify_callback_timeout(self, notice=_CALLBACK_TIMEOUT_NOTICE) -> None:
        async with asyncio.timeout(_HTTP_TIMEOUT):
            self._require_window()
            option = lark.RequestOption.builder().tenant_access_token(await self._tenant_token()).build()
            body = CreateMessageRequestBody.builder().receive_id(self.settings.chat_id).msg_type(
                "text",
            ).content(json.dumps({"text": notice}, ensure_ascii=False)).build()
            request = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
            self._require_window()
            _before_send(self, "message", vars(body))
            _checked(await self.im.message.acreate(request, option))

    def _history_items(
        self, container_type: str, container_id: str, *, start: float | None = None,
        end: float | None = None, newest_first: bool = False,
    ):
        page = None
        seen_pages = set()
        while True:
            self._require_window()
            request = (
                ListMessageRequest.builder().container_id_type(container_type).container_id(container_id)
                .sort_type("ByCreateTimeDesc" if newest_first else "ByCreateTimeAsc").page_size(50)
            )
            if start is not None:
                request.start_time(str(math.floor(start)))
            if end is not None:
                request.end_time(str(math.ceil(end)))
            if page:
                request.page_token(page)
            data = _checked(self.im.message.list(request.build())).data
            if type(data.has_more) is not bool:
                raise RuntimeError("飞书未返回有效的历史分页状态。")
            yield from data.items or ()
            if not data.has_more:
                return
            page = data.page_token
            if not page or page in seen_pages:
                raise RuntimeError("飞书历史分页游标无效。")
            seen_pages.add(page)

    def _history_message(self, item, *, include_all_senders: bool = False) -> Message | None:
        if item.deleted or item.chat_id != self.settings.chat_id:
            return None
        sender = item.sender
        sender_type = (sender.sender_type or "unknown") if sender else "unknown"
        if not include_all_senders and (sender_type != "user" or not item.body):
            return None
        expected_type = {"user": "open_id", "app": "app_id"}.get(sender_type)
        sender_id = sender.id if sender and expected_type and sender.id_type == expected_type else ""
        keys = tuple(
            mention.key for mention in item.mentions or ()
            if mention.key and mention.id_type == "open_id"
            and mention.id == self.settings.bot_open_id
        )
        try:
            return _message(
                item, sender_id, item.msg_type, item.body.content if item.body else "",
                keys, self.settings.bot_open_id, sender_type=sender_type,
            )
        except (TypeError, ValueError):
            return None

    def history(
        self, start: float, end: float, *, newest_first: bool = False, include_all_senders: bool = False,
    ) -> Iterator[Message]:
        for item in self._history_items("chat", self.settings.chat_id, start=start, end=end,
                                        newest_first=newest_first):
            message = self._history_message(item, include_all_senders=include_all_senders)
            if message is not None and start <= message.created_ms / 1000 <= end:
                yield message

    def handoff_history(self, start: float, end: float, stop: Event) -> Iterator[Message]:
        # 旧根消息也可能在停机期间收到回复。话题接口不接受起止时间，按倒序读到窗口前。
        visited_threads = set()
        incomplete = False
        for item in self._history_items("chat", self.settings.chat_id, end=end, newest_first=True):
            if stop.is_set():
                return
            message = self._history_message(item)
            if message is not None and start <= message.created_ms / 1000 <= end:
                yield message
            thread_id = getattr(item, "thread_id", None)
            if item.chat_id != self.settings.chat_id or not thread_id or thread_id in visited_threads:
                continue
            visited_threads.add(thread_id)
            try:
                for reply in self._history_items("thread", thread_id, newest_first=True):
                    if stop.is_set():
                        return
                    created = int(reply.create_time) / 1000
                    if created < start:
                        break
                    if created <= end and reply.message_id != item.message_id:
                        message = self._history_message(reply)
                        if message is not None:
                            yield message
            except Exception:
                if stop.is_set():
                    return
                incomplete = True
        if incomplete:
            raise RuntimeError("飞书话题历史补扫不完整。")

    def download(self, message_id: str, attachment: Attachment, *, limit: int | None = None) -> bytes:
        request = GetMessageResourceRequest.builder().message_id(message_id).file_key(
            attachment.key,
        ).type(attachment.kind)
        self._require_window()
        response = _checked(self.im.message_resource.get(request.build()))
        with response.file as stream:
            data = stream.read() if limit is None else stream.read(limit + 1)
        if limit is not None and len(data) > limit:
            raise ValueError("飞书附件超过读取限制。")
        return data


def checked_websocket_connection(url, check):
    expected = parse_uri(url)

    def create(protocol, **options):
        if protocol.uri != expected:
            raise RuntimeError("审批长连接地址已变化。")
        send = protocol.send_request

        def send_request(upgrade):
            if upgrade.path != expected.resource_name:
                raise RuntimeError("审批长连接路径已变化。")
            request = httpx.Request("GET", url, headers=list(upgrade.headers.raw_items()))
            check("http", (request, "websocket", None))
            send(upgrade)

        protocol.send_request = send_request
        return ws.websockets.ClientConnection(protocol, **options)

    return create


class _Connection(ws.Client):
    """绑定 SDK 帧与当前连接。SDK 升级时需复核这些私有生命周期方法。"""

    def __init__(self, service, *, approvals: bool = True):
        self.service = service
        self._approvals = approvals
        self._dispatch = ContextVar("feishu_dispatch", default=(None, 0.0, 0.0))
        self.receiver = None
        self.frames = set()
        self._budget = FrameBudget()
        self.card_drops = self.ack_drops = self.pending_acks = 0
        self._generation = 0
        dispatcher = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(
            self._receive,
        )
        if approvals:
            dispatcher.register_p2_card_action_trigger(self._approve)
        super().__init__(
            service.settings.app_id, service.settings.app_secret,
            event_handler=dispatcher.build(), log_level=lark.LogLevel.ERROR,
        )

    async def _connect(self):
        self.service.transport._require_window()
        async with asyncio.timeout(self.service._remaining()):
            async with self._lock:
                if self._conn is not None:
                    return
                conn_url = await self._discover_endpoint()
                _before_send(self.service, "websocket", conn_url)
                query = parse_qs(urlparse(conn_url).query)
                conn_id, service_id = query[ws.DEVICE_ID][0], query[ws.SERVICE_ID][0]
                self.service.transport._require_window()
                options = ws._ws_connect_kwargs()
                if not self._approvals:
                    options.update(max_size=MESSAGE, max_queue=8, write_limit=65536)
                http_check = getattr(self.service, "http_check", None)
                if http_check:
                    options["create_connection"] = checked_websocket_connection(conn_url, http_check)
                try:
                    self._conn = await ws.websockets.connect(
                        conn_url, close_timeout=1, **options,
                    )
                except ws.InvalidHandshake as error:
                    ws._parse_ws_conn_exception(error)
                self._conn_url, self._conn_id, self._service_id = conn_url, conn_id, service_id
                self.service.transport._require_window()
                self._budget.groups.clear()
                self._generation += 1
                self.service._connected(self._conn)
                self.receiver = asyncio.create_task(self._receive_message_loop())

    async def _discover_endpoint(self) -> str:
        # 固定版本的 SDK 在这里直接调用无超时的 requests.post。
        headers = {"locale": "zh", ws.USER_AGENT: self._user_agent}
        body = {"AppID": self._app_id, "AppSecret": self._app_secret}
        _before_send(self.service, "endpoint", body)
        _before_send(self.service, "endpoint_metadata", [headers, self._domain + ws.GEN_ENDPOINT_URI])
        remaining = self.service._remaining()
        limit = _HTTP_TIMEOUT if remaining is None else min(_HTTP_TIMEOUT, remaining)
        try:
            async with asyncio.timeout(limit):
                http_check = getattr(self.service, "http_check", None)
                options = {"trust_env": False} if http_check else {}
                async with httpx.AsyncClient(timeout=httpx.Timeout(limit, connect=limit), **options) as client:
                    if http_check:
                        CheckedHTTPTransport(client, lambda req: http_check("http", (req, "endpoint", None)))
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
        self._budget.groups.clear()
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
                if not self._approvals:
                    # 同步检查/分发一帧，避免已缓冲的洪峰先创建无界 task。
                    await self._handle_message(msg, dispatch)
                    await asyncio.sleep(0)
                    continue
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
                    encoded = frame.SerializeToString()
                    _before_send(self.service, "ping", encoded)
                    await self._write_message(encoded)
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
        if origin is None or origin is not self._conn:
            return
        if not self._approvals and not bounded_wire(msg):
            self._budget.reject()
            return
        task = asyncio.current_task()
        self.frames.add(task)
        token = self._dispatch.set(dispatch)
        try:
            if self._approvals:
                await super()._handle_message(msg)
            else:
                try:
                    frame = ws.Frame()
                    frame.ParseFromString(msg)
                    if not frame.IsInitialized():
                        self._budget.reject()
                    elif frame.method == ws.FrameType.DATA.value:
                        await self._handle_data_frame(frame)
                    elif frame.method == ws.FrameType.CONTROL.value:
                        await self._handle_control_frame(frame)
                    else:
                        self._budget.reject()
                except Exception:
                    self._budget.reject()
        finally:
            self._dispatch.reset(token)
            self.frames.discard(task)

    async def _handle_data_frame(self, frame):
        # 沿用 SDK 帧格式和分发器，在编码应答前等待审批回调。
        origin, started, deadline = self._dispatch.get()
        if origin is not self._conn:
            return
        headers = frame.headers
        if not self._approvals:
            completed = self._budget.accept(frame, self.service.settings, started, deadline)
            if completed is None:
                return
            frame_type, payload, started, deadline = completed
        else:
            frame_type = ws._get_by_key(headers, ws.HEADER_TYPE)
            if frame_type not in (ws.MessageType.EVENT.value, ws.MessageType.CARD.value):
                return
            payload = frame.payload
            count = int(ws._get_by_key(headers, ws.HEADER_SUM))
            if count > 1:
                payload = self._combine(ws._get_by_key(headers, ws.HEADER_MESSAGE_ID), count,
                                        int(ws._get_by_key(headers, ws.HEADER_SEQ)), payload)
                if payload is None:
                    return

        _before_send(self.service, "callback_headers", [(h.key, h.value) for h in headers])
        response = ws.Response(code=HTTPStatus.OK)
        timing = self._dispatch.set((origin, started, deadline))
        try:
            if not self._approvals:
                self._budget.decoded += 1
                envelope = json.loads(payload)
                header = envelope.get("header") if isinstance(envelope, dict) else None
                event_type = header.get("event_type") if isinstance(header, dict) else None
                message_event = event_type == "im.message.receive_v1"
                bound = origin is self.service._origin and current_app(header, self.service.settings)
                if frame_type == "card" or not (bound and message_event):
                    sidecar = self.service._sidecar
                    if sidecar is not None and bound and event_type == "card.action.trigger":
                        sidecar.offer(payload, started, deadline)
                    self._card_reply(origin, frame, started, ignored_message=message_event)
                    return
            if frame_type == ws.MessageType.CARD.value:
                header = json.loads(payload).get("header")
                if not isinstance(header, dict) or header.get("event_type") != "card.action.trigger":
                    raise ValueError("卡片帧缺少卡片回调类型。")
            result = self._event_handler._do_without_validation(payload)
            if result is not None:
                encoded = ws.JSON.marshal(await result).encode()
                _before_send(self.service, "callback_content", encoded)
                response.data = base64.b64encode(encoded)
        except Exception:
            if not self._approvals:
                self._budget.reject()
                self._card_reply(origin, frame, started)
                return
            _LOG.error("feishu_event_dispatch_failed")
            if frame_type == ws.MessageType.CARD.value:
                warning = _approval_warning("卡片回调格式无效")
                response.data = base64.b64encode(ws.JSON.marshal(warning).encode())
            else:
                response = ws.Response(code=HTTPStatus.INTERNAL_SERVER_ERROR)
        finally:
            self._dispatch.reset(timing)
        header = headers.add()
        header.key = ws.HEADER_BIZ_RT
        header.value = str(round((time.monotonic() - started) * 1000))
        frame.payload = ws.JSON.marshal(response).encode()
        remaining = started + _CALLBACK_BUDGET - time.monotonic()
        if remaining <= 0:
            if not self._approvals:
                self.ack_drops += 1
            return
        encoded = frame.SerializeToString()
        if self._approvals:
            await self._send_ack(origin, encoded, remaining)
        elif self.pending_acks < 8 and len(encoded) <= 8192:
            self.pending_acks += 1
            task = asyncio.create_task(self._queued_ack(origin, encoded, started + _CALLBACK_BUDGET))
            self.frames.add(task)
            task.add_done_callback(self.frames.discard)
        else:
            self.ack_drops += 1

    async def _send_ack(self, origin, encoded, remaining):
        async with asyncio.timeout(remaining):
            async with self._lock:
                if origin is self._conn and origin.close_code is None:
                    _before_send(self.service, "callback_response", encoded)
                    await origin.send(encoded)

    async def _queued_ack(self, origin, encoded, deadline):
        try:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                await self._send_ack(origin, encoded, remaining)
            else:
                self.ack_drops += 1
        except Exception:
            self.ack_drops += 1
        finally:
            self.pending_acks -= 1

    def _drop_card(self):
        self.card_drops += 1
        if self.service._sidecar is not None:
            self.service._sidecar.card_drops = self.card_drops

    def _card_reply(self, origin, frame, started, *, ignored_message=False):
        # 转交 CLI 从未注册卡片处理器，保留 main 的 CARD 警告和 EVENT 500。
        now = time.monotonic()
        transport = getattr(origin, "transport", None)
        if (transport is None or origin is not self._conn or origin.close_code is not None
                or self._lock.locked() or self.pending_acks
                or now >= started + _CALLBACK_BUDGET or self._budget.pending_messages()):
            self._drop_card()
            return
        kind = ws._get_by_key(frame.headers, ws.HEADER_TYPE)
        response = ws.Response(code=HTTPStatus.OK if ignored_message else HTTPStatus.INTERNAL_SERVER_ERROR)
        if kind == "card":
            warning = _approval_warning("卡片回调格式无效")
            response = ws.Response(code=HTTPStatus.OK)
            response.data = base64.b64encode(ws.JSON.marshal(warning).encode())
        header = frame.headers.add()
        header.key, header.value = ws.HEADER_BIZ_RT, str(round((now - started) * 1000))
        frame.payload = ws.JSON.marshal(response).encode()
        encoded = frame.SerializeToString()
        # asyncio 的 TLS 层和底层传输均须有完整余量。单循环中检查到
        # write 之间无 await；Sans-I/O 同步接收整个帧，不进入 drain。
        tls = getattr(transport, "_ssl_protocol", None)
        raw = tls._transport if tls else transport
        if (len(encoded) > 4096 or raw is None or any(
                t.is_closing() or t.get_write_buffer_size() or t.get_write_buffer_limits()[1] < 16384
                for t in (transport, raw))):
            self._drop_card()
            return
        try:
            sock = raw.get_extra_info("socket")
            if (sock is None or sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF) // 2 < 16384
                    or struct.unpack("i", fcntl.ioctl(sock, termios.TIOCOUTQ, bytes(4)))[0]):
                self._drop_card()
                return
            _before_send(self.service, "callback_response", encoded)
            origin.protocol.send_binary(encoded)
            origin.send_data()
        except Exception:
            self._drop_card()

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
        if self.service.entrance is None or not self._from_current_app(data) or not data.event:
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
        if self._approvals:
            self.service.entrance.inject_message(incoming)
        else:
            _, _, deadline = self._dispatch.get()
            self.service.entrance.inject_message(incoming, deadline=deadline)

    async def _approve(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        if not self._from_current_app(data):
            return _approval_warning("审批来源与当前飞书连接配置不符")
        origin, _, deadline = self._dispatch.get()
        try:
            return await self.service._record_action(data, origin, deadline)
        except _DecisionRecorded:
            return P2CardActionTriggerResponse({
                "toast": {"type": "info", "content": "审批决定已记录，处理结果请稍后核对"},
            })
        except _UnconfirmedDecision:
            notice = "审批决定未确认，请人工核对"
            self.service._notify_callback_timeout(notice)
            return _approval_warning(notice)
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
        process: Callable[[Application], Report] | None, *, clock: Callable[[], float] = time.time,
        membership_check: Callable[[str], Awaitable[bool]] | None = None,
        callback_response_margin: float = _CALLBACK_RESPONSE_MARGIN,
        before_send: Callable[[str, object], None] | None = None,
        http_check: Callable[[str, object], None] | None = None,
    ):
        if not 0 < callback_response_margin < _CALLBACK_BUDGET:
            raise ValueError("飞书卡片回调的应答预留时间无效。")
        self.settings, self.window, self.clock = settings, window, clock
        self._lock = Lock()
        self._origin = None
        self._approval_generation = 0
        self._approval_disconnected = Event()
        self._approval_disconnected.set()
        self._connected_at: float | None = None
        self.before_send = before_send
        self.http_check = http_check
        self._membership_check = membership_check
        self._callback_response_margin = callback_response_margin
        self._callback_notices = set()
        self._callback_notice_failures = 0
        _before_send(self, "approval_scope", (settings.app_id, settings.chat_id))
        self._state = State(state_path, settings)
        self._state.close_approvals()
        self._callback_claim_lock = asyncio.Lock()
        self._callback_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="feishu-callback")
        # SDK 和 HTTP 请求日志可能包含响应原文或消息标识。
        logging.getLogger("Lark").disabled = True
        logging.getLogger("httpx").disabled = True
        self.transport = (_Transport(settings, window, clock, http_check) if http_check
                          else _Transport(settings, window, clock))
        self.transport.before_send = before_send
        self.entrance = (
            Entrance(settings, window, self._state, self.transport, process, clock=clock)
            if process is not None else None
        )
        self._machine_lease = ExitStack()
        self._client = _Connection(self)

    def _connected(self, origin) -> None:
        with self._lock:
            self._connected_at = self.clock()
            self._state.close_approvals()
            self._origin = origin
            self._approval_generation += 1
            self._approval_disconnected.clear()
        _LOG.info("feishu_connected")
        if self.entrance is not None:
            self.entrance.recover()

    def _disconnected(self) -> None:
        with self._lock:
            self._approval_disconnected.set()
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
            origin, generation = self._origin, self._approval_generation
            if origin is None or origin.close_code is not None or not self.window.contains(self.clock()):
                raise RuntimeError("审批长连接不可用。")
        approval_id = token_urlsafe(32)
        message_id = self.transport.send_card(approval_card(summary, approval_id, record_id))
        with self._lock:
            if (
                self._origin is not origin or generation != self._approval_generation or origin.close_code is not None
                or not self.window.contains(self.clock())
            ):
                raise RuntimeError("发送审批卡片期间审批已关闭。")
            _before_send(self, "approval_state", (approval_id, message_id, record_id, self.window.end.timestamp()))
            self._state.bind(approval_id, message_id, record_id, self.window.end.timestamp())
        return approval_id

    def approval_connection_ready(self) -> bool:
        origin = self._origin
        return (not self._approval_disconnected.is_set() and origin is not None
                and origin.close_code is None and self.window.contains(self.clock()))

    def approval_status(self, approval_id: str, record_id: str) -> str:
        with self._lock:
            status = self._state.approval_status(approval_id, record_id)
            if status in ("pending", "approve", "claimed") and not self.approval_connection_ready():
                self._state.close_approvals()
                return "closed"
            return status

    def close_create_approval(self, approval_id: str, record_id: str) -> None:
        with self._lock:
            self._state.close_create_approval(approval_id, record_id)

    def send_first_create(self, approval_id, record_id, prepare, record_attempt, send):
        """认领后查询，查询期间可以关闭审批。随后串行记录尝试与交接。"""
        def check(expires):
            now = self.clock()
            if (self._approval_disconnected.is_set() or origin is not self._origin
                    or generation != self._approval_generation or origin.close_code is not None
                    or not self.window.contains(now)
                    or min(expires, self.window.end.timestamp()) - now < _CREATE_SEND_MARGIN):
                raise ApprovalClosed("建单审批已关闭")

        def dispatch(expires):
            check(expires)
            record_attempt()
            send(lambda: check(expires))

        with self._lock:
            if not self.approval_connection_ready():
                return False
            origin, generation = self._origin, self._approval_generation
            expires = self._state.claim_create(approval_id, record_id)
            if expires is None:
                return False
            check(expires)

        prepare()
        with self._lock:
            return self._state.dispatch_create(approval_id, record_id, dispatch)

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
        _before_send(self, "approval_state", (approval_id, record_id, decision, context.open_message_id,
                                              event_id, operator_id))
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
        generation = self._approval_generation

        def current_time() -> float | None:
            now = self.clock()
            if (
                origin is not None and origin is self._origin and origin.close_code is None
                and generation == self._approval_generation and not self._approval_disconnected.is_set()
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
        # 等候去重的帧留在事件循环。已核实成员的决定无需排在整批去重写入之后。
        async with asyncio.timeout(max(0, deadline - time.monotonic())):
            async with self._callback_claim_lock:
                claimed = await self._write_callback(deadline, self._state.claim_callback, event_id)
        if not claimed:
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
        # 两笔决定提交共用处理截止；只有均及时才可借用一半回包余量保存证明。
        receipt = ProofReceipt(deadline + self._callback_response_margin / 2)

        def before_commit():
            if time.monotonic() >= deadline:
                raise TimeoutError

        def record():
            token = callback_receipt.set(receipt)
            try:
                return operation(*args, deadline=deadline, before_commit=before_commit)
            finally:
                callback_receipt.reset(token)

        write = asyncio.wrap_future(self._callback_writer.submit(record))
        try:
            done, _ = await asyncio.wait((write,), timeout=max(0, deadline - time.monotonic()))
            if not done:
                if not receipt.allow_proof_wait():
                    raise TimeoutError
                remaining = max(0, receipt.proof_deadline - time.monotonic())
                done, _ = await asyncio.wait((write,), timeout=remaining)
                if not done:
                    if receipt.end_wait():
                        raise _DecisionRecorded
                    raise TimeoutError
            result = write.result()
            if not result and receipt.review_required:
                raise _UnconfirmedDecision
            return result
        except TimeoutError:
            if receipt.review_required:
                raise _UnconfirmedDecision from None
            raise
        finally:
            receipt.end_wait()
            if not write.done():
                write.cancel()

    def _notify_callback_timeout(self, *notice: str) -> None:
        async def notify():
            try:
                await self.transport.notify_callback_timeout(*notice)
            except Exception:
                self._callback_notice_failures += 1

        task = asyncio.create_task(notify())
        self._callback_notices.add(task)
        task.add_done_callback(self._callback_notices.discard)

    def _remaining(self) -> float:
        return self.window.end.timestamp() - self.clock()

    async def _serve(self) -> None:
        from .handoff import machine_listener

        self._machine_lease.enter_context(machine_listener())
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

    async def _stop(self, *, finish_frames=False) -> None:
        # 真实连接关闭前仍须排斥下一监听器（含收尾中的窗口服务）。
        with self._machine_lease:
            await self._stop_connection(finish_frames)

    async def _stop_connection(self, finish_frames) -> None:
        if self._client.receiver is not None:
            self._client.receiver.cancel()
            await asyncio.gather(self._client.receiver, return_exceptions=True)
        frames = tuple(self._client.frames)
        if finish_frames and frames:
            await asyncio.wait(frames, timeout=_CALLBACK_BUDGET)
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
                    if self.entrance is not None:
                        self.entrance.close()
                finally:
                    self._state.close()

    def run_create(self, work):
        """审批监听在 SDK 事件循环运行，阻塞业务在工作线程运行。"""
        stop = Event()
        serving = ws.loop.create_task(self._serve())

        async def listen():
            try:
                await serving
            except Exception:
                _LOG.error("feishu_create_listener_failed")
            finally:
                # 先关审批资格。持久化清理可能等待证明写锁，原连接仍须按时回包。
                self._approval_disconnected.set()
                if self.window.contains(self.clock()):
                    stop.set()
                await asyncio.to_thread(self._disconnected)

        listener = ws.loop.create_task(listen())
        worker = ws.loop.create_task(asyncio.to_thread(work, stop))
        try:
            return ws.loop.run_until_complete(worker)
        except KeyboardInterrupt:
            with self._lock:
                stop.set()
            ws.loop.run_until_complete(asyncio.gather(worker, return_exceptions=True))
            raise
        finally:
            serving.cancel()
            try:
                ws.loop.run_until_complete(asyncio.gather(listener, return_exceptions=True))
                ws.loop.run_until_complete(self._stop(finish_frames=True))
            finally:
                self._state.close()
