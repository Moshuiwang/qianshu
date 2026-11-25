import asyncio
import json
import time
from contextlib import closing
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from qianshu.feishu.trial_listener import ApprovalSettings, Listener, checked_transport, send_trial_message
from qianshu.mail.trial_state import Store


def settings():
    return ApprovalSettings("app", "private-app-key", "group", "bot", frozenset({"owner"}),
                            trial_credentials=(), trial_credentials_complete=True)


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


def test_bound_card_callback(tmp_path, monkeypatch):
    from qianshu.feishu.trial_approval import recovery
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
    assert {r["kind"] for r in recovery(store.db)} >= {"trial_task", "trial_card"}
    store.db.close()


def test_trial_outlet_blocks_before_token_and_sdk(tmp_path):
    login = tmp_path / "mingdao-state.json"
    login.write_text(json.dumps({"cookies": [{"value": "synthetic-session-fragment"}]}))
    login.chmod(0o600)
    transport = checked_transport(settings(), tmp_path)
    transport._tenant_token = AsyncMock(side_effect=AssertionError)
    transport.im.message.acreate = AsyncMock(side_effect=AssertionError)
    with pytest.raises(Exception):
        send_trial_message(transport, {"excerpt": "session-fragment"}, "fixed", True, Mock())
    transport._tenant_token.assert_not_awaited()
    transport.before_send = None
    with pytest.raises(RuntimeError, match="出口检查器"):
        send_trial_message(transport, "notice", "fixed", False, Mock())
    transport._tenant_token.assert_not_awaited()


def test_single_connection_callback_then_disabled_cli_recovery(tmp_path, monkeypatch):
    import sqlite3
    from threading import Event
    from qianshu import __main__ as cli
    from qianshu.feishu import trial_consumer, trial_listener
    from qianshu.feishu.state import State
    from tests.feishu.startup_replay import observe
    from tests.feishu.test_trial_approval import bind, click

    path = tmp_path / "trial-050.sqlite3"
    store = Store(path)
    with store.write() as db:
        from qianshu.feishu.trial_approval import Journal
        db.execute("INSERT INTO mailbox(scope,state) VALUES('s','active')")
        db.execute("INSERT INTO registration(id,scope,version,attachments,status) VALUES(1,'s',1,0,'done')")
    journal = Journal(store.db)
    journal.rotate("session", "连接")
    bind(journal)
    assert "已核验" in click(journal, "verify")
    assert "执行器未就绪" in click(journal, "approve")
    with store.write() as db:
        db.execute("""UPDATE trial_card SET snapshot=json_set(snapshot,'$.contact','ou_config')""")
    store.db.close()
    assert {r["kind"] for r in State.recovery_list(path)} >= {"trial_task", "trial_card"}
    switch_file = tmp_path / "switches"
    switch_file.write_text("")
    monkeypatch.setattr(cli, "load_handoff_settings", lambda *a, **k: (settings(), tmp_path / "mention"))
    monkeypatch.setattr(trial_consumer, "load_approval_settings", lambda *_: (settings(), tmp_path / "mention"))
    delivered = Event()
    observe(monkeypatch, delivered)
    deliveries = []

    def send(transport, content, uuid, card, claim):
        claim()
        deliveries.append(content)
        delivered.set()
        return "ok"

    monkeypatch.setattr(trial_listener, "send_trial_message", send)
    assert cli.main(["--handoff-only", "--trial-switches", str(switch_file)]) == 0
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT status FROM trial_task").fetchone()[0] == "closed"
        bodies = db.execute("SELECT body FROM trial_notification").fetchall()
        assert bodies and all("未建单" in row[0] for row in bodies)
    assert delivered.is_set() and deliveries
    assert all('<at user_id="ou_config">' in message for message in deliveries)


def test_cli_recovery_lists_confirmations_and_approvals(tmp_path, monkeypatch, capsys):
    # 同一试运行登记库：冻结确认请求与审批恢复事项须由守望者一次调用全部列出。
    from qianshu import __main__ as cli
    from qianshu.feishu.trial_approval import Journal
    from qianshu.mail.trial_state import _REQUEST_SCHEMA
    from tests.feishu.test_trial_approval import bind, click

    path = tmp_path / "trial-050.sqlite3"
    store = Store(path)
    with store.write() as db:
        db.execute("INSERT INTO mailbox(scope,state) VALUES('s','active')")
        db.execute("INSERT INTO registration(id,scope,version,attachments,status) VALUES(1,'s',1,0,'done')")
        db.execute(_REQUEST_SCHEMA)
        db.execute("""INSERT INTO recovery_request(scope,generation,validity,cursor,freeze_id,actor,requested)
            VALUES('s',1,1,1,0,0,'2030-01-01T00:00:00+00:00')""")
    journal = Journal(store.db)
    journal.rotate("session", "连接")
    bind(journal)
    assert "已核验" in click(journal, "verify")
    assert "执行器未就绪" in click(journal, "approve")
    store.db.close()
    for key in ("MAIL_USER", "MAIL_PASSWORD", "MINGDAO_ACCOUNT", "MINGDAO_PASSWORD",
                "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "AI_BACKUP_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    assert cli.main(["--state", str(path), "--list-recovery"]) == 0
    output = capsys.readouterr()
    rows = json.loads(output.out)
    confirmations = [row for row in rows if "kind" not in row]
    assert [(row["scope"], row["status"]) for row in confirmations] == [("s", "pending")]
    assert {row["kind"] for row in rows if "kind" in row} >= {"trial_task", "trial_card"}
    assert "确认请求数 1" in output.err and "审批恢复事项数" in output.err
