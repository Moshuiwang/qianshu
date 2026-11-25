import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from qianshu.feishu import sdk
from qianshu.feishu.trial_listener import Listener, checked_transport, send_trial_message
from qianshu.mingdao.create_order import OutletConflict
from tests.feishu.test_trial_approval import bind, click, journal
from tests.feishu.test_trial_paths import settings, setup_card, switches


def fake_delivery(transport):
    transport._tenant_token = AsyncMock(return_value="token")
    response = SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="receipt"))
    transport.im.message.acreate = AsyncMock(return_value=response)


def test_card_conflict_allows_next_card_and_close_notice(tmp_path):
    path = tmp_path / "trial"
    store, _, _, _ = setup_card(path)
    try:
        listener = Listener(path, settings(), switches)
        sender = listener.transport
        fake_delivery(sender)
        with pytest.raises(OutletConflict):
            send_trial_message(sender, {"excerpt": settings().app_secret}, "bad", True, lambda: None)
        listener.maintain()
        listener.maintain()
        assert store.row("SELECT status FROM trial_notification")[0] == "sent"
        assert send_trial_message(sender, {}, "clean", True, lambda: None) == "receipt"
        assert sender.im.message.acreate.await_count == 2
    finally:
        store.db.close()


def test_unreadable_session_records_blocked_close_and_manual_notice(tmp_path):
    path = tmp_path / "trial"
    store, _, _, _ = setup_card(path)
    login = tmp_path / "mingdao-state.json"
    login.write_text('{"cookies":null}')
    login.chmod(0o600)
    try:
        listener = Listener(path, settings(), switches)
        fake_delivery(listener.transport)
        listener.maintain()
        listener.maintain()
        assert dict(store.db.execute("SELECT kind,status FROM trial_notification")) == {
            "closed": "blocked", "manual": "blocked",
        }
        listener.transport._tenant_token.assert_not_awaited()
    finally:
        store.db.close()


def test_click_after_midnight_before_scan_reminds_once(journal):
    bind(journal)
    assert "已核验" in click(journal, "verify", now=86401)
    journal.scan(86402)
    journal.scan(86403)
    assert [r[0] for r in journal.db.execute("SELECT kind FROM trial_notification")] == ["remind"]


@pytest.mark.parametrize("registration,version", [(2, 1), (1, 2)])
def test_wrong_registration_or_version_leaves_no_receipt(journal, registration, version):
    bind(journal)
    result = journal.action("session", "card", registration, version, "message2", "verify",
                            "event", "ou_actor", 86500, lambda: 86500, lambda: True, True, switches())
    assert "不符" in result
    assert not journal.db.execute("SELECT * FROM trial_receipt").fetchall()


def test_short_reject_reason_filtered_before_announcement(journal, tmp_path, monkeypatch, caplog):
    config = replace(settings(), trial_credentials=(("custom", "xYz456"),))
    listener = Listener(tmp_path / "trial", config, switches, clock=lambda: 86002)
    origin = SimpleNamespace(close_code=None)
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
        send_trial_message(sender, "合成通知", "uid", False, lambda: claims.append(1))
    assert posts == claims == []
