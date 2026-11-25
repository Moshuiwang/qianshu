"""#154 段 2d-1：存档成功后建正式单、逐条回读、写错即停。

多数用例用进程内替身（假明道云、假飞书、假断线判断）；两个真实子进程用例验证断线空档照发 1 张、在途建单时子进程被强杀后
按结果未知冻结不重发，沙箱拒绝 Unix 套接字时跳过，只在本机跑。
"""

import json
import os
import socket
import subprocess
import sys
import time
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid5

import pytest

from qianshu.feishu import archive as A
from qianshu.feishu.trial_approval import journal_at, recovery
from qianshu.feishu.trial_order import (
    ORDER_NOTICES, SUPERSEDED, NotSent, OrderRunner, Plan, build_order_runner, name_fingerprint, name_key,
)
from qianshu.mingdao.create_order import UnverifiedCombination
from test_trial_archive import NOW, SW, accept, deliver, query, sent
from test_trial_archive import env  # noqa: F401  （复用 2c 夹具）
from tests.mingdao.test_create_order import case  # noqa: F401  （复用 X5 组装夹具）

NAMES = ("合成政策甲", "合成政策乙")
FIELDS = ["政策名称", "任务审批链接"]
NS = UUID(int=7)


def record(item):
    return str(uuid5(NS, f"record-{item}"))


class FakeMingdao:
    """假明道云：plan 只用卡片快照里的政策名；send 先「预建连」再调交接回调，通过后才记为发出；readback 按 verdicts 返回。"""

    def __init__(self):
        self.sent, self.reads, self.urls = [], [], []
        self.plan_errors, self.verdicts, self.send_errors = {}, {}, {}
        self.on_send = None
        self.before_handoff = None  # 预建连期间发生的事（如时钟走过截止、开关关闭、断线）
        self.record_ids = {}

    def plan(self, snapshot, item, url, *, now, sent):
        if item in self.plan_errors:
            raise self.plan_errors[item]
        self.urls.append(url)
        name = snapshot["reviewed"][item - 1]["policy_name"]["value"]
        values = {"政策名称": sha256(name.encode()).hexdigest()[:16], "任务审批链接": sha256(url.encode()).hexdigest()[:16]}
        return Plan(name, "sheet", list(FIELDS), values, dict(values), {"item": item, "url": url})

    def send(self, plan, handoff):
        item = plan.payload["item"]
        if self.before_handoff:
            self.before_handoff(item)
        try:
            handoff()
        except Exception as error:
            raise NotSent(getattr(error, "code", "not_sent")) from None
        self.sent.append(item)
        if self.on_send:
            self.on_send(item)
        if item in self.send_errors:
            raise self.send_errors[item]
        return self.record_ids.get(item, record(item))

    def readback(self, plan, record_id, budget):
        self.reads.append(record_id)
        verdict = self.verdicts.get(plan.payload["item"], "一致")
        if verdict == "slow":
            return None, []
        if verdict == "raise":
            raise TimeoutError
        results = ["一致", "不一致" if verdict == "写错" else "一致"]
        return verdict, [{"字段名": f, "结果": r} for f, r in zip(FIELDS, results)]


def rewrite_snapshot(path, **changes):
    with journal_at(path) as j, j.db:
        snapshot = json.loads(j.db.execute("SELECT snapshot FROM trial_card").fetchone()[0])
        for key, value in changes.items():
            snapshot[key] = value
        packed = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        j.db.execute("UPDATE trial_card SET snapshot=?, fingerprint=?", (packed, sha256(packed.encode()).hexdigest()))


_TEMPLATE = {}


def archived(env, names=NAMES):
    """出卡 → 同意 → 存档成功（2c 真实执行器）→ 两条拟建条目待建单。

    2c 存档一次约 3 秒：首次真实走一遍，之后复制同一登记库，控制本文件总时长。
    """
    if names == NAMES and _TEMPLATE:
        fd = os.open(env.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(_TEMPLATE["db"])
        return _TEMPLATE["card"]
    card = _archived(env, names)
    if names == NAMES:
        _TEMPLATE.update(db=env.path.read_bytes(), card=card)
    return card


def _archived(env, names):
    card, _ = accept(env.path)
    assert env.runner().run_once()
    with journal_at(env.path) as j, j.db:
        snapshot = json.loads(j.db.execute("SELECT snapshot FROM trial_card").fetchone()[0])
        j.db.execute("INSERT INTO trial_item_task VALUES(1,1,2,'formal',?,'archive_done')", (card,))
    reviewed = snapshot["reviewed"]
    for n, name in enumerate(names):
        reviewed[n]["policy_name"]["value"] = name
    rewrite_snapshot(env.path, eligible=[1, 2], reviewed=reviewed)
    with journal_at(env.path) as j, j.db:
        # 夹具把第 2 条改成出卡时即拟建：撤掉出卡时为它登记的人工移交（特批第 3 轮第 1 条），与「甲乙都拟建」一致。
        j.db.execute("DELETE FROM trial_handoff")
    assert query(env.path, "SELECT item,status FROM trial_item_task ORDER BY item") == [
        (1, "archive_done"), (2, "archive_done")]
    return card


@pytest.fixture
def order(env):
    fake = FakeMingdao()
    env.mingdao = fake

    def runner(**kwargs):
        def pause(seconds):
            env.now += max(seconds, 1.0)
        kwargs.setdefault("readback_seconds", 5)
        return OrderRunner(env.path, switches=lambda: env.switches, backend=fake, connected=lambda: env.connected,
                           ledger_dir=env.tmp, clock=lambda: env.now, pause=pause, **kwargs)
    env.orders = runner
    return env


def ledger(env):
    path = env.tmp / "trial-orders.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def events(env, item=None):
    return [r["event"] for r in ledger(env) if item is None or r["item"] == item]


def notices(env):
    return dict(query(env.path, "SELECT kind,body FROM trial_notification"))


# ---------------------------------------------------------------- 主链

def test_serial_build_with_archive_link_and_readback_then_done(order):
    card = archived(order)
    url = query(order.path, "SELECT url FROM trial_archive")[0][0]
    runner = order.orders()
    assert runner.run_once() is True
    fake = order.mingdao
    assert fake.sent == [1, 2] and fake.reads == [record(1), record(2)] and fake.urls == [url, url]
    assert query(order.path, "SELECT item,status,record_id,disconnect FROM trial_order ORDER BY item") == [
        (1, "verified", record(1), 0), (2, "verified", record(2), 0)]
    assert query(order.path, "SELECT status FROM trial_item_task ORDER BY item") == [("order_done",), ("order_done",)]
    assert query(order.path, "SELECT status FROM trial_card") == [("done",)]
    assert query(order.path, "SELECT * FROM trial_order_slot") == []
    assert notices(order) == {"order_done": "登记 1 · 版本 1 · 条目 1、2 · " + ORDER_NOTICES["done"]}
    assert events(order, 1) == ["create_reserved", "create_attempt", "created", "readback_compared"]
    rows = ledger(order)
    assert all(r["registration"] == f"trial-1-v1-i{r['item']}" and r["card_uuid"] == card and r["is_test"] is False
               and r["trial_registration"] == 1 and r["trial_version"] == 1 and r["country"] == "区域甲"
               and r["target"] == "sheet" for r in rows)
    attempt = next(r for r in rows if r["event"] == "create_attempt")
    assert attempt["request_fields"] == FIELDS and attempt["approved"] is True and attempt["record_id"] is None
    created = next(r for r in rows if r["event"] == "created")
    assert created["displayed"] == created["written"] and created["disconnect"] is False
    compared = next(r for r in rows if r["event"] == "readback_compared")
    assert compared["结论"] == "一致" and compared["total"] == 2 and compared["counts"] == {"一致": 2}
    # 再跑、重启都不再发。
    assert runner.run_once() is False and order.orders().run_once() is False and fake.sent == [1, 2]


def test_readback_ledger_is_readable_by_daily_report(order):
    from scripts.trial_report import read_orders
    from datetime import UTC, datetime, timedelta
    archived(order)
    order.orders().run_once()
    now = datetime.now(UTC)
    result = read_orders(order.tmp / "trial-orders.jsonl", now - timedelta(hours=1), now + timedelta(hours=1))
    assert set(result.orders) == {record(1), record(2)} and result.gaps == 0
    assert not any(o.wrong for o in result.orders.values())


@pytest.mark.parametrize("scenario", ["expired", "create_off", "card_off", "country", "halted", "slot_busy",
                                      "card_closed", "session", "disconnected", "unreadable_switches"])
def test_five_checks_in_one_transaction_any_failure_sends_nothing(order, scenario):
    case = scenario
    archived(order)
    if case == "expired":
        order.now = NOW + 3 * 86400
    elif case == "create_off":
        order.switches["MAIL_TRIAL_CREATE_ENABLED"] = "false"
    elif case == "card_off":
        order.switches["MAIL_TRIAL_CARD_ENABLED"] = "false"
    elif case == "country":
        order.switches["MAIL_TRIAL_CREATE_COUNTRY"] = "区域乙"
    elif case == "unreadable_switches":
        order.switches = None
    with journal_at(order.path) as j, j.db:
        if case == "halted":
            j.db.execute("INSERT INTO trial_halt VALUES(1,'守望者停写',0)")
        elif case == "card_closed":
            j.db.execute("UPDATE trial_card SET status='closed'")
        elif case == "session":
            j.db.execute("UPDATE trial_session SET session='other'")
    runner = order.orders()
    if case == "slot_busy":
        with journal_at(order.path) as j, j.db:
            j.db.execute("INSERT INTO trial_order_slot VALUES(1,9,9,9,0)")
        assert runner.run_once() is False
    elif case == "disconnected":
        # 规划期间观察到断线：登记事务核到断线，不登记发送。
        def plan(*a, **k):
            order.connected = False
            return FakeMingdao.plan(order.mingdao, *a, **k)
        order.mingdao.plan = plan
        runner.run_once()
    elif case == "card_closed":
        assert runner.run_once() is False
    else:
        if case == "unreadable_switches":
            runner.switches = lambda: 1 / 0
        runner.run_once()
    assert order.mingdao.sent == []
    assert query(order.path, "SELECT count(*) FROM trial_order") == [(0,)]
    assert "create_attempt" not in events(order)
    if case in ("slot_busy", "card_closed"):
        return
    assert query(order.path, "SELECT status FROM trial_item_task ORDER BY item") == [("closed",), ("closed",)]
    bodies = [b for k, b in notices(order).items() if k.startswith("order_cancel")]
    expected = "断线期间未发出，已转人工。" if case == "disconnected" else "未建单，已转人工。"
    assert bodies and all(expected in b for b in bodies)
    assert events(order, 1) == ["create_reserved", "aborted_before_write", "approval_closed"]
    assert all(r.get("allowed_writes", 0) == 0 for r in ledger(order))


def test_slow_readback_blocks_next_and_freezes_as_unknown(order):
    archived(order)
    order.mingdao.verdicts[1] = "slow"
    runner = order.orders()
    runner.run_once()
    assert order.mingdao.sent == [1] and len(order.mingdao.reads) >= 2
    assert query(order.path, "SELECT item,status,reason FROM trial_order") == [(1, "unknown", "readback")]
    assert query(order.path, "SELECT item FROM trial_order_slot") == [(1,)]  # 不释放
    assert query(order.path, "SELECT status FROM trial_item_task ORDER BY item") == [("order_unknown",), ("closed",)]
    texts = notices(order)
    assert texts["order_unknown:1"].endswith(ORDER_NOTICES["unknown"])
    assert "条目 2" in texts["order_cancel:2"] and "未建单" in texts["order_cancel:2"]
    assert events(order, 1)[-1] == "readback_unconfirmed"
    with journal_at(order.path) as j:
        kinds = {(r["kind"], r.get("item")) for r in recovery(j.db)}
        assert ("order", 1) in kinds and ("order_slot", 1) in kinds
        assert j.notice_counts()[0] >= 1
    # 冻结后不重发、不清理：同一执行器、新执行器（重启）都不再发。
    assert runner.run_once() is False and order.orders().run_once() is False
    assert order.mingdao.sent == [1]


def test_first_readback_mismatch_halts_all_writes_and_alerts(order):
    archived(order)
    order.mingdao.verdicts[1] = "写错"
    order.orders().run_once()
    assert order.mingdao.sent == [1]
    with journal_at(order.path) as j:
        assert j.write_halted()
        assert j.halt_state()["last_change"]["action"] == "request_create_off"
    assert query(order.path, "SELECT status FROM trial_order") == [("mismatch",)]
    texts = notices(order)
    assert texts["order_mismatch"].endswith(ORDER_NOTICES["mismatch"])
    assert "建单已停写" in texts["order_cancel:2"]
    compared = [r for r in ledger(order) if r["event"] == "readback_compared"]
    assert compared[0]["结论"] == "写错" and compared[0]["counts"] == {"一致": 1, "不一致": 1}
    # 停写位置位后，即便条目重新待建也一律不发。
    with journal_at(order.path) as j, j.db:
        j.db.execute("UPDATE trial_item_task SET status='archive_done' WHERE item=2")
        j.db.execute("UPDATE trial_card SET status='accepted'")
    order.orders().run_once()
    assert order.mingdao.sent == [1]
    # 告警投递时 @ 配置负责人。
    listener, transport = deliver(order)
    listener.maintain()
    assert all('<at user_id=\\"ou_config\\">' in t for t in sent(transport))


def test_readback_missing_written_field_halts_like_mismatch(order):
    """修复轮 2 第 4 条（守望者 12:0xZ）：回读缺 1 个已写字段 → 判不一致（不是未确认）、同一事务置停写位、
    下一条 0 发、告警 @ 配置负责人。回读结论用正式路径同一函数 readback_verdict 从 X5 compare 形状的报告得出。"""
    from qianshu.feishu.trial_order import readback_verdict

    def readback(plan, record_id, budget):
        order.mingdao.reads.append(record_id)
        # X5 compare 对缺失控件给「缺失」，整份结论为「未确认」。
        return readback_verdict({"结论": "未确认", "逐项核对": [
            {"字段名": "政策名称", "结果": "一致"}, {"字段名": "任务审批链接", "结果": "缺失"}]})
    archived(order)
    order.mingdao.readback = readback
    order.orders().run_once()
    assert order.mingdao.sent == [1]  # 下一条 0 发
    assert query(order.path, "SELECT item,status FROM trial_order") == [(1, "mismatch")]
    with journal_at(order.path) as j:
        assert j.write_halted() and j.halt_state()["halt_reason"] == "readback_mismatch"
    compared = [r for r in ledger(order) if r["event"] == "readback_compared"]
    assert compared[0]["结论"] == "写错" and compared[0]["counts"] == {"一致": 1, "不一致": 1}
    texts = notices(order)
    assert texts["order_mismatch"].endswith(ORDER_NOTICES["mismatch"]) and "建单已停写" in texts["order_cancel:2"]
    listener, transport = deliver(order)
    listener.maintain()
    alerts = [t for t in sent(transport) if ORDER_NOTICES["mismatch"] in t]
    assert alerts and all('<at user_id=\\"ou_config\\">' in t for t in alerts)


@pytest.mark.parametrize("effect", [TimeoutError, ConnectionResetError, "bad_record_id"])
def test_unknown_send_result_freezes_without_resend(order, effect):
    archived(order)
    if effect == "bad_record_id":
        order.mingdao.record_ids[1] = "not-a-record"
    else:
        order.mingdao.send_errors[1] = effect()
    order.orders().run_once()
    assert order.mingdao.sent == [1] and order.mingdao.reads == []
    assert query(order.path, "SELECT status,record_id FROM trial_order") == [("unknown", None)]
    assert query(order.path, "SELECT item FROM trial_order_slot") == [(1,)]
    assert events(order, 1) == ["create_reserved", "create_attempt", "create_unknown"]
    assert all(r["record_id"] is None for r in ledger(order))  # 不合格记录 ID 不进台账
    assert notices(order)["order_unknown:1"].endswith("建单结果未知，请人工到明道云核对。")
    with journal_at(order.path) as j:
        j.rotate("session2", "服务重启或关闭")
    assert order.orders().run_once() is False and order.mingdao.sent == [1]


def test_proven_not_sent_releases_slot_and_stops_batch(order):
    archived(order)
    order.mingdao.send_errors[1] = NotSent()
    order.orders().run_once()
    assert query(order.path, "SELECT status FROM trial_order") == [("aborted",)]
    assert query(order.path, "SELECT * FROM trial_order_slot") == []
    assert query(order.path, "SELECT status FROM trial_item_task ORDER BY item") == [("closed",), ("closed",)]
    assert events(order, 1)[-2:] == ["aborted_before_write", "approval_closed"]


@pytest.mark.parametrize("change", ["expired", "create_off", "halted", "slot"])
def test_handoff_rechecks_after_preconnect_and_writes_nothing(order, change):
    """P1-1：预建连期间时钟走过截止、开关关闭、停写位置位或本条持槽被动过 → 交接处拒绝，写出 0 次。"""
    archived(order)

    def preconnect(item):
        if change == "expired":
            order.now = NOW + 3 * 86400
        elif change == "create_off":
            order.switches["MAIL_TRIAL_CREATE_ENABLED"] = "false"
        else:
            with journal_at(order.path) as j, j.db:
                if change == "halted":
                    j.db.execute("INSERT INTO trial_halt VALUES(1,'守望者停写',0)")
                else:
                    j.db.execute("DELETE FROM trial_order_slot")
    order.mingdao.before_handoff = preconnect
    order.orders().run_once()
    assert order.mingdao.sent == [] and order.mingdao.reads == []
    assert query(order.path, "SELECT item,status FROM trial_order") == [(1, "aborted")]
    assert query(order.path, "SELECT * FROM trial_order_slot") == []
    assert query(order.path, "SELECT status FROM trial_item_task ORDER BY item") == [("closed",), ("closed",)]
    reason = {"expired": "审批到期", "create_off": "建单开关已关闭", "halted": "建单已停写", "slot": "本条发送登记已变化"}
    assert reason[change] + "，未建单" in notices(order)["order_cancel:1"]
    assert events(order, 1)[-2:] == ["aborted_before_write", "approval_closed"]


def test_disconnect_seen_right_after_registration_commit_sends_nothing(order):
    """G464 判序：登记提交后才核断线；此刻已断线即不能证明登记先于断线，本条与余项都不发。"""
    archived(order)
    runner = order.orders()
    admit = runner._admit

    def admit_then_drop(*args):
        code = admit(*args)
        order.connected = False
        return code
    runner._admit = admit_then_drop
    runner.run_once()
    assert order.mingdao.sent == []
    assert query(order.path, "SELECT item,status,disconnect FROM trial_order") == [(1, "aborted", 0)]
    texts = notices(order)
    assert "断线期间未发出，已转人工。" in texts["order_cancel:1"] and "断线期间未发出，已转人工。" in texts["order_cancel:2"]
    assert "order_disconnect" not in texts


def test_disconnect_during_preconnect_sends_the_registered_one(order):
    """登记提交后仍连通、预建连期间才断线：登记先于断线的那 1 张照发、单独计数，其余撤销。"""
    archived(order)
    order.mingdao.before_handoff = lambda item: setattr(order, "connected", False)
    order.orders().run_once()
    assert order.mingdao.sent == [1] and order.mingdao.reads == [record(1)]
    assert query(order.path, "SELECT item,status,disconnect FROM trial_order") == [(1, "verified", 1)]
    texts = notices(order)
    assert texts["order_disconnect"].endswith(ORDER_NOTICES["disconnect"])
    assert "断线期间未发出，已转人工。" in texts["order_cancel:2"]


def production_switches(env, values=SW):
    """生产 `Consumer.switches()` / `Consumer.switch_file()`：读真实私有开关文件，连接判断用替身断线开关。"""
    from qianshu.feishu.trial_consumer import Consumer, switch_source
    path = env.tmp / "trial-switches.env"
    if path.exists():
        path.unlink()
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    path.chmod(0o600)
    consumer = Consumer.__new__(Consumer)
    consumer.args = SimpleNamespace(trial_switches=path)
    consumer.connected = lambda: env.connected
    consumer.initial = switch_source(path)
    return consumer


def production_runner(env, consumer):
    return OrderRunner(env.path, switches=consumer.switches, switch_file=consumer.switch_file, backend=env.mingdao,
                       connected=lambda: env.connected, ledger_dir=env.tmp, clock=lambda: env.now,
                       pause=lambda seconds: None, readback_seconds=5)


def test_production_switches_connected_builds_both(order):
    """生产开关返回 Config（映射、非 dict）：连通时交接处照常放行，两条都建。"""
    archived(order)
    production_runner(order, production_switches(order)).run_once()
    assert order.mingdao.sent == [1, 2]
    assert query(order.path, "SELECT item,status,disconnect FROM trial_order ORDER BY item") == [
        (1, "verified", 0), (2, "verified", 0)]


@pytest.mark.parametrize("switch", ["on", "off"])
def test_production_switches_disconnect_during_preconnect(order, switch):
    """修复轮 2 第 3 条（G464 交接回退）：用生产开关函数复演——领槽提交后仍连通、预建连期间断线。
    开关仍开 → 登记先于断线的那 1 张照发（写出 1 次）、单独公告与计数，余项按「断线期间未发出」公告，
    不得写成「建单开关已关闭」；开关文件真关闭 → 按开关关闭处理，写出 0 次。"""
    archived(order)
    consumer = production_switches(order)
    assert consumer.switches()["MAIL_TRIAL_CREATE_ENABLED"] == "true"

    def preconnect(item):
        order.connected = False
        if switch == "off":
            production_switches(order, dict(SW, MAIL_TRIAL_CREATE_ENABLED="false"))
    order.mingdao.before_handoff = preconnect
    production_runner(order, consumer).run_once()
    texts = notices(order)
    if switch == "on":
        with pytest.raises(ValueError):
            consumer.switches()  # 断线后生产 switches() 本身拒绝：交接处不得因此拦下
        assert order.mingdao.sent == [1] and order.mingdao.reads == [record(1)]
        assert query(order.path, "SELECT item,status,disconnect FROM trial_order") == [(1, "verified", 1)]
        assert texts["order_disconnect"] == "登记 1 · 版本 1 · 条目 1 · " + ORDER_NOTICES["disconnect"]
        assert texts["order_cancel:2"] == "登记 1 · 版本 1 · 条目 2 · 断线期间未发出，已转人工。"
        assert not any("建单开关已关闭" in b for b in texts.values())
        with journal_at(order.path) as j:
            assert j.disconnect_orders() == 1
    else:
        assert order.mingdao.sent == []
        assert query(order.path, "SELECT item,status FROM trial_order") == [(1, "aborted")]
        assert "建单开关已关闭，未建单" in texts["order_cancel:1"]


@pytest.mark.parametrize("change", ["frozen", "bumped"])
def test_batch_frozen_or_bumped_during_preconnect_writes_nothing(order, change):
    """修复轮 2 第 2 条：领槽后、预建连期间收信线程冻结登记并升版（同 trial_state 改件冲突的真实写法）→
    交接处按与领槽同口径的同意判定拒绝，写出 0 次，按确证未发出公告未建单。"""
    archived(order)

    def preconnect(item):
        with journal_at(order.path) as j, j.db:
            if change == "frozen":
                j.db.execute("UPDATE registration SET status='frozen',version=version+1,reason='conflict' WHERE id=1")
            else:
                j.db.execute("UPDATE registration SET version=version+1 WHERE id=1")
    order.mingdao.before_handoff = preconnect
    order.orders().run_once()
    assert order.mingdao.sent == [] and order.mingdao.reads == []
    assert query(order.path, "SELECT item,status FROM trial_order") == [(1, "aborted")]
    assert query(order.path, "SELECT * FROM trial_order_slot") == []
    assert "同意已失效或卡片已关闭，未建单" in notices(order)["order_cancel:1"]
    assert events(order, 1)[-2:] == ["aborted_before_write", "approval_closed"]


def test_unverified_combination_stops_only_that_item(order):
    archived(order)
    order.mingdao.plan_errors[1] = UnverifiedCombination("该类型尚未经真实回读验证")
    order.orders().run_once()
    assert order.mingdao.sent == [2]
    assert query(order.path, "SELECT item,status FROM trial_item_task ORDER BY item") == [
        (1, "closed"), (2, "order_done")]
    assert "该类型尚未经真实回读验证" in notices(order)["order_cancel:1"]
    assert events(order, 1) == []


@pytest.mark.parametrize("url", [None, "https://feishu.cn/wiki/Other", "http://feishu.cn/docx/Created"])
def test_missing_archive_link_stops_batch(order, url):
    archived(order)
    with journal_at(order.path) as j, j.db:
        j.db.execute("UPDATE trial_archive SET url=?", (url,))
    order.orders().run_once()
    assert order.mingdao.sent == [] and order.mingdao.urls == []
    assert "取不到本批存档链接" in notices(order)["order_cancel:1,2"]
    assert query(order.path, "SELECT status FROM trial_card") == [("done",)]


def test_ledger_identity_formats_checked_before_write(order):
    archived(order)
    with journal_at(order.path) as j, j.db:
        for table in ("trial_card", "trial_item_task", "trial_task", "trial_archive", "trial_notification",
                      "trial_receipt", "trial_bound"):
            j.db.execute(f"UPDATE {table} SET card='NOT-A-UUID'")
    order.orders().run_once()
    assert order.mingdao.sent == [] and ledger(order) == []
    assert "台账标识格式不符" in notices(order)["order_cancel:1"]


def test_disconnect_in_flight_sends_that_one_reads_back_and_cancels_rest(order):
    archived(order)
    order.mingdao.on_send = lambda item: setattr(order, "connected", False)
    order.orders().run_once()
    assert order.mingdao.sent == [1] and order.mingdao.reads == [record(1)]
    assert query(order.path, "SELECT item,status,disconnect FROM trial_order") == [(1, "verified", 1)]
    texts = notices(order)
    assert texts["order_disconnect"] == "登记 1 · 版本 1 · 条目 1 · " + ORDER_NOTICES["disconnect"]
    assert "断线期间未发出，已转人工。" in texts["order_cancel:2"]
    with journal_at(order.path) as j:
        assert j.disconnect_orders() == 1


def test_disconnect_before_start_claims_nothing(order):
    archived(order)
    order.connected = False
    assert order.orders().run_once() is False and order.mingdao.sent == [] and ledger(order) == []


def test_card_closing_after_partial_build_reports_built_items(order):
    archived(order)
    order.mingdao.plan_errors[2] = RuntimeError
    with journal_at(order.path) as j, j.db:
        j.db.execute("INSERT INTO trial_item_task VALUES(1,1,3,'formal',(SELECT card FROM trial_card),'archive_done')")
    rewrite_snapshot(order.path, items=[1, 2, 3])
    # 只建第 1 条后卡片在途关闭：第 1 条报「已建单并回读一致」，不得报未建单。
    order.mingdao.on_send = None
    runner = order.orders()
    original = runner._one

    def one(job, item):
        if item == 3:
            with journal_at(order.path) as j:
                j.rotate("session2", "连接断开")
        return original(job, item)
    runner._one = one
    runner.run_once()
    bodies = [b for k, b in notices(order).items() if k.startswith("closed_1")]
    assert bodies and "已建单并回读一致" in bodies[0] and "未建单" not in bodies[0]


# ---------------------------------------------------------------- 晚投核对与移交

def test_old_unbuilt_notice_queued_then_new_card_built_is_rewritten(order):
    card = archived(order)
    with journal_at(order.path) as j:
        j.rotate("session2", "审批到期")  # 旧卡关闭，「未建单」公告排队
    pending = notices(order)
    assert any("未建单" in b for b in pending.values())
    with journal_at(order.path) as j, j.db:
        # 新卡（同目标表、同归一政策名）先建成。
        j.db.execute("""INSERT INTO trial_order(registration,version,item,card,status,name_key,target,sent)
                     VALUES(1,2,1,'new-card','verified',?,'sheet',0)""", (name_key("合成政策甲 "),))
    listener, transport = deliver(order)
    listener.maintain()
    texts = sent(transport)
    stale = [t for t in texts if SUPERSEDED in t]
    assert stale and not any("未建单，转人工" in t and SUPERSEDED not in t and "条目 1" in t for t in texts)
    assert query(order.path, "SELECT count(*) FROM trial_handoff WHERE item=1") == [(0,)]
    assert card


def test_delivered_unbuilt_notice_blocks_until_guardian_releases(order, capsys):
    from qianshu import __main__ as cli
    archived(order)
    # 第 1 条曾按「未建单」交人工（旧公告已投递），第 2 条未交。
    with journal_at(order.path) as j, j.db:
        j.db.execute("INSERT INTO trial_handoff VALUES(?,NULL,1,0,1,0,NULL,NULL)", (name_key(NAMES[0]),))
    order.orders().run_once()
    assert order.mingdao.sent == [2]
    assert "须守望者销清移交" in notices(order)["order_cancel:1"]
    state = str(order.path)
    fingerprint = name_fingerprint(name_key(NAMES[0]))
    before = query(order.path, "SELECT * FROM trial_handoff")
    # 登记 ID 或指纹不符：拒绝、非 0、零写入。
    assert cli.main(["--trial-release-handoff", "1", "0", "1", "0" * 16, "--state", state, "--reason", "人工已核"]) == 1
    assert cli.main(["--trial-release-handoff", "1", "0", "2", fingerprint, "--state", state, "--reason", "人工已核"]) == 1
    assert query(order.path, "SELECT * FROM trial_handoff") == before
    with pytest.raises(SystemExit):
        cli.main(["--trial-release-handoff", "1", "0", "1", fingerprint, "--state", state])
    capsys.readouterr()
    assert cli.main(["--trial-release-handoff", "1", "0", "1", fingerprint, "--state", state, "--reason", "人工已核"]) == 0
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert out[0]["before"]["open_handoffs"] >= 1 and out[1]["after"]["open_handoffs"] == 0
    assert NAMES[0] not in json.dumps(out, ensure_ascii=False)
    assert query(order.path, "SELECT count(*) FROM trial_handoff WHERE cleared IS NULL AND item=1 AND version=0") == [(0,)]
    assert query(order.path, "SELECT action FROM trial_guardian_log")[-1][0].startswith("release_handoff")


def test_release_also_frees_frozen_slot(order, capsys):
    from qianshu import __main__ as cli
    archived(order)
    order.mingdao.verdicts[1] = "slow"
    order.orders().run_once()
    listener, transport = deliver(order)
    listener.maintain()  # 「建单结果未知」与「未建单」投递，条目交人工
    assert query(order.path, "SELECT item FROM trial_order_slot") == [(1,)]
    fingerprint = name_fingerprint(name_key(NAMES[0]))
    assert query(order.path, "SELECT count(*) FROM trial_handoff WHERE item=1 AND cleared IS NULL")[0][0] >= 1
    assert cli.main(["--trial-release-handoff", "1", "1", "1", fingerprint, "--state", str(order.path),
                     "--reason", "守望者只读核对后人工处理完毕"]) == 0
    assert query(order.path, "SELECT * FROM trial_order_slot") == []
    assert query(order.path, "SELECT status FROM trial_order") == [("unknown",)]  # 不改冻结记录，只放行后续


def old_card(env, names, kind, body):
    """同名政策的旧卡（登记 1 · 版本 0，已关闭）与其排队中的公告。"""
    with journal_at(env.path) as j, j.db:
        row = dict(j.db.execute("SELECT * FROM trial_card").fetchone())
        snapshot = json.loads(row["snapshot"])
        for n, name in enumerate(names):
            snapshot["reviewed"][n]["policy_name"]["value"] = name
        snapshot["eligible"] = snapshot["items"] = list(range(1, len(names) + 1))
        packed = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        old = str(uuid5(NS, "old-card"))
        row.update(card=old, version=0, status="closed", snapshot=packed, fingerprint=sha256(packed.encode()).hexdigest())
        j.db.execute(f"INSERT INTO trial_card VALUES({','.join('?' * len(row))})", tuple(row.values()))
        j.db.execute("INSERT INTO trial_notification VALUES(?,?,?,?,'pending')", (old, kind, str(uuid5(NS, kind)), body))
    return old


@pytest.mark.parametrize("moment", ["receipt_returned", "receipt_lost", "between_checks"])
def test_notice_visible_before_receipt_new_card_sends_nothing(order, moment):
    """P1-3：旧公告已在群里可见、回执未返回时推进同名新卡 → 机器人 0 发；回执丢失时移交照样保留。"""
    from unittest.mock import AsyncMock
    archived(order)
    old_card(order, [NAMES[0]], "closed_1", "登记 1 · 条目 1 · 审批到期，未建单，转人工。待补项：无。")
    listener, transport = deliver(order)
    runner = order.orders()
    if moment == "between_checks":
        # 公告事务恰在建单的预查之后、领槽事务之前提交：领槽事务内裁决，仍 0 发。
        admit = runner._admit
        delivered = []

        def notice_commits_first(*args):
            if not delivered:
                delivered.append(listener.maintain())
            return admit(*args)
        runner._admit = notice_commits_first
        runner.run_once()
    else:
        async def visible(*args, **kwargs):
            runner.run_once()
            if moment == "receipt_lost":
                raise ConnectionResetError
            return SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="receipt"))
        transport.im.message.acreate = AsyncMock(side_effect=visible)
        if moment == "receipt_lost":
            with pytest.raises(Exception):
                listener.maintain()
        else:
            listener.maintain()
    assert 1 not in order.mingdao.sent
    assert query(order.path, "SELECT registration,version,item,cleared FROM trial_handoff") == [(1, 0, 1, None)]
    text = sent(transport)[0]
    assert "未建单" in text and "需守望者核对后解除" in text and SUPERSEDED not in text
    assert "须守望者销清移交" in notices(order)["order_cancel:1"]


def test_merged_notice_partly_taken_keeps_other_item_unbuilt_and_handed_off(order):
    """P1-4：甲、乙合批的旧公告，新卡只接管甲 → 甲标过时，乙仍有「未建单」提示与移交记录。"""
    archived(order)
    old_card(order, list(NAMES), "closed", "登记 1 · 条目 1、2 · 审批到期，未建单，转人工。待补项：无。")
    with journal_at(order.path) as j, j.db:
        j.db.execute("""INSERT INTO trial_order(registration,version,item,card,status,name_key,target,sent)
                     VALUES(1,1,1,(SELECT card FROM trial_card WHERE version=1),'verified',?,'sheet',0)""",
                     (name_key(NAMES[0]),))
    listener, transport = deliver(order)
    listener.maintain()
    text, = sent(transport)
    assert "条目 1、2 · 审批到期，未建单，转人工" in text and "条目 1 · " + SUPERSEDED in text
    assert "需守望者核对后解除" in text
    assert query(order.path, "SELECT item,name_key,cleared FROM trial_handoff") == [(2, name_key(NAMES[1]), None)]


def test_merged_notice_covers_manual_item_not_in_eligible(order):
    """修复轮 2 P1-1：乙因字段待核对标人工（items=[甲,乙]、eligible=[甲]），旧卡按真实关卡逻辑排队甲乙的「未建单」公告；
    新卡只接管甲并先建成 → 甲标过时，乙仍有「未建单」提示与移交记录。"""
    from qianshu.feishu.trial_approval import Journal
    archived(order)
    old = old_card(order, list(NAMES), "placeholder", "占位")
    with journal_at(order.path) as j, j.db:
        snapshot = json.loads(j.db.execute("SELECT snapshot FROM trial_card WHERE card=?", (old,)).fetchone()[0])
        snapshot.update(items=[1, 2], eligible=[1])
        packed = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        j.db.execute("UPDATE trial_card SET status='open', snapshot=?, fingerprint=? WHERE card=?",
                     (packed, sha256(packed.encode()).hexdigest(), old))
        j.db.execute("DELETE FROM trial_notification WHERE card=?", (old,))
        Journal(j.db).close(j.db.execute("SELECT * FROM trial_card WHERE card=?", (old,)).fetchone(), "审批到期")
        j.db.execute("""INSERT INTO trial_order(registration,version,item,card,status,name_key,target,sent)
                     VALUES(1,1,1,(SELECT card FROM trial_card WHERE version=1),'verified',?,'sheet',0)""",
                     (name_key(NAMES[0]),))
    assert "条目 1、2 · 审批到期，未建单" in notices(order)["closed"]
    listener, transport = deliver(order)
    listener.maintain()
    text, = sent(transport)
    assert "条目 1 · " + SUPERSEDED in text and "条目 2 · 未建单，已转人工。" in text and "需守望者核对后解除" in text
    assert query(order.path, "SELECT version,item,name_key,cleared FROM trial_handoff") == [
        (0, 2, name_key(NAMES[1]), None)]


def test_release_clears_only_the_confirmed_registration(order, capsys):
    """守望者销清只作用于双确认的那一条：同名其他登记的移交与在途槽不动，回显全部同名未销清移交。"""
    from qianshu import __main__ as cli
    archived(order)
    key = name_key(NAMES[0])
    with journal_at(order.path) as j, j.db:
        for version in (0, 2):
            j.db.execute("INSERT INTO trial_handoff VALUES(?,NULL,1,?,1,0,NULL,NULL)", (key, version))
        j.db.execute("""INSERT INTO trial_order(registration,version,item,card,status,name_key,target,sent)
                     VALUES(1,2,1,'other-card','unknown',?,'sheet',0)""", (key,))
        j.db.execute("INSERT INTO trial_order_slot VALUES(1,1,2,1,0)")
    assert cli.main(["--trial-release-handoff", "1", "0", "1", name_fingerprint(key), "--state", str(order.path),
                     "--reason", "人工已核"]) == 0
    before, after = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    other = {"registration": 1, "version": 2, "item": 1}
    assert before["before"]["open"] == [{"registration": 1, "version": 0, "item": 1}, other]
    assert after["after"]["open"] == [other] and after["after"]["slot"] == other
    assert query(order.path, "SELECT version FROM trial_handoff WHERE cleared IS NULL") == [(2,)]
    assert query(order.path, "SELECT registration,version,item FROM trial_order_slot") == [(1, 2, 1)]
    assert NAMES[0] not in json.dumps([before, after], ensure_ascii=False)


def test_reread_reapprove_closes_as_not_sent_and_asks_reapproval(order):
    """特批第 3 轮第 3 条：适配层在来源重读后核到开始日期需顺延或证明过期（NotSent reapprove）→ 确证未发出：释放在途槽，
    公告需重新审批、未建单，同批余项不发。"""
    archived(order)

    def reapprove(plan, handoff):
        raise NotSent("reapprove")
    order.mingdao.send = reapprove
    assert order.orders().run_once() is True
    assert query(order.path, "SELECT item,status FROM trial_order") == [(1, "aborted")]
    assert query(order.path, "SELECT * FROM trial_order_slot") == []
    text = notices(order)["order_cancel:1"]
    assert "需重新审批，未建单，已转人工。" in text and "order_cancel:2" in notices(order)
    assert events(order, 1)[-2:] == ["aborted_before_write", "approval_closed"]


# ---------------------------------------------------------------- 出卡即登记人工移交（特批第 3 轮第 1 条）

def issue(env, monkeypatch, eligible):
    """真实出卡（decision_pages → Journal.prepare 原样执行）：两条政策名为甲、乙，eligible 内的条目用已验证组合（拟建），
    其余用未验证组合（卡面「该类型尚未经真实回读验证」）。"""
    import qianshu.mail.trial_cards as cards
    real = cards.decision_pages

    def named(trial, row, text, details, parsed, policies, *rest):
        for n, policy in enumerate(policies, 1):
            policy.draft.policy_name.value = NAMES[n - 1]
            policy.draft.benefit_type.value = "组合0" if n in eligible else "组合1"
        return real(trial, row, text, details, parsed, policies, *rest)
    monkeypatch.setattr(cards, "decision_pages", named)


def archive_success(env, card):
    """存档成功（2c 已有专测）：只置存档链接与条目「存档完成、待建单」，省去真实存档的耗时。"""
    with journal_at(env.path) as j, j.db:
        j.db.execute("""INSERT INTO trial_archive(registration,version,card,status,purpose,url)
                     VALUES(1,1,?,'success','X8b','https://feishu.cn/docx/Created')""", (card,))
        j.db.execute("UPDATE trial_item_task SET status='archive_done' WHERE card=?", (card,))
        j.db.execute("UPDATE trial_task SET status='archive_done' WHERE card=?", (card,))


def handoffs(env):
    return query(env.path, "SELECT registration,version,item,name_key,target,cleared FROM trial_handoff ORDER BY rowid")


def occupied(env):
    """同名占位：按归一政策名读移交与在途槽（与守望者回显同一函数）。"""
    from qianshu.feishu.trial_order import handoff_state
    with journal_at(env.path, readonly=True) as j:
        return {name: (s["open_handoffs"], s["slot_held"])
                for name in NAMES for s in [handoff_state(j.db, name_key(name))]}


def card_face(env):
    pages = json.loads(query(env.path, "SELECT snapshot FROM trial_card WHERE version=1")[0][0])["pages"]
    return json.dumps(pages, ensure_ascii=False)


def test_card_time_handoff_all_eligible_built_without_handoff(order, monkeypatch):
    """全拟建：甲乙都拟建、都建成 → 无移交、无占位，建成公告覆盖甲乙各一次。"""
    issue(order, monkeypatch, eligible=(1, 2))
    card, _ = accept(order.path)
    assert handoffs(order) == [] and "已转人工" not in card_face(order)
    archive_success(order, card)
    assert order.orders().run_once() is True and order.mingdao.sent == [1, 2]
    listener, transport = deliver(order)
    listener.maintain()
    texts = sent(transport)
    assert len(texts) == 1 and "条目 1、2 · " + ORDER_NOTICES["done"] in texts[0] and "未建单" not in texts[0]
    assert handoffs(order) == [] and occupied(order) == {NAMES[0]: (0, False), NAMES[1]: (0, False)}


def test_card_time_handoff_partial_registers_manual_item_at_card_time(order, monkeypatch):
    """部分拟建（复核 P1-1）：甲拟建建成、乙待人工 → 乙出卡即有移交与占位、卡面「已转人工」；收口只公告甲建成，
    乙仍在移交清单，且不因投递再登记第二条移交。"""
    issue(order, monkeypatch, eligible=(1,))
    card, _ = accept(order.path)
    expected = [(1, 1, 2, name_key(NAMES[1]), None, None)]
    assert handoffs(order) == expected
    assert occupied(order) == {NAMES[0]: (0, False), NAMES[1]: (1, False)}
    face = card_face(order)
    assert "该类型尚未经真实回读验证，已转人工" in face and face.count("已转人工") == 1
    archive_success(order, card)
    assert order.orders().run_once() is True and order.mingdao.sent == [1]
    assert notices(order) == {"order_done": "登记 1 · 版本 1 · 条目 1 · " + ORDER_NOTICES["done"]}
    listener, transport = deliver(order)
    listener.maintain()
    text, = sent(transport)
    assert "条目 1 · " + ORDER_NOTICES["done"] in text and "条目 2" not in text
    assert query(order.path, "SELECT status FROM trial_card") == [("done",)]
    assert handoffs(order) == expected
    assert occupied(order) == {NAMES[0]: (0, False), NAMES[1]: (1, False)}
    listed = [r for r in recovery_rows(order) if r["kind"] == "order_handoff"]
    assert [(r["registration"], r["version"], r["item"], r["name_fp"]) for r in listed] == [
        (1, 1, 2, name_fingerprint(name_key(NAMES[1])))]


def test_card_time_handoff_not_registered_twice_when_batch_closes(order, monkeypatch):
    """部分拟建的卡在建单前关闭：合批「未建单」公告照旧投递，甲按投递端裁决登记移交；乙出卡时已登记，不重复登记。"""
    issue(order, monkeypatch, eligible=(1,))
    accept(order.path)
    with journal_at(order.path) as j:
        j.rotate("session2", "审批到期")
    assert "未建单" in notices(order)["closed"]
    listener, transport = deliver(order)
    listener.maintain()
    text, = sent(transport)
    assert "条目 1、2" in text and "需守望者核对后解除" in text
    assert handoffs(order) == [(1, 1, 2, name_key(NAMES[1]), None, None), (1, 1, 1, name_key(NAMES[0]), None, None)]


def recovery_rows(env):
    with journal_at(env.path, readonly=True) as j:
        return recovery(j.db)


def test_card_time_handoff_none_eligible_hands_off_everything(order, monkeypatch):
    """全不拟建：出卡即全部登记移交（卡片只读、整批转人工），建单 0、无收口公告。"""
    from test_trial_paths import setup_card
    issue(order, monkeypatch, eligible=())
    store, _, _, _ = setup_card(order.path)
    store.db.close()
    assert handoffs(order) == [(1, 1, 1, name_key(NAMES[0]), None, None), (1, 1, 2, name_key(NAMES[1]), None, None)]
    assert occupied(order) == {NAMES[0]: (1, False), NAMES[1]: (1, False)}
    face = card_face(order)
    assert face.count("该类型尚未经真实回读验证，已转人工") == 2 and '"button"' not in face
    assert query(order.path, "SELECT readonly FROM trial_card") == [(1,)]
    assert order.orders().run_once() is False and order.mingdao.sent == []
    assert notices(order) == {} and query(order.path, "SELECT * FROM trial_order") == []


def test_card_time_handoff_unnamed_item_registered_by_registration(order, monkeypatch):
    """缺可信政策名的未拟建条目：按「登记 × 版本 × 条目」登记（名键留空），不占用任何政策名。"""
    from test_trial_paths import setup_card
    store, _, _, _ = setup_card(order.path)  # 仓内夹具的草稿不带政策名，第 2 条为未验证组合
    store.db.close()
    assert handoffs(order) == [(1, 1, 2, "", None, None)]
    assert occupied(order) == {NAMES[0]: (0, False), NAMES[1]: (0, False)}


def test_card_time_handoff_blocks_same_name_new_version(order):
    """同名新版本（取舍 A）：旧版本出卡时乙未拟建、已登记未销清移交；同名新版本出卡且乙拟建 → 机器人对乙 0 发，
    乙的公告写「需守望者核对后解除」。"""
    from qianshu.feishu.trial_approval import Journal
    archived(order)  # 版本 1：甲乙都拟建、存档完成
    with journal_at(order.path) as j:
        snapshot = json.loads(j.db.execute("SELECT snapshot FROM trial_card").fetchone()[0])
        snapshot.update(items=[1, 2], eligible=[1])
        old = str(uuid5(NS, "card-v0"))
        Journal(j.db).prepare(old, 1, 0, "session", "区域甲", snapshot, False, NOW)
        with j.db:
            j.db.execute("UPDATE trial_card SET status='closed' WHERE card=?", (old,))
    assert handoffs(order) == [(1, 0, 2, name_key(NAMES[1]), None, None)]
    assert order.orders().run_once() is True and order.mingdao.sent == [1]
    assert "须守望者销清移交" in notices(order)["order_cancel:2"]
    listener, transport = deliver(order)
    listener.maintain()
    unbuilt = [t for t in sent(transport) if "条目 2" in t]
    assert len(unbuilt) == 1 and "未建单" in unbuilt[0] and "需守望者核对后解除" in unbuilt[0]
    assert [r[:3] for r in handoffs(order)] == [(1, 0, 2), (1, 1, 2)]
    assert 2 not in order.mingdao.sent


# ---------------------------------------------------------------- 同名已有机器人记录时的移交提示（复核 rv-2d4 P1）

def reissue(env, version, eligible=(), names=NAMES):
    """同一登记的新版本真实出卡（decision_pages → Journal.prepare 原样执行，不轮换会话、不动旧卡）：
    eligible 内的条目用已验证组合（拟建），其余用未验证组合。"""
    from dataclasses import fields
    from datetime import UTC, datetime
    from qianshu.draft import DraftField, OfferPolicyDraft
    from qianshu.mail.trial_cards import decision_pages
    from qianshu.mail.trial_state import Store
    from qianshu.parse import ParsedPolicyDraft
    from qianshu.parse.protocol import FieldResult, StatementStatus
    from qianshu.pipeline import _Policy
    from qianshu.validate import ValidationResult
    from test_trial_paths import switches
    store = Store(env.path)
    try:
        text = "\n".join(f"{f}：明细{n}" for n, f in enumerate(
            ("活动周期", "每个标签的用户数", "政策内容", "使用场景", "标签名称")))
        trial = SimpleNamespace(store=store, config={}, clock=lambda: datetime(2030, 1, 1, 1, tzinfo=UTC),
                                sensitive=())
        drafts, policies = [], []
        for n, name in enumerate(names, 1):
            draft = ParsedPolicyDraft(**{f.name: DraftField() for f in fields(OfferPolicyDraft)})
            draft.policy_type.value = "定向政策"
            draft.trigger_once.value = "否"
            draft.effective_multiplier.value = 1
            draft.maintain_mutual_exclusion_group.value = "是"
            draft.benefit_type.value = "组合0" if n in eligible else "组合1"
            draft.policy_name.value = name
            draft.source_span = (0, len(text))
            draft.field_results = {"country": FieldResult(SimpleNamespace(status=StatementStatus.CERTAIN), "区域甲",
                                                          True, needs_confirmation=False)}
            draft.to_dict = lambda: {"sample": True}
            drafts.append(draft)
            policies.append(_Policy(draft, (), {}, ValidationResult(())))
        return decision_pages(trial, {"id": 1, "version": version, "attachments": 0}, text, [], drafts, policies,
                              False, (switches(), {"verified_combinations": [["定向政策", "组合0"]]}, "ou_config"),
                              False)
    finally:
        store.db.close()


def face_lines(env, version):
    """卡面逐条文案：{条目号: 文案}。"""
    pages = json.loads(query(env.path, "SELECT snapshot FROM trial_card WHERE version=?", version)[0][0])["pages"]
    contents = [e["text"]["content"] for page in pages for e in page["elements"] if e.get("tag") == "div"]
    lines = {int(c.split("\n", 1)[0][3:]): c.split("\n", 1)[1] for c in contents if c.startswith("条目 ")}
    return lines, json.dumps(pages, ensure_ascii=False)


def recovery_kinds(env):
    """恢复清单里移交两类：可补建项（order_handoff）与只读核对（order_readonly_check），按 (版本, 条目) 列出。"""
    rows = recovery_rows(env)
    return {kind: [(r["version"], r["item"]) for r in rows if r["kind"] == kind]
            for kind in ("order_handoff", "order_readonly_check")}


@pytest.mark.parametrize("eligible", [(2,), ()], ids=["mixed", "none"])
def test_robot_sending_same_name_new_card_says_read_only(order, eligible):
    """复核 rv-2d4 P1 原样复现（G497）：旧甲已领槽在途（sending）时出同名新卡（甲缺字段未拟建；mixed 为乙拟建的混合卡，
    none 为全不拟建）→ 新卡甲写「只读核对，请勿重复建单」、不写「已转人工」，不进恢复清单可补建项（单列只读核对、可查），
    同名占位保留；旧甲照常发出并回读一致、不重发，新卡对甲 0 发。"""
    from qianshu.feishu.trial_order import ROBOT_HELD
    archived(order)  # 版本 1：甲乙都拟建、存档完成
    issued = []

    def new_card_while_sending(item):
        if item == 1 and not issued:
            assert query(order.path, "SELECT item,status FROM trial_order") == [(1, "sending")]
            issued.append(reissue(order, 2, eligible))
    order.mingdao.before_handoff = new_card_while_sending
    assert order.orders().run_once() is True and issued
    lines, face = face_lines(order, 2)
    assert ROBOT_HELD in lines[1] and "已转人工" not in lines[1]
    assert ("已转人工" in lines[2]) is (not eligible)
    assert query(order.path, "SELECT version,item,status FROM trial_order WHERE item=1") == [(1, 1, "verified")]
    kinds = recovery_kinds(order)
    assert kinds["order_readonly_check"] == [(2, 1)] and (2, 1) not in kinds["order_handoff"]
    assert kinds["order_handoff"] == ([] if eligible else [(2, 2)])
    assert occupied(order)[NAMES[0]] == (1, False)
    if eligible:
        assert order.mingdao.sent == [1, 2]  # 乙无同名移交，旧乙照常建成
    else:
        assert order.mingdao.sent == [1] and "须守望者销清移交" in notices(order)["order_cancel:2"]
    sent_before = list(order.mingdao.sent)
    order.orders().run_once()
    assert order.mingdao.sent == sent_before and order.mingdao.sent.count(1) == 1
    assert query(order.path, "SELECT count(*) FROM trial_order WHERE version=2") == [(0,)]
    assert 1 not in json.loads(query(order.path, "SELECT snapshot FROM trial_card WHERE version=2")[0][0])["eligible"]


@pytest.mark.parametrize("eligible", [(2,), ()], ids=["mixed", "none"])
def test_robot_verified_same_name_new_card_says_read_only(order, eligible):
    """复核 rv-2d4 P1 原样复现之二（G497）：旧甲乙已建成并回读一致（verified）后出同名新卡、甲未拟建 → 甲写
    「只读核对，请勿重复建单」，不进可补建项，同名占位保留；机器人不重发，新卡对甲 0 发。"""
    from qianshu.feishu.trial_order import ROBOT_HELD
    archived(order)
    assert order.orders().run_once() is True and order.mingdao.sent == [1, 2]
    reissue(order, 2, eligible)
    lines, face = face_lines(order, 2)
    assert ROBOT_HELD in lines[1] and "已转人工" not in lines[1]
    if not eligible:
        assert ROBOT_HELD in lines[2] and face.count(ROBOT_HELD) == 2 and '"button"' not in face
    kinds = recovery_kinds(order)
    assert kinds["order_handoff"] == [] and kinds["order_readonly_check"] == ([(2, 1)] if eligible else [(2, 1), (2, 2)])
    assert occupied(order)[NAMES[0]] == (1, False)
    assert order.orders().run_once() is False and order.mingdao.sent == [1, 2]
    assert query(order.path, "SELECT count(*) FROM trial_order WHERE version=2") == [(0,)]


@pytest.mark.parametrize("status,held", [("created", True), ("unknown", True), ("mismatch", True), ("aborted", False)])
def test_card_time_hint_follows_notice_adjudication(order, status, held):
    """出卡提示与公告投递同一裁决（robot_held / ACTIVE）：在途、已建、写错、结果未知都按「只读核对」；确证未发出不算。"""
    from qianshu.feishu.trial_order import ROBOT_HELD
    archived(order)
    with journal_at(order.path) as j, j.db:
        j.db.execute("""INSERT INTO trial_order(registration,version,item,card,status,name_key,target,sent)
                     VALUES(1,9,1,'other-card',?,?,'sheet',0)""", (status, name_key(NAMES[0])))
    reissue(order, 2)
    lines, _ = face_lines(order, 2)
    assert (ROBOT_HELD in lines[1]) is held and ("已转人工" in lines[1]) is not held
    assert "已转人工" in lines[2]
    assert [r[:4] for r in handoffs(order)] == [(1, 2, 1, name_key(NAMES[0])), (1, 2, 2, name_key(NAMES[1]))]
    kinds = recovery_kinds(order)
    assert kinds["order_readonly_check"] == ([(2, 1)] if held else [])
    assert kinds["order_handoff"] == ([] if held else [(2, 1)]) + [(2, 2)]


def final_summary(env, version):
    """只读卡末页「本批处理」一行的文案。"""
    pages = json.loads(query(env.path, "SELECT snapshot FROM trial_card WHERE version=?", version)[0][0])["pages"]
    contents = [e["text"]["content"] for e in pages[-1]["elements"] if e.get("tag") == "div"]
    return [c.split("\n", 1)[1] for c in contents if c.startswith("本批处理\n")]


@pytest.mark.parametrize("kind", ["plain", "mixed", "all_held", "supplement"])
def test_readonly_card_final_page_summarizes_by_item_kind(order, monkeypatch, kind):
    """复核 rv-2d5 P2：只读卡末页按条目实际类别汇总，与逐条提示一致——有「只读核对」条目时不写「整批转人工」；
    缺字段待补充的条目按 G496 定稿前暂用口径「已转人工，补充后由守望者核对解除」。逐条文案不变。"""
    from qianshu.draft import Uncertainty
    from qianshu.feishu.trial_order import ROBOT_HELD
    import qianshu.mail.trial_cards as cards
    archived(order)
    if kind == "all_held":
        assert order.orders().run_once() is True and order.mingdao.sent == [1, 2]
    elif kind == "mixed":
        with journal_at(order.path) as j, j.db:
            j.db.execute("""INSERT INTO trial_order(registration,version,item,card,status,name_key,target,sent)
                         VALUES(1,9,1,'other-card','verified',?,'sheet',0)""", (name_key(NAMES[0]),))
    elif kind == "supplement":
        real = cards.decision_pages

        def missing_field(trial, row, text, details, parsed, policies, *rest):
            policies[0].draft.benefit_type.value = "组合0"
            policies[0].draft.gift_days.uncertainty = Uncertainty(reason="合成：赠送天数待申请人补充")
            return real(trial, row, text, details, parsed, policies, *rest)
        monkeypatch.setattr(cards, "decision_pages", missing_field)
    reissue(order, 2)
    assert query(order.path, "SELECT readonly FROM trial_card WHERE version=2") == [(1,)]
    lines, face = face_lines(order, 2)
    expected = {
        "plain": "整批转人工：2 条已转人工",
        "mixed": "1 条已转人工；1 条只读核对，请勿重复建单",
        "all_held": "2 条只读核对，请勿重复建单",
        "supplement": "整批转人工：1 条已转人工；1 条已转人工，补充后由守望者核对解除",
    }[kind]
    assert final_summary(order, 2) == [expected]
    held = {"plain": set(), "mixed": {1}, "all_held": {1, 2}, "supplement": set()}[kind]
    assert {n for n in (1, 2) if ROBOT_HELD in lines[n]} == held
    assert all(("已转人工" in lines[n]) is (n not in held) for n in (1, 2))
    if kind == "supplement":
        assert lines[1] == "待人工核对字段，已转人工"
    assert ("整批转人工" in face) is not held


# ---------------------------------------------------------------- 守望者子命令与零装配

def test_guardian_halt_command_reads_back_and_requires_reason(order, capsys):
    from qianshu import __main__ as cli
    archived(order)
    state = str(order.path)
    assert cli.main(["--trial-halt", "status", "--state", state]) == 0
    assert json.loads(capsys.readouterr().out)["halted"] is False
    for argv in (["--trial-halt", "set", "--state", state], ["--trial-halt", "clear", "--state", state],
                 ["--trial-halt", "status", "--state", state, "--reason", "x"]):
        with pytest.raises(SystemExit):
            cli.main(argv)
    assert cli.main(["--trial-halt", "set", "--state", state, "--reason", "巡检停写"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["halted"] and shown["halt_reason"] == "巡检停写" and shown["halt_at"].endswith("+00:00")
    assert shown["last_change"]["action"] == "halt_set" and shown["last_change"]["reason"] == "巡检停写"
    assert cli.main(["--trial-halt", "clear", "--state", state, "--reason", "查清重开"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert not shown["halted"] and shown["last_change"]["reason"] == "查清重开"
    assert [r[0] for r in query(order.path, "SELECT action FROM trial_guardian_log")] == ["halt_set", "halt_clear"]


def test_guardian_init_archive_ledger_never_overwrites(tmp_path, capsys):
    from qianshu import __main__ as cli
    path = tmp_path / "archive-ledger.sqlite3"
    assert cli.main(["--trial-init-archive-ledger", str(path)]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["result"] == "已初始化" and "registration" in first["grants_columns"]
    with A._database(path) as db:
        db.execute("INSERT INTO grants(registration,digest,country,verified,release,expires,registry) "
                   "VALUES('r','d','c',1,'X8b',0,'g')")
    assert cli.main(["--trial-init-archive-ledger", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["result"] == "已存在，未覆盖"
    with A._database(path) as db:
        assert db.execute("SELECT count(*) FROM grants").fetchone()[0] == 1
    with pytest.raises(SystemExit):
        cli.main(["--trial-init-archive-ledger", str(tmp_path / "other.sqlite3")])


@pytest.mark.parametrize("argv", [["--trial-halt", "status"], ["--trial-init-archive-ledger", "archive-ledger.sqlite3"],
                                  ["--trial-release-handoff", "1", "1", "1", "0" * 16, "--reason", "x"]])
def test_guardian_commands_refuse_business_credentials(tmp_path, monkeypatch, argv):
    from qianshu import __main__ as cli
    monkeypatch.setenv("MINGDAO_PASSWORD", "placeholder")
    with pytest.raises(SystemExit) as stop:
        cli.main(argv + (["--state", str(tmp_path / "trial-050.sqlite3")] if "--trial-halt" in argv
                         or "--trial-release-handoff" in argv else []))
    assert stop.value.code == 2
    assert not list(tmp_path.iterdir())
    code = subprocess.run([sys.executable, "-m", "qianshu", *argv], cwd=tmp_path, capture_output=True, text=True,
                          env={**os.environ, "MINGDAO_PASSWORD": "placeholder"}, timeout=60)
    assert code.returncode != 0 and "业务凭据" in code.stderr and not list(tmp_path.iterdir())


def test_guardian_help_states_owner_consent():
    from qianshu import __main__ as cli
    import contextlib
    import io
    text = io.StringIO()
    with contextlib.redirect_stdout(text), pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "此命令只在产品负责人当场明确同意时由守望者执行" in text.getvalue().replace("\n", "").replace(" ", "")


def assembly_inputs(tmp_path, *, web_rules=True):
    """最小正式单建单配置与同目录正式单授权登记（按配置字节摘要绑定）；web_rules 为假时配置不含网页公式字段规则。"""
    from qianshu.feishu.trial_order import AUTHORIZATIONS_FILE, WEB_EVIDENCE
    from qianshu.mingdao.create_order import compact, digest
    login = tmp_path / "login"
    login.mkdir(mode=0o700)
    (login / "mingdao-state.json").write_text(json.dumps({"cookies": [{
        "name": "session", "domain": "www.mingdao.com", "path": "/", "expires": -1, "value": "placeholdervalue"}]}))
    (login / "mingdao-state.json").chmod(0o600)
    config = {"create": {"identifiers": {"worksheetId": "sheet"}, "session_cookie": "session"},
              "official": {"token_key": "TOKEN_KEY", "app_id_key": "APP_KEY"}, "template": {}, "fields": {},
              "fixed": {}, "readback_skip": [], "applicant_control": "c",
              "order_snapshot": {"verified_combinations": [], **({"web_rules": {}} if web_rules else {})}}
    path = login / "mingdao.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    entry = {**{key: {} for key in WEB_EVIDENCE}, "is_test": False, "config_sha256": digest(path.read_bytes()),
             "web_rules_sha256": digest(compact({}).encode())}
    (login / AUTHORIZATIONS_FILE).write_text(json.dumps({"formal": entry}))
    (login / AUTHORIZATIONS_FILE).chmod(0o600)
    good = dict(args=SimpleNamespace(mingdao_config=path),
                settings=SimpleNamespace(trial_credentials_complete=True, trial_login_dir=login,
                                         trial_credentials=(("TOKEN_KEY", "t"), ("APP_KEY", "a"))),
                state=tmp_path / "trial-050.sqlite3", switches=lambda: dict(SW), connected=lambda: True,
                sensitive=(), archive_runner=object())
    return good, path, config, login


def test_zero_assembly_without_confirmed_configuration(tmp_path):
    # 特批第 3 轮第 2 条：正式单配置须含 web_rules 才装配，「可装配」基线改用含 web_rules 的配置与正式单授权登记。
    good, path, config, login = assembly_inputs(tmp_path)
    assert isinstance(build_order_runner(**good), OrderRunner)
    variants = {
        "no_archive_runner": dict(archive_runner=None),
        "create_off": dict(switches=lambda: dict(SW, MAIL_TRIAL_CREATE_ENABLED="false")),
        "broken_switches": dict(switches=lambda: 1 / 0),
        "no_config": dict(args=SimpleNamespace(mingdao_config=None)),
        "credentials": dict(settings=SimpleNamespace(trial_credentials_complete=False, trial_login_dir=login,
                                                     trial_credentials=())),
        "no_token": dict(settings=SimpleNamespace(trial_credentials_complete=True, trial_login_dir=login,
                                                  trial_credentials=())),
        "no_login": dict(settings=SimpleNamespace(trial_credentials_complete=True, trial_login_dir=tmp_path,
                                                  trial_credentials=(("TOKEN_KEY", "t"), ("APP_KEY", "a")))),
    }
    for name, change in variants.items():
        assert build_order_runner(**{**good, **change}) is None, name
    config.pop("order_snapshot")
    path.write_text(json.dumps(config))
    assert build_order_runner(**good) is None


def test_formal_config_without_web_rules_is_not_wired(tmp_path, caplog):
    """非网页正式路径收窄（特批第 3 轮第 2 条）：正式单建单配置不含 order_snapshot.web_rules → 不装配（建单 0），
    日志写明原因；其余条件（开关、凭据、登录态、授权登记）都齐也一样。"""
    from qianshu.feishu.trial_order import NO_WEB_RULES
    good, _, _, _ = assembly_inputs(tmp_path, web_rules=False)
    with caplog.at_level("WARNING", logger="qianshu.feishu.trial_order"):
        assert build_order_runner(**good) is None
    assert NO_WEB_RULES in caplog.messages


def test_worker_runs_archive_then_orders_in_one_thread(order):
    import threading
    from qianshu.feishu.trial_archive import archive_worker
    archived(order)
    seen = []
    orders = order.orders()
    original = orders.run_once

    def run_once():
        seen.append(threading.current_thread().name)
        return original()
    orders.run_once = run_once
    import sqlite3

    def status():
        # 轮询读与工作线程的提交竞争登记库锁（忙等 0.25 秒）：只有「database is locked」下一轮再读，其他错误照常抛出。
        try:
            return query(order.path, "SELECT status FROM trial_card")
        except sqlite3.OperationalError as error:
            if "database is locked" not in str(error):
                raise
            return None
    with archive_worker(order.runner(), interval=0.01, join=5, orders=orders):
        end = time.monotonic() + 10
        while time.monotonic() < end and status() != [("done",)]:
            time.sleep(0.02)
    assert query(order.path, "SELECT status FROM trial_card") == [("done",)]  # 循环后断言终态
    assert order.mingdao.sent == [1, 2] and set(seen) == {"trial-archive"}


def test_formal_assembly_writes_archive_link(case):
    from qianshu.mingdao import create_order as O
    url = "https://feishu.cn/docx/Created"
    control = case.control["approval_url"]
    controls, summary, _ = O.assemble_controls(case.values, case.cells, case.config, case.relations.__getitem__,
                                               is_test=False, approval_url=url)
    cell = next(c for c in controls if c["controlId"] == control)
    assert cell["value"] == url
    assert any(row["来源"] == "本批存档" for row in summary)
    tested, _ = O.assemble(case.values, case.cells, case.config, case.relations.__getitem__, is_test=True)
    assert next(c for c in tested if c["controlId"] == control)["value"] != cell["value"]
    with pytest.raises(O.Stop, match="正式单不得照抄任务审批链接"):
        O.assemble(case.values, case.cells, case.config, case.relations.__getitem__, is_test=False)
    with pytest.raises(O.Stop):
        O.assemble_controls(case.values, case.cells, case.config, case.relations.__getitem__, is_test=True,
                            approval_url=url)
    with pytest.raises(O.Stop):
        O.assemble_controls(case.values, case.cells, case.config, case.relations.__getitem__, is_test=False,
                            approval_url="http://feishu.cn/docx/Created")


def test_readback_switch_parsing(tmp_path):
    from qianshu.config import ConfigError, trial_config
    path = tmp_path / "switches"
    path.write_text("")
    assert "MAIL_TRIAL_READBACK_SECONDS" not in trial_config(path, approval=True)
    path.write_text("MAIL_TRIAL_READBACK_SECONDS=300\n")
    assert trial_config(path, approval=True)["MAIL_TRIAL_READBACK_SECONDS"] == "300"
    for bad in ("0", "601", "abc", ""):
        path.write_text(f"MAIL_TRIAL_READBACK_SECONDS={bad}\n")
        with pytest.raises(ConfigError):
            trial_config(path, approval=True)


# ---------------------------------------------------------------- 真实子进程（只在本机跑）

CHILD = r'''
import json, os, select, socket, sys, time
from pathlib import Path
from uuid import UUID, uuid5
spec = json.loads(sys.argv[1])
from qianshu.feishu.trial_order import OrderRunner, Plan
channel = socket.socket(fileno=int(sys.argv[2]))
life = select.poll()
life.register(channel, select.POLLHUP | select.POLLERR | select.POLLRDHUP)
log = Path(spec["log"])

def note(text):
    with log.open("a") as out:
        out.write(text + "\n")

class Slow:
    def plan(self, snapshot, item, url, *, now, sent):
        name = snapshot["reviewed"][item - 1]["policy_name"]["value"]
        return Plan(name, "sheet", ["政策名称"], {"政策名称": "a"}, {"政策名称": "a"}, {"item": item})
    def send(self, plan, handoff):
        handoff()
        note(f"send {plan.payload['item']}")
        Path(spec["flag"]).write_text("in-flight")
        if spec["mode"] == "kill":
            time.sleep(30)
        time.sleep(0.8)
        return str(uuid5(UUID(int=7), f"record-{plan.payload['item']}"))
    def readback(self, plan, record_id, budget):
        note("read")
        return "一致", [{"字段名": "政策名称", "结果": "一致"}]

runner = OrderRunner(Path(spec["path"]), switches=lambda: spec["switches"], backend=Slow(),
                     connected=lambda: not life.poll(0), ledger_dir=Path(spec["ledger"]), clock=lambda: spec["now"])
runner.run_once()
'''


def child(env, mode):
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("沙箱内未跑：无 Unix 套接字")
    try:
        parent, endpoint = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    except PermissionError:
        pytest.skip("沙箱内未跑：SEQPACKET 被拒绝")
    log, flag = env.tmp / "child.log", env.tmp / "flag"
    spec = dict(mode=mode, path=str(env.path), ledger=str(env.tmp), log=str(log), flag=str(flag), switches=SW,
                now=env.now)
    proc = subprocess.Popen([sys.executable, "-I", "-c", CHILD, json.dumps(spec), str(endpoint.fileno())],
                            pass_fds=(endpoint.fileno(),), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=(env.tmp / "child.err").open("w"), process_group=0)
    endpoint.close()
    return parent, proc, log, flag


def wait_flag(flag, proc, seconds=30):
    end = time.monotonic() + seconds
    while time.monotonic() < end and not flag.exists():
        if proc.poll() is not None:
            break
        time.sleep(0.02)
    assert flag.exists(), "子进程未在时限内发出建单"


def test_real_child_sends_in_flight_order_after_parent_hangs_up(order):
    archived(order)
    parent, proc, log, flag = child(order, "disconnect")
    try:
        wait_flag(flag, proc)
        parent.close()  # 父进程观察到断线，关闭原连接
        assert proc.wait(30) == 0
    finally:
        parent.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert log.read_text().split("\n")[:2] == ["send 1", "read"]
    assert query(order.path, "SELECT item,status,disconnect FROM trial_order") == [(1, "verified", 1)]
    texts = notices(order)
    assert texts["order_disconnect"].endswith(ORDER_NOTICES["disconnect"])
    assert "断线期间未发出，已转人工。" in texts["order_cancel:2"]


def test_real_child_killed_mid_send_freezes_as_unknown_and_never_resends(order):
    archived(order)
    parent, proc, log, flag = child(order, "kill")
    try:
        wait_flag(flag, proc)
        parent.close()
        time.sleep(0.2)
        os.killpg(proc.pid, 9)  # 2b：父进程观察到断线后强杀审批子进程
        proc.wait(30)
    finally:
        parent.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert log.read_text().split() == ["send", "1"]
    assert query(order.path, "SELECT status FROM trial_order") == [("sending",)]
    with journal_at(order.path) as j:
        j.rotate("session2", "服务重启或关闭")
    assert order.orders().run_once() is False
    assert order.mingdao.sent == [] and log.read_text().split() == ["send", "1"]
    assert query(order.path, "SELECT status,reason FROM trial_order") == [("unknown", "restart")]
    assert query(order.path, "SELECT item FROM trial_order_slot") == [(1,)]
    assert notices(order)["order_unknown:1"].endswith(ORDER_NOTICES["unknown"])
    assert events(order, 1)[-1] == "create_unknown"


# ---------------------------------------------------------------- 同名归一查重（守望者 G506，agy P1-2）

BASE = "合成 政策（A）"
VARIANTS = {
    "half_width_brackets": "合成 政策(A)",
    "full_width_letter_and_space": "合成　政策（Ａ）",
    "multi_space": "合成   政策（A）",
    "edge_space": "  合成 政策（A） ",
}


def prior_order(env, status, name=BASE):
    """其他卡（登记 1 · 版本 9）的机器人建单记录，名键按同一归一函数存。不占在途槽：只测本地查重本身
    （真实在途时在途槽先挡住其他批次）。"""
    with journal_at(env.path) as j, j.db:
        j.db.execute("""INSERT INTO trial_order(registration,version,item,card,status,name_key,target,sent)
                     VALUES(1,9,1,'other-card',?,?,'sheet',0)""", (status, name_key(name)))


def rename_first(env, name):
    """本卡第 1 条的政策名改为 name（申请人退回后重发、顺手改了标点或空格）。"""
    reviewed = json.loads(query(env.path, "SELECT snapshot FROM trial_card")[0][0])["reviewed"]
    reviewed[0]["policy_name"]["value"] = name
    rewrite_snapshot(env.path, reviewed=reviewed)


@pytest.mark.parametrize("status", ["sending", "unknown", "verified"], ids=["in_flight", "unknown", "built"])
@pytest.mark.parametrize("variant", list(VARIANTS.values()), ids=list(VARIANTS))
def test_same_normalized_name_with_robot_record_sends_nothing_and_says_read_only(order, variant, status):
    """G506：同一归一政策名已有其他卡的机器人在途 / 结果未知 / 已建成记录（无移交），新卡同名变体（全半角括号、全角字母
    与空格、多空格、首尾空格）获同意 → 前置检查即停：机器人 0 发、不调用明道云组装与查重，公告写「只读核对，请勿重复建单」，
    登记只读核对类移交；投递照原文、不改写过时、不补登；再轮询也不发。"""
    from qianshu.feishu.trial_order import READONLY_CHECK, ROBOT_HELD
    archived(order)
    rename_first(order, variant)
    prior_order(order, status)
    assert variant != BASE and name_key(variant) == name_key(BASE)
    assert order.orders().run_once() is True
    assert order.mingdao.sent == [2] and len(order.mingdao.urls) == 1  # 甲未进 plan：不碰明道云
    assert notices(order)["order_cancel:1"] == "登记 1 · 版本 1 · 条目 1 · " + ROBOT_HELD + "。"
    assert query(order.path, "SELECT count(*) FROM trial_order WHERE version=1 AND item=1") == [(0,)]
    assert query(order.path, "SELECT item,status FROM trial_item_task ORDER BY item") == [
        (1, "closed"), (2, "order_done")]
    assert events(order, 1) == []
    expected = [(1, 1, 1, name_key(BASE), READONLY_CHECK, None)]
    assert query(order.path, "SELECT registration,version,item,name_key,reason,cleared FROM trial_handoff") == expected
    kinds = recovery_kinds(order)
    assert kinds == {"order_handoff": [], "order_readonly_check": [(1, 1)]}
    listener, transport = deliver(order)
    listener.maintain()
    texts = sent(transport)
    assert any(ROBOT_HELD in t and "条目 1" in t for t in texts)
    assert not any(SUPERSEDED in t or "未建单" in t for t in texts)
    assert query(order.path, "SELECT registration,version,item,name_key,reason,cleared FROM trial_handoff") == expected
    order.orders().run_once()
    assert order.mingdao.sent == [2]


@pytest.mark.parametrize("status", ["sending", "unknown", "verified"], ids=["in_flight", "unknown", "built"])
def test_same_name_record_landing_before_slot_is_caught_in_slot_transaction(order, status):
    """G506：同名机器人记录恰在前置检查之后、领槽事务之前提交 → 领槽事务里同一判定（robot_held）裁决本条不建，
    按确证未发出收口（台账 aborted_before_write / approval_closed，不占槽、无发送记录），公告只读核对、移交只登一条。"""
    from qianshu.feishu.trial_order import READONLY_CHECK, ROBOT_HELD
    archived(order)
    rename_first(order, VARIANTS["half_width_brackets"])
    runner = order.orders()
    admit = runner._admit

    def record_lands_first(job, item, plan, key):
        if item == 1:
            prior_order(order, status)
        return admit(job, item, plan, key)
    runner._admit = record_lands_first
    assert runner.run_once() is True
    assert order.mingdao.sent == [2]
    assert notices(order)["order_cancel:1"].endswith(ROBOT_HELD + "。")
    assert events(order, 1) == ["create_reserved", "aborted_before_write", "approval_closed"]
    assert ledger(order)[2]["reason"] == "held"
    assert query(order.path, "SELECT count(*) FROM trial_order WHERE version=1 AND item=1") == [(0,)]
    assert query(order.path, "SELECT * FROM trial_order_slot") == []
    assert query(order.path, "SELECT registration,version,item,name_key,reason,cleared FROM trial_handoff") == [
        (1, 1, 1, name_key(BASE), READONLY_CHECK, None)]


@pytest.mark.parametrize("cause", ["no_approver", "no_contact", "x8a"])
def test_readonly_card_hands_off_would_be_eligible_items_too(order, monkeypatch, cause):
    """锁住 agy P1-1 不成立（G506 第 4 条）：缺审批人、缺配置负责人、存档用途 X8a 等使只读卡（readonly=True）的情形，
    原本可拟建的条目也全部登记移交——出卡时 readonly 即清空 eligible（src/qianshu/mail/trial_cards.py:133-134），
    card_handoffs 覆盖全部条目；同名已有机器人记录的条目写「只读核对」、移交标只读核对类。"""
    import qianshu.mail.trial_cards as cards
    from qianshu.feishu.trial_order import READONLY_CHECK, ROBOT_HELD, card_handoffs
    archived(order)
    prior_order(order, "verified", NAMES[0])
    real = cards.decision_pages

    def readonly(trial, row, text, details, parsed, policies, full_review, setup, blocked):
        settings, snapshot, contact = setup
        settings = dict(settings)
        if cause == "no_approver":
            settings.pop("MAIL_TRIAL_APPROVER_OPEN_IDS")
        elif cause == "x8a":
            settings["MAIL_TRIAL_ARCHIVE_PURPOSE"] = "X8a"
        else:
            contact = None
        return real(trial, row, text, details, parsed, policies, full_review, (settings, snapshot, contact), blocked)
    monkeypatch.setattr(cards, "decision_pages", readonly)
    reissue(order, 2, eligible=(1, 2))
    assert query(order.path, "SELECT readonly FROM trial_card WHERE version=2") == [(1,)]
    snapshot = json.loads(query(order.path, "SELECT snapshot FROM trial_card WHERE version=2")[0][0])
    assert snapshot["items"] == [1, 2] and snapshot["eligible"] == []
    assert card_handoffs(snapshot) == [(1, name_key(NAMES[0])), (2, name_key(NAMES[1]))]
    assert query(order.path, "SELECT item,name_key,reason,cleared FROM trial_handoff WHERE version=2 ORDER BY item") == [
        (1, name_key(NAMES[0]), READONLY_CHECK, None), (2, name_key(NAMES[1]), None, None)]
    lines, face = face_lines(order, 2)
    assert ROBOT_HELD in lines[1] and "已转人工" not in lines[1] and lines[2].endswith("已转人工")
    assert "本次拟建" not in face and '"button"' not in face


# ---------------------------------------------------------------- 同卡同名合为一组交人工（守望者 G509，取代 G507 / G508）

PAIR = ("合成政策（A）", "合成政策(A)")


def group_card(env, monkeypatch, names, *, eligible, extra=None, approve=False, bind=True):
    """真实出卡（decision_pages → Journal.prepare 原样执行）：条目政策名为 names，eligible 内的条目用已验证组合（本可拟建），
    其余用未验证组合；extra 为 {字段: 逐条取值}。bind 时送达并绑定（卡片 open），approve 时再核验并同意。返回卡片 ID。"""
    import qianshu.mail.trial_cards as cards
    from test_trial_paths import setup_card
    real = cards.decision_pages

    def named(trial, row, text, details, parsed, policies, *rest):
        for n, policy in enumerate(policies, 1):
            policy.draft.policy_name.value = names[n - 1]
            policy.draft.benefit_type.value = "组合0" if n in eligible else "组合1"
            for field, values in (extra or {}).items():
                getattr(policy.draft, field).value = values[n - 1]
        return real(trial, row, text, details, parsed, policies, *rest)
    monkeypatch.setattr(cards, "decision_pages", named)
    store, _, card, pages = setup_card(env.path, countries=("区域甲",) * len(names))
    store.db.close()
    if bind:
        with journal_at(env.path) as j:
            for n in range(1, len(pages) + 1):
                j.db.execute("INSERT INTO card_intent VALUES(?,1,1,?,'sent','')", (f"r{n}", n))
                j.received(f"r{n}", "message" if n == len(pages) else f"page{n}")
            j.bind(card, NOW + 1)
            j.db.commit()
            for event, action in (("e1", "verify"), ("e2", "approve"))[:2 if approve else 1]:
                text = j.action("session", card, 1, 1, "message", action, event, "ou_actor", NOW + 2,
                                lambda: NOW + 2, lambda: True, True, SW, archive=True)
            assert ("将建存档" if approve else "已核验") in text
            j.db.commit()
    return card


def group_rows(env):
    return [r for r in recovery_rows(env) if r["kind"] == "order_group_handoff"]


def open_handoffs(env):
    return query(env.path, "SELECT version,item,name_key,reason FROM trial_handoff WHERE cleared IS NULL ORDER BY rowid")


def group_line(*items):
    from qianshu.feishu.trial_order import group_text
    return group_text(items) + "。"


@pytest.mark.parametrize("names", [("合成政策甲", "合成政策甲"), PAIR], ids=["exact", "width_only"])
def test_same_name_twice_in_one_card_hands_whole_group_to_manual(order, monkeypatch, names):
    """G509 同名同内容（复核 rv-2dH P1-1 之一）：正文与附件把同一条政策各列一次、两条都本可拟建 → 整组机器人 0 发：
    两条都不进拟建（卡片按只读出），卡面两条都写组文案；出卡同一事务只登记一项组移交（同组首条、按组名键占用）；
    恢复清单只有 1 项组移交、列 2 个成员，差异摘要只写「相同 / 不同」；不进可补建或只读核对单项。"""
    from qianshu.feishu.trial_order import SAME_NAME_GROUP_MARK
    group_card(order, monkeypatch, names, eligible=(1, 2), bind=False)
    snapshot = json.loads(query(order.path, "SELECT snapshot FROM trial_card")[0][0])
    assert snapshot["items"] == [1, 2] and snapshot["eligible"] == [] and "same_card" not in snapshot
    assert query(order.path, "SELECT readonly FROM trial_card") == [(1,)]
    key = name_key(names[0])
    assert open_handoffs(order) == [(1, 1, key, SAME_NAME_GROUP_MARK)]
    lines, face = face_lines(order, 1)
    assert lines == {1: group_line(1, 2)[:-1], 2: group_line(1, 2)[:-1]}
    assert "本次拟建" not in face and "已转人工" not in face and '"button"' not in face
    assert final_summary(order, 1) == ["整批转人工：2 条同名合为 1 项交人工核对"]
    assert recovery_kinds(order) == {"order_handoff": [], "order_readonly_check": []}
    group, = group_rows(order)
    assert (group["registration"], group["version"], group["item"], group["name_fp"]) == (1, 1, 1, name_fingerprint(key))
    assert [m["item"] for m in group["members"]] == [1, 2] and group["robot_record"] is False
    assert group["diff"]["policy_name"] == ("相同" if names[0] == names[1] else "不同")
    assert set(group["diff"].values()) <= {"相同", "不同"}
    assert names[0] not in json.dumps(group_rows(order), ensure_ascii=False)
    assert order.orders().run_once() is False and order.mingdao.sent == []
    assert query(order.path, "SELECT * FROM trial_item_task") == []


def test_same_name_different_content_both_members_reach_manual(order, monkeypatch):
    """G509 同名异内容（复核 rv-2dH P1-1 原样场景）：同名两条标签不同、赠送天数不同 → 两条都在同一项组移交里（成员齐全），
    差异摘要把标签与赠送天数标「不同」、政策名标「相同」，摘要与恢复清单都不含任何取值。"""
    tags = (["合成标签甲"], ["合成标签乙"])
    group_card(order, monkeypatch, ("合成政策甲", "合成政策甲"), eligible=(1, 2), bind=False,
               extra={"user_tags": tags, "gift_days": (10, 20)})
    group, = group_rows(order)
    assert [m["item"] for m in group["members"]] == [1, 2]
    assert group["diff"]["user_tags"] == "不同" and group["diff"]["gift_days"] == "不同"
    assert group["diff"]["policy_name"] == "相同" and group["diff"]["policy_type"] == "相同"
    shown = json.dumps(recovery_rows(order), ensure_ascii=False)
    assert not any(value in shown for value in ("合成标签甲", "合成标签乙", "合成政策甲"))
    assert '"gift_days": "不同"' in shown and ": 10" not in shown and ": 20" not in shown
    assert recovery_kinds(order) == {"order_handoff": [], "order_readonly_check": []}
    assert order.orders().run_once() is False and order.mingdao.sent == []


@pytest.mark.parametrize("ending", ["expired", "rejected", "disconnected"])
def test_same_name_group_card_close_hands_off_one_group(order, monkeypatch, ending):
    """G509 关卡结局：「合成政策（A）」「合成政策(A)」与异名第 3 条（拟建）同卡，到截止、驳回、断线关卡 → 机器人 0 发；
    整批公告只把第 3 条写「未建单，转人工」，同名两条写组文案；恢复清单只有 1 项组移交（2 成员）加第 3 条单项。"""
    from qianshu.feishu.trial_order import HANDOFF_NOTE, SAME_NAME_GROUP_MARK
    card = group_card(order, monkeypatch, (*PAIR, NAMES[1]), eligible=(1, 2, 3), approve=ending != "rejected")
    assert json.loads(query(order.path, "SELECT snapshot FROM trial_card")[0][0])["eligible"] == [3]
    assert query(order.path, "SELECT readonly FROM trial_card") == [(0,)]
    with journal_at(order.path) as j:
        if ending == "expired":
            j.scan(query(order.path, "SELECT expires FROM trial_card")[0][0])
            reason = "审批到期"
        elif ending == "rejected":
            assert j.action("session", card, 1, 1, "message", "reject", "e2", "ou_actor", NOW + 3, lambda: NOW + 3,
                            lambda: True, True, SW, reason="合成驳回原因") == "已驳回"
            j.db.commit()
            reason = "驳回：合成驳回原因"
        else:
            j.rotate("session2", "连接断开")
            reason = "连接断开"
    assert query(order.path, "SELECT status FROM trial_card") == [("closed",)]
    listener, transport = deliver(order)
    listener.maintain()
    text, = [json.loads(t)["text"] for t in sent(transport)]
    assert f"登记 1 · 条目 3 · {reason}，未建单，转人工。{group_line(1, 2)}待补项" in text
    assert text.count("未建单") == 1 and text.count("合为一项交人工核对") == 1 and text.endswith(HANDOFF_NOTE)
    assert order.orders().run_once() is False and order.mingdao.sent == []
    assert open_handoffs(order) == [(1, 1, name_key(PAIR[0]), SAME_NAME_GROUP_MARK), (1, 3, name_key(NAMES[1]), None)]
    assert recovery_kinds(order) == {"order_handoff": [(1, 3)], "order_readonly_check": []}
    assert [[m["item"] for m in g["members"]] for g in group_rows(order)] == [[1, 2]]


def test_mixed_card_same_name_group_includes_handed_off_member(order, monkeypatch):
    """G509 混合卡：第 1 条未拟建（未验证组合），第 2 条与之同名、本可拟建，第 3 条异名拟建 → 第 1、2 条合为一组：卡面都写
    组文案、都不进拟建，出卡只登记一项组移交；第 3 条照常拟建。"""
    from qianshu.feishu.trial_order import SAME_NAME_GROUP_MARK
    group_card(order, monkeypatch, (*PAIR, NAMES[1]), eligible=(2, 3))
    snapshot = json.loads(query(order.path, "SELECT snapshot FROM trial_card")[0][0])
    assert snapshot["eligible"] == [3] and query(order.path, "SELECT readonly FROM trial_card") == [(0,)]
    lines, face = face_lines(order, 1)
    assert lines[1] == lines[2] == group_line(1, 2)[:-1] and lines[3] == "本次拟建"
    assert "已转人工" not in face and '"button"' in face
    assert open_handoffs(order) == [(1, 1, name_key(PAIR[0]), SAME_NAME_GROUP_MARK)]
    assert recovery_kinds(order) == {"order_handoff": [], "order_readonly_check": []}
    assert [[m["item"] for m in g["members"]] for g in group_rows(order)] == [[1, 2]]


def test_archive_failure_whole_batch_notice_hands_off_one_group(order, monkeypatch):
    """G509 存档失败整批：同名组卡（第 1、2 条同名，第 3 条拟建）获同意后存档关卡失败 → 整批公告补写组文案一次，
    移交仍只有一项组移交加第 3 条单项，机器人 0 发。"""
    from qianshu.feishu.trial_approval import ARCHIVE_NOTICES
    from qianshu.feishu.trial_order import SAME_NAME_GROUP_MARK
    group_card(order, monkeypatch, (*PAIR, NAMES[1]), eligible=(1, 2, 3), approve=True)
    order.switches.pop("MAIL_TRIAL_ARCHIVE_PURPOSE")
    assert order.runner().run_once() is False
    assert notices(order)["archive_failed"].endswith(ARCHIVE_NOTICES["unconfirmed"])
    listener, transport = deliver(order)
    listener.maintain()
    text, = [json.loads(t)["text"] for t in sent(transport)]
    assert ARCHIVE_NOTICES["unconfirmed"] + "\n" + group_line(1, 2) in text and text.count("合为一项交人工核对") == 1
    assert open_handoffs(order) == [(1, 1, name_key(PAIR[0]), SAME_NAME_GROUP_MARK), (1, 3, name_key(NAMES[1]), None)]
    assert [[m["item"] for m in g["members"]] for g in group_rows(order)] == [[1, 2]]
    assert order.orders().run_once() is False and order.mingdao.sent == []


def release_argv(env, members, reason="合成：产品负责人当场同意，人工已逐条核对"):
    argv = ["--trial-release-group", "1", "1", "--state", str(env.path), "--reason", reason]
    for item, fp, disposition in members:
        argv += ["--member", str(item), fp, disposition]
    return argv


def test_group_release_needs_every_member_and_matching_fingerprints(order, monkeypatch, capsys):
    """G509 / G487 组销清：缺一个成员去向、成员指纹不符、去向非法 → 拒绝、非 0、零写入；单条销清对组内条目拒绝；
    齐全 → 成功，执行前回显、执行后读回（不含政策名原文），留痕记各成员去向，恢复清单不再有该组。"""
    from qianshu import __main__ as cli
    group_card(order, monkeypatch, PAIR, eligible=(1, 2), bind=False)
    group, = group_rows(order)
    fps = {m["item"]: m["display_fp"] for m in group["members"]}
    assert fps[1] != fps[2]  # 原名仅全半角不同：展示名指纹逐条不同
    before = query(order.path, "SELECT * FROM trial_handoff")

    def unchanged():
        return query(order.path, "SELECT * FROM trial_handoff") == before and query(
            order.path, "SELECT count(*) FROM trial_guardian_log") == [(0,)]
    assert cli.main(release_argv(order, [(1, fps[1], "built")])) == 1 and unchanged()
    assert cli.main(release_argv(order, [(1, fps[1], "built"), (2, fps[1], "merged")])) == 1 and unchanged()
    assert cli.main(release_argv(order, [(1, fps[1], "built"), (2, fps[2], "maybe")])) == 1 and unchanged()
    assert cli.main(release_argv(order, [(1, fps[1], "built"), (2, fps[2], "merged"), (3, fps[1], "built")])) == 1
    assert unchanged()  # 成员集合多出组外条目同样拒绝
    assert cli.main(["--trial-release-handoff", "1", "1", "1", group["name_fp"], "--state", str(order.path),
                     "--reason", "合成"]) == 1 and unchanged()
    for argv in (release_argv(order, [(1, fps[1], "built"), (2, fps[2], "merged")], reason=" "),
                 ["--member", "1", fps[1], "built", "--trial-halt", "status", "--state", str(order.path)]):
        with pytest.raises(SystemExit):
            cli.main(argv)
    assert unchanged()
    capsys.readouterr()
    assert cli.main(release_argv(order, [(2, fps[2], "merged"), (1, fps[1], "built")])) == 0
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert out[0]["before"]["groups"][0]["open_handoffs"] == 1 and out[1]["after"]["groups"][0]["open_handoffs"] == 0
    assert [m["item"] for m in out[1]["after"]["groups"][0]["members"]] == [1, 2]
    assert not any(name in json.dumps(out, ensure_ascii=False) for name in PAIR)
    log, = query(order.path, "SELECT action FROM trial_guardian_log")
    assert log[0] == f"release_group 1-1 {group['name_fp']} 1:built,2:merged"
    assert group_rows(order) == [] and query(order.path, "SELECT count(*) FROM trial_handoff WHERE cleared IS NULL") == [
        (0,)]
    assert cli.main(release_argv(order, [(1, fps[1], "built"), (2, fps[2], "merged")])) == 1  # 已销清：无可销项


def test_guardian_help_states_owner_consent_for_group_release():
    """组销清帮助文本写明「只在产品负责人当场明确同意时由守望者执行」。"""
    from qianshu import __main__ as cli
    import contextlib
    import io
    text = io.StringIO()
    with contextlib.redirect_stdout(text), pytest.raises(SystemExit):
        cli.main(["--help"])
    flat = text.getvalue().replace("\n", "").replace(" ", "")
    part = flat[flat.index("--trial-release-groupREGISTRATIONVERSION守望者用"):]
    assert "此命令只在产品负责人当场明确同意时由守望者执行" in part[:200]


def test_old_snapshot_with_both_group_members_eligible_sends_nothing(order):
    """复核 rv-2dH P1-2 之一：旧快照（无分组字段时出卡）同名两条都列为拟建、存档完成 → 建单执行按快照可信政策名重算分组，
    整组撤下：机器人 0 发、不碰明道云，公告组文案，一项组移交。"""
    from qianshu.feishu.trial_order import SAME_NAME_GROUP_MARK
    archived(order, PAIR)
    assert json.loads(query(order.path, "SELECT snapshot FROM trial_card")[0][0])["eligible"] == [1, 2]
    assert order.orders().run_once() is True
    assert order.mingdao.sent == [] and order.mingdao.urls == []
    assert notices(order)["order_cancel:1,2"] == "登记 1 · 版本 1 · 条目 1、2 · " + group_line(1, 2)
    assert open_handoffs(order) == [(1, 1, name_key(PAIR[0]), SAME_NAME_GROUP_MARK)]
    listener, transport = deliver(order)
    listener.maintain()
    text, = [json.loads(t)["text"] for t in sent(transport)]
    assert text.count("合为一项交人工核对") == 1 and "需守望者核对后解除" in text
    assert open_handoffs(order) == [(1, 1, name_key(PAIR[0]), SAME_NAME_GROUP_MARK)]
    assert order.orders().run_once() is False and order.mingdao.sent == []


@pytest.mark.parametrize("path", ["close", "queued"])
def test_old_snapshot_three_same_names_rebuilds_one_group(order, monkeypatch, path):
    """复核 rv-2dH P1-2 之二：旧快照三条同名、旧分组字段只覆盖 [[3, 2]]、第 3 条已有旧版只读核对移交 → 按可信政策名重算为
    一组（1、2、3）：close 为新版关卡，queued 为旧版排队的整批「未建单」公告投递；两种都只给一项组移交（恢复清单合并旧行），
    公告写「本卡第 1、2、3 条同名」，按组销清须列全 3 个成员。"""
    from qianshu import __main__ as cli
    from qianshu.feishu.trial_order import READONLY_CHECK, SAME_NAME_GROUP_MARK
    group_card(order, monkeypatch, ("甲1", "乙2", "丙3"), eligible=(1, 2, 3))
    reviewed = json.loads(query(order.path, "SELECT snapshot FROM trial_card")[0][0])["reviewed"]
    for n in range(3):
        reviewed[n]["policy_name"]["value"] = PAIR[n % 2]
    rewrite_snapshot(order.path, reviewed=reviewed, eligible=[1, 2], same_card=[[3, 2]])
    with journal_at(order.path) as j, j.db:
        j.db.execute("DELETE FROM trial_handoff")
        j.db.execute("INSERT INTO trial_handoff VALUES('',NULL,1,1,3,?,NULL,?)", (NOW, READONLY_CHECK))
    if path == "close":
        with journal_at(order.path) as j:
            j.rotate("session2", "连接断开")
    else:
        old_body = "登记 1 · 条目 1、2、3 · 审批到期，未建单，转人工。待补项：无。"
        with journal_at(order.path) as j, j.db:
            j.db.execute("UPDATE trial_card SET status='closed'")
            j.db.execute("INSERT INTO trial_notification VALUES(?,'closed',?,?,'pending')",
                         (query(order.path, "SELECT card FROM trial_card")[0][0], str(uuid5(NS, "q")), old_body))
    listener, transport = deliver(order)
    listener.maintain()
    text, = [json.loads(t)["text"] for t in sent(transport)]
    assert group_line(1, 2, 3) in text and text.count("合为一项交人工核对") == 1
    if path == "close":
        assert "登记 1 · 连接断开。" + group_line(1, 2, 3) + "待补项" in text and "未建单" not in text
    assert open_handoffs(order) == [(1, 3, "", READONLY_CHECK), (1, 1, name_key(PAIR[0]), SAME_NAME_GROUP_MARK)]
    assert recovery_kinds(order) == {"order_handoff": [], "order_readonly_check": []}
    group, = group_rows(order)
    fps = [(m["item"], m["display_fp"], "merged") for m in group["members"]]
    assert [m[0] for m in fps] == [1, 2, 3]
    assert cli.main(release_argv(order, fps[:2])) == 1
    assert query(order.path, "SELECT count(*) FROM trial_handoff WHERE cleared IS NULL") == [(2,)]
    assert cli.main(release_argv(order, fps)) == 0
    assert query(order.path, "SELECT count(*) FROM trial_handoff WHERE cleared IS NULL") == [(0,)]
    assert order.orders().run_once() is False and order.mingdao.sent == []
