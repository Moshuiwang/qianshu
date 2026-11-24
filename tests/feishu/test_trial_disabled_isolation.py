"""开关全关时，审批恢复出任何错都不得拖累群 @ 转交（守望者对 #154 段 1 的补充要求）。"""
import sqlite3
from types import SimpleNamespace

import pytest

from qianshu.mail import trial


class Service:
    def __init__(self):
        self.runs = 0

    def run(self):
        self.runs += 1


def test_no_history_runs_original_handoff_once(tmp_path):
    service = Service()
    trial.run_disabled(service, tmp_path / "trial-050.sqlite3", SimpleNamespace())
    assert service.runs == 1


@pytest.mark.parametrize("failure", ["history", "listener"])
def test_recovery_setup_failure_still_runs_handoff(tmp_path, monkeypatch, failure):
    service = Service()

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked") if failure == "history" else RuntimeError("synthetic")

    if failure == "history":
        monkeypatch.setattr(trial, "has_approval_history", broken)
    else:
        monkeypatch.setattr(trial, "has_approval_history", lambda path: True)
        monkeypatch.setattr(trial, "Listener", broken)
    trial.run_disabled(service, tmp_path / "trial-050.sqlite3", SimpleNamespace())
    assert service.runs == 1


def test_locked_history_db_keeps_handoff_and_recovery_retries(tmp_path, monkeypatch):
    path = tmp_path / "trial-050.sqlite3"
    path.touch(mode=0o600)
    holder = sqlite3.connect(path)
    holder.execute("BEGIN EXCLUSIVE")
    attempts = []

    class Listener:
        def __init__(self, *args, **kwargs):
            pass

        def maintain(self):
            attempts.append(1)
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(trial, "Listener", Listener)
    service = Service()

    def run():
        service.runs += 1
        # 转交运行期间恢复线程持续失败，不影响转交本身。
        for _ in range(200):
            if attempts:
                break
            import time
            time.sleep(0.01)

    service.run = run
    try:
        assert trial.has_approval_history(path) is True  # 读不到即按「可能有历史」处理
        trial.run_disabled(service, path, SimpleNamespace())
    finally:
        holder.rollback()
        holder.close()
    assert service.runs == 1 and attempts
