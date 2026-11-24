from functools import partial
from unittest.mock import AsyncMock

from qianshu.__main__ import _send_trial_message
from qianshu.feishu.state import State
from qianshu.feishu.trial_listener import checked_transport
from tests.feishu.test_trial_paths import settings
from tests.feishu.test_trial_repair import fake_delivery
from test_trial_journey import journey, mixed


def test_unavailable_source_alerts_local_operator(journey, tmp_path, caplog):
    worker, *_ = journey(mixed(("plain", "合成正文")), "是")
    source = tmp_path / "mingdao-state.json"
    source.write_text('{"cookies":null}')
    source.chmod(0o600)
    sender = checked_transport(settings(), tmp_path)
    fake_delivery(sender)
    worker.send = partial(_send_trial_message, sender)
    with worker.store.write():
        worker.store.notice("received:1", worker.scope, 1, "received")
    worker.flush()
    worker.flush()
    sender.im.message.acreate.assert_not_awaited()
    assert any(row.get("notice") == "blocked" and row["status"] == "blocked"
               for row in State.recovery_list(worker.store.path))
    assert "E-TRIAL-MANUAL" in caplog.text


def test_manual_notice_token_timeout_retries(journey):
    worker, *_ = journey(mixed(("plain", "合成正文")), "是")
    with worker.store.write():
        worker.blocked_handoff(1)
    sender = checked_transport(settings())
    fake_delivery(sender)
    sender._tenant_token = AsyncMock(side_effect=[TimeoutError(), "token"])
    worker.send = partial(_send_trial_message, sender)
    worker.flush()
    assert worker.store.row("SELECT status FROM notice WHERE kind='blocked'")[0] == "pending"
    worker.flush()
    assert worker.store.row("SELECT status FROM notice WHERE kind='blocked'")[0] == "sent"
    sender.im.message.acreate.assert_awaited_once()


def test_message_post_timeout_preserves_unknown(journey, monkeypatch):
    import httpx

    worker, *_ = journey(mixed(("plain", "合成正文")), "是")
    with worker.store.write():
        worker.blocked_handoff(1)
    sender = checked_transport(settings())
    sender._tenant_token = AsyncMock(return_value="token")
    states = []

    def post(request):
        states.append(worker.store.row("SELECT status FROM notice WHERE kind='blocked'")[0])
        raise httpx.ReadTimeout("synthetic")

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(post), **kw))
    worker.send = partial(_send_trial_message, sender)
    worker.flush()
    worker.flush()
    assert states == ["unknown"]
    assert worker.store.row("SELECT status FROM notice WHERE kind='blocked'")[0] == "unknown"


def test_receipt_after_midnight_uses_handoff_day(journey, monkeypatch):
    from datetime import UTC, datetime, timedelta
    from qianshu.feishu.trial_approval import Journal
    from test_trial_g448 import _call, _drive

    worker, *_ = journey(mixed(("plain", "合成正文")), "是")
    Journal(worker.store.db).rotate("session", "连接")
    worker.approval_setup = lambda: ({}, {}, "ou_config")
    _drive(monkeypatch, [_call(2)])["trial"] = worker
    sent = datetime(2030, 1, 1, 23, 59, 59, tzinfo=UTC)
    now = [sent]
    worker.clock = lambda: now[0]

    def deliver(card, key, interactive, check):
        check()
        page = worker.store.row("SELECT page FROM card_intent WHERE uuid=?", key)[0]
        total = worker.store.row("SELECT json_array_length(snapshot, '$.pages') FROM trial_card")[0]
        if page == total:
            now[0] += timedelta(seconds=2)
        return key

    worker.send = deliver
    worker.poll()
    assert now[0].day == 2
    assert tuple(worker.store.row("SELECT sent,expires FROM trial_card")) == (
        sent.timestamp(), datetime(2030, 1, 3, tzinfo=UTC).timestamp())
