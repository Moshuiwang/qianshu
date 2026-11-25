"""异步回写与固定提示的进程内用例（G463、P1-3、P1-8）。

取代此前逐项起真实子进程的组合：改卡失败、超时、回执不明、出口检查拦截、缺检查器，
人工核对提示发送超时与被拦截，以及点击时间不符。真实子进程只保留每类故障一例
（test_approval_runtime.py）。
"""

import asyncio
import json
import time
from contextlib import closing, contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qianshu.mingdao.create_order import OutletConflict
from qianshu.feishu import trial_listener
from qianshu.feishu.trial_consumer import Consumer
from qianshu.feishu.trial_listener import Listener, OutletUnavailable

from test_trial_paths import settings, setup_card, switches

STAMP = 1893459600


class Transport:
    """受检传输的替身：成员即时查询为真，改卡请求按指定结局返回。"""

    def __init__(self, outcome):
        self.outcome = outcome
        self.patched = []
        self.is_member = AsyncMock(return_value=True)
        self.order_http = SimpleNamespace(async_request=self.request)
        self.before_send = None if outcome == "no_checker" else self.check

    @contextmanager
    def trial_action(self, claim=None):
        yield

    def check(self, outlet, value):
        if self.outcome == "bytes" and outlet == "patch_content":
            raise OutletConflict()

    async def _tenant_token(self):
        return "token"

    async def request(self, request, option, purpose, model):
        assert purpose == "patch"
        if self.outcome == "fail":
            raise OSError
        if self.outcome == "timeout":
            raise TimeoutError
        if self.outcome == "lost":
            raise ValueError("回执不明")
        self.patched.append(request.message_id)


def clicked(tmp_path, monkeypatch, transport, *, offset=5):
    path = tmp_path / "trial-050.sqlite3"
    store, journal, original, pages = setup_card(path)
    now = [STAMP - 1]
    monkeypatch.setattr("qianshu.feishu.trial_listener.checked_transport", lambda *_: transport)
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
    snapshot = json.loads(store.row("SELECT snapshot FROM trial_card WHERE card=?", original)[0])
    journal.prepare("live", 1, 2, listener.session, "区域甲", snapshot, False, now[0])
    with store.write() as db:
        for n in range(1, len(pages) + 1):
            db.execute("INSERT INTO card_intent VALUES(?,1,2,?,'sent','')", (str(n), n))
            journal.received(str(n), "message" if n == len(pages) else str(n))
        journal.bind("live", now[0])
    now[0] = STAMP + 5
    header = SimpleNamespace(app_id="app", tenant_key="tenant", event_type="card.action.trigger",
                             create_time=str(STAMP + 5 - offset), event_id="click")
    event = SimpleNamespace(context=SimpleNamespace(open_message_id="message", open_chat_id="group"),
                            operator=SimpleNamespace(open_id="ou_actor"),
                            action=SimpleNamespace(value=dict(card="live", format=1, registration=1,
                                                              version=2, action="verify")))
    consumer.deadline = time.monotonic() + 4
    with asyncio.Runner() as runner:
        loop = runner.get_loop()

        def tick():
            loop.call_later(.01, tick)

        tick()
        runner.run(consumer.record(SimpleNamespace(header=header, event=event)))
    return store, listener


@pytest.mark.parametrize("outcome", ["healthy", "fail", "timeout", "lost", "bytes", "no_checker"])
def test_async_patch_outcomes(tmp_path, monkeypatch, outcome):
    transport = Transport(outcome)
    store, _ = clicked(tmp_path, monkeypatch, transport)
    with closing(store.db):
        assert store.row("SELECT count(*) FROM trial_receipt")[0] == 1
        assert transport.is_member.await_count == 1
        status, = store.row("SELECT status FROM trial_update")
        manual = store.row("SELECT count(*) FROM trial_notification WHERE kind='result_manual'")[0]
        if outcome == "healthy":
            assert (status, manual, transport.patched) == ("sent", 0, ["message"])
        else:
            # 改卡失败、超时或拦截：不重试改卡，登记一次人工核对固定提示。
            assert (status, manual, transport.patched) == ("manual", 1, [])


@pytest.mark.parametrize("failure,status", [(TimeoutError, "unknown"), (OutletUnavailable, "blocked"),
                                            (OutletConflict, "blocked")])
def test_manual_notice_send_failures(tmp_path, monkeypatch, failure, status):
    store, listener = clicked(tmp_path, monkeypatch, Transport("fail"))
    sent = {}

    def send(transport, content, request_id, card, claim):
        if failure is TimeoutError:
            claim()
        sent.setdefault(request_id, []).append(content)
        raise failure()

    monkeypatch.setattr(trial_listener, "send_trial_message", send)
    with closing(store.db):
        for _ in range(2):
            try:
                listener.maintain()
            except TimeoutError:
                # 已交接后结果未知：本轮巡检中止，下一轮不重发该条。
                assert failure is TimeoutError
        uuid, current = store.row("SELECT uuid,status FROM trial_notification WHERE kind='result_manual'")
        assert sent[uuid] == [trial_listener.MANUAL_NOTICE]
        assert current == status


def test_future_click_time_refused_with_fixed_notice(tmp_path, monkeypatch):
    transport = Transport("healthy")
    store, _ = clicked(tmp_path, monkeypatch, transport, offset=-5)
    with closing(store.db):
        assert store.row("SELECT count(*) FROM trial_receipt")[0] == 0
        assert transport.is_member.await_count == 0
        assert store.row("SELECT count(*) FROM trial_feedback WHERE status='pending'")[0] == 1


@pytest.mark.parametrize("mode,create,action,expected", [
    ("cards", False, "approve", "建单未开启"),
    ("cards", True, "approve", "已收到点击，请以卡片更新为准"),
    ("cards", True, "verify", "已收到点击，请以卡片更新为准"),
    ("unavailable", True, "verify", "审批暂不可用"),
    ("off", True, "verify", "审批暂不可用"),
])
def test_supervisor_click_replies(mode, create, action, expected):
    from qianshu.feishu.trial_supervisor import TrialSupervisor
    supervisor = TrialSupervisor.__new__(TrialSupervisor)
    supervisor.mode, supervisor.create_enabled = mode, create
    offered = []
    supervisor.offer = lambda *args: offered.append(args) or True
    now = time.monotonic()
    data = {"event": {"action": {"value": {"action": action}}}}
    assert supervisor.click(b"{}", now, now + 5, data) == expected
    assert len(offered) == int(mode == "cards")


def test_supervisor_click_when_child_rejects():
    from qianshu.feishu.trial_supervisor import TrialSupervisor
    supervisor = TrialSupervisor.__new__(TrialSupervisor)
    supervisor.mode, supervisor.create_enabled = "cards", True
    supervisor.offer = lambda *args: False
    now = time.monotonic()
    assert supervisor.click(b"{}", now, now + 5, {"event": {}}) == "审批暂不可用"


def test_disconnect_closes_every_in_flight_card_at_once(tmp_path):
    """G478：断线时一次性关闭全部在途卡片，未发出的条目公告「未建单」。"""
    import sqlite3
    from qianshu.feishu.trial_approval import Journal
    from qianshu.mail.trial_state import Store

    store = Store(tmp_path / "trial")
    with store.write() as db:
        db.execute("INSERT INTO mailbox(scope,state) VALUES('s','active')")
        for reg in (1, 2):
            db.execute("INSERT INTO registration(id,scope,version,attachments,status) VALUES(?,'s',1,0,'done')",
                       (reg,))
    store.db.close()
    snapshot = {"preview": "digest", "eligible": [1], "items": [1], "missing": [],
                "pages": [{"title": "detail"}, {"title": "decision"}]}
    with closing(sqlite3.connect(store.path)) as db:
        db.row_factory = sqlite3.Row
        j = Journal(db)
        j.rotate("session", "重启")
        j.prepare("open", 1, 1, "session", "区域甲", snapshot, False, 86000)
        for n in (1, 2):
            db.execute("INSERT INTO card_intent VALUES(?,1,1,?,'sent','')", (f"open{n}", n))
            j.received(f"open{n}", f"message{n}")
        j.bind("open", 86001)
        # 第二张卡只发出首页，末页请求结果未知。
        j.prepare("sending", 2, 1, "session", "区域甲", snapshot, False, 86000)
        db.execute("INSERT INTO card_intent VALUES('sending1',2,1,1,'sent','')")
        j.received("sending1", "message3")
        db.execute("INSERT INTO card_intent VALUES('sending2',2,1,2,'unknown','')")
        db.commit()
        j.rotate("", "审批连接断开")
        assert dict(db.execute("SELECT card,status FROM trial_card")) == {"open": "closed", "sending": "closed"}
        closed = dict(db.execute("SELECT card,body FROM trial_notification WHERE kind='closed'"))
        assert set(closed) == {"open", "sending"}
        assert all("审批连接断开，未建单" in body for body in closed.values())
        assert db.execute("SELECT count(*) FROM trial_notification WHERE kind GLOB 'first_manual:*'").fetchone()[0] == 1
        j.rotate("new", "重连")
        assert db.execute("SELECT count(*) FROM trial_notification WHERE kind='closed'").fetchone()[0] == 2
