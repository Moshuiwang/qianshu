import asyncio
import json
import logging
import os
import shutil
import signal
import sqlite3
import sys
import threading
import time
from contextlib import ExitStack, closing, contextmanager
from types import SimpleNamespace

import pytest

from qianshu.feishu import card_reply, sdk
from qianshu.feishu.sidecar import read_status
from qianshu.feishu.trial_approval import journal_at
from qianshu.feishu.trial_connection import TrialConnection
from qianshu.mail.trial_diagnostics import listener_logs

from test_listener_replay import CANDIDATE, cli as replay_cli, empty, observations, prepare, snapshot, until


@pytest.fixture(scope="module", autouse=True)
def installed_source(tmp_path_factory):
    # 使用正常安装布局，合成收信私有目录可放在本树的临时区。
    if not (CANDIDATE.parent / ".git").exists():
        yield
        return
    source = tmp_path_factory.mktemp("approval-package") / "src"
    shutil.copytree(CANDIDATE / "qianshu", source / "qianshu",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sys.modules[__name__], "CANDIDATE", source)
        yield


def cli(case, source=None):
    return replay_cli(case, source or CANDIDATE)


def scenario(root, name, fault, *, enabled=True, create=True):
    case = prepare(root / name, switches=("true", "true") if enabled else None)
    if enabled:
        (case / "switches").write_text("MAIL_TRIAL_RECEIVE_ENABLED=true\nMAIL_TRIAL_CARD_ENABLED=true\n"
             f"MAIL_TRIAL_CREATE_ENABLED={str(create).lower()}\nMAIL_TRIAL_CREATE_COUNTRY=区域甲\n"
             "MAIL_TRIAL_APPROVER_OPEN_IDS=ou_actor\n")
        from qianshu.__main__ import main
        (case / "switches").chmod(0o600)
        assert main(["--trial-publish-switches", str(case / "switches"),
                     "--state", str(root / "private/trial-050.sqlite3")]) == 0
    settings = case / "settings"
    content = settings.read_text()
    for key, value in (("FEISHU_APP_SECRET", "outlet-sentinel-7"), ("MAIL_PASSWORD", "offline-mail"), ("MAIL_USER", "inbox"), ("MAIL_HOST", "host")):
        content = content.replace(f"{key}=synthetic", f"{key}={value}")
    settings.write_text(content)
    with settings.open("a") as out:
        out.write("FEISHU_TENANT_KEY=tenant\nANTHROPIC_API_KEY=offline-key\n")
    with (case / "sitecustomize.py").open("a") as out:
        out.write("from approval_replay import install\ninstall()\n")
    (case / "scenario").write_text(fault)
    (case / "candidate").write_text(str(CANDIDATE))
    return case


def send(case, n, value):
    pending = case / f"stage-{n}"
    pending.write_text(json.dumps(value))
    pending.rename(case / f"input-{n}")


def events(case):
    path = case / "child-events"
    return path.read_text().splitlines() if path.exists() else []


def ready(case, proc):
    def sample():
        if read_status(case.parent / "machine")["state"] != "ready" or not (case / "mail-ready").exists():
            return None
        owner, registration = json.loads((case / "mail-ready").read_text())
        pid, group, _ = json.loads((case / "child-facts").read_text())
        return registration if (owner, group) == (pid, proc.pid) else None
    return until(sample, proc, seconds=90)


@pytest.fixture(scope="module")
def baseline_handoffs(baseline, tmp_path_factory, packet_io):
    root = tmp_path_factory.mktemp("plain")
    initial = scenario(root, "first", "none", enabled=False)
    with cli(initial, baseline) as proc:
        send(initial, 0, "group")
        first = snapshot(initial, proc)
    successor = scenario(root, "after", "none", enabled=False)
    (successor / "takeover").touch()
    with cli(successor, baseline) as proc:
        send(successor, 0, "group")
        return first, snapshot(successor, proc, end=1020, takeover=True)


@pytest.fixture
def compared_cli(baseline_handoffs):
    @contextmanager
    def compare(case, *, dials=1):
        with cli(case) as proc:
            yield proc
            send(case, len(list(case.glob("input-*"))), "group")
            assert snapshot(case, proc, dials=dials) == baseline_handoffs[0]
            from qianshu.feishu.state import State
            rows = State.recovery_list(case.parent / "private/trial-050.sqlite3")
            assert "outlet-sentinel-7" not in json.dumps(rows)
            for path in (case / "parent.log", *list((case.parent / "machine").glob("*.log"))):
                assert b"outlet-sentinel-7" not in path.read_bytes()
        successor = scenario(case.parent, "handoff-after-" + case.name, "none", enabled=False)
        (successor / "takeover").touch()
        with cli(successor) as proc:
            send(successor, 0, "group")
            assert snapshot(successor, proc, end=1020, takeover=True) == baseline_handoffs[1]
    return compare


@pytest.mark.parametrize("fault", ["members", "stop_event", "dispatcher", "import", "constructor", "read_disk",
                                  "close_raise", "close_hang", "memory", "malformed", "recursion", "rotate",
                                  "maintain", "mail_poll", "startup", "child_log", "native_gil", "pool_exit", "crash"])
@pytest.mark.parametrize("create", [False, True])
def test_cli_approval_fault_and_takeover(tmp_path, baseline_handoffs, packet_io, fault, create):
    case = scenario(tmp_path, "first", fault, create=create)
    (case / "fault-on").touch()
    with cli(case) as proc:
        inject_click = fault in ("read_disk", "native_gil", "malformed")
        if inject_click:
            reg = ready(case, proc)
            send(case, 0, [reg, "verify"])
        until((case / "hit").exists, proc, seconds=65)
        diagnostic = case.parent / "machine/supervisor.log"
        until(lambda: diagnostic.exists() and "CRITICAL E-TRIAL-DISABLED" in diagnostic.read_text(), proc, seconds=75)
        n = int(inject_click)
        send(case, n, [1, "verify"])
        until(lambda: ("toast", [str(n), "审批暂不可用"]) in [(k, v) for k, v, _ in observations(case)], proc)
        send(case, n+1, "group")
        assert snapshot(case, proc) == baseline_handoffs[0]
        assert "external" not in events(case)
        if fault == "read_disk":
            assert float((case / "read-seconds").read_text()) > 3.25
        (case / "fault-on").unlink()
        reg = ready(case, proc)
        send(case, n+2, [reg, "verify"])
        until(lambda: ("toast", [str(n+2), "已收到点击，请以卡片更新为准"])
              in [(k, v) for k, v, _ in observations(case)], proc)
        until(lambda: "patch" in events(case), proc)
        assert sum(k == "dial" for k, _, _ in observations(case)) == 1
        child_pid, group, _ = json.loads((case / "child-facts").read_text())
        assert group == proc.pid and os.getpgid(child_pid) == group
        os.kill(child_pid, signal.SIGSTOP)
        began = time.monotonic()
        os.kill(proc.pid, signal.SIGTERM)
        proc.wait(5)
        until(lambda: empty(proc.pid), seconds=5)
        assert time.monotonic() - began < 10
    takeover = scenario(tmp_path, "second", "healthy", create=create)
    (takeover / "takeover").touch()
    with cli(takeover) as proc:
        send(takeover, 0, "group")
        assert snapshot(takeover, proc, end=1020, takeover=True) == baseline_handoffs[1]
        reg = ready(takeover, proc)
        send(takeover, 1, [reg, "verify"])
        until(lambda: ("toast", ["1", "已收到点击，请以卡片更新为准"])
              in [(k, v) for k, v, _ in observations(takeover)], proc)
        until(lambda: "patch" in events(takeover), proc)


@pytest.mark.parametrize("fault", ["healthy", "no_proof", "joined_late", "rejoined", "watermark",
                                    "clock_overlap", "lookup_failure", "current_absent", "expired_click", "outside_list",
                                    "patch_fail", "patch_timeout", "patch_lost", "patch_bytes",
                                    "patch_no_checker", "feedback_bytes", "notice_timeout"])
def test_cli_click_evidence_and_async_result(tmp_path, compared_cli, fault):
    case = scenario(tmp_path, "first", fault)
    with compared_cli(case) as proc:
        reg = ready(case, proc)
        pid, group, cap = json.loads((case / "child-facts").read_text())
        assert group == proc.pid and pid != proc.pid and 0 < cap <= 512 << 20
        send(case, 0, [reg, "verify"])
        until(lambda: any(k == "toast" and v == ["0", "已收到点击，请以卡片更新为准"]
                          for k, v, _ in observations(case)), proc)
        confirmed = fault in {"healthy", "no_proof", "joined_late", "rejoined", "watermark"}
        positive = confirmed or fault.startswith("patch_") or fault == "notice_timeout"
        expected = {fault: "patch" if confirmed else "manual", "feedback_bytes": "tampered", "patch_no_checker": "start"}
        until(lambda: expected.get(fault, "manual") in events(case), proc)
        def settled():
            with closing(sqlite3.connect(tmp_path / "private/trial-050.sqlite3")) as db:
                if fault == "feedback_bytes":
                    return db.execute("SELECT 1 FROM trial_feedback WHERE status='blocked'").fetchone()
                if positive and not confirmed:
                    status = {"patch_no_checker": "blocked", "notice_timeout": "unknown"}.get(fault, "sent")
                    return db.execute("SELECT 1 FROM trial_notification WHERE kind='result_manual' AND status=?",
                                      (status,)).fetchone()
                return True
        until(settled, proc)
        if fault in ("patch_bytes", "patch_no_checker"):
            assert "patch" not in events(case)
        if fault == "feedback_bytes":
            assert "manual" not in events(case)
        with closing(sqlite3.connect(tmp_path / "private/trial-050.sqlite3")) as db:
            assert db.execute("SELECT count(*) FROM trial_receipt").fetchone()[0] == int(positive)
            assert not db.execute("SELECT 1 FROM trial_task").fetchone()
            if confirmed:
                assert events(case).count("member-lookup") == 1
            if fault.startswith("patch_"):
                assert db.execute("SELECT count(*) FROM trial_notification WHERE kind='result_manual'").fetchone()[0] == 1
        assert "external" not in events(case)
        assert sum(k == "dial" for k, _, _ in observations(case)) == 1

    if fault == "notice_timeout":
        restarted = scenario(tmp_path, "inspect", "healthy")
        with cli(restarted) as proc:
            ready(restarted, proc)
            from qianshu.feishu.state import State
            rows = State.recovery_list(tmp_path / "private/trial-050.sqlite3")
            assert any(row["kind"] == "manual_check" and row["status"] == "unknown" for row in rows)
            assert "manual" not in events(restarted)



def test_cli_consumer_dies_before_receipt(tmp_path, compared_cli):
    case = scenario(tmp_path, "first", "kill_consumer")
    with compared_cli(case) as proc:
        reg = ready(case, proc)
        (case / "fault-on").touch()
        send(case, 0, [reg, "verify"])
        until((case / "hit").exists, proc)
        until(lambda: ("toast", ["0", "已收到点击，请以卡片更新为准"])
              in [(k, v) for k, v, _ in observations(case)], proc)
        until(lambda: set(events(case)) >= {"unsent", "uncertain", "readback"}
              and events(case).count("start") > 1, proc, seconds=120)
        state = tmp_path / "private/trial-050.sqlite3"
        from qianshu.feishu.state import State
        recovery = State.recovery_list(state)
        assert {row["status"] for row in recovery} >= {"create_unknown", "readback_pending", "unknown"}
        assert "offline-record" not in json.dumps(recovery)
        with closing(sqlite3.connect(state)) as db:
            closed = db.execute("SELECT body FROM trial_notification WHERE kind LIKE 'closed%'").fetchall()
            assert any("结果未知" in body and "未建单" not in body for body, in closed)
            assert any("已有记录待回读" in body and "未建单" not in body for body, in closed)
            assert db.execute("SELECT status FROM trial_card WHERE registration=?", (reg,)).fetchone()[0] == "closed"
            assert not db.execute("SELECT 1 FROM trial_receipt").fetchone()
        assert sum(k == "dial" for k, _, _ in observations(case)) == 1


def test_journal_refuses_symlink_before_writable_open(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"unchanged")
    target.chmod(0o600)
    link = tmp_path / "journal"
    link.symlink_to(target)
    with pytest.raises(ValueError), journal_at(link):
        pytest.fail("跟随了软链接")
    assert target.read_bytes() == b"unchanged"


def test_card_send_log_stall_keeps_group_dispatch(monkeypatch):
    from tests.feishu.test_sdk import encoded_frame
    from qianshu.feishu.settings import Settings

    entered, release = threading.Event(), threading.Event()
    class Block(logging.Handler):
        def emit(self, record):
            entered.set()
            release.wait(4)

    async def exercise():
        monkeypatch.setattr(sdk.ws, "loop", asyncio.get_running_loop())
        supervisor = SimpleNamespace(click=lambda *args: "审批暂不可用", mode="cards", card_drops=0)
        service = SimpleNamespace(settings=Settings("app", "secret", "group", "bot", frozenset({"actor"})),
                                  _sidecar=supervisor, _callback_response_margin=0)
        seen = []
        service.entrance = SimpleNamespace(inject_message=lambda *a, **k: seen.append(time.monotonic()))
        service.inject_message = service.entrance.inject_message
        client = TrialConnection(service)
        def broken(encoded):
            logging.getLogger("websockets.client").error("send_binary")
            asyncio.get_running_loop().call_exception_handler({"message": "send_binary"})
            raise OSError
        transport = SimpleNamespace(is_closing=lambda: False, get_write_buffer_size=lambda: 0,
                    get_write_buffer_limits=lambda: (32768, 65536),
                    get_extra_info=lambda _: SimpleNamespace(getsockopt=lambda *_: 65536))
        monkeypatch.setattr(card_reply.fcntl, "ioctl", lambda *a: bytes(4))
        ack = []
        async def write(data):
            ack.append(data)
        client._conn = service._origin = SimpleNamespace(close_code=None, transport=transport,
                                                        protocol=SimpleNamespace(send_binary=broken), send=write)
        card = dict(schema="2.0", header=dict(event_type="card.action.trigger", app_id="app"), event={})
        group = dict(schema="2.0", header=dict(event_type="im.message.receive_v1", app_id="app"),
                     event=dict(sender=dict(sender_type="user", sender_id=dict(open_id="actor")),
                                message=dict(message_id="live", message_type="text", create_time="1000",
                                             chat_type="group", chat_id="group", content="{}",
                                             mentions=[dict(key="@bot", id=dict(open_id="bot"))])))
        root = logging.getLogger()
        handler = Block()
        root.addHandler(handler)
        try:
            with listener_logs(logging.getLogger("qianshu"), socket_logs=True):
                started = time.monotonic()
                await asyncio.gather(client._handle_message(encoded_frame(card, sdk.ws.MessageType.CARD)),
                                     client._handle_message(encoded_frame(group)))
                assert len(seen) == len(ack) == supervisor.card_drops == 1
                assert seen[0] - started < 1
                assert entered.wait(1)
                release.set()
        finally:
            release.set()
            root.removeHandler(handler)
            client._cache._cron.cancel()
            await asyncio.gather(client._cache._cron, return_exceptions=True)
    with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
        runner.run(exercise())


@pytest.mark.parametrize("fault", ["late_verify", "late_approve", "kill_after_commit"])
def test_cli_candidate_commit_never_resigned(tmp_path, compared_cli, fault):
    case = scenario(tmp_path, "first", fault)
    with compared_cli(case) as proc:
        reg = ready(case, proc)
        n = 0
        if fault == "late_approve":
            send(case, n, [reg, "verify"])
            until(lambda: "patch" in events(case), proc)
            n += 1
        send(case, n, [reg, "approve" if n else "verify"])
        until((case / "committed").exists, proc)
        until(lambda: events(case).count("start") > 1 and "notice" in events(case), proc, seconds=75)
        with closing(sqlite3.connect(tmp_path / "private/trial-050.sqlite3")) as db:
            assert db.execute("SELECT count(*) FROM trial_click_candidate").fetchone()[0] == n+1
            assert db.execute("SELECT count(*) FROM trial_receipt").fetchone()[0] == n
            assert db.execute("SELECT count(*) FROM trial_item_task").fetchone()[0] == 0


def test_cli_group_after_card_flood(tmp_path, baseline, packet_io):
    def measure(root, source, enabled):
        case = scenario(root, "first", "healthy", enabled=enabled)
        with cli(case, source) as proc:
            if enabled:
                ready(case, proc)
            send(case, 0, "flood")
            send(case, 1, "group")
            result = snapshot(case, proc)
            tags = observations(case)
            start, = [t for k, _, t in tags if k == "first"]
            end, = [t for k, value, t in tags if k == "ack" and value[0] == "group"]
            return result, end - start

    expected, baseline_delay = measure(tmp_path / "main", baseline, False)
    actual, actual_delay = measure(tmp_path / "work", CANDIDATE, True)
    assert actual == expected
    assert actual_delay <= baseline_delay


@pytest.mark.parametrize("fault", ["first_fail", "first_timeout", "first_bytes", "first_no_checker"])
def test_cli_first_send_failure_uses_registered_key(tmp_path, compared_cli, fault):
    case = scenario(tmp_path, "first", fault)
    with compared_cli(case) as proc:
        reg = ready(case, proc)
        state = tmp_path / "private/trial-050.sqlite3"
        def result():
            with closing(sqlite3.connect(state)) as db:
                return db.execute("""SELECT i.status,n.status FROM card_intent i JOIN trial_card c
                    USING(registration,version) JOIN trial_notification n USING(card)
                    WHERE i.registration=? AND n.kind='first_manual:'||i.uuid""", (reg,)).fetchall()
        blocked = fault in ("first_bytes", "first_no_checker")
        expected = [("blocked" if blocked else "unknown", "blocked" if fault == "first_no_checker" else "sent")]
        until(lambda: result() == expected, proc)
        assert events(case).count("interactive") == int(not blocked)
        assert events(case).count("manual") == int(fault != "first_no_checker")
        assert "external" not in events(case)


@pytest.mark.parametrize("fault", ["recovery_bytes", "recovery_no_checker"])
def test_cli_closed_notice_checked_at_final_handoff(tmp_path, compared_cli, fault):
    case = scenario(tmp_path, "first", "healthy")
    with cli(case) as proc:
        reg = ready(case, proc)
        os.kill(proc.pid, signal.SIGTERM)
        proc.wait(5)
        until(lambda: empty(proc.pid), seconds=5)
    case = scenario(tmp_path, "recovery", fault)
    with compared_cli(case) as proc:
        ready(case, proc)
        with closing(sqlite3.connect(tmp_path / "private/trial-050.sqlite3")) as db:
            rows = db.execute("""SELECT kind,status FROM trial_notification JOIN trial_card USING(card)
                                WHERE registration=?""", (reg,)).fetchall()
            assert ("closed", "blocked") in rows
            assert not db.execute("SELECT 1 FROM trial_receipt").fetchone()
        assert "external" not in events(case)
        if fault == "recovery_bytes":
            assert "tampered" in events(case)
        else:
            assert "notice" not in events(case)


def test_cli_verified_approval_has_one_unexecutable_item(tmp_path, compared_cli):
    case = scenario(tmp_path, "first", "healthy")
    with compared_cli(case) as proc:
        reg = ready(case, proc)
        for index, action in enumerate(("verify", "approve", "approve")):
            send(case, index, [reg, action])
            until(lambda: any(key == "toast" and value[0] == str(index)
                              for key, value, _ in observations(case)), proc)
            if index < 2:
                until(lambda: events(case).count("patch") == index + 1, proc)
            else:
                until(lambda: "manual" in events(case), proc)
            time.sleep(.06)  # 让第三次独立点击落在下一次卡片回包额度。
        with closing(sqlite3.connect(tmp_path / "private/trial-050.sqlite3")) as db:
            assert db.execute("SELECT action FROM trial_receipt ORDER BY recorded").fetchall() == [
                ("verify",), ("approve",)]
            assert db.execute("SELECT item,status FROM trial_item_task").fetchall() == [
                (1, "executor_unavailable")]
        assert "external" not in events(case)


def test_cli_click_from_disabled_interval_denied_after_republish(tmp_path, compared_cli):
    from qianshu.__main__ import main

    case = scenario(tmp_path, "first", "healthy")
    switches = case / "switches"
    on = switches.read_text()
    publish = ["--trial-publish-switches", str(switches), "--state", str(tmp_path / "private/trial-050.sqlite3")]
    with compared_cli(case) as proc:
        old = ready(case, proc)
        switches.write_text("MAIL_TRIAL_RECEIVE_ENABLED=false\nMAIL_TRIAL_CARD_ENABLED=false\n")
        assert main(publish) == 0
        stamp = int(time.time() * 1000)
        switches.write_text(on)
        assert main(publish) == 0
        until(lambda: events(case).count("start") > 1 and
              int((case / "mail-started").read_text()) != old, proc, seconds=120)
        new = ready(case, proc)
        for index, reg in enumerate((new, new, old)):
            send(case, index, dict(reg=reg, action="verify", event_id="old", stamp=stamp))
            until(lambda: any(k == "toast" and v[0] == str(index) for k, v, _ in observations(case)), proc)
            time.sleep(.06)
        until(lambda: "manual" in events(case), proc)
        with closing(sqlite3.connect(tmp_path / "private/trial-050.sqlite3")) as db:
            assert not db.execute("SELECT 1 FROM trial_receipt").fetchone()
            assert not db.execute("SELECT 1 FROM trial_item_task").fetchone()
            assert db.execute("SELECT count(*) FROM trial_feedback").fetchone()[0] == 1
        assert sum(k == "dial" for k, _, _ in observations(case)) == 1


def test_cli_create_disabled_only_returns_notice(tmp_path, compared_cli):
    case = scenario(tmp_path, "first", "healthy", create=False)
    with compared_cli(case) as proc:
        reg = ready(case, proc)
        send(case, 0, [reg, "approve"])
        until(lambda: ("toast", ["0", "建单未开启"]) in [(k, v) for k, v, _ in observations(case)], proc)
        def consumed():
            with closing(sqlite3.connect(tmp_path / "private/trial-050.sqlite3")) as db:
                if not db.execute("SELECT 1 FROM trial_event").fetchone():
                    return False
                assert not db.execute("SELECT 1 FROM trial_receipt").fetchone()
                assert not db.execute("SELECT 1 FROM trial_feedback").fetchone()
                return True
        until(consumed, proc)
        assert set(events(case)).isdisjoint({"patch", "notice", "manual", "external"})


def test_cli_sigterm_parent_with_live_child(tmp_path, packet_io):
    case = scenario(tmp_path, "first", "healthy")
    with cli(case) as proc:
        ready(case, proc)
        pid, group, _ = json.loads((case / "child-facts").read_text())
        assert group == proc.pid and pid != proc.pid
        state = read_status(tmp_path / "machine")
        assert state["alive"] and state["backlog"] == state["unconfirmed"] == 0
        assert "external" not in events(case)
        start = time.monotonic()
        os.kill(proc.pid, signal.SIGTERM)
        proc.wait(5)
        until(lambda: empty(group), seconds=5)
        assert time.monotonic() - start < 10
        assert read_status(tmp_path / "machine")["alive"] is False


def test_cli_old_lease_keeps_recovery_visible(tmp_path, compared_cli):
    import fcntl
    from qianshu.feishu.state import State

    case = scenario(tmp_path, "first", "healthy")
    with cli(case) as proc:
        reg = ready(case, proc)
        os.kill(proc.pid, signal.SIGTERM)
        proc.wait(5)
        until(lambda: empty(proc.pid), seconds=5)
    case = scenario(tmp_path, "second", "healthy")
    (case / "takeover").touch()
    state = tmp_path / "private/trial-050.sqlite3"
    with ExitStack() as stack:
        lease = stack.enter_context((tmp_path / "machine/consumer.lock").open("a"))
        fcntl.flock(lease, fcntl.LOCK_EX)
        diagnostic = tmp_path / "machine/supervisor.log"
        previous = diagnostic.stat().st_size if diagnostic.exists() else 0
        proc = stack.enter_context(compared_cli(case))
        until(lambda: diagnostic.exists() and diagnostic.stat().st_size > previous, proc, seconds=120)
        assert any(row.get("registration") == reg and row["status"] == "open" for row in State.recovery_list(state))
        assert not (case / "mail-started").exists()
        fcntl.flock(lease, fcntl.LOCK_UN)
        ready(case, proc)
        until(lambda: "notice" in events(case), proc)
        assert sum(k == "dial" for k, _, _ in observations(case)) == 1


def test_cli_first_off_creates_no_approval_ledger(tmp_path, baseline_handoffs, packet_io):
    case = scenario(tmp_path, "first", "none", enabled=False)
    (case / "switches").write_text("MAIL_TRIAL_CARD_ENABLED=false\nMAIL_TRIAL_RECEIVE_ENABLED=false\n")
    with cli(case) as proc:
        send(case, 0, "group")
        assert snapshot(case, proc) == baseline_handoffs[0]
        until(lambda: (tmp_path / "machine/status.json").exists(), proc)
        assert not read_status(tmp_path / "machine")["alive"]
        assert not (tmp_path / "private/trial-050.sqlite3").exists()
        assert not events(case)
        assert not any(tag in ("mail", "external") for tag, _, _ in observations(case))


def test_cli_old_envelope_on_reconnect_never_grants(tmp_path, compared_cli):
    case = scenario(tmp_path, "first", "reconnect")
    with compared_cli(case, dials=2) as proc:
        old = ready(case, proc)
        click = dict(reg=old, action="verify", event_id="in-flight", stamp=int(time.time() * 1000))
        send(case, 0, click)
        until((case / "hit").exists, proc)
        until(lambda: ("toast", ["0", "已收到点击，请以卡片更新为准"])
              in [(k, v) for k, v, _ in observations(case)], proc)
        send(case, 1, "disconnect")
        until(lambda: int((case / "mail-started").read_text()) != old and
              read_status(tmp_path / "machine")["state"] == "ready", proc, seconds=120)
        send(case, 2, click)
        until(lambda: any(k == "toast" and v[0] == "2" for k, v, _ in observations(case)), proc)
        until(lambda: "manual" in events(case), proc)
        with closing(sqlite3.connect(tmp_path / "private/trial-050.sqlite3")) as db:
            assert db.execute("SELECT status FROM trial_card WHERE registration=?", (old,)).fetchone() == ("closed",)
            assert not db.execute("SELECT 1 FROM trial_receipt").fetchone()
            assert not db.execute("SELECT 1 FROM trial_item_task").fetchone()
        assert sum(k == "dial" for k, _, _ in observations(case)) == 2


@pytest.mark.parametrize("kind", ["card", "event"])
def test_cli_fragmented_card_releases_group_reservation(tmp_path, compared_cli, kind):
    case = scenario(tmp_path, "first", "healthy")
    with compared_cli(case) as proc:
        reg = ready(case, proc)
        send(case, 0, dict(reg=reg, action="verify", event_id="split", stamp=int(time.time()*1000),
                           fragment_kind=kind))
        until(lambda: ("toast", ["0", "已收到点击，请以卡片更新为准"])
              in [(k, v) for k, v, _ in observations(case)], proc)
        until(lambda: "patch" in events(case), proc)


def test_cli_status_distinguishes_queue_from_unconfirmed_work(tmp_path, compared_cli):
    case = scenario(tmp_path, "first", "hold_consume")
    with compared_cli(case) as proc:
        reg = ready(case, proc)
        send(case, 0, [reg, "verify"])
        until((case / "hit").exists, proc)
        try:
            send(case, 1, [reg, "verify"])
            def queued():
                status = read_status(tmp_path / "machine")
                return (status["alive"] and status["backlog"] >= 1 and status["unconfirmed"] >= 2
                        and status["oldest_wait"] > 0)
            until(queued, proc, seconds=2)
        finally:
            (case / "release").touch()
        until(lambda: read_status(tmp_path / "machine")["unconfirmed"] == 0, proc)


@pytest.mark.parametrize("age,member,actor,receipts", [
    (120, True, "ou_actor", 1),
    (120.001, True, "ou_actor", 0),
    (5, TimeoutError(), "ou_actor", 0),
    (5, False, "ou_actor", 0),
    (5, True, "ou_other", 0),
], ids=["at-limit", "expired", "lookup-failed", "absent", "outside-list"])
def test_g475_click_member_window(tmp_path, monkeypatch, age, member, actor, receipts):
    from dataclasses import replace
    from unittest.mock import AsyncMock
    from test_trial_paths import settings, setup_card, switches
    from qianshu.feishu.trial_consumer import Consumer
    from qianshu.feishu.trial_listener import Listener

    path = tmp_path / "trial-050.sqlite3"
    store, journal, original, pages = setup_card(path)
    with closing(store.db):
        stamp = 1893459600
        now = [stamp - 1]
        check = AsyncMock(return_value=member, side_effect=member if isinstance(member, Exception) else None)
        monkeypatch.setattr("qianshu.feishu.trial_listener.checked_transport",
                            lambda *_: SimpleNamespace(is_member=check))
        consumer = Consumer.__new__(Consumer)
        consumer.origin = SimpleNamespace(close_code=None)
        consumer.create_enabled = True
        consumer.epoch = "enabled"
        consumer.listener = listener = Listener(path, replace(settings(), tenant_key="tenant"), switches,
                                               clock=lambda: now[0])
        listener.rotate(consumer.origin, "连接")
        listener.queue_updates = True
        with store.write() as db:
            db.execute("INSERT INTO trial_switch_epoch VALUES('enabled','source',?,NULL,1)", (now[0],))
            db.execute("INSERT INTO trial_session_grant VALUES(?,?)", (listener.session, consumer.epoch))
            db.execute("UPDATE registration SET version=2")
        snapshot_data = json.loads(store.row("SELECT snapshot FROM trial_card WHERE card=?", original)[0])
        journal.prepare("live", 1, 2, listener.session, "区域甲", snapshot_data, False, now[0])
        with store.write() as db:
            for n in range(1, len(pages) + 1):
                db.execute("INSERT INTO card_intent VALUES(?,1,2,?,'sent','')", (str(n), n))
                journal.received(str(n), "message" if n == len(pages) else str(n))
            journal.bind("live", now[0])
        now[0] = stamp + age
        header = SimpleNamespace(app_id="app", tenant_key="tenant", event_type="card.action.trigger",
                                 create_time=str(stamp), event_id="click")
        event = SimpleNamespace(context=SimpleNamespace(open_message_id="message", open_chat_id="group"),
                                operator=SimpleNamespace(open_id=actor),
                                action=SimpleNamespace(value=dict(card="live", format=1, registration=1,
                                                                  version=2, action="verify")))
        consumer.deadline = time.monotonic() + 4
        with asyncio.Runner() as runner:
            loop = runner.get_loop()

            def tick():
                # 沙箱禁用 asyncio 跨线程唤醒套接字，时钟同时驱动登记及线程池关闭。
                loop.call_later(.01, tick)

            tick()
            runner.run(consumer.record(SimpleNamespace(header=header, event=event)))
        assert check.await_count == int(age <= 120 and actor == "ou_actor")
        assert store.row("SELECT count(*) FROM trial_receipt")[0] == receipts
        assert store.row("SELECT count(*) FROM trial_feedback WHERE status='pending'")[0] == 1 - receipts


def test_g475_private_window_cap(tmp_path):
    from qianshu.config import ConfigError, trial_config

    path = tmp_path / "switches"
    path.write_text("")
    key = "MAIL_TRIAL_MEMBER_CHECK_SECONDS"
    assert trial_config(path, approval=True)[key] == "120"
    path.write_text(f"{key}=45\n")
    assert trial_config(path, approval=True)[key] == "45"
    path.write_text(f"{key}=121\n")
    with pytest.raises(ConfigError):
        trial_config(path, approval=True)
