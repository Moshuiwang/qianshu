import asyncio
import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from qianshu.feishu import sdk
from qianshu.feishu.handoff import HandoffService, MentionLog
from qianshu.feishu.trial_listener import Listener
from qianshu.mail import trial
from tests.feishu.test_handoff import FakeFeishu, message
from tests.feishu.test_sdk import FakeSocket, encoded_frame
from tests.feishu.test_trial_approval import bind, journal
from tests.feishu.test_trial_paths import settings, switches


async def replay(service, patch, fault_seen):
    socket = FakeSocket()
    patch.setattr(sdk.ws, "loop", asyncio.get_running_loop())
    client = sdk._Connection(service, approvals=service.trial_callbacks)
    client._discover_endpoint = AsyncMock(return_value="wss://example.invalid/?device_id=d&service_id=1")
    patch.setattr(sdk.ws.websockets, "connect", AsyncMock(return_value=socket))
    fake = FakeFeishu()
    service.handoff.transport = fake
    store = service.handoff.store
    try:
        await client._connect()
        assert client.receiver and service._scan.is_set()
        assert fault_seen.wait(5)
        now = int(store.clock() * 1000)
        event = dict(sender=dict(sender_type="user", sender_id=dict(open_id="owner")),
                     message=dict(message_id="live", chat_id="group", chat_type="group", message_type="text",
                                  create_time=str(now), content="{}", mentions=[dict(key="@1", id=dict(open_id="bot"))]))
        frame = encoded_frame(dict(schema="2.0", header=dict(app_id="app", event_id="event",
                                   event_type="im.message.receive_v1"), event=event))
        for _ in range(2):
            await client._handle_message(frame, client._frame_timing(socket))
        service._receipts.shutdown(wait=True)
        assert fake.reactions == ["live"]
        fake.messages = [replace(message("missed"), chat_id="group", sender_id="owner", created_ms=now)]
        store.recover(fake, threading.Event())
        assert store._db.execute("SELECT through FROM recovery").fetchone()[0] >= now / 1000
        assert {r[0] for r in store._db.execute("SELECT message_id FROM mentions")} == {"live", "missed"}
        assert socket.responses == ["event_ack"] * 2
    finally:
        if client._conn:
            await client._disconnect()
        for task in (client.receiver, client._cache._cron):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (client.receiver, client._cache._cron) if t), return_exceptions=True)


@pytest.mark.parametrize("fault", ["thread_start", "recursive_setup", "rotation"])
def test_approval_fault_keeps_handoff(tmp_path, monkeypatch, fault):
    config = settings()
    path = tmp_path / "mentions"
    fault_seen = threading.Event()

    def run(service):
        asyncio.run(replay(service, monkeypatch, fault_seen))

    monkeypatch.setattr(HandoffService, "run", run)
    if fault == "thread_start":
        start = threading.Thread.start
        def fail_start(thread):
            if thread.name == "trial-approval-recovery":
                fault_seen.set()
                raise RuntimeError()
            return start(thread)
        monkeypatch.setattr(threading.Thread, "start", fail_start)
        monkeypatch.setattr(trial, "has_approval_history", lambda _: True)
        with MentionLog.listening(path, config) as log:
            trial.run_disabled(HandoffService(log), tmp_path / "trial", config)
    else:
        business = tmp_path / "business"
        business.write_text(f"MAIL_PRIVATE_DIR={tmp_path}\nMAIL_HOST=synthetic\nMAIL_USER=local\n"
                            "MAIL_PASSWORD=secret\nFEISHU_CHAT_ID=group")
        if fault == "recursive_setup":
            def recursive(*args, **kwargs):
                fault_seen.set()
                json.loads("[" * 10000 + "]" * 10000)
            monkeypatch.setattr(trial, "Listener", recursive)
        else:
            original = Listener.request_rotation
            def broken(listener, origin, reason):
                if origin is not None:
                    fault_seen.set()
                    raise RuntimeError()
                original(listener, origin, reason)
            monkeypatch.setattr(Listener, "request_rotation", broken)
        monkeypatch.setattr(trial.Trial, "run", lambda self: None)
        trial.run_service(SimpleNamespace(business_env_file=business), config, path, switches)


def test_short_reject_reason_filtered_before_announcement(journal, tmp_path, monkeypatch, caplog):
    config = replace(settings(), trial_credentials=(("custom", "xYz456"),))
    listener = Listener(tmp_path / "trial", config, switches, clock=lambda: 86002)
    origin = FakeSocket()
    listener.rotate(origin, "连接")
    # bind() 使用固定合成代次。
    listener.session = "session"
    journal.rotate("session", "连接")
    bind(journal)
    listener.transport.is_member = AsyncMock(return_value=True)
    event = SimpleNamespace(context=SimpleNamespace(open_chat_id="group", open_message_id="message2"),
                            operator=SimpleNamespace(open_id="ou_actor"),
                            action=SimpleNamespace(value=dict(card="card", format=1, registration=1, version=1,
                                                   action="reject"), form_value={"reason": "理由 xYz456"}))
    header = SimpleNamespace(event_type="card.action.trigger", event_id="evt", create_time="86002")
    def callback():
        return asyncio.run(listener.record(SimpleNamespace(header=header, event=event), origin,
                                           time.monotonic() + 3)).toast.content

    for field in ("registration", "version"):
        event.action.value[field] = 2
        header.event_id = field
        assert "不符" in callback()
        assert not journal.db.execute("SELECT 1 FROM trial_receipt").fetchone()
        event.action.value[field] = 1
    header.event_id = "reject"
    assert "驳回" in callback()
    sent = []
    def http(req):
        sent.append(json.loads(json.loads(req.content)["content"])["text"])
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "receipt"}})
    client = httpx.AsyncClient
    monkeypatch.setattr(sdk.httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(http), **kw))
    listener.transport._tenant_token = AsyncMock(return_value="token")
    listener.maintain()
    assert len(sent) == 1 and "驳回" in sent[0] and "xYz456" not in sent[0]
    assert "E-CRED-STOP" in caplog.text


def test_final_http_checks_short_value_after_serialization(monkeypatch):
    from qianshu.__main__ import _send_trial_message
    from qianshu.feishu.trial_listener import checked_transport
    from qianshu.mingdao.create_order import OutletConflict

    sender = checked_transport(replace(settings(), trial_credentials=(("custom", 'x"Y'),)))
    sender._tenant_token = AsyncMock(return_value="token")
    client = httpx.AsyncClient
    posts = []
    monkeypatch.setattr(sdk.httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(posts.append), **kw))
    original = sender.order_http.request

    def mutated(client, model, purpose, token):
        model.body.content = json.dumps({"text": 'x"Y'})
        return original(client, model, purpose, token)

    monkeypatch.setattr(sender.order_http, "request", mutated)
    claims = []
    with pytest.raises(OutletConflict):
        _send_trial_message(sender, "合成通知", "uid", False, lambda: claims.append(1))
    assert posts == claims == []
