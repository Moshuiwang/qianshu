import asyncio
import json
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from qianshu.feishu.settings import Settings
from qianshu.mail.trial_state import Store
from tests.feishu.startup_replay import observe


def settings():
    return Settings("app", "private-app-key", "group", "bot", frozenset({"owner"}))


def switches():
    return dict(MAIL_TRIAL_RECEIVE_ENABLED="true", MAIL_TRIAL_CARD_ENABLED="true",
                MAIL_TRIAL_CREATE_ENABLED="true", MAIL_TRIAL_CREATE_COUNTRY="区域甲",
                MAIL_TRIAL_APPROVER_OPEN_IDS="ou_actor")


def setup_card(path, *, countries=("区域甲", "区域甲"), attachments=0):
    from dataclasses import fields
    from qianshu.draft import DraftField, OfferPolicyDraft
    from qianshu.feishu.trial_approval import Journal
    from qianshu.mail.trial_cards import decision_pages
    from qianshu.parse import ParsedPolicyDraft
    from qianshu.parse.protocol import FieldResult, StatementStatus
    from qianshu.pipeline import _Policy
    from qianshu.validate import ValidationResult
    store = Store(path)
    day = datetime(2030, 1, 1, 1, tzinfo=UTC)
    with store.write() as db:
        db.execute("INSERT INTO mailbox(scope,state) VALUES('s','active')")
        db.execute("INSERT INTO registration(id,scope,version,status,attachments) VALUES(1,'s',1,'done',?)",
                   (attachments,))
    j = Journal(store.db)
    j.rotate("session", "启动")
    text = "\n".join(f"{f}：明细{n}" for n, f in enumerate(
        ("活动周期", "每个标签的用户数", "政策内容", "使用场景", "标签名称")))
    trial = SimpleNamespace(store=store, config={}, clock=lambda: day, sensitive=())
    drafts, policies = [], []
    for n, country in enumerate(countries):
        draft = ParsedPolicyDraft(**{f.name: DraftField() for f in fields(OfferPolicyDraft)})
        draft.policy_type.value = "定向政策"
        draft.trigger_once.value = "否"
        draft.effective_multiplier.value = 1
        draft.maintain_mutual_exclusion_group.value = "是"
        draft.benefit_type.value = f"组合{n}"
        draft.source_span = (0, len(text))
        # 仅替换模型返回的国家事实，仍调用正式的资格检查和预览。
        field = FieldResult(SimpleNamespace(status=StatementStatus.CERTAIN), country, bool(country),
                            needs_confirmation=not country)
        draft.field_results = {"country": field}
        draft.to_dict = lambda: {"sample": True}
        drafts.append(draft)
        policies.append(_Policy(draft, (), {}, ValidationResult(())))
    pages = []
    card = decision_pages(trial, store.row("SELECT * FROM registration"), text, pages, drafts, policies, False,
                          (switches(), {"verified_combinations": [["定向政策", "组合0"]]}, "ou_config"),
                          attachments != 0)
    return store, j, card, pages


@pytest.mark.parametrize("countries", [("区域乙",), ("",), ("区域甲", "区域乙")])
def test_review_only_countries(tmp_path, countries):
    store, _, _, pages = setup_card(tmp_path / "db", countries=countries)
    with closing(store.db):
        assert store.row("SELECT readonly FROM trial_card")[0] == 1
        display = json.dumps(pages, ensure_ascii=False)
        assert "本国未开建单，仅供核对" in display and '"button"' not in display


@pytest.mark.parametrize("attachments", [1, None])
def test_attachment_batch_has_no_preview_or_actions(tmp_path, attachments):
    store, _, _, pages = setup_card(tmp_path / "db", attachments=attachments)
    with closing(store.db):
        snapshot = json.loads(store.row("SELECT snapshot FROM trial_card")[0])
        assert snapshot["eligible"] == [] and snapshot["preview"] is None
        display = json.dumps(pages, ensure_ascii=False)
        assert "整批转人工" in display and '"button"' not in display


def test_mixed_combinations_bind_complete_preview(tmp_path):
    store, _, _, pages = setup_card(tmp_path / "db")
    with closing(store.db):
        snapshot = json.loads(store.row("SELECT snapshot FROM trial_card")[0])
        assert snapshot["pages"] == pages and snapshot["eligible"] == [1]
        assert len(snapshot["preview"]) == 64
        display = json.dumps(pages, ensure_ascii=False)
        assert all(s in display for s in ("该类型尚未经真实回读验证", "存档预览", "定向政策汇总行"))
        assert [b["value"]["action"] for b in pages[-1]["elements"][-1]["elements"][-1]["actions"]] == ["verify", "approve", "reject"]


def test_single_connection_callback_then_disabled_cli_recovery(tmp_path, monkeypatch):
    from qianshu import __main__ as cli
    from qianshu.feishu.state import State
    from qianshu.feishu.trial_listener import Listener
    from qianshu.feishu.trial_approval import Journal
    path = tmp_path / "trial-050.sqlite3"
    store, journal, card, pages = setup_card(path)
    now = [datetime(2030, 1, 1, 1, tzinfo=UTC).timestamp()]
    checked = SimpleNamespace(is_member=AsyncMock(return_value=True))
    monkeypatch.setattr("qianshu.feishu.trial_listener.checked_transport", lambda *_: checked)
    # 建立连接后才准备实际卡片。
    listener = Listener(path, settings(), switches, clock=lambda: now[0])
    origin = SimpleNamespace(close_code=None)
    listener.rotate(origin, "连接")
    with store.write() as db:
        db.execute("UPDATE registration SET version=2")
    journal.prepare("live", 1, 2, listener.session, "区域甲",
                    json.loads(store.row("SELECT snapshot FROM trial_card WHERE card=?", card)[0]),
                    False, now[0])
    def trigger(action, actor="ou_actor", **changes):
        now[0] += 1
        event = SimpleNamespace(context=SimpleNamespace(open_message_id="message", open_chat_id="group"),
                                operator=SimpleNamespace(open_id=actor),
                                action=SimpleNamespace(value=dict(card="live", format=1, registration=1,
                                                                  version=2, action=action)))
        for key, value in changes.items():
            setattr(event.context, key, value)
        header = SimpleNamespace(event_type="card.action.trigger", event_id=str(now[0]), create_time=str(int(now[0])))
        return asyncio.run(listener.record(SimpleNamespace(event=event, header=header), origin, time.monotonic() + 2.5)).toast.content

    assert "不符" in trigger("verify")
    with store.write() as db:
        for i in range(1, len(pages)+1):
            key = str(i)
            db.execute("INSERT INTO card_intent VALUES(?,1,2,?,'sent','')", (key, i))
            journal.received(key, "message" if i == len(pages) else "page" + key)
        journal.bind("live", now[0])
    assert "名单不符" in trigger("reject", actor="ou_outside")
    assert "来源不符" in trigger("verify", open_chat_id="other")
    checked.is_member.side_effect = TimeoutError
    assert "未能确认" in trigger("verify")
    checked.is_member.side_effect = None
    assert "已核验" in trigger("verify")
    assert "执行器未就绪" in trigger("approve")
    store.db.close()
    assert {r["kind"] for r in State.recovery_list(path)} >= {"trial_task", "trial_card"}

    switch_file = tmp_path / "switches"
    switch_file.write_text("")
    monkeypatch.setattr(cli, "load_handoff_settings", lambda *a, **k: (settings(), tmp_path/"mention"))
    delivered = Event()
    observe(monkeypatch, delivered)
    deliveries = []
    def send(transport, content, uuid, card, claim):
        claim()
        deliveries.append(content)
        delivered.set()
        return "ok"
    monkeypatch.setattr(cli, "_send_trial_message", send)
    assert cli.main(["--handoff-only", "--trial-switches", str(switch_file)]) == 0
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT status FROM trial_task").fetchone()[0] == "closed"
        bodies = db.execute("SELECT body FROM trial_notification").fetchall()
        assert bodies and all("未建单" in row[0] for row in bodies)
    assert delivered.is_set() and deliveries
    assert all('<at user_id="ou_config">' in message for message in deliveries)


def test_trial_outlet_blocks_before_token_and_sdk(tmp_path):
    from qianshu.__main__ import _send_trial_message
    from qianshu.feishu.trial_listener import checked_transport
    login = tmp_path / "mingdao-state.json"
    login.write_text(json.dumps({"cookies": [{"value": "synthetic-session-fragment"}]}))
    login.chmod(0o600)
    transport = checked_transport(settings(), tmp_path)
    transport._tenant_token = AsyncMock(side_effect=AssertionError)
    transport.im.message.acreate = AsyncMock(side_effect=AssertionError)
    with pytest.raises(Exception):
        _send_trial_message(transport, {"excerpt": "session-fragment"}, "fixed", True, Mock())
    transport._tenant_token.assert_not_awaited()
    transport.before_send = None
    with pytest.raises(RuntimeError, match="出口检查器"):
        _send_trial_message(transport, "notice", "fixed", False, Mock())
    transport._tenant_token.assert_not_awaited()


def test_one_socket_keeps_handoff_deadline_and_routes_card(monkeypatch):
    from qianshu.feishu.sdk import P2CardActionTriggerResponse, _Connection, ws
    from tests.feishu.test_sdk import FakeSocket, encoded_frame

    async def exercise():
        monkeypatch.setattr(ws, "loop", asyncio.get_running_loop())
        origin = FakeSocket()
        record = AsyncMock(return_value=P2CardActionTriggerResponse({"toast": {"type": "info", "content": "已记录"}}))
        inject = Mock()
        service = SimpleNamespace(settings=settings(), _origin=origin, _record_action=record,
                                  trial_callbacks=True, _callback_response_margin=0,
                                  entrance=SimpleNamespace(inject_message=inject))
        connection = _Connection(service, approvals=True)
        connection._conn = origin
        timing = connection._frame_timing(origin)
        header = {"app_id": "app", "event_id": "event", "event_type": "im.message.receive_v1"}
        message = {"sender": {"sender_type": "user", "sender_id": {"open_id": "owner"}},
                   "message": {"message_id": "m", "chat_id": "group", "chat_type": "group", "message_type": "text",
                               "create_time": "1000", "content": "{}", "mentions": [{"key": "@1", "id": {"open_id": "bot"}}]}}
        try:
            await connection._handle_message(encoded_frame(dict(schema="2.0", header=header, event=message)), timing)
            assert inject.call_args.kwargs == {"deadline": timing[-1]}
            header["event_type"] = "card.action.trigger"
            for app in ("other", "app"):
                header["app_id"] = app
                await connection._handle_message(encoded_frame(dict(schema="2.0", header=header, event={}),
                                                               ws.MessageType.CARD), timing)
            record.assert_awaited_once()
            assert origin.responses == ["event_ack", "warning", "info"]
        finally:
            connection._cache._cron.cancel()
            await asyncio.gather(connection._cache._cron, return_exceptions=True)

    asyncio.run(exercise())
