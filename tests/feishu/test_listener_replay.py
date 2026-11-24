import contextlib
import ctypes
import fcntl
import io
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

from qianshu.feishu import handoff
from qianshu.feishu.sidecar import read_status

CANDIDATE = Path(handoff.__file__).resolve().parents[2]
HELPERS = Path(__file__).resolve().parent
FAULTS = ("members", "stop_event", "dispatcher", "import", "constructor", "read_disk",
          "close_raise", "close_hang", "log", "native_gil", "pool", "startup",
          "crash", "memory", "killed", "consumer", "lease")


def until(check, proc=None, seconds=45):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        if proc is not None and proc.poll() is not None:
            pytest.fail(f"监听提前退出 {proc.returncode}")
        time.sleep(0.025)
    pytest.fail("合成回放超时")


def observations(case):
    file = case / "observed"
    if not file.exists():
        return []
    # 写端的一行使用单次 O_APPEND 写。忽略并发读取时的最后半行。
    return [json.loads(line) for line in file.read_bytes().splitlines(keepends=True)
            if line.endswith(b"\n")]


def prepare(case, fault="none", switches=None, *, invalid=False, other=False):
    case.mkdir(parents=True)
    (case / "fault").write_text(fault)
    db = case.parent / ("other" if other else "private") / "handoff.db"
    (case / "settings").write_text("FEISHU_APP_ID=app\nFEISHU_APP_SECRET=synthetic\nFEISHU_CHAT_ID=group\n"
                                   "FEISHU_BOT_OPEN_ID=ou_bot\nFEISHU_HANDOFF_OWNER_OPEN_ID="
                                   + ("" if invalid else "ou_owner")
                                   + f"\nFEISHU_MENTION_LOG={db}\nMAIL_PRIVATE_DIR={case / 'mail'}\n"
                                     "MAIL_USER=synthetic\nMAIL_HOST=synthetic\nMAIL_PASSWORD=synthetic\n")
    if switches is not None:
        receive, card = switches
        (case / "switches").write_text(f"MAIL_TRIAL_RECEIVE_ENABLED={receive}\n"
                                       f"MAIL_TRIAL_CARD_ENABLED={card}\n"
                                       "MAIL_TRIAL_CREATE_ENABLED=true\n")
    (case / "sitecustomize.py").write_text(
        "import os\ntry:\n    from listener_replay import install\n    install()\n"
        "except BaseException:\n    import traceback\n    traceback.print_exc()\n    os._exit(124)\n")
    if fault == "read_disk":
        os.mkfifo(case / "disk")
    return case


def drain_orphans(pgid):
    while True:
        try:
            pid, _ = os.waitpid(-pgid, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def empty(pgid):
    drain_orphans(pgid)
    result = subprocess.run(["pgrep", "-g", str(pgid)], stdout=subprocess.DEVNULL)
    assert result.returncode in (0, 1)
    return result.returncode == 1


@contextlib.contextmanager
def cli(case, source=CANDIDATE):
    # 父亡测试的孙进程由本测试回收（容器 PID 1 不一定回收孤儿）。
    libc = ctypes.CDLL(None)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    argv = [sys.executable, "-m", "qianshu", "--handoff-only", "--env-file", "settings",
            "--business-env-file", "settings"]
    if (case / "switches").exists():
        argv.extend(["--trial-switches", "switches"])
    env = {"PATH": os.defpath, "REPLAY_SOURCE": str(source), "PYTHONDONTWRITEBYTECODE": "1",
           "TMPDIR": str(case), "PYTHONPATH": os.pathsep.join(map(str, (case, source, HELPERS)))}
    try:
        with (case / "parent.log").open("ab") as log:
            proc = subprocess.Popen(["/usr/bin/setpriv", "--pdeathsig", "KILL", "/bin/sh", "-c",
                                     '[ "$PPID" = "$1" ] || exit 125; shift; exec "$@"',
                                     "cli", str(os.getpid()), *argv],
                                    cwd=case, env=env, process_group=0, stdout=log, stderr=log)
            try:
                yield proc
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(3)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(3)
                if not empty(proc.pid):
                    os.killpg(proc.pid, signal.SIGKILL)
                until(lambda: empty(proc.pid), seconds=5)
    finally:
        assert libc.prctl(36, previous.value, 0, 0, 0) == 0


def snapshot(case, proc, end=1010, takeover=False, dials=1):
    db = case.parent / "private" / "handoff.db"

    def complete():
        tags = observations(case)
        acks = [(value, at) for tag, value, at in tags if tag == "ack" and value[0] in ("group", "repeat")]
        if len(acks) < 2 or not db.exists():
            return
        with contextlib.closing(sqlite3.connect(db)) as conn:
            water = conn.execute("SELECT started, through FROM recovery").fetchone()
            rows = conn.execute("SELECT message_id, count(*) FROM mentions GROUP BY message_id ORDER BY 1").fetchall()
        reactions = [t for tag, mid, t in tags if tag == "react" and mid == "live"]
        if water != (1000, end) or (not reactions and not takeover):
            return
        first, = [at for tag, _, at in tags if tag == "first"]
        assert all(0 <= at - first < 3 for _, at in acks)
        assert all(0 <= at - first < 3 for at in reactions)
        assert len(reactions) == (0 if takeover else 1)
        assert [item for item, _ in acks] == [["group", {"code": 200}], ["repeat", {"code": 200}]]
        assert rows == [("backfill", 1), ("live", 1)]
        for tag in ("open", "lock", "dial"):
            assert sum(key == tag for key, _, _ in tags) == (dials if tag == "dial" else 1)
        assert not any(key == "external" for key, _, _ in tags)
        return water, rows, len(reactions), [value for value, _ in acks]

    return until(complete, proc)


@pytest.fixture(scope="module")
def versions(tmp_path_factory):
    roots = {}
    for ref in ("917dc13", "v0.4.0"):
        folder = tmp_path_factory.mktemp("version")
        data = subprocess.check_output(["git", "archive", ref, "src"])
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            archive.extractall(folder, filter="data")
        roots[ref] = folder / "src"
    return roots


def require_endpoint(kind):
    a, b = socket.socketpair(type=kind)
    with a, b:
        try:
            a.send(b"H")
            assert b.recv(1) == b"H"
            a.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16384)
            if kind == socket.SOCK_SEQPACKET:
                a.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        except PermissionError:
            pytest.skip(f"沙箱内未跑：{kind.name} 的 I/O 被拒绝")


@pytest.fixture(scope="module")
def stream_io():
    require_endpoint(socket.SOCK_STREAM)


@pytest.fixture(scope="module")
def packet_io(stream_io):
    require_endpoint(socket.SOCK_SEQPACKET)


@pytest.fixture(scope="module")
def legacy_snapshot(versions, tmp_path_factory):
    case = prepare(tmp_path_factory.mktemp("legacy") / "initial")
    with cli(case, versions["v0.4.0"]) as proc:
        return snapshot(case, proc)


@pytest.mark.parametrize("fault", FAULTS)
def test_c1_cli_fault_and_takeover(packet_io, tmp_path, fault, legacy_snapshot):
    case = prepare(tmp_path / "first", fault)
    with contextlib.ExitStack() as stack:
        if fault == "lease":
            directory = case / "approval"
            directory.mkdir(mode=0o700)
            held = stack.enter_context((directory / "consumer.lock").open("a"))
            fcntl.flock(held, fcntl.LOCK_EX)
        proc = stack.enter_context(cli(case))
        assert snapshot(case, proc) == legacy_snapshot
        if fault != "lease":
            facts = json.loads((case / "bootstrap").read_text())
            assert 32 << 20 <= facts.pop("cap") <= 128 << 20
            assert facts == dict(death=signal.SIGKILL, sockets=1, inherited=False, sdk=False)
        if fault in {"memory", "killed", "lease", "crash", "consumer"}:
            state = until(lambda: (s if (s := read_status(case / "approval"))["state"] in ("disabled", "stale")
                                   else None), proc)
            if fault == "consumer":
                assert state["unconfirmed"] >= 1
            else:
                assert (case / "approval" / "supervisor.log").read_text().startswith("CRITICAL E-TRIAL-DISABLED ")
        if fault == "crash":
            hits = [float(row.split()[1]) for row in (case / "generations").read_text().splitlines()]
            assert hits[1] - hits[0] >= 1 and hits[2] - hits[1] >= 2
        if fault in {"startup", "native_gil", "read_disk", "close_hang"}:
            pid, started = (case / "hit").read_text().splitlines()[-1].split()
            assert Path(f"/proc/{pid}").exists()
            if fault == "read_disk":
                assert time.monotonic() - float(started) > 3.25
        # TERM 只发父进程，先检整组，不让 finally 的兜底清场冒充成功。
        began = time.monotonic()
        proc.send_signal(signal.SIGTERM if fault in {"startup", "native_gil"} else signal.SIGINT)
        proc.wait(7)
        until(lambda: empty(proc.pid), seconds=2)
        assert time.monotonic() - began < 10
    second = prepare(tmp_path / "second")
    (second / "takeover").touch()
    with cli(second) as successor:
        snapshot(second, successor, end=1020, takeover=True)


@pytest.mark.parametrize(("fault", "peak"), [("c2", 15), ("c2_partial", 14)])
def test_c2_cli_fragment_flood(packet_io, tmp_path, legacy_snapshot, fault, peak):
    case = prepare(tmp_path / "first", fault)
    with cli(case) as proc:
        assert snapshot(case, proc) == legacy_snapshot
        metrics = [v for t, v, _ in observations(case) if t == "budget"]
        rejected, groups, size, drops, refused = metrics[-1]
        assert rejected >= 4 and groups == peak and size < 8192
        assert drops >= 120 and refused > 0


def test_other_cli_with_another_database_never_dials(tmp_path):
    with cli(case := prepare(tmp_path / "one")) as first:
        snapshot(case, first)
        with cli(other := prepare(tmp_path / "two", other=True)) as second:
            assert second.wait(45) == 1
            assert [row[0] for row in observations(other)] == ["installed"]
            assert not (tmp_path / "other" / "handoff.db").exists()


def test_invalid_plain_config_matches_v040(tmp_path, versions):
    for i, source in enumerate((versions["v0.4.0"], CANDIDATE)):
        case = prepare(tmp_path / str(i), invalid=True)
        with cli(case, source) as proc:
            assert proc.wait(45) == 1
            assert [row[0] for row in observations(case)] == ["installed"]


@pytest.mark.parametrize("switches", [None, ("false", "false"), ("false", "true"),
                                      ("true", "false"), ("true", "true")])
def test_production_switches_match_main(tmp_path, versions, switches):
    def replay(root, source):
        case = prepare(root / "first", switches=switches)
        with cli(case, source) as proc:
            if switches == ("false", "true"):
                assert proc.wait(45) == 1
                assert [row[0] for row in observations(case)] == ["installed"]
                return 1
            result = snapshot(case, proc)
            replies = [value for tag, value, _ in observations(case) if tag == "ordinary"]
            assert [v["code"] for _, v in replies] == [200, 500]
            result += (replies,)
            mail = switches and switches[0] == "true"
            if mail:
                until(lambda: any(t == "mail" for t, _, _ in observations(case)), proc)
            assert not (case / "approval").exists()
            if source == CANDIDATE:
                until(lambda: (root / "machine" / "status.json").exists(), proc)
                state = read_status(root / "machine")
                assert state["state"] == "disabled" and not state["alive"]
            assert (root / "private" / "trial-050.sqlite3").exists() == bool(mail)
            assert not any(t == "mail" for t, _, _ in observations(case)) or mail
        if mail:
            off = prepare(root / "closed", switches=("false", "false"))
            (off / "takeover").touch()
            with cli(off, source) as proc:
                result += snapshot(off, proc, end=1020, takeover=True)
                assert not any(t == "mail" for t, _, _ in observations(off))
                assert not (off / "approval").exists()
        return result
    assert replay(tmp_path / "main", versions["917dc13"]) == replay(tmp_path / "candidate", CANDIDATE)

def test_c2_bounds_reconnect_and_transport_pressure(stream_io, tmp_path, legacy_snapshot):
    case = prepare(tmp_path / "first", "wire")
    with cli(case) as proc:
        assert snapshot(case, proc, dials=2) == legacy_snapshot
        rows = observations(case)
        before = [v for t, v, _ in rows if t == "wire" and v[0] == 1]
        assert before[-1][1] == 0 and before[-1][2] >= 9
        assert 2 << 20 < before[-1][3] < (2 << 20) + 4096
        after = [v for t, v, _ in rows if t == "wire" and v[0] == 2]
        assert after[-1][1] == 3 and after[-1][4] == 1
        assert not any(t == "ack" and v[0] in ("cross", "pressure") for t, v, _ in rows)


def test_c2_unwritable_connection_has_no_observed_ack(stream_io, tmp_path):
    case = prepare(tmp_path / "first", "unwritable")
    with cli(case) as proc:
        until(lambda: (case / "still-blocked").exists(), proc)
        rows = observations(case)
        assert [v for t, v, _ in rows if t == "expired"] == [1, 2]
        assert not any(t == "ack" for t, _, _ in rows)
        assert sum(t == "react" and v == "live" for t, v, _ in rows) == 1
