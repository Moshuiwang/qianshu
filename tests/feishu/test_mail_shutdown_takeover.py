import fcntl
import os
import signal

from test_listener_replay import cli, empty, observations, until
from test_production_replay import case_at, observed, stop


def test_blocked_root_handler_does_not_hold_listener_or_exit(tmp_path):
    case = case_at(tmp_path, "first", "single", fault="raise", switches=("true", "false"))
    # 在真正的关闭边界把根处理器卡在 emit 内，并持有 Handler 锁。
    with (case / "sitecustomize.py").open("a") as setup:
        setup.write("""
import logging
import threading
import time
from pathlib import Path
import listener_replay
original_record = listener_replay.record
def record(tag, *args):
    if tag == "mail-close":
        entered = threading.Event()
        class Blocked(logging.Handler):
            def emit(self, record):
                entered.set()
                while not Path("unblock").exists():
                    time.sleep(0.01)
        logging.getLogger().addHandler(Blocked())
        threading.Thread(target=lambda: logging.critical("synthetic"), daemon=True).start()
        assert entered.wait(1)
    original_record(tag, *args)
listener_replay.record = record
""")
    db = tmp_path / "private/handoff.db"
    with cli(case) as parent:
        try:
            observed(case, parent, db, 1)
            until(lambda: any(t == "mail" for t, _, _ in observations(case)), parent)
            os.killpg(parent.pid, signal.SIGINT)
            until(lambda: any(t == "mail-close" for t, _, _ in observations(case)))
            assert parent.wait(2.5) in (130, -signal.SIGINT)
            assert empty(parent.pid)
            with db.open("rb") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            second = case_at(tmp_path, "second", "single", other=True)
            with cli(second) as successor:
                observed(second, successor, tmp_path / "other/handoff.db", 1)
                assert stop(successor) == 0
        finally:
            (case / "unblock").touch()
    assert db.with_name("trial-050.dirty").exists()
    assert "CRITICAL 收信登记库关闭失败" in db.with_name("trial-050.fault").read_text()


def test_listener_error_and_mail_close_fault_exit_with_blocked_handler(tmp_path):
    case = case_at(tmp_path, "first", "single", fault="raise", switches=("true", "false"))
    with (case / "sitecustomize.py").open("a") as setup:
        setup.write("""
import logging
from threading import Event
from qianshu.feishu.handoff import HandoffService

def failed_listener(self):
    entered = Event()
    class Blocked(logging.Handler):
        def emit(self, record):
            entered.set()
            Event().wait()
    logging.getLogger().addHandler(Blocked())
    logging.getLogger("qianshu.feishu.handoff").warning("synthetic")
    assert entered.wait(2)
    raise RuntimeError("synthetic listener failure")

HandoffService.run = failed_listener
""")
    with cli(case) as parent:
        until(lambda: any(t == "mail-close" for t, _, _ in observations(case)), parent)
        assert parent.wait(3) == 1
        assert empty(parent.pid)
    for lock in (tmp_path / "machine/listener.lock", tmp_path / "private/handoff.db"):
        with lock.open("rb") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert "CRITICAL" in (tmp_path / "private/trial-050.fault").read_text()
