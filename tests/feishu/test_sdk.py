import asyncio
import base64
import json
import time
from threading import Event
from types import SimpleNamespace

import pytest

from lark_oapi.api.im.v1 import Message as HistoryMessage

from qianshu.feishu import FeishuService, Report
from qianshu.feishu.sdk import ws


class FakeSocket:
    close_code = None

    def __init__(self):
        self.responses = []

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
    socket = FakeSocket()
    service._client._conn = socket
    service._connected(socket)
    try:
        yield service, socket, clock
    finally:
        service._disconnected()
        service.entrance.close()
        cron = service._client._cache._cron
        cron.cancel()
        loop.run_until_complete(asyncio.gather(cron, return_exceptions=True))
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
    direct = service._client._event_handler._do_without_validation(json.dumps(data).encode())
    assert direct.toast.type == "warning"
    dispatch(service, data)
    dispatch(service, data)
    data["event"]["action"]["value"]["decision"] = "reject"
    data["header"]["event_id"] = "<另一回调占位>"
    dispatch(service, data)
    assert socket.responses == ["success", "warning", "warning"]
    assert states(service) == [("approve",)]


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
