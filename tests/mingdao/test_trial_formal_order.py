"""#154 段 2d-1 修复轮 1：正式单接入 050 网页公式字段与授权核对（照 X5 `_run_registered` 的 web_profile 路径）。

用仓内 050 业务夹具（同 test_order_field_checks 的 web_order）驱动 #154 的明道云适配 MingdaoBackend：
配置含 web_rules 时正式单正常建单、逐字段回读一致；公式授权指纹不符、授权登记缺失或不符 → 0 发；
卡面按出卡日冻结、跨 UTC 日建单 → 0 发；预建连期间走过截止 → 写出 0 次。
"""
import json
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from qianshu.feishu.trial_order import MingdaoBackend, NotSent, OrderRunner, Refused, build_order_runner
from test_create_order import case, platform, business_cli, order, private_export, synthetic_uuids  # noqa: F401
from test_order_field_checks import registered_entry, web_order  # noqa: F401

URL = "https://feishu.cn/docx/FormalArchive1"
OPTIONS = (Path(order.__file__).resolve().parents[3] / "rules" / "parse-options.tsv").read_bytes()


@pytest.fixture(autouse=True)
def same_app_id(case):
    """真实部署配置中建单请求体 appId 与官方应用 ID 同值（X5 配置结构所致）；本文件一律按同值配置（复核 rv-2d5 第 4 问）。
    正式路径要求两者逐字相等，不等即停（另有专测）。"""
    case.ids["appId"] = case.ids["official_app"]
    case.config["create"]["identifiers"]["appId"] = case.ids["official_app"]


@pytest.fixture(autouse=True)
def formal_ledger(case):
    """替身平台按台账末行记录读取时点；#154 的台账由执行器写，这里先建空文件。"""
    (case.private / "trial-orders.jsonl").touch(mode=0o600)


def backend(case, **changes):
    kwargs = dict(official_token=case.official_token, official_app_id=case.ids["official_app"],
                  login_token=case.session_token, options_data=OPTIONS,
                  sensitive={case.session_token, case.official_token, case.ids["official_app"]},
                  evidence=formal_evidence(case))
    kwargs.update(changes)
    return MingdaoBackend(case.config, **kwargs)


def formal_evidence(case):
    return {**deepcopy(case.evidence), "is_test": False}


def card_snapshot(case):
    """出卡快照：整批草稿与来源，reviewed 为出卡时经校验的草稿（开始日期已按出卡当日顺延）。"""
    export = private_export(case.values)
    return {**export, "reviewed": deepcopy(export["drafts"])}


def arm(case, platform):
    """替身平台要求：建单前已有审批卡动作、台账末行为 create_attempt（#154 由执行器写，这里只模拟）。"""
    platform.actions.append("card")
    with (case.private / "trial-orders.jsonl").open("a") as out:
        out.write(json.dumps({"event": "create_attempt"}) + "\n")


def test_formal_order_with_web_rules_builds_and_reads_back_every_field(case, platform, business_cli, web_order):
    rules, _, queries = web_order
    mingdao = backend(case)
    assert mingdao.web and mingdao.tags
    plan = mingdao.plan(card_snapshot(case), 1, URL, now=time.time(), sent=time.time())
    assert platform.create_count == 0 and len(queries) >= 1
    cells = {c["controlId"]: c["value"] for c in plan.extra["controls"]}
    assert cells[case.control["approval_url"]] == URL
    expected = [case.values["policy_name"], case.values["user_scope"], case.values["start_date"],
                case.values["end_date"], case.values["end_date"], "12", "30", "31", "1", '["none"]',
                "合成提示", "[]", "1", ""]
    assert [cells[rule["control"]] for rule in rules.values()] == expected
    assert plan.displayed == plan.written
    # 应回读字段覆盖全部已写控件（含 readback_skip）；指纹仍只取 readback_skip 以外的控件。
    skipped = [case.config["fields"][c]["label"] for c in case.config["readback_skip"]
               if c in {cell["controlId"] for cell in plan.extra["controls"]}]
    assert [f for f in plan.request_fields if f not in skipped] == list(plan.written) and skipped
    assert plan.extra["approved"] is not None
    arm(case, platform)
    handed = []
    record_id = mingdao.send(plan, lambda: handed.append(1))
    assert handed == [1] and platform.create_count == 1 and record_id == case.ids["created"]
    assert [platform.created_cells[rule["control"]] for rule in rules.values()] == expected
    assert platform.created_cells[case.control["approval_url"]] == URL
    verdict, checks = mingdao.readback(plan, record_id, 30)
    assert verdict == "一致" and [c["字段名"] for c in checks] == plan.request_fields
    assert all(c["结果"] == "一致" for c in checks)


@pytest.mark.parametrize("change", ["fingerprint", "no_evidence"])
def test_formal_order_without_matching_formula_authorization_sends_nothing(case, platform, business_cli, web_order,
                                                                           change):
    evidence = formal_evidence(case)
    if change == "fingerprint":
        evidence["web_rules_sha256"] = "0" * 64
    mingdao = backend(case, evidence=None if change == "no_evidence" else evidence)
    with pytest.raises(order.Stop, match="网页字段公式登记未确认" if change == "fingerprint" else "授权登记缺失"):
        mingdao.plan(card_snapshot(case), 1, URL, now=time.time(), sent=time.time())
    assert platform.create_count == 0


@pytest.mark.parametrize("change", ["next_day", "reviewed_value"])
def test_card_face_frozen_at_card_time(case, platform, business_cli, web_order, change):
    """P1-2：卡面按出卡当日生成（开始日期 1 日），发送与建单在 2 日（重算顺延到 2 日）→ 停单、0 发。"""
    snapshot = card_snapshot(case)
    now = time.time()
    if change == "next_day":
        start = datetime.fromisoformat(case.values["start_date"]).replace(tzinfo=UTC)
        now = (start + timedelta(days=1, hours=1)).timestamp()
    else:
        snapshot["reviewed"][0]["gift_days"]["value"] += 1
    with pytest.raises(order.Stop, match="需重新审批"):
        backend(case).plan(snapshot, 1, URL, now=now, sent=now)
    assert platform.create_count == 0


def test_preconnect_past_deadline_never_writes(case, platform, business_cli, web_order, monkeypatch):
    """P1-1：真实 CreateRequest.prepare，预建连把时钟从 100 推到 103（截止 101）→ 交接处拒绝，endheaders 0 次。"""
    mingdao = backend(case)
    plan = mingdao.plan(card_snapshot(case), 1, URL, now=time.time(), sent=time.time())
    clock = [100.0]

    def slow(host, **kwargs):
        connection = platform.connection(host, **kwargs)
        connect = connection.connect

        def preconnect():
            connect()
            clock[0] = 103.0
        connection.connect = preconnect
        return connection
    monkeypatch.setattr(order, "CreateRequest", partial(order.CreateRequest.func, connect=slow))

    def handoff():
        if clock[0] >= 101:
            raise Refused("expired")
    arm(case, platform)
    with pytest.raises(NotSent) as stopped:
        mingdao.send(plan, handoff)
    assert stopped.value.code == "expired" and platform.create_count == 0


@pytest.mark.parametrize("change", ["tariff", "template"])
def test_source_change_during_preconnect_never_writes(case, platform, business_cli, web_order, monkeypatch, change):
    """修复轮 2 第 5 条：真实 CreateRequest.prepare，预建连期间当前资费计费单位由日改为月（或模板单元格被改）→
    交接前按 X5 同一路径重读来源，来源变化即停单，endheaders 0 次。"""
    rules, _, _ = web_order
    mingdao = backend(case)
    plan = mingdao.plan(card_snapshot(case), 1, URL, now=time.time(), sent=time.time())

    def slow(host, **kwargs):
        connection = platform.connection(host, **kwargs)
        connect = connection.connect

        def preconnect():
            connect()
            if change == "tariff":
                case.rate_rows[0].update(unit="月", cycle=0)
            else:
                platform.template[rules["文本"]["control"]] = "预建连期间改动"
        connection.connect = preconnect
        return connection
    monkeypatch.setattr(order, "CreateRequest", partial(order.CreateRequest.func, connect=slow))
    arm(case, platform)
    with pytest.raises(NotSent) as stopped:
        mingdao.send(plan, lambda: pytest.fail("来源变化仍进入交接"))
    assert stopped.value.code == "plan" and platform.create_count == 0


@pytest.mark.parametrize("change", ["start_date", "proof", "same_day"])
def test_reread_across_utc_midnight_rechecks_start_date_and_proof(case, platform, business_cli, web_order, monkeypatch,
                                                                  change):
    """特批第 3 轮第 3 条（复核 P1-3）：真实 CreateRequest.prepare，23:59:45Z 规划、来源重读结束于次日 00:00:05Z →
    开始日期需顺延，写出 0 次、按需重新审批收口；同期政策与标签证明在重读期间过期同样 0 发；
    对照：重读结束仍在当日（23:59:58Z）→ 照常写出 1 次。"""
    start = datetime.fromisoformat(case.values["start_date"]).replace(tzinfo=UTC)
    if change == "proof":
        # 开始日期为次日、跨零点不需顺延；证明在次日 00:00:00Z 到期。
        planned, ended, until = start - timedelta(seconds=15), start + timedelta(seconds=5), start
    else:
        day_end = start + timedelta(hours=23, minutes=59, seconds=45)
        planned, until = day_end, start + timedelta(days=3)
        ended = day_end + timedelta(seconds=13 if change == "same_day" else 20)
    assert datetime.fromisoformat(case.config["order_snapshot"]["concurrent_policies"]["as_of"]) <= planned
    case.evidence["tag_scope"]["unique_until"] = until.isoformat()
    case.evidence["concurrent_scope"]["unique_until"] = until.isoformat()
    case.config["order_snapshot"]["concurrent_policies"]["valid_until"] = until.isoformat()
    clock = [planned.timestamp()]
    mingdao = backend(case, clock=lambda: clock[0])
    plan = mingdao.plan(card_snapshot(case), 1, URL, now=clock[0], sent=clock[0])
    local, calls = MingdaoBackend._local, []

    def reread_done(self):
        # 交接前复核：平台读取之后的第二次本地核对即来源重读结束，此刻时钟已走到 ended。
        local(self)
        calls.append(1)
        if len(calls) == 2:
            clock[0] = ended.timestamp()
    monkeypatch.setattr(MingdaoBackend, "_local", reread_done)
    monkeypatch.setattr(order, "CreateRequest", partial(order.CreateRequest.func, connect=platform.connection))
    arm(case, platform)
    handed = []
    if change == "same_day":
        assert mingdao.send(plan, lambda: handed.append(1)) == case.ids["created"]
        assert handed == [1] and platform.create_count == 1
        return
    with pytest.raises(NotSent) as stopped:
        mingdao.send(plan, lambda: handed.append(1))
    assert stopped.value.code == "reapprove" and handed == [] and platform.create_count == 0 and len(calls) == 2


def test_readback_compares_approved_business_values(case, platform, business_cli, web_order):
    rules, _, _ = web_order
    mingdao = backend(case)
    plan = mingdao.plan(card_snapshot(case), 1, URL, now=time.time(), sent=time.time())
    case.readback_change = lambda row: row[rules["标签与适用条件关系"]["control"]][0].update(value="有适用条件")
    arm(case, platform)
    record_id = mingdao.send(plan, lambda: None)
    verdict, checks = mingdao.readback(plan, record_id, 30)
    assert verdict == "写错" and [c["字段名"] for c in checks if c["结果"] != "一致"] == ["标签与适用条件关系"]


@pytest.mark.parametrize("which", ["skipped", "regular"])
def test_readback_missing_written_field_is_mismatch(case, platform, business_cli, web_order, which):
    """修复轮 2 第 4 条：回读缺 1 个已写字段（readback_skip 中的或普通的）→ 采用 X5 compare 逐项结果，判「写错」（不一致），
    不得汇总成「一致」；应回读字段与逐项结果一一对应。"""
    rules, _, _ = web_order
    mingdao = backend(case)
    plan = mingdao.plan(card_snapshot(case), 1, URL, now=time.time(), sent=time.time())
    control = case.config["readback_skip"][0] if which == "skipped" else rules["文本"]["control"]
    assert control in {c["controlId"] for c in plan.extra["controls"]}
    case.readback_change = lambda row: row.pop(control)
    arm(case, platform)
    record_id = mingdao.send(plan, lambda: None)
    verdict, checks = mingdao.readback(plan, record_id, 30)
    assert verdict == "写错" and [c["字段名"] for c in checks] == plan.request_fields
    assert [c["字段名"] for c in checks if c["结果"] != "一致"] == [case.config["fields"][control]["label"]]


def test_formal_authorization_bound_by_config_digest(case, platform, business_cli, web_order):
    """正式单授权登记：与配置同目录 create-authorizations.json 中 is_test 为假、config_sha256 相符的唯一一条；
    缺失、不符、公式指纹不符或缺项 → 不装配（建单 0）。装配后配置或登记变化 → 交接前复核拒绝、0 发。"""
    private = case.private
    config = private / "config.json"
    order.exclusive(config, case.config)
    order.exclusive(private / "mingdao-state.json", {"cookies": [{
        "name": case.config["create"]["session_cookie"], "value": case.session_token, "domain": ".mingdao.com",
        "path": "/", "secure": False, "expires": -1}]})
    entry = {**formal_evidence(case), "config_sha256": order.digest(config.read_bytes())}
    registry = private / "create-authorizations.json"

    def build(content):
        if registry.exists():
            registry.unlink()
        if content is not None:
            order.exclusive(registry, content)
        return build_order_runner(
            SimpleNamespace(mingdao_config=config),
            SimpleNamespace(trial_credentials_complete=True, trial_login_dir=private, trial_credentials=(
                ("PLACEHOLDER_OFFICIAL_TOKEN", case.official_token),
                ("PLACEHOLDER_OFFICIAL_APP", case.ids["official_app"]))),
            private / "trial-050.sqlite3", switches=lambda: {"MAIL_TRIAL_CREATE_ENABLED": "true",
                                                              "MAIL_TRIAL_CARD_ENABLED": "true"},
            connected=lambda: True, sensitive=(case.session_token,), archive_runner=object())

    missing = dict(entry)
    del missing["tariffs_scope"]
    for content in (None, {"formal": {**entry, "web_rules_sha256": "0" * 64}}, {"formal": {**entry, "is_test": True}},
                    {"formal": {**entry, "config_sha256": "0" * 64}}, {"a": entry, "b": dict(entry)},
                    {"formal": missing}):
        assert build(content) is None
    runner = build({"x5-test": case.evidence, "formal": entry})
    assert isinstance(runner, OrderRunner) and runner.backend.evidence == entry and runner.backend.web
    plan = runner.backend.plan(card_snapshot(case), 1, URL, now=time.time(), sent=time.time())
    registry.unlink()
    order.exclusive(registry, {"formal": {**entry, "tag_scope": {**entry["tag_scope"], "view": "other"}}})
    arm(case, platform)
    with pytest.raises(NotSent) as stopped:
        runner.backend.send(plan, lambda: pytest.fail("复核不符仍进入交接"))
    assert stopped.value.code == "plan" and platform.create_count == 0


# ---------------------------------------------------------------- 建单请求体 appId 字段级豁免（复核 rv-2d5 第 4 问）

def feishu_outlet(case, monkeypatch):
    """群消息出口替身：与生产同一 checked_transport，试运行凭据含官方令牌与应用 ID；投递走进程内替身 HTTP。"""
    from unittest.mock import AsyncMock

    import httpx

    from qianshu.feishu import sdk
    from qianshu.feishu.trial_listener import ApprovalSettings, checked_transport
    settings = ApprovalSettings("app", "private-app-key", "group", "bot", frozenset({"owner"}), trial_credentials=(
        ("PLACEHOLDER_OFFICIAL_TOKEN", case.official_token), ("PLACEHOLDER_OFFICIAL_APP", case.ids["official_app"])),
        trial_credentials_complete=True)
    sender = checked_transport(settings)
    sender._tenant_token = AsyncMock(return_value="tenant")
    posts = []

    def http(request):
        posts.append(request.content.decode())
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "receipt"}})
    client = httpx.AsyncClient
    monkeypatch.setattr(sdk.httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(http), **kw))
    return sender, posts


def test_same_app_id_formal_order_sends_once_and_feishu_outlets_still_block(case, platform, business_cli, web_order,
                                                                           monkeypatch):
    """同值配置：明道云建单请求体顶层 appId 与官方应用 ID 逐字相等 → 按该凭据自有位置放行，发 1 次、逐字段回读一致；
    同一用例内，群公告 / 卡片带上该值仍按凭据拦截、0 投递，日志脱敏。"""
    import io
    import logging

    from qianshu.feishu.trial_listener import send_trial_message
    from qianshu.logs import JsonStreamHandler
    app_id = case.ids["official_app"]
    mingdao = backend(case)
    plan = mingdao.plan(card_snapshot(case), 1, URL, now=time.time(), sent=time.time())
    assert json.loads(plan.payload)["appId"] == app_id and platform.create_count == 0
    arm(case, platform)
    handed = []
    record_id = mingdao.send(plan, lambda: handed.append(1))
    assert handed == [1] and platform.create_count == 1 and record_id == case.ids["created"]
    verdict, checks = mingdao.readback(plan, record_id, 30)
    assert verdict == "一致" and [c["字段名"] for c in checks] == plan.request_fields
    assert all(c["结果"] == "一致" for c in checks)
    # 飞书出口：同一值进群公告或卡片 → 按凭据拦截，不领投递、不发请求；对照公告照常投递。
    sender, posts = feishu_outlet(case, monkeypatch)
    claims = []
    with pytest.raises(order.CredentialConflict):
        send_trial_message(sender, "条目 1 · 建单完成，应用 " + app_id, "notice", False, lambda: claims.append(1))
    with pytest.raises(order.CredentialConflict):
        send_trial_message(sender, {"excerpt": "应用" + app_id}, "card", True, lambda: claims.append(1))
    assert posts == [] and claims == []
    assert send_trial_message(sender, "条目 1 · 建单完成", "plain", False, lambda: claims.append(1)) == "receipt"
    assert len(posts) == 1 and app_id not in posts[0] and claims == [1]
    stream = io.StringIO()
    log = logging.getLogger("test.formal.same_app_id")
    log.addHandler(JsonStreamHandler(stream, sensitive_values={"PLACEHOLDER_OFFICIAL_APP": app_id}))
    log.propagate = False
    log.warning("建单完成 %s", app_id)
    assert stream.getvalue() and app_id not in stream.getvalue()


@pytest.mark.parametrize("change", ["other_field", "partial", "token", "superset", "different_app_fragment",
                                    "different_token_fragment"])
def test_app_id_exemption_bounds_stop_before_sending(case, platform, business_cli, web_order, change):
    """必停：同值进请求体其他字段；部分包含；官方令牌（PAT）进请求体；appId 与应用 ID 不逐字相等时不给豁免、照常按凭据
    检查（守望者 16:4xZ 口径更正）——超集、含应用 ID 或令牌 8 字符片段的另一个值照停 → 规划阶段即按凭据冲突停，0 发。"""
    app_id, identifiers = case.ids["official_app"], case.config["create"]["identifiers"]
    if change == "other_field":
        identifiers["projectId"] = app_id
    elif change == "partial":
        identifiers["projectId"] = "p-" + app_id[:12]
    elif change == "token":
        identifiers["projectId"] = case.official_token
    elif change == "superset":
        identifiers["appId"] = app_id + "0"
    elif change == "different_app_fragment":
        identifiers["appId"] = "x-" + app_id[:12]
    else:
        identifiers["appId"] = "x-" + case.official_token[:12]
    assert identifiers["appId"] != app_id or change in ("other_field", "partial", "token")
    with pytest.raises(order.OutletConflict):
        backend(case).plan(card_snapshot(case), 1, URL, now=time.time(), sent=time.time())
    assert platform.create_count == 0


def test_different_app_id_is_not_exempt_but_checked_and_sends(case, platform, business_cli, web_order):
    """原「另一个值 → 停」改为放行（守望者 16:4xZ 口径更正）：appId 与官方应用 ID 完全不同、且不含任何凭据 8 字符片段
    → 不给豁免、照常检查整个请求体，无凭据即放行；发 1 次、逐字段回读一致。"""
    app_id = case.ids["official_app"]
    other = str(uuid4())
    assert not any(other[i:i + 8] in app_id or other[i:i + 8] in case.official_token for i in range(len(other) - 7))
    case.ids["appId"] = other
    case.config["create"]["identifiers"]["appId"] = other
    mingdao = backend(case)
    plan = mingdao.plan(card_snapshot(case), 1, URL, now=time.time(), sent=time.time())
    assert json.loads(plan.payload)["appId"] == other and app_id not in plan.payload.decode()
    arm(case, platform)
    handed = []
    record_id = mingdao.send(plan, lambda: handed.append(1))
    assert handed == [1] and platform.create_count == 1 and record_id == case.ids["created"]
    verdict, checks = mingdao.readback(plan, record_id, 30)
    assert verdict == "一致" and all(c["结果"] == "一致" for c in checks)


def test_short_credential_stops_before_any_request(case, platform, business_cli, web_order):
    """任一鉴权值短于 8 字符（出口按 8 字符片段识别，短值无法拦截）→ 规划即停，官方读取与建单均 0 次。"""
    case.config["create"]["identifiers"]["appId"] = "app1234"
    with pytest.raises(order.OutletConflict):
        backend(case, official_app_id="app1234").plan(card_snapshot(case), 1, URL, now=time.time(), sent=time.time())
    assert platform.create_count == 0 and "template" not in platform.actions


def test_send_exemption_bound_to_app_id_field(case, platform, business_cli, web_order):
    """发送出口单独复演：规划通过后，请求体 projectId 被换成应用 ID（同值出现在豁免字段以外）→ 预建连出口按凭据冲突停，
    确证未发出（NotSent），0 发。"""
    mingdao = backend(case)
    plan = mingdao.plan(card_snapshot(case), 1, URL, now=time.time(), sent=time.time())
    body = json.loads(plan.payload)
    body["projectId"] = case.ids["official_app"]
    plan.payload = order.compact(body).encode()
    arm(case, platform)
    with pytest.raises(NotSent):
        mingdao.send(plan, lambda: pytest.fail("凭据冲突仍进入交接"))
    assert platform.create_count == 0
