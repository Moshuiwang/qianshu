import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from qianshu.__main__ import main
from qianshu.feishu import State
from qianshu.mail.trial_state import Hold, Store
from test_freeze_recovery import confirm_snapshot, gaps, notices
from test_trial_journey import revision_trial
from trial_segments import Inbox, synthetic


@pytest.mark.parametrize("origin", ["initialization", "rollover"])
def test_first_snapshot_loss(tmp_path, origin):
    store, inbox = Store(tmp_path / "trial"), Inbox()
    trial = revision_trial(store, inbox, [])
    with closing(store.db):
        if origin == "initialization":
            store.begin(trial.scope)
        else:
            trial.poll()
            inbox.generation += 1
        trial.poll()
        del inbox.messages[1]
        with pytest.raises(Hold):
            confirm_snapshot(store, trial, trial.box())
        trial.poll()
        confirm_snapshot(store, trial, trial.box())
        assert trial.box()["state"] == "active"
        assert gaps(store) == [(1, 1)]
        assert notices(store) == [(1, "sent")]
        assert store.row("SELECT gaps FROM recovery_request WHERE status='restored'")[0] == 1


def test_sparse_snapshots_exclude_unobserved_history_and_old_validity(tmp_path):
    store, inbox = Store(tmp_path / "trial"), Inbox()
    trial = revision_trial(store, inbox, [])
    with closing(store.db):
        trial.poll()
        store.freeze(trial.scope, "mapping")
        trial.poll()  # 旧代快照含 UID 1。
        inbox.generation += 1
        inbox.messages = {uid: synthetic(uid, "held") for uid in (3, 5, 7)}
        inbox.next_uid = 9
        trial.poll()
        del inbox.messages[3]
        trial.poll()
        del inbox.messages[7]
        trial.poll()
        confirm_snapshot(store, trial, trial.box())
        assert gaps(store) == [(3, 3), (7, 7)]
        assert notices(store) == [(1, "sent"), (1, "sent")]
        assert store.row("SELECT gaps FROM recovery_request")[0] == 2


@pytest.mark.parametrize("phase", ["confirm", "capture"])
def test_legacy_incomplete_search_preserves_pending(tmp_path, monkeypatch, phase):
    store, inbox = Store(tmp_path / "trial"), Inbox()
    trial = revision_trial(store, inbox, [])
    with closing(store.db):
        dispatched = []

        def record_dispatch(row, generation, item):
            dispatched.append(row["uid"])
            trial.status(row["id"], "no")

        trial.triage = record_dispatch
        store.import_spend("0")
        uid_one = inbox.messages.pop(1)
        inbox.next_uid = 1
        trial.poll()
        inbox.add(uid_one)
        store.observe(trial.scope, 1, 1, 2, [1])
        with store.write() as db:
            db.execute("INSERT INTO legacy VALUES('old','pending')")
        trial.poll()
        with monkeypatch.context() as patch:
            patch.setattr(inbox, "interval", lambda first, last: ())
            if phase == "capture":
                with pytest.raises(Hold):
                    trial.poll()
            else:
                with pytest.raises(Hold):
                    confirm_snapshot(store, trial, trial.box())
                assert store.row("SELECT reason FROM recovery_request")[0] == "snapshot"
        assert trial.box()["cursor"] == 1 and store.pending(trial.scope, 1) == [1]
        assert gaps(store) == notices(store) == []
        previous = store.row("SELECT id FROM recovery_request")
        store.request_confirmation(trial.scope, 1, 1, 1, retry_of=previous[0] if previous else 0)
        trial.poll()
        trial.poll()
        assert trial.box()["cursor"] == 2
        assert store.row("SELECT count(*) FROM registration")[0] == 1
        trial.poll()
        assert dispatched == [1]


@pytest.mark.parametrize("version", [0, 1])
def test_entry_recovery_with_trial_filename(tmp_path, capsys, version):
    path = tmp_path / "trial-050.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE feishu_messages(scope,digest,status)")
        db.execute("INSERT INTO feishu_messages VALUES('scope','digest','notice_pending')")
        db.execute(f"PRAGMA user_version={version}")
        db.commit()
    path.chmod(0o600)
    expected = State.recovery_list(path)
    assert expected[0]["stage"] == "notice_pending"
    assert main(["--list-recovery", "--state", str(path)]) == 0
    assert json.loads(capsys.readouterr().out) == expected


@pytest.mark.parametrize("command", ["--trial-reconcile", "--list-recovery"])
@pytest.mark.parametrize("controlled", [False, True])
def test_guardian_process_environment(tmp_path, command, controlled):
    path = tmp_path / "trial-050.sqlite3"
    store = Store(path)
    trial = revision_trial(store, Inbox(), [])
    with closing(store.db):
        trial.poll()
        store.freeze(trial.scope, "mapping")
        trial.poll()
        box = trial.box()
    args = ["--state", str(path), command]
    if command == "--trial-reconcile":
        args.extend([trial.scope, *(str(box[k]) for k in ("generation", "validity", "cursor"))])
    keys = ("MAIL_PASSWORD", "MINGDAO_PASSWORD", "ANTHROPIC_API_KEY")
    env = {"PATH": os.defpath, "HOME": str(tmp_path), **dict.fromkeys(keys, "synthetic-secret")}
    if controlled:
        # 在部署副本的解释器入口检查 exec 环境，再用本测试的解释器执行真实 CLI。
        script = tmp_path / "scripts/guardian-trial.sh"
        script.parent.mkdir()
        shutil.copy2(Path("scripts") / script.name, script)
        python = tmp_path / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text(f"#!{sys.executable}\nimport os,sys\n"
                          f"assert set({keys!r}).isdisjoint(os.environ)\n"
                          f"os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n")
        python.chmod(0o700)
        cli = [str(script)]
    else:
        cli = [sys.executable, "-m", "qianshu"]
    result = subprocess.run([*cli, *args], env=env, capture_output=True, text=True)
    assert "synthetic-secret" not in result.stdout + result.stderr
    if controlled:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0 and "guardian-trial.sh" in result.stderr
