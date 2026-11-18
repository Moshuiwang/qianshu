import asyncio
import base64
import json
import time
from threading import Event
from types import SimpleNamespace

import pytest

from lark_oapi.api.im.v1 import Message as HistoryMessage

from qianshu.feishu import FeishuService, Message, Report
from qianshu.feishu.sdk import TokenManager, ws


class FakeSocket:
    close_code = None

    def __init__(self):
        self.responses = []

    async def recv(self):
        await asyncio.Future()

    async def close(self):
        self.close_code = 1000

    async def send(self, data):
        frame = ws.Frame()
        frame.ParseFromString(data)
        response = json.loads(frame.payload)
        assert response["code"] == 200
        if "data" in response:
            self.responses.append(json.loads(base64.b64decode(response["data"]))["toast"]["type"])
        else:
            self.responses.append("event_ack")


def payload(service, approval_id, message_id, record_id):
    return {
        "schema": "2.0",
        "header": {
            "app_id": service.settings.app_id, "event_id": "<回调占位>",
            "event_type": "card.action.trigger",
        },
        "event": {
            "operator": {"open_id": "<点击人占位>"},
            "context": {"open_chat_id": service.settings.chat_id, "open_message_id": message_id},
            "action": {"value": {
                "approval_id": approval_id, "record_id": record_id, "decision": "approve",
            }},
        },
    }


def dispatch(service, data):
    frame = ws.Frame(method=ws.FrameType.DATA.value, SeqID=0, LogID=0, service=1)
    for key, value in {
        ws.HEADER_TYPE: "event", ws.HEADER_MESSAGE_ID: "<帧占位>", ws.HEADER_TRACE_ID: "<追踪占位>",
        ws.HEADER_SUM: "1", ws.HEADER_SEQ: "0",
    }.items():
        header = frame.headers.add()
        header.key, header.value = key, value
    frame.payload = json.dumps(data).encode()
    # 实际 SDK 帧分发与应答，套接字及 REST 由离线替身提供。
    ws.loop.run_until_complete(service._client._handle_message(frame.SerializeToString()))


@pytest.fixture
def connected(tmp_path, settings, window, monkeypatch):
    loop = asyncio.new_event_loop()
    monkeypatch.setattr(ws, "loop", loop)
    asyncio.set_event_loop(loop)
    clock = SimpleNamespace(now=150)
    service = FeishuService(settings, window, tmp_path / "private.sqlite",
                            lambda app: pytest.fail("意外进入业务处理"), clock=lambda: clock.now)
    monkeypatch.setattr(service.transport, "history", lambda start, end: iter(()))

    async def is_member(operator_id):
        return True

    monkeypatch.setattr(service.transport, "is_member", is_member)
    socket = FakeSocket()
    service._client._conn = socket
    service._connected(socket)
    service.entrance._history.submit(lambda: None).result(timeout=2)
    try:
        yield service, socket, clock
    finally:
        loop.run_until_complete(service._stop())
        service.entrance.close()
        loop.close()
        asyncio.set_event_loop(None)


def publish(connected, monkeypatch, message_id="<审批卡占位>"):
    service, socket, clock = connected

    def send(card):
        buttons = card["elements"][1]["actions"]
        assert [button["text"]["content"] for button in buttons] == ["同意", "驳回"]
        return message_id

    monkeypatch.setattr(service.transport, "send_card", send)
    record_id = "<台账记录占位>"
    token = service.publish_approval(record_id, "【测试】合成单据")
    return payload(service, token, message_id, record_id)


def states(service):
    with service._state.connection() as db:
        return db.execute("SELECT status FROM feishu_approvals ORDER BY rowid").fetchall()


def test_authenticated_decision_and_callback_replay(connected, monkeypatch):
    service, socket, _ = connected
    data = publish(connected, monkeypatch)
    # 直接调用 SDK 分发器，只有相同的 app_id，仍缺当前长连接来源。
    direct = ws.loop.run_until_complete(
        service._client._event_handler._do_without_validation(json.dumps(data).encode()),
    )
    assert direct.toast.type == "warning"
    dispatch(service, data)
    dispatch(service, data)
    data["event"]["action"]["value"]["decision"] = "reject"
    data["header"]["event_id"] = "<另一回调占位>"
    dispatch(service, data)
    assert socket.responses == ["success", "warning", "warning"]
    assert states(service) == [("approve",)]


@pytest.mark.parametrize("outcome", [False, None, "error", "timeout"])
def test_rejects_unconfirmed_membership(connected, monkeypatch, caplog, outcome):
    service, socket, _ = connected
    data = publish(connected, monkeypatch)
    calls = []
    monkeypatch.setattr("qianshu.feishu.sdk._HTTP_TIMEOUT", 0.05)

    async def is_member(operator_id):
        calls.append(operator_id)
        if outcome == "error":
            raise RuntimeError("合成查询故障")
        if outcome == "timeout":
            await asyncio.Future()
        return outcome

    monkeypatch.setattr(service.transport, "is_member", is_member)
    dispatch(service, data)
    assert calls == [data["event"]["operator"]["open_id"]]
    assert socket.responses == ["warning"]
    assert states(service) == [("pending",)]
    assert caplog.messages == ["feishu_approval_membership_rejected"]


def test_membership_checks_configured_group_and_open_id_across_pages(connected, monkeypatch):
    service, socket, _ = connected
    data = publish(connected, monkeypatch)
    operator_id = data["event"]["operator"]["open_id"]
    pages = []

    def get_members(request):
        assert request.chat_id == service.settings.chat_id
        assert request.member_id_type == "open_id"
        pages.append(request.page_token)
        next_page = request.page_token is None
        return SimpleNamespace(success=lambda: True, data=SimpleNamespace(
            items=[SimpleNamespace(member_id_type="user_id" if next_page else "open_id",
                                   member_id=operator_id)],
            has_more=next_page, page_token="<群成员分页占位>", trigger_security_conf_limit=False,
        ))

    monkeypatch.delattr(service.transport, "is_member")
    monkeypatch.setattr(service.transport.im.chat_members, "get", get_members)
    dispatch(service, data)
    assert pages == [None, "<群成员分页占位>"]
    assert socket.responses == ["success"]
    assert states(service) == [("approve",)]


def test_reply_retries_send_the_same_platform_deduplication_key(connected, monkeypatch):
    service, _, _ = connected
    token = "<租户令牌占位>"
    message_id = "<合成拒绝消息占位>"
    requests = []

    async def reply(request, option):
        assert option.tenant_access_token == token
        assert request.message_id == message_id
        assert json.loads(request.body.content) == {"text": "概念验证期不处理"}
        requests.append(request.body.uuid)
        return SimpleNamespace(success=lambda: True)

    monkeypatch.setattr(TokenManager, "get_self_tenant_token", lambda config: token)
    monkeypatch.setattr(service.transport.im.message, "areply", reply)
    for _ in range(2):
        service.transport.reply(message_id, "概念验证期不处理", deadline=time.monotonic() + 3)
    assert len(requests) == 2
    assert requests[0] and requests[0] == requests[1]


@pytest.mark.parametrize("change", ["disconnect", "expiry"])
def test_rechecks_approval_after_membership_lookup(connected, monkeypatch, change):
    service, socket, clock = connected
    data = publish(connected, monkeypatch)

    async def is_member(operator_id):
        if change == "disconnect":
            service._disconnected()
        else:
            clock.now = 200
        return True

    monkeypatch.setattr(service.transport, "is_member", is_member)
    dispatch(service, data)
    assert socket.responses == ["warning"]
    assert states(service) == [("closed",)]


@pytest.mark.parametrize("location,key,replacement", [
    ("context", "open_chat_id", "<他群占位>"),
    ("context", "open_message_id", "<他卡占位>"),
    ("context", "open_message_id", None),
    ("context", "open_chat_id", None),
    ("value", "record_id", "<他记录占位>"),
    ("value", "record_id", None),
    ("value", "approval_id", None),
    ("value", "approval_id", "<另一审批占位>"),
    ("header", "app_id", "<他应用占位>"),
    ("header", "event_id", None),
])
def test_rejects_invalid_context_or_binding(connected, monkeypatch, location, key, replacement):
    service, socket, _ = connected
    data = publish(connected, monkeypatch)
    location = {
        "context": data["event"]["context"], "value": data["event"]["action"]["value"],
        "header": data["header"],
    }[location]
    if replacement is None:
        del location[key]
    else:
        location[key] = replacement
    dispatch(service, data)
    assert socket.responses == ["warning"]
    assert states(service) == [("pending",)]


def test_reject_disconnect_reconnect_and_timeout(connected, monkeypatch):
    service, socket, clock = connected
    rejected = publish(connected, monkeypatch)
    rejected["event"]["action"]["value"]["decision"] = "reject"
    dispatch(service, rejected)
    old = publish(connected, monkeypatch, "<旧卡占位>")
    old["header"]["event_id"] = "<旧回调占位>"
    dispatch(service, old)
    service._disconnected()
    replacement = FakeSocket()
    service._client._conn = replacement
    service._connected(replacement)
    dispatch(service, old)
    late = publish(connected, monkeypatch, "<将到期卡占位>")
    clock.now = 200
    dispatch(service, late)
    assert socket.responses == ["success", "success"]
    assert replacement.responses == ["warning", "warning"]
    assert states(service) == [("reject",), ("closed",), ("closed",)]


def test_live_event_and_paginated_history_deduplicate(connected, monkeypatch):
    service, socket, _ = connected
    settings = service.settings
    sender_id = next(iter(settings.allowed_senders))
    mention_key = "@_user_1"
    content = json.dumps({"text": f"{mention_key} 合成样例 ABC"})
    calls = []
    finished = Event()

    def processed(app):
        calls.append(("work", app.message.message_id))
        return Report("合成来源")

    def send_card(card):
        if len([call for call in calls if call[0] == "work"]) == 2:
            finished.set()

    live_message_id = "<实时消息占位>"
    history_message_id = "<补漏消息占位>"
    monkeypatch.setattr(service.entrance, "process", processed)
    monkeypatch.setattr(service.transport, "react", lambda mid: calls.append(("reaction", mid)))
    monkeypatch.setattr(service.transport, "send_card", send_card)
    dispatch(service, {
        "schema": "2.0",
        "header": {"app_id": settings.app_id, "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": sender_id}},
            "message": {
                "message_id": live_message_id, "chat_id": settings.chat_id, "chat_type": "group",
                "create_time": "120000", "message_type": "text", "content": content,
                "mentions": [{"key": mention_key, "id": {"open_id": settings.bot_open_id}}],
            },
        },
    })

    pages = []

    def list_messages(request):
        pages.append(request.page_token)
        item = HistoryMessage({
            "message_id": history_message_id if request.page_token else live_message_id,
            "chat_id": settings.chat_id, "create_time": 120000, "msg_type": "text",
            "sender": {"id": sender_id, "id_type": "open_id", "sender_type": "user"},
            "body": {"content": content},
            "mentions": [{"key": mention_key, "id": settings.bot_open_id, "id_type": "open_id"}],
        })
        assert request.container_id == settings.chat_id
        return SimpleNamespace(success=lambda: True, data=SimpleNamespace(
            items=[item], has_more=not request.page_token, page_token="<分页占位>",
        ))

    # 调用生产历史适配器，但在 SDK REST 边界返回合成分页。
    monkeypatch.delattr(service.transport, "history")
    monkeypatch.setattr(service.transport.im.message, "list", list_messages)
    service.entrance.recover().result(timeout=2)
    assert finished.wait(2)
    assert socket.responses == ["event_ack"]
    assert pages == [None, "<分页占位>"]
    for mid in (live_message_id, history_message_id):
        assert [kind for kind, message_id in calls if message_id == mid] == ["reaction", "work"]


def test_event_mentioning_another_user_is_ignored(connected, monkeypatch):
    service, socket, _ = connected
    settings = service.settings
    reactions = []
    receipts = []
    inject = service.entrance.inject_message

    def track_receipt(message):
        receipt = inject(message)
        receipts.append(receipt)
        return receipt

    monkeypatch.setattr(service.entrance, "inject_message", track_receipt)
    monkeypatch.setattr(service.transport, "react", reactions.append)
    monkeypatch.setattr(service.transport, "reply", lambda *args, **kwargs: reactions.append(args))
    monkeypatch.setattr(service.transport, "send_card", reactions.append)
    dispatch(service, {
        "schema": "2.0",
        "header": {"app_id": settings.app_id, "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": next(iter(settings.allowed_senders))}},
            "message": {
                "message_id": "<他人提及消息占位>", "chat_id": settings.chat_id, "chat_type": "group",
                "create_time": "120000", "message_type": "text",
                "content": json.dumps({"text": "@_user_1 合成样例 ABC"}),
                "mentions": [{"key": "@_user_1", "id": {"open_id": "<他人占位>"}}],
            },
        },
    })
    for receipt in receipts:
        if receipt is not None:
            work = receipt.result(timeout=2)
            if work is not None:
                work.result(timeout=2)
    assert socket.responses == ["event_ack"]
    assert receipts == [None]
    assert reactions == []


def test_reconnect_recovers_messages_received_during_disconnect(connected, monkeypatch):
    service, first, _ = connected
    client = service._client
    replacement = FakeSocket()
    recovered = Event()
    calls = []

    async def endpoint():
        return f"wss://example.invalid/ws?{ws.DEVICE_ID}=1&{ws.SERVICE_ID}=1"

    async def connect(url, **kwargs):
        return replacement

    message = Message(
        "<断线消息占位>", service.settings.chat_id, next(iter(service.settings.allowed_senders)),
        120_000, "text", json.dumps({"text": "@_user_1 合成样例 ABC"}), ("@_user_1",),
    )

    def history(start, end):
        assert service._origin is replacement
        calls.append((start, end))
        yield message

    async def disconnect_and_reconnect():
        await client._disconnect()
        await client._reconnect()

    monkeypatch.setattr(client, "_discover_endpoint", endpoint)
    monkeypatch.setattr(client, "_reconnect_nonce", 0)
    monkeypatch.setattr(ws.websockets, "connect", connect)
    monkeypatch.setattr(service.transport, "history", history)
    monkeypatch.setattr(service.transport, "react", lambda mid: calls.append(mid))
    monkeypatch.setattr(service.entrance, "process", lambda app: Report("合成来源"))
    monkeypatch.setattr(service.transport, "send_card", lambda card: recovered.set())
    ws.loop.run_until_complete(disconnect_and_reconnect())
    assert first.close_code == 1000
    assert recovered.wait(1)
    assert calls == [(100, 150), message.message_id]


def test_run_closes_connection_and_approvals_on_window_end(connected, monkeypatch):
    service, socket, _ = connected
    deadline = time.monotonic() + 0.2
    monkeypatch.setattr(service, "clock", lambda: service.window.end.timestamp() - deadline + time.monotonic())

    async def connect():
        publish(connected, monkeypatch)

    async def close_socket():
        socket.close_code = 1000

    async def ping():
        await asyncio.Future()

    monkeypatch.setattr(service._client, "_connect", connect)
    monkeypatch.setattr(service._client, "_ping_loop", ping)
    monkeypatch.setattr(socket, "close", close_socket, raising=False)
    service.run()
    assert socket.close_code == 1000
    assert states(service) == [("closed",)]


@pytest.mark.parametrize("reconnect", [False, True], ids=["initial", "reconnect"])
def test_stalled_endpoint_does_not_prevent_window_cleanup(connected, monkeypatch, reconnect):
    service, original, _ = connected
    first = FakeSocket()
    calls = []
    cancelled = []
    ticks = []

    class Http:
        def __init__(self, *, timeout):
            assert 0 < timeout.connect <= 2
            assert 0 < timeout.read <= 2

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        async def post(self, url, *, headers, json):
            assert url.endswith(ws.GEN_ENDPOINT_URI)
            assert json == {"AppID": service.settings.app_id, "AppSecret": service.settings.app_secret}
            calls.append(url)
            if reconnect and len(calls) == 1:
                endpoint = f"wss://example.invalid/ws?{ws.DEVICE_ID}=1&{ws.SERVICE_ID}=1"
                return SimpleNamespace(status_code=200, text='{"code":0,"data":{"URL":"' + endpoint + '"}}')
            try:
                await asyncio.Future()
            finally:
                cancelled.append(True)

    async def connect(url, **kwargs):
        return first

    async def drop():
        raise OSError("合成长连接断开")

    monkeypatch.setattr("qianshu.feishu.sdk.httpx.AsyncClient", Http)
    monkeypatch.setattr(ws.websockets, "connect", connect)
    monkeypatch.setattr(first, "recv", drop)
    monkeypatch.setattr(service._client, "_reconnect_nonce", 0)
    ws.loop.run_until_complete(service._client._disconnect())
    deadline = time.monotonic() + 0.25
    clock = lambda: service.window.end.timestamp() - deadline + time.monotonic()
    monkeypatch.setattr(service, "clock", clock)
    monkeypatch.setattr(service.transport, "clock", clock)
    ws.loop.call_later(0.1, ticks.append, True)
    service.run()
    assert len(calls) == (2 if reconnect else 1)
    assert ticks == cancelled == [True]
    assert original.close_code == 1000
    if reconnect:
        assert first.close_code == 1000
    assert service._client._conn is service._origin is None
