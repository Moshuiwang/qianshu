import multiprocessing
from contextlib import closing
from dataclasses import replace
from math import ceil
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qianshu.feishu.entrance import Message
from qianshu.feishu.handoff import Handoff, HandoffService, MentionLog
from qianshu.feishu.settings import Settings
from qianshu.mail import trial


@pytest.fixture
def service(tmp_path, monkeypatch):
    business = tmp_path / "business"
    business.write_text(f"MAIL_PRIVATE_DIR={tmp_path}\nMAIL_HOST=synthetic\nMAIL_USER=local\n"
                        "MAIL_PASSWORD=secret\nFEISHU_CHAT_ID=group\n")
    settings = Settings("app", "secret", "group", "bot", frozenset({"owner"}))
    path = tmp_path / "mention"
    monkeypatch.setattr(HandoffService, "__init__", lambda self, store: setattr(self, "store", store))

    def start(config):
        trial.run_service(SimpleNamespace(business_env_file=business), config, path, lambda: {})

    return path, settings, start


def test_contending_trial_process_opens_neither_database(service, monkeypatch, fork_process):
    path, settings, start = service
    processes = multiprocessing.get_context("fork")
    ready, finish = processes.Event(), processes.Event()

    def listen(self):
        assert finish.wait(15)

    monkeypatch.setattr(HandoffService, "run", listen)
    monkeypatch.setattr(trial.Trial, "run", lambda self: ready.set())
    holder = fork_process(start, settings)
    try:
        assert ready.wait(15)
        assert path.exists() and path.with_name("trial-050.sqlite3").exists()

        open_handoff = Mock(side_effect=AssertionError("抢锁失败仍打开转交库"))
        open_trial = Mock(side_effect=AssertionError("抢锁失败仍打开试运行登记库"))
        monkeypatch.setattr(MentionLog, "_open_database", open_handoff)
        monkeypatch.setattr(trial, "Store", open_trial)
        with pytest.raises(BlockingIOError):
            start(replace(settings, handoff_member_open_ids=frozenset({"member"})))
        open_handoff.assert_not_called()
        open_trial.assert_not_called()
    finally:
        finish.set()
        holder.join(15)
        if holder.is_alive():
            holder.kill()
            holder.join()
    assert holder.exitcode == 0


@pytest.mark.parametrize("trial_enabled", [False, True])
def test_member_handoff_starts_at_listener_activation(service, monkeypatch, trial_enabled):
    path, settings, start = service
    with closing(MentionLog(path, settings, create=True, clock=lambda: 1)) as log:
        assert not log.pending()
    enabled = replace(settings, handoff_member_open_ids=frozenset({"member"}))

    def check(log):
        since, = log._db.execute("SELECT since FROM memberships WHERE active = 1").fetchone()
        first_ms = ceil(since * 1000)
        reactions = []
        relay = Handoff(log, SimpleNamespace(react=lambda mid, *, deadline: reactions.append(mid)))
        for message_id, timestamp in (("before", first_ms - 1), ("active", first_ms)):
            message = Message(message_id, "group", "member", timestamp, "text", "synthetic", ("bot",))
            relay.receive(message, deadline=float("inf"))
        assert reactions == ["active"]
        row, = log.pending()
        assert (row["message_id"], row["sender"], row["fallback_status"]) == ("active", "名单内同事", "pending")

    if trial_enabled:
        monkeypatch.setattr(trial.Trial, "run", lambda self: None)
        monkeypatch.setattr(HandoffService, "run", lambda self: check(self.store))
        start(enabled)
    else:
        with MentionLog.listening(path, enabled) as log:
            check(log)
