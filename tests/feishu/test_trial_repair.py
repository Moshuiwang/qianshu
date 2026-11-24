import asyncio
import json
import sqlite3
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from qianshu.__main__ import _send_trial_message
from qianshu.feishu import sdk
from qianshu.feishu.handoff import HandoffService
from qianshu.feishu.settings import load_handoff_settings
from qianshu.feishu.trial_approval import Journal
from qianshu.feishu.trial_listener import Listener, checked_transport
from qianshu.mail import trial
from qianshu.mingdao.create_order import OutletConflict
from tests.feishu.test_trial_approval import bind, click, journal
from tests.feishu.test_sdk import FakeSocket
from tests.feishu.test_trial_paths import settings, setup_card, switches


def socket_client(monkeypatch, service, socket):
    monkeypatch.setattr(sdk.ws, "loop", asyncio.get_running_loop())
    client = sdk._Connection(service)
    client._discover_endpoint = AsyncMock(return_value="wss://example.invalid/?device_id=d&service_id=1")
    monkeypatch.setattr(sdk.ws.websockets, "connect", AsyncMock(return_value=socket))
    return client


def test_rotate_locked_keeps_receiver_and_rescan(tmp_path, monkeypatch):
    business = tmp_path / "business"
    business.write_text(f"MAIL_PRIVATE_DIR={tmp_path}\nMAIL_HOST=synthetic\nMAIL_USER=local\nMAIL_PASSWORD=secret\nFEISHU_CHAT_ID=group")
    original = Journal.rotate
    sessions = []

    def rotate(journal, session, reason):
        if session:
            sessions.append(session)
            if len(sessions) == 1:
                raise sqlite3.OperationalError("database is locked")
        original(journal, session, reason)

    listeners = []
    monkeypatch.setattr(Journal, "rotate", rotate)
    monkeypatch.setattr(trial, "maintain_approvals", lambda listener, stop: listeners.append(listener))
    ready = Event()
    monkeypatch.setattr(trial.Trial, "run", lambda self: ready.set())

    async def exercise(service):
        assert ready.wait(5)
        origin = FakeSocket()
        client = socket_client(monkeypatch, service, origin)
        try:
            await client._connect()
            assert client.receiver and service._origin is origin and service._scan.is_set()
            listener = listeners[0]
            for _ in range(3):
                try:
                    listener.maintain()
                except sqlite3.OperationalError:
                    assert listener.origin is None
            assert len(sessions) == 2 and listener.origin is origin
            sdk.ws.websockets.connect.assert_awaited_once()
            await client._disconnect()
            assert service._origin is None and listener.origin is None
        finally:
            for task in (client.receiver, client._cache._cron):
                if task:
                    task.cancel()
            await asyncio.gather(*(t for t in (client.receiver, client._cache._cron) if t),
                                 return_exceptions=True)

    monkeypatch.setattr(HandoffService, "run", lambda service: asyncio.run(exercise(service)))
    trial.run_service(SimpleNamespace(business_env_file=business), settings(), tmp_path/"mentions", switches)


def test_failed_socket_setup_closes_connection(monkeypatch):
    async def exercise():
        service = SimpleNamespace(settings=settings(), _remaining=lambda: None, _disconnected=Mock(),
                                  transport=SimpleNamespace(_require_window=Mock()),
                                  _connected=Mock(side_effect=RuntimeError("setup failed")))
        origin = FakeSocket()
        client = socket_client(monkeypatch, service, origin)
        try:
            with pytest.raises(RuntimeError, match="setup failed"):
                await client._connect()
            assert client._conn is None and origin.close_code and client.receiver is None
        finally:
            client._cache._cron.cancel()
            await asyncio.gather(client._cache._cron, return_exceptions=True)
    asyncio.run(exercise())


def fake_delivery(transport):
    transport._tenant_token = AsyncMock(return_value="token")
    response = SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="receipt"))
    transport.im.message.acreate = AsyncMock(return_value=response)


@pytest.mark.parametrize("key", ["APP_ALIAS", "PRIVATE_EXTRA"])
def test_registered_sources_block_actual_card(tmp_path, key):
    config = tmp_path / "config"
    values = {"APP_ALIAS": "synthetic-app-value", "PRIVATE_EXTRA": "other-private-value"}
    config.write_text("FEISHU_APP_ID=app\nFEISHU_APP_SECRET=secret\nFEISHU_CHAT_ID=group\n"
                      "FEISHU_BOT_OPEN_ID=ou_bot\nFEISHU_HANDOFF_OWNER_OPEN_ID=ou_owner\n"
                      "TOKEN_ALIAS=token-value\n" + "\n".join(f"{k}={v}" for k,v in values.items()))
    official = tmp_path / "official"
    official.write_text(json.dumps({"official": {"token_key": "TOKEN_ALIAS", "app_id_key": "APP_ALIAS"}}))
    conf, _ = load_handoff_settings(config, for_listener=True, business_env_file=config, mingdao_config=official)
    login = tmp_path / "mingdao-state.json"
    login.write_text('{"cookies":[]}')
    login.chmod(0o600)
    transport = checked_transport(conf, tmp_path)
    fake_delivery(transport)
    with pytest.raises(OutletConflict):
        _send_trial_message(transport, {"excerpt": values[key]}, "key", True, lambda: None)
    transport.im.message.acreate.assert_not_awaited()
    assert values[key] in transport.sensitive_values


def test_card_conflict_allows_next_card_and_close_notice(tmp_path):
    path = tmp_path / "trial"
    store, _, _, _ = setup_card(path)
    try:
        listener = Listener(path, settings(), switches)
        sender = listener.transport
        fake_delivery(sender)
        with pytest.raises(OutletConflict):
            _send_trial_message(sender, {"excerpt": settings().app_secret}, "bad", True, lambda: None)
        listener.maintain()
        listener.maintain()
        assert store.row("SELECT status FROM trial_notification")[0] == "sent"
        assert _send_trial_message(sender, {}, "clean", True, lambda: None) == "receipt"
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
