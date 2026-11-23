import asyncio
import json
import logging
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from email.message import Message
from functools import partial
from io import BytesIO
from threading import Event, Thread, current_thread
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from lark_oapi.ws import client as ws

from qianshu.feishu.sdk import ApprovalClosed, FeishuService
from qianshu.feishu.settings import Settings, Window
from qianshu.mingdao import create_order as order


@pytest.fixture
def case(tmp_path):
    ids = {key: str(uuid4()) for key in (
        "template", "created", "tag", "option", "member", "package", "gift_package",
        "worksheetId", "appId", "viewId", "projectId", "registration",
        "additional_view", "tag_name", "tag_alias", "official_app",
        "new_status", "pending_status", "configuring_status", "configured_status",
    )}
    fields = {}
    control = {}

    def field(key, kind, label, **extra):
        control[key] = str(uuid4())
        fields[control[key]] = {"label": label, "type": kind, **extra}
        if key in order._DRAFT_FIELDS:
            fields[control[key]]["draft"] = key

    child = {key: str(uuid4()) for key in ("current", "gift", "price")}
    field("policy_name", 2, "政策名称")
    field("start_date", 15, "开始日期")
    field("end_date", 15, "结束日期")
    field("recharge_days", 6, "充值天数")
    field("gift_days", 6, "赠送天数")
    field("user_scope", 2, "用户范围")
    field("user_tags", 29, "用户标签")
    field("packages", 34, "节目包", children={
        child["current"]: {"type": 29}, child["gift"]: {"type": 29}, child["price"]: {"type": 6},
    }, current={"control": child["current"], "name_path": [0, "name"]},
          gift={"control": child["gift"], "name_path": [0, "name"]})
    field("approval_url", 2, "任务审批链接")
    field("company", 2, "公司")
    field("maintain_mutual_exclusion_group", 11, "互斥组",
          draft_values={order.compact("是"): [ids["option"]], order.compact("否"): [str(uuid4())]})
    field("country", 2, "国家")
    field("member", 26, "申请人")
    field("status", 11, "政策状态")
    field("generated", 2, "回填字段")

    template = {
        "policy_name": "合成模板", "start_date": "2026-11-21", "end_date": "2026-12-01",
        "recharge_days": "1", "gift_days": "2", "user_scope": "占位模板用户范围",
        "user_tags": 1, "packages": 1,
        "approval_url": "https://placeholder.invalid/approval",
        "company": "合成公司", "maintain_mutual_exclusion_group": [{"key": ids["option"], "value": "占位选项"}],
        "country": "合成国家", "member": [{"accountId": str(uuid4()), "name": "占位成员"}],
        "status": [{"key": ids["new_status"], "value": "新建（占位部门）"}],
        "generated": "合成旧值",
    }
    relations = {
        control["packages"]: [{
            **record_identity(str(uuid4())), "_owner": {},
            child["current"]: [{"sid": ids["package"], "name": "合成当前包"}],
            child["gift"]: [{"sid": ids["gift_package"], "name": "合成赠送包"}],
            child["price"]: "3.00",
        }],
        control["user_tags"]: [{**record_identity(ids["tag"]), "_owner": {}, ids["tag_name"]: "合成标签"}],
    }
    values = {"policy_name": "【测试】合成单", "start_date": "2026-12-01", "end_date": "2026-12-31",
              "recharge_days": 30, "gift_days": 31, "user_scope": "占位草稿用户范围",
              "company": "合成公司", "user_tags": ["合成标签"],
              "maintain_mutual_exclusion_group": "是",
              "packages": [{"current_package": "合成当前包", "gift_package": "合成赠送包"}]}
    endpoint = {"host": order._CREATE_HOST, "method": "POST", "path": order._CREATE_PATH}
    rows_path = f"/v3/app/worksheets/{ids['worksheetId']}/rows"
    statuses = {index: ids[key] for index, key in enumerate(
        ("new_status", "pending_status", "configuring_status", "configured_status"), start=1,
    )}
    status_names = dict(enumerate(("新建", "待审批", "待配置", "配置完成"), start=1))
    status_labels = {index: f"{name}（占位部门）" for index, name in status_names.items()}

    config = {
        "configurator": {"id": str(uuid4()), "label": "合成负责人"},
        "notice_recipients": [{"key": "placeholder_owner", "label": "占位通知对象甲", "role": "owner"}],
        "fields": fields, "readback_skip": [control["generated"]],
        "applicant_control": control["member"],
        "fixed": {control["status"]: [ids["new_status"]], control["member"]: [ids["member"]]},
        "template": {"row_id": ids["template"], "country": {"control": control["country"], "value": "合成国家"}},
        "create": {"endpoint": endpoint.copy(), "session_cookie": "placeholder_session", "response_id": ["id"],
                   "identifiers": {key: ids[key] for key in ("worksheetId", "appId", "viewId", "projectId")}},
        "official": {
            "origin": "https://placeholder.invalid", "token_key": "PLACEHOLDER_OFFICIAL_TOKEN",
            "app_id_key": "PLACEHOLDER_OFFICIAL_APP",
            "status_control": control["status"],
            "status_map": {key: name for index, name in status_names.items()
                           for key in (statuses[index], status_labels[index])},
            "read": {"path": rows_path + "/{record_id}", "missing_status": 200,
                     "missing_key": ["error_code"], "missing_value": 12345},
            "search": {"path": rows_path + "/list", "name_control": control["policy_name"],
                       "view_ids": [ids["viewId"]]},
            "tag_relation": {"name_keys": [ids["tag_name"], ids["tag_alias"]]},
            "logs": {"path": rows_path + "/{record_id}/logs"},
            "delete": {"path": rows_path + "/{record_id}", "method": "DELETE", "condition": "ifStatus"},
        },
    }
    now = datetime.now(UTC)
    window = Window(now - timedelta(minutes=1), now + timedelta(minutes=1))
    evidence = {key: f"占位回执 {index}" for index, key in enumerate(order._EVIDENCE)}
    evidence.update(is_test=True, create_endpoint=endpoint.copy(),
                    notice_waiver="占位免通知批准登记",
                    notices={"placeholder_owner": "占位免通知回执"},
                    window={"start": window.start.isoformat(), "end": window.end.isoformat()},
                    two_reviews={"internal": "占位内审", "external": "占位外审"})
    private = tmp_path / ".private"
    private.mkdir(mode=0o700)
    return SimpleNamespace(
        ids=ids, control=control, child=child, values=values, config=config, evidence=evidence,
        statuses=statuses, status_labels=status_labels,
        cells={control[key]: value for key, value in template.items()}, private=private, window=window,
        relations=relations,
        session_token="placeholder-sensitive-" + uuid4().hex,
        official_token="placeholder-sensitive-" + uuid4().hex,
    )


def assemble(case):
    return order.assemble(case.values, case.cells, case.config, case.relations.__getitem__, is_test=True)


def company_relation(case):
    control = case.control["company"]
    display_key = str(uuid4())
    case.config["fields"][control].update(type=29, display_name_key=display_key)
    other = {**record_identity(str(uuid4())), display_key: "占位其他公司"}
    matched = {**record_identity(str(uuid4())), display_key: case.values["company"]}
    case.relations[control] = [other, matched]
    case.cells[control] = len(case.relations[control])
    return control, display_key, matched


def add_notice_recipients(case):
    del case.evidence["notice_waiver"]
    roles = (("b", "占位通知对象乙", "approver"), ("c", "占位通知对象丙", "configurator"))
    for suffix, label, role in roles:
        key = f"placeholder_{suffix}"
        case.config["notice_recipients"].append({"key": key, "label": label, "role": role})
        case.evidence["notices"][key] = f"占位回执 {suffix}"


def test_assemble_values_and_legacy_cells(case):
    case.cells.update(record_identity(case.ids["template"]), _owner={}, _createdBy={})
    case.cells.update({"_createdAt": "", "_updatedAt": "", str(uuid4()): "占位自动计算值"})
    case.cells[case.control["maintain_mutual_exclusion_group"]] = order.compact([case.ids["option"]])
    controls, summary = assemble(case)
    fields = {row["controlId"]: row["value"] for row in controls}
    assert fields.keys() == case.config["fields"].keys()
    assert all(isinstance(value, str) for value in fields.values())
    for key in ("policy_name", "start_date", "end_date", "recharge_days", "gift_days"):
        assert fields[case.control[key]] == str(case.values[key])
    assert json.loads(fields[case.control["user_tags"]]) == [{"sid": case.ids["tag"]}]
    assert json.loads(fields[case.control["maintain_mutual_exclusion_group"]]) == [case.ids["option"]]
    assert json.loads(fields[case.control["member"]]) == [{"accountId": case.ids["member"]}]
    assert json.loads(fields[case.control["status"]]) == [case.ids["new_status"]]
    assert json.loads(fields[case.control["packages"]]) == [[
        {"controlId": case.child["current"], "value": order.compact([{"sid": case.ids["package"]}])},
        {"controlId": case.child["gift"], "value": order.compact([{"sid": case.ids["gift_package"]}])},
        {"controlId": case.child["price"], "value": "3.00"},
    ]]
    for key in ("approval_url", "company", "country", "generated"):
        assert fields[case.control[key]] == case.cells[case.control[key]]
    assert [row["来源"] for row in summary].count("草稿") == 7
    assert [row["来源"] for row in summary].count("固定值") == 2
    assert all(row.keys() == {"字段名", "类型", "来源", "值指纹"}
               and len(row["值指纹"]) == 12 for row in summary)
    assert order.encode_cell([case.ids["option"]], {"type": 9}) == order.compact([case.ids["option"]])


def test_user_scope_is_written_from_draft_and_cannot_skip_readback(case):
    control = case.control["user_scope"]
    controls, summary = assemble(case)
    field, = (value for value in controls if value["controlId"] == control)
    assert field["value"] == case.values["user_scope"] != case.cells[control]
    assert next(row for row in summary if row["字段名"] == "用户范围")["来源"] == "草稿"
    case.config["readback_skip"].append(control)
    with pytest.raises(order.Stop, match="不可跳过建单关键字段的回读"):
        assemble(case)


@pytest.mark.parametrize(("draft_name", "template_name"), [
    ("PLACEHOLDER Basic", "PLACEHOLDER_Basic"),
    ("PLACEHOLDER classic", "PLACEHOLDER_Classic"),
    ("  placeholder  _  basic  ", "PLACEHOLDER_Basic"),
])
def test_packages_select_only_third_template_row(case, draft_name, template_name):
    control = case.control["packages"]
    current, gift, price = (case.child[key] for key in ("current", "gift", "price"))
    spec = case.config["fields"][control]
    spec["children"][current] = {"type": 2}
    spec["current"]["name_path"] = []
    uuid_control, extra_control = str(uuid4()), str(uuid4())
    uuid_value = str(uuid4())
    spec["children"][uuid_control] = {"type": 2}
    gift_cell = [{"sid": case.ids["gift_package"], "name": "PLACEHOLDER_Gift"}]
    other_gift = [{"sid": str(uuid4()), "name": "PLACEHOLDER_Other"}]
    rows = [
        {**record_identity(str(uuid4())), "_owner": {}, extra_control: "占位额外值",
         current: f"占位包{index}", gift: gift_cell, price: str(index), uuid_control: uuid_value}
        for index in range(1, 7)
    ]
    rows[0].update({current: template_name, gift: other_gift})
    rows[2][current] = template_name
    rows[1][gift] = []
    case.relations[control] = rows
    case.cells[control] = len(rows)
    case.values["packages"] = [{"current_package": draft_name, "gift_package": "placeholder gift"}]

    controls, _ = assemble(case)
    fields = {row["controlId"]: row["value"] for row in controls}
    selected = [
        {"controlId": current, "value": template_name},
        {"controlId": gift, "value": order.compact([{"sid": case.ids["gift_package"]}])},
        {"controlId": price, "value": "3"},
        {"controlId": uuid_control, "value": uuid_value},
    ]
    assert json.loads(fields[control]) == [selected]

    case.values["packages"].insert(0, {"current_package": "占位包2", "gift_package": None})
    controls, _ = assemble(case)
    fields = {row["controlId"]: row["value"] for row in controls}
    assert json.loads(fields[control]) == [[
        {"controlId": current, "value": "占位包2"},
        {"controlId": gift, "value": "[]"},
        {"controlId": price, "value": "2"},
        {"controlId": uuid_control, "value": uuid_value},
    ], selected]


def test_packages_stop_on_duplicate_normalized_pair(case):
    control, current = case.control["packages"], case.child["current"]
    first, = case.relations[control]
    first[current][0]["name"] = "PLACEHOLDER_Basic"
    second = deepcopy(first)
    second.update(record_identity(str(uuid4())))
    second[current][0]["name"] = "placeholder basic"
    second[case.child["price"]] = "6"
    case.relations[control].append(second)
    case.cells[control] = 2
    case.values["packages"][0]["current_package"] = "Placeholder Basic"
    with pytest.raises(order.Stop, match="节目包与模板不同，需人工"):
        assemble(case)


@pytest.mark.parametrize(("failure", "reason"), [
    ("conflict", "互斥组"), ("package", "节目包与模板不同"), ("missing", "模板字段不全"),
    ("country_bound_to_tags", "模板国家绑定无效"),
    ("formal", "正式单不得照抄任务审批链接"),
])
def test_assembly_stops(case, failure, reason):
    if failure == "conflict":
        case.values["maintain_mutual_exclusion_group"] = "否"
    if failure == "package":
        case.values["packages"][0]["gift_package"] = "合成替换包"
    if failure == "missing":
        del case.cells[case.control["country"]]
    if failure == "country_bound_to_tags":
        control = case.control["user_tags"]
        case.config["template"]["country"] = {"control": control, "value": case.cells[control]}
    with pytest.raises(order.Stop, match=reason):
        order.assemble(case.values, case.cells, case.config, case.relations.__getitem__,
                       is_test=failure != "formal")


@pytest.mark.parametrize("field", ["policy_name", "start_date", "end_date", "recharge_days",
                                   "gift_days", "approval_url", "unknown"])
def test_fixed_fields_reject_overlaps_and_unconfigured_controls(case, field):
    case.config["fixed"][case.control.get(field, str(uuid4()))] = "占位固定值"
    with pytest.raises(order.Stop, match="固定字段配置无效"):
        assemble(case)


def test_fixed_company_cannot_replace_missing_template_value(case):
    company = case.control["company"]
    del case.cells[company]
    case.config["fixed"][company] = "占位固定公司"
    with pytest.raises(order.Stop, match="固定字段配置无效"):
        assemble(case)


@pytest.mark.parametrize("scenario", ["no_match", "duplicate_name", "missing_name", "missing_key", "skip_readback"])
def test_company_selection_requires_one_provable_display_name_match(case, scenario):
    control, display_key, matched = company_relation(case)
    reason = "公司显示名不确定"
    if scenario == "no_match":
        matched[display_key] = "占位不同显示名"
    elif scenario == "duplicate_name":
        case.relations[control][0][display_key] = matched[display_key]
    elif scenario == "missing_name":
        del case.relations[control][0][display_key]
    elif scenario == "missing_key":
        del case.config["fields"][control]["display_name_key"]
    else:
        case.config["readback_skip"].append(control)
        reason = "不可跳过建单关键字段的回读"
    with pytest.raises(order.Stop, match=reason):
        assemble(case)


def test_changed_company_outside_readable_template_stops_before_create(case):
    control, _, matched = company_relation(case)
    case.relations[control] = [matched]
    case.cells[control] = 1
    assemble(case)
    case.values["company"] = "占位改表后公司"
    platform = Platform(case)
    approvals = Approvals(platform)

    with pytest.raises(order.Stop, match="模板可读关联中无同名公司"):
        run_case(case, platform, approvals)

    assert platform.create_count == 0
    assert not approvals.notices
    assert not list(case.private.iterdir())


@pytest.mark.parametrize("field", ["status", "member"])
def test_fixed_fields_require_new_status_and_applicant(case, field):
    del case.config["fixed"][case.control[field]]
    with pytest.raises(order.Stop, match="固定字段配置无效"):
        assemble(case)
    if field == "status":
        case.config["fixed"][case.control["status"]] = [case.ids["pending_status"]]
        with pytest.raises(order.Stop, match="固定政策状态须为新建"):
            assemble(case)


def test_package_relation_requires_registered_children(case):
    del case.relations[case.control["packages"]][0][case.child["price"]]
    with pytest.raises(order.Stop, match="节目包关联行缺少已登记的子控件"):
        assemble(case)


def test_package_names_must_use_registered_children(case):
    control = case.control["packages"]
    extra = str(uuid4())
    row = case.relations[control][0]
    row[extra] = row[case.child["current"]]
    case.config["fields"][control]["current"]["control"] = extra
    with pytest.raises(order.Stop, match="节目包名称控件须在已登记的子控件中"):
        assemble(case)


def test_tags_use_both_name_keys_in_draft_order(case):
    control = case.control["user_tags"]
    name, alias = case.config["official"]["tag_relation"]["name_keys"]
    first = {**record_identity(str(uuid4())), name: "PLACEHOLDER_First", alias: "PLACEHOLDER_Alias"}
    second = {**record_identity(str(uuid4())), alias: "PLACEHOLDER_Second"}
    case.relations[control] = [second, first]
    case.cells[control] = 2
    case.values["user_tags"] = ["  placeholder  _  FIRST  ", "placeholder second"]
    controls, _ = assemble(case)
    fields = {row["controlId"]: row["value"] for row in controls}
    assert json.loads(fields[control]) == [{"sid": row["rowId"]} for row in (first, second)]


def record_identity(row_id):
    return {"_id": uuid4().hex[:24], "rowId": row_id}


class Platform:
    def __init__(self, case):
        self.case = case
        self.relations = deepcopy(case.relations)
        self.relation_response = lambda rows: self.response({"rows": rows, "total": len(rows)})
        self.created_relations = {}
        self.created_relation_response = lambda _, rows: self.relation_response(rows)
        self.same_name = False
        self.rows = {}
        self.actions = []
        self.states = [1, 2, 1]
        self.log_effect = lambda logs: {"logs": logs}
        self.log_envelope = {}
        self.logs_success = True
        self.delete_effect = "missing"
        self.create_effect = "ok"
        self.create_count = 0
        self.created_cells = {}
        self.mismatch = False
        self.before_search = lambda: None
        self.before_read = lambda row: None
        self.before_handoff = lambda: None
        self.query_pages = {}
        self.after_delete_query_pages = {}
        self.queries = []
        self.timeouts = []
        self.template = {**case.cells, **record_identity(case.ids["template"]), "_createdBy": {}, "_owner": {}}

    def status_cell(self, status):
        return [{"key": self.case.statuses[status], "value": self.case.status_labels[status]}]

    def record(self, record_id, status, cells):
        return {
            **cells, **record_identity(record_id),
            self.case.control["policy_name"]: cells.get(self.case.control["policy_name"],
                                                       self.case.values["policy_name"]),
            self.case.control["status"]: self.status_cell(status),
            "_createdAt": "2026-11-20 08:00:00", "_updatedAt": "2026-11-20 08:00:01",
            "_createdBy": {}, "_owner": {},
        }

    def logs(self):
        return [{
            "operator": {},
            "content": {
                "type": 1 if index == 0 else 2,
                "_createdAt": f"2026-11-20 08:00:{index % 60:02d}",
                "logData": [{
                    "id": self.case.control["status"], "name": "政策状态",
                    "type": 11, "editType": 1,
                    "oldValue": self.case.status_labels[self.states[index - 1]] if index else "", "oldText": "",
                    "newValue": self.case.status_labels[state], "newText": "",
                    "isDeleted": False,
                }],
            },
        } for index, state in enumerate(self.states)]

    def official(self, request):
        case = self.case
        spec = case.config["official"]
        assert request.headers["Authorization"] == "Bearer " + case.official_token
        assert request.headers["HAP-Appid"] == case.ids["official_app"]
        assert request.url.host == "placeholder.invalid"
        self.timeouts.append(request.extensions["timeout"])
        path = request.url.path
        template_path = spec["read"]["path"].replace("{record_id}", case.ids["template"])
        record_id = case.ids["created"]
        created_path = spec["read"]["path"].replace("{record_id}", record_id)
        for control in self.relations:
            if path in (f"{template_path}/relations/{control}", f"{created_path}/relations/{control}"):
                assert request.method == "GET" and not request.content
                assert dict(request.url.params) == {"pageIndex": "1", "pageSize": "50"}
                if path.startswith(template_path + "/"):
                    self.actions.append("relations:" + control)
                    return self.relation_response(self.relations[control])
                self.actions.append("created_relations:" + control)
                return self.created_relation_response(control, self.created_relations[control])
        if path == spec["logs"]["path"].replace("{record_id}", case.ids["created"]):
            assert dict(request.url.params) == {"pageIndex": "1", "pageSize": "50"}
        else:
            assert not request.url.params
        if path == spec["search"]["path"]:
            self.actions.append("search")
            query = json.loads(request.content)
            view, page = query["viewId"], query["pageIndex"]
            control = spec["search"]["name_control"]
            name = query["filter"]["children"][0]["value"]
            assert request.method == "POST" and name == case.values["policy_name"]
            assert view in spec["search"]["view_ids"] and page > 0
            assert query == {"viewId": view, "pageSize": spec.get("page_size", 50), "pageIndex": page,
                             "filter": {"type": "group", "logic": "AND", "children": [
                                 {"type": "condition", "field": control, "operator": "eq", "value": name},
                             ]}}
            self.queries.append(("search", view, page))
            self.before_search()
            rows = list(self.rows.values())
            if self.same_name:
                rows.append(self.record(str(uuid4()), 1, {}))
            pages = self.after_delete_query_pages if "delete" in self.actions else self.query_pages
            page_rows = pages.get((view, page), rows)
            if page_rows is None:
                return httpx.Response(503, json={"success": False})
            return self.response({"rows": page_rows})
        if path == spec["logs"]["path"].replace("{record_id}", record_id):
            self.actions.append("history")
            assert request.method == "GET"
            return self.response(self.log_effect(self.logs()), success=self.logs_success, **self.log_envelope)
        if path == template_path:
            self.actions.append("template")
            assert request.method == "GET"
            return self.response(self.template)
        assert path == created_path
        if request.method == "GET":
            self.actions.append("read")
            if record_id not in self.rows:
                code = 99999 if self.delete_effect == "wrong_missing" else spec["read"]["missing_value"]
                return httpx.Response(200, json={"success": False, "error_code": code})
            self.before_read(self.rows[record_id])
            return self.response(self.rows[record_id])
        self.actions.append("delete")
        assert request.method == "DELETE"
        body = {"triggerWorkflow": False, "permanent": False}
        if spec["delete"]["condition"] is not None:
            body[spec["delete"]["condition"]] = case.statuses[self.states[-1]]
        assert json.loads(request.content) == body
        if self.delete_effect == "interrupted":
            raise KeyboardInterrupt
        if self.delete_effect != "still_exists":
            del self.rows[record_id]
        return self.response(None)

    @staticmethod
    def response(data, *, success=True, **envelope):
        return httpx.Response(200, json={"success": success, "error_code": 0, "data": data, **envelope})

    def connection(self, host, **kwargs):
        platform = self

        class Connection:
            sock = SimpleNamespace(settimeout=lambda seconds: platform.timeouts.append(seconds))
            response_class = None

            def set_debuglevel(self, level):
                assert level == 0

            def connect(self):
                assert host == order._CREATE_HOST
                assert kwargs["timeout"] == 10

            def putrequest(self, method, path, **kwargs):
                assert (method, path) == ("POST", order._CREATE_PATH)
                assert platform.actions[-1] == "card"

            def putheader(self, header, value):
                if header == "Authorization":
                    assert value == "md_pss_id " + platform.case.session_token

            def endheaders(self, body):
                platform.before_handoff()
                platform.create_count += 1
                platform.actions.append("create")
                case = platform.case
                ledger = case.private / "trial-orders.jsonl"
                assert json.loads(ledger.read_text().splitlines()[-1])["event"] == "create_attempt"
                payload = json.loads(body)
                assert payload.keys() == case.config["create"]["identifiers"].keys() | {
                    "rowStatus", "receiveControls",
                }
                assert payload["rowStatus"] == 1
                assert all(payload[key] == value for key, value in case.config["create"]["identifiers"].items())
                raw_cells = {cell["controlId"]: cell["value"] for cell in payload["receiveControls"]}
                platform.created_cells = raw_cells.copy()
                packages = case.control["packages"]
                tags = case.control["user_tags"]
                platform.created_relations[packages] = deepcopy(platform.relations[packages])
                platform.created_relations[tags] = [
                    record_identity(tag["sid"].lower()) for tag in json.loads(raw_cells[tags])
                ]
                for control in (packages, tags):
                    raw_cells[control] = len(json.loads(raw_cells[control]))
                company = case.control["company"]
                if case.config["fields"][company]["type"] == 29:
                    selected = {item["sid"] for item in json.loads(raw_cells[company])}
                    platform.created_relations[company] = [deepcopy(row) for row in platform.relations[company]
                                                           if row["rowId"] in selected]
                    raw_cells[company] = len(selected)
                mutual_exclusion = case.control["maintain_mutual_exclusion_group"]
                key, = json.loads(raw_cells[mutual_exclusion])
                raw_cells[mutual_exclusion] = [{"key": key, "value": "占位选项"}]
                if platform.mismatch:
                    raw_cells[case.control["gift_days"]] = "9"
                    del raw_cells[case.control["start_date"]]
                record_id = case.ids["created"]
                platform.rows[record_id] = platform.record(
                    record_id, platform.states[-1], raw_cells,
                )
                if platform.create_effect == "timeout":
                    raise TimeoutError(platform.case.session_token)
                if platform.create_effect == "interrupted":
                    raise KeyboardInterrupt

            def getresponse(self):
                assert platform.timeouts[-1] == 30
                effect = platform.create_effect
                if effect == "read_timeout":
                    raise TimeoutError
                body = order.compact({"id": platform.case.ids["created"]}).encode()
                status = b"307 Temporary Redirect" if effect == "redirect" else b"200 OK"
                if effect == "bad_json":
                    body = platform.case.session_token.encode()
                if effect.startswith("chunk"):
                    headers = b"Transfer-Encoding: chunked"
                    top = f"{len(body):x}".encode()
                    endings = {"chunk_ok": b"0\r\nTrailer: value\r\n\r\n",
                               "chunk_missing_end": b"", "chunk_short_trailer": b"0\r\n"}
                    endings["chunk_bad_crlf"] = endings["chunk_ok"]
                    separator = b"xx" if effect == "chunk_bad_crlf" else b"\r\n"
                    body = top + b"\r\n" + body + separator + endings[effect]
                else:
                    size = len(body) + (3 if effect == "short_length" else 0)
                    headers = f"Content-Length: {size}".encode()
                raw = b"HTTP/1.1 " + status + b"\r\n" + headers + b"\r\n\r\n" + body
                response = self.response_class(SimpleNamespace(makefile=lambda _: BytesIO(raw)))
                response.begin()
                return response

            def close(self):
                pass

        return Connection()


@pytest.fixture
def platform(case):
    return Platform(case)


@pytest.mark.parametrize(("failure", "reason"), [
    ("total_mismatch", "关联行结果不完整"),
    ("full_page", "关联行结果不完整"),
    ("bool_total", "关联行结果不完整"),
    ("missing_total", "关联行结果不完整"),
    ("invalid_rows", "关联行结果不完整"),
    ("missing_row_id", "关联行记录编号未确认"),
    ("invalid_row_id", "关联行记录编号未确认"),
    ("unsuccessful", "官方接口结果未确认"),
])
def test_relations_reject_unconfirmed_results(case, platform, failure, reason):
    control = case.control["packages"]
    rows = platform.relations[control]
    result = {"rows": rows, "total": len(rows)}
    if failure == "total_mismatch":
        result["total"] += 1
    if failure == "full_page":
        rows.extend(record_identity(str(uuid4())) for _ in range(49))
        result["total"] = 50
    if failure == "bool_total":
        result["total"] = True
    if failure == "missing_total":
        del result["total"]
    if failure == "invalid_rows":
        result["rows"] = {}
    if failure == "missing_row_id":
        rows[0].pop("rowId")
    if failure == "invalid_row_id":
        rows[0]["rowId"] = "占位无效编号"
    platform.relation_response = lambda _: platform.response(result, success=failure != "unsuccessful")
    with httpx.Client(transport=httpx.MockTransport(platform.official)) as client:
        api = order.Official(case.config["official"], case.official_token, case.ids["official_app"], client)
        with pytest.raises(order.Stop, match=reason):
            api.relations(case.ids["template"], control)
    assert platform.actions == ["relations:" + control]


@pytest.mark.parametrize("count", [0, 3])
def test_tag_relation_requires_one_or_two_name_keys(case, count):
    config = case.config["official"]
    config["tag_relation"]["name_keys"] = [str(uuid4()) for _ in range(count)]
    with pytest.raises(order.Stop, match="用户标签名称键配置无效"):
        order.Official(config, case.official_token, case.ids["official_app"], None)


@pytest.mark.parametrize("mode", ["zero_matches", "duplicate", "later_page_failed", "page_limit"])
def test_duplicate_check_reads_every_page_of_every_view(case, platform, mode):
    official = case.config["official"]
    view, extra = (case.ids[key] for key in ("viewId", "additional_view"))
    official["search"]["view_ids"].append(extra)
    official["page_size"] = 2
    control = official["search"]["name_control"]
    name = case.values["policy_name"]
    near_match = {**record_identity(str(uuid4())), control: name + "（占位后缀）"}
    exact_match = {**record_identity(str(uuid4())), control: name}
    platform.query_pages = {
        (view, 1): [near_match, near_match],
        (view, 2): [],
        (extra, 1): [near_match, exact_match if mode == "duplicate" else near_match],
        (extra, 2): None if mode == "later_page_failed" else [],
    }
    if mode == "page_limit":
        official["page_limit"] = 2
        platform.query_pages[(extra, 2)] = [near_match, near_match]
    if mode == "zero_matches":
        platform.after_delete_query_pages = platform.query_pages.copy()
    assert run_case(case, platform, Approvals(platform)) == (mode == "zero_matches")
    expected = [("search", view, 1), ("search", view, 2), ("search", extra, 1), ("search", extra, 2)]
    assert platform.queries == expected * (2 if mode == "zero_matches" else 1)
    assert platform.create_count == int(mode == "zero_matches")
    assert ("aborted_before_write" in events(case)) == (mode != "zero_matches")


@pytest.mark.parametrize("invalid_row_id", [None, "占位无效编号"])
def test_list_requires_valid_row_id(case, platform, invalid_row_id):
    config = case.config["official"]
    name = case.values["policy_name"]
    spec = config["search"]
    row = {"_id": uuid4().hex[:24], spec["name_control"]: name}
    if invalid_row_id is not None:
        row["rowId"] = invalid_row_id
    platform.query_pages[(spec["view_ids"][0], 1)] = [row]
    with httpx.Client(transport=httpx.MockTransport(platform.official)) as client:
        api = order.Official(config, case.official_token, case.ids["official_app"], client)
        with pytest.raises(order.Stop, match="官方查询记录编号未确认"):
            api.find(name)


def test_search_requires_configured_view(case):
    config = case.config["official"]
    config["search"]["view_ids"] = []
    with pytest.raises(order.Stop, match="查询配置"):
        order.Official(config, case.official_token, case.ids["official_app"], None)


def test_search_stops_at_default_page_limit(case, platform):
    config = case.config["official"]
    config["page_size"] = 1
    view = config["search"]["view_ids"][0]
    filler = {**record_identity(str(uuid4())), config["search"]["name_control"]: "占位近似名称"}
    platform.query_pages = {(view, page): [filler] for page in range(1, 22)}
    with httpx.Client(transport=httpx.MockTransport(platform.official)) as client:
        api = order.Official(config, case.official_token, case.ids["official_app"], client)
        with pytest.raises(order.Stop, match="分页上限"):
            api.find(case.values["policy_name"])
    assert platform.queries == [("search", view, page) for page in range(1, 21)]


class Approvals:
    def __init__(self, platform, *, state="approve", close_during_send=False):
        self.platform = platform
        self.state = state
        self.close_during_send = close_during_send
        self.transport = SimpleNamespace(announce=self.announce)
        self.notices = []
        self.closed = False

    def announce(self, text):
        self.notices.append(text)
        assert self.platform.case.session_token not in text

    def approval_connection_ready(self):
        return True

    def publish_approval(self, binding, summary):
        self.platform.actions.append("card")
        assert "拟建 X5 测试单 1 条" in summary
        assert self.platform.case.values["policy_name"] in summary
        assert all(value in summary for value in ("起止日期", "充值 / 赠送天数", "用户标签", "节目包"))
        assert self.platform.create_count == 0
        return "placeholder_approval"

    def approval_status(self, approval, binding):
        return self.state

    def close_create_approval(self, approval, binding):
        self.closed = True

    def send_first_create(self, approval, binding, prepare, record_attempt, send):
        if self.state != "approve":
            return False
        prepare()
        record_attempt()

        def check():
            if self.close_during_send:
                raise ApprovalClosed("建单审批已关闭")

        send(check)
        return True


def run_case(case, platform, approvals, *, clock=order.time.time, stop=None, pause=None, monotonic=None):
    create = order.CreateRequest(case.config["create"], case.evidence, case.session_token,
                                 connect=platform.connection)
    with httpx.Client(transport=httpx.MockTransport(platform.official)) as client:
        api = order.Official(case.config["official"], case.official_token, case.ids["official_app"], client)
        controls, _ = assemble(case)
        with order.ledger_open(case.private, case.ids["registration"], case.values["policy_name"]) as ledger:
            ledger.reserve()
            return order.run_order(
                ledger, api, create, approvals, case.window, create.payload(controls),
                order.card_summary(case.values), controls, case.config, case.evidence,
                '<at user_id="placeholder">占位</at>', lambda: None, stop or Event(),
                packages=case.values["packages"], company=case.values["company"], clock=clock,
                pause=pause or (lambda _: None),
                monotonic=monotonic or (lambda: 0),
            )


def events(case):
    return [json.loads(line)["event"] for line in (case.private / "trial-orders.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("decision", ["reject", "closed", "pending", "duplicate", "expired_after_fsync"])
def test_does_not_create_without_current_approval_or_unique_name(case, platform, decision):
    approvals = Approvals(platform, state=decision if decision in ("reject", "closed", "pending") else "approve",
                          close_during_send=decision == "expired_after_fsync")
    platform.same_name = decision == "duplicate"
    clock = lambda: case.window.end.timestamp()
    assert not run_case(case, platform, approvals, clock=clock)
    assert platform.create_count == 0
    assert approvals.closed
    assert any("未建单" in text for text in approvals.notices)
    assert "aborted_before_write" in events(case)
    assert ("create_attempt" in events(case)) == (decision == "expired_after_fsync")


@pytest.mark.parametrize("effect", ["timeout", "read_timeout", "bad_json", "redirect", "interrupted", "short_length",
                                     "chunk_missing_end", "chunk_short_trailer", "chunk_bad_crlf"])
def test_create_unknown_freezes_and_secrets_stay_out(case, platform, effect, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    approvals = Approvals(platform)
    platform.create_effect = effect
    with pytest.raises(order.Stop, match="建单结果未知") as error:
        run_case(case, platform, approvals)
    assert platform.create_count == 1
    assert platform.actions == ["card", "search", "create", "search"]
    assert events(case)[-2:] == ["create_unknown", "create_reconciled_read"]
    assert "created" not in events(case)
    with order.ledger_open(case.private, str(uuid4()), case.values["policy_name"]) as ledger:
        with pytest.raises(order.Stop, match="资格已占用"):
            ledger.reserve()
    captured = capsys.readouterr()
    output = captured.out + captured.err + caplog.text + str(error.value)
    assert error.value.__cause__ is None
    for file in case.private.iterdir():
        output += file.read_text()
    assert case.session_token not in output
    assert case.official_token not in output


def test_exact_endpoint_matches_authorization(case, platform):
    for key, value in (("host", "placeholder.invalid"), ("method", "GET"), ("path", "/placeholder")):
        config = deepcopy(case.config["create"])
        config["endpoint"][key] = value
        with pytest.raises(order.Stop, match="入口"):
            order.CreateRequest(config, case.evidence, case.session_token, connect=platform.connection)
        evidence = {**case.evidence, "create_endpoint": config["endpoint"]}
        with pytest.raises(order.Stop, match="入口"):
            order.CreateRequest(config, evidence, case.session_token, connect=platform.connection)
    assert platform.create_count == 0


@pytest.mark.parametrize(("condition", "deleted", "verified"), [
    ("pending_then_new", True, True),
    ("new_only", True, True),
    ("default_delete_body", True, True),
    ("logs_failed", False, False),
    ("malformed_log", False, False),
    ("malformed_change", False, False),
    ("bad_log_time", False, False),
    ("bad_operator", False, False),
    ("no_type_one", True, True),
    ("manual_edit", True, True),
    ("empty_old_fields", True, True),
    ("null_old_fields", True, True),
    ("null_status_text", True, True),
    ("null_status_value", True, True),
    ("empty_new_fields", False, False),
    ("flag_true", False, False),
    ("envelope_flag_true", False, False),
    ("last_mark", False, False),
    ("flag_false", True, True),
    ("configured_then_new", False, False),
    ("old_configuring_only", False, False),
    ("old_configuring_without_text", False, False),
    ("old_unknown_without_text", False, False),
    ("new_configuring_with_new_text", False, False),
    ("old_conflicting_allowed_states", False, False),
    ("old_pending_only", True, True),
    ("blank_status", True, True),
    ("unknown_status", False, False),
    ("deleted_status", False, False),
    ("current_pending", False, False),
    ("pending_before_delete", False, False),
    ("49_logs", True, True),
    ("50_logs", False, False),
    ("lower_log_limit", False, False),
    ("empty", False, False),
    ("undetermined_completeness", False, False),
    ("missing_notice", False, False),
    ("missing_approver", False, False),
    ("missing_configurator", False, False),
    ("changed_before_delete", False, False),
    ("still_exists", True, False),
    ("wrong_missing", True, False),
    ("interrupted", True, False),
])
def test_d15_verification(case, platform, condition, deleted, verified):
    approvals = Approvals(platform)

    def edit_logs(logs):
        content = logs[0]["content"]
        change = content["logData"][0]
        if condition == "malformed_log":
            del logs[-1]["content"]
        if condition == "malformed_change":
            del change["oldText"]
        if condition == "bad_log_time":
            content["_createdAt"] = "占位无效时刻"
        if condition == "bad_operator":
            logs[0]["operator"] = None
        if condition == "no_type_one":
            content["type"] = 2
        if condition == "manual_edit":
            manual_change = dict(change, id=case.control["company"], name="占位字段",
                                 oldValue=None, oldText=None, newValue=None, newText=None)
            logs.append({"operator": {}, "content": dict(content, logData=[manual_change])})
        if condition == "empty_old_fields":
            change.update(oldValue="", oldText=None)
        if condition == "null_old_fields":
            change.update(oldValue=None, oldText=None)
        if condition in ("old_configuring_only", "old_pending_only"):
            old_state = 3 if condition == "old_configuring_only" else 2
            old_value = case.status_labels[old_state]
            change.update(oldValue=old_value, oldText=case.config["official"]["status_map"][old_value])
        if condition == "old_configuring_without_text":
            change.update(oldValue=case.status_labels[3], oldText="")
        if condition == "old_unknown_without_text":
            change.update(oldValue="占位未知状态键", oldText="")
        if condition == "new_configuring_with_new_text":
            change.update(newValue=case.status_labels[3], newText="新建")
        if condition == "old_conflicting_allowed_states":
            change.update(oldValue=case.status_labels[2], oldText="新建")
        if condition == "null_status_text":
            change["newText"] = None
        if condition == "null_status_value":
            change.update(newValue=None, newText="新建")
        if condition == "empty_new_fields":
            change.update(newValue=None, newText="")
        if condition == "blank_status":
            change.update(newValue=case.statuses[1], newText="")
        if condition == "unknown_status":
            change["newText"] = "占位未知状态"
        if condition == "deleted_status":
            change["isDeleted"] = True
        if condition == "empty":
            logs = []
        if condition == "undetermined_completeness":
            return {"logs": logs, "more": True}
        markers = {"flag_true": {"flag": True}, "last_mark": {"lastMark": "占位续页标记"},
                   "flag_false": {"flag": False, "lastMark": ""}}
        return {"logs": list(reversed(logs)), **markers.get(condition, {})}

    def modify_after_history(row):
        if platform.actions[-2] == "history":
            if condition == "pending_before_delete":
                row[case.control["status"]] = platform.status_cell(2)
            else:
                row["_updatedAt"] = "2026-11-20 08:00:02"

    platform.log_effect = edit_logs
    platform.logs_success = condition != "logs_failed"
    if condition == "configured_then_new":
        platform.states = [1, 2, 3, 1]
    if condition == "current_pending":
        platform.states = [1, 2]
    if condition in ("new_only", "old_configuring_only", "old_pending_only"):
        platform.states = [1]
    if condition in ("49_logs", "50_logs"):
        platform.states = [1] * int(condition[:2])
    if condition == "lower_log_limit":
        case.config["official"]["logs"]["page_limit"] = 3
    if condition == "envelope_flag_true":
        platform.log_envelope = {"flag": True}
    if condition == "default_delete_body":
        case.config["official"]["delete"]["condition"] = None
    if condition == "missing_notice":
        add_notice_recipients(case)
        platform.before_handoff = lambda: case.evidence["notices"].pop("placeholder_b")
    if condition in ("missing_approver", "missing_configurator"):
        add_notice_recipients(case)
        index = 1 if condition == "missing_approver" else 2
        platform.before_handoff = lambda: case.config["notice_recipients"][index].update(role="owner")
    if condition in ("changed_before_delete", "pending_before_delete"):
        platform.before_read = modify_after_history
    if deleted and not verified:
        platform.delete_effect = condition
    if verified:
        assert run_case(case, platform, approvals)
    else:
        message = {"missing_notice": "通知回执缺失",
                   "missing_approver": "通知名单须覆盖审批人与配置人",
                   "missing_configurator": "通知名单须覆盖审批人与配置人",
                   "old_configuring_only": "状态历史包含待配置",
                   "current_pending": "删除前状态须为新建",
                   "pending_before_delete": "删除前状态须为新建"}.get(condition)
        if condition in ("old_configuring_without_text", "old_unknown_without_text",
                         "new_configuring_with_new_text", "old_conflicting_allowed_states",
                         "empty_new_fields"):
            message = "状态历史包含待配置"
        with pytest.raises((order.Stop, KeyboardInterrupt), match=message):
            run_case(case, platform, approvals)
        assert "delete_unconfirmed" in events(case)
        assert any("需人工核对" in notice and "<at " in notice for notice in approvals.notices)
    assert ("delete" in platform.actions) == deleted
    assert ("delete_verified" in events(case)) == verified
    if condition == "missing_notice":
        assert "history" not in platform.actions
        assert any('通知回执缺失：["placeholder_b"]' in notice for notice in approvals.notices)
        assert all("占位回执" not in notice for notice in approvals.notices)
    if condition in ("missing_approver", "missing_configurator"):
        assert platform.create_count == 1
        assert "history" not in platform.actions
    assert any("删除已核验" in notice for notice in approvals.notices) == verified
    if deleted:
        idx = platform.actions.index("delete")
        assert platform.actions[idx - 2:idx] == ["history", "read"]
    assert all("<at " in notice for notice in approvals.notices)


@pytest.mark.parametrize("mode", ["both_confirmed", "name_remains", "detail_still_exists", "name_page_failed"])
def test_delete_verification_requires_missing_signal_and_no_exact_name(case, platform, mode):
    config = case.config["official"]
    view, extra = (case.ids[key] for key in ("viewId", "additional_view"))
    config["search"]["view_ids"].append(extra)
    config["page_size"] = 1
    control = config["search"]["name_control"]
    name = case.values["policy_name"]
    near_match = {**record_identity(str(uuid4())), control: name + "（占位后缀）"}
    exact_match = {**record_identity(str(uuid4())), control: name}
    platform.after_delete_query_pages = {
        (view, 1): [near_match], (view, 2): [],
        (extra, 1): [near_match], (extra, 2): [], (extra, 3): [],
    }
    expected_search = [("search", view, 1), ("search", view, 2),
                       ("search", extra, 1), ("search", extra, 2)]
    if mode == "name_remains":
        platform.after_delete_query_pages[(extra, 2)] = [exact_match]
        expected_search.append(("search", extra, 3))
    if mode == "detail_still_exists":
        platform.delete_effect = "still_exists"
    if mode == "name_page_failed":
        platform.after_delete_query_pages[(extra, 2)] = None

    approvals = Approvals(platform)
    if mode == "both_confirmed":
        assert run_case(case, platform, approvals)
        assert events(case)[-1] == "delete_verified"
    else:
        reason = {"name_remains": "删除后仍有同名记录",
                  "detail_still_exists": "删除后未收到登记的记录不存在信号",
                  "name_page_failed": "官方接口结果未确认"}[mode]
        with pytest.raises(order.Stop, match=reason):
            run_case(case, platform, approvals)
        assert events(case)[-1] == "delete_unconfirmed"
        assert "delete_verified" not in events(case)
        assert any("删除未获确认" in notice and "<at " in notice for notice in approvals.notices)
    assert platform.actions.count("delete") == 1
    assert platform.queries == [("search", view, 1), ("search", extra, 1), *expected_search]


@pytest.mark.parametrize("limit", [0, 51, True])
def test_invalid_log_page_limit(case, limit):
    case.config["official"]["logs"]["page_limit"] = limit
    with pytest.raises(order.Stop, match="官方分页上限配置无效"):
        order.Official(case.config["official"], case.official_token, case.ids["official_app"], None)


@pytest.mark.parametrize("mode", ["stable", "pending_during_wait", "timestamp_changes", "option_key_changes",
                                  "never_stable", "late_read"])
def test_deletion_waits_for_stable_status_and_timestamp(case, platform, mode):
    now, pauses = [0], []

    def pause(seconds):
        pauses.append(seconds)
        now[0] += seconds

    def update(row):
        if mode == "pending_during_wait" and now[0] >= 5:
            row[case.control["status"]] = platform.status_cell(2)
        if mode == "timestamp_changes" and now[0] >= 5:
            row["_updatedAt"] = "2026-11-20 08:00:02"
        if mode == "option_key_changes":
            row[case.control["status"]] = platform.status_cell(1)
            if now[0] == 5:
                row[case.control["status"]][0]["key"] = "占位备用选项键"
        if mode == "never_stable":
            row["_updatedAt"] = f"2026-11-20 08:00:{now[0] % 60:02d}"
        if mode == "late_read" and now[0] >= 5:
            now[0] = 121

    platform.before_read = update
    approvals = Approvals(platform)
    run = partial(run_case, case, platform, approvals, pause=pause, monotonic=lambda: now[0])
    if mode in ("never_stable", "late_read"):
        with pytest.raises(order.Stop, match="120 秒"):
            run()
        assert pauses == [5] * (24 if mode == "never_stable" else 1)
        assert "history" not in platform.actions and "delete" not in platform.actions
        assert "delete_attempt" not in events(case)
        assert platform.rows[case.ids["created"]][case.control["status"]] == platform.status_cell(1)
        assert any("需人工核对" in notice and "<at " in notice for notice in approvals.notices)
    elif mode == "pending_during_wait":
        with pytest.raises(order.Stop, match="删除前状态须为新建"):
            run()
        assert pauses == [5]
        assert "history" not in platform.actions and "delete" not in platform.actions
    else:
        assert run()
        expected_reads = {"stable": 1, "timestamp_changes": 2, "option_key_changes": 3}
        assert pauses == [5] * expected_reads[mode]
        assert platform.actions.count("read") == len(pauses) + 3


def test_official_record_interprets_naive_updated_at_as_utc_plus_eight(case, platform):
    api = order.Official(case.config["official"], case.official_token, case.ids["official_app"], None)
    row = platform.record(case.ids["created"], 1, {})
    assert api.record(row).updated_at == datetime(2026, 11, 20, 0, 0, 1, tzinfo=UTC)


@pytest.mark.parametrize("mapped", ["both", "key", "value", "neither", "conflict"])
def test_official_record_single_option_status_mapping(case, platform, mapped):
    config = case.config["official"]
    key, label = case.statuses[1], case.status_labels[1]
    config["status_map"] = {entry: "新建" for entry in {
        "both": (key, label), "key": (key,), "value": (label,), "neither": (), "conflict": (key,),
    }[mapped]}
    if mapped == "conflict":
        config["status_map"][label] = "待审批"
    api = order.Official(config, case.official_token, case.ids["official_app"], None)
    row = platform.record(case.ids["created"], 1, {})
    if mapped in ("neither", "conflict"):
        with pytest.raises(order.Stop, match="官方记录标识或状态未确认"):
            api.record(row)
    else:
        record = api.record(row)
        assert (record.status, record.platform_status) == ("新建", key)


@pytest.mark.parametrize("state", [
    [],
    pytest.param(lambda platform: platform.status_cell(1) * 2, id="duplicate-new"),
    pytest.param(lambda platform: platform.status_cell(1) + platform.status_cell(3), id="new-and-configuring"),
    ["占位选项键"],
    [{}], [{"key": "占位选项键"}], [{"value": "占位显示文本"}],
    [{"key": "", "value": "占位显示文本"}], [{"key": "占位选项键", "value": ""}],
    [{"key": 1, "value": "占位显示文本"}], [{"key": "占位选项键", "value": None}],
    {"key": "占位选项键", "value": "占位显示文本"}, True, None, 1.0,
])
def test_official_record_rejects_invalid_status_shape(case, platform, state):
    api = order.Official(case.config["official"], case.official_token, case.ids["official_app"], None)
    row = platform.record(case.ids["created"], 1, {})
    row[case.control["status"]] = state(platform) if callable(state) else state
    with pytest.raises(order.Stop, match="官方记录标识或状态未确认"):
        api.record(row)


@pytest.mark.parametrize("state", [1, '["placeholder_option_key"]'])
def test_official_record_legacy_status(case, platform, state):
    config = case.config["official"]
    config["status_map"] = {str(state): "新建"}
    api = order.Official(config, case.official_token, case.ids["official_app"], None)
    row = platform.record(case.ids["created"], 1, {})
    row[case.control["status"]] = state
    record = api.record(row)
    assert (record.status, record.platform_status) == ("新建", state)


@pytest.mark.parametrize("read_number", [1, 2, 3])
@pytest.mark.parametrize("mode", ["mismatched", "invalid", "missing"])
def test_created_detail_without_matching_row_id_stops_cleanup(case, platform, read_number, mode):
    def replace_id(row):
        if platform.actions.count("read") == read_number:
            row["_id"] = case.ids["created"]
            if mode == "missing":
                del row["rowId"]
            else:
                row["rowId"] = case.ids["template"] if mode == "mismatched" else "占位无效编号"

    platform.before_read = replace_id
    reason = "官方回读未获确认" if read_number == 1 else "官方详情记录编号未确认"
    with pytest.raises(order.Stop, match=reason):
        run_case(case, platform, Approvals(platform))
    assert "delete" not in platform.actions


def test_created_row_id_case_can_vary_between_reads(case, platform):
    case.ids["created"] = "a" + case.ids["created"][1:]

    def change_case(row):
        row["rowId"] = (case.ids["created"].upper() if platform.actions.count("read") % 2
                        else case.ids["created"])

    platform.before_read = change_case
    assert run_case(case, platform, Approvals(platform))
    assert events(case)[-1] == "delete_verified"


@pytest.mark.parametrize("effect", ["ok", "chunk_ok"])
def test_complete_create_response_is_accepted(case, platform, effect):
    platform.create_effect = effect
    assert run_case(case, platform, Approvals(platform))
    assert platform.create_count == 1
    assert "created" in events(case)


def test_chunked_response_requires_closed_stream():
    headers = Message()
    headers["Transfer-Encoding"] = "chunked"
    response = SimpleNamespace(headers=headers, status=200, chunked=True,
                               read=lambda _: b'{"id":"placeholder"}', isclosed=lambda: False)
    with pytest.raises(order.Stop, match="建单分块响应不完整"):
        order.read_create_response(response)


def test_readback_report_counts_and_modes(case, platform):
    platform.mismatch = True
    approvals = Approvals(platform)
    assert not run_case(case, platform, approvals)
    assert events(case)[-1] == "delete_verified"
    path, = case.private.glob("create-readback-*.json")
    report = json.loads(path.read_text())
    rows = report["字段比对"]
    assert all(row.keys() == {"字段名", "结果"} for row in rows)
    assert len(rows) == len(case.config["fields"])
    assert sorted(row["结果"] for row in rows if row["结果"] != "一致") == ["不一致", "缺失", "跳过"]
    assert report["可能由工作流回填"] == [{"字段名": "赠送天数", "类型": 6}]
    assert any("一致 12 / 不一致 1 / 缺失 1 / 跳过 1 / 无法核对 0" in text for text in approvals.notices)
    assert all(file.stat().st_mode & 0o777 == 0o600 for file in case.private.iterdir())


@pytest.mark.parametrize("mode", ["match", "wrong_name", "wrong_record", "incomplete"])
def test_company_is_selected_and_read_back_by_display_name(case, mode, monkeypatch):
    control, display_key, matched = company_relation(case)
    platform = Platform(case)

    def relations(key, rows):
        if key == control:
            if mode == "wrong_name":
                rows[0][display_key] = "占位回读不同显示名"
            elif mode == "wrong_record":
                rows[0]["rowId"] = str(uuid4())
            elif mode == "incomplete":
                return platform.response({"rows": [], "total": 1})
        return platform.relation_response(rows)

    platform.created_relation_response = relations
    delete_test = order.delete_test
    cleanup_calls = []

    def cleanup(*args, **kwargs):
        cleanup_calls.append(True)
        return delete_test(*args, **kwargs)

    monkeypatch.setattr(order, "delete_test", cleanup)
    approvals = Approvals(platform)
    assert run_case(case, platform, approvals) == (mode == "match")
    assert json.loads(platform.created_cells[control]) == [{"sid": matched["rowId"]}]
    assert platform.actions.count("created_relations:" + control) == 1
    path, = case.private.glob("create-readback-*.json")
    results = {row["字段名"]: row["结果"] for row in json.loads(path.read_text())["字段比对"]}
    assert results["公司"] == ("一致" if mode == "match" else "不确定")
    assert any("公司回读不确定，已保留记录，需人工核对" in text for text in approvals.notices) == (mode != "match")
    assert len(cleanup_calls) == platform.actions.count("delete") == int(mode == "match")
    assert events(case)[-1] == ("delete_verified" if mode == "match" else "readback_compared")


@pytest.mark.parametrize(("mode", "field", "result"), [
    ("success", "packages", "一致"),
    ("wrong_tag_id", "user_tags", "不一致"),
    ("wrong_package_name", "packages", "不一致"),
    ("wrong_gift_name", "packages", "不一致"),
    ("wrong_package_row_count", "packages", "不一致"),
    ("request_failed", "user_tags", "无法核对"),
    ("incomplete_relations", "packages", "无法核对"),
    ("wrong_detail_count", "packages", "不一致"),
    ("wrong_detail_count", "user_tags", "不一致"),
    ("wrong_scope", "user_scope", "不一致"),
])
def test_created_record_readback_uses_relations(case, platform, mode, field, result):
    control = case.control[field]
    packages = case.control["packages"]
    package = platform.relations[packages][0]
    package[case.child["current"]][0]["name"] = "  PLACEHOLDER__Basic  "
    case.relations[packages][0] = deepcopy(package)
    case.values["packages"][0]["current_package"] = "placeholder basic"

    def relations(key, rows):
        if key == control:
            if mode == "request_failed":
                return httpx.Response(503, json={"success": False})
            if mode == "incomplete_relations":
                return platform.response({"rows": rows, "total": len(rows) + 1})
            if mode == "wrong_tag_id":
                rows = [record_identity(str(uuid4()))]
            if mode in ("wrong_package_name", "wrong_gift_name"):
                child = case.child["current" if mode == "wrong_package_name" else "gift"]
                rows[0][child][0]["name"] = "占位其他节目包"
            if mode == "wrong_package_row_count":
                rows = []
        return platform.relation_response(rows)

    def before_read(row):
        if mode == "wrong_detail_count":
            row[control] += 1
        if mode == "wrong_package_row_count":
            row[control] = 0
        if mode == "wrong_scope":
            row[control] = case.cells[control]

    platform.created_relation_response = relations
    platform.before_read = before_read
    approvals = Approvals(platform)
    assert run_case(case, platform, approvals) == (mode == "success")
    assert events(case)[-1] == "delete_verified"
    assert platform.actions.count("created_relations:" + packages) == 1
    assert platform.actions.count("created_relations:" + case.control["user_tags"]) == 1
    assert platform.created_cells[case.control["user_scope"]] == case.values["user_scope"]
    path, = case.private.glob("create-readback-*.json")
    results = {row["字段名"]: row["结果"] for row in json.loads(path.read_text())["字段比对"]}
    assert results["用户标签"] == (result if field == "user_tags" else "一致")
    assert results["节目包"] == (result if field == "packages" else "一致")
    assert results["用户范围"] == (result if field == "user_scope" else "一致")
    assert any(f"无法核对 {int(result == '无法核对')}" in text for text in approvals.notices)


@pytest.mark.parametrize("kind", [9, 11])
@pytest.mark.parametrize("same_key", [True, False])
def test_readback_compares_single_option_by_key(case, kind, same_key):
    control = case.control["maintain_mutual_exclusion_group"]
    case.config["fields"][control]["type"] = kind
    expected = [{"controlId": control, "value": order.compact([case.ids["option"]])}]
    key = case.ids["option"] if same_key else "占位其他选项键"
    raw = {control: [{"key": key, "value": "占位显示文本"}]}
    _, counts = order.compare(expected, raw, case.config, read_relations=case.relations.__getitem__, packages=[])
    assert counts == {"一致" if same_key else "不一致": 1}


@pytest.mark.parametrize("same_id", [True, False])
def test_tag_readback_compares_uuid_without_case(case, same_id):
    control = case.control["user_tags"]
    tag_id = "A" + case.ids["tag"][1:]
    expected = [{"controlId": control, "value": order.compact([{"sid": tag_id}])}]
    rows = {control: [record_identity(tag_id.lower() if same_id else str(uuid4()))]}
    _, counts = order.compare(expected, {control: 1}, case.config, read_relations=rows.__getitem__, packages=[])
    assert counts == {"一致" if same_id else "不一致": 1}


def test_trace_lock_blocks_crash_and_allows_proven_no_send(case):
    with order.ledger_open(case.private, case.ids["registration"], case.values["policy_name"]) as ledger:
        ledger.reserve()
    with order.ledger_open(case.private, str(uuid4()), case.values["policy_name"]) as ledger:
        with pytest.raises(order.Stop, match="未清理"):
            ledger.reserve()
    with order.ledger_open(case.private, case.ids["registration"], case.values["policy_name"]) as ledger:
        ledger.append("aborted_before_write", allowed_writes=0)
        with pytest.raises(order.Stop, match="已使用"):
            ledger.reserve()
    with order.ledger_open(case.private, str(uuid4()), case.values["policy_name"]) as ledger:
        ledger.reserve()


@pytest.fixture
def live_approval(case, platform, monkeypatch):
    app, chat, user = (str(uuid4()) for _ in range(3))
    now = [case.window.start.timestamp() + 1]
    loop = asyncio.new_event_loop()
    monkeypatch.setattr(ws, "loop", loop)
    asyncio.set_event_loop(loop)
    service = FeishuService(Settings(app, str(uuid4()), chat, user, frozenset({user}), "f" * 64),
                            case.window, case.private / "state.sqlite", None, clock=lambda: now[0])
    origin = SimpleNamespace(close_code=None)
    service._connected(origin)
    end = case.window.end.timestamp()
    state = service._state
    notices = []

    def publish(binding, summary):
        platform.actions.append("card")
        approval, message, event = (str(uuid4()) for _ in range(3))
        state.bind(approval, message, binding, end)
        assert state.claim_callback(event)
        assert state.decide(approval, message, binding, "approve", event, user, lambda: now[0])
        return approval

    service.transport = SimpleNamespace(announce=notices.append)
    service.publish_approval = publish
    try:
        yield service, now, origin, notices
    finally:
        try:
            loop.run_until_complete(service._stop())
        finally:
            service._state.close()
            loop.close()
            asyncio.set_event_loop(None)


@pytest.mark.parametrize("remaining", [10, 9.999, 0])
def test_create_checks_margin_after_recording_attempt(case, platform, live_approval, remaining, monkeypatch):
    service, now, _, notices = live_approval
    append = order.Ledger.append

    def advance_after_fsync(ledger, event, *args, **kwargs):
        append(ledger, event, *args, **kwargs)
        if event == "create_attempt":
            now[0] = case.window.end.timestamp() - remaining

    monkeypatch.setattr(order.Ledger, "append", advance_after_fsync)
    assert run_case(case, platform, service, clock=lambda: now[0]) == (remaining == 10)
    assert platform.create_count == int(remaining == 10)
    assert "create_attempt" in events(case)
    assert ("aborted_before_write" in events(case)) == (remaining != 10)
    assert any("未建单" in notice for notice in notices) == (remaining != 10)


@pytest.mark.parametrize("closure", ["explicit", "disconnect", "reconnect", "close_code"])
def test_approval_closed_during_query_aborts_before_transport(case, platform, live_approval, closure):
    service, now, origin, notices = live_approval

    def close():
        if closure == "disconnect":
            service._disconnected()
        if closure == "reconnect":
            service._connected(origin)
        if closure == "close_code":
            origin.close_code = 1000

    if closure == "explicit":
        real_publish = service.publish_approval

        def publish(binding, summary):
            approval = real_publish(binding, summary)
            platform.before_search = lambda: service.close_create_approval(approval, binding)
            return approval

        service.publish_approval = publish
    else:
        platform.before_search = close

    assert not run_case(case, platform, service, clock=lambda: now[0])
    assert platform.create_count == 0
    assert "aborted_before_write" in events(case)
    assert any("未建单" in notice for notice in notices)


def test_closed_socket_after_recording_attempt_aborts_before_transport(case, platform, live_approval, monkeypatch):
    service, now, origin, notices = live_approval
    append = order.Ledger.append

    def close_socket_after_fsync(ledger, event, *args, **kwargs):
        append(ledger, event, *args, **kwargs)
        if event == "create_attempt":
            origin.close_code = 1000

    monkeypatch.setattr(order.Ledger, "append", close_socket_after_fsync)
    assert not run_case(case, platform, service, clock=lambda: now[0])
    assert platform.create_count == 0
    assert events(case)[-3:] == ["create_attempt", "aborted_before_write", "approval_closed"]
    assert any("未建单" in notice for notice in notices)


def test_disconnect_waits_until_transport_handoff(case, platform, live_approval):
    service, now, _, _ = live_approval
    disconnect_waiting, disconnected = Event(), Event()
    lock = service._lock

    def disconnect():
        service._disconnected()
        disconnected.set()

    worker = Thread(target=disconnect)

    class ObservedLock:
        def __enter__(self):
            if current_thread() is worker:
                disconnect_waiting.set()
            return lock.__enter__()

        def __exit__(self, *error):
            return lock.__exit__(*error)

    service._lock = ObservedLock()

    def at_endheaders():
        worker.start()
        assert disconnect_waiting.wait(timeout=2)
        assert not disconnected.is_set()
        assert not service._approval_disconnected.is_set()

    platform.before_handoff = at_endheaders
    try:
        assert run_case(case, platform, service, clock=lambda: now[0])
    finally:
        if worker.ident is not None:
            worker.join(timeout=2)
    assert disconnected.is_set()
    assert platform.create_count == 1
    assert "created" in events(case) and "aborted_before_write" not in events(case)


@pytest.mark.parametrize("mode", [
    "success", "missing_review", "digest_changed", "template_conflict", "delete_interrupted",
    "draft_tag", "draft_tag_missing", "draft_tag_ambiguous", "draft_tag_duplicate_id", "draft_tag_empty",
    "package_count_mismatch", "tag_count_mismatch", "tag_count_boolean", "relation_total_mismatch",
    "login_changed_after_approval", "normal_notice", "missing_notice", "duplicate_receipt",
    "missing_approver", "missing_configurator", "waiver_with_configurator", "empty_waiver",
    "waiver_without_receipt",
    "unexpected_notice", "empty_recipients", "duplicate_recipient", "malformed_recipient",
    "template_mismatched_id", "template_only_legacy_id",
    "missing_user_scope", "duplicate_draft_package",
])
def test_cli_with_private_registration(case, platform, mode, monkeypatch, capsys):
    private = case.private
    config_path = private / "config.json"
    draft_path = private / "draft.json"
    login_path = private / "mingdao-state.json"
    env_file = private / "env"
    if mode in ("normal_notice", "missing_notice", "duplicate_receipt", "missing_approver",
                "missing_configurator"):
        add_notice_recipients(case)
    if mode == "normal_notice":
        case.config["notice_recipients"].pop(0)
        del case.evidence["notices"]["placeholder_owner"]
    if mode in ("missing_approver", "missing_configurator"):
        case.config["notice_recipients"][1 if mode == "missing_approver" else 2]["role"] = "owner"
    if mode == "waiver_with_configurator":
        case.config["notice_recipients"][0]["role"] = "configurator"
    if mode == "empty_waiver":
        case.evidence["notice_waiver"] = ""
    if mode == "waiver_without_receipt":
        del case.evidence["notices"]["placeholder_owner"]
    if mode == "empty_recipients":
        case.config["notice_recipients"].clear()
    if mode == "duplicate_recipient":
        case.config["notice_recipients"].extend(case.config["notice_recipients"][:1])
    if mode == "malformed_recipient":
        case.config["notice_recipients"][0]["key"] = []
    if mode == "missing_notice":
        del case.evidence["notices"]["placeholder_b"]
    if mode == "duplicate_receipt":
        case.evidence["notices"]["placeholder_b"] = " " + case.evidence["notices"]["placeholder_owner"]
    if mode == "unexpected_notice":
        case.evidence["notices"]["placeholder_unlisted"] = "占位未列回执"
    if mode == "template_mismatched_id":
        platform.template["rowId"] = case.ids["created"]
    if mode == "template_only_legacy_id":
        platform.template["_id"] = platform.template.pop("rowId")
    tag_control, package_control = (case.control[key] for key in ("user_tags", "packages"))
    name, alias = case.config["official"]["tag_relation"]["name_keys"]
    if mode in ("draft_tag", "draft_tag_missing", "draft_tag_ambiguous", "draft_tag_duplicate_id"):
        case.values["user_tags"] = ["占位标签乙"]
        case.ids["tag"] = "A" + case.ids["tag"][1:]
        row = {**record_identity(case.ids["tag"]), name: "占位标签甲", alias: "占位标签乙"}
        platform.relations[tag_control] = [row]
        if mode == "draft_tag_missing":
            row[alias] = "占位标签丙"
        if mode == "draft_tag_ambiguous":
            platform.relations[tag_control].append({**record_identity(str(uuid4())), name: "占位标签乙"})
            platform.template[tag_control] = 2
        if mode == "draft_tag_duplicate_id":
            case.values["user_tags"].insert(0, "占位标签甲")
    if mode == "draft_tag_empty":
        case.values["user_tags"] = []
    if mode == "package_count_mismatch":
        platform.template[package_control] = 2
    if mode == "tag_count_mismatch":
        platform.template[tag_control] = 0
    if mode == "tag_count_boolean":
        platform.template[tag_control] = True
    if mode == "relation_total_mismatch":
        platform.relation_response = lambda rows: platform.response({"rows": rows, "total": len(rows) + 1})
    draft_inputs = order_fixture(case, platform)
    order.exclusive(config_path, case.config)
    draft = {**deepcopy(case.values), **draft_inputs}
    if mode == "template_conflict":
        set_mutex_template(case, platform, "否")
    if mode == "missing_user_scope":
        del draft["user_scope"]
    if mode == "duplicate_draft_package":
        draft["packages"] = [
            {"current_package": "PLACEHOLDER__Basic", "gift_package": "PLACEHOLDER_Gift"},
            {"current_package": " placeholder basic ", "gift_package": "placeholder gift"},
        ]
    order.exclusive(draft_path, private_export(draft))
    order.exclusive(login_path, {"cookies": [{
        "name": case.config["create"]["session_cookie"], "value": case.session_token,
        # 明道云真实下发形态：父域、会话期、未标 secure（2026-11-20 X5 首跑实测）
        "domain": ".mingdao.com", "path": "/", "secure": False, "expires": -1,
    }]})
    official_key = case.config["official"]["token_key"]
    env_file.write_text("\n".join([
        f"{official_key}={case.official_token}",
        f"{case.config['official']['app_id_key']}={case.ids['official_app']}",
        f"FEISHU_APP_ID={uuid4()}",
        f"FEISHU_APP_SECRET={uuid4()}",
        f"FEISHU_CHAT_ID={uuid4()}",
        f"FEISHU_BOT_OPEN_ID={uuid4()}",
        f"FEISHU_ALLOWED_SENDER_IDS={uuid4()}",
        f"FEISHU_APPROVED_FINGERPRINT={'f' * 64}",
    ]))
    env_file.chmod(0o600)
    case.evidence["policy_id"] = "p1"
    for key, path in (("config", config_path), ("draft", draft_path), ("login", login_path)):
        case.evidence[key + "_sha256"] = order.digest(path.read_bytes())
    if mode == "missing_review":
        del case.evidence["two_reviews"]["external"]
    if mode == "digest_changed":
        draft_path.write_bytes(draft_path.read_bytes() + b"\n")
    if mode == "delete_interrupted":
        platform.delete_effect = "interrupted"
    order.exclusive(private / "create-authorizations.json", {case.ids["registration"]: case.evidence})
    monkeypatch.chdir(private.parent)
    approvals = Approvals(platform)
    approvals.run_create = lambda work: work(Event())
    if mode == "login_changed_after_approval":
        def rewrite_login():
            login_path.write_bytes(login_path.read_bytes() + b"\n")

        platform.before_search = rewrite_login
    monkeypatch.setattr(order, "run_order", partial(order.run_order, pause=lambda _: None, monotonic=lambda: 0))
    monkeypatch.setattr(order, "FeishuService", lambda *args, **kwargs: approvals)
    monkeypatch.setattr(order, "CreateRequest", partial(order.CreateRequest, connect=platform.connection))
    monkeypatch.setattr(httpx, "Client", partial(httpx.Client, transport=httpx.MockTransport(platform.official)))

    success = mode in ("success", "normal_notice", "draft_tag")
    assert order.main(["--config", str(config_path), "--registration", case.ids["registration"],
                       "--env-file", str(env_file), str(draft_path)]) == (0 if success else 1)
    captured = capsys.readouterr()
    assert ("D15 删除已核验" in captured.out) == success
    assert platform.create_count == int(success or mode == "delete_interrupted")
    notice_errors = {
        "missing_notice": '通知回执缺失：["placeholder_b"]',
        "duplicate_receipt": "通知回执重复",
        "unexpected_notice": "通知回执含未登记对象",
        "empty_recipients": "通知名单配置无效",
        "duplicate_recipient": "通知名单配置无效",
        "malformed_recipient": "通知名单配置无效",
        "missing_approver": "通知名单须覆盖审批人与配置人",
        "missing_configurator": "通知名单须覆盖审批人与配置人",
        "waiver_with_configurator": "免通知名单须仅含产品负责人",
        "empty_waiver": "免通知登记无效",
        "waiver_without_receipt": '通知回执缺失：["placeholder_owner"]',
    }
    if mode in notice_errors:
        assert captured.err == f"已停止：{notice_errors[mode]}\n"
        assert not platform.actions
    if mode == "missing_notice":
        path, = private.glob("create-preflight-*.json")
        assert json.loads(path.read_text())["通知回执"] == [
            {"通知对象": "占位通知对象甲", "已回执": True},
            {"通知对象": "占位通知对象乙", "已回执": False},
            {"通知对象": "占位通知对象丙", "已回执": True},
        ]
        assert "占位免通知回执" not in path.read_text()
    if mode == "login_changed_after_approval":
        assert "aborted_before_write" in events(case)
        assert any("建单登录状态已变化" in notice for notice in approvals.notices)
    if mode in ("template_mismatched_id", "template_only_legacy_id"):
        assert "官方详情记录编号未确认" in captured.err
        assert platform.actions == ["template"]
    if mode == "draft_tag":
        assert json.loads(platform.created_cells[case.control["user_tags"]]) == [{"sid": case.ids["tag"]}]
    template_relations = ["template", "relations:" + package_control, "relations:" + tag_control]
    if success:
        assert platform.actions[:4] == [*template_relations, "card"]
    if mode in ("draft_tag_missing", "draft_tag_ambiguous"):
        assert "用户标签与模板不同，需人工" in captured.err
        assert platform.actions == template_relations
    if mode == "draft_tag_duplicate_id":
        assert "不同用户标签对应同一条记录，需人工" in captured.err
        assert platform.actions == template_relations
    if mode == "package_count_mismatch":
        assert "模板节目包字段计数与关联行总数不一致" in captured.err
        assert platform.actions == template_relations[:2]
    if mode in ("tag_count_mismatch", "tag_count_boolean"):
        assert "模板用户标签字段计数与关联行总数不一致" in captured.err
        assert platform.actions == (template_relations if mode == "tag_count_mismatch" else template_relations[:2])
    if mode == "relation_total_mismatch":
        assert "官方关联行结果不完整" in captured.err
        assert platform.actions == template_relations[:2]
    if mode == "draft_tag_empty":
        assert "用户标签为空，需人工" in captured.err
        assert not platform.actions
    if mode in ("missing_user_scope", "duplicate_draft_package"):
        reason = "草稿缺少必填字段" if mode == "missing_user_scope" else "节目包行重复，需人工"
        assert f"已停止：{reason}\n" == captured.err
        assert not platform.actions
    assert all(timeout["connect"] == 10 and timeout["read"] == 30
               for timeout in platform.timeouts if isinstance(timeout, dict))
    if mode == "template_conflict":
        path, = private.glob("create-preflight-*.json")
        assert "互斥组" in json.loads(path.read_text())["原因"]
        assert not approvals.notices
    if mode == "delete_interrupted":
        assert "执行中断" in captured.err
        assert "delete_verified" not in events(case)
    if success:
        summary_path, = private.glob("create-fields-*.json")
        material = json.loads(summary_path.read_text())
        assert len(material["字段"]) == len(case.config["fields"])
        assert material["通知回执"] == [
            {"通知对象": row["label"], "已回执": True} for row in case.config["notice_recipients"]
        ]
        assert "合成模板" not in summary_path.read_text()
        assert summary_path.stat().st_mode & 0o777 == 0o600
        assert all(value not in summary_path.read_text() for value in case.evidence["notices"].values())
    assert all(value not in captured.out + captured.err for value in case.evidence["notices"].values())
    for token in (case.session_token, case.official_token, case.ids["official_app"]):
        assert token not in captured.out + captured.err


def test_extract_token_accepts_real_mingdao_cookie_shape_and_keeps_other_checks():
    # 建单固定经 HTTPS 发往登记主机，cookie 是否标 secure 不影响传输安全，不作为门禁。
    real = {"name": "md_pss_id", "value": "placeholder-session-value", "domain": ".mingdao.com",
            "path": "/", "secure": False, "expires": -1}
    assert order.extract_token({"cookies": [real]}, "md_pss_id") == "placeholder-session-value"
    for bad in ({"domain": "evil.example"}, {"path": "/other"}, {"expires": 1.0}, {"expires": "never"}):
        with pytest.raises(order.Stop, match="登录会话无法核实"):
            order.extract_token({"cookies": [{**real, **bad}]}, "md_pss_id")
    with pytest.raises(order.Stop, match="登录会话无法核实"):
        order.extract_token({"cookies": [real, dict(real)]}, "md_pss_id")


def order_fixture(case, platform):
    # X5 名称与标签共同使用测试前缀；保留模拟主名、别名及歧义关系。
    tags = ["【测试】" + tag for tag in case.values["user_tags"]]
    case.values["user_tags"] = tags
    if tags:
        case.values["policy_name"] = tags[0]
    for row in platform.relations[case.control["user_tags"]]:
        for key in case.config["official"]["tag_relation"]["name_keys"]:
            if key in row:
                row[key] = "【测试】" + row[key]
    today = case.window.start.date()
    values = deepcopy(case.values)
    values.update(start_date=(today + timedelta(days=1)).isoformat(),
                  end_date=(today + timedelta(days=4)).isoformat(),
                  policy_type="定向政策" if values["user_tags"] else "公开政策",
                  benefit_type="赠送时长", cancel_on_downgrade="否", trigger_once="否",
                  repeat_bonus="是", effective_multiplier=1)
    for key, label in (("policy_type", "政策类型"), ("benefit_type", "优惠类型"),
                       ("cancel_on_downgrade", "降包用户取消优惠"), ("trigger_once", "只触发一次"),
                       ("repeat_bonus", "多买多送"), ("effective_multiplier", "生效倍数")):
        control = case.control[key] = str(uuid4())
        spec = {"type": 6, "label": label, "draft": key}
        value = values[key]
        if key != "effective_multiplier":
            option = str(uuid4())
            spec.update(type=11, draft_values={order.compact(value): [option]})
            value = [{"key": option, "value": value}]
        case.config["fields"][control] = spec
        platform.template[control] = value
    set_mutex_template(case, platform, "是")
    gift = "合成当前包_copy"
    values["packages"][0]["gift_package"] = gift
    platform.relations[case.control["packages"]][0][case.child["gift"]][0]["name"] = gift
    columns = ("record_type,company_id,company_name,offer_id,offer_name,offer_type,is_synchronized,"
               "parent_offer_id,offer_level_type,offer_level,status,sale_start_date,sale_end_date")
    package = case.ids["package"]
    case.config["order_snapshot"] = {
        "version": "synthetic-1", "companies": [{"id": "01", "name": "合成公司", "country": "合成国家"}],
        "tags": [{"name": name, "id": case.ids["tag"], "end": values["end_date"], "shadow": False}
                 for name in values["user_tags"]],
        "package_levels": "\n".join([columns,
            f"OFFER,01,合成公司,{package},合成当前包,基本,否,,A,1,在售,2000-01-01,2100-01-01",
            f"OFFER,01,合成公司,{case.ids['gift_package']},{gift},附属,是,{package},A,1,在售,2000-01-01,2100-01-01"]),
    }
    case.config["order_snapshot"]["companies"][0]["keyword"] = "合成公司"
    return {key: value for key, value in values.items() if value != case.values.get(key)}


def set_mutex_template(case, platform, value):
    control = case.control["maintain_mutual_exclusion_group"]
    option, = case.config["fields"][control]["draft_values"][order.compact(value)]
    platform.template[control] = [{"key": option, "value": value}]


def private_export(values):
    # 独立合成来源，各引文以来源坐标标识。模型字段值可在测试中单独变动。
    text = ""
    results = {}
    fields = {}
    for name, value in values.items():
        examples = {"company": "本活动由合成公司办理", "user_scope": "适用于合成召回用户",
                    "packages": "当前节目包为合成当前包", "recharge_days": "充三十天",
                    "gift_days": "送三十一天", "benefit_type": "活动赠送时长",
                    "trigger_once": "允许重复触发", "repeat_bonus": "多买多送",
                    "effective_multiplier": "生效倍数为一", "cancel_on_downgrade": "降包后保留优惠",
                    "maintain_mutual_exclusion_group": "需要维护互斥组"}
        quote_text = examples.get(name, f"{name}（合成申请）{value}")
        span = {"source_id": "body", "start": len(text), "end": len(text) + len(quote_text)}
        text += quote_text + "\n"
        quotes = [{"span": span, "text": quote_text}]
        fields[name] = {"value": value, "uncertainty": None, "evidence": [
            {"text": quote_text, "start": span["start"]}]}
        results[name] = {"value": value, "has_value": True, "needs_confirmation": False,
                         "issues": [], "source": "原文", "evidence": quotes, "input_fields": [],
                         "statement": {"value": value, "status": "确定", "evidence": quotes, "candidates": []}}
    for name, value, literals in (
        ("country", "合成国家", ["合成国家"]),
        ("company_keyword", ["合成公司"], ["合成公司"]),
        ("user_tags", values["user_tags"], values["user_tags"]),
    ):
        quotes = []
        for literal in literals:
            span = {"source_id": "body", "start": len(text), "end": len(text) + len(literal)}
            quotes.append({"text": literal, "span": span})
            text += literal + "\n"
        results[name] = {"value": value, "has_value": True, "needs_confirmation": False, "source": "原文",
                         "issues": [], "evidence": quotes, "input_fields": [],
                         "statement": {"status": "确定", "value": value, "evidence": quotes, "candidates": []}}
        if name == "user_tags":
            fields[name]["evidence"] = [{"text": q["text"], "start": q["span"]["start"]} for q in quotes]
    if "policy_type" in results and "user_tags" in results:
        tag_quote = results["user_tags"]["evidence"]
        result = results["policy_type"]
        result.update(source="查表派生", input_fields=["user_tags"], evidence=tag_quote, rule="R8")
        result["statement"].update(value=None, evidence=[], status="原文未写")
        fields["policy_type"]["evidence"] = deepcopy(fields["user_tags"]["evidence"])
    if values.get("packages") and all(row["gift_package"] for row in values["packages"]):
        results["packages"].update(source="查表派生", rule="6.2.R6",
                                   input_fields=["benefit_type", "company", "start_date", "packages"])
        results["packages"]["statement"]["value"] = [
            {"current_package": row["current_package"],
             "gift_package": None if values.get("benefit_type") == "赠送时长" else row["gift_package"].removesuffix("_copy")}
            for row in values["packages"]]
    scope = {"id": "p1", "main": {"source_id": "body", "start": 0, "end": 1}, "continuations": [],
             "has_title": True}
    return {"drafts": [{**fields, "policy_id": "p1", "scope": scope, "field_results": results}],
            "sources": [{"id": "body", "source_id": "body", "start": 0, "text": text}],
            "requires_full_review": False, "structure_issues": []}


@pytest.fixture
def business_cli(case, platform, monkeypatch, capsys):
    private = case.private
    case.values.update(order_fixture(case, platform))
    now = [case.window.start.timestamp() + 1]
    approvals = Approvals(platform)
    summaries = []
    publish = approvals.publish_approval

    def record_card(binding, summary):
        summaries.append(summary)
        return publish(binding, summary)

    approvals.publish_approval = record_card
    approvals.run_create = lambda work: work(Event())
    monkeypatch.chdir(private.parent)
    monkeypatch.setattr(order, "_Transport", lambda *args: approvals.transport)
    monkeypatch.setattr(order, "FeishuService", lambda *args, **kwargs: approvals)
    monkeypatch.setattr(order, "load_settings", lambda _: None)
    monkeypatch.setattr(order, "run", partial(order.run, clock=lambda: now[0]))
    monkeypatch.setattr(order, "run_order", partial(order.run_order, pause=lambda _: None, monotonic=lambda: 0))
    monkeypatch.setattr(order, "CreateRequest", partial(order.CreateRequest, connect=platform.connection))
    monkeypatch.setattr(httpx, "Client", partial(httpx.Client, transport=httpx.MockTransport(platform.official)))

    def run(export=None, *, tamper=None):
        case.evidence["policy_id"] = "p1"
        login = {"cookies": [{"name": case.config["create"]["session_cookie"], "value": case.session_token,
                              "domain": ".mingdao.com", "path": "/", "secure": False, "expires": -1}]}
        for key, filename, material in (("draft", "draft.json", private_export(case.values) if export is None else export),
                                        ("config", "config.json", case.config), ("login", "mingdao-state.json", login)):
            path = private / filename
            order.exclusive(path, material)
            case.evidence[key + "_sha256"] = order.digest(path.read_bytes())
            if tamper == key:
                path.write_bytes(path.read_bytes() + b" ")
        order.exclusive(private / "create-authorizations.json", {case.ids["registration"]: case.evidence})
        settings = private / "synthetic-settings"
        settings.write_text(f"PLACEHOLDER_OFFICIAL_TOKEN={case.official_token}\n"
                            f"PLACEHOLDER_OFFICIAL_APP={case.ids['official_app']}\n")
        code = order.main(["--config", str(private / "config.json"), "--registration", case.ids["registration"],
                           "--env-file", str(settings), str(private / "draft.json")])
        return code, capsys.readouterr().err

    return SimpleNamespace(run=run, summaries=summaries, now=now, notices=approvals.notices)


@pytest.mark.parametrize(("mode", "reason"), [
    ("legacy", "整批来源证据"), ("missing_type", "优惠类型"), ("mutex_no", "互斥组"),
    ("public", "政策类型"), ("invalid_date", "日期"), ("expired", "调整后晚于"),
    ("no_date", "标签截止日"), ("alias_conflict", "截止日登记冲突"), ("late", "晚于标签截止日"),
    ("bad_quote", "可核证据"), ("no_quote", "可核证据"), ("exclusive", "归属声明矛盾"),
    ("stale_watch", "watch_days"), ("stale_penalty", "penalty_days"), ("inactive", "watch_days"),
    ("no_gift", "销售品"), ("wrong_parent", "销售品"), ("duplicate_offer", "销售品"),
    ("other_gift_id", "销售品"), ("other_tag_id", "记录标识"), ("wrong_label", "优惠类型"),
    ("wrong_key", "优惠类型"), ("ambiguous_key", "优惠类型"), ("replaced", "优惠类型"),
    ("upgrade", "after_offer"), ("special", "amount_mode"), ("bad_default", "可核证据"),
    ("penalty_format", "penalty_days"), ("missing_scope", "整批结构"), ("missing_target", "目标政策"),
])
def test_order_business_stops(case, platform, business_cli, mode, reason, monkeypatch):
    values = case.values
    snapshot = case.config["order_snapshot"]
    tag = snapshot["tags"][0]
    benefit = case.control["benefit_type"]
    gift = platform.relations[case.control["packages"]][0][case.child["gift"]]
    if mode == "missing_type":
        del values["benefit_type"]
    elif mode == "mutex_no":
        values["maintain_mutual_exclusion_group"] = "否"
        set_mutex_template(case, platform, "否")
    elif mode == "public":
        values["policy_type"] = "公开政策"
    elif mode == "invalid_date":
        values["start_date"] = "2025-02-29"
    elif mode == "expired":
        values.update(start_date="2000-01-01", end_date="2000-01-02")
    elif mode == "no_date":
        tag.pop("end")
    elif mode == "late":
        tag["end"] = values["start_date"]
    elif mode == "alias_conflict":
        snapshot["tags"].append({**tag, "name": tag["name"] + " ", "end": values["start_date"]})
    elif mode in ("stale_watch", "stale_penalty"):
        name = "watch_days" if mode == "stale_watch" else "penalty_days"
        control = str(uuid4())
        case.config["fields"][control] = {"type": 6, "label": name, "draft": name}
        platform.template[control] = "9"
    elif mode == "inactive":
        values["watch_days"] = 12
    elif mode == "no_gift":
        gift.clear()
        values["packages"][0]["gift_package"] = None
    elif mode == "wrong_parent":
        snapshot["package_levels"] = snapshot["package_levels"].replace("是," + case.ids["package"], "是,wrong")
    elif mode == "duplicate_offer":
        snapshot["package_levels"] += "\n" + snapshot["package_levels"].splitlines()[-1]
    elif mode == "other_gift_id":
        gift[0]["sid"] = str(uuid4())
    elif mode == "other_tag_id":
        tag["id"] = str(uuid4())
    elif mode == "wrong_label":
        platform.template[benefit][0]["value"] += " "
    elif mode == "wrong_key":
        platform.template[benefit][0]["key"] = str(uuid4())
    elif mode == "ambiguous_key":
        mapping = case.config["fields"][benefit]["draft_values"]
        mapping[order.compact("其他")] = next(iter(mapping.values()))
    elif mode == "replaced":
        payload = order.CreateRequest.func.payload
        def corrupt(self, controls):
            cells = deepcopy(controls)
            next(cell for cell in cells if cell["controlId"] == benefit)["value"] = "[]"
            return payload(self, cells)
        monkeypatch.setattr(order.CreateRequest.func, "payload", corrupt)
    elif mode == "upgrade":
        values["benefit_type"] = "升级权益"
    elif mode == "special":
        values.update(benefit_type="特价优惠", after_offer="恢复原包", forbid_package_change="是",
                      watch_days=3, recharge_amount=5)
        del values["recharge_days"], values["gift_days"]
    elif mode == "penalty_format":
        values["penalty_days"] = -1

    export = private_export(values)
    draft = export["drafts"][0]
    scope = draft["field_results"]["user_scope"]
    if mode == "legacy":
        export = {key: {"value": v} for key, v in values.items()}
    elif mode == "bad_quote":
        scope["evidence"][0]["span"]["start"] += 1
    elif mode == "no_quote":
        scope["evidence"] = []
    elif mode == "exclusive":
        other_policy(export, scope["evidence"][0]["span"], exclusive=True)
    elif mode == "bad_default":
        draft["field_results"]["maintain_mutual_exclusion_group"]["source"] = "默认规则"
    elif mode == "missing_scope":
        export["requires_full_review"] = True
    elif mode == "missing_target":
        draft["scope"]["id"] = draft["policy_id"] = "p2"
    code, error = business_cli.run(export)
    assert code == 1 and reason in error
    assert business_cli.summaries == [] and platform.create_count == 0


def other_policy(export, span, *, exclusive):
    scope = {"id": "p2", "main": span, "continuations": []}
    if exclusive:
        scope["exclusive_spans"] = [span]
    export["drafts"].append({"policy_id": "p2", "scope": scope})


@pytest.mark.parametrize("end_today", [True, False])
def test_business_card_and_request(case, platform, business_cli, end_today):
    today = datetime.fromtimestamp(business_cli.now[0], UTC).date().isoformat()
    case.values["start_date"] = "2000-01-01"
    if end_today:
        case.values["end_date"] = today
    export = private_export(case.values)
    results = export["drafts"][0]["field_results"]
    for key in ("maintain_mutual_exclusion_group", "effective_multiplier"):
        results[key].update(source="默认规则", evidence=[])
        results[key]["statement"].update(status="原文未写", evidence=[], value=None)
        export["drafts"][0][key]["evidence"] = []
    other_policy(export, results["start_date"]["evidence"][0]["span"], exclusive=False)
    code, error = business_cli.run(export)
    assert (code, error, platform.create_count) == (0, "", 1)
    card, = business_cli.summaries
    assert all(text in card for text in ("政策类型按用户标签判定", "互斥组按默认填是", "生效倍数按默认填 1",
                                        "优惠类型：赠送时长", "互斥组：是", "政策类型：定向政策",
                                        f"开始日期由 2000-01-01 调整至 {today}"))
    for name in ("start_date", "end_date", "user_scope"):
        assert platform.created_cells[case.control[name]] in card


@pytest.mark.parametrize("key", ["config", "draft"])
def test_registered_order_material_cannot_change(business_cli, platform, key):
    code, error = business_cli.run(tamper=key)
    assert code == 1 and "摘要与登记不符" in error
    assert not platform.actions and platform.create_count == 0


@pytest.mark.parametrize("mode", ["downgrade", "midnight"])
def test_approval_cannot_release_unconfirmed_value(case, platform, business_cli, mode):
    if mode == "midnight":
        today = case.window.start.date()
        case.values["start_date"] = today.isoformat()
        tomorrow = datetime.combine(today + timedelta(days=1), datetime.min.time(), UTC)
        case.evidence["window"]["end"] = (tomorrow + timedelta(days=1)).isoformat()
        platform.before_search = lambda: business_cli.now.__setitem__(0, tomorrow.timestamp())
    export = private_export(case.values)
    if mode == "downgrade":
        key = "cancel_on_downgrade"
        draft = export["drafts"][0]
        draft[key].update(value=None, uncertainty={"reason": "待确认", "candidates": []}, evidence=[])
        result = draft["field_results"][key]
        result.update(value=None, source=None, has_value=False, needs_confirmation=True, evidence=[],
                      issues=["downgrade_confirmation"])
        result["statement"].update(value=None, status="原文未写", evidence=[])
    code, _ = business_cli.run(export)
    assert code == 1 and platform.create_count == 0
    card, = business_cli.summaries
    if mode == "downgrade":
        assert "降包用户取消优惠待确认，请选择是或否" in card
    reason = "降包用户取消优惠" if mode == "downgrade" else "开始日期"
    assert any("未建单" in notice and reason in notice for notice in business_cli.notices)


def test_order_evidence_masks_intersection_with_loaded_secret(case, platform, business_cli):
    # 引文只取合成凭据中间一段，字段值是模型概括出的自由文本。
    secret = case.session_token
    fragment = secret[4:13]
    case.values["user_scope"] = "合成范围" + fragment
    export = private_export(case.values)
    block = export["sources"][0]
    start = len(block["text"]) + 4
    block["text"] += secret
    draft = export["drafts"][0]
    quote = {"text": fragment, "span": {"source_id": "body", "start": start, "end": start + len(fragment)}}
    result = draft["field_results"]["user_scope"]
    result["evidence"] = result["statement"]["evidence"] = [quote]
    draft["user_scope"]["evidence"] = [{"text": fragment, "start": start}]
    code, error = business_cli.run(export)
    assert code == 1 and "凭据" in error
    assert not business_cli.summaries
    assert platform.create_count == 0
    assert fragment not in error + "".join(business_cli.notices)


@pytest.mark.parametrize(("name", "mutate", "reason"), [
    ("maintain_mutual_exclusion_group", "candidate", "可核证据"),
    ("user_scope", "uncertain", "待确认"), ("effective_multiplier", "value", "生效倍数"),
    ("trigger_once", "value", "触发次数"), ("policy_type", "dependency", "可核证据"),
    ("benefit_type", "fixed", "优惠类型"), ("benefit_type", "skip", "优惠类型"),
    ("benefit_type", "prefix", "优惠类型"), ("benefit_type", "encoding", "优惠类型"),
    ("end_date", "calendar", "日期"), ("penalty_days", "required", "penalty_days"),
])
def test_business_field_proof(case, platform, business_cli, name, mutate, reason):
    export = private_export(case.values)
    draft = export["drafts"][0]
    if mutate == "candidate":
        draft["field_results"][name]["statement"]["candidates"].append({"value": "否", "evidence": []})
    elif mutate == "uncertain":
        draft[name]["uncertainty"] = {"reason": "带条件"}
    elif mutate == "value":
        draft[name]["value"] = 2 if name == "effective_multiplier" else "是"
    elif mutate == "dependency":
        draft["field_results"][name]["input_fields"] = ["company"]
    elif mutate == "fixed":
        applicant = case.config["applicant_control"]
        control = case.control[name]
        del case.config["fields"][control]["draft"]
        case.config["fields"][applicant]["draft"] = name
    elif mutate == "skip":
        case.config["readback_skip"].append(case.control[name])
    elif mutate == "prefix":
        platform.template[case.control[name]][0]["value"] = "赠送"
    elif mutate == "encoding":
        case.config["fields"][case.control[name]]["draft_values"][order.compact("赠送时长")] = [str(uuid4())]
    elif mutate == "calendar":
        draft[name]["value"] = "2026-13-31"
    elif mutate == "required":
        draft["field_results"][name] = {"statement": {"status": "确定", "value": 10, "candidates": []}}
    code, error = business_cli.run(export)
    assert code == 1 and reason in error
    assert not business_cli.summaries and platform.create_count == 0


@pytest.mark.parametrize(("kind", "change", "reason"), [
    ("升级权益", "valid", "after_offer"), ("特价优惠", "valid", "after_offer"),
    ("升级权益", "inactive", "watch_days"), ("特价优惠", "inactive", "gift_days"),
    ("特价优惠", "bad_amount", "recharge_amount"), ("特价优惠", "no_rate", "资费"),
    ("特价优惠", "wrong_total", "充值金额"), ("特价优惠", "rate", "after_offer"),
    ("升级权益", "missing_target", "目标节目包"),
])
def test_conditional_offer_before_projection(case, platform, business_cli, kind, change, reason):
    values = case.values
    values.update(benefit_type=kind, after_offer="恢复原包", forbid_package_change="是")
    if kind == "特价优惠":
        del values["recharge_days"], values["gift_days"]
        values.update(watch_days=2, amount_mode="自定义", recharge_amount=8)
    if change == "inactive":
        values["watch_days" if kind == "升级权益" else "gift_days"] = 5
    if change == "bad_amount":
        values["recharge_amount"] = -1
    if change == "missing_target":
        values.update(after_offer="指定节目包", after_offer_package="合成目标包")
    if change in ("no_rate", "wrong_total", "rate"):
        ref = {"base_package": "合成当前包", "price_plan": "合成计划", "unit": "月", "version": "1"}
        values.update(amount_mode="节目包资费", tariff=ref, tariff_multiplier=1.5,
                      recharge_amount=8 if change == "wrong_total" else 7)
        if change != "no_rate":
            case.config["order_snapshot"]["benefit_catalog"] = {
                "tariffs": [{**ref, "amount": 5, "valid_from": values["start_date"]}]}
    code, error = business_cli.run()
    assert code == 1 and reason in error
    assert not business_cli.summaries and platform.create_count == 0


def test_shadow_tag_alias_mixed_with_ordinary_tag(case, platform, business_cli):
    keys = case.config["official"]["tag_relation"]["name_keys"]
    control = case.control["user_tags"]
    selected, = platform.relations[control]
    selected[keys[1]], selected[keys[0]] = selected[keys[0]], "合成主名_image"
    ordinary = {**record_identity(str(uuid4())), keys[0]: "合成普通标签"}
    platform.relations[control].append(ordinary)
    platform.template[control] = 2
    case.values["user_tags"].append(ordinary[keys[0]])
    case.config["order_snapshot"]["tags"].append({
        "name": ordinary[keys[0]], "id": ordinary["rowId"], "end": case.values["end_date"], "shadow": False})
    code, error = business_cli.run()
    assert code == 1 and "影子标签须单选" in error
    assert not business_cli.summaries and platform.create_count == 0


def test_rendered_full_secret_stops_before_card(case, platform, business_cli, monkeypatch):
    original = order._CardRedaction.display

    def broken_renderer(self, text, start=None, **kwargs):
        if start is not None:
            return case.session_token
        return original(self, text, start, **kwargs)

    monkeypatch.setattr(order._CardRedaction, "display", broken_renderer)
    code, error = business_cli.run()
    assert code == 1 and "展示字段与凭据冲突" in error
    assert not business_cli.summaries and platform.create_count == 0
    failure, = case.private.glob("create-preflight-*.json")
    assert case.session_token not in error + failure.read_text() + "".join(business_cli.notices)


def test_card_explicit_values_match_selected_options(case, platform, business_cli):
    code, error = business_cli.run()
    assert (code, error, platform.create_count) == (0, "", 1)
    card, = business_cli.summaries
    for name in ("benefit_type", "policy_type", "maintain_mutual_exclusion_group", "cancel_on_downgrade",
                 "trigger_once", "repeat_bonus"):
        control = case.control[name]
        spec = case.config["fields"][control]
        value = case.values[name]
        assert json.loads(platform.created_cells[control]) == spec["draft_values"][order.compact(value)]
        assert f"{spec['label']}：{value}" in card
    assert "互斥组按默认" not in card and "生效倍数按默认" not in card
    assert json.loads(platform.created_cells[case.control["user_tags"]]) == [{"sid": case.ids["tag"]}]
    assert case.values["user_tags"][0] in card


@pytest.mark.parametrize("source", ["session", "template", "fragment"])
def test_sensitive_write_value_stops_before_approval(case, platform, business_cli, source):
    if source == "template":
        platform.template[case.control["approval_url"]] += "/" + case.official_token
    else:
        value = case.session_token if source == "session" else case.session_token[4:13]
        case.values["user_scope"] = "合成范围" + value
    code, error = business_cli.run()
    assert code == 1 and "凭据" in error
    assert not business_cli.summaries and platform.create_count == 0


@pytest.mark.parametrize("source", ["display", "template", "fragment"])
def test_credential_stop_announces_manual_handoff(case, platform, business_cli, source):
    if source == "display":
        case.config["fields"][case.control["user_scope"]]["label"] += case.session_token
    elif source == "template":
        platform.template[case.control["approval_url"]] += "/" + case.official_token
    else:
        case.values["user_scope"] += case.session_token[4:13]
    code, error = business_cli.run()
    assert code == 1 and "凭据" in error
    assert not business_cli.summaries and platform.create_count == 0
    assert business_cli.notices == ["未建单（凭据冲突），转人工处理"]


def test_unknown_create_reentry_with_credential_conflict(case, platform, business_cli, capsys):
    platform.create_effect = "timeout"
    code, error = business_cli.run()
    assert code == 1 and "需人工对账" in error
    assert events(case)[-2:] == ["create_unknown", "create_reconciled_read"]
    assert platform.actions[-4:] == ["card", "search", "create", "search"]

    settings = case.private / "synthetic-settings"
    with settings.open("a") as stream:
        stream.write(f"PLACEHOLDER_EXTRA={case.values['policy_name']}\n")
    before = events(case)
    actions = platform.actions.copy()
    code = order.main(["--config", str(case.private / "config.json"), "--registration", case.ids["registration"],
                       "--env-file", str(settings), str(case.private / "draft.json")])

    assert code == 1 and "登记编号已使用" in capsys.readouterr().err
    assert events(case) == before
    assert platform.actions == actions
    assert platform.create_count == 1
    assert all("未建单" not in message for message in business_cli.notices)


def test_fresh_registration_with_credential_conflict(case, platform, business_cli):
    case.config["fields"][case.control["user_scope"]]["label"] += case.session_token
    code, error = business_cli.run()
    assert code == 1 and "展示字段与凭据冲突" in error
    assert not business_cli.summaries and platform.create_count == 0
    assert business_cli.notices == ["未建单（凭据冲突），转人工处理"]


def test_unmapped_inactive_control_stops_before_approval(case, platform, business_cli):
    control = str(uuid4())
    case.config["fields"][control] = {"type": 6, "label": "可看天数"}
    platform.template[control] = "9"
    code, error = business_cli.run()
    assert code == 1 and "watch_days" in error
    assert not business_cli.summaries and platform.create_count == 0


def test_midnight_during_login_read_stops_transmission(case, platform, business_cli, monkeypatch):
    today = case.window.start.date()
    case.values["start_date"] = today.isoformat()
    midnight = datetime.combine(today + timedelta(days=1), datetime.min.time(), UTC)
    case.evidence["window"]["end"] = (midnight + timedelta(days=1)).isoformat()
    read = order.private_bytes

    def read_across_midnight(path):
        data = read(path)
        if path.name == "mingdao-state.json" and business_cli.summaries:
            business_cli.now[0] = midnight.timestamp()
        return data

    monkeypatch.setattr(order, "private_bytes", read_across_midnight)
    code, _ = business_cli.run()
    assert code == 1 and platform.create_count == 0
    assert len(business_cli.summaries) == 1
    assert any("未建单" in msg and "开始日期" in msg for msg in business_cli.notices)


@pytest.mark.parametrize("closure", ["disconnect", "expiry"])
def test_approval_lost_during_date_check_stops_transmission(
        case, platform, business_cli, live_approval, monkeypatch, closure):
    service, now, socket, notices = live_approval
    monkeypatch.setattr(order, "FeishuService", lambda *args, **kwargs: service)
    monkeypatch.setattr(service, "run_create", lambda work: work(Event()))
    effective_start_date = order.effective_start_date

    def date_check(start, today):
        if "card" in platform.actions:
            if closure == "disconnect":
                socket.close_code = 1000
            else:
                now[0] = case.window.end.timestamp()
        return effective_start_date(start, today)

    monkeypatch.setattr(order, "effective_start_date", date_check)
    code, _ = business_cli.run()
    assert platform.create_count == 0
    assert code == 1 and platform.actions.count("card") == 1
    assert events(case)[-3:] == ["create_attempt", "aborted_before_write", "approval_closed"]
    assert any("未建单" in notice for notice in notices)


@pytest.mark.parametrize("multiple", [True, False])
def test_targeted_original_name_requires_matching_unique_tag(case, platform, business_cli, multiple):
    if multiple:
        control = case.control["user_tags"]
        key, _ = case.config["official"]["tag_relation"]["name_keys"]
        tag = "第二普通标签"
        row = {**record_identity(str(uuid4())), key: tag}
        platform.relations[control].append(row)
        platform.template[control] = 2
        case.values["user_tags"].append(tag)
        case.config["order_snapshot"]["tags"].append(
            {"name": tag, "id": row["rowId"], "end": case.values["end_date"], "shadow": False})
    else:
        case.values["policy_name"] = "【测试】不同名"
    code, error = business_cli.run()
    assert code == 1 and "唯一标签" in error
    assert not business_cli.summaries and platform.create_count == 0


def test_selected_tag_requires_its_own_quote(case, platform, business_cli):
    export = private_export(case.values)
    draft = export["drafts"][0]
    results = draft["field_results"]
    quote = results["user_scope"]["evidence"]
    for key in ("user_tags", "policy_type"):
        results[key]["evidence"] = quote
        draft[key]["evidence"] = draft["user_scope"]["evidence"]
    results["user_tags"]["statement"]["evidence"] = quote
    code, error = business_cli.run(export)
    assert code == 1 and "标签引文" in error
    assert not business_cli.summaries and platform.create_count == 0


def test_original_company_requires_confirmed_country(case, platform, business_cli):
    export = private_export(case.values)
    country = export["drafts"][0]["field_results"].setdefault("country", {"statement": {}})
    country.update(has_value=False, needs_confirmation=True)
    country["statement"]["status"] = "歧义"
    code, error = business_cli.run(export)
    assert code == 1 and "公司主体查表输入" in error
    assert not business_cli.summaries and platform.create_count == 0


def test_published_days_and_package_names_match_request(case, platform, business_cli):
    code, error = business_cli.run()
    assert (code, error, platform.create_count) == (0, "", 1)
    card, = business_cli.summaries
    cells = platform.created_cells
    days = [cells[case.control[key]] for key in ("recharge_days", "gift_days")]
    assert "充值 / 赠送天数：" + " / ".join(days) in card.splitlines()
    packages = case.control["packages"]
    names = {entry["sid"]: entry["name"] for row in platform.relations[packages]
             for key in ("current", "gift") for entry in row[case.child[key]]}
    pairs = []
    for row in json.loads(cells[packages]):
        children = {cell["controlId"]: json.loads(cell["value"]) for cell in row}
        pairs.append(" / ".join(names[children[case.child[key]][0]["sid"]] for key in ("current", "gift")))
    assert "节目包（当前 / 赠送）：" + "；".join(pairs) in card.splitlines()


def test_only_evidence_intersects_secret_and_stays_masked(case, platform, business_cli):
    export = private_export(case.values)
    block = export["sources"][0]
    offset = 4
    fragment = case.session_token[offset:13]
    start = len(block["text"]) + offset
    block["text"] += case.session_token
    draft = export["drafts"][0]
    result = draft["field_results"]["user_scope"]
    quote = {"text": fragment, "span": {"source_id": "body", "start": start, "end": start + len(fragment)}}
    result["evidence"] = result["statement"]["evidence"] = [quote]
    draft["user_scope"]["evidence"] = [{"text": fragment, "start": start}]
    code, error = business_cli.run(export)
    assert (code, error, platform.create_count) == (0, "", 1)
    card, = business_cli.summaries
    assert "用户范围依据：[已脱敏]" in card
    assert fragment not in card + "".join(business_cli.notices)
    assert platform.created_cells[case.control["user_scope"]] == case.values["user_scope"]


def replace_order_quotes(export, field, text, parts):
    block = export["sources"][0]
    start = len(block["text"])
    block["text"] += text + "\n"
    quotes = [{"text": text[left:right], "span": {"source_id": "body", "start": start + left,
                                                  "end": start + right}} for left, right in parts]
    draft = export["drafts"][0]
    result = draft["field_results"][field]
    result["statement"]["evidence"] = result["evidence"] = quotes
    draft[field]["evidence"] = [{"text": q["text"], "start": q["span"]["start"]} for q in quotes]


def offer_export(case, platform, kind, option_type=11):
    values = case.values
    values.update(benefit_type=kind, after_offer="恢复原包", forbid_package_change="是")
    if kind == "特价优惠":
        del values["recharge_days"], values["gift_days"]
        values.update(watch_days=3, amount_mode="自定义", recharge_amount=8, first_recharge_day_only="是")
    benefit = case.control["benefit_type"]
    key = platform.template[benefit][0]["key"]
    platform.template[benefit][0]["value"] = kind
    case.config["fields"][benefit]["draft_values"] = {order.compact(kind): [key]}
    for name in values.keys() & order.OFFER_FIELD_LABELS.keys():
        value = values[name]
        control = case.control[name] = str(uuid4())
        spec = {"draft": name, "label": order.OFFER_FIELD_LABELS[name], "type": 6}
        if isinstance(value, str):
            key = str(uuid4())
            spec.update(type=option_type, draft_values={order.compact(value): [key]})
            value = [{"key": key, "value": value}]
        case.config["fields"][control] = spec
        platform.template[control] = value
    snapshot = case.config["order_snapshot"]
    base_id = str(uuid4())
    header, current, gift = snapshot["package_levels"].splitlines()
    base = current.replace("合成当前包", "合成受赠包").replace(case.ids["package"], base_id)
    gift = gift.replace("合成当前包", "合成受赠包").replace(case.ids["package"], base_id)
    snapshot["package_levels"] = "\n".join((header, current, base, gift))
    gift_name = "合成受赠包_copy"
    values["packages"][0]["gift_package"] = gift_name
    platform.relations[case.control["packages"]][0][case.child["gift"]][0]["name"] = gift_name
    export = private_export(values)
    text = "合成当前包可获合成受赠包"
    replace_order_quotes(export, "packages", text, [(0, len(text))])
    return export


@pytest.mark.parametrize("name", ["after_offer", "forbid_package_change", "amount_mode", "first_recharge_day_only"])
@pytest.mark.parametrize("change", ["label", "shared_key", "wrong_type", "payload"])
def test_extended_single_option_requires_template_label_key_and_type(
        case, platform, business_cli, monkeypatch, name, change):
    export = offer_export(case, platform, "特价优惠")
    control = case.control[name]
    spec = case.config["fields"][control]
    if change == "label":
        platform.template[control][0]["value"] = "指定节目包"
    elif change == "shared_key":
        spec["draft_values"][order.compact("其他")] = [platform.template[control][0]["key"], str(uuid4())]
    elif change == "wrong_type":
        spec["type"] = 2
        del spec["draft_values"]
        platform.template[control] = case.values[name]
    else:
        payload = order.CreateRequest.func.payload

        def corrupt(self, controls):
            cells = deepcopy(controls)
            next(c for c in cells if c["controlId"] == control)["value"] = order.compact([str(uuid4())])
            return payload(self, cells)

        monkeypatch.setattr(order.CreateRequest.func, "payload", corrupt)
    code, error = business_cli.run(export)
    assert code == 1 and spec["label"] in error
    assert business_cli.summaries == [] and platform.create_count == 0


@pytest.mark.parametrize("option_type", [9, 11])
def test_extended_single_option_card_matches_created_key(case, platform, business_cli, option_type):
    export = offer_export(case, platform, "特价优惠", option_type)
    code, error = business_cli.run(export)
    assert (code, error, platform.create_count) == (0, "", 1)
    card, = business_cli.summaries
    for name in ("after_offer", "forbid_package_change", "amount_mode", "first_recharge_day_only"):
        control = case.control[name]
        option, = platform.template[control]
        assert json.loads(platform.created_cells[control]) == [option["key"]]
        assert f"{order.OFFER_FIELD_LABELS[name]}：{option['value']}" in card.splitlines()


@pytest.mark.parametrize("size", [1, 2, 3, 4, 9, "split"])
def test_order_evidence_masks_registered_fragment_without_full_secret(case, platform, business_cli, size):
    export = private_export(case.values)
    text = case.session_token[1:]
    if size == "split":
        text = text[:2] + "隔开" + text[2:4]
        parts = [(0, 2), (4, 6)]
    else:
        parts = [(3, 3 + size)]
    replace_order_quotes(export, "user_scope", text, parts)
    code, error = business_cli.run(export)
    assert (code, error, platform.create_count) == (0, "", 1)
    card, = business_cli.summaries
    assert [line for line in card.splitlines() if line.startswith("用户范围依据：")] == ["用户范围依据：[已脱敏]"] * len(parts)
    if isinstance(size, int) and size >= 4:
        assert text[3:3 + size] not in card + error + "".join(business_cli.notices)
    assert platform.created_cells[case.control["user_scope"]] == case.values["user_scope"]


@pytest.mark.parametrize("source", ["查表派生", "原文", "升级权益"])
def test_titleless_package_card_matches_approved_request(case, platform, business_cli, source):
    export = offer_export(case, platform, source) if source == "升级权益" else private_export(case.values)
    draft = export["drafts"][0]
    draft["scope"]["has_title"] = False
    result = draft["field_results"]["packages"]
    if source == "原文":
        result.update(source=source, input_fields=[])
        result["statement"]["value"] = result["value"]
    code, error = business_cli.run(export)
    assert (code, error, platform.create_count) == (0, "", 1)
    card, = business_cli.summaries
    assert "节目包依据：" + result["statement"]["evidence"][0]["text"] in card.splitlines()
    control = case.control["packages"]
    names = {entry["sid"]: entry["name"] for row in platform.relations[control]
             for role in ("current", "gift") for entry in row[case.child[role]]}
    row, = json.loads(platform.created_cells[control])
    pair = {cell["controlId"]: json.loads(cell["value"]) for cell in row if cell["controlId"] in
            (case.child["current"], case.child["gift"])}
    assert "节目包（当前 / 赠送）：" + " / ".join(names[pair[case.child[k]][0]["sid"]]
                                              for k in ("current", "gift")) in card.splitlines()


@pytest.mark.parametrize("mode", ["original", "derived", "dependency", "missing", "coordinates", "exclusive"])
def test_order_packages_require_names_in_own_verified_quotes(case, platform, business_cli, mode):
    export = offer_export(case, platform, "升级权益") if mode == "dependency" else private_export(case.values)
    draft = export["drafts"][0]
    result = draft["field_results"]["packages"]
    text = "合成当前包" if mode == "dependency" else "本次活动"
    replace_order_quotes(export, "packages", text, [(0, len(text))])
    reason = "节目包缺少原文归属依据"
    if mode == "original":
        draft["scope"]["has_title"] = False
        result.update(source="原文", input_fields=[])
        result["statement"]["value"] = result["value"]
    elif mode == "dependency":
        replace_order_quotes(export, "benefit_type", "合成受赠包", [(0, 5)])
        result["evidence"] = result["evidence"] + draft["field_results"]["benefit_type"]["evidence"]
        draft["packages"]["evidence"] += draft["benefit_type"]["evidence"]
    elif mode == "missing":
        result["evidence"] = []
        reason = "可核证据"
    elif mode == "coordinates":
        result["evidence"][0]["span"]["end"] -= 1
        reason = "可核证据"
    elif mode == "exclusive":
        other_policy(export, result["evidence"][0]["span"], exclusive=True)
        reason = "归属声明矛盾"
    code, error = business_cli.run(export)
    assert code == 1 and reason in error
    assert not business_cli.summaries and platform.create_count == 0


@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize("has_title", [True, False])
def test_package_proof_survives_card_redaction(case, platform, business_cli, monkeypatch, hidden, has_title):
    export = private_export(case.values)
    draft = export["drafts"][0]
    draft["scope"]["has_title"] = has_title
    fragment = case.session_token[4:13]
    if hidden:
        read_settings = order.dotenv_values
        monkeypatch.setattr(order, "dotenv_values", lambda **kw: {**read_settings(**kw), "EXTRA": "密文合成当前包后缀"})
    text = "本政策适用于合成当前包，联系编号 " + fragment
    replace_order_quotes(export, "packages", text, [(0, len(text))])
    code, error = business_cli.run(export)
    if hidden:
        assert code == 1 and "节目包原文归属依据已脱敏" in error
        assert not business_cli.summaries and platform.create_count == 0
    else:
        assert (code, error, platform.create_count) == (0, "", 1)
        card, = business_cli.summaries
        assert "节目包依据：本政策适用于合成当前包，联系编号 [已脱敏]" in card.splitlines()
        assert fragment not in card + "".join(business_cli.notices)


@pytest.mark.parametrize("key", ["id", "label"])
def test_contact_credential_collision_announces_fixed_handoff(case, platform, business_cli, monkeypatch, key):
    secret = "synthetic<&>credential" if key == "id" else "synthetic-contact-credential"
    read_settings = order.dotenv_values
    monkeypatch.setattr(order, "dotenv_values", lambda **kw: {**read_settings(**kw), "EXTRA": secret})
    case.config["configurator"][key] = secret
    code, error = business_cli.run()
    assert code == 1 and "凭据冲突" in error
    assert not business_cli.summaries and platform.create_count == 0
    assert business_cli.notices == ["未建单（凭据冲突），转人工处理"]
    assert secret not in error + "".join(business_cli.notices)


def test_same_tag_id_conflicting_deadline_stops_before_card(case, platform, business_cli):
    tags = case.config["order_snapshot"]["tags"]
    tags.append({**tags[0], "name": "合成另一标签", "end": case.values["start_date"]})
    code, error = business_cli.run()
    assert code == 1 and "标签截止日登记冲突" in error
    assert not business_cli.summaries and platform.create_count == 0


@pytest.mark.parametrize("duplicate", [False, True])
def test_order_package_rows_require_unique_current_offer(case, platform, business_cli, duplicate):
    offer_export(case, platform, "升级权益")
    control = case.control["packages"]
    row = deepcopy(platform.relations[control][0])
    row.update(record_identity(str(uuid4())))
    snapshot = case.config["order_snapshot"]
    _, current, base, gift = snapshot["package_levels"].splitlines()
    base_id, gift_id = str(uuid4()), str(uuid4())
    old_base = base.split(",")[3]
    base = base.replace("合成受赠包", "合成另一受赠包").replace(old_base, base_id)
    gift = gift.replace("合成受赠包", "合成另一受赠包").replace(old_base, base_id).replace(case.ids["gift_package"], gift_id)
    snapshot["package_levels"] += "\n" + base + "\n" + gift
    current_name = "合成当前包"
    if not duplicate:
        current_id = str(uuid4())
        current_name = "合成另一当前包"
        snapshot["package_levels"] += "\n" + current.replace("合成当前包", current_name).replace(case.ids["package"], current_id)
        row[case.child["current"]] = [{"sid": current_id, "name": current_name}]
    gift_name = "合成另一受赠包_copy"
    row[case.child["gift"]] = [{"sid": gift_id, "name": gift_name}]
    platform.relations[control].append(row)
    platform.template[control] = 2
    case.values["packages"].append({"current_package": current_name, "gift_package": gift_name})
    export = private_export(case.values)
    text = "合成当前包可获合成受赠包或合成另一受赠包"
    replace_order_quotes(export, "packages", text, [(0, len(text))])
    code, error = business_cli.run(export)
    reason = "当前销售品重复" if duplicate else "节目包缺少原文归属依据"
    assert code == 1 and reason in error
    assert not business_cli.summaries and platform.create_count == 0


@pytest.mark.parametrize("name", ["recharge_amount", "watch_days", "approval_url"])
def test_scalar_mapping_cannot_change_approved_value(case, platform, business_cli, name):
    if name == "approval_url":
        control = case.control[name]
        value = platform.template[control].replace("placeholder", "sample")
        case.values[name] = value
        replacement = value + "/other"
        export = private_export(case.values)
    else:
        export = offer_export(case, platform, "特价优惠")
        control = case.control[name]
        value = case.values[name]
        replacement = 9
    spec = case.config["fields"][control]
    spec["draft_values"] = {order.compact(value): replacement}
    platform.template[control] = replacement
    code, error = business_cli.run(export)
    assert code == 1 and f"字段 {spec['label']} 的写入值不一致" in error
    assert business_cli.summaries == [] and platform.create_count == 0


@pytest.mark.parametrize("hidden,allowed", [("本政策唯一适用的当前包为", False), ("末尾标记", True), ("五字整一半", True)])
def test_package_quote_retains_at_least_half(case, platform, business_cli, monkeypatch, hidden, allowed):
    export = private_export(case.values)
    export["drafts"][0]["scope"]["has_title"] = False
    read_settings = order.dotenv_values
    monkeypatch.setattr(order, "dotenv_values", lambda **kw: {**read_settings(**kw), "EXTRA": hidden})
    name = case.values["packages"][0]["current_package"]
    text = hidden + name if not allowed else name + hidden
    replace_order_quotes(export, "packages", text, [(0, len(text))])
    code, error = business_cli.run(export)
    if allowed:
        assert (code, error, platform.create_count) == (0, "", 1)
        assert "节目包依据：" + name + "[已脱敏]" in business_cli.summaries[0].splitlines()
    else:
        assert code == 1 and "节目包原文归属依据已脱敏" in error
        assert not business_cli.summaries and platform.create_count == 0


def test_trimmed_tenant_key_cannot_appear_in_contact(case, platform, business_cli, monkeypatch):
    secret = "synthetic-tenant-check"
    read_settings = order.dotenv_values
    monkeypatch.setattr(order, "dotenv_values",
                        lambda **kw: {**read_settings(**kw), "FEISHU_TENANT_KEY": " " + secret + " "})
    case.config["configurator"]["label"] = secret
    code, error = business_cli.run()
    assert code == 1 and "凭据冲突" in error and secret not in error
    assert business_cli.notices == ["未建单（凭据冲突），转人工处理"]
    assert not business_cli.summaries and platform.create_count == 0


@pytest.mark.parametrize("declared", ["充一送一", "赠送时长"])
def test_benefit_declaration_uses_trusted_option(case, platform, business_cli, declared):
    export = private_export(case.values)
    draft = export["drafts"][0]
    draft["scope"]["has_title"] = False
    draft["field_results"]["benefit_type"]["statement"]["value"] = declared
    replace_order_quotes(export, "benefit_type", declared, [(0, len(declared))])
    code, error = business_cli.run(export)
    assert (code, error, platform.create_count) == (0, "", 1)
    control = case.control["benefit_type"]
    assert "优惠类型：赠送时长" in business_cli.summaries[0].splitlines()
    assert json.loads(platform.created_cells[control]) == [platform.template[control][0]["key"]]
