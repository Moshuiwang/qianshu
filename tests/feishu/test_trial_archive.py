"""#154 段 2c：同意后建存档。

多数用例用进程内替身（假传输、假断线判断）；两个真实子进程用例验证断线空档照发 1 步、子进程中途退出后不重发，
沙箱拒绝 Unix 套接字时跳过，只在本机跑。
"""

import json
import os
import select
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qianshu.config import Config, ConfigError, trial_config
from qianshu.feishu import archive as A
from qianshu.feishu.trial_approval import ARCHIVE_NOTICES, DISCONNECT_NOTICES, journal_at, recovery
from qianshu.feishu.trial_archive import ArchiveRunner, DisconnectGate, archive_worker, build_runner
from qianshu.feishu.trial_listener import Listener, checked_transport
from test_trial_paths import setup_card, settings

NOW = datetime(2030, 1, 1, 1, tzinfo=UTC).timestamp()
DOMAIN = "https://" + "feishu.cn"
CFG = Config({A._ROUTES["定向政策"]: DOMAIN + "/wiki/Node", A._ROUTES["公开政策"]: DOMAIN + "/docx/Public",
              A._SHARE: "anyone_readable", "FEISHU_APP_ID": "syntheticapp", "FEISHU_APP_SECRET": "synthetickey"})
SW = dict(MAIL_TRIAL_RECEIVE_ENABLED="true", MAIL_TRIAL_CARD_ENABLED="true", MAIL_TRIAL_CREATE_ENABLED="true",
          MAIL_TRIAL_CREATE_COUNTRY="区域甲", MAIL_TRIAL_APPROVER_OPEN_IDS="ou_actor", MAIL_TRIAL_ARCHIVE_PURPOSE="X8b")


class Fake:
    """假飞书传输。hook(stage) 在写步骤生效后、返回前调用；读目的页时以 "target" 调用。"""

    def __init__(self):
        self.writes, self.permission = [], {}
        self.hook = self.reject = self.lose = None
        self.reads = []

    def request(self, method, path, *, query, body):
        if path.endswith("get_node") or (method == "GET" and not path.endswith("/public")):
            self.reads.append("target")
            if self.hook:
                self.hook("target")
        if path.endswith("get_node"):
            return {"node": {"obj_token": "Target", "obj_type": "docx"}}
        if method == "GET" and not path.endswith("/public"):
            return {"document": {"document_id": path.split("/")[-1]}}
        if method == "GET":
            self.reads.append("verify")
            if self.hook:
                self.hook("verify")
            return {"permission_public": self.permission}
        stage = "share" if method == "PATCH" else path.split("/")[-4] if path.endswith("/children") else "create"
        self.writes.append(stage)
        if self.reject == stage:
            raise A.ArchiveRejected
        if stage == "share":
            self.permission, result = body, {}
        elif stage == "create":
            result = {"document": {"document_id": "Created"}}
        else:
            result = {"children": [{"block_id": f"block{i}"} for i in range(len(body["children"]))]}
        if self.hook:
            self.hook(stage)
        if self.lose == stage:
            raise TimeoutError
        return result


def query(path, sql, *args):
    with journal_at(path, readonly=True) as j:
        return [tuple(r) for r in j.db.execute(sql, args)]


def accept(path, *, mixed=False, switches=SW):
    """出卡 → 已送达 → 邮件审批已核验 → 有效同意（执行器已装配）。"""
    store, _, card, pages = setup_card(path)
    store.db.close()
    with journal_at(path) as j:
        if mixed:
            snapshot = json.loads(j.db.execute("SELECT snapshot FROM trial_card").fetchone()[0])
            snapshot["material"]["policies"][1]["kind"] = "公开政策"
            from qianshu.feishu.trial_archive import _batch
            batch = _batch(snapshot["material"], "X8b", "1-v1", 1)
            snapshot["preview"] = A.preview_archive_batch(batch, config=CFG).fingerprint
            from hashlib import sha256
            packed = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
            j.db.execute("UPDATE trial_card SET snapshot=?, fingerprint=?", (packed, sha256(packed.encode()).hexdigest()))
        for n in range(1, len(pages) + 1):
            j.db.execute("INSERT INTO card_intent VALUES(?,1,1,?,'sent','')", (f"r{n}", n))
            j.received(f"r{n}", "message" if n == len(pages) else f"page{n}")
        j.bind(card, NOW + 1)
        j.db.commit()
        for event, action in (("e1", "verify"), ("e2", "approve")):
            text = j.action("session", card, 1, 1, "message", action, event, "ou_actor", NOW + 2, lambda: NOW + 2,
                            lambda: True, True, switches, archive=True)
        assert "将建存档" in text
        j.db.commit()
    return card, pages


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_JOURNALS", tmp_path / "journals")
    monkeypatch.setattr(A, "_WAIT", .04)
    ledger = tmp_path / "archive-ledger.sqlite3"
    A.initialize_archive_ledger(ledger)
    path = tmp_path / "trial-050.sqlite3"
    fake = Fake()
    state = SimpleNamespace(connected=True, switches=dict(SW), now=NOW + 10, path=path, ledger=ledger, fake=fake,
                            tmp=tmp_path)

    def runner():
        return ArchiveRunner(path, switches=lambda: state.switches, config=CFG, sensitive=(), ledger=ledger,
                             connected=lambda: state.connected, clock=lambda: state.now, factory=lambda cfg: fake)
    state.runner = runner
    return state


def test_consent_builds_archive_once_and_records_link_and_page(env):
    accept(env.path)
    assert query(env.path, "SELECT status FROM trial_task") == [("archive_pending",)]
    runner = env.runner()
    assert runner.run_once() is True
    assert env.fake.writes == ["create", "Created", "share", "Target"]
    status, url, purpose, update = query(env.path, "SELECT status,url,purpose,card_update FROM trial_archive")[0]
    assert (status, purpose, update) == ("success", "X8b", "pending") and url.endswith("/docx/Created")
    assert query(env.path, "SELECT kind,page,summary FROM trial_archive_page") == [
        ("定向政策", "Target", "20300101　区域甲　2 条　")]
    assert query(env.path, "SELECT status FROM trial_item_task") == [("archive_done",)]
    assert query(env.path, "SELECT status FROM trial_task") == [("archive_done",)]
    assert query(env.path, "SELECT status FROM trial_card") == [("accepted",)]
    assert not query(env.path, "SELECT * FROM trial_notification")
    assert runner.run_once() is False and env.fake.writes == ["create", "Created", "share", "Target"]


def test_mixed_batch_binds_both_pages_with_preview_summaries(env):
    accept(env.path, mixed=True)
    assert env.runner().run_once()
    assert env.fake.writes[-2:] == ["Target", "Public"]
    assert query(env.path, "SELECT kind,page,summary FROM trial_archive_page ORDER BY kind") == [
        ("公开政策", "Public", "20300101　区域甲　1 条　"), ("定向政策", "Target", "20300101　区域甲　1 条　")]


@pytest.mark.parametrize("case", ["create_off", "card_off", "country", "halted", "expired", "frozen", "session",
                                  "no_verify", "no_material", "unreadable_switches"])
def test_authorization_checked_once_before_any_write(env, case):
    accept(env.path)
    with journal_at(env.path) as j, j.db:
        if case == "halted":
            j.halt_writes("守望者停写", NOW)
        elif case == "frozen":
            j.db.execute("UPDATE registration SET status='frozen'")
        elif case == "session":
            j.db.execute("UPDATE trial_session SET session='other'")
        elif case == "no_verify":
            j.db.execute("DELETE FROM trial_receipt WHERE action='verify'")
        elif case == "no_material":
            from hashlib import sha256
            snapshot = json.loads(j.db.execute("SELECT snapshot FROM trial_card").fetchone()[0])
            snapshot["material"] = None
            packed = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
            j.db.execute("UPDATE trial_card SET snapshot=?, fingerprint=?", (packed, sha256(packed.encode()).hexdigest()))
    if case == "create_off":
        env.switches["MAIL_TRIAL_CREATE_ENABLED"] = "false"
    elif case == "card_off":
        env.switches["MAIL_TRIAL_CARD_ENABLED"] = "false"
    elif case == "country":
        env.switches["MAIL_TRIAL_CREATE_COUNTRY"] = "区域乙"
    elif case == "expired":
        env.now = NOW + 3 * 86400
    elif case == "unreadable_switches":
        env.switches = None
    runner = ArchiveRunner(env.path, switches=lambda: env.switches if env.switches else 1 / 0, config=CFG,
                           sensitive=(), ledger=env.ledger, connected=lambda: True, clock=lambda: env.now,
                           factory=lambda cfg: env.fake)
    assert runner.run_once() is False
    assert env.fake.writes == [] and not query(env.path, "SELECT * FROM trial_archive WHERE status!='failed'")
    assert query(env.path, "SELECT status,stage FROM trial_archive") == [("failed", "gate")]
    assert query(env.path, "SELECT status FROM trial_card") == [("closed",)]
    assert query(env.path, "SELECT status FROM trial_item_task") == [("closed",)]
    kind, body = query(env.path, "SELECT kind,body FROM trial_notification")[0]
    assert kind == "archive_failed" and body.endswith(ARCHIVE_NOTICES["gate"])
    assert not query(env.path, "SELECT * FROM trial_known_record")


def test_unregistered_purpose_never_publishes(env):
    accept(env.path)
    env.switches.pop("MAIL_TRIAL_ARCHIVE_PURPOSE")
    assert env.runner().run_once() is False and env.fake.writes == []
    kind, body = query(env.path, "SELECT kind,body FROM trial_notification")[0]
    assert kind == "archive_failed" and body.endswith("本批存档发布确认未登记，未建单，已转人工。")


def test_purpose_x8a_is_refused_for_ordinary_application_before_any_write(env):
    # X8a 只授权【测试】单合成试写：普通申请在授权核对处即按「发布确认未登记」停下，不出存档授权、零写入。
    accept(env.path)
    env.switches["MAIL_TRIAL_ARCHIVE_PURPOSE"] = "X8a"
    assert env.runner().run_once() is False and env.fake.writes == []
    assert query(env.path, "SELECT status,stage,reason FROM trial_archive") == [("failed", "gate", "unconfirmed")]
    assert query(env.path, "SELECT status FROM trial_item_task") == [("closed",)]
    assert query(env.path, "SELECT kind,body FROM trial_notification") == [
        ("archive_failed", "登记 1 · 版本 1 · " + ARCHIVE_NOTICES["unconfirmed"])]
    assert not list(env.tmp.glob("journals/*"))


def test_x8a_never_publishes_two_ordinary_batches(env):
    # 同一 X8a 确认下，两批普通申请照常核验、同意，均不发布。
    second = env.tmp / "second"
    second.mkdir(mode=0o700)
    paths = [env.path, second / "trial-050.sqlite3"]
    switches = dict(SW, MAIL_TRIAL_ARCHIVE_PURPOSE="X8a")
    for path in paths:
        accept(path)
        runner = ArchiveRunner(path, switches=lambda: switches, config=CFG, sensitive=(), ledger=env.ledger,
                               connected=lambda: True, clock=lambda: env.now, factory=lambda cfg: env.fake)
        assert runner.run_once() is False and runner.run_once() is False
        assert query(path, "SELECT status,reason FROM trial_archive") == [("failed", "unconfirmed")]
        assert query(path, "SELECT body FROM trial_notification")[0][0].endswith(ARCHIVE_NOTICES["unconfirmed"])
    assert env.fake.writes == [] and env.fake.reads == []
    with A._database(env.ledger) as grants:
        assert grants.execute("SELECT count(*) FROM grants").fetchone()[0] == 0


def test_x8a_card_shows_no_publishable_preview_and_is_readonly(tmp_path, monkeypatch):
    import test_trial_paths as paths
    monkeypatch.setattr(paths, "switches", lambda: dict(SW, MAIL_TRIAL_ARCHIVE_PURPOSE="X8a"))
    path = tmp_path / "trial-050.sqlite3"
    store, _, card, pages = paths.setup_card(path)
    store.db.close()
    content = json.dumps(pages, ensure_ascii=False)
    assert ARCHIVE_NOTICES["unconfirmed"] in content and "【测试】" not in content
    assert "存档标题" not in content and '"form"' not in content and "整批转人工" in content
    readonly, snapshot = query(path, "SELECT readonly,snapshot FROM trial_card")[0]
    assert readonly and json.loads(snapshot)["preview"] is None and json.loads(snapshot)["material"] is None


def test_failure_stops_whole_batch_with_fixed_notice(env):
    accept(env.path)
    env.fake.reject = "share"
    assert env.runner().run_once()
    assert env.fake.writes == ["create", "Created", "share"]
    assert query(env.path, "SELECT status,stage FROM trial_archive") == [("failed", "share")]
    assert query(env.path, "SELECT status FROM trial_item_task") == [("closed",)]
    assert query(env.path, "SELECT status FROM trial_card") == [("closed",)]
    assert query(env.path, "SELECT kind,body FROM trial_notification") == [
        ("archive_failed", "登记 1 · 版本 1 · 本批存档失败，未建单，已转人工。")]
    assert not query(env.path, "SELECT url FROM trial_archive WHERE url IS NOT NULL")


def test_unknown_result_freezes_without_resend_or_cleanup(env):
    accept(env.path)
    env.fake.lose = "Target"
    runner = env.runner()
    assert runner.run_once()
    writes = list(env.fake.writes)
    assert query(env.path, "SELECT status FROM trial_archive") == [("unknown",)]
    assert query(env.path, "SELECT status FROM trial_item_task") == [("archive_unknown",)]
    assert query(env.path, "SELECT kind,body FROM trial_notification") == [
        ("archive_unknown", "登记 1 · 版本 1 · 本批存档结果未知，未建单，请配置负责人核对存档文档与汇总页。")]
    with journal_at(env.path) as j:
        assert any(r["kind"] == "item_task" and r["status"] == "archive_unknown" for r in recovery(j.db))
        counts = j.notice_counts()
        assert counts[0] == 1 and counts[1] == 1
    # 同批再次触发、新建执行器（重启）都不再发。
    assert runner.run_once() is False and env.runner().run_once() is False
    with journal_at(env.path) as j:
        j.rotate("session2", "重启")
    assert env.runner().run_once() is False and env.fake.writes == writes
    assert query(env.path, "SELECT status FROM trial_archive") == [("unknown",)]


def test_repeat_consent_and_restart_do_not_resend(env):
    card, _ = accept(env.path)
    runner = env.runner()
    assert runner.run_once()
    writes = list(env.fake.writes)
    with journal_at(env.path) as j:
        again = j.action("session", card, 1, 1, "message", "approve", "e3", "ou_actor", NOW + 3, lambda: NOW + 3,
                         lambda: True, True, SW, archive=True)
        assert "不符" in again
        j.rotate("session2", "重启")
    assert env.runner().run_once() is False and env.fake.writes == writes
    # 同意后、存档前重启：卡片被关闭，条目不可再执行，存档写 0。
    assert query(env.path, "SELECT count(*) FROM trial_archive") == [(1,)]


def test_restart_before_archive_starts_closes_without_write(env):
    accept(env.path)
    with journal_at(env.path) as j:
        j.rotate("session2", "重启")
    assert env.runner().run_once() is False and env.fake.writes == []
    assert query(env.path, "SELECT status FROM trial_item_task") == [("closed",)]
    assert "未建单" in query(env.path, "SELECT body FROM trial_notification")[0][0]


def test_midway_exit_closes_as_unknown_and_scan_does_not_preempt_running(env):
    accept(env.path)
    with journal_at(env.path) as j, j.db:
        j.db.execute("BEGIN IMMEDIATE")
        j.db.execute("INSERT INTO trial_archive(registration,version,card,status,started) "
                     "SELECT registration,version,card,'running',0 FROM trial_card")
        j.db.execute("UPDATE trial_item_task SET status='archive_running'")
        j.db.execute("UPDATE trial_task SET status='archive_running'")
    with journal_at(env.path) as j:
        j.scan(NOW + 9 * 86400)
        assert j.db.execute("SELECT status FROM trial_card").fetchone()[0] == "accepted"
        j.rotate("session2", "服务重启或关闭")
    assert query(env.path, "SELECT status,reason FROM trial_archive") == [("unknown", "card_closed")]
    assert query(env.path, "SELECT status FROM trial_item_task") == [("archive_unknown",)]
    assert "存档结果未知" in query(env.path, "SELECT body FROM trial_notification")[0][0]
    assert env.runner().run_once() is False and env.fake.writes == []


def test_archive_done_card_closing_marks_items_closed(env):
    accept(env.path)
    assert env.runner().run_once()
    with journal_at(env.path) as j:
        j.rotate("session2", "审批到期")
    assert query(env.path, "SELECT status FROM trial_item_task") == [("closed",)]
    assert query(env.path, "SELECT status FROM trial_archive") == [("success",)]


def test_disconnect_during_step_sends_that_one_step_only(env):
    accept(env.path)
    env.fake.hook = lambda stage: setattr(env, "connected", False) if stage == "create" else None
    env.runner().run_once()
    assert env.fake.writes == ["create"]
    assert query(env.path, "SELECT status,stage,reason,disconnect_writes FROM trial_archive") == [
        ("failed", "details", "rejected", 1)]
    notices = dict(query(env.path, "SELECT kind,body FROM trial_notification"))
    assert notices["archive_disconnect"].endswith(DISCONNECT_NOTICES[0])
    assert notices["archive_failed"].endswith(ARCHIVE_NOTICES["failed"])
    assert query(env.path, "SELECT status FROM trial_card") == [("closed",)]
    with journal_at(env.path) as j:
        assert j.disconnect_archive_writes() == 1


def test_disconnect_during_permission_read_sends_no_summary(env):
    accept(env.path, mixed=True)
    env.fake.hook = lambda stage: setattr(env, "connected", False) if stage == "verify" else None
    env.runner().run_once()
    # 断线在权限回读期间：汇总写尚未登记发送中，一个也不发（两页汇总写均为 0）。
    assert env.fake.writes == ["create", "Created", "share"]
    assert query(env.path, "SELECT status,stage,reason,disconnect_writes FROM trial_archive") == [
        ("failed", "summary", "rejected", 0)]
    assert not query(env.path, "SELECT * FROM trial_archive_page")
    assert query(env.path, "SELECT kind,body FROM trial_notification") == [
        ("archive_failed", "登记 1 · 版本 1 · " + ARCHIVE_NOTICES["failed"])]
    assert query(env.path, "SELECT status FROM trial_item_task") == [("closed",)]


def test_disconnect_during_target_read_sends_no_create(env):
    accept(env.path)
    env.fake.hook = lambda stage: setattr(env, "connected", False) if stage == "target" else None
    env.runner().run_once()
    assert env.fake.writes == [] and env.fake.reads
    assert query(env.path, "SELECT status,stage,reason,disconnect_writes FROM trial_archive") == [
        ("failed", "create", "rejected", 0)]
    assert query(env.path, "SELECT kind FROM trial_notification") == [("archive_failed",)]
    assert query(env.path, "SELECT status FROM trial_card") == [("closed",)]


def test_last_step_in_flight_at_disconnect_stops_batch_instead_of_pending_order(env):
    accept(env.path)
    env.fake.hook = lambda stage: setattr(env, "connected", False) if stage == "Target" else None
    env.runner().run_once()
    assert env.fake.writes == ["create", "Created", "share", "Target"]
    status, reason, writes, url, update = query(
        env.path, "SELECT status,reason,disconnect_writes,url,card_update FROM trial_archive")[0]
    assert (status, reason, writes, update) == ("success", "disconnect", 1, "none") and url.endswith("/docx/Created")
    assert query(env.path, "SELECT kind,page FROM trial_archive_page") == [("定向政策", "Target")]
    assert query(env.path, "SELECT status FROM trial_item_task") == [("closed",)]
    assert query(env.path, "SELECT status FROM trial_task") == [("closed",)]
    assert query(env.path, "SELECT status FROM trial_card") == [("closed",)]
    notices = dict(query(env.path, "SELECT kind,body FROM trial_notification"))
    assert notices == {"archive_disconnect": "登记 1 · 版本 1 · " + DISCONNECT_NOTICES[0],
                       "archive_stopped": "登记 1 · 版本 1 · " + ARCHIVE_NOTICES["disconnected"]}
    listener, transport = deliver(env)
    listener.maintain()
    texts = sent(transport)
    assert len(texts) == 2 and all('<at user_id=\\"ou_config\\">' in t for t in texts)
    assert any("未建单" in t for t in texts)
    assert transport.order_http.async_request.await_count == 0  # 不改写为「已建存档，等待建单」
    assert env.runner().run_once() is False and len(env.fake.writes) == 4


def test_disconnect_before_start_claims_nothing(env):
    accept(env.path)
    env.connected = False
    assert env.runner().run_once() is False
    assert env.fake.writes == [] and not query(env.path, "SELECT * FROM trial_archive")


def test_disconnect_gate_counts_one_and_reads_stay_open():
    calls, state, marks = [], {"up": True}, []

    class Inner:
        def request(self, method, path, *, query, body):
            calls.append(method)
            return {"document": {"document_id": "Doc"}}

    gate = DisconnectGate(Inner(), lambda: state["up"], lambda: marks.append(1))
    with pytest.raises(A.ArchiveRejected):  # 未经登记边界放行的写不发
        DisconnectGate(Inner(), lambda: True, lambda: None).request("POST", A._DOCS, query={}, body={})
    assert gate.admit()
    gate.request("POST", A._DOCS, query={}, body={})
    assert not marks
    with pytest.raises(A.ArchiveRejected):  # 放行票只用一次
        gate.request("POST", A._DOCS, query={}, body={})
    gate = DisconnectGate(Inner(), lambda: state["up"], lambda: marks.append(1))
    assert gate.admit()  # 登记后核对时仍连通：登记先于断线
    state["up"] = False
    gate.request("POST", A._DOCS, query={}, body={})  # 已登记发送中的 1 步照发
    assert marks == [1] and calls == ["POST", "POST"]
    assert gate.admit() is False  # 断线后登记的步骤不放行
    with pytest.raises(A.ArchiveRejected):
        gate.request("POST", A._DOCS, query={}, body={})
    assert calls == ["POST", "POST"] and marks == [1]
    assert gate.request("GET", "/y", query={}, body=None)  # 只读核对不受限
    # 读请求期间观察到断线：此后登记的写一律不放行，不计为断线写。
    state["up"], marks[:] = True, []
    gate = DisconnectGate(Inner(), lambda: state["up"], lambda: marks.append(1))
    state["up"] = False
    gate.request("GET", "/x", query={}, body=None)
    state["up"] = True  # 即便随后又判为连通，也不再放行
    assert gate.admit() is False and not marks and calls[-1] == "GET"
    # 判断本身出错（先后无法确认）：不放行。
    assert DisconnectGate(Inner(), lambda: 1 / 0, lambda: None).admit() is False


def test_halt_flag_roundtrip(env):
    accept(env.path)
    with journal_at(env.path) as j:
        assert not j.write_halted()
        j.halt_writes("停写", NOW)
        j.halt_writes("再次", NOW)
        assert j.write_halted()
        j.clear_halt()
        assert not j.write_halted()


def deliver(env, switches=SW):
    transport = checked_transport(settings(), env.tmp)
    transport._tenant_token = AsyncMock(return_value="token")
    transport.im.message.acreate = AsyncMock(return_value=SimpleNamespace(
        success=lambda: True, data=SimpleNamespace(message_id="receipt")))
    transport.order_http.async_request = AsyncMock(return_value=None)
    listener = Listener(env.path, settings(), lambda: switches, transport=transport, defer_rotation=True)
    listener._pending = None
    listener.session = "session"
    return listener, transport


def sent(transport):
    return [c.args[0].request_body.content for c in transport.im.message.acreate.await_args_list]


def test_notices_mention_configurator_and_card_is_rewritten(env):
    accept(env.path)
    env.fake.hook = lambda stage: setattr(env, "connected", False) if stage == "create" else None
    env.runner().run_once()
    listener, transport = deliver(env)
    listener.maintain()
    texts = sent(transport)
    assert len(texts) == 2 and all('<at user_id=\\"ou_config\\">' in t for t in texts)
    assert any(DISCONNECT_NOTICES[0] in t for t in texts) and any(ARCHIVE_NOTICES["failed"] in t for t in texts)
    assert {r[0] for r in query(env.path, "SELECT status FROM trial_notification")} == {"sent"}


def test_success_rewrites_original_card_once_and_failure_logs_manual(env):
    accept(env.path)
    assert env.runner().run_once()
    listener, transport = deliver(env)
    listener.maintain()
    listener.maintain()
    assert transport.order_http.async_request.await_count == 1
    assert query(env.path, "SELECT card_update FROM trial_archive") == [("sent",)]
    request = transport.order_http.async_request.await_args.args[0]
    assert "已建存档，等待建单" in request.body.content and '"form"' not in request.body.content
    with journal_at(env.path) as j, j.db:
        j.db.execute("UPDATE trial_archive SET card_update='pending'")
    transport.order_http.async_request.side_effect = TimeoutError
    listener.maintain()
    assert query(env.path, "SELECT card_update FROM trial_archive") == [("manual",)]
    assert query(env.path, "SELECT kind FROM trial_notification") == [("result_manual",)]


def good_args(tmp, **changes):
    ledger = tmp / "archive-ledger.sqlite3"
    ledger.write_text("")
    ledger.chmod(0o600)
    business = tmp / "business.env"
    business.write_text("")
    args = SimpleNamespace(business_env_file=business)
    kwargs = dict(args=args, settings=SimpleNamespace(trial_credentials_complete=True), state=tmp / "state",
                  handoff_path=tmp / "mention", switches=lambda: dict(SW), connected=lambda: True, sensitive=())
    kwargs.update(changes)
    return kwargs


def test_build_runner_only_with_confirmed_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr("qianshu.mail.trial.business_config", lambda _: dict(CFG))
    assert isinstance(build_runner(**good_args(tmp_path)), ArchiveRunner)
    for name in ("create_off", "card_off", "no_business", "credentials", "no_ledger", "config_gap", "broken_switches"):
        tmp = tmp_path / name
        tmp.mkdir()
        kwargs = good_args(tmp)
        if name == "create_off":
            kwargs["switches"] = lambda: dict(SW, MAIL_TRIAL_CREATE_ENABLED="false")
        elif name == "card_off":
            kwargs["switches"] = lambda: dict(SW, MAIL_TRIAL_CARD_ENABLED="false")
        elif name == "no_business":
            kwargs["args"] = SimpleNamespace(business_env_file=None)
        elif name == "credentials":
            kwargs["settings"] = SimpleNamespace(trial_credentials_complete=False)
        elif name == "no_ledger":
            (tmp / "archive-ledger.sqlite3").unlink()
        elif name == "config_gap":
            monkeypatch.setattr("qianshu.mail.trial.business_config", lambda _: {"FEISHU_APP_ID": "x"})
        else:
            kwargs["switches"] = lambda: 1 / 0
        assert build_runner(**kwargs) is None, name


def test_no_runner_means_no_thread_and_no_executor():
    import threading
    before = threading.active_count()
    with archive_worker(None) as runner:
        assert runner is None and threading.active_count() == before


def test_worker_thread_runs_and_stops(env):
    import threading
    accept(env.path)
    with archive_worker(env.runner(), interval=0.01, join=5):
        end = time.monotonic() + 10
        while time.monotonic() < end and not query(env.path, "SELECT * FROM trial_archive WHERE status='success'"):
            time.sleep(0.02)
    assert env.fake.writes == ["create", "Created", "share", "Target"]
    assert not any(t.name == "trial-archive" and t.is_alive() for t in threading.enumerate())


def test_normal_cli_with_switches_off_never_assembles_archive(tmp_path, monkeypatch):
    from threading import Event
    from qianshu import __main__ as cli
    from qianshu.feishu import trial_archive, trial_consumer, trial_listener
    from qianshu.feishu.trial_approval import Journal
    from qianshu.mail.trial_state import Store
    from tests.feishu.startup_replay import observe
    from tests.feishu.test_trial_approval import bind, click

    # 与 2b 的「开关全关后恢复历史」同一入口：登记库里有已同意的卡，开关全关，正常命令行启动。
    path = tmp_path / "trial-050.sqlite3"
    store = Store(path)
    with store.write() as db:
        db.execute("INSERT INTO mailbox(scope,state) VALUES('s','active')")
        db.execute("INSERT INTO registration(id,scope,version,attachments,status) VALUES(1,'s',1,0,'done')")
    journal = Journal(store.db)
    journal.rotate("session", "连接")
    bind(journal)
    click(journal, "verify")
    assert "执行器未就绪" in click(journal, "approve")
    store.db.close()
    switch_file = tmp_path / "switches"
    switch_file.write_text("")
    mention = tmp_path / "mention"

    def forbidden(*a, **k):
        raise AssertionError("未确认配置下不得装配存档执行器")

    for name in ("ArchiveRunner", "build_runner", "archive_worker"):
        monkeypatch.setattr(trial_archive, name, forbidden)
    monkeypatch.setattr(trial_consumer, "build_runner", forbidden)
    monkeypatch.setattr(trial_consumer, "archive_worker", forbidden)
    monkeypatch.setattr(A, "register_archive_batch", forbidden)
    monkeypatch.setattr(A, "_SDK", forbidden)
    monkeypatch.setattr(cli, "load_handoff_settings", lambda *a, **k: (settings(), mention))
    monkeypatch.setattr(trial_consumer, "load_approval_settings", lambda *_: (settings(), mention))
    delivered = Event()
    observe(monkeypatch, delivered)

    def send(transport, content, uuid, card, claim):
        claim()
        delivered.set()
        return "ok"

    monkeypatch.setattr(trial_listener, "send_trial_message", send)
    assert cli.main(["--handoff-only", "--trial-switches", str(switch_file)]) == 0
    assert delivered.is_set()
    assert query(path, "SELECT count(*) FROM trial_archive") == [(0,)]
    assert query(path, "SELECT status FROM trial_task") == [("closed",)]


def test_archive_purpose_switch_parsing(tmp_path):
    path = tmp_path / "switches"
    assert "MAIL_TRIAL_ARCHIVE_PURPOSE" not in trial_config(_write(path, ""), approval=True)
    assert trial_config(_write(path, "MAIL_TRIAL_ARCHIVE_PURPOSE=X8a\n"), approval=True)[
        "MAIL_TRIAL_ARCHIVE_PURPOSE"] == "X8a"
    with pytest.raises(ConfigError):
        trial_config(_write(path, "MAIL_TRIAL_ARCHIVE_PURPOSE=yes\n"), approval=True)


def _write(path, text):
    path.write_text(text)
    return path


# ---------------------------------------------------------------- 真实子进程（只在本机跑）

CHILD = r'''
import json, os, select, socket, sys, time
from pathlib import Path
spec = json.loads(sys.argv[1])
from qianshu.config import Config
from qianshu.feishu import archive as A
from qianshu.feishu.trial_archive import ArchiveRunner
A._JOURNALS = Path(spec["journals"])
A._WAIT = .04
channel = socket.socket(fileno=int(sys.argv[2]))
life = select.poll()
life.register(channel, select.POLLHUP | select.POLLERR | select.POLLRDHUP)
log = Path(spec["log"])

def note(text):
    with log.open("a") as out:
        out.write(text + "\n")

class Slow:
    permission = {}
    def request(self, method, path, *, query, body):
        if path.endswith("get_node"):
            return {"node": {"obj_token": "Target", "obj_type": "docx"}}
        if method == "GET" and not path.endswith("/public"):
            return {"document": {"document_id": path.split("/")[-1]}}
        if method == "GET":
            return {"permission_public": Slow.permission}
        stage = "share" if method == "PATCH" else path.split("/")[-4] if path.endswith("/children") else "create"
        note(stage)
        if stage == "create" and spec["mode"] == "disconnect":
            Path(spec["flag"]).write_text("in-flight")
            time.sleep(0.8)
        if stage == "share":
            Slow.permission = body
            if spec["mode"] == "exit":
                os._exit(3)
            return {}
        if stage == "create":
            return {"document": {"document_id": "Created"}}
        return {"children": [{"block_id": f"b{i}"} for i in range(len(body["children"]))]}

runner = ArchiveRunner(Path(spec["path"]), switches=lambda: spec["switches"], config=Config(spec["config"]),
                       sensitive=(), ledger=Path(spec["ledger"]), connected=lambda: not life.poll(0),
                       clock=lambda: spec["now"], factory=lambda cfg: Slow())
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
    spec = dict(mode=mode, path=str(env.path), ledger=str(env.ledger), journals=str(env.tmp / "journals"),
                log=str(log), flag=str(flag), switches=SW, config=dict(CFG), now=NOW + 10)
    proc = subprocess.Popen([sys.executable, "-I", "-c", CHILD, json.dumps(spec), str(endpoint.fileno())],
                            pass_fds=(endpoint.fileno(),), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=(env.tmp / "child.err").open("w"))
    endpoint.close()
    return parent, proc, log, flag


def wait_for(check, proc, seconds=30):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return
        if proc.poll() is not None and not check():
            time.sleep(0.05)
            if check():
                return
            break
        time.sleep(0.02)
    pytest.fail("子进程未在时限内到达预期状态")


def test_real_child_finishes_in_flight_step_after_parent_hangs_up(env):
    accept(env.path)
    parent, proc, log, flag = child(env, "disconnect")
    try:
        wait_for(flag.exists, proc)
        parent.close()  # 父进程观察到断线，关闭原连接
        assert proc.wait(30) == 0
    finally:
        parent.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert log.read_text().split() == ["create"]
    assert query(env.path, "SELECT status,stage,disconnect_writes FROM trial_archive") == [("failed", "details", 1)]
    assert {r[0] for r in query(env.path, "SELECT kind FROM trial_notification")} == {"archive_disconnect", "archive_failed"}


def test_real_child_exit_midway_is_never_resent(env):
    accept(env.path)
    parent, proc, log, flag = child(env, "exit")
    try:
        assert proc.wait(30) == 3
    finally:
        parent.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    writes = log.read_text().split()
    assert writes == ["create", "Created", "share"]
    assert query(env.path, "SELECT status FROM trial_archive") == [("running",)]
    with journal_at(env.path) as j:
        j.rotate("session2", "服务重启或关闭")
    assert env.runner().run_once() is False
    assert log.read_text().split() == writes and env.fake.writes == []
    assert query(env.path, "SELECT status FROM trial_archive") == [("unknown",)]
    assert "存档结果未知" in query(env.path, "SELECT body FROM trial_notification")[0][0]
