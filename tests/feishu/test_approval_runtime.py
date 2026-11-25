import asyncio
import compileall
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
    # 与正常安装一致预编译；回放进程不写字节码，否则每个解释器都要重新编译本包。
    compileall.compile_dir(source / "qianshu", quiet=1)
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
    # 仅回放夹具：在入口之前载入飞书 SDK 时暂停循环垃圾回收以缩短用例耗时，生产入口不做此处理。
    hooks = (case / "sitecustomize.py").read_text()
    (case / "sitecustomize.py").write_text("import gc\ngc.disable()\n" + hooks
                                           + "from approval_replay import install\ninstall()\ngc.enable()\n")
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


def ready(case, proc, seconds=15):
    def sample():
        if read_status(case.parent / "machine")["state"] != "ready" or not (case / "mail-ready").exists():
            return None
        owner, registration = json.loads((case / "mail-ready").read_text())
        pid, group, _ = json.loads((case / "child-facts").read_text())
        return registration if (owner, group) == (pid, proc.pid) else None
    return until(sample, proc, seconds=seconds)


@pytest.fixture(scope="module")
def baseline_handoffs(baseline, tmp_path_factory, packet_io):
    """main 独立源码的一次常规转交（先卡片洪峰再群 @）：群快照与群回执时延，作为对照。"""
    case = scenario(tmp_path_factory.mktemp("plain"), "first", "none", enabled=False)
    with cli(case, baseline) as proc:
        send(case, 0, "flood")
        send(case, 1, "group")
        return snapshot(case, proc), group_delay(case)


def group_delay(case):
    tags = observations(case)
    start, = [t for k, _, t in tags if k == "first"]
    end, = [t for k, value, t in tags if k == "ack" and value[0] == "group"]
    return end - start


def toast(case, n, text):
    return ("toast", [str(n), text]) in [(k, v) for k, v, _ in observations(case)]


def test_cli_healthy_click_flood_and_parent_death(tmp_path, baseline_handoffs):
    """健康档：G475 即时核验、G463 异步改写原卡、G470 洪峰后群 @，以及守望者运维四条。"""
    expected, baseline_delay = baseline_handoffs
    case = scenario(tmp_path, "first", "healthy")
    with cli(case) as proc:
        reg = ready(case, proc)
        send(case, 0, [reg, "verify"])
        send(case, 1, "flood")
        send(case, 2, "group")
        until(lambda: toast(case, 0, "已收到点击，请以卡片更新为准"), proc, seconds=3)
        until(lambda: "patch" in events(case), proc, seconds=3)
        assert snapshot(case, proc) == expected
        assert group_delay(case) <= baseline_delay
        assert events(case).count("member-lookup") == 1
        assert sum(k == "dial" for k, _, _ in observations(case)) == 1
        assert "external" not in events(case)
        pid, group, cap = json.loads((case / "child-facts").read_text())
        assert group == proc.pid and pid != proc.pid and 0 < cap <= 512 << 20

        def settled():
            state = read_status(tmp_path / "machine")
            return state["state"] == "ready" and state["alive"] and state["backlog"] == state["unconfirmed"] == 0
        until(settled, proc, seconds=3)
        # 父亡：子进程被冻结时，父进程收到 SIGTERM 后整组在 10 秒内退出。
        os.kill(pid, signal.SIGSTOP)
        began = time.monotonic()
        os.kill(proc.pid, signal.SIGTERM)
        proc.wait(5)
        until(lambda: empty(group), seconds=5)
        assert time.monotonic() - began < 10
        assert read_status(tmp_path / "machine")["alive"] is False


# G476 / G478：每类故障一例真实子进程，证明故障中审批停用（本地 CRITICAL、点击只回固定提示）、
# 群 @ 转交不受影响、不重拨。同类其余故障点、启动挂起与就绪后卡死，以及故障解除后的退避
# 自动恢复，见 test_sidecar_recovery.py（真实监督 + 轻量替身子进程）与进程内用例。
FAULT_CLASSES = {
    "import": "启动期异常退出（审批模块导入失败）",
    "crash": "启动期被信号终止",
    "mail_poll": "收信线程故障",
    "malformed": "点击处理异常退出",
}


@pytest.mark.parametrize("fault", list(FAULT_CLASSES))
def test_cli_approval_fault_disables_without_touching_handoff(tmp_path, baseline_handoffs, fault):
    case = scenario(tmp_path, "first", fault)
    (case / "fault-on").touch()
    diagnostic = tmp_path / "machine/supervisor.log"
    with cli(case) as proc:
        n = 0
        if fault == "malformed":
            # 只等审批就绪即点击（格式错的负载不需要卡片登记号）。
            until(lambda: read_status(tmp_path / "machine")["state"] == "ready", proc, seconds=15)
            send(case, n, [1, "verify"])
            n += 1
        send(case, n, "group")
        until((case / "hit").exists, proc, seconds=10)
        until(lambda: diagnostic.exists() and "CRITICAL E-TRIAL-DISABLED" in diagnostic.read_text(), proc,
              seconds=5)
        send(case, n + 1, [1, "verify"])
        until(lambda: toast(case, n + 1, "审批暂不可用"), proc, seconds=3)
        assert snapshot(case, proc) == baseline_handoffs[0]
        assert sum(k == "dial" for k, _, _ in observations(case)) == 1
        assert "external" not in events(case)


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
