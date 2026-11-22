import asyncio
import base64
import json
import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from threading import Barrier, Event
from types import SimpleNamespace

import pytest

from lark_oapi.api.im.v1 import Message as HistoryMessage
from lark_oapi.core.cache import LocalCache

from qianshu.feishu import FeishuService, Message, Report
from qianshu.feishu.sdk import SDKTransport, TokenManager, ws


class FakeSocket:
    close_code = None

    def __init__(self):
        self.responses = []
        self.callback_responses = []

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
            callback = json.loads(base64.b64decode(response["data"]))
            self.callback_responses.append(callback)
            self.responses.append(callback["toast"]["type"])
        else:
            self.responses.append("event_ack")


def payload(service, approval_id, message_id, record_id):
    return {
        "schema": "2.0",
        "header": {
            "app_id": service.settings.app_id, "event_id": "<回调占位>",
            "event_type": "card.action.trigger", "create_time": str(int(service.clock() * 1000)),
        },
        "event": {
            "operator": {"open_id": "<点击人占位>"},
            "context": {"open_chat_id": service.settings.chat_id, "open_message_id": message_id},
            "action": {"value": {
                "approval_id": approval_id, "record_id": record_id, "decision": "approve",
            }},
        },
    }


def encoded_frame(data, frame_type=ws.MessageType.EVENT):
    frame = ws.Frame(method=ws.FrameType.DATA.value, SeqID=0, LogID=0, service=1)
    for key, value in {
        ws.HEADER_TYPE: frame_type.value,
        ws.HEADER_MESSAGE_ID: data.get("header", {}).get("event_id", "<帧占位>"),
        ws.HEADER_TRACE_ID: "<追踪占位>",
        ws.HEADER_SUM: "1", ws.HEADER_SEQ: "0",
    }.items():
        header = frame.headers.add()
        header.key, header.value = key, value
    frame.payload = json.dumps(data).encode()
    return frame.SerializeToString()


def dispatch(service, data, *, frame_type=ws.MessageType.EVENT):
    # 实际 SDK 帧分发与应答，套接字及 REST 由离线替身提供。
    ws.loop.run_until_complete(service._client._handle_message(encoded_frame(data, frame_type)))


@pytest.fixture
def connected(tmp_path, settings, window, monkeypatch):
    def unexpected_rest(*args, **kwargs):
        pytest.fail("意外调用未替换的飞书 REST 传输")

    monkeypatch.setattr(SDKTransport, "execute", unexpected_rest)
    monkeypatch.setattr(SDKTransport, "aexecute", unexpected_rest)
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
        service._state.close()
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
    membership_queries = []

    async def is_member(operator_id):
        membership_queries.append(operator_id)
        return True

    monkeypatch.setattr(service.transport, "is_member", is_member)
    dispatch(service, data)
    dispatch(service, data)
    assert membership_queries == [data["event"]["operator"]["open_id"]]
    data["event"]["action"]["value"]["decision"] = "reject"
    data["header"]["event_id"] = "<另一回调占位>"
    dispatch(service, data)
    assert socket.responses == ["success", "warning", "warning"]
    assert states(service) == [("approve",)]
    assert all("card" not in response for response in socket.callback_responses[1:])
    assert len(membership_queries) == 2


def test_card_frame_missing_header_returns_warning(connected):
    service, socket, _ = connected
    dispatch(service, {"schema": "2.0"}, frame_type=ws.MessageType.CARD)
    assert socket.responses == ["warning"]
    assert "card" not in socket.callback_responses[0]


def test_replayed_event_cannot_be_used_with_another_card(connected, monkeypatch):
    service, socket, _ = connected
    dispatch(service, publish(connected, monkeypatch))
    other_card = publish(connected, monkeypatch, "<第二张卡占位>")
    dispatch(service, other_card)
    assert socket.responses == ["success", "warning"]
    assert "card" not in socket.callback_responses[1]
    assert states(service) == [("approve",), ("pending",)]


def test_locked_database_rejects_callback_within_sync_budget(connected, monkeypatch):
    service, socket, _ = connected
    data = publish(connected, monkeypatch)
    blocker = sqlite3.connect(service._state.path, autocommit=True)

    async def unexpected_lookup(operator_id):
        pytest.fail("无法持久认领的回调触发了群成员查询")

    async def failed_notice():
        raise RuntimeError("合成发送故障")

    monkeypatch.setattr(service, "_callback_response_margin", 2.75)
    monkeypatch.setattr(service.transport, "is_member", unexpected_lookup)
    monkeypatch.setattr(service.transport, "notify_callback_timeout", failed_notice)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        dispatch(service, data, frame_type=ws.MessageType.CARD)
        assert time.monotonic() - started < 3
    finally:
        blocker.close()
    assert socket.responses == ["warning"]
    assert "card" not in socket.callback_responses[0]
    assert socket.callback_responses[0]["toast"]["content"] == "处理超时，请重新点击"
    assert service._callback_notice_failures == 1
    with service._state.connection() as db:
        assert db.execute("SELECT count(*) FROM feishu_callback_events").fetchone() == (0,)
    assert states(service) == [("pending",)]


def test_concurrent_frames_under_write_lock_match_durable_decisions(connected, monkeypatch):
    service, socket, _ = connected
    receipts, replies, notices = {}, {}, []
    pending = []
    fast_operator = "<即时核验点击人占位>"
    for index in range(8):
        data = publish(connected, monkeypatch, f"<并发卡片占位-{index}>")
        event_id = f"<并发回调占位-{index}>"
        data["header"]["event_id"] = event_id
        if index == 0:
            data["event"]["operator"]["open_id"] = fast_operator
        pending.append((event_id, encoded_frame(data, ws.MessageType.CARD)))
    frames = iter(pending)
    send = socket.send
    complete = asyncio.Event()

    async def recv():
        item = next(frames, None)
        if item is None:
            await asyncio.Future()
        event_id, data = item
        receipts[event_id] = time.monotonic()
        return data

    async def record_reply(raw):
        frame = ws.Frame()
        frame.ParseFromString(raw)
        event_id = ws._get_by_key(frame.headers, ws.HEADER_MESSAGE_ID)
        await send(raw)
        replies[event_id] = (time.monotonic(), socket.callback_responses[-1])
        if len(replies) == len(pending):
            complete.set()

    async def is_member(operator_id):
        if operator_id != fast_operator:
            await asyncio.sleep(1)
        return True

    async def fake_token():
        return "<令牌占位>"

    async def record_notice(request, option):
        assert request.receive_id_type == "chat_id"
        assert request.request_body.receive_id == service.settings.chat_id
        assert request.request_body.msg_type == "text"
        notices.append(json.loads(request.request_body.content)["text"])
        return SimpleNamespace(success=lambda: True)

    monkeypatch.setattr(socket, "recv", recv)
    monkeypatch.setattr(socket, "send", record_reply)
    monkeypatch.setattr(service.transport, "is_member", is_member)
    monkeypatch.setattr(service.transport, "_tenant_token", fake_token)
    monkeypatch.setattr(service.transport.im.message, "acreate", record_notice)

    async def receive_batch():
        with closing(sqlite3.connect(service._state.path, autocommit=True)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            receiver = asyncio.create_task(service._client._receive_message_loop())
            try:
                await asyncio.sleep(2)
                assert len(receipts) == len(pending)
                assert not replies
                blocker.execute("ROLLBACK")
                await asyncio.wait_for(complete.wait(), timeout=1)
                await asyncio.gather(*service._callback_notices)
            finally:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)

    ws.loop.run_until_complete(receive_batch())
    assert replies.keys() == receipts.keys()
    with service._state.connection() as db:
        decisions = dict(db.execute("SELECT event_id, status FROM feishu_approvals WHERE event_id IS NOT NULL"))
    assert decisions == {pending[0][0]: "approve"}
    for event_id, (sent_at, reply) in replies.items():
        assert sent_at - receipts[event_id] < 3
        if event_id in decisions:
            assert reply["toast"]["type"] == "success"
            assert reply["card"]["data"]["elements"][0]["text"]["content"] == "已收到，处理中"
        else:
            assert reply == {"toast": {"type": "warning", "content": "处理超时，请重新点击"}}
    assert notices == ["处理超时，请重新点击"] * (len(pending) - 1)
    assert service._callback_notice_failures == 0


@pytest.mark.parametrize("pause_after_commit", [False, True])
def test_deadline_racing_with_decision_commit(connected, monkeypatch, pause_after_commit):
    service, socket, _ = connected
    data = publish(connected, monkeypatch)
    decide = service._state.decide
    release = Event()
    entered = asyncio.Event()
    if not pause_after_commit:
        queued = publish(connected, monkeypatch, "<排队卡片占位>")
        queued["header"]["event_id"] = "<排队回调占位>"

    def delayed_decision(*args, deadline, before_commit):
        def pause_before_commit():
            if not pause_after_commit:
                ws.loop.call_soon_threadsafe(entered.set)
                assert release.wait(2)
            before_commit()

        result = decide(*args, deadline=deadline, before_commit=pause_before_commit)
        if pause_after_commit:
            ws.loop.call_soon_threadsafe(entered.set)
            assert release.wait(2)
        return result

    async def record_notice():
        pass

    async def run_frame():
        timing = service._client._frame_timing(socket)
        task = asyncio.create_task(
            service._client._handle_message(encoded_frame(data, ws.MessageType.CARD), timing),
        )
        queued_task = None
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            if not pause_after_commit:
                queued_task = asyncio.create_task(
                    service._client._handle_message(encoded_frame(queued, ws.MessageType.CARD), timing),
                )
            await asyncio.sleep(0.35)
            assert task.done() is not pause_after_commit
            if queued_task is not None:
                assert queued_task.done()
        finally:
            release.set()
            await task
            if queued_task is not None:
                await queued_task

    monkeypatch.setattr(service, "_callback_response_margin", 2.75)
    monkeypatch.setattr(service._state, "decide", delayed_decision)
    monkeypatch.setattr(service.transport, "notify_callback_timeout", record_notice)
    ws.loop.run_until_complete(run_frame())
    # 等待写线程完整退出，再回读事务最终状态。
    service._callback_writer.submit(lambda: None).result(timeout=2)
    if pause_after_commit:
        [reply] = socket.callback_responses
        assert states(service) == [("approve",)]
        assert reply["toast"]["type"] == "success"
        assert "card" in reply
    else:
        assert states(service) == [("pending",), ("pending",)]
        assert socket.callback_responses == [
            {"toast": {"type": "warning", "content": "处理超时，请重新点击"}},
        ] * 2
        with service._state.connection() as db:
            assert db.execute("SELECT event_id FROM feishu_callback_events").fetchall() == [
                (data["header"]["event_id"],),
            ]


def test_callback_deadline_includes_delay_before_frame_handler_starts(connected, monkeypatch):
    service, socket, _ = connected
    data = publish(connected, monkeypatch)
    notified = asyncio.Event()

    async def record_notice():
        notified.set()

    async def delayed_dispatch():
        timing = service._client._frame_timing(socket)
        await asyncio.sleep(0.15)
        await service._client._handle_message(encoded_frame(data, ws.MessageType.CARD), timing)
        await asyncio.wait_for(notified.wait(), timeout=1)

    monkeypatch.setattr(service, "_callback_response_margin", 2.9)
    monkeypatch.setattr(service.transport, "notify_callback_timeout", record_notice)
    ws.loop.run_until_complete(delayed_dispatch())
    assert socket.callback_responses == [
        {"toast": {"type": "warning", "content": "处理超时，请重新点击"}},
    ]
    with service._state.connection() as db:
        assert db.execute("SELECT count(*) FROM feishu_callback_events").fetchone() == (0,)
    assert states(service) == [("pending",)]


@pytest.mark.parametrize("decision,status", [("approve", "已收到，处理中"), ("reject", "已驳回")])
def test_card_frame_returns_disabled_raw_card(connected, monkeypatch, decision, status):
    service, socket, _ = connected
    data = publish(connected, monkeypatch)
    data["event"]["action"]["value"]["decision"] = decision

    started = time.monotonic()
    dispatch(service, data, frame_type=ws.MessageType.CARD)
    assert time.monotonic() - started < 3

    [response] = socket.callback_responses
    assert response["card"]["type"] == "raw"
    summary, actions = response["card"]["data"]["elements"]
    assert summary["text"]["content"] == status
    assert len(actions["actions"]) == 2
    for button in actions["actions"]:
        assert button["disabled"] is True
        assert button["disabled_tips"]["content"]
        assert "value" not in button
    assert states(service) == [(decision,)]


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


@pytest.mark.parametrize("outcome", ["found", "failed", "timeout"])
def test_membership_checks_configured_group_and_open_id_across_pages(connected, monkeypatch, outcome):
    service, socket, _ = connected
    data = publish(connected, monkeypatch)
    operator_id = data["event"]["operator"]["open_id"]
    pages = []

    async def get_members(request, option):
        assert option.tenant_access_token == token
        assert request.chat_id == service.settings.chat_id
        assert request.member_id_type == "open_id"
        pages.append(request.page_token)
        next_page = request.page_token is None
        if not next_page and outcome == "failed":
            return SimpleNamespace(success=lambda: False)
        if not next_page and outcome == "timeout":
            await asyncio.Future()
        return SimpleNamespace(success=lambda: True, data=SimpleNamespace(
            items=[SimpleNamespace(member_id_type="user_id" if next_page else "open_id",
                                   member_id=operator_id)],
            has_more=next_page, page_token="<群成员分页占位>", trigger_security_conf_limit=False,
        ))

    monkeypatch.delattr(service.transport, "is_member")
    token = "<租户令牌占位>"

    async def fetch_token():
        return token

    monkeypatch.setattr(service.transport, "_tenant_token", fetch_token)
    monkeypatch.setattr(service.transport.im.chat_members, "aget", get_members)
    monkeypatch.setattr("qianshu.feishu.sdk._HTTP_TIMEOUT", 0.05)
    dispatch(service, data)
    assert pages == [None, "<群成员分页占位>"]
    if outcome == "found":
        assert socket.responses == ["success"]
        assert states(service) == [("approve",)]
    else:
        assert socket.responses == ["warning"]
        assert "card" not in socket.callback_responses[0]
        assert states(service) == [("pending",)]


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

    monkeypatch.setattr(TokenManager, "cache", LocalCache())
    TokenManager.cache.set(f"self_tenant_token:{service.settings.app_id}", token, int(time.time()) + 60)
    monkeypatch.setattr(service.transport.im.message, "areply", reply)
    for _ in range(2):
        service.transport.reply(message_id, "概念验证期不处理", deadline=time.monotonic() + 3)
    assert len(requests) == 2
    assert requests[0] and requests[0] == requests[1]


def test_notice_with_cold_token_cache_while_reactions_are_blocked(connected, monkeypatch):
    service, _, _ = connected
    settings = service.settings
    reactions_started = Barrier(9)
    release_reactions = Event()
    token = "<租户令牌占位>"
    requests = []
    token_calls = []

    def react(message_id):
        reactions_started.wait(timeout=3)
        assert release_reactions.wait(5)

    async def execute(config, request, option=None):
        if option is not None:
            assert option.tenant_access_token == token
            requests.append((request.message_id, json.loads(request.body.content), time.monotonic()))
            return SimpleNamespace(status_code=200, content=b'{"code":0}')
        assert request.body.app_id == settings.app_id
        assert request.body.app_secret == settings.app_secret
        token_calls.append(True)
        return SimpleNamespace(status_code=200, content=json.dumps({
            "code": 0, "tenant_access_token": token, "expire": 7200,
        }).encode())

    accepted = [
        Message(f"<合成申请{index}>", settings.chat_id, next(iter(settings.allowed_senders)), 120_000,
                "text", '{"text":"@_user_1 合成样例 ABC"}', ("@_user_1",))
        for index in range(8)
    ]
    rejected = Message("<合成拒绝>", settings.chat_id, next(iter(settings.allowed_senders)), 120_000,
                       "text", '{"text":"@_user_1 合成拒绝"}', ("@_user_1",))
    monkeypatch.setattr(service.transport, "react", react)
    monkeypatch.setattr(service.entrance, "process", lambda app: Report("合成来源"))
    monkeypatch.setattr(service.transport, "send_card", lambda card: "<结果卡占位>")
    monkeypatch.setattr(TokenManager, "cache", LocalCache())
    monkeypatch.setattr(SDKTransport, "aexecute", execute)
    try:
        for message in accepted:
            service.entrance.inject_message(message)
        reactions_started.wait(timeout=3)
        start = time.monotonic()
        service.entrance.inject_message(rejected).result(timeout=3)
        assert token_calls == [True]
        assert len(requests) == 1
        message_id, content, sent = requests[0]
        assert (message_id, content) == (rejected.message_id, {"text": "概念验证期不处理"})
        assert sent - start < 3
    finally:
        release_reactions.set()


@pytest.mark.parametrize("stalled", ["token", "reply"])
def test_reply_deadline_cancels_token_exchange_or_send(connected, monkeypatch, stalled):
    service, _, _ = connected
    stages = []

    async def exchange(config, request):
        stages.append("token")
        if stalled == "token":
            try:
                await asyncio.Future()
            finally:
                stages.append("cancelled")
        return SimpleNamespace(status_code=200, content=json.dumps({
            "code": 0, "tenant_access_token": "<租户令牌占位>", "expire": 7200,
        }).encode())

    async def reply(request, option):
        stages.append("reply")
        try:
            await asyncio.Future()
        finally:
            stages.append("cancelled")

    monkeypatch.setattr(TokenManager, "cache", LocalCache())
    monkeypatch.setattr(SDKTransport, "aexecute", exchange)
    monkeypatch.setattr(service.transport.im.message, "areply", reply)
    with pytest.raises(TimeoutError):
        service.transport.reply("<消息占位>", "概念验证期不处理", deadline=time.monotonic() + 0.15)
    expected = ["token", "cancelled"] if stalled == "token" else ["token", "reply", "cancelled"]
    assert stages == expected


@pytest.mark.parametrize("change", ["disconnect", "expiry", "age"])
def test_rechecks_approval_after_membership_lookup(connected, monkeypatch, change):
    service, socket, clock = connected
    data = publish(connected, monkeypatch)

    async def is_member(operator_id):
        if change == "disconnect":
            service._disconnected()
        elif change == "expiry":
            clock.now = 200
        else:
            clock.now += service.settings.callback_max_age + 1
        return True

    monkeypatch.setattr(service.transport, "is_member", is_member)
    dispatch(service, data)
    assert socket.responses == ["warning"]
    assert states(service) == [("pending" if change == "age" else "closed",)]


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
    ("header", "app_id", None),
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


@pytest.mark.parametrize("scale", [1, 1_000, 1_000_000])
@pytest.mark.parametrize("encode", [int, str])
def test_accepts_callback_time_in_seconds_milliseconds_or_microseconds(connected, monkeypatch, scale, encode):
    service, socket, clock = connected
    data = publish(connected, monkeypatch)
    data["header"]["create_time"] = encode(int(clock.now * scale))
    dispatch(service, data, frame_type=ws.MessageType.CARD)
    assert socket.responses == ["success"]
    assert states(service) == [("approve",)]


@pytest.mark.parametrize("created,now", [
    ("99000", 150), ("149999", 150), ("150000", 156), ("150001", 150),
    ("坏时间", 150), (None, 150), (150000.0, 150), (True, 150), ("150000.0", 150),
    ("150000000000000000000000000", 150),
], ids=["before-window", "before-connection", "over-age-limit", "future",
        "malformed", "missing", "float", "boolean", "decimal-string", "out-of-range"])
def test_rejects_invalid_callback_time(connected, monkeypatch, created, now):
    service, socket, clock = connected
    service.settings = replace(service.settings, callback_max_age=5)
    data = publish(connected, monkeypatch)
    if created is None:
        del data["header"]["create_time"]
    else:
        data["header"]["create_time"] = created
    clock.now = now

    async def unexpected_lookup(operator_id):
        pytest.fail("无效时间触发了群成员查询")

    monkeypatch.setattr(service.transport, "is_member", unexpected_lookup)
    dispatch(service, data)
    assert socket.responses == ["warning"]
    assert "card" not in socket.callback_responses[0]
    assert states(service) == [("pending",)]


def test_rejects_ambiguous_callback_time(connected, monkeypatch):
    service, socket, _ = connected
    service.settings = replace(service.settings, callback_max_age=200)
    data = publish(connected, monkeypatch)
    # 150 可解释为 150 秒、0.15 秒或 0.00015 秒，都是允许时差内的值。
    data["header"]["create_time"] = 150
    dispatch(service, data, frame_type=ws.MessageType.CARD)
    assert socket.responses == ["warning"]
    assert states(service) == [("pending",)]


def test_rejects_backlog_on_reconnect_even_with_valid_new_card_binding(connected, monkeypatch):
    service, _, clock = connected
    service._disconnected()
    clock.now += 2
    socket = FakeSocket()
    service._client._conn = socket
    service._connected(socket)

    data = publish(connected, monkeypatch)
    data["header"]["create_time"] = str(int((clock.now - 1) * 1000))
    dispatch(service, data)
    assert states(service) == [("pending",)]
    data["header"]["create_time"] = str(int(clock.now * 1000))
    dispatch(service, data)
    assert socket.responses == ["warning", "success"]
    assert states(service) == [("approve",)]


@pytest.mark.parametrize("key,match", [
    ("app_id", None), ("app_id", False),
    ("tenant_key", None), ("tenant_key", False), ("tenant_key", True),
])
def test_validates_app_and_configured_tenant_for_both_event_types(connected, monkeypatch, key, match):
    service, socket, _ = connected
    service.settings = replace(service.settings, tenant_key="<本租户占位>")
    settings = service.settings
    data = publish(connected, monkeypatch)
    data["header"]["tenant_key"] = settings.tenant_key
    if match is None:
        del data["header"][key]
    elif not match:
        data["header"][key] = "<不匹配占位>"

    dispatch(service, data)
    received = []
    monkeypatch.setattr(service.entrance, "inject_message", received.append)
    dispatch(service, {
        "schema": "2.0",
        "header": {**data["header"], "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": next(iter(settings.allowed_senders))}},
            "message": {
                "message_id": "<来源校验消息占位>", "chat_id": settings.chat_id, "chat_type": "group",
                "create_time": "150000", "message_type": "text", "content": '{"text":"合成样例"}',
            },
        },
    })
    assert socket.responses == ["success" if match else "warning", "event_ack"]
    assert len(received) == int(bool(match))


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


@pytest.mark.parametrize("mention_other", [True, False], ids=["other-user", "no-mention"])
def test_recovery_ignores_messages_without_bot_mention(connected, monkeypatch, mention_other):
    service, socket, _ = connected
    settings = service.settings
    message_id = "<历史消息占位>"
    mention_key = "@_user_1"
    item = HistoryMessage({
        "message_id": message_id, "chat_id": settings.chat_id, "create_time": 120000,
        "msg_type": "text",
        "sender": {"id": next(iter(settings.allowed_senders)), "id_type": "open_id", "sender_type": "user"},
        "body": {"content": json.dumps({"text": (
            f"{mention_key} 合成样例 ABC" if mention_other else "合成样例 ABC"
        )})},
        "mentions": [{"key": mention_key, "id": "<他人占位>", "id_type": "open_id"}] if mention_other else [],
    })
    effects = []
    work = []
    receive = service.entrance._receive

    def track_receive(message, deadline):
        future = receive(message, deadline)
        if future is not None:
            work.append(future)
        return future

    def process(application):
        effects.append("process")
        return Report("合成来源")

    def list_messages(request):
        assert request.container_id == settings.chat_id
        return SimpleNamespace(success=lambda: True, data=SimpleNamespace(items=[item], has_more=False))

    monkeypatch.delattr(service.transport, "history")
    monkeypatch.setattr(service.transport.im.message, "list", list_messages)
    monkeypatch.setattr(service.entrance, "_receive", track_receive)
    monkeypatch.setattr(service.entrance, "process", process)
    monkeypatch.setattr(service.transport, "react", lambda mid: effects.append("reaction"))
    monkeypatch.setattr(service.transport, "reply", lambda *args, **kwargs: effects.append("reply"))
    monkeypatch.setattr(service.transport, "send_card", lambda card: effects.append("card"))
    service.entrance.recover().result(timeout=2)
    for future in work:
        future.result(timeout=2)
    assert effects == []
    assert service._state.message_status(message_id) is None
    assert socket.responses == []


def test_acked_notice_retries_without_reconnect_or_redelivery(connected, monkeypatch):
    service, socket, _ = connected
    settings = service.settings
    message_id = "<合成拒绝消息占位>"
    requests = []
    acked = Event()
    first_started = Event()
    completed = Event()
    complete_notice = service._state.complete_notice

    async def reply(request, option):
        requests.append(request.body.uuid)
        assert request.message_id == message_id
        assert json.loads(request.body.content) == {"text": "概念验证期不处理"}
        if len(requests) == 1:
            first_started.set()
            assert await asyncio.to_thread(acked.wait, 2)
            raise OSError("合成 REST 网络中断")
        return SimpleNamespace(success=lambda: True)

    def complete(mid):
        complete_notice(mid)
        completed.set()

    monkeypatch.setattr(TokenManager, "cache", LocalCache())
    TokenManager.cache.set(
        f"self_tenant_token:{settings.app_id}", "<租户令牌占位>", int(time.time()) + 60,
    )
    monkeypatch.setattr(service.transport.im.message, "areply", reply)
    monkeypatch.setattr(service._state, "complete_notice", complete)
    monkeypatch.setattr(service.entrance, "recover", lambda: pytest.fail("意外触发回读"))
    try:
        dispatch(service, {
            "schema": "2.0",
            "header": {"app_id": settings.app_id, "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_type": "user", "sender_id": {"open_id": next(iter(settings.allowed_senders))}},
                "message": {
                    "message_id": message_id, "chat_id": settings.chat_id, "chat_type": "group",
                    "create_time": "120000", "message_type": "text",
                    "content": '{"text":"@_user_1 合成拒绝"}',
                    "mentions": [{"key": "@_user_1", "id": {"open_id": settings.bot_open_id}}],
                },
            },
        })
        assert socket.responses == ["event_ack"]
        assert first_started.wait(2)
        assert service._state.message_status(message_id) == "notice_pending"
    finally:
        acked.set()
    assert completed.wait(2)
    assert len(requests) == 2 and requests[0] and requests[0] == requests[1]
    assert service._state.message_status(message_id) == "done"
    assert service._client._conn is service._origin is socket
    assert socket.close_code is None


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


@pytest.mark.parametrize("server_error", [False, True], ids=["timeout", "timeout-and-server-error"])
def test_initial_endpoint_retries_then_recovers(connected, monkeypatch, server_error):
    service, original, _ = connected
    replacement = FakeSocket()
    recovered = Event()
    attempts = []
    cancellations = []
    ranges = []
    endpoint = f"wss://example.invalid/ws?{ws.DEVICE_ID}=1&{ws.SERVICE_ID}=1"

    class Http:
        def __init__(self, *, timeout):
            assert 0 < timeout.connect <= 0.05

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        async def post(self, url, *, headers, json):
            assert url.endswith(ws.GEN_ENDPOINT_URI)
            attempts.append(time.monotonic())
            if len(attempts) == 1:
                try:
                    await asyncio.Future()
                finally:
                    cancellations.append(True)
            if server_error and len(attempts) == 2:
                return SimpleNamespace(status_code=503)
            return SimpleNamespace(status_code=200, text='{"code":0,"data":{"URL":"' + endpoint + '"}}')

    async def connect(url, **kwargs):
        assert url == endpoint
        return replacement

    def history(start, end):
        assert service._origin is replacement
        ranges.append((start, end))
        yield Message(
            "<首次连接补漏占位>", service.settings.chat_id, next(iter(service.settings.allowed_senders)),
            120_000, "text", '{"text":"@_user_1 合成样例 ABC"}', ("@_user_1",),
        )

    async def serve_until_recovered():
        serving = asyncio.create_task(service._serve())
        try:
            assert await asyncio.to_thread(recovered.wait, 3)
            assert not serving.done()
        finally:
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)

    monkeypatch.setattr("qianshu.feishu.sdk._HTTP_TIMEOUT", 0.05)
    monkeypatch.setattr("qianshu.feishu.sdk.httpx.AsyncClient", Http)
    monkeypatch.setattr(ws.websockets, "connect", connect)
    monkeypatch.setattr(service.transport, "history", history)
    monkeypatch.setattr(service.transport, "react", lambda mid: None)
    monkeypatch.setattr(service.entrance, "process", lambda app: Report("合成来源"))
    monkeypatch.setattr(service.transport, "send_card", lambda card: recovered.set())
    ws.loop.run_until_complete(service._client._disconnect())
    ws.loop.run_until_complete(serve_until_recovered())
    assert original.close_code == 1000
    assert cancellations == [True]
    assert len(attempts) == (3 if server_error else 2)
    assert attempts[1] - attempts[0] >= 0.25
    if server_error:
        assert attempts[2] - attempts[1] >= 0.5
    assert ranges == [(100, 150)]


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


@pytest.fixture
def ticking_socket_loop(connected):
    # 合成连接没有可读网络帧，沙箱也可能禁止 self-pipe 写入。定时唤醒真实循环，
    # 让写线程已投递的完成回调得到调度，保留真实单调时钟和原发送路径。
    def tick():
        nonlocal timer
        timer = ws.loop.call_later(0.01, tick)

    timer = ws.loop.call_soon(tick)
    try:
        yield
    finally:
        timer.cancel()


@pytest.mark.parametrize(("phase", "release_after"), [
    ("candidate", 0.55), ("confirming", 0.55), ("confirming", 3.05), ("proof", 3.05),
])
def test_blocked_commit_warns_once_on_original_socket_without_a_grant(
    connected, monkeypatch, ticking_socket_loop, phase, release_after,
):
    from qianshu.feishu import State

    service, socket, _ = connected
    data = publish(connected, monkeypatch)
    value = data["event"]["action"]["value"]
    key = (value["approval_id"], value["record_id"])
    entered = asyncio.Event()
    release = Event()
    match = {
        "candidate": "SET status = 'candidate_approve'",
        "confirming": "SET status = 'confirming_approve'",
        "proof": "INSERT INTO feishu_decision_proofs",
    }[phase]
    armed = False

    def block_one_commit(sql):
        nonlocal armed
        if match in sql:
            armed = True
        if armed and sql == "COMMIT":
            armed = False
            ws.loop.call_soon_threadsafe(entered.set)
            release.wait(5)

    async def quiet_notice(*notice):
        pass

    async def run_frame(reader):
        timing = service._client._frame_timing(socket)
        task = asyncio.create_task(service._client._handle_message(encoded_frame(data, ws.MessageType.CARD), timing))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(task, 2.5)
            assert time.monotonic() - timing[1] < 3
            notice = "处理超时，请重新点击" if phase == "candidate" else "审批决定未确认，请人工核对"
            assert socket.callback_responses == [
                {"toast": {"type": "warning", "content": notice}},
            ]
            assert reader.approval_status(*key) == "pending"
            await asyncio.sleep(max(0, timing[1] + release_after - time.monotonic()))
        finally:
            release.set()
            await task

    monkeypatch.setattr(service, "_callback_response_margin", 2.6)
    monkeypatch.setattr(service.transport, "notify_callback_timeout", quiet_notice)
    with closing(State(service._state.path, service.settings)) as reader:
        service._state._writer.set_trace_callback(block_one_commit)
        try:
            ws.loop.run_until_complete(run_frame(reader))
            service._callback_writer.submit(lambda: None).result(timeout=2)
        finally:
            service._state._writer.set_trace_callback(None)
        assert reader.claim_create(*key) is None
        assert not reader.dispatch_create(*key, pytest.fail)
    with closing(State(service._state.path, service.settings)) as reopened:
        assert reopened.claim_create(*key) is None
        assert not reopened.dispatch_create(*key, pytest.fail)
    assert socket.responses == ["warning"]
    assert not service.send_first_create(*key, pytest.fail, pytest.fail, pytest.fail)
    dispatch(service, data)
    assert socket.responses == ["warning", "warning"]
    if phase == "candidate":
        data["header"]["event_id"] = "<新的点击>"
        dispatch(service, data)
        assert socket.responses[-1] == "success"
        assert service._state.claim_create(*key) == service.window.end.timestamp()


def test_durable_proof_independent_of_socket_delivery(connected, monkeypatch, ticking_socket_loop):
    from qianshu.feishu import State

    service, socket, _ = connected
    data = publish(connected, monkeypatch)
    value = data["event"]["action"]["value"]
    key = (value["approval_id"], value["record_id"])
    handoffs = []

    async def drop_reply(raw):
        with closing(State(service._state.path, service.settings)) as reader:
            assert reader.approval_status(*key) == "approve"
        # 测试连接在交付前丢包。
        return

    monkeypatch.setattr(socket, "send", drop_reply)
    dispatch(service, data)
    assert socket.responses == []
    with closing(State(service._state.path, service.settings)) as reader:
        def handoff(check):
            check()
            handoffs.append("sent")

        assert service.send_first_create(*key, lambda: None, lambda: None, handoff)
        assert reader.claim_create(*key) is None
        assert not reader.dispatch_create(*key, pytest.fail)
    assert handoffs == ["sent"]


def test_recorded_proof_has_bounded_reply_while_publishing_claim(connected, monkeypatch, ticking_socket_loop):
    service, socket, _ = connected
    data = publish(connected, monkeypatch)
    key = (data["event"]["action"]["value"]["approval_id"], data["event"]["action"]["value"]["record_id"])
    entered, release = asyncio.Event(), Event()
    publishing = False

    def stall_publication(sql):
        nonlocal publishing
        if "SET status = 'approve'" in sql:
            publishing = True
        if publishing and sql == "COMMIT":
            publishing = False
            ws.loop.call_soon_threadsafe(entered.set)
            release.wait(5)

    async def run_frame():
        timing = service._client._frame_timing(socket)
        task = asyncio.create_task(service._client._handle_message(encoded_frame(data, ws.MessageType.CARD), timing))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            with service._state.connection() as reader:
                assert reader.execute("SELECT count(*) FROM feishu_decision_proofs").fetchone() == (1,)
            await asyncio.wait_for(task, 2.5)
            assert time.monotonic() - timing[1] < 3
            assert socket.callback_responses == [
                {"toast": {"type": "info", "content": "审批决定已记录，处理结果请稍后核对"}},
            ]
        finally:
            release.set()
            await task

    monkeypatch.setattr(service, "_callback_response_margin", 2.4)
    service._state._writer.set_trace_callback(stall_publication)
    try:
        ws.loop.run_until_complete(run_frame())
        service._callback_writer.submit(lambda: None).result(timeout=2)
    finally:
        service._state._writer.set_trace_callback(None)
    assert service._state.claim_create(*key) == service.window.end.timestamp()
    assert service._state.claim_create(*key) is None


def test_timely_decision_can_finish_proof_in_reserved_time(connected, monkeypatch, ticking_socket_loop):
    service, socket, _ = connected
    data = publish(connected, monkeypatch)
    value = data["event"]["action"]["value"]
    monkeypatch.setattr(service, "_callback_response_margin", 2.3)
    timing = service._client._frame_timing(socket)
    proof = False
    entered = asyncio.Event()
    release = Event()

    def pause_proof(sql):
        nonlocal proof
        if "INSERT INTO feishu_decision_proofs" in sql:
            proof = True
        if proof and sql == "COMMIT":
            proof = False
            ws.loop.call_soon_threadsafe(entered.set)
            release.wait(4)

    async def run_frame():
        task = asyncio.create_task(service._client._handle_message(encoded_frame(data, ws.MessageType.CARD), timing))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.sleep(max(0, timing[2] + 0.1 - time.monotonic()))
            assert not socket.responses
        finally:
            release.set()
            await task
        assert time.monotonic() - timing[1] < 3

    service._state._writer.set_trace_callback(pause_proof)
    try:
        ws.loop.run_until_complete(run_frame())
    finally:
        service._state._writer.set_trace_callback(None)
    assert socket.responses == ["success"]
    assert service._state.claim_create(value["approval_id"], value["record_id"]) == service.window.end.timestamp()


def test_run_create_window_expiry_during_proof_warns_on_original_socket(
    connected, monkeypatch, ticking_socket_loop,
):
    from qianshu.feishu import State

    service, socket, _ = connected
    expires = time.monotonic() + 1
    monkeypatch.setattr(service, "clock", lambda: service.window.end.timestamp() + time.monotonic() - expires)
    data = publish(connected, monkeypatch)
    value = data["event"]["action"]["value"]
    key = value["approval_id"], value["record_id"]
    received = []
    sent = []
    proof_commits = []
    proof = False
    original_send = socket.send

    def stall_proof(sql):
        nonlocal proof
        if "INSERT INTO feishu_decision_proofs" in sql:
            proof = True
        if proof and sql == "COMMIT":
            proof = False
            proof_commits.append(time.monotonic())
            time.sleep(max(0, received[0] + 3.1 - time.monotonic()))

    async def connect():
        timing = service._client._frame_timing(socket)
        received.append(timing[1])
        asyncio.create_task(service._client._handle_message(encoded_frame(data, ws.MessageType.CARD), timing))

    async def send(raw):
        assert socket.close_code is None
        await original_send(raw)
        sent.append(time.monotonic())

    async def quiet_notice(*notice):
        pass

    def work(stop):
        time.sleep(max(0, expires - time.monotonic()) + 0.1)

    monkeypatch.setattr(service._client, "_connect", connect)
    monkeypatch.setattr(socket, "send", send)
    monkeypatch.setattr(service.transport, "notify_callback_timeout", quiet_notice)
    monkeypatch.setattr(service._client, "_ping_loop", socket.recv)
    service._state._writer.set_trace_callback(stall_proof)
    service.run_create(work)

    assert len(proof_commits) == 1 and received[0] <= proof_commits[0] < expires, socket.callback_responses
    assert len(sent) == 1 and expires <= sent[0] < received[0] + 3
    assert socket.callback_responses == [
        {"toast": {"type": "warning", "content": "审批决定未确认，请人工核对"}},
    ]
    with closing(State(service._state.path, service.settings)) as reader:
        assert reader.approval_status(*key) == "closed"
        assert reader.claim_create(*key) is None
        assert not reader.dispatch_create(*key, pytest.fail)
