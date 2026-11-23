from contextlib import closing

import httpx
import pytest

from qianshu.mail.trial_state import MANUAL_BODY_MISSED, Store
from test_trial_journey import journey, mixed, revision_trial
from trial_segments import Inbox


@pytest.mark.parametrize("rollover", [True, False])
def test_reused_terminal_is_revoked(journey, rollover):
    msg = mixed(("plain", "CURRENT"), ("html", "OTHER"))
    worker, inbox, _, cards = journey(msg, "否")
    store = worker.store
    with store.write() as db:
        db.execute("DELETE FROM missed_body")
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
    assert store.batch(1, row["version"])[3] == "正文未参与解析"
    assert sum("正文未参与解析" in str(content) for _, content in cards) == 1


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


def test_pending_warning_follows_verified_generation(tmp_path, monkeypatch):
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
    inbox.generation += 1
    worker.poll()
    box = worker.box()
    worker.store.reconcile(worker.scope, box["generation"], box["validity"], box["cursor"])
    worker.switches["MAIL_TRIAL_CARD_ENABLED"] = "true"
    worker.poll()
    assert sum("正文未参与解析" in str(content) for _, content in cards) == 1
    assert worker.store.body_notice_counts() == (0, 0)


def negative_verdict(config, item, reserve):
    with reserve(httpx.Request("POST", "/", json={"model": "claude-haiku-5-5", "max_tokens": 12})):
        return "否", None, 10, 2


@pytest.mark.parametrize("page_status,expected", [(None, "pending"), ("unknown", "unknown"), ("sent", "sent")])
def test_upgrade_recovers_warning_page_evidence(journey, page_status, expected):
    worker, _, _, _ = journey(mixed(("plain", "CURRENT"), ("html", "OTHER")), "否")
    store = worker.store
    with store.write() as db:
        db.execute("DELETE FROM notice")
        if page_status:
            db.execute("INSERT INTO card_intent VALUES('page',1,1,1,?,'')", (page_status,))
    with closing(Store(store.path).db) as reopened:
        assert reopened.execute("SELECT status FROM notice WHERE kind='body_missed'").fetchone()[0] == expected
