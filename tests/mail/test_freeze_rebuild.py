from contextlib import closing
from threading import Event

import pytest

from qianshu.mail.trial_diagnostics import Writer
from qianshu.mail.trial_state import Hold, Store
from test_trial_journey import revision_trial
from trial_segments import Inbox, synthetic


@pytest.mark.parametrize("reason", ["unclean_close", "missing"])
def test_freeze_rebuild_keeps_vanished_positions_main_regression(tmp_path, reason):
    store = Store(tmp_path / "trial")
    inbox = Inbox()
    inbox.messages.clear()
    inbox.next_uid = 100
    sent = []
    trial = revision_trial(store, inbox, sent)
    with closing(store.db):
        trial.poll()
        inbox.next_uid = 102
        inbox.messages[101] = synthetic(101, "pending")
        store.observe(trial.scope, 1, 100, 102, [101])
        store.freeze(trial.scope, reason)
        inbox.messages.clear()
        trial.poll()
        store.reconcile(trial.scope, trial.box()["generation"], 1, 102)

    reopened = Store(store.path)
    with closing(reopened.db):
        assert tuple(reopened.row("SELECT state,cursor FROM mailbox")) == ("active", 102)
        assert [tuple(r) for r in reopened.db.execute(
            "SELECT first,last FROM unknown_range ORDER BY first"
        )] == [(100, 100), (101, 101)]
        assert [tuple(r) for r in reopened.db.execute(
            "SELECT quantity,status FROM notice WHERE kind='gap'"
        )] == [(1, "sent"), (1, "sent")]


def test_freeze_rebuild_keeps_vanished_snapshot_main_regression(tmp_path):
    store = Store(tmp_path / "trial")
    inbox = Inbox()
    inbox.next_uid = 100
    inbox.messages.clear()
    trial = revision_trial(store, inbox, [])
    with closing(store.db):
        trial.poll()
        store.freeze(trial.scope, "mapping")
        inbox.add(synthetic(100, "snapshot"))
        trial.poll()
        inbox.messages.clear()
        inbox.next_uid = 103
        trial.poll()
        store.reconcile(trial.scope, trial.box()["generation"], 1, 103)
        assert tuple(store.row("SELECT state,cursor FROM mailbox")) == ("active", 103)
        gaps = list(store.db.execute("SELECT first,last FROM unknown_range ORDER BY first"))
        assert [uid for a, b in gaps for uid in range(a, b + 1)] == [100, 101, 102]
        assert store.row("SELECT sum(quantity) FROM notice WHERE kind='gap' AND status='sent'")[0] == 3


def test_reconcile_requires_notice_for_vanished_pending(tmp_path):
    store = Store(tmp_path / "trial")
    with closing(store.db):
        store.begin("scope")
        store.activate("scope", 1, 100, 0)
        store.observe("scope", 1, 100, 101, [100])
        store.freeze("scope", "missing")
        store.rebuild("scope", 1, 101, 0, [])
        with store.write() as db:
            db.execute("DELETE FROM notice WHERE kind='gap'")
        with pytest.raises(Hold):
            store.reconcile("scope", 2, 1, 101)
        assert store.row("SELECT state FROM mailbox")[0] == "frozen"


def test_diagnostic_timeout_counts_abandoned_queue():
    entered, release = Event(), Event()
    written = []

    def blocked(value):
        entered.set()
        release.wait()
        written.append(value)

    writer = Writer(blocked, "test-diagnostics-timeout")
    try:
        writer.post(0)
        assert entered.wait(2)
        for value in range(writer.pending.maxsize + 1):
            writer.post(value + 1)
        writer.finish()
        assert writer.dropped == writer.pending.maxsize + 1
    finally:
        release.set()
        writer.thread.join(2)
    assert written == [0]
