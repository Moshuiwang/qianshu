import sqlite3
from contextlib import closing
from threading import Event

import pytest

from qianshu.feishu.handoff import HandoffService
from qianshu.mail.trial import Trial
from qianshu.mail.trial_state import Store
from test_trial_journey import revision_trial
from test_trial_listener import service
from trial_segments import Inbox


@pytest.mark.parametrize("changed,observed", [(False, False), (True, False), (False, True)])
def test_dirty_restart_reconciles_search_gaps(service, monkeypatch, changed, observed):
    path, settings, start = service
    inbox = Inbox()
    inbox.messages.clear()
    inbox.next_uid = 100
    state = path.with_name("trial-050.sqlite3")
    store = Store(state)
    trial = revision_trial(store, inbox, [])
    trial.poll()
    if observed:
        store.observe(trial.scope, 1, 100, 102, [101])
    store.db.close()
    state.with_suffix(".dirty").touch()
    inbox.generation += changed
    inbox.next_uid = 104 if observed else 102
    done = Event()

    def scan(worker):
        try:
            restart = revision_trial(worker.store, inbox, [])
            assert restart.box()["reason"] == "unclean_close"
            restart.poll()
        finally:
            done.set()

    monkeypatch.setattr(Trial, "run", scan)
    monkeypatch.setattr(HandoffService, "run", lambda self: done.wait(5))
    start(settings)
    store = Store(state)
    with closing(store.db):
        box = store.row("SELECT * FROM mailbox")
        assert box["reason"] == "reconciling"
        store.reconcile(trial.scope, box["generation"], inbox.generation, inbox.next_uid)
        assert store.row("SELECT state FROM mailbox")[0] == "active"
        ranges = [tuple(r) for r in store.db.execute("SELECT first,last FROM unknown_range ORDER BY first")]
        expected = [(100, 101)]
        if changed:
            expected = []
        elif observed:
            # 101 已观察但未登记、冻结期间消失，须登记为缺口（G471，修 main 原有的冻结重建缺陷）。
            expected = [(100, 100), (101, 103)]
        assert ranges == expected
        assert [tuple(r) for r in store.db.execute(
            "SELECT quantity,status FROM notice WHERE kind='gap' ORDER BY rowid"
        )] == [(last - first + 1, "sent") for first, last in expected]


def test_constructor_close_failure_has_critical_diagnostic(service, monkeypatch):
    path, settings, start = service
    state = path.with_name("trial-050.sqlite3")
    state.touch(mode=0o600)
    closed = Event()
    connect = sqlite3.connect

    class BrokenClose(sqlite3.Connection):
        def close(self):
            closed.set()
            raise OSError

    def open_database(target, *args, **kwargs):
        if target == state:
            kwargs["factory"] = BrokenClose
        return connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", open_database)
    monkeypatch.setattr(HandoffService, "run", lambda self: closed.wait(5))
    start(settings)
    assert closed.is_set() and state.with_suffix(".dirty").exists()
    assert "CRITICAL 收信登记库关闭失败" in state.with_suffix(".fault").read_text()


def test_full_diagnostic_queue_counts_drops():
    from qianshu.mail.trial_diagnostics import Writer

    entered, release = Event(), Event()
    written = []

    def blocked(value):
        entered.set()
        release.wait()
        written.append(value)

    writer = Writer(blocked, "test-diagnostic")
    try:
        writer.post(0)
        assert entered.wait(2)
        for value in range(1, writer.pending.maxsize + 2):
            writer.post(value)
        assert writer.dropped == 1
    finally:
        release.set()
        writer.finish()
    assert written == list(range(writer.pending.maxsize + 1))
