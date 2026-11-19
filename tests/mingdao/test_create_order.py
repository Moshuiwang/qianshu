import asyncio
import json
import logging
from copy import deepcopy
from datetime import UTC, datetime, timedelta
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
        "worksheetId", "appId", "viewId", "projectId", "tag_sheet", "registration",
        "additional_view", "tag_view", "tag_name", "status", "official_app",
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
    field("member", 26, "成员")
    field("generated", 2, "回填字段")

    template = {
        "policy_name": "合成模板", "start_date": "2026-11-21", "end_date": "2026-12-01",
        "recharge_days": "1", "gift_days": "2", "user_tags": [],
        "packages": [{
            child["current"]: [{"rowid": ids["package"], "name": "合成当前包"}],
            child["gift"]: [{"rowid": ids["gift_package"], "name": "合成赠送包"}],
            child["price"]: "3.00",
        }],
        "approval_url": "https://placeholder.invalid/approval",
        "company": "合成公司", "maintain_mutual_exclusion_group": order.compact([ids["option"]]),
        "country": "合成国家", "member": [{"accountId": ids["member"], "name": "合成人员"}],
        "generated": "合成旧值",
    }
    values = {"policy_name": "【测试】合成单", "start_date": "2026-12-01", "end_date": "2026-12-31",
              "recharge_days": 30, "gift_days": 31, "company": "合成公司", "user_tags": ["合成标签"],
              "maintain_mutual_exclusion_group": "是",
              "packages": [{"current_package": "合成当前包", "gift_package": "合成赠送包"}]}
    endpoint = {"host": order._CREATE_HOST, "method": "POST", "path": order._CREATE_PATH}
    rows_path = f"/v3/app/worksheets/{ids['worksheetId']}/rows"

    config = {
        "configurator": {"id": str(uuid4()), "label": "合成负责人"},
        "fields": fields, "readback_skip": [control["generated"]],
        "template": {"row_id": ids["template"], "country": {"control": control["country"], "value": "合成国家"}},
        "create": {"endpoint": endpoint.copy(), "session_cookie": "placeholder_session", "response_id": ["id"],
                   "identifiers": {key: ids[key] for key in ("worksheetId", "appId", "viewId", "projectId")}},
        "official": {
            "origin": "https://placeholder.invalid", "token_key": "PLACEHOLDER_OFFICIAL_TOKEN",
            "app_id_key": "PLACEHOLDER_OFFICIAL_APP",
            "status_control": ids["status"], "record_metadata": [ids["status"]],
            "status_map": {"1": "新建", "2": "待审批", "3": "待配置", "4": "配置完成"},
            "read": {"path": rows_path + "/{record_id}", "missing_status": 404,
                     "missing_key": ["error_code"], "missing_value": 12345},
            "search": {"path": rows_path + "/list", "name_control": control["policy_name"],
                       "view_ids": [ids["viewId"]]},
            "tags": {"path": f"/v3/app/worksheets/{ids['tag_sheet']}/rows/list",
                     "name_control": ids["tag_name"], "view_ids": [ids["tag_view"]]},
            "logs": {"path": rows_path + "/{record_id}/logs"},
            "delete": {"path": rows_path + "/{record_id}", "method": "DELETE", "condition": "ifStatus"},
        },
    }
    now = datetime.now(UTC)
    window = Window(now - timedelta(minutes=1), now + timedelta(minutes=1))
    evidence = {key: f"占位回执 {index}" for index, key in enumerate(order._EVIDENCE)}
    evidence.update(is_test=True, create_endpoint=endpoint.copy(),
                    window={"start": window.start.isoformat(), "end": window.end.isoformat()},
                    two_reviews={"internal": "占位内审", "external": "占位外审"})
    private = tmp_path / ".private"
    private.mkdir(mode=0o700)
    return SimpleNamespace(
        ids=ids, control=control, child=child, values=values, config=config, evidence=evidence,
        cells={control[key]: value for key, value in template.items()}, private=private, window=window,
        session_token="placeholder-sensitive-" + uuid4().hex,
        official_token="placeholder-sensitive-" + uuid4().hex,
    )


def assemble(case):
    return order.assemble(case.values, case.cells, case.config, lambda _: case.ids["tag"], is_test=True)


def test_assemble_values_and_legacy_cells(case):
    controls, summary = assemble(case)
    fields = {row["controlId"]: row["value"] for row in controls}
    assert fields.keys() == case.cells.keys()
    assert all(isinstance(value, str) for value in fields.values())
    for key in ("policy_name", "start_date", "end_date", "recharge_days", "gift_days"):
        assert fields[case.control[key]] == str(case.values[key])
    assert json.loads(fields[case.control["user_tags"]]) == [{"sid": case.ids["tag"]}]
    assert json.loads(fields[case.control["maintain_mutual_exclusion_group"]]) == [case.ids["option"]]
    assert json.loads(fields[case.control["member"]]) == [{"accountId": case.ids["member"]}]
    assert json.loads(fields[case.control["packages"]]) == [[
        {"controlId": case.child["current"], "value": order.compact([{"sid": case.ids["package"]}])},
        {"controlId": case.child["gift"], "value": order.compact([{"sid": case.ids["gift_package"]}])},
        {"controlId": case.child["price"], "value": "3.00"},
    ]]
    for key in ("approval_url", "company", "country", "generated"):
        assert fields[case.control[key]] == case.cells[case.control[key]]
    assert [row["来源"] for row in summary].count("草稿") == 6
    assert all(row.keys() == {"字段名", "类型", "来源", "值指纹"}
               and len(row["值指纹"]) == 12 for row in summary)
    assert order.encode_cell([case.ids["option"]], {"type": 9}) == order.compact([case.ids["option"]])


@pytest.mark.parametrize(("failure", "reason"), [
    ("conflict", "互斥组"), ("package", "节目包与模板不同"), ("missing", "模板字段不全"),
    ("type", "未配置类型"), ("formal", "正式单不得照抄任务审批链接"),
])
def test_assembly_stops(case, failure, reason):
    if failure == "conflict":
        case.values["maintain_mutual_exclusion_group"] = "否"
    if failure == "package":
        case.values["packages"][0]["gift_package"] = "合成替换包"
    if failure == "missing":
        del case.cells[case.control["country"]]
    if failure == "type":
        del case.config["fields"][case.control["generated"]]
        case.config["readback_skip"] = []
    with pytest.raises(order.Stop, match=reason):
        order.assemble(case.values, case.cells, case.config, lambda _: pytest.fail("提前查标签"),
                       is_test=failure != "formal")


class Platform:
    def __init__(self, case):
        self.case = case
        self.tag_count = 1
        self.same_name = False
        self.rows = {}
        self.actions = []
        self.states = [1, 2, 1]
        self.log_effect = lambda logs: {"logs": logs}
        self.logs_success = True
        self.delete_effect = "missing"
        self.create_effect = "ok"
        self.create_count = 0
        self.mismatch = False
        self.before_search = lambda: None
        self.before_read = lambda row: None
        self.before_handoff = lambda: None
        self.query_pages = {}
        self.queries = []
        self.timeouts = []
        self.template = {**case.cells, "_id": case.ids["template"], "_createdBy": {}, "_owner": {}}

    def record(self, record_id, status, cells):
        return {
            **cells, "_id": record_id,
            self.case.control["policy_name"]: cells.get(self.case.control["policy_name"],
                                                       self.case.values["policy_name"]),
            self.case.ids["status"]: status,
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
                    "id": self.case.ids["status"], "name": "政策状态",
                    "type": 11, "editType": 1, "oldValue": "", "oldText": "",
                    "newValue": str(state), "newText": self.case.config["official"]["status_map"][str(state)],
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
        assert not request.url.params
        self.timeouts.append(request.extensions["timeout"])
        path = request.url.path
        if path in (spec["search"]["path"], spec["tags"]["path"]):
            action = "search" if path == spec["search"]["path"] else "tags"
            self.actions.append("search" if action == "search" else "tag")
            query = json.loads(request.content)
            view, page = query["viewId"], query["pageIndex"]
            control = spec[action]["name_control"]
            name = case.values["policy_name"] if action == "search" else case.values["user_tags"][0]
            assert request.method == "POST"
            assert view in spec[action]["view_ids"] and page > 0
            assert query == {"viewId": view, "pageSize": spec.get("page_size", 50), "pageIndex": page,
                             "filter": {"type": "group", "logic": "AND", "children": [
                                 {"type": "condition", "field": control, "operator": "eq", "value": name},
                             ]}}
            self.queries.append((action, view, page))
            if action == "search":
                self.before_search()
                rows = list(self.rows.values())
                if self.same_name:
                    rows.append(self.record(str(uuid4()), 1, {}))
            else:
                rows = [{"_id": case.ids["tag"], control: name}] * self.tag_count
            page_rows = self.query_pages.get((view, page), rows)
            if page_rows is None:
                return httpx.Response(503, json={"success": False})
            return self.response({"rows": page_rows})
        record_id = case.ids["created"]
        if path == spec["logs"]["path"].replace("{record_id}", record_id):
            self.actions.append("history")
            assert request.method == "GET"
            return self.response(self.log_effect(self.logs()), success=self.logs_success)
        if path == spec["read"]["path"].replace("{record_id}", case.ids["template"]):
            self.actions.append("template")
            assert request.method == "GET"
            return self.response(self.template)
        assert path == spec["read"]["path"].replace("{record_id}", record_id)
        if request.method == "GET":
            self.actions.append("read")
            if record_id not in self.rows:
                code = 99999 if self.delete_effect == "wrong_missing" else 12345
                return httpx.Response(404, json={"success": False, "error_code": code})
            self.before_read(self.rows[record_id])
            return self.response(self.rows[record_id])
        self.actions.append("delete")
        assert request.method == "DELETE"
        body = {"triggerWorkflow": False, "permanent": False}
        if spec["delete"]["condition"] is not None:
            body[spec["delete"]["condition"]] = self.states[-1]
        assert json.loads(request.content) == body
        if self.delete_effect == "interrupted":
            raise KeyboardInterrupt
        if self.delete_effect != "still_exists":
            del self.rows[record_id]
        return self.response(None)

    @staticmethod
    def response(data, *, success=True):
        return httpx.Response(200, json={"success": success, "error_code": 0, "data": data})

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
                assert platform.actions[-1] in ("template", "tag", "card")

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
                    body = top + b"\r\n" + body + b"\r\n" + endings[effect]
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


def test_tag_search_requires_one_record(case, platform):
    with httpx.Client(transport=httpx.MockTransport(platform.official)) as client:
        api = order.Official(case.config["official"], case.official_token, case.ids["official_app"], client)
        assert api.tag(case.values["user_tags"][0]) == case.ids["tag"]
        for count in (0, 2):
            platform.tag_count = count
            with pytest.raises(order.Stop, match="唯一"):
                api.tag(case.values["user_tags"][0])


@pytest.mark.parametrize("mode", ["zero_matches", "duplicate", "later_page_failed", "page_limit"])
def test_duplicate_check_reads_every_page_of_every_view(case, platform, mode):
    official = case.config["official"]
    view, extra = (case.ids[key] for key in ("viewId", "additional_view"))
    official["search"]["view_ids"].append(extra)
    official["page_size"] = 2
    control = official["search"]["name_control"]
    name = case.values["policy_name"]
    near_match = {"_id": str(uuid4()), control: name + "（占位后缀）"}
    exact_match = {"_id": str(uuid4()), control: name}
    platform.query_pages = {
        (view, 1): [near_match, near_match],
        (view, 2): [],
        (extra, 1): [near_match, exact_match if mode == "duplicate" else near_match],
        (extra, 2): None if mode == "later_page_failed" else [],
    }
    if mode == "page_limit":
        official["page_limit"] = 2
        platform.query_pages[(extra, 2)] = [near_match, near_match]
    assert run_case(case, platform, Approvals(platform)) == (mode == "zero_matches")
    assert platform.queries == [("search", view, 1), ("search", view, 2),
                                ("search", extra, 1), ("search", extra, 2)]
    assert platform.create_count == int(mode == "zero_matches")
    assert ("aborted_before_write" in events(case)) == (mode != "zero_matches")


@pytest.mark.parametrize("mode", ["later_exact", "duplicate", "later_page_failed"])
def test_tag_query_requires_unique_exact_name_across_pages(case, platform, mode):
    config = case.config["official"]
    config["page_size"] = 2
    view, control = config["tags"]["view_ids"][0], config["tags"]["name_control"]
    name = case.values["user_tags"][0]
    near_match = {"_id": str(uuid4()), control: name + "（占位后缀）"}
    exact = {"_id": case.ids["tag"], control: name}
    platform.query_pages = {
        (view, 1): [near_match, exact if mode == "duplicate" else near_match],
        (view, 2): None if mode == "later_page_failed" else [exact],
    }
    with httpx.Client(transport=httpx.MockTransport(platform.official)) as client:
        api = order.Official(config, case.official_token, case.ids["official_app"], client)
        if mode == "later_exact":
            assert api.tag(name) == case.ids["tag"]
        else:
            with pytest.raises(order.Stop):
                api.tag(name)
    assert platform.queries == [("tags", view, 1), ("tags", view, 2)]


def test_search_requires_configured_view(case):
    config = case.config["official"]
    config["search"]["view_ids"] = []
    with pytest.raises(order.Stop, match="查询配置"):
        order.Official(config, case.official_token, case.ids["official_app"], None)


def test_search_stops_at_default_page_limit(case, platform):
    config = case.config["official"]
    config["page_size"] = 1
    view = config["search"]["view_ids"][0]
    filler = {"_id": str(uuid4()), config["search"]["name_control"]: "占位近似名称"}
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
                clock=clock, pause=pause or (lambda _: None), monotonic=monotonic or (lambda: 0),
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
                                     "chunk_missing_end", "chunk_short_trailer"])
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
    ("no_creation", False, False),
    ("two_creations", False, False),
    ("configured_then_new", False, False),
    ("blank_status", False, False),
    ("unknown_status", False, False),
    ("deleted_status", False, False),
    ("current_pending", False, False),
    ("49_logs", True, True),
    ("50_logs", False, False),
    ("lower_log_limit", False, False),
    ("empty", False, False),
    ("undetermined_completeness", False, False),
    ("missing_notice", False, False),
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
        if condition == "no_creation":
            content["type"] = 2
        if condition == "two_creations":
            logs[-1]["content"]["type"] = 1
        if condition == "blank_status":
            change["newText"] = ""
        if condition == "unknown_status":
            change["newText"] = "占位未知状态"
        if condition == "deleted_status":
            change["isDeleted"] = True
        if condition == "empty":
            logs = []
        if condition == "undetermined_completeness":
            return {"logs": logs, "more": True}
        return {"logs": list(reversed(logs))}

    def modify_after_history(row):
        if platform.actions[-2] == "history":
            row["_updatedAt"] = "2026-11-20 08:00:02"

    platform.log_effect = edit_logs
    platform.logs_success = condition != "logs_failed"
    if condition == "configured_then_new":
        platform.states = [1, 2, 3, 1]
    if condition == "current_pending":
        platform.states = [1, 2]
    if condition == "new_only":
        platform.states = [1]
    if condition in ("49_logs", "50_logs"):
        platform.states = [1] * int(condition[:2])
    if condition == "lower_log_limit":
        case.config["official"]["logs"]["page_limit"] = 3
    if condition == "default_delete_body":
        case.config["official"]["delete"]["condition"] = None
    if condition == "missing_notice":
        del case.evidence["configurator_notice"]
    if condition == "changed_before_delete":
        platform.before_read = modify_after_history
    if deleted and not verified:
        platform.delete_effect = condition
    if verified:
        assert run_case(case, platform, approvals)
    else:
        with pytest.raises((order.Stop, KeyboardInterrupt)):
            run_case(case, platform, approvals)
        assert "delete_unconfirmed" in events(case)
        assert any("需人工核对" in notice and "<at " in notice for notice in approvals.notices)
    assert ("delete" in platform.actions) == deleted
    assert ("delete_verified" in events(case)) == verified
    assert any("删除已核验" in notice for notice in approvals.notices) == verified
    if deleted:
        idx = platform.actions.index("delete")
        assert platform.actions[idx - 2:idx] == ["history", "read"]
    assert all("<at " in notice for notice in approvals.notices)


@pytest.mark.parametrize("mode", ["stable", "status_changes", "timestamp_changes", "never_stable", "late_read"])
def test_deletion_waits_for_stable_status_and_timestamp(case, platform, mode):
    now, pauses = [0], []

    def pause(seconds):
        pauses.append(seconds)
        now[0] += seconds

    def update(row):
        if mode == "status_changes" and now[0] == 0:
            row[case.ids["status"]] = 2
        if mode == "status_changes" and now[0] >= 5:
            row[case.ids["status"]] = 1
        if mode == "timestamp_changes" and now[0] >= 5:
            row["_updatedAt"] = "2026-11-20 08:00:02"
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
        assert platform.rows[case.ids["created"]][case.ids["status"]] == 1
        assert any("需人工核对" in notice and "<at " in notice for notice in approvals.notices)
    else:
        assert run()
        assert pauses == ([5] if mode == "stable" else [5, 5])
        assert platform.actions.count("read") == len(pauses) + 3


def test_official_record_interprets_naive_updated_at_as_utc_plus_eight(case, platform):
    api = order.Official(case.config["official"], case.official_token, case.ids["official_app"], None)
    row = platform.record(case.ids["created"], 1, {})
    assert api.record(row).updated_at == datetime(2026, 11, 20, 0, 0, 1, tzinfo=UTC)


@pytest.mark.parametrize("effect", ["ok", "chunk_ok"])
def test_complete_create_response_is_accepted(case, platform, effect):
    platform.create_effect = effect
    assert run_case(case, platform, Approvals(platform))
    assert platform.create_count == 1
    assert "created" in events(case)


def test_readback_report_counts_and_modes(case, platform):
    platform.mismatch = True
    approvals = Approvals(platform)
    assert not run_case(case, platform, approvals)
    assert events(case)[-1] == "delete_verified"
    path, = case.private.glob("create-readback-*.json")
    rows = json.loads(path.read_text())
    assert all(row.keys() == {"字段名", "结果"} for row in rows)
    assert len(rows) == len(case.cells)
    assert sorted(row["结果"] for row in rows if row["结果"] != "一致") == ["不一致", "缺失", "跳过"]
    assert any("一致 10 / 不一致 1 / 缺失 1 / 跳过 1" in text for text in approvals.notices)
    assert all(file.stat().st_mode & 0o777 == 0o600 for file in case.private.iterdir())


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
    "login_changed_after_approval",
])
def test_cli_with_private_registration(case, platform, mode, monkeypatch, capsys):
    private = case.private
    config_path = private / "config.json"
    draft_path = private / "draft.json"
    login_path = private / "mingdao-state.json"
    env_file = private / "env"
    order.exclusive(config_path, case.config)
    draft = deepcopy(case.values)
    if mode == "template_conflict":
        draft["maintain_mutual_exclusion_group"] = "否"
    order.exclusive(draft_path, {key: {"value": value} for key, value in draft.items()})
    order.exclusive(login_path, {"cookies": [{
        "name": case.config["create"]["session_cookie"], "value": case.session_token,
        "domain": order._CREATE_HOST, "path": "/", "secure": True, "expires": -1,
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

    success = mode == "success"
    assert order.main(["--config", str(config_path), "--registration", case.ids["registration"],
                       "--env-file", str(env_file), str(draft_path)]) == (0 if success else 1)
    captured = capsys.readouterr()
    assert ("D15 删除已核验" in captured.out) == success
    assert platform.create_count == int(mode in ("success", "delete_interrupted"))
    if mode == "login_changed_after_approval":
        assert "aborted_before_write" in events(case)
        assert any("建单登录状态已变化" in notice for notice in approvals.notices)
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
        fields = json.loads(summary_path.read_text())
        assert len(fields) == len(case.cells)
        assert "合成模板" not in summary_path.read_text()
        assert summary_path.stat().st_mode & 0o777 == 0o600
    for token in (case.session_token, case.official_token, case.ids["official_app"]):
        assert token not in captured.out + captured.err
