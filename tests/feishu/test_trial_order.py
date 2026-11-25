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
    assert bodies and all("未建单，已转人工。" in b for b in bodies)
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
    assert "断线，未建单" in texts["order_cancel:1"] and "断线，未建单" in texts["order_cancel:2"]
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
    assert "断线，未建单" in texts["order_cancel:2"]


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
    assert "断线，未建单" in texts["order_cancel:2"]
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


def test_zero_assembly_without_confirmed_configuration(tmp_path):
    login = tmp_path / "login"
    login.mkdir(mode=0o700)
    (login / "mingdao-state.json").write_text(json.dumps({"cookies": [{
        "name": "session", "domain": "www.mingdao.com", "path": "/", "expires": -1, "value": "placeholdervalue"}]}))
    (login / "mingdao-state.json").chmod(0o600)
    config = {"create": {"identifiers": {"worksheetId": "sheet"}, "session_cookie": "session"},
              "official": {"token_key": "TOKEN_KEY", "app_id_key": "APP_KEY"}, "template": {}, "fields": {},
              "fixed": {}, "readback_skip": [], "applicant_control": "c",
              "order_snapshot": {"verified_combinations": []}}
    path = login / "mingdao.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    good = dict(args=SimpleNamespace(mingdao_config=path),
                settings=SimpleNamespace(trial_credentials_complete=True, trial_login_dir=login,
                                         trial_credentials=(("TOKEN_KEY", "t"), ("APP_KEY", "a"))),
                state=tmp_path / "trial-050.sqlite3", switches=lambda: dict(SW), connected=lambda: True,
                sensitive=(), archive_runner=object())
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
    with archive_worker(order.runner(), interval=0.01, join=5, orders=orders):
        end = time.monotonic() + 10
        while time.monotonic() < end and query(order.path, "SELECT status FROM trial_card") != [("done",)]:
            time.sleep(0.02)
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
    assert "断线，未建单" in texts["order_cancel:2"]


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
