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


def confirm_snapshot(store, trial, box):
    request = store.request_confirmation(trial.scope, box["generation"], box["validity"], box["cursor"])
    trial.poll()
    result = store.row("SELECT status FROM recovery_request WHERE id=?", request)[0]
    if result == "hold":
        raise Hold()
    assert result == "restored"


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
            confirm_snapshot(store, trial, before)
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
        confirm_snapshot(store, trial, trial.box())
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
        confirm_snapshot(store, trial, box)

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
                confirm_snapshot(store, trial, old)
            assert trial.box()["state"] == "frozen"
            trial.poll()  # 常规冻结轮询准备新代快照
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


def test_cli_queues_confirmation(tmp_path, monkeypatch, capsys):
    import json
    import os
    from datetime import datetime
    from qianshu import __main__ as entry

    path = tmp_path / "trial-050.sqlite3"
    handoff = tmp_path / "handoff.conf"
    handoff.write_text("FEISHU_APP_ID=synthetic\nFEISHU_APP_SECRET=synthetic\nFEISHU_CHAT_ID=synthetic\n")
    store, inbox = Store(path), Inbox()
    trial = revision_trial(store, inbox, [])
    trial.poll()
    store.freeze(trial.scope, "mapping")
    inbox.add(synthetic(2, "snapshot"))
    trial.poll()
    del inbox.messages[2]
    reads = []

    def forbidden(*args, **kwargs):
        reads.append(args)
        pytest.fail("守望者命令读取业务配置或打开邮箱")

    for name in ("load_config", "dotenv_values", "load_model_config", "load_handoff_settings"):
        monkeypatch.setattr(entry, name, forbidden)
    monkeypatch.setattr("qianshu.mail.imap.trial_connect", forbidden)
    args = ["--state", str(path), "--env-file", str(handoff)]

    def confirm():
        box = trial.box()
        assert entry.main([*args, "--trial-reconcile", trial.scope, str(box["generation"]),
                           str(box["validity"]), str(box["cursor"])]) == 0
        capsys.readouterr()

    def results():
        assert entry.main([*args, "--list-recovery"]) == 0
        return json.loads(capsys.readouterr().out)

    with closing(store.db):
        confirm()
        # 独立守望者进程：配置键读探针覆盖环境、配置入口及 Git 路径检查。
        import subprocess
        import sys
        probe = """
import os, sys
from qianshu import __main__ as entry
original = type(os.environ).__getitem__
def lookup(env, key):
    if key.startswith(('MAIL_', 'MINGDAO_', 'ANTHROPIC_', 'AI_')):
        raise AssertionError('读取业务配置键')
    return original(env, key)
def forbidden(*args, **kwargs):
    raise AssertionError('进入配置读取入口')
type(os.environ).__getitem__ = lookup
entry.load_config = entry.load_model_config = entry.dotenv_values = forbidden
raise SystemExit(entry.main(sys.argv[1:]))
"""
        box = trial.box()
        cli = [sys.executable, "-c", probe, *args, "--trial-reconcile", trial.scope,
               str(box["generation"]), str(box["validity"]), str(box["cursor"])]
        subprocess.run(cli, check=True, capture_output=True)
        first, = results()
        assert first["status"] == "pending" and first["actor"] == os.getuid()
        assert datetime.fromisoformat(first["requested"]).utcoffset().total_seconds() == 0
        confirm()
        assert results() == [first] and gaps(store) == []
        trial.poll()
        assert results()[0]["reason"] == "snapshot"
        assert trial.box()["state"] == "frozen" and gaps(store) == [(2, 2)]
        assert notices(store) == [(1, "sent")]
        trial.poll()
        confirm()
        trial.poll()
        assert [row["status"] for row in results()] == ["hold", "restored"]
        assert trial.box()["state"] == "active" and reads == []


@pytest.mark.parametrize("case,reason,message", [
    ("restored", None, "收信已恢复，缺口 1 个已登记待人工核对"),
    ("read", "mail_read", "收信确认未完成：邮箱读取失败，请配置负责人核对"),
    ("identity", "snapshot", "收信确认未完成：快照身份不符，请配置负责人核对"),
    ("folder", "folder", "收信确认未完成：文件夹不符，请配置负责人核对"),
])
def test_service_confirmation_metadata_only(tmp_path, monkeypatch, case, reason, message):
    sent = []
    store, inbox = Store(tmp_path / "trial"), Inbox()
    trial = revision_trial(store, inbox, sent)
    with closing(store.db):
        trial.poll()
        store.freeze(trial.scope, "mapping")
        inbox.next_uid += 1
        trial.poll()
        box = trial.box()
        keys = (trial.scope, box["generation"], box["validity"], box["cursor"])
        request = store.request_confirmation(*keys)
        if case == "read":
            monkeypatch.setattr(inbox, "response", lambda key: ("NO", []))
        elif case == "identity":
            inbox.generation += 1
        elif case == "folder":
            with store.write() as db:
                db.execute("UPDATE trial_folder SET folder='different'")
        inbox.commands.clear()
        sent.clear()
        flags = set(inbox.seen)
        trial.poll()
        row = store.row("SELECT * FROM recovery_request WHERE id=?", request)
        assert row["status"] == ("hold" if reason else "restored") and row["completed"]
        assert row["reason"] == reason and row["gaps"] == (None if reason else 1)
        assert trial.box()["state"] == ("frozen" if reason else "active")
        assert all(cmd[0] in {"EXAMINE", "SEARCH"} for cmd in inbox.commands)
        assert ("EXAMINE" in [cmd[0] for cmd in inbox.commands]) == (case != "folder")
        assert ("SEARCH" in [cmd[0] for cmd in inbox.commands]) == (case == "restored")
        assert inbox.seen == flags
        assert store.request_confirmation(*keys) == request
        trial.flush()
        assert sent == [(False, message)]
        if case == "read":
            monkeypatch.undo()
            retry = store.request_confirmation(*keys, retry_of=request)
            assert retry != request and store.request_confirmation(*keys, retry_of=request) == retry
            trial.poll()
            assert store.row("SELECT status FROM recovery_request WHERE id=?", retry)[0] == "restored"
