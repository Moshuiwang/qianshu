import fcntl
import multiprocessing
import os
import signal
import threading
from unittest.mock import Mock

import pytest

from qianshu.feishu.handoff import HandoffService, MentionLog
from qianshu.mail import trial
from test_trial_listener import service


@pytest.mark.parametrize("phase", ["start", "wait", "twice", "exit", "normal", "close_error"])
def test_signal_scope(service, monkeypatch, phase):
    path, settings, run = service
    ctx = multiprocessing.get_context("fork")
    ready, resume, closed, closing, exit_log, caught, failed = (ctx.Event() for _ in range(7))
    original_start = threading.Thread.start
    original_close = MentionLog.close

    def start(thread):
        original_start(thread)
        if thread.name == "trial-mail" and phase == "start":
            assert ready.wait(5)
            signal.raise_signal(signal.SIGINT)

    def poll(worker):
        db = worker.store.db

        def close():
            closing.set()
            if phase == "close_error":
                raise RuntimeError
            assert resume.wait(8)
            db.close()
            closed.set()

        worker.store.db = Mock(wraps=db)
        worker.store.db.close = close
        ready.set()

    def close_log(log):
        if phase == "exit":
            signal.raise_signal(signal.SIGINT)
        assert closed.is_set()
        original_close(log)
        exit_log.set()

    def serve():
        try:
            run(settings)
        except KeyboardInterrupt:
            caught.set()
            assert exit_log.is_set()

    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(trial.Trial, "run", poll)
    monkeypatch.setattr(HandoffService, "run", lambda self: None)
    monkeypatch.setattr(MentionLog, "close", close_log)
    monkeypatch.setattr(trial._LOG, "critical", lambda *args: failed.set())
    process = ctx.Process(target=serve)
    process.start()
    try:
        assert closing.wait(5)
        with path.open("rb") as contender:
            if phase in {"wait", "twice"}:
                os.kill(process.pid, signal.SIGINT)
                if phase == "twice":
                    caught.wait(0.1)
                    os.kill(process.pid, signal.SIGINT)
            if phase == "close_error":
                assert failed.wait(2)
            assert not caught.wait(0.1)
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if phase != "close_error":
                resume.set()
                process.join(5)
                assert process.exitcode == 0 and exit_log.is_set()
                assert caught.is_set() == (phase != "normal")
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        resume.set()
        if process.is_alive():
            process.kill()
        process.join()
