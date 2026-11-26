"""建单来源与回读的合成验收。"""
from copy import deepcopy
from datetime import UTC, datetime
import json

import httpx
import pytest

from test_create_order import (
    Approvals, business_cli, case, events, offer_export, order, platform, private_export,
    replace_order_quotes, run_case, synthetic_uuids,
)


def test_fixed_amount_derived_without_amount_evidence_stops(case, platform, business_cli):
    export = offer_export(case, platform, "特价优惠")
    draft = export["drafts"][0]
    results = draft["field_results"]
    for key in ("tariff", "tariff_multiplier"):
        results[key] = {"evidence": [], "statement": {"value": None}}
    result = results["recharge_amount"]
    result.update(source="查表派生", evidence=results["amount_mode"]["evidence"],
                  input_fields=["start_date", "amount_mode", "tariff", "tariff_multiplier"])
    result["statement"].update(value=None, status="原文未写", evidence=[])
    draft["recharge_amount"]["evidence"] = draft["amount_mode"]["evidence"]
    code, error = business_cli.run(export)
    assert platform.create_count == 0
    assert code == 1 and "recharge_amount" in error


@pytest.mark.parametrize("clipped", [False, True])
def test_short_package_name_only_in_long_catalog_name_stops(case, platform, business_cli, clipped):
    snapshot = case.config["order_snapshot"]
    base = snapshot["package_levels"].splitlines()[1]
    snapshot["package_levels"] += "\n" + base.replace("合成当前包", "合成当前包 Plus").replace(
        case.ids["package"], case.ids["template"])
    export = private_export(case.values)
    text = "合成当前包 Plus"
    replace_order_quotes(export, "packages", text, [(0, len("合成当前包") if clipped else len(text))])
    code, error = business_cli.run(export)
    assert platform.create_count == 0
    assert code == 1 and "节目包" in error


@pytest.mark.parametrize("change", ["price", "missing", "identifier"])
def test_readback_checks_every_package_cell(case, platform, change):
    def modified(control, rows):
        if control == case.control["packages"]:
            if change == "price":
                rows[0][case.child["price"]] = "4"
            elif change == "missing":
                del rows[0][case.child["price"]]
            else:
                rows[0][case.child["current"]][0]["sid"] = case.ids["template"]
        return platform.relation_response(rows)

    platform.created_relation_response = modified
    assert not run_case(case, platform, Approvals(platform))
    assert platform.create_count == 1 and "delete" not in platform.actions


@pytest.fixture
def registered_entry(monkeypatch):
    """在内存存根上验收共用门禁。原命令行另测启动拒绝。"""
    def enter(args, directory, clock, guard):
        authorizations = json.loads(order.private_bytes(directory / "create-authorizations.json"))
        return order._run_registered(args, directory, clock, guard, order.private_bytes(args.config), authorizations)
    monkeypatch.setattr(order, "_run", enter)


@pytest.mark.parametrize("change", ["price", "template", "catalog", "allowlist", "tags"])
@pytest.mark.usefixtures("registered_entry")
def test_current_sources_checked_after_card(case, platform, business_cli, change):
    snapshot = case.config["order_snapshot"]
    snapshot["verified_combinations"] = [[case.values["policy_type"], case.values["benefit_type"]]]
    def change_source():
        if change == "price":
            platform.relations[case.control["packages"]][0][case.child["price"]] = "4"
        elif change == "template":
            platform.template[case.control["approval_url"]] += "/changed"
        else:
            path = case.private / "config.json"
            config = json.loads(path.read_bytes())
            key = {"catalog": "package_levels", "tags": "tags", "allowlist": "verified_combinations"}[change]
            config["order_snapshot"][key] = []
            path.write_text(json.dumps(config))
    platform.before_search = change_source
    business_cli.run()
    assert len(business_cli.summaries) == 1 and platform.create_count == 0
    assert events(case)[-1] == "approval_closed"


@pytest.mark.parametrize(("mode", "expected_pages"), [("description", 1), ("reduced", 2), ("empty", 2)])
def test_search_page_contract(case, mode, expected_pages):
    cfg = case.config["official"]
    pages = []
    def respond(request):
        page = json.loads(request.content)["pageIndex"]
        pages.append(page)
        ids = [case.ids["template"], case.ids["created"]]
        rows = [{"rowId": ids[page-1], cfg["search"]["name_control"]: "相近"}]
        if mode == "empty" and page == 2:
            rows = []
        total = 1 if mode == "description" else 2
        return httpx.Response(200, json={"success": True, "error_code": 1, "error_msg": "成功",
            "data": {"rows": rows, "total": total, "pageIndex": page, "pageSize": 1, "hasMore": page < total}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        api = order.Official(cfg, case.official_token, case.ids["official_app"], client)
        if mode == "empty":
            with pytest.raises(order.Stop):
                api.find("查重")
        else:
            assert api.find("查重") == []
    assert pages == list(range(1, expected_pages + 1))


def test_conflicting_near_names_across_views(case, platform):
    spec = case.config["official"]["search"]
    spec["view_ids"].append(case.ids["additional_view"])
    for i, view in enumerate(spec["view_ids"]):
        platform.query_pages[view, 1] = [{"rowId": case.ids["template"], spec["name_control"]: f"相近{i}"}]
    assert not run_case(case, platform, Approvals(platform))
    assert platform.create_count == 0


@pytest.mark.parametrize("pairs", [None, [], [["定向政策", "升级权益"]], [["公开政策", "赠送时长"]]])
def test_trial_allowlist_requires_exact_pair(pairs):
    snapshot = {} if pairs is None else {"verified_combinations": pairs}
    with pytest.raises(order.Stop, match="该类型尚未经真实回读验证"):
        order.require_verified_combination(snapshot, "定向政策", "赠送时长")
    order.require_verified_combination({"verified_combinations": [["定向政策", "赠送时长"]]}, "定向政策", "赠送时长")


@pytest.mark.parametrize("outcome", ["attempt", "failed", "expired"])
def test_trial_registration_closed_across_reopen(case, outcome):
    entry = order.TrialRegistration("本次", "批准回执", "【测试】本次", "定向政策", "赠送时长", case.window.end.isoformat())
    now = case.window.start.timestamp()
    if outcome == "attempt":
        entry.consume(case.private, "【测试】本次", "定向政策", "赠送时长", now)
    elif outcome == "failed":
        with pytest.raises(order.Stop):
            entry.consume(case.private, "【测试】本次", "公开政策", "赠送时长", now)
    else:
        with pytest.raises(order.Stop):
            entry.consume(case.private, "【测试】本次", "定向政策", "赠送时长", case.window.end.timestamp())
    with pytest.raises(order.Stop, match="关闭"):
        entry.consume(case.private, "【测试】本次", "定向政策", "赠送时长", now)
    assert "verified_combinations" not in case.config.get("order_snapshot", {})


WEB_TYPES = {
    "promotionName": 2, "mem": 2, "promotionStartDate": 15, "promotionEndDate": 15,
    "原优惠政策到期日期": 15, "companyId": 6, "singlePurchaseTime": 6, "cycleGivingTime": 6,
    "effectiveMinMultiple": 6, "标签与适用条件关系": 11, "文本": 2, "同期有效优惠政策": 34,
    "billingCycle": 6, "升包执行任务状态值": 2,
}


@pytest.fixture
def web_order(case, platform, business_cli, monkeypatch, registered_entry):
    from test_create_order import company_relation

    company, display, matched = company_relation(case)
    matched["企业编号"] = "12"
    platform.template[company] = case.cells[company]
    platform.relations[company] = case.relations[company]
    snapshot = case.config["order_snapshot"]
    field_rules = {}
    for i, (name, kind) in enumerate(WEB_TYPES.items()):
        key = f"web-{i}"
        case.config["fields"][key] = {"label": name, "type": kind}
        platform.template[key] = None
        field_rules[name] = {"control": key, "formula": name}
    case.config["fields"][field_rules["同期有效优惠政策"]["control"]]["children"] = {"政策": {"type": 29}}
    field_rules["同期有效优惠政策"].update(child="政策")
    field_rules["标签与适用条件关系"].update(default={"key": "none", "value": "无适用条件"})
    platform.template[field_rules["标签与适用条件关系"]["control"]] = [{"key": "none", "value": "无适用条件"}]
    field_rules["文本"].update(text="合成提示")
    field_rules["companyId"].update(column="企业编号")
    field_rules["billingCycle"].update(cycles={"日": 1, "月": 0})
    field_rules["singlePurchaseTime"].update(empty_input="")
    field_rules["升包执行任务状态值"].update(inactive_value="")
    snapshot["template_sources"] = {case.control["generated"]: {"label": "回填字段", "type": 2, "source": "模板复制"}}
    snapshot["member_names"] = {case.ids["member"]: "登记申请人"}
    snapshot.update(web_rules=field_rules, condition_scenario={"policy": case.values["policy_name"], "condition": "无适用条件"}, tariffs=[
        {"package": "合成当前包", "price_plan": "3.00", "unit": "日", "start": "2000-01-01", "end": "2100-01-01"}],
        concurrent_policies={"complete": True, "rows": [], "as_of": case.window.start.isoformat(), "total": 0, "valid_until": case.window.end.isoformat()})
    case.config["fields"][case.control["packages"]]["price_plan_control"] = case.child["price"]
    case.config["order_snapshot"]["verified_combinations"] = [["定向政策", "赠送时长"]]
    tag_scope = {"path": "/v3/app/worksheets/labels/rows/list", "view": "whole", "name_control": "全名"}
    case.config["official"]["tag_search"] = tag_scope.copy()
    case.evidence.update(web_rules_sha256=order.digest(order.compact(field_rules).encode()),
                         tag_scope={**tag_scope, "origin": case.config["official"]["origin"],
                                    "app_sha256": order.digest(case.ids["official_app"].encode()),
                                    "coverage": "full_table", "snapshot": "第一版",
                                    "unique_until": case.window.end.isoformat()})
    rows = [{"rowId": case.ids["tag"], "全名": case.values["user_tags"][0]}]
    case.evidence["concurrent_scope"] = {
        **case.evidence["tag_scope"], "path": case.config["official"]["search"]["path"], "view": "all-policies",
        "columns": {name: name for name in ("name", "company", "end", "created_at")},
    }
    snapshot["tags"][0]["complete"] = True
    rows[0].update(end=snapshot["tags"][0]["end"], shadow=False, complete=True)
    case.evidence["tag_scope"]["columns"] = {"name": "全名", "end": "end", "shadow": "shadow", "complete": "complete"}
    case.config["fields"][case.control["packages"]]["price_plan_id_column"] = "计划记录"
    platform.relations[case.control["packages"]][0]["计划记录"] = case.ids["template"]
    snapshot["tariffs"][0].update(id=case.ids["package"], price_plan_id=case.ids["template"], amount=3, version="第一版", cycle=1)
    case.rate_rows = deepcopy(snapshot["tariffs"])
    case.task_rows = {}
    case.readback_change = lambda row: None
    case.target_reads = []
    platform.template[case.control["member"]] = [{"accountId": case.ids["member"], "name": "登记申请人"}]
    scope = case.evidence["tag_scope"]
    for table in ("tariffs", "tasks"):
        path = f"/v3/app/worksheets/{table}/rows/" + ("list" if table == "tariffs" else "{record_id}")
        case.config["official"][table] = {"path": path}
        case.evidence[table + "_scope"] = {"path": path, "origin": scope["origin"], "app_sha256": scope["app_sha256"]}
    case.evidence["tariffs_scope"].update(view="全部资费", snapshot="第一版", coverage="full_table",
        columns={k: k for k in ("package", "price_plan", "price_plan_id", "start", "end", "unit")})
    case.evidence["tariffs_scope"]["columns"].update(amount="amount", version="version", cycle="cycle")
    for action, columns in (("products", {"name": "name"}), ("plans", {"name": "name"}),
                            ("company_rows", {"name": display, "number": "企业编号"})):
        path = f"/v3/app/worksheets/{action}/rows/{{record_id}}"
        case.config["official"][action] = {"path": path}
        case.evidence[action + "_scope"] = {"path": path, "origin": scope["origin"],
                                             "app_sha256": scope["app_sha256"], "columns": columns}
    case.tag_version = "第一版"
    queries = []
    check_cards = []
    def review_card(card):
        check_cards.append(card)
        return "manual"
    monkeypatch.setattr(order, "_Transport", lambda *args: type("ReviewTransport", (), {"send_card": staticmethod(review_card)})())
    case.review_cards = check_cards
    base = platform.official
    def exchange(request):
        path = request.url.path
        row_id = path.split("/")[-1]
        if "/products/rows/" in path:
            product, = [entry for r in platform.relations[case.control["packages"]]
                        for role in ("current", "gift") for entry in r[case.child[role]] if entry["sid"] == row_id]
            return platform.response({"rowId": row_id, "name": product["name"]})
        if "/plans/rows/" in path:
            rate = next(r for r in case.rate_rows if r["price_plan_id"] == row_id)
            return platform.response({"rowId": row_id, "name": rate["price_plan"]})
        if "/company_rows/rows/" in path:
            return platform.response(matched)
        if "/tariffs/rows/" in path and row_id != "list":
            rate, = [r for r in case.rate_rows if r["id"] == row_id]
            return platform.response({"rowId": row_id, **rate})
        for policy in snapshot["concurrent_policies"]["rows"]:
            if path == case.config["official"]["read"]["path"].replace("{record_id}", policy["id"]) and policy["company"] == case.values["company"]:
                return platform.response({"rowId": policy["id"], **policy})
        if path == case.config["official"]["tariffs"]["path"]:
            return platform.response({"rows": [{"rowId": r["id"], **r} for r in case.rate_rows],
                                      "total": len(case.rate_rows), "snapshotVersion": "第一版"})
        if path.startswith("/v3/app/worksheets/tasks/rows/"):
            return platform.response(case.task_rows[path.split("/")[-1]])
        if path == case.config["official"]["tag_search"]["path"].removesuffix("list") + case.ids["tag"]:
            case.target_reads.append(((events(case) or ["preflight"])[-1], platform.create_count))
            return platform.response(rows[0])
        if (request.url.path == case.config["official"]["search"]["path"]
                and json.loads(request.content)["viewId"] == "all-policies"):
            candidates = snapshot["concurrent_policies"]["rows"]
            return platform.response({"rows": [{"rowId": r["id"], **r} for r in candidates],
                                      "total": len(candidates), "snapshotVersion": "第一版"})
        if request.url.path.endswith("/rows/list") and request.url.path != case.config["official"]["search"]["path"]:
            queries.append(json.loads(request.content))
            return platform.response({"rows": rows, "total": len(rows), "snapshotVersion": case.tag_version,
                "uniqueConstraint": {"field": "全名", "coverage": "full_table", "enforced": True,
                                     "version": "第一版", "validUntil": case.evidence["tag_scope"]["unique_until"]}})
        response = base(request)
        if request.url.path == case.config["official"]["search"]["path"]:
            payload = response.json()
            payload["data"]["total"] = len(payload["data"]["rows"])
            return httpx.Response(200, json=payload)
        if (response.json().get("success") and request.method == "GET"
                and path.endswith("/" + case.ids["created"])):
            row = response.json()["data"]
            for key, cell in platform.template.items():
                spec = case.config["fields"].get(key, {})
                if spec.get("type") in (9, 10, 11, 26) and key in row and row[key] not in ("", "[]"):
                    if key != case.control["status"]:
                        row[key] = cell
            concurrent = field_rules["同期有效优惠政策"]["control"]
            if row[concurrent] != "[]":
                row[concurrent] = [{"政策": [{"sid": r["id"], "name": r["name"]}]}
                    for r in snapshot["concurrent_policies"]["rows"] if r["company"] == case.values["company"]]
            case.readback_change(row)
            return platform.response(row)
        return response
    from functools import partial
    monkeypatch.setattr(httpx, "Client", partial(httpx.Client.func, transport=httpx.MockTransport(exchange)))
    return field_rules, rows, queries


@pytest.mark.parametrize("name", list(WEB_TYPES))
def test_missing_web_rule_stops(case, platform, business_cli, web_order, name):
    rules, _, _ = web_order
    del rules[name]
    case.evidence["web_rules_sha256"] = order.digest(order.compact(rules).encode())
    code, _ = business_cli.run()
    assert code == 1 and platform.create_count == 0 and not business_cli.summaries
    card, = case.review_cards
    assert name in str(card) and "button" not in str(card)


@pytest.mark.parametrize("failure", ["template", "multiplier", "table", "view", "duplicate"])
def test_trial_preflight_reproductions(case, platform, business_cli, web_order, failure):
    rules, rows, queries = web_order
    if failure == "template":
        del platform.template[rules["promotionName"]["control"]]
    elif failure == "multiplier":
        platform.template[case.control["effective_multiplier"]] = case.values["effective_multiplier"] = 2
    elif failure in ("table", "view"):
        spec = case.config["official"]["tag_search"]
        if failure == "table":
            spec["path"] = spec["path"].replace("labels", "alternative")
        else:
            spec["view"] = "alternative"
    else:
        rows.append({**rows[0], "rowId": case.ids["template"]})
    code, _ = business_cli.run()
    assert code == 1 and not business_cli.summaries and platform.create_count == 0
    if failure in ("table", "view"):
        assert not queries


@pytest.fixture
def web_clock(request, monkeypatch):
    if request.param is None:
        return None
    instant = datetime(2026, 11, request.param, 12, tzinfo=UTC)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz):
            return instant.astimezone(tz)

    monkeypatch.setattr("test_create_order.datetime", FrozenDatetime)
    return instant


@pytest.mark.parametrize("web_clock", [None, 25, 26, 29], indirect=True,
                         ids=["today", "11-25", "11-26", "11-29"])
def test_web_fields_card_request_and_complete_readback(web_clock, case, platform, business_cli, web_order):
    if web_clock is not None:
        business_cli.now[0] = web_clock.timestamp()
    rules, _, queries = web_order
    code, error = business_cli.run()
    assert (code, error, platform.create_count) == (0, "", 1)
    expected = [case.values["policy_name"], case.values["user_scope"], case.values["start_date"],
                case.values["end_date"], case.values["end_date"], "12", "30", "31", "1", '["none"]',
                "合成提示", "[]", "1", ""]
    assert [platform.created_cells[rule["control"]] for rule in rules.values()] == expected
    assert all(name in business_cli.summaries[0] for name in rules)
    assert "3.00" in business_cli.summaries[0] and len(queries) >= 2
    assert events(case)[-1] == "delete_verified"


@pytest.mark.parametrize(("kind", "purchase", "giving"), [
    ("赠送时长", "30", "31"), ("升级权益", "30", "31"), ("特价优惠", "", "3"),
])
def test_web_days_per_benefit(case, platform, business_cli, web_order, kind, purchase, giving):
    rules, _, _ = web_order
    export = private_export(case.values) if kind == "赠送时长" else offer_export(case, platform, kind)
    code, error = business_cli.run(export)
    assert (code, error) == (0, "")
    assert platform.created_cells[rules["singlePurchaseTime"]["control"]] == purchase
    assert platform.created_cells[rules["cycleGivingTime"]["control"]] == giving


@pytest.mark.parametrize("failure", ["condition", "cycle", "candidate", "company", "unmapped", "empty_days"])
def test_web_field_lacks_source(case, platform, business_cli, web_order, failure):
    rules, _, _ = web_order
    snapshot = case.config["order_snapshot"]
    export = None
    if failure == "condition":
        snapshot["condition_scenario"] = "需改选"
    elif failure == "cycle":
        snapshot["tariffs"][0]["unit"] = "未知"
    elif failure == "candidate":
        snapshot["concurrent_policies"]["complete"] = False
    elif failure == "company":
        del platform.relations[case.control["company"]][-1]["企业编号"]
    elif failure == "unmapped":
        del case.config["fields"][rules["文本"]["control"]]
    else:
        export = offer_export(case, platform, "特价优惠")
        del rules["singlePurchaseTime"]["empty_input"]
        case.evidence["web_rules_sha256"] = order.digest(order.compact(rules).encode())
    assert business_cli.run(export)[0] == 1
    assert not business_cli.summaries and platform.create_count == 0


def test_second_page_relation_failure_keeps_record(case, platform, monkeypatch):
    fetch = order.Official._request
    pages = []
    def read_page(api, action, record_id="", **kw):
        if (action == "relations" and record_id == case.ids["created"]
                and kw.get("relation_control") == case.control["packages"]):
            page = kw["params"]["pageIndex"]
            pages.append(page)
            if page == 2:
                raise order.Stop("次页未确认")
            return {"rows": platform.created_relations[case.control["packages"]], "total": 2,
                    "pageSize": 1, "pageIndex": 1, "hasMore": True}
        return fetch(api, action, record_id, **kw)
    monkeypatch.setattr(order.Official, "_request", read_page)
    assert not run_case(case, platform, Approvals(platform))
    assert pages == [1, 2] and "delete" not in platform.actions


@pytest.mark.parametrize("decision", ["expire", "version", "collision"])
def test_tag_proof_must_cover_handoff(case, platform, business_cli, web_order, decision):
    _, rows, queries = web_order
    def invalidate():
        if decision == "expire":
            business_cli.now[0] += 5
        elif decision == "version":
            scope_path = case.private / "create-authorizations.json"
            entries = json.loads(scope_path.read_bytes())
            entries[case.ids["registration"]]["tag_scope"]["snapshot"] = "下一版"
            scope_path.write_text(json.dumps(entries))
        else:
            rows.append({**rows[0], "rowId": case.ids["template"]})
    if decision == "expire":
        from datetime import UTC, datetime
        case.evidence["tag_scope"]["unique_until"] = datetime.fromtimestamp(
            business_cli.now[0] + 4, UTC).isoformat()
    platform.before_search = invalidate
    assert business_cli.run()[0] == 1
    assert len(business_cli.summaries) == 1 and queries
    assert platform.create_count == 0 and events(case)[-1] == "approval_closed"


def test_unproven_template_field_cannot_enter_web_request(case, platform, business_cli, web_order):
    key = "unregistered"
    case.config["fields"][key] = {"label": "新增模板栏", "type": 2}
    platform.template[key] = "历史值"
    assert business_cli.run()[0] == 1
    assert not business_cli.summaries and platform.create_count == 0


def test_declared_readback_skip_does_not_authorize_cleanup(case, platform):
    platform.before_read = lambda row: row.update({case.control["generated"]: "其他值"})
    assert not run_case(case, platform, Approvals(platform))
    assert "delete" not in platform.actions


def test_detail_failure_records_full_unconfirmed_scope(case, platform):
    platform.before_read = lambda row: row.pop("rowId")
    with pytest.raises(order.Stop, match="官方回读未获确认"):
        run_case(case, platform, Approvals(platform))
    path, = case.private.glob("create-readback-*.json")
    report = json.loads(path.read_bytes())
    assert report["分类计数"] == {"无法核对": len(case.config["fields"])}
    assert len(report["字段比对"]) == report["应核总数"]
    assert "delete" not in platform.actions


def test_web_nonempty_concurrent_at_date_boundary(case, platform, business_cli, web_order):
    rules, _, _ = web_order
    snapshot = case.config["order_snapshot"]
    rows = snapshot["concurrent_policies"]["rows"]
    rows.extend([
        {"id": case.ids["tag"], "name": "同日结束", "company": case.values["company"], "end": case.values["start_date"]},
        {"id": case.ids["template"], "name": "其他主体", "company": "其他", "end": case.values["end_date"]},
    ])
    snapshot["concurrent_policies"]["total"] = len(rows)
    for row in rows:
        row["created_at"] = case.window.start.isoformat()
    code, error = business_cli.run()
    assert (code, error) == (0, "")
    written = platform.created_cells[rules["同期有效优惠政策"]["control"]]
    row, = order.subtable_rows(written)
    assert json.loads(row["政策"]) == [{"sid": case.ids["tag"]}]
    assert "同日结束" in business_cli.summaries[0] and "其他主体" not in business_cli.summaries[0]


def task_export(case, platform, web_order):
    rules, _, _ = web_order
    offer_export(case, platform, "升级权益")
    target = "合成目标包"
    case.values.update(after_offer="指定节目包", after_offer_package=target)
    control = case.control["after_offer"]
    option = platform.template[control][0]
    option["value"] = case.values["after_offer"]
    case.config["fields"][control]["draft_values"] = {order.compact(option["value"]): [option["key"]]}
    case.config["fields"]["target"] = {"label": "优惠到期目标包", "type": 2, "draft": "after_offer_package"}
    platform.template["target"] = target
    rules["升包执行任务状态值"]["column"] = "状态"
    case.evidence["web_rules_sha256"] = order.digest(order.compact(rules).encode())
    snapshot = case.config["order_snapshot"]
    snapshot.update(benefit_catalog={"package_names": [target]},
                    upgrade_task_id=case.ids["created"], upgrade_tasks=[{"id": case.ids["created"], "company": case.values["company"], "package": target, "状态": "待执行"}])
    export = private_export(case.values)
    text = "合成当前包可获合成受赠包"
    replace_order_quotes(export, "packages", text, [(0, len(text))])
    task, = snapshot["upgrade_tasks"]
    case.task_rows[task["id"]] = {"rowId": task["id"], **task}
    case.evidence["tasks_scope"].update(record_id=task["id"], columns={"package": "package", "company": "company", "status": "状态"})
    return export


def test_web_task_reads_unique_status_column(case, platform, business_cli, web_order):
    rules, _, _ = web_order
    export = task_export(case, platform, web_order)
    assert business_cli.run(export) == (0, "")
    assert platform.created_cells[rules["升包执行任务状态值"]["control"]] == "待执行"


def test_one_use_authorization_can_close_before_attempt(case):
    entry = order.TrialRegistration("本次", "批准回执", "【测试】本次", "定向政策", "赠送时长", case.window.end.isoformat())
    assert entry.close(case.private)
    assert not entry.close(case.private)
    with pytest.raises(order.Stop, match="关闭"):
        entry.consume(case.private, "【测试】本次", "定向政策", "赠送时长", case.window.start.timestamp())


def test_revoked_after_attempt_has_no_handoff(case, platform, business_cli, web_order, monkeypatch):
    append = order.Ledger.append
    def revoke(ledger, event, *args, **kw):
        append(ledger, event, *args, **kw)
        if event == "create_attempt":
            path = case.private / "config.json"
            config = json.loads(path.read_bytes())
            config["order_snapshot"]["verified_combinations"] = []
            path.write_text(json.dumps(config))
    monkeypatch.setattr(order.Ledger, "append", revoke)
    assert business_cli.run()[0] == 1
    assert platform.create_count == 0 and "create_attempt" in events(case)
    assert events(case)[-2:] == ["aborted_before_write", "approval_closed"]


def test_skipped_readback_and_workflow_counts_are_independent(case, platform, business_cli, web_order):
    case.config["order_snapshot"]["template_sources"][case.control["generated"]]["source"] = "工作流回填"
    assert business_cli.run() == (0, "")
    path, = case.private.glob("create-readback-*.json")
    report = json.loads(path.read_bytes())
    assert report["已核工作流字段"] == ["回填字段"]
    assert report["分类计数"] == {"一致": report["应核总数"]}


def test_trial_conflicting_tag_version_never_publishes(case, platform, business_cli, web_order):
    case.evidence["tag_scope"]["snapshot"] = "另一版"
    assert business_cli.run()[0] == 1 and not business_cli.summaries
    assert platform.create_count == 0


def test_trial_allowlist_does_not_release_other_pairs_or_released_card():
    snapshot = {"verified_combinations": [["定向政策", "赠送时长"]]}
    order.require_verified_combination(snapshot, "定向政策", "赠送时长")
    for policy, benefit in (("公开政策", "赠送时长"), ("定向政策", "升级权益")):
        with pytest.raises(order.Stop, match="该类型尚未经真实回读验证"):
            order.require_verified_combination(snapshot, policy, benefit)
    snapshot["verified_combinations"].clear()
    with pytest.raises(order.Stop, match="该类型尚未经真实回读验证"):
        order.require_verified_combination(snapshot, "定向政策", "赠送时长")


def test_full_table_page_requires_proof_of_write_time_uniqueness(case, platform, business_cli, web_order, monkeypatch):
    raw = order.Official._request
    def no_constraint(api, action, *args, **kw):
        data = raw(api, action, *args, **kw)
        if action == "tag_search":
            data.pop("uniqueConstraint")
        return data
    monkeypatch.setattr(order.Official, "_request", no_constraint)
    assert business_cli.run()[0] == 1 and platform.create_count == 0
    assert not business_cli.summaries and len(case.review_cards) == 1


@pytest.mark.parametrize("clipped", [False, True])
def test_exact_short_package_separately_in_original(case, platform, business_cli, clipped):
    snapshot = case.config["order_snapshot"]
    base = snapshot["package_levels"].splitlines()[1]
    snapshot["package_levels"] += "\n" + base.replace("合成当前包", "合成当前包 Plus").replace(
        case.ids["package"], case.ids["template"])
    export = private_export(case.values)
    text = "合成当前包 Plus，合成当前包"
    end = len("合成当前包") if clipped else len("合成当前包 Plus")
    replace_order_quotes(export, "packages", text, [(0, end), (text.rindex("合成当前包"), len(text))])
    assert business_cli.run(export) == (0, "")
    assert platform.create_count == 1


def test_fixed_amount_derived_matches_located_original(case, platform, business_cli):
    export = offer_export(case, platform, "特价优惠")
    draft = export["drafts"][0]
    results = draft["field_results"]
    for key in ("tariff", "tariff_multiplier"):
        results[key] = {"evidence": [], "statement": {"value": None}}
    results["recharge_amount"].update(source="查表派生",
                                     input_fields=["start_date", "amount_mode", "tariff", "tariff_multiplier"])
    assert business_cli.run(export) == (0, "") and platform.create_count == 1


@pytest.mark.parametrize("pairs", [None, []])
@pytest.mark.parametrize("policy", ["公开政策", "定向政策"])
@pytest.mark.parametrize("benefit", ["赠送时长", "升级权益", "特价优惠"])
def test_unverified_combinations_only_produce_review_card(pairs, policy, benefit):
    snapshot = {} if pairs is None else {"verified_combinations": pairs}
    with pytest.raises(order.UnverifiedCombination) as stopped:
        order.require_verified_combination(snapshot, policy, benefit)
    card = stopped.value.card()
    assert "该类型尚未经真实回读验证" in str(card) and "button" not in str(card)


def test_readback_report_write_failure_freezes_cleanup(case, platform, monkeypatch):
    write = order.exclusive
    def fail_report(path, value):
        if path.name.startswith("create-readback-"):
            raise OSError("写入失败")
        write(path, value)
    monkeypatch.setattr(order, "exclusive", fail_report)
    with pytest.raises(order.Stop, match="官方回读未获确认"):
        run_case(case, platform, Approvals(platform))
    assert events(case)[-1] == "readback_unconfirmed" and "delete" not in platform.actions
    with order.ledger_open(case.private, case.ids["registration"], case.values["policy_name"]) as ledger:
        with pytest.raises(order.Stop, match="全项回读尚未确认"):
            ledger.registered(case.ids["created"])


@pytest.mark.parametrize("change", ["attempt", "mismatch", "closed"])
def test_test_registration_race_keeps_single_use(case, change):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    entry = order.TrialRegistration("竞用", "回执", "【测试】本次", "定向政策", "赠送时长", case.window.end.isoformat())
    barrier = Barrier(2)
    def try_once(first):
        barrier.wait()
        if first and change == "closed":
            entry.close(case.private)
            return False
        name = "【测试】其他" if first and change == "mismatch" else "【测试】本次"
        try:
            entry.consume(case.private, name, "定向政策", "赠送时长", case.window.start.timestamp())
        except order.Stop:
            return False
        return True
    with ThreadPoolExecutor(2) as pool:
        wins = sum(pool.map(try_once, (True, False)))
    assert wins <= 1
    if change == "attempt":
        assert wins == 1
    assert not entry.close(case.private)


def test_concurrent_detail_count_is_not_a_subtable_payload(case, platform, business_cli, web_order):
    rules, _, _ = web_order
    control = rules["同期有效优惠政策"]["control"]
    platform.template[control] = 4
    assert business_cli.run() == (0, "")
    assert platform.created_cells[control] == "[]"


def test_failed_test_readback_never_registers_combination(case, platform, business_cli, web_order):
    case.config["order_snapshot"]["verified_combinations"] = []
    platform.mismatch = True
    assert business_cli.run()[0] == 1 and platform.create_count == 1
    assert "delete" not in platform.actions and case.ids["created"] in platform.rows
    snapshot = json.loads((case.private / "config.json").read_bytes())["order_snapshot"]
    with pytest.raises(order.UnverifiedCombination):
        order.require_verified_combination(snapshot, case.values["policy_type"], case.values["benefit_type"])


def test_short_package_also_checked_against_tariff_catalog(case, platform, business_cli):
    snapshot = case.config["order_snapshot"]
    snapshot["benefit_catalog"] = {"tariffs": [{"base_package": "合成当前包 Plus", "price_plan": "计划",
        "unit": "日", "version": "一", "amount": 3, "valid_from": "2000-01-01"}]}
    export = private_export(case.values)
    replace_order_quotes(export, "packages", "合成当前包 Plus", [(0, len("合成当前包"))])
    code, _ = business_cli.run(export)
    assert code == 1 and platform.create_count == 0


def test_readback_group_only_receives_counts_and_disposition(case, platform):
    platform.mismatch = True
    approval = Approvals(platform)
    assert not run_case(case, platform, approval)
    assert len(approval.notices) == 2
    counts, disposition = approval.notices
    assert "不一致 1 / 缺失 1" in counts and "已保留记录，需人工核对" in disposition
    for text in approval.notices:
        assert all(detail not in text for detail in (case.values["policy_name"], case.values["user_scope"],
                                                      case.ids["created"], "赠送天数"))


@pytest.mark.parametrize("part", ["draft", "sources"])
@pytest.mark.parametrize("stage", ["prepare", "handoff"])
def test_bound_material_changes_after_card(case, platform, business_cli, web_order, part, stage, monkeypatch):
    def change():
        path = case.private / "draft.json"
        export = json.loads(path.read_bytes())
        if part == "draft":
            export["drafts"][0]["user_scope"]["value"] = "其他范围"
        else:
            export["sources"][0]["text"] += "补充"
        path.write_text(json.dumps(export))
    if stage == "prepare":
        platform.before_search = change
    else:
        append = order.Ledger.append
        def after_prepare(ledger, event, *args, **kw):
            append(ledger, event, *args, **kw)
            if event == "create_attempt":
                change()
        monkeypatch.setattr(order.Ledger, "append", after_prepare)
    assert business_cli.run()[0] == 1
    assert ("create_attempt" in events(case)) == (stage == "handoff")
    assert platform.create_count == 0 and events(case)[-1] == "approval_closed"


def test_concurrent_candidate_added_after_card(case, platform, business_cli, web_order):
    def change():
        case.config["order_snapshot"]["concurrent_policies"]["rows"].append({
            "id": case.ids["created"], "name": "新增同期", "end": case.values["end_date"],
            "company": case.values["company"], "created_at": case.window.start.isoformat(),
        })
    platform.before_search = change
    assert business_cli.run()[0] == 1
    assert platform.create_count == 0 and events(case)[-1] == "approval_closed"


def test_condition_template_disagrees(case, platform, business_cli, web_order):
    rules, _, _ = web_order
    platform.template[rules["标签与适用条件关系"]["control"]] = [{"key": "none", "value": "有适用条件"}]
    assert business_cli.run()[0] == 1 and platform.create_count == 0


@pytest.mark.parametrize(("registered", "matched_id"), [("日计划", True), ("月计划", False), ("月计划", True)])
def test_billing_uses_platform_plan(case, platform, business_cli, web_order, registered, matched_id):
    rules, _, _ = web_order
    control = case.control["packages"]
    price = case.child["price"]
    plan = case.ids["template"]
    field = {"type": 29, "business_names": {order.compact([{"sid": plan}]): registered}}
    case.config["fields"][control]["children"][price] = field
    platform.relations[control][0][price] = [{"sid": plan, "name": "月计划"}]
    rate, = case.config["order_snapshot"]["tariffs"]
    rate["cycle"] = int(registered[0] == "日")
    case.rate_rows[0]["cycle"] = 0
    rate.update(price_plan=registered, price_plan_id=plan if matched_id else case.ids["tag"], unit=registered[0])
    case.rate_rows[0].update(price_plan="月计划", price_plan_id=plan, unit="月")
    code, _ = business_cli.run()
    if registered == "日计划" or not matched_id:
        assert code == 1 and platform.create_count == 0
    else:
        assert code == 0 and platform.created_cells[rules["billingCycle"]["control"]] == "0"
        assert registered in business_cli.summaries[0]


def test_billing_plan_relation_must_hold_one_plan(case, platform, business_cli, web_order):
    # G447：价格计划关联多于一条时停单，不得只取第一条（恢复 76bedd4 的单值检查）。
    control = case.control["packages"]
    price = case.child["price"]
    plan, other = case.ids["template"], case.ids["tag"]
    both = order.compact([{"sid": plan}, {"sid": other}])
    field = {"type": 29, "business_names": {order.compact([{"sid": plan}]): "月计划", both: "月计划"}}
    case.config["fields"][control]["children"][price] = field
    platform.relations[control][0][price] = [{"sid": plan, "name": "月计划"}, {"sid": other, "name": "年计划"}]
    rate, = case.config["order_snapshot"]["tariffs"]
    rate.update(cycle=0, price_plan="月计划", price_plan_id=plan, unit="月")
    case.rate_rows[0].update(cycle=0, price_plan="月计划", price_plan_id=plan, unit="月")
    code, out = business_cli.run()
    assert code == 1 and platform.create_count == 0
    assert "所选平台行与登记或模板不一致" in out


@pytest.mark.parametrize("moment", ["login", "handoff", "handoff_new_version"])
def test_tag_rename_at_transport(case, platform, business_cli, web_order, monkeypatch, moment):
    _, rows, _ = web_order
    def rename():
        rows[0]["全名"] = "改名"
        if moment == "handoff_new_version":
            case.tag_version = "新版"
    if moment.startswith("handoff"):
        platform.before_handoff = rename
    else:
        read = order.private_bytes
        def read_login(path):
            data = read(path)
            if path.name == "mingdao-state.json" and business_cli.summaries and "create_attempt" in events(case):
                rename()
            return data
        monkeypatch.setattr(order, "private_bytes", read_login)
    assert business_cli.run()[0] == 1
    assert platform.create_count == int(moment.startswith("handoff")) and "delete" not in platform.actions
    if moment.startswith("handoff"):
        path, = case.private.glob("create-readback-*.json")
        assert json.loads(path.read_bytes())["分类计数"]["不一致"] == 1
        with order.ledger_open(case.private, "新的登记", case.values["policy_name"]) as ledger:
            with pytest.raises(order.Stop, match="资格已占用"):
                ledger.check_available()
    else:
        assert events(case)[-1] == "approval_closed"


def test_clipped_amount_in_original(case, platform, business_cli):
    export = offer_export(case, platform, "特价优惠")
    results = export["drafts"][0]["field_results"]
    for key in ("tariff", "tariff_multiplier"):
        results[key] = {"evidence": [], "statement": {"value": None}}
    results["recharge_amount"].update(source="查表派生",
                                     input_fields=["start_date", "amount_mode", "tariff", "tariff_multiplier"])
    replace_order_quotes(export, "recharge_amount", "固定实付180元", [(5, 6)])
    assert business_cli.run(export)[0] == 1 and platform.create_count == 0


def test_complete_long_package(case, platform, business_cli):
    short = "合成当前包"
    long = short + " Plus"
    levels = case.config["order_snapshot"]["package_levels"]
    case.config["order_snapshot"]["package_levels"] = levels.replace(short, long) + "\n" + (
        levels.splitlines()[1].replace(case.ids["package"], case.ids["template"]))
    package, = case.values["packages"]
    linked = platform.relations[case.control["packages"]][0]
    for role in ("current", "gift"):
        field = role + "_package"
        package[field] = package[field].replace(short, long)
        linked[case.child[role]][0]["name"] = package[field]
    export = private_export(case.values)
    replace_order_quotes(export, "packages", long, [(0, len(long))])
    assert business_cli.run(export) == (0, "") and platform.create_count == 1


@pytest.mark.parametrize("marker", ["snapshot", "registration", "runtime"])
def test_legacy_x5_refuses_trial_configuration(case, platform, business_cli, marker):
    if marker == "snapshot":
        case.config["order_snapshot"]["verified_combinations"] = []
    elif marker == "registration":
        case.evidence["tag_scope"] = {}
    else:
        (case.private / "trial-config.json").touch()
    code, error = business_cli.run()
    assert (code, error) == (1, "已停止：050 配置已禁用原格式 X5 入口\n")
    assert platform.actions == [] and platform.create_count == 0


@pytest.mark.parametrize("source", ["authorization", "allowlist"])
def test_final_query_revocation(case, platform, business_cli, web_order, monkeypatch, source):
    original = order.Official.tag_index
    def query(api, *args, **kwargs):
        result = original(api, *args, **kwargs)
        if business_cli.summaries and "create_attempt" in events(case):
            path = case.private / ("config.json" if source == "allowlist" else "create-authorizations.json")
            data = json.loads(path.read_bytes())
            if source == "allowlist":
                data["order_snapshot"]["verified_combinations"] = []
            else:
                data.clear()
            path.write_text(json.dumps(data))
        return result
    monkeypatch.setattr(order.Official, "tag_index", query)
    assert business_cli.run()[0] == 1 and platform.create_count == 0
    assert events(case)[-1] == "approval_closed"


@pytest.mark.parametrize("source", ["rate", "task", "member"])
def test_current_platform_field_source(case, platform, business_cli, web_order, source):
    export = None
    if source == "rate":
        case.rate_rows[0]["unit"] = "月"
    elif source == "task":
        export = task_export(case, platform, web_order)
        platform.before_search = lambda: case.task_rows[case.ids["created"]].update(状态="已取消")
    else:
        platform.template[case.control["member"]][0]["name"] = "其他申请人"
    assert business_cli.run(export)[0] == 1 and platform.create_count == 0
    assert bool(business_cli.summaries) == (source == "task")


def test_deployment_marker_outside_cwd(case, platform, business_cli, monkeypatch):
    root = case.private.parent / "deployment"
    marker = root / ".private"
    marker.mkdir(parents=True)
    (root / "rules").symlink_to(order.Path(order.__file__).resolve().parents[3] / "rules")
    (marker / "trial-config.json").touch()
    monkeypatch.setattr(order, "__file__", str(root / "src/qianshu/mingdao/create_order.py"))
    monkeypatch.setattr(order, "Official", lambda *args: pytest.fail("原格式入口已构造明道云客户端"))
    assert business_cli.run() == (1, "已停止：050 配置已禁用原格式 X5 入口\n")
    assert platform.actions == []


@pytest.mark.parametrize("field", ["tag", "cycle", "condition", "member", "plan", "missing_name", "bad_number"])
def test_confirmed_business_readback(case, platform, business_cli, web_order, monkeypatch, field):
    returned = []
    run_order = order.run_order

    def record_result(*args, **kwargs):
        result = run_order(*args, **kwargs)
        returned.append(result.conclusion)
        return result

    monkeypatch.setattr(order, "run_order", record_result)
    rules, tags, _ = web_order
    if field == "tag":
        platform.before_handoff = lambda: tags[0].update(全名="改名")
    elif field in ("cycle", "bad_number"):
        case.readback_change = lambda row: row.update({rules["billingCycle"]["control"]: "0" if field == "cycle" else "?"})
    elif field == "condition":
        case.readback_change = lambda row: row[rules["标签与适用条件关系"]["control"]][0].update(value="有适用条件")
    elif field == "plan":
        key = case.control["packages"]
        price = case.child["price"]
        plan = case.ids["template"]
        case.config["fields"][key]["children"][price] = {
            "type": 29, "business_names": {order.compact([{"sid": plan}]): "月计划"}}
        platform.relations[key][0][price] = [{"sid": plan, "name": "月计划"}]
        case.rate_rows[0]["cycle"] = 0
        case.config["order_snapshot"]["tariffs"][0].update(cycle=0, price_plan="月计划", price_plan_id=plan, unit="月")
        case.rate_rows[0].update(price_plan="月计划", price_plan_id=plan, unit="月")
        def changed(control, rows):
            if control == key:
                rows[0][price][0]["name"] = "其他计划"
            return platform.relation_response(rows)
        platform.created_relation_response = changed
    else:
        def member(row):
            cell = row[case.control["member"]][0]
            if field == "missing_name":
                del cell["name"]
            else:
                cell["name"] = "其他申请人"
        case.readback_change = member
    assert business_cli.run()[0] == 1 and platform.create_count == 1
    assert "delete" not in platform.actions
    report = json.loads(next(case.private.glob("create-readback-*")).read_bytes())
    conclusion = "未确认" if field in ("missing_name", "bad_number") else "写错"
    assert returned == [conclusion]
    assert report["结论"] == conclusion
    assert [r["结论"] for r in report["逐项核对"] if r["结论"] != "一致"] == [conclusion]
    assert json.loads((case.private / "trial-orders.jsonl").read_text().splitlines()[-1])["结论"] == conclusion


@pytest.mark.parametrize(("field", "text", "span"), [
    ("recharge_amount", "固定实付－8元", (5, 6)), ("recharge_days", "充值1300天", (3, 5)),
])
def test_complete_numeric_evidence(case, platform, business_cli, web_order, field, text, span):
    export = offer_export(case, platform, "特价优惠") if field == "recharge_amount" else private_export(case.values)
    replace_order_quotes(export, field, text, [span])
    assert business_cli.run(export)[0] == 1 and platform.create_count == 0


def test_handoff_targeted_tag_read(case, platform, business_cli, web_order):
    assert business_cli.run() == (0, "")
    assert ("create_attempt", 0) in case.target_reads

@pytest.mark.parametrize(("text", "excerpt", "value", "complete"), [
    ("金额，＋１，３００．５０元", "＋１，３００．５０", 1300.5, True),
    ("金额＋１，３００．５０元", "３００．５０", 300.5, False),
    ("倍数＋．５", "＋．５", 0.5, True),
    ("赠送三十一天", "三十", 30, False),
    ("赠送若干天", "若干", 30, False),
])
def test_number_token_at_original_coordinates(text, excerpt, value, complete):
    sources = order.Sources([order.SourceBlock(id="body", source_id="body", start=0, text=text)])
    start = text.index(excerpt)
    quote = {"span": {"source_id": "body", "start": start, "end": start + len(excerpt)}, "text": excerpt}
    assert order.complete_number_in_source(value, quote, sources) is complete


def test_unreadable_marker_stops_before_client(case, platform, business_cli, monkeypatch):
    original = order.os.lstat
    def denied(path, *args, **kwargs):
        if order.Path(path).name == "trial-config.json":
            raise PermissionError(13, "合成拒绝")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(order.os, "lstat", denied)
    assert business_cli.run() == (1, "已停止：050 配置已禁用原格式 X5 入口\n")
    assert platform.actions == []


@pytest.mark.parametrize("symbol", ["－ ", "﹣", "– ", "∸ "])
def test_negative_near_quote(case, platform, business_cli, web_order, symbol):
    export = offer_export(case, platform, "特价优惠")
    text = "固定实付" + symbol + "8元"
    start = text.index("8")
    replace_order_quotes(export, "recharge_amount", text, [(start, start + 1)])
    assert business_cli.run(export)[0] == 1 and platform.create_count == 0


@pytest.mark.parametrize("change", [{"end": "2000-01-01"}, {"shadow": True}, {"complete": False}])
def test_official_tag_facts_before_card(case, platform, business_cli, web_order, change):
    web_order[1][0].update(change)
    assert business_cli.run()[0] == 1 and not business_cli.summaries and platform.create_count == 0


def test_official_package_spelling_before_card(case, platform, business_cli, web_order):
    row = platform.relations[case.control["packages"]][0]
    row[case.child["gift"]][0]["name"] = "合成当前包 copy"
    assert business_cli.run()[0] == 1 and not business_cli.summaries and platform.create_count == 0


@pytest.mark.parametrize("fault", ["list", "notice"])
def test_persistent_result_with_second_failure(case, platform, business_cli, web_order, monkeypatch, fault):
    if fault == "list":
        platform.before_handoff = lambda: web_order[1][0].update(全名="已改名")
        original = order.Official.tag_index
        def index(api, *args, **kwargs):
            if platform.create_count:
                raise order.Stop("全表未确认")
            return original(api, *args, **kwargs)
        monkeypatch.setattr(order.Official, "tag_index", index)
    else:
        case.readback_change = lambda row: row.update({web_order[0]["billingCycle"]["control"]: "0"})
        def unavailable(*args):
            raise RuntimeError("公告失败")
        monkeypatch.setattr(Approvals, "announce", unavailable, raising=False)
    results = []
    original_run = order.run_order
    def capture(*args, **kwargs):
        if fault == "notice":
            args[3].transport.announce = unavailable
        result = original_run(*args, **kwargs)
        results.append(result.conclusion)
        return result
    monkeypatch.setattr(order, "run_order", capture)
    assert business_cli.run()[0] == 1
    assert results == ["写错"] and "delete" not in platform.actions
    assert json.loads(next(case.private.glob("create-readback-*")).read_bytes())["结论"] == "写错"
    assert json.loads((case.private / "trial-orders.jsonl").read_text().splitlines()[-1])["结论"] == "写错"


def test_tariff_detail_wrong_plan_before_card(case, platform, business_cli, web_order, monkeypatch):
    request = order.Official._request
    def changed(api, action, *args, **kwargs):
        result = request(api, action, *args, **kwargs)
        if action == "tariffs" and kwargs.get("row_path"):
            result["price_plan_id"] = case.ids["gift_package"]
        return result
    monkeypatch.setattr(order.Official, "_request", changed)
    assert business_cli.run()[0] == 1 and not business_cli.summaries and platform.create_count == 0


@pytest.mark.parametrize("fault", ["same_name", "wrong_relation", None])
def test_amount_and_cycle_use_selected_rate(case, platform, business_cli, web_order, fault):
    snapshot = case.config["order_snapshot"]
    values = case.values
    ref = {"base_package": "合成当前包", "price_plan": "3.00", "unit": "月", "version": "第一版"}
    values.update(tariff=ref, tariff_multiplier=1)
    offer_export(case, platform, "特价优惠")
    values["amount_mode"] = "节目包资费"
    mode = case.control["amount_mode"]
    platform.template[mode][0]["value"] = values["amount_mode"]
    case.config["fields"][mode]["draft_values"] = {order.compact(values["amount_mode"]): [platform.template[mode][0]["key"]]}
    snapshot["benefit_catalog"] = {"tariffs": [{**ref, "amount": 8, "valid_from": "2000-01-01", "valid_until": "2100-01-01"}]}
    rate = {**case.rate_rows[0], "id": case.ids["tag"], "unit": "月", "amount": 8, "cycle": 0}
    if fault == "same_name":
        rate["price_plan_id"] = case.ids["gift_package"]
        case.rate_rows.append(rate)
    else:
        case.rate_rows[:] = [rate]
    snapshot["tariffs"] = deepcopy(case.rate_rows)
    control = case.control["tariff"]
    row_id = case.ids["template"] if fault == "wrong_relation" else rate["id"]
    case.config["fields"][control].update(type=29, draft_values={order.compact(ref): [row_id]})
    platform.template[control] = [{"sid": row_id, "name": str(ref)}]
    export = private_export(values)
    text = "合成当前包可获合成受赠包"
    replace_order_quotes(export, "packages", text, [(0, len(text))])
    case.readback_change = lambda row: row.update({control: platform.template[control]})
    code, error = business_cli.run(export)
    if fault:
        assert code == 1 and not business_cli.summaries and platform.create_count == 0
    else:
        assert (code, error, platform.create_count) == (0, "", 1)
        assert platform.created_cells[control] == order.compact([{"sid": rate["id"]}])
        assert platform.created_cells[case.control["recharge_amount"]] == "8"
        assert platform.created_cells[web_order[0]["billingCycle"]["control"]] == "0"


def test_trial_marker_precedes_directory_mode(case, platform, business_cli, monkeypatch):
    root = case.private.parent / "deployment"
    (root / ".private").mkdir(parents=True)
    (root / ".private" / "trial-config.json").touch()
    monkeypatch.setattr(order, "__file__", str(root / "src/qianshu/mingdao/create_order.py"))
    case.private.chmod(0o750)
    assert business_cli.run() == (1, "已停止：050 配置已禁用原格式 X5 入口\n")
    assert platform.actions == []


@pytest.mark.parametrize("action", ["products", "plans", "company_rows", "tariffs", "tag_search"])
def test_detail_change_after_approval_stops(case, platform, business_cli, web_order, monkeypatch, action):
    read = order.Official._request
    def changed(api, name, row_id="", **kwargs):
        row = read(api, name, row_id, **kwargs)
        if name == action and row_id and "create_attempt" in events(case):
            if action == "company_rows":
                row["企业编号"] = "13"
            elif action == "tag_search":
                row["complete"] = False
            elif action == "tariffs":
                row["amount"] += 1
            else:
                row["name"] = "另一个名称"
        return row
    monkeypatch.setattr(order.Official, "_request", changed)
    assert business_cli.run()[0] == 1
    assert len(business_cli.summaries) == 1 and platform.create_count == 0
    assert events(case)[-1] == "approval_closed"
