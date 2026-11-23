"""建单来源与回读的合成验收。"""
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


@pytest.mark.parametrize("change", ["price", "template", "catalog", "allowlist", "tags"])
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
        return httpx.Response(200, json={"success": True, "error_code": 0, "error_msg": "成功",
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
def web_order(case, platform, business_cli, monkeypatch):
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
    queries = []
    check_cards = []
    def review_card(card):
        check_cards.append(card)
        return "manual"
    monkeypatch.setattr(order, "_Transport", lambda *args: type("ReviewTransport", (), {"send_card": staticmethod(review_card)})())
    case.review_cards = check_cards
    base = platform.official
    def exchange(request):
        if request.url.path.endswith("/rows/list") and request.url.path != case.config["official"]["search"]["path"]:
            queries.append(json.loads(request.content))
            return platform.response({"rows": rows, "total": len(rows), "snapshotVersion": "第一版",
                "uniqueConstraint": {"field": "全名", "coverage": "full_table", "enforced": True,
                                     "version": "第一版", "validUntil": case.evidence["tag_scope"]["unique_until"]}})
        response = base(request)
        if request.url.path == case.config["official"]["search"]["path"]:
            payload = response.json()
            payload["data"]["total"] = len(payload["data"]["rows"])
            return httpx.Response(200, json=payload)
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


def test_web_fields_card_request_and_complete_readback(case, platform, business_cli, web_order):
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


def test_web_task_reads_unique_status_column(case, platform, business_cli, web_order):
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
