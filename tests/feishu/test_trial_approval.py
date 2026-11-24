import sqlite3
from contextlib import closing

import pytest

from qianshu.config import ConfigError, trial_config


def test_invalid_create_switch_is_rejected(tmp_path):
    path = tmp_path / "switches"
    path.write_text("MAIL_TRIAL_CREATE_ENABLED=yes")
    with pytest.raises(ConfigError):
        trial_config(path)


@pytest.fixture
def journal(tmp_path):
    from qianshu.mail.trial_state import Store
    from qianshu.feishu.trial_approval import Journal
    store = Store(tmp_path / "trial")
    with store.write() as db:
        db.execute("INSERT INTO mailbox(scope,state) VALUES('s','active')")
        db.execute("INSERT INTO registration(id,scope,version,attachments,status) VALUES(1,'s',1,0,'done')")
    store.db.close()
    with closing(sqlite3.connect(store.path)) as db:
        db.row_factory = sqlite3.Row
        journal = Journal(db)
        journal.rotate("session", "重启")
        yield journal


def bind(j, *, readonly=False, receipt=True):
    j.prepare("card", 1, 1, "session", "区域甲", {"preview": "digest", "eligible": [1],
              "items": [1], "missing": [], "pages": [{"title": "detail"}, {"title": "decision"}]},
              readonly, 86000)
    for n in range(1, 3 if receipt else 2):
        key = f"request{n}"
        j.db.execute("INSERT INTO card_intent VALUES(?,1,1,?,'sent','')", (key, n))
        j.received(key, f"message{n}")
    if receipt:
        j.bind("card", 86001)
    j.db.commit()


def click(j, action, *, now=86500, event=None, enabled=True, member=True, message="message2"):
    return j.action("session", "card", 1, 1, message, action, event or action, "ou_actor",
                    now, lambda: now, lambda: True, member, {
                        "MAIL_TRIAL_CARD_ENABLED": "true", "MAIL_TRIAL_CREATE_ENABLED": str(enabled).lower(),
                        "MAIL_TRIAL_CREATE_COUNTRY": "区域甲", "MAIL_TRIAL_APPROVER_OPEN_IDS": "ou_actor"})


def test_verified_consent_discoverable_and_restart_closes(journal):
    j = journal
    bind(j)
    assert "先核验" in click(j, "approve")
    assert "已核验" in click(j, "verify")
    assert not j.db.execute("SELECT * FROM trial_task").fetchall()
    assert "执行器未就绪" in click(j, "approve", event="second")
    assert j.db.execute("SELECT status FROM trial_task").fetchone()[0] == "executor_unavailable"
    j.rotate("", "全关")
    assert j.db.execute("SELECT status FROM trial_task").fetchone()[0] == "closed"
    assert "未建单" in j.db.execute("SELECT body FROM trial_notification").fetchone()[0]


@pytest.mark.parametrize("case", ["readonly", "unknown_receipt", "wrong_message", "nonmember",
                                   "expired", "disconnected", "attachments", "unknown_count"])
def test_binding_and_batch_refusals(journal, case):
    j = journal
    bind(j, readonly=case == "readonly", receipt=case != "unknown_receipt")
    if case in {"attachments", "unknown_count"}:
        j.db.execute("UPDATE registration SET attachments=?", (1 if case == "attachments" else None,))
        j.db.commit()
    if case == "disconnected":
        j.rotate("", "断线")
    click(j, "verify", member=case != "nonmember", now=172800 if case == "expired" else 86500,
          message="old" if case == "wrong_message" else "message2")
    assert not j.db.execute("SELECT * FROM trial_receipt").fetchall()


def test_disabled_click_cannot_replay(journal):
    bind(journal)
    click(journal, "verify")
    assert click(journal, "approve", enabled=False) == "建单未开启"
    click(journal, "approve")
    assert not journal.db.execute("SELECT * FROM trial_task").fetchall()


def test_midnight_and_reject(journal):
    bind(journal)
    journal.scan(86400)
    journal.scan(86401)
    assert journal.db.execute("SELECT count(*) FROM trial_notification").fetchone()[0] == 1
    assert "驳回" in click(journal, "reject")
    assert "条目 1" in journal.db.execute("SELECT body FROM trial_notification ORDER BY rowid DESC").fetchone()[0]


def test_expiry_closes_verified_card_without_reminding(journal):
    bind(journal)
    assert "已核验" in click(journal, "verify", now=86300)
    journal.scan(86400)
    assert not journal.db.execute("SELECT * FROM trial_notification").fetchall()
    journal.scan(172800)
    journal.rotate("new", "重启")
    events = journal.db.execute("SELECT kind,body FROM trial_notification").fetchall()
    assert len(events) == 1 and events[0][0] == "closed"
    assert "条目 1" in events[0][1] and "到期，未建单" in events[0][1]


def test_disconnect_after_consent_closes_task_and_old_receipt(journal):
    bind(journal)
    click(journal, "verify")
    click(journal, "approve")
    journal.rotate("", "审批连接断开")
    journal.rotate("new", "重连")
    assert journal.db.execute("SELECT status FROM trial_task").fetchone()[0] == "closed"
    body, = journal.db.execute("SELECT body FROM trial_notification").fetchone()
    assert "条目 1" in body and "审批连接断开，未建单" in body
    assert "不符" in click(journal, "approve", event="replayed")


@pytest.mark.parametrize("attachments", [2, None])
def test_matching_x6_registration_and_old_attachment_action_cannot_override_batch(journal, tmp_path, attachments):
    import json
    bind(journal)
    journal.db.execute("UPDATE registration SET attachments=?", (attachments,))
    journal.db.commit()
    registration = tmp_path / "registration"
    registration.write_text(json.dumps({"card": "card", "registration": 1, "version": 1, "entry": 1,
                                        "test_name": "【测试】合成条目", "expires": 172800}))
    registration.chmod(0o600)
    config = {"MAIL_TRIAL_CARD_ENABLED": "true", "MAIL_TRIAL_CREATE_ENABLED": "true",
              "MAIL_TRIAL_CREATE_COUNTRY": "区域甲", "MAIL_TRIAL_APPROVER_OPEN_IDS": "ou_actor",
              "MAIL_TRIAL_TEST_REGISTRATION_FILE": str(registration)}
    for action in ("confirm_attachment", "verify", "approve"):
        journal.action("session", "card", 1, 1, "message2", action, action, "ou_actor", 86500,
                       lambda: 86500, lambda: True, True, config)
    assert not journal.db.execute("SELECT * FROM trial_receipt").fetchall()
    assert not journal.db.execute("SELECT * FROM trial_task").fetchall()
