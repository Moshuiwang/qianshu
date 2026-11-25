from contextlib import closing

import pytest

from qianshu.mail.trial_state import Hold, Store
from test_trial_journey import revision_trial
from trial_segments import Inbox, synthetic


def mailbox(tmp_path):
    store = Store(tmp_path / "trial")
    inbox = Inbox()
    inbox.next_uid = 100
    trial = revision_trial(store, inbox, [])
    trial.poll()
    return store, inbox, trial


def gaps(store):
    return [tuple(row) for row in store.db.execute(
        "SELECT first,last FROM unknown_range ORDER BY first")]


def notices(store):
    return [tuple(row) for row in store.db.execute(
        "SELECT quantity,status FROM notice WHERE kind='gap' ORDER BY rowid")]


@pytest.mark.parametrize("reason", ["unclean_close", "mapping", "legacy"])
def test_observed_disappears_before_recovery(tmp_path, reason):
    store, inbox, trial = mailbox(tmp_path)
    with closing(store.db):
        inbox.next_uid = 102
        store.observe(trial.scope, 1, 100, 102, [101])
        if reason == "legacy":
            with store.write() as db:
                db.execute("INSERT INTO legacy VALUES('old','pending')")
            trial.poll()
        else:
            store.freeze(trial.scope, reason)
        trial.poll()
        assert gaps(store) == [(100, 100), (101, 101)]
        assert notices(store) == [(1, "sent"), (1, "sent")]


def test_snapshot_disappears_even_with_stable_uidnext(tmp_path):
    store, inbox, trial = mailbox(tmp_path)
    with closing(store.db):
        store.freeze(trial.scope, "mapping")
        inbox.add(synthetic(100, "held"))
        trial.poll()
        before = trial.box()
        del inbox.messages[100]
        # 人工确认与上一次轮询之间没有新的 UID。
        with pytest.raises(Hold):
            store.reconcile(trial.scope, before["generation"], 1, 101)
        assert gaps(store) == [(100, 100)]
        assert trial.box()["state"] == "frozen"


def test_mapping_unseen_range_main_control(tmp_path):
    store, inbox, trial = mailbox(tmp_path)
    with closing(store.db):
        store.freeze(trial.scope, "mapping")
        inbox.next_uid = 102
        trial.poll()
        assert gaps(store) == [(100, 101)]
        assert notices(store) == [(2, "sent")]


def test_legacy_present_main_control(tmp_path):
    store, inbox, trial = mailbox(tmp_path)
    with closing(store.db):
        inbox.add(synthetic(100, "pending"))
        store.observe(trial.scope, 1, 100, 101, [100])
        with store.write() as db:
            db.execute("INSERT INTO legacy VALUES('old','pending')")
        trial.poll()
        trial.poll()
        assert trial.box()["reason"] == "legacy"
        assert trial.box()["cursor"] == 100
        store.reconcile(trial.scope, 1, 1, 100)
        trial.poll()
        assert set(inbox.reads) == {100}
        assert tuple(trial.box()[key] for key in ("state", "cursor")) == ("active", 101)
        assert store.row("SELECT status FROM legacy")[0] == "reviewed"
        assert store.row("SELECT status FROM registration")[0] == "triage"
        assert gaps(store) == notices(store) == []


REASONS = ("legacy", "mapping", "unclean_close", "reconciling", "missing", "seen",
           "folder", "generation", "initialization", "originals", "interrupted_search")
EVENTS = ("unchanged", "observed_loss", "unseen_loss", "arrival", "stable_next_loss", "rollover")


@pytest.mark.parametrize("path", ["poll_then_confirm", "confirm_after_change"])
@pytest.mark.parametrize("event", EVENTS)
@pytest.mark.parametrize("reason", REASONS)
def test_recovery_matrix(tmp_path, reason, event, path):
    store, inbox, trial = mailbox(tmp_path)
    scope = trial.scope
    dispatched = []

    def triage(row, generation, item):
        dispatched.append(row["uid"])
        trial.status(row["id"], "no")

    def confirm():
        box = trial.box()
        store.reconcile(scope, box["generation"], box["validity"], box["cursor"])

    with closing(store.db):
        trial.triage = triage
        store.import_spend("0")
        if event in {"observed_loss", "stable_next_loss", "rollover"}:
            inbox.add(synthetic(100, "held"))
        if event in {"observed_loss", "rollover"}:
            store.observe(scope, 1, 100, 101, [100])
        if reason == "legacy":
            with store.write() as db:
                db.execute("INSERT INTO legacy VALUES('old','pending')")
        else:
            store.freeze(scope, "mapping" if reason == "reconciling" else reason)
        if reason == "reconciling":
            trial.poll()
        trial.poll()
        old = trial.box()
        if event in {"observed_loss", "stable_next_loss"}:
            del inbox.messages[100]
        elif event == "unseen_loss":
            inbox.next_uid += 2
        elif event == "arrival":
            inbox.add(synthetic(100, "arriving"))
        elif event == "rollover":
            inbox.messages.clear()
            inbox.generation += 1
            inbox.next_uid = 3
        if path == "poll_then_confirm":
            trial.poll()
        elif event != "unchanged" and (reason != "legacy" or event == "rollover"):
            with pytest.raises(Hold):
                store.reconcile(scope, old["generation"], old["validity"], old["cursor"])
            assert trial.box()["state"] == "frozen"
        confirm()
        # 换代沿用原流程。未确认的旧台账仍须按新代次单独结清。
        trial.poll()
        if reason == "legacy" and event == "rollover":
            assert trial.box()["reason"] == "legacy"
            confirm()
        trial.poll()
        ranges = {"unseen_loss": [(100, 101)], "observed_loss": [(100, 100)],
                  "stable_next_loss": [(100, 100)]}.get(event, [])
        assert gaps(store) == ranges
        assert notices(store) == [(b - a + 1, "sent") for a, b in ranges]
        assert tuple(trial.box()[k] for k in ("state", "cursor", "validity", "initial")) == (
            "active", inbox.next_uid, inbox.generation, 100)
        assert [row[0] for row in store.db.execute("SELECT status FROM legacy")] == (
            ["reviewed"] if reason == "legacy" else [])
        assert dispatched == ([100] if reason == "legacy" and event == "arrival" else [])
        if reason == "legacy" and event != "rollover":
            assert 1 not in inbox.reads
        registrations = list(store.db.execute("SELECT id,status FROM registration ORDER BY id"))
        expected = (int(event != "rollover" and reason != "legacy") + int(event == "arrival"))
        assert len(registrations) == expected
        trial.poll()
        assert list(store.db.execute("SELECT id,status FROM registration ORDER BY id")) == registrations
        assert notices(store) == [(b - a + 1, "sent") for a, b in ranges]
        assert dispatched == ([100] if reason == "legacy" and event == "arrival" else [])


def test_replaced_snapshot_and_increased_uidnext(tmp_path):
    store, inbox, trial = mailbox(tmp_path)
    with closing(store.db):
        store.freeze(trial.scope, "mapping")
        inbox.add(synthetic(100, "snapshot"))
        trial.poll()
        del inbox.messages[100]
        inbox.next_uid = 103
        trial.poll()
        trial.poll()
        assert gaps(store) == [(100, 102)]
        assert notices(store) == [(3, "sent")]


def test_cli_reopens_mailbox_for_confirmation(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from qianshu import __main__ as entry

    path = tmp_path / "trial-050.sqlite3"
    store, inbox = Store(path), Inbox()
    trial = revision_trial(store, inbox, [])
    trial.poll()
    store.freeze(trial.scope, "mapping")
    inbox.add(synthetic(2, "snapshot"))
    trial.poll()
    del inbox.messages[2]
    monkeypatch.setattr(entry, "load_config", lambda *args, **kw: trial.config)
    monkeypatch.setattr("qianshu.mail.imap.trial_connect", lambda _: nullcontext(inbox))

    def confirm():
        box = trial.box()
        return entry.main(["--state", str(path), "--trial-reconcile", trial.scope,
                           str(box["generation"]), str(box["validity"]), str(box["cursor"])])

    with closing(store.db):
        assert confirm() == 1
        assert trial.box()["state"] == "frozen" and gaps(store) == [(2, 2)]
        assert notices(store) == [(1, "pending")]
        assert confirm() == 0
        trial.poll()
        assert notices(store) == [(1, "sent")]
        assert trial.box()["state"] == "active"
