"""#154 段 3：首张【测试】单在建单开关关闭时的唯一例外（守望者 G505 / G514 / G515 / G516）。

进程内替身覆盖出卡绑定、2b 审批门禁与扫描、2c 存档（X8a）、2d 建单与组合核对；1 个真实子进程用例验证父子状态帧
（当前有效测试卡 → 父进程回「测试授权核对中」，旧帧回「建单未开启」），沙箱拒绝 Unix 套接字时跳过，只在本机跑。
"""

import json
import socket
import subprocess
import sys
import time
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from qianshu._approval_protocol import SERVICE_STATUS, SNAPSHOT, TEST_CARD
from qianshu.feishu.sidecar import Generation, Sidecar
from qianshu.feishu.trial_approval import Journal, journal_at, recovery
from qianshu.feishu.trial_archive import ArchiveRunner
from qianshu.feishu.trial_listener import Listener
from qianshu.feishu.trial_order import CANCEL_REASONS, OrderRunner
from qianshu.feishu.trial_supervisor import TEST_PENDING, TrialSupervisor
from qianshu.feishu.trial_test import (READY, SNAPSHOT_KEY, active_test_card, snapshot_exception,
                                       trial_test_exception)
from qianshu.mail.trial_state import Store
from qianshu.mingdao.create_order import TrialRegistration, UnverifiedCombination

from test_trial_archive import CFG, NOW, SW, query
from test_trial_archive import env  # noqa: F401  （复用 2c 夹具）
from test_trial_order import FakeMingdao

NAME = "【测试】合成政策"
KEY = "T-1"
OFF = dict(SW, MAIL_TRIAL_CREATE_ENABLED="false", MAIL_TRIAL_ARCHIVE_PURPOSE="X8a")
DAY = datetime(2030, 1, 1, 1, tzinfo=UTC)


def authorize(directory, **changes):
    entry = dict(is_test=True, trial_registration=1, trial_version=1, test_name=NAME, policy_type="定向政策",
                 benefit_type="组合0", expires="2030-01-02T00:00:00+00:00", owner_receipt="负责人当场确认")
    entry.update(changes)
    path = directory / "create-authorizations.json"
    path.unlink(missing_ok=True)
    path.write_text(json.dumps({KEY: entry, "F-1": {"is_test": False, "config_sha256": "x"}}, ensure_ascii=False))
    path.chmod(0o600)
    return path


def issue(path, registry, *, name=NAME, switches=OFF, reg=1, version=1, new=True, verified=()):
    """出卡（正式出卡函数 decision_pages），单条合成政策；verified_combinations 默认为空，证明组合由授权代替。"""
    from qianshu.draft import DraftField, OfferPolicyDraft
    from qianshu.mail.trial_cards import decision_pages
    from qianshu.parse import ParsedPolicyDraft
    from qianshu.parse.protocol import FieldResult, StatementStatus
    from qianshu.pipeline import _Policy
    from qianshu.validate import ValidationResult
    store = Store(path)
    with store.write() as db:
        if new:
            db.execute("INSERT INTO mailbox(scope,state) VALUES('s','active')")
        if new:
            db.execute("INSERT INTO registration(id,scope,version,status,attachments) VALUES(?,'s',?,'done',0)",
                       (reg, version))
        else:
            db.execute("UPDATE registration SET version=? WHERE id=?", (version, reg))  # 同一登记出新版本
    j = Journal(store.db)
    if new:
        j.rotate("session", "启动")
    text = "\n".join(f"{f}：明细{n}" for n, f in enumerate(("活动周期", "每个标签的用户数", "政策内容", "使用场景", "标签名称")))
    draft = ParsedPolicyDraft(**{f.name: DraftField() for f in fields(OfferPolicyDraft)})
    draft.policy_type.value, draft.benefit_type.value, draft.policy_name.value = "定向政策", "组合0", name
    draft.trigger_once.value, draft.effective_multiplier.value = "否", 1
    draft.maintain_mutual_exclusion_group.value = "是"
    draft.source_span = (0, len(text))
    draft.field_results = {"country": FieldResult(SimpleNamespace(status=StatementStatus.CERTAIN), "区域甲", True,
                                                  needs_confirmation=False)}
    draft.to_dict = lambda: {"sample": True}
    trial = SimpleNamespace(store=store, config={}, clock=lambda: DAY, sensitive=())
    pages = []
    row = store.row("SELECT * FROM registration WHERE id=?", reg)
    card = decision_pages(trial, row, text, pages, [draft], [_Policy(draft, (), {}, ValidationResult(()))], False,
                          (switches, {"verified_combinations": [list(v) for v in verified]}, "ou_config", registry),
                          False)
    store.db.close()
    return card, pages


def deliver(path, card, pages, reg=1, version=1):
    with journal_at(path) as j, j.db:
        for n in range(1, len(pages) + 1):
            j.db.execute("INSERT INTO card_intent VALUES(?,?,?,?,'sent','')", (f"{card}-{n}", reg, version, n))
            j.received(f"{card}-{n}", f"{card}-message" if n == len(pages) else f"{card}-page{n}")
        j.bind(card, NOW + 1)


def exception_of(registry, clock):
    """审批门禁与扫描处接入的判定（Listener.test_exception）。"""
    listener = Listener.__new__(Listener)
    listener.registry, listener.clock = registry, clock
    return listener.test_exception


def click(path, card, registry, *, at=NOW + 2, switches=OFF, reg=1, version=1):
    """邮件审批已核验 → 同意，建单开关关闭。"""
    texts = []
    with journal_at(path) as j:
        for action in ("verify", "approve"):
            texts.append(j.action("session", card, reg, version, f"{card}-message", action, f"{card}-{action}",
                                  "ou_actor", at, lambda: at, lambda: True, True, switches, archive=True,
                                  test_exception=exception_of(registry, lambda: at)))
        j.db.commit()
    return texts


def runners(env, registry, fake):
    archive = ArchiveRunner(env.path, switches=lambda: env.switches, config=CFG, sensitive=(), ledger=env.ledger,
                            connected=lambda: True, clock=lambda: env.now, factory=lambda cfg: env.fake,
                            registry=registry)

    def pause(seconds):
        env.now += max(seconds, 1.0)
    orders = OrderRunner(env.path, switches=lambda: env.switches, backend=fake, connected=lambda: True,
                         ledger_dir=env.tmp, clock=lambda: env.now, pause=pause, readback_seconds=5,
                         registry=registry)
    return archive, orders


def ledger(env):
    path = env.tmp / "trial-orders.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def binding(path):
    return query(path, "SELECT authorization, card, status FROM trial_test_binding")


# ---------------------------------------------------------------- 正向

def test_bound_test_card_runs_once_with_create_off(env):
    registry = authorize(env.tmp)
    env.switches = dict(OFF)
    card, pages = issue(env.path, registry)
    snapshot = json.loads(query(env.path, "SELECT snapshot FROM trial_card")[0][0])
    face = json.dumps(pages, ensure_ascii=False)
    # 出卡：冻结授权登记号并回显；X8a 预览（标题带【测试】）；组合由授权代替空的 verified_combinations。
    assert snapshot[SNAPSHOT_KEY] == KEY and snapshot["eligible"] == [1] and "授权登记号 " + KEY in face
    assert "【测试】申请存档" in face and '"approve"' in face
    assert binding(env.path) == [(KEY, card, "bound")]
    deliver(env.path, card, pages)
    # 子进程状态帧的当前有效测试卡（G516 乙）；父进程据此只回「测试授权核对中」。
    with journal_at(env.path) as j:
        assert active_test_card(j.db, registry, NOW + 2) == (1, 1, KEY, card)
    supervisor = TrialSupervisor.__new__(TrialSupervisor)
    supervisor.mode, supervisor.create_enabled, supervisor.test_card = "cards", False, (1, 1, KEY)
    supervisor.offer = lambda *args: True
    value = {"action": "approve", "registration": 1, "version": 1, "card": card, "format": 1}
    reply = supervisor.click(b"{}", time.monotonic(), time.monotonic() + 5, {"event": {"action": {"value": value}}})
    assert reply == TEST_PENDING and "已建单" not in reply
    assert click(env.path, card, registry) == ["邮件审批已核验", "有效同意已记录，将建存档"]
    # 关开关扫描不收口在途测试卡。
    with journal_at(env.path) as j:
        j.scan(NOW + 3, create_enabled=False, test_exception=exception_of(registry, lambda: NOW + 3))
    assert query(env.path, "SELECT status FROM trial_card") == [("accepted",)]
    fake = FakeMingdao()
    archive, orders = runners(env, registry, fake)
    assert archive.run_once() is True
    assert query(env.path, "SELECT status, purpose FROM trial_archive") == [("success", "X8a")]
    assert binding(env.path) == [(KEY, card, "consumed")]
    assert (env.tmp / ("trial-closed-" + __import__("hashlib").sha256(KEY.encode()).hexdigest() + ".json")).exists()
    with journal_at(env.path) as j:
        j.scan(NOW + 11, create_enabled=False, test_exception=exception_of(registry, lambda: NOW + 11))
    assert orders.run_once() is True
    assert fake.sent == [1] and query(env.path, "SELECT status FROM trial_order") == [("verified",)]
    assert binding(env.path) == [(KEY, card, "verified")]
    rows = ledger(env)
    assert rows and all(r["is_test"] is True and r["name"] == NAME for r in rows)
    assert [r["event"] for r in rows] == ["create_reserved", "create_attempt", "created", "readback_compared"]
    with journal_at(env.path, readonly=True) as j:
        ready = [r for r in recovery(j.db) if r["kind"] == "test_combination_ready"]
    assert ready == [{"kind": "test_combination_ready", "registration": 1, "version": 1, "status": READY}]
    # 不自动登记组合；重启（新执行器）后不再存档、不再建单。
    again_archive, again_orders = runners(env, registry, fake)
    assert again_archive.run_once() is False and again_orders.run_once() is False and fake.sent == [1]
    assert env.fake.writes.count("create") == 1


# ---------------------------------------------------------------- 七类回归：关开关 0 存档、0 建单

@pytest.mark.parametrize("case", ["ordinary", "unbound", "expired", "consumed", "prefix", "combination", "second"])
def test_create_off_refusals_archive_and_order_nothing(env, case):
    registry = authorize(env.tmp, **{"unbound": {"trial_registration": 9}, "prefix": {"test_name": "合成政策"},
                                     "combination": {"benefit_type": "组合9"},
                                     "expired": {"expires": "2030-01-01T01:00:05+00:00"}}.get(case, {}))
    switches = dict(OFF, MAIL_TRIAL_ARCHIVE_PURPOSE="X8b") if case == "ordinary" else OFF
    env.switches = dict(switches)
    name = "合成政策" if case == "prefix" else NAME
    if case == "second":
        first, pages = issue(env.path, registry)
        # 同一「登记 × 版本」已绑定：再判定出卡一律不绑定；新版本未经重新确认，旧授权无效。
        with journal_at(env.path) as j:
            assert trial_test_exception(j.db, registry, registration=1, version=1, card="again",
                                        snapshot={"reviewed": [{"policy_name": {"value": NAME},
                                                                "policy_type": {"value": "定向政策"},
                                                                "benefit_type": {"value": "组合0"}}]},
                                        now=NOW, stage="issue") is None
        card, pages = issue(env.path, registry, version=2, new=False)
        version = 2
    else:
        # 普通卡：未绑定、组合已登记，照常有同意按钮。
        card, pages = issue(env.path, None if case == "ordinary" else registry, name=name, switches=switches,
                            verified=[("定向政策", "组合0")] if case == "ordinary" else ())
        version = 1
    snapshot = json.loads(query(env.path, "SELECT snapshot FROM trial_card WHERE card=?", card)[0][0])
    assert (SNAPSHOT_KEY in snapshot) == (case in ("expired", "consumed"))
    deliver(env.path, card, pages, version=version)
    if case == "consumed":
        TrialRegistration(KEY, "", "", "", "", "").close(env.tmp)
    texts = click(env.path, card, registry, at=NOW + 10, switches=switches, version=version)
    assert not any("将建存档" in t or "执行器未就绪" in t for t in texts)
    if case in ("ordinary", "expired", "consumed"):
        assert texts[-1] == "建单未开启"
    fake = FakeMingdao()
    archive, orders = runners(env, registry, fake)
    for _ in range(2):
        archive.run_once()
        orders.run_once()
    assert env.fake.writes == [] and fake.sent == []
    assert query(env.path, "SELECT count(*) FROM trial_archive WHERE status!='failed'") == [(0,)]
    assert query(env.path, "SELECT count(*) FROM trial_order") == [(0,)]
    assert query(env.path, "SELECT count(*) FROM trial_receipt WHERE action='approve'") == [(0,)]


def test_exception_closes_after_approval_when_authorization_expires_before_archive(env):
    """同意后、存档认领前授权到期：存档门禁按唯一判定停批并永久关闭登记，0 存档、0 建单。"""
    registry = authorize(env.tmp, expires="2030-01-01T01:00:05+00:00")
    env.switches = dict(OFF)
    card, pages = issue(env.path, registry)
    deliver(env.path, card, pages)
    assert click(env.path, card, registry, at=NOW + 2)[-1] == "有效同意已记录，将建存档"
    fake = FakeMingdao()
    archive, orders = runners(env, registry, fake)
    assert archive.run_once() is False and orders.run_once() is False
    assert query(env.path, "SELECT status, reason FROM trial_archive") == [("failed", "test")]
    assert binding(env.path) == [(KEY, card, "closed")] and env.fake.writes == [] and fake.sent == []


# ---------------------------------------------------------------- 并发、重启

def test_two_cards_on_one_authorization_only_first_passes(env):
    registry = authorize(env.tmp)
    env.switches = dict(OFF)
    card, pages = issue(env.path, registry)
    # 另一张卡（登记 2）快照同样冻结了该授权登记号（如并发出卡）：绑定已被占用，判定不放行。
    with journal_at(env.path) as j, j.db:
        j.db.execute("INSERT INTO registration(id,scope,version,status,attachments) VALUES(2,'s',1,'done',0)")
        snapshot = json.loads(j.db.execute("SELECT snapshot FROM trial_card").fetchone()[0])
    with journal_at(env.path) as j:
        j.prepare("other", 2, 1, "session", "区域甲", dict(snapshot, pages=snapshot["pages"]), False, NOW)
    deliver(env.path, card, pages)
    deliver(env.path, "other", pages, reg=2)
    assert binding(env.path) == [(KEY, card, "bound")]
    assert click(env.path, "other", registry, reg=2)[-1] == "建单未开启"
    assert click(env.path, card, registry)[-1] == "有效同意已记录，将建存档"
    with journal_at(env.path) as j:
        assert active_test_card(j.db, registry, NOW + 2) is None  # 已同意，不再是待审批的测试卡


def test_restart_during_send_freezes_and_never_resends(env):
    registry = authorize(env.tmp)
    env.switches = dict(OFF)
    card, pages = issue(env.path, registry)
    deliver(env.path, card, pages)
    click(env.path, card, registry)
    fake = FakeMingdao()
    archive, orders = runners(env, registry, fake)
    assert archive.run_once() is True

    def crash(item):
        raise SystemExit  # 交接后、结果未知时进程被杀
    fake.on_send = crash
    with pytest.raises(SystemExit):
        orders.run_once()
    assert binding(env.path) == [(KEY, card, "sending")]
    fake.on_send = None
    _, restarted = runners(env, registry, fake)
    restarted.run_once()
    restarted.run_once()
    assert fake.sent == [1] and query(env.path, "SELECT status FROM trial_order") == [("unknown",)]


# ---------------------------------------------------------------- 2d 组合核对、帧兼容

def test_plan_uses_authorized_combination_only_for_bound_card():
    from qianshu.feishu.trial_order import MingdaoBackend

    class Reached(Exception):
        pass
    backend = MingdaoBackend.__new__(MingdaoBackend)
    backend.O = SimpleNamespace(require_verified_combination=Mock(side_effect=UnverifiedCombination("x")),
                                verify_company_inputs=Mock(side_effect=Reached))
    backend.tags, backend.evidence, backend.snapshot = False, None, {}
    backend.config = {"template": {"country": {"value": "区域甲"}}}
    backend._values = lambda *args: ({}, [], {}, {"policy_type": "定向政策", "benefit_type": "组合0"}, set())
    backend.card_face = lambda *args: {}
    backend.test_combination = lambda snapshot, policy, benefit: (policy, benefit) == ("定向政策", "组合0")
    with pytest.raises(Reached):
        backend.plan({SNAPSHOT_KEY: KEY}, 1, "url", now=NOW, sent=NOW)
    with pytest.raises(UnverifiedCombination):
        backend.plan({}, 1, "url", now=NOW, sent=NOW)
    backend.test_combination = lambda *args: False
    with pytest.raises(UnverifiedCombination):
        backend.plan({SNAPSHOT_KEY: KEY}, 1, "url", now=NOW, sent=NOW)
    assert "test" in CANCEL_REASONS


def test_snapshot_exception_requires_frozen_snapshot_and_exact_values(env):
    registry = authorize(env.tmp)
    card, _ = issue(env.path, registry)
    snapshot = json.loads(query(env.path, "SELECT snapshot FROM trial_card")[0][0])
    with journal_at(env.path) as j, j.db:
        j.db.execute("UPDATE trial_test_binding SET status='consumed'")
    assert snapshot_exception(env.path, registry, snapshot, NOW, "order", policy="定向政策", benefit="组合0")
    assert snapshot_exception(env.path, registry, snapshot, NOW, "order", policy="定向政策", benefit="组合9") is None
    assert snapshot_exception(env.path, registry, dict(snapshot, eligible=[]), NOW, "order") is None
    registry.chmod(0o644)  # 登记文件不可读（权限不符）：不放行
    assert snapshot_exception(env.path, registry, snapshot, NOW, "order") is None


def frame(*tail):
    return SNAPSHOT.pack(time.monotonic(), 0, 0) + SERVICE_STATUS.pack(0, 0, 0) + b"".join(tail)


@pytest.mark.parametrize("tail,expected", [
    ((), None),
    ((TEST_CARD.pack(1, 1, 1, KEY.encode()),), (1, 1, KEY)),
    ((TEST_CARD.pack(2, 1, 1, KEY.encode()),), None),
    ((TEST_CARD.pack(1, 0, 1, KEY.encode()),), None),
    ((TEST_CARD.pack(1, 1, 1, b"\xff\xfe"),), None),
    ((TEST_CARD.pack(1, 1, 1, b""),), None),
])
def test_frame_compatibility_defaults_to_create_off(tmp_path, tail, expected):
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_NONBLOCK)
    with parent, child:
        sidecar = Sidecar(tmp_path / "machine", service_args=("synthetic",))
        sidecar.test_card = ("stale",)
        child.send(frame(*tail))
        assert sidecar._snapshots(Generation(1, 1, parent)) is True
    assert sidecar.test_card == expected
    supervisor = TrialSupervisor.__new__(TrialSupervisor)
    supervisor.mode, supervisor.create_enabled, supervisor.test_card = "cards", False, sidecar.test_card
    supervisor.offer = lambda *args: True
    value = {"action": "approve", "registration": 1, "version": 1}
    reply = supervisor.click(b"{}", time.monotonic(), time.monotonic() + 5, {"event": {"action": {"value": value}}})
    assert reply == (TEST_PENDING if expected else "建单未开启")
    other = dict(value, version=2)
    assert supervisor.click(b"{}", time.monotonic(), time.monotonic() + 5,
                            {"event": {"action": {"value": other}}}) == "建单未开启"


@pytest.mark.parametrize("test_card", [None, (1, 1, KEY, "other"), (1, 2, KEY, "live")])
def test_child_refuses_approve_unless_frame_card_matches(test_card):
    from qianshu.feishu.trial_consumer import Consumer
    consumer = Consumer.__new__(Consumer)
    consumer.create_enabled, consumer.test_card, consumer.epoch = False, test_card, "epoch"
    consumer.origin, consumer.deadline = SimpleNamespace(close_code=None), time.monotonic() + 4
    consumer.listener = SimpleNamespace(settings=SimpleNamespace(app_id="app", tenant_key="tenant", chat_id="group"),
                                        refuse_click=Mock(), record=AsyncMock(),
                                        update_result=AsyncMock(return_value=True))
    header = SimpleNamespace(app_id="app", tenant_key="tenant", event_id="click")
    event = SimpleNamespace(context=SimpleNamespace(open_message_id="message", open_chat_id="group"),
                            operator=SimpleNamespace(open_id="ou_actor"),
                            action=SimpleNamespace(value=dict(card="live", format=1, registration=1, version=1,
                                                              action="approve")))
    import asyncio
    asyncio.run(consumer.record(SimpleNamespace(header=header, event=event)))
    consumer.listener.refuse_click.assert_called_once()
    consumer.listener.record.assert_not_awaited()
    consumer.test_card = (1, 1, KEY, "live")
    consumer.listener.refuse_click.reset_mock()
    asyncio.run(consumer.record(SimpleNamespace(header=header, event=event)))
    consumer.listener.refuse_click.assert_not_called()
    consumer.listener.record.assert_awaited_once()


# ---------------------------------------------------------------- 真实子进程：父子状态帧

CHILD = r"""
import select, socket, sys, time
from types import SimpleNamespace
sys.path.insert(0, sys.argv[2])
from qianshu._approval_child import snapshot
channel = socket.socket(fileno=int(sys.argv[1]))
consumer = SimpleNamespace(create_enabled=False, manual_checks=0, pending_notices=0, test_card=(1, 1, "T-1", "live"))
read = 0
ends = time.monotonic() + 20
while time.monotonic() < ends:
    if select.select([channel], [], [], 0.05)[0]:
        packet = channel.recv(16384)
        if not packet:
            break
        if packet.startswith(b"D"):
            read += 1
            if packet.endswith(b"old"):
                consumer.test_card = None
    snapshot(channel, read, read, consumer)
"""


class Parent(TrialSupervisor):
    """真实监督进程逻辑（Sidecar 帧解析 + TrialSupervisor.click），子进程用标准库替身发真实帧函数的包。"""
    startup_timeout = snapshot_timeout = 2
    _generation = Sidecar._generation  # 不读试运行开关文件，直接起子进程代次

    def __init__(self, directory):
        Sidecar.__init__(self, directory, service_args=("synthetic",))
        self.mode, self.create_enabled = "cards", False
        self.children = []

    def _launch(self, endpoint):
        src = str(Path(__file__).resolve().parents[2] / "src")
        child = subprocess.Popen([sys.executable, "-I", "-S", "-c", CHILD, str(endpoint.fileno()), src],
                                 pass_fds=(endpoint.fileno(),), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(child)
        return child


def until(check, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    pytest.fail("状态帧未在时限内到达")


def test_real_child_frame_selects_reply_and_old_frame_falls_back(tmp_path):
    try:
        socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)[0].close()
    except OSError:
        pytest.skip("沙箱拒绝 Unix 套接字")
    parent = Parent(tmp_path / "machine")
    parent.connect()
    parent.beat = time.monotonic() + 3600
    try:
        until(lambda: parent.test_card == (1, 1, KEY))

        def reply(version, payload=b"{}"):
            now = time.monotonic()
            value = {"action": "approve", "registration": 1, "version": version}
            return parent.click(payload, now, now + 5, {"event": {"action": {"value": value}}})
        assert reply(1) == TEST_PENDING
        assert reply(2) == "建单未开启"
        assert reply(1, b"old") == TEST_PENDING  # 本次仍按当前帧；子进程收到后改发旧帧
        until(lambda: parent.test_card is None)
        assert reply(1) == "建单未开启"
    finally:
        parent.stop()
        until(lambda: not parent.alive)
        for child in parent.children:
            child.wait(5)
