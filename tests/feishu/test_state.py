import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from qianshu.feishu import State


def test_serializes_concurrent_claims_and_transitions(tmp_path, settings):
    state = State(tmp_path / "private.sqlite", settings, busy_timeout=0.01)
    start = Barrier(8)
    rounds = range(50)
    try:
        for i in rounds:
            state.bind(f"<审批占位-{i}>", f"<卡片占位-{i}>", "<记录占位>", 200)

        def write(worker):
            start.wait(timeout=5)
            message_claims = callback_claims = approvals = 0
            for i in rounds:
                unique = f"<独有消息占位-{worker}-{i}>"
                shared = f"<共享消息占位-{i}>"
                assert state.claim_message(unique, "notice_pending")
                if state.claim_message(shared, "notice_pending"):
                    message_claims += 1
                    state.complete_notice(shared)
                event = f"<回调占位-{i}>"
                if state.claim_callback(event):
                    callback_claims += 1
                # 每个线程另认领一个独有回调，竞争同一张卡。
                event = f"<点击占位-{worker}-{i}>"
                assert state.claim_callback(event)
                approvals += state.decide(
                    f"<审批占位-{i}>", f"<卡片占位-{i}>", "<记录占位>", "approve",
                    event, "<点击人占位>", lambda: 150,
                )
                state.complete_notice(unique)
                assert state.message_status(unique) == "done"
            return message_claims, callback_claims, approvals

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(write, range(8)))
        assert [sum(counts) for counts in zip(*results)] == [50, 50, 50]
        with state.connection() as db:
            assert db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
            assert db.execute("PRAGMA busy_timeout").fetchone() == (10,)
            assert db.execute("SELECT status, count(*) FROM feishu_messages GROUP BY status").fetchall() == [
                ("done", 450),
            ]
            assert db.execute("SELECT status, count(*) FROM feishu_approvals GROUP BY status").fetchall() == [
                ("approve", 50),
            ]
    finally:
        state.close()


def test_callback_claims_survive_restart_and_import_prior_decisions(tmp_path, settings):
    path = tmp_path / "private.sqlite"
    state = State(path, settings)
    prior = "<旧回调占位>"
    current = "<已认领回调占位>"
    state.bind("<审批占位>", "<卡片占位>", "<记录占位>", 200)
    state.close()

    # 模拟本轮以前的库，旧回调只留在审批表。
    with sqlite3.connect(path, autocommit=True) as db:
        db.execute("DROP TABLE feishu_callback_events")
        db.execute("UPDATE feishu_approvals SET status = 'approve', event_id = ?", (prior,))
    db.close()

    state = State(path, settings)
    try:
        assert not state.claim_callback(prior)
        assert state.claim_callback(current)
    finally:
        state.close()

    reopened = State(path, settings)
    try:
        assert not reopened.claim_callback(prior)
        assert not reopened.claim_callback(current)
    finally:
        reopened.close()


@pytest.mark.parametrize("close_at", ["before_click", "before_claim", "after_claim", "after_dispatch"])
def test_closed_approval_cannot_grant_or_reuse_create_claim(tmp_path, settings, close_at):
    state = State(tmp_path / "private.sqlite", settings)
    approval, card, record, event = ("<审批占位>", "<卡片占位>", "<记录占位>", "<回调占位>")
    sent = []
    try:
        state.bind(approval, card, record, 200)
        if close_at == "before_click":
            state.close_approvals()
        assert state.claim_callback(event)
        assert state.decide(approval, card, record, "approve", event, "<点击人占位>", lambda: 150) == (
            close_at != "before_click"
        )
        if close_at == "before_claim":
            state.close_create_approval(approval, record)
        expected_claim = 200 if close_at in ("after_claim", "after_dispatch") else None
        assert state.claim_create(approval, record) == expected_claim
        assert state.claim_create(approval, record) is None
        if close_at == "after_claim":
            state.close_approvals()
        assert state.dispatch_create(approval, record, sent.append) == (close_at == "after_dispatch")
        state.close_approvals()
        assert not state.dispatch_create(approval, record, sent.append)
        assert sent == ([200] if close_at == "after_dispatch" else [])
    finally:
        state.close()
