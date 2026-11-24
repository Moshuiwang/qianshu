import contextlib
import fcntl
import json
import time
import os
import signal
import sqlite3
import subprocess

import pytest

from test_listener_replay import CANDIDATE, cli, empty, observations, prepare, until


@pytest.fixture(scope="module")
def main_source(tmp_path_factory):
    root = tmp_path_factory.mktemp("main-source")
    for path in subprocess.check_output(["git", "ls-tree", "-r", "--name-only", "917dc13", "src"],
                                        text=True).splitlines():
        dest = root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(subprocess.check_output(["git", "show", f"917dc13:{path}"]))
    return root / "src"


def case_at(root, name, scenario, *, fault=None, switches=None, other=False):
    case = prepare(root / name, switches=switches, other=other)
    with (case / "sitecustomize.py").open("a") as script:
        script.write(f"from production_endpoint import install as endpoint\nendpoint({scenario!r}, {fault!r})\n")
    return case


def observed(case, proc, db, count):
    def complete():
        if not (case / "stimulus").exists():
            return
        with contextlib.closing(sqlite3.connect(db)) as conn:
            water = conn.execute("SELECT started,through FROM recovery").fetchone()
            rows = conn.execute("SELECT message_id,count(*) FROM mentions GROUP BY 1 ORDER BY 1").fetchall()
        tags = observations(case)
        reactions = sorted(v for t, v, _ in tags if t == "react")
        if water == (1000, 1010) and len(rows) == count + 1 and len(reactions) == count + 1:
            return sorted(v for t, v, _ in tags if t == "response"), reactions, rows, water
    return until(complete, proc, seconds=10)


def stop(proc):
    os.killpg(proc.pid, signal.SIGINT)
    code = proc.wait(5)
    assert empty(proc.pid)
    return code


@pytest.mark.parametrize("scenario", ["single", "interleaved", "burst", "fragment", "unknown"])
def test_production_frame_replay(tmp_path, main_source, scenario):
    def replay(root, source):
        case = case_at(root, "first", scenario)
        with cli(case, source) as proc:
            count = 9 if scenario == "burst" else 1
            data = observed(case, proc, root / "private/handoff.db", count)
            assert stop(proc) == 0
            assert sum(t == "closed" for t, _, _ in observations(case)) == 1
            return data

    expected = replay(tmp_path / "main", main_source)
    assert len(expected[0]) == {"burst": 9, "interleaved": 3}.get(scenario, 2)
    assert replay(tmp_path / "candidate", CANDIDATE) == expected


@pytest.mark.parametrize("fault", ["raise", "hang"])
def test_mail_close_fault_releases_listener(tmp_path, fault):
    db = tmp_path / "private/handoff.db"
    case = case_at(tmp_path, "first", "single", fault=fault, switches=("true", "false"))
    with cli(case) as proc:
        observed(case, proc, db, 1)
        until(lambda: any(t == "mail" for t, _, _ in observations(case)), proc)
        os.killpg(proc.pid, signal.SIGINT)
        until(lambda: any(t == "mail-close" for t, _, _ in observations(case)))
        assert proc.wait(5) in (-signal.SIGINT, 130)
        assert empty(proc.pid)
    assert db.with_name("trial-050.dirty").exists()
    assert "CRITICAL" in (case / "parent.log").read_text()
    with db.open("rb") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    second = case_at(tmp_path, "second", "single", other=True)
    with cli(second) as proc:
        observed(second, proc, tmp_path / "other/handoff.db", 1)
        assert stop(proc) == 0

    # 原库重启在第一次访问邮箱前已经冻结登记；对账后的恢复沿用收信流程。
    restart = case_at(tmp_path, "restart", "single", switches=("true", "false"))
    with cli(restart) as proc:
        until(lambda: any(t == "mail" for t, _, _ in observations(restart)), proc)
        with contextlib.closing(sqlite3.connect(db.with_name("trial-050.sqlite3"))) as conn:
            state, reason = conn.execute("SELECT state,reason FROM mailbox").fetchone()
            assert state == "frozen" and reason in {"unclean_close", "reconciling"}
        assert stop(proc) in (-signal.SIGINT, 130)
    assert not db.with_name("trial-050.dirty").exists()


def test_normal_mail_shutdown_matches_main(tmp_path, main_source):
    def replay(root, source):
        case = case_at(root, "first", "single", switches=("true", "false"))
        with cli(case, source) as proc:
            result = observed(case, proc, root / "private/handoff.db", 1)
            until(lambda: any(t == "mail" for t, _, _ in observations(case)), proc)
            exit_code = stop(proc)
            assert sum(t == "closed" for t, _, _ in observations(case)) == 1
        assert not (root / "private/trial-050.dirty").exists()
        return result, exit_code

    assert replay(tmp_path / "baseline", main_source) == replay(tmp_path / "candidate", CANDIDATE)


@pytest.mark.parametrize("sample_age,heartbeat_age", [(10, 0), (0, 10), (-10, 0)])
def test_expired_status_has_unknown_liveness(tmp_path, sample_age, heartbeat_age):
    from qianshu.feishu.sidecar import read_status

    state = dict(state="ready", alive=True, sampled=time.monotonic() - sample_age,
                 snapshot_age=heartbeat_age)
    (tmp_path / "status.json").write_text(json.dumps(state))
    result = read_status(tmp_path)
    assert result["state"] == "stale"
    assert result["alive"] is None
