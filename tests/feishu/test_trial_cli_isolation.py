import asyncio
import logging
import sqlite3
import threading
import sys
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from qianshu import __main__ as cli
from qianshu.feishu import handoff, sdk
from tests.feishu.test_handoff import FakeFeishu, message
from tests.feishu.test_sdk import FakeSocket, encoded_frame


def replay(tmp_path, patch, caplog, fault=None, enabled=False):
    path = tmp_path / "mentions"
    business = tmp_path / "private-config"
    business.write_text(f"FEISHU_MENTION_LOG={path}\nFEISHU_HANDOFF_OWNER_OPEN_ID=ou_owner\n"
                        "FEISHU_BOT_OPEN_ID=ou_bot\nFEISHU_APP_ID=app\nFEISHU_APP_SECRET=secret\n"
                        f"FEISHU_CHAT_ID=group\nMAIL_PRIVATE_DIR={tmp_path}\nMAIL_HOST=synthetic\n"
                        "MAIL_USER=local\nMAIL_PASSWORD=secret\n")
    argv = ["--env-file", str(business), "--business-env-file", str(business), "--handoff-only"]
    locks = []
    acquire = handoff.MentionLog.listener
    @contextmanager
    def listening(store, *args, **kwargs):
        with acquire(store, *args, **kwargs):
            locks.append(1)
            yield
    patch.setattr(handoff.MentionLog, "listener", listening)
    opened = []
    original = handoff.MentionLog.__init__
    def open_log(store, *args, **kwargs):
        opened.append(1)
        original(store, *args, clock=lambda: 1000 if not dial.await_count else 1010, **kwargs)

    fake = FakeFeishu()
    fake._require_window = lambda: None
    fake.messages = [replace(message("missed", created=1005, sender="ou_owner"), chat_id="group")]
    patch.setattr(handoff, "_Transport", lambda *args: fake)
    patch.setattr(handoff.MentionLog, "__init__", open_log)
    patch.setattr(cli, "setup_logging", lambda *args: None)
    patch.setattr(sdk._Connection, "_discover_endpoint", AsyncMock(return_value="wss://synthetic.invalid/?device_id=d&service_id=1"))
    socket = FakeSocket()
    dial = AsyncMock(return_value=socket)
    patch.setattr(sdk.ws.websockets, "connect", dial)
    seen = []
    if fault:
        from qianshu.mail import trial
        switch_file = tmp_path / "switches"
        flag = str(enabled).lower()
        contents = f"MAIL_TRIAL_RECEIVE_ENABLED={flag}\nMAIL_TRIAL_CARD_ENABLED={flag}\n"
        if fault == "approvers":
            contents += "MAIL_TRIAL_APPROVER_OPEN_IDS=invalid\n"
        elif fault in {"malformed", "repair"}:
            contents += "MAIL_TRIAL_CREATE_COUNTRY='\n"
        switch_file.write_text(contents)
        argv += ["--trial-switches", str(switch_file)]
        patch.setattr(trial, "has_approval_history", lambda _: True)
        patch.setattr(trial.Trial, "run", lambda self: None)
        def fail(*args, **kwargs):
            seen.append(1)
            raise {"stop_event": OSError, "listener": RecursionError,
                   "maintenance": BaseException}.get(fault, RuntimeError)()
        if fault == "listener":
            patch.setattr(trial, "Listener", fail)
        elif fault == "maintenance":
            patch.setattr(trial.Listener, "maintain", fail)
        elif fault == "rotation":
            request = trial.Listener.request_rotation
            def schedule(listener, origin, reason):
                if not enabled or origin is not None:
                    fail()
                request(listener, origin, reason)
            patch.setattr(trial.Listener, "request_rotation", schedule)
        elif fault == "register":
            builder = type(sdk.lark.EventDispatcherHandler.builder("", ""))
            patch.setattr(builder, "register_p2_card_action_trigger", fail)
        elif fault == "stop_event":
            class StopEvent(threading.Event):
                def __init__(self):
                    caller = sys._getframe(1).f_globals.get("__name__")
                    if caller in {trial.__name__, "qianshu.feishu.trial_lifecycle"}:
                        fail()
                    super().__init__()
            patch.setattr(threading, "Event", StopEvent)
        elif fault == "thread_start":
            start = threading.Thread.start
            def start_thread(thread):
                if thread.name.startswith("trial-approval"):
                    fail()
                return start(thread)
            patch.setattr(threading.Thread, "start", start_thread)

    serve = handoff.HandoffService._serve
    event = dict(sender=dict(sender_type="user", sender_id=dict(open_id="ou_owner")),
                 message=dict(message_id="live", chat_id="group", chat_type="group", message_type="text",
                              create_time="1005000", content="{}", mentions=[dict(key="@1", id=dict(open_id="ou_bot"))]))
    frame = encoded_frame(dict(schema="2.0", header=dict(app_id="app", event_id="event",
                               event_type="im.message.receive_v1"), event=event))
    card = encoded_frame(dict(schema="2.0", header=dict(app_id="app", event_id="card",
                              event_type="card.action.trigger"), event={}), sdk.ws.MessageType.CARD)
    async def exercise(service, client):
        running = asyncio.create_task(serve(service, client))
        try:
            async with asyncio.timeout(4):
                while not client.receiver:
                    await asyncio.sleep(.01)
                for _ in range(2):
                    await client._handle_message(frame, client._frame_timing(socket))
                def recovered():
                    with sqlite3.connect(path) as db:
                        return db.execute("SELECT through FROM recovery").fetchone()[0] == 1010
                while "live" not in fake.reactions or not recovered():
                    await asyncio.sleep(.01)
                if fault:
                    while not any(r.levelno == logging.CRITICAL and r.getMessage() == "E-TRIAL-DISABLED"
                                  for r in caplog.records):
                        await asyncio.sleep(.02)
                    if fault == "rotation":
                        while len(seen) < 2:
                            await asyncio.sleep(.02)
                    await client._handle_message(card, client._frame_timing(socket))
                    if fault == "repair":
                        assert socket.callback_responses[-1]["toast"]["content"] == "审批暂不可用"
                        switch_file.write_text("MAIL_TRIAL_RECEIVE_ENABLED=true\nMAIL_TRIAL_CARD_ENABLED=true\n")
                        while socket.callback_responses[-1]["toast"]["content"] == "审批暂不可用":
                            await asyncio.sleep(.05)
                            await client._handle_message(card, client._frame_timing(socket))
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    patch.setattr(handoff.HandoffService, "_serve", exercise)
    assert cli.main(argv) == 0
    assert len(locks) == len(opened) == dial.await_count == 1
    assert socket.responses[:2] == ["event_ack"] * 2
    assert fake.reactions.count("live") == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT message_id FROM mentions ORDER BY 1").fetchall() == [("live",), ("missed",)]
        assert db.execute("SELECT started, through FROM recovery").fetchone() == (1000, 1010)
    if fault == "repair":
        assert "不符" in socket.callback_responses[-1]["toast"]["content"]
    elif fault:
        assert fault in {"malformed", "approvers"} or seen
        assert socket.callback_responses[-1]["toast"]["content"] == "审批暂不可用"


def test_reference_handoff(tmp_path, monkeypatch, caplog):
    replay(tmp_path, monkeypatch, caplog)


@pytest.mark.parametrize("enabled", [False, True], ids=["off", "cards"])
@pytest.mark.parametrize("fault", ["approvers", "malformed", "stop_event", "listener", "register",
                                   "thread_start", "rotation", "maintenance"])
def test_cli_fault_preserves_handoff(tmp_path, monkeypatch, caplog, fault, enabled):
    replay(tmp_path, monkeypatch, caplog, fault, enabled)


def test_cli_repairs_switch_file_without_redial(tmp_path, monkeypatch, caplog):
    replay(tmp_path, monkeypatch, caplog, "repair", True)
