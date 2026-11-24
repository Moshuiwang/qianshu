import sqlite3
from contextlib import closing
from unittest.mock import Mock

import httpx
import pytest

from qianshu.mail.trial_state import BODY_NOTICE_COUNTS_SQL, MANUAL_BODY_MISSED, FormatHold, Store
from test_trial_listener import service
from test_trial_journey import journey, mixed, revision_trial
from trial_segments import Inbox


@pytest.mark.parametrize("rollover", [True, False])
def test_reused_terminal_is_revoked(journey, rollover):
    msg = mixed(("plain", "CURRENT"), ("html", "OTHER"))
    worker, inbox, _, cards = journey(msg, "否")
    store = worker.store
    with store.write() as db:
        db.execute("DELETE FROM body_shape")
        db.execute("DELETE FROM notice")
        db.execute("UPDATE registration SET status='done'")
    cards.clear()
    if rollover:
        inbox.generation += 1
        worker.poll()
        box = worker.box()
        store.reconcile(worker.scope, box["generation"], box["validity"], box["cursor"])
        assert store.row("SELECT status FROM registration WHERE id=1")[0] == MANUAL_BODY_MISSED
    inbox.add(msg)
    worker.poll()
    row = store.row("SELECT * FROM registration WHERE id=1")
    assert row["status"] == MANUAL_BODY_MISSED and row["version"] > 1
    assert store.batch(1, 1)[3] == "登记缺失或版本失效"
    assert store.batch(1, row["version"])[3] == "正文结构超出自动处理范围，请人工核对原邮件"
    assert sum("正文结构超出自动处理范围，请人工核对原邮件" in str(content) for _, content in cards) == int(not rollover)


@pytest.mark.parametrize("crossed", [False, True])
def test_failed_warning_delivery(journey, crossed):
    worker, _, _, _ = journey(mixed(("plain", "CURRENT"), ("html", "OTHER")), "是")

    send = worker.send

    def fail(content, uid, card, check):
        if crossed:
            check()
        raise RuntimeError

    worker.send = fail
    worker.poll()
    expected = "unknown" if crossed else "pending"
    assert tuple(worker.store.row("SELECT status FROM notice WHERE kind='body_missed'") or ()) == (expected,)
    assert worker.store.body_notice_counts() == (int(not crossed), int(crossed))
    notice_id = worker.store.row("SELECT uuid FROM notice WHERE kind='body_missed'")[0]
    assert worker.store.row("SELECT uuid FROM card_intent WHERE page=1")[0] == notice_id
    worker.send = send
    worker.flush()
    final = "unknown" if crossed else "sent"
    assert worker.store.row("SELECT status FROM notice WHERE kind='body_missed'")[0] == final


@pytest.mark.parametrize("late", [False, True])
def test_pending_warning_requires_review_after_rollover(tmp_path, monkeypatch, late):
    # 使用同一轮询入口，只在换代后打开出卡。
    cards, inbox = [], Inbox()
    store = Store(tmp_path / "trial")
    worker = revision_trial(store, inbox, cards)
    worker.switches["MAIL_TRIAL_CARD_ENABLED"] = "false"
    worker.poll()
    store.import_spend("0")
    worker.input_limits["claude-haiku-5-5"] = 100000
    monkeypatch.setattr("qianshu.mail.trial.classify", negative_verdict)
    inbox.add(mixed(("plain", "CURRENT"), ("html", "OTHER")))
    worker.poll()
    original = inbox.messages[2]
    if late:
        del inbox.messages[2]
    inbox.generation += 1
    worker.poll()
    box = worker.box()
    worker.store.reconcile(worker.scope, box["generation"], box["validity"], box["cursor"])
    worker.switches["MAIL_TRIAL_CARD_ENABLED"] = "true"
    worker.poll()
    if late:
        inbox.add(original)
        worker.poll()
    assert not any(card for card, _ in cards)
    assert worker.store.body_notice_counts() == (0, 1)
    with closing(sqlite3.connect(store.path.as_uri() + "?mode=ro", uri=True)) as report:
        assert report.execute(BODY_NOTICE_COUNTS_SQL).fetchone() == (0, 1)


def negative_verdict(config, item, reserve):
    with reserve(httpx.Request("POST", "/", json={"model": "claude-haiku-5-5", "max_tokens": 12})):
        return "否", None, 10, 2


@pytest.mark.parametrize("page_status", [None, "unknown", "sent"])
def test_old_format_freezes_with_any_page_evidence(journey, page_status):
    worker, _, _, _ = journey(mixed(("plain", "CURRENT"), ("html", "OTHER")), "否")
    store = worker.store
    with store.write() as db:
        db.execute("PRAGMA user_version=0")
        db.execute("DELETE FROM notice")
        if page_status:
            db.execute("INSERT INTO card_intent VALUES('page',1,1,1,?,'')", (page_status,))
    with pytest.raises(FormatHold, match="登记库格式版本不符，已整体冻结，请人工处理"):
        Store(store.path)


@pytest.mark.parametrize("version", [0, 91])
def test_incompatible_format_blocks_old_card(journey, version):
    worker, _, requests, cards = journey(mixed(("plain", "CURRENT")), "否")
    store = worker.store
    with store.write() as db:
        db.execute("UPDATE registration SET status='done'")
        db.execute(f"PRAGMA user_version={version}")
    requests.clear()
    cards.clear()
    with pytest.raises(FormatHold, match="登记库格式版本不符"):
        worker.store = Store(store.path)
        worker.poll()
    assert not requests and not cards
    assert store.batch(1, 1)[3] == "登记库格式版本不符，已整体冻结，请人工处理"


def test_existing_empty_file_is_unversioned(tmp_path):
    path = tmp_path / "trial"
    path.touch(mode=0o600)
    with pytest.raises(FormatHold, match="登记库格式版本不符"):
        Store(path)
    with closing(sqlite3.connect(path)) as db:
        assert not db.execute("SELECT name FROM sqlite_schema").fetchall()

def test_service_stops_before_processing_old_store(service, monkeypatch, caplog):
    path, settings, start = service
    state = path.with_name("trial-050.sqlite3")
    store = Store(state)
    with closing(store.db) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] > 0
        db.execute("PRAGMA user_version=0")
    original = state.read_bytes()
    run_mail = Mock()
    monkeypatch.setattr("qianshu.mail.trial.Trial.run", run_mail)
    monkeypatch.setattr("qianshu.feishu.handoff.HandoffService.run", lambda self: None)
    start(settings)
    run_mail.assert_not_called()
    assert "登记库格式版本不符，已整体冻结，请人工处理" in caplog.text
    assert state.read_bytes() == original


def test_flush_marks_mismatched_pending_for_review(journey):
    worker, _, _, cards = journey(mixed(("plain", "CURRENT"), ("plain", "OTHER")), "否")
    with worker.store.write() as db:
        db.execute("UPDATE notice SET generation=0, status='pending' WHERE kind='body_missed'")
    cards.clear()
    worker.flush()
    assert not cards
    assert worker.store.body_notice_counts() == (0, 1)
