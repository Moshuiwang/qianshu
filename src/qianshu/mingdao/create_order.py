"""Trace #12 X5 单条测试单，审批后建单，经官方接口回读与清理。"""

import argparse
import fcntl
import hashlib
import http.client
import json
import logging
import os
import re
import ssl
import sys
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from stat import S_ISREG
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from dotenv import dotenv_values

from qianshu.draft import PolicyDraft
from qianshu.feishu.cards import mention
from qianshu.feishu.sdk import ApprovalClosed, FeishuService
from qianshu.feishu.settings import Window, load_settings

_PREFIX = "【测试】"
_NEW = "新建"
_PENDING = "待审批"
_PLATFORM_TZ = timezone(timedelta(hours=8))
_LIMIT = 8 * 1024 * 1024
_CREATE_HOST = "www.mingdao.com"
_CREATE_PATH = "/api/Worksheet/AddWorksheetRow"
_EVIDENCE = (
    "owner_approval", "readiness_confirmation", "approver_notice", "configurator_notice",
    "trace_decision", "comparison_report", "same_country_count_evidence", "owner_login_confirmation",
)
_VARIABLE = (
    "policy_name", "start_date", "end_date", "recharge_days", "gift_days", "user_tags", "packages",
)
_DRAFT_FIELDS = {field.name for field in fields(PolicyDraft)}
_SELECT = {9, 10, 11}
_ARRAY = _SELECT | {26, 29, 34}


class Stop(RuntimeError):
    """只承载固定原因与私有配置批准的字段名。"""


def require(condition, reason):
    if not condition:
        raise Stop(reason)


def utc(value, *, platform=False):
    require(isinstance(value, str) and len(value) > 10 and value[10] in ("T", " "),
            "时刻格式无效")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None and platform:
        parsed = parsed.replace(tzinfo=_PLATFORM_TZ)
    require(parsed.tzinfo is not None, "登记时刻缺少时区")
    return parsed.astimezone(UTC)


def uuid_record(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value.lower()
    except ValueError:
        return False


def at(value, keys):
    require(isinstance(keys, list), "响应取值路径无效")
    for key in keys:
        value = value[key]
    return value


def unique_object(pairs):
    result = {}
    for name, value in pairs:
        require(name not in result, "JSON 包含重复字段")
        result[name] = value
    return result


def bad_constant(_):
    raise Stop("JSON 含无效数值")


def decode(value):
    return json.loads(value, object_pairs_hook=unique_object, parse_constant=bad_constant)


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def private_bytes(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        mode = os.fstat(fd).st_mode
        require(S_ISREG(mode) and mode & 0o777 == 0o600, "私有文件须为 0600")
        data = stream.read(_LIMIT + 1)
        require(len(data) <= _LIMIT, "私有文件超出限制")
        return data


def sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def exclusive(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(compact(value) + "\n")
        stream.flush()
        os.fsync(fd)
    sync_directory(path.parent)


class Ledger:
    def __init__(self, stream, directory, registration, name):
        self.stream = stream
        self.directory = directory
        self.registration = registration
        self.name = name

    def append(self, event, record_id=None, **results):
        row = dict(registration=self.registration, event=event, name=self.name,
                   record_id=record_id, is_test=True, utc=datetime.now(UTC).isoformat(), **results)
        self.stream.write(compact(row) + "\n")
        self.stream.flush()
        os.fsync(self.stream.fileno())

    def records(self):
        self.stream.seek(0)
        return [decode(line) for line in self.stream]

    def check_available(self):
        rows = self.records()
        require(not any(row["registration"] == self.registration for row in rows), "登记编号已使用")
        # 共用 t11 的资格锚点与台账，兼容已证明从未放行的旧预留。
        occupied = {"create_unknown", "create_reconciled_candidate", "created", "delete_attempt", "delete_verified"}
        aborted = {row["registration"] for row in rows if row["event"] == "aborted_before_write"
                   and type(row.get("allowed_writes")) is int and row["allowed_writes"] == 0}
        require(not any(row["event"] in occupied or
                        (row["event"] == "create_attempt" and row["registration"] not in aborted)
                        for row in rows), "本 Trace 建单资格已占用，须对账")
        reserved = [row["registration"] for row in rows if row["event"] == "create_reserved"]
        require(set(reserved) <= aborted, "本 Trace 可能存在未清理单据，须对账")
        anchor = self.directory / "trace-12-create.lock"
        if reserved:
            require(decode(private_bytes(anchor)) == {"registration": reserved[0]}, "资格锁与台账不符")
        else:
            require(not os.path.lexists(anchor), "本 Trace 建单资格已占用，须对账")
        return anchor, not reserved

    def reserve(self):
        anchor, first = self.check_available()
        if first:
            exclusive(anchor, {"registration": self.registration})
        self.append("create_reserved")

    def registered(self, record_id):
        rows = [row for row in self.records() if row["registration"] == self.registration]
        created = [row for row in rows if row["event"] == "created"]
        require(sum(row["event"] == "create_attempt" for row in rows) == 1
                and uuid_record(record_id) and self.name.startswith(_PREFIX) and len(created) == 1
                and created[0]["is_test"] is True and created[0]["name"] == self.name
                and created[0]["record_id"] == record_id
                and not any(row["event"] in {"create_unknown", "delete_attempt", "delete_verified"}
                            for row in rows), "建单台账不符或已尝试清理")


@contextmanager
def ledger_open(directory, registration, name):
    fd = os.open(directory / "trial-orders.jsonl",
                 os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "a+", encoding="utf-8") as stream:
        mode = os.fstat(fd).st_mode
        require(S_ISREG(mode) and mode & 0o777 == 0o600, "建单台账权限不符")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sync_directory(directory)
        yield Ledger(stream, directory, registration, name)


def receipt(value):
    return isinstance(value, str) and bool(value.strip())


def require_cleanup_notice(evidence):
    require(all(receipt(evidence.get(key)) for key in ("approver_notice", "configurator_notice")),
            "清理前通知留痕缺失")
    require(evidence["approver_notice"] != evidence["configurator_notice"], "清理通知须留存两份回执")


def preflight(evidence, config_data, draft_data, login_data, clock):
    require(evidence.get("is_test") is True, "登记须明确授权测试单")
    require(all(receipt(evidence.get(key)) for key in _EVIDENCE), "当次前置留痕不完整")
    require_cleanup_notice(evidence)
    reviews = evidence.get("two_reviews")
    require(isinstance(reviews, dict)
            and all(receipt(reviews.get(key)) for key in ("internal", "external"))
            and reviews["internal"] != reviews["external"], "缺少两层审核的独立回执")
    for key, data in (("config", config_data), ("draft", draft_data), ("login", login_data)):
        require(digest(data) == evidence[key + "_sha256"], "私有文件摘要与登记不符")
    window = Window(utc(evidence["window"]["start"]), utc(evidence["window"]["end"]))
    require(window.contains(clock()), "验收窗口未开启")
    return window


def https_origin(origin):
    parts = urlsplit(origin)
    require(parts.scheme == "https" and parts.hostname is not None
            and re.fullmatch(r"[a-z0-9.-]+", parts.hostname) is not None
            and not (parts.username or parts.password or parts.path or parts.query or parts.fragment),
            "官方接口须为规范 HTTPS 源")
    canonical = "https://" + parts.hostname + (f":{parts.port}" if parts.port else "")
    require(origin == canonical, "官方接口须为规范 HTTPS 源")
    return canonical


def valid_path(path):
    return (isinstance(path, str) and re.fullmatch(r"/[A-Za-z0-9_./-]+", path) is not None
            and all(segment not in ("", ".", "..") for segment in path[1:].split("/")))


def secret_safe(value):
    return isinstance(value, str) and re.fullmatch(r"[!-~]+", value) is not None


def extract_token(logged, name):
    require(receipt(name), "会话 cookie 名称未登记")
    matches = [cookie for cookie in logged["cookies"] if cookie["name"] == name]
    require(len(matches) == 1, "登录会话无法核实")
    cookie = matches[0]
    require(cookie["domain"] in (_CREATE_HOST, ".mingdao.com", "mingdao.com")
            and cookie.get("secure") is True and isinstance(cookie.get("path"), str)
            and (_CREATE_PATH == cookie["path"] or _CREATE_PATH.startswith(cookie["path"].rstrip("/") + "/"))
            and type(cookie.get("expires")) in (int, float)
            and (cookie["expires"] == -1 or cookie["expires"] > time.time())
            and secret_safe(cookie["value"]), "登录会话无法核实")
    return cookie["value"]


@dataclass(frozen=True, repr=False)
class Record:
    record_id: str
    name: str
    status: str
    revision: int
    platform_status: str | int


_MISSING = object()
_NO_CELL = object()


class Official:
    """REST 取值路径、查询字段和状态映射由私有登记冻结。"""

    def __init__(self, config, token, client):
        self.config, self._token, self.client = config, token, client
        self.origin = https_origin(config["origin"])
        for action in ("read", "search", "tags", "logs", "delete"):
            spec = config[action]
            path = spec["path"]
            bound = action in ("read", "logs", "delete")
            require(path.count("{record_id}") == int(bound)
                    and valid_path(path.replace("{record_id}", "placeholder")), "官方接口路径无效")
            if action in ("search", "tags"):
                require(receipt(spec["name_param"]) and isinstance(spec.get("params", {}), dict)
                        and spec["name_param"] not in spec.get("params", {}), "官方查询配置无效")
        require(config["read"]["path"].endswith("/rows/{record_id}")
                and config["search"]["path"] + "/{record_id}" == config["read"]["path"]
                and config["tags"]["path"].endswith("/rows")
                and config["tags"]["path"] != config["search"]["path"]
                and config["logs"]["path"] == config["read"]["path"] + "/logs"
                and config["delete"]["path"] == config["read"]["path"]
                and config["delete"]["method"] == "DELETE", "官方记录、日志或删除入口未绑定同一张表")
        deletion = config["delete"]
        condition = deletion["condition"]
        require(condition is None or (receipt(condition) and condition != "permanent"
                and deletion.get("condition_value", "revision") in ("revision", "status")),
                "官方删除条件配置无效")
        miss_status = config["read"]["missing_status"]
        require(type(miss_status) is int and (200 <= miss_status < 300 or miss_status in (404, 410))
                and config["read"]["missing_key"] and config["read"]["missing_value"] is not None
                and (config["read"]["missing_key"], config["read"]["missing_value"])
                != (config["read"]["success_key"], config["read"]["success_value"]),
                "记录不存在信号配置无效")

    def _request(self, action, record_id="", *, params=None, body=None):
        require(not record_id or uuid_record(record_id), "记录编号格式无效")
        spec = self.config[action]
        url = self.origin + spec["path"].replace("{record_id}", record_id)
        method = "DELETE" if action == "delete" else "GET"
        failed = False
        try:
            with self.client.stream(method, url, params=params, json=body, follow_redirects=False,
                                    headers={"Authorization": "Bearer " + self._token}) as response:
                buffer = bytearray()
                for chunk in response.iter_bytes():
                    buffer.extend(chunk)
                    require(len(buffer) <= _LIMIT, "官方接口响应超出限制")
                status = response.status_code
            payload = decode(buffer)
        except Exception:
            failed = True
        if failed:
            raise Stop("官方接口请求或响应未确认")
        if action == "read" and status == spec["missing_status"]:
            try:
                marker = at(payload, spec["missing_key"])
            except (KeyError, TypeError, IndexError):
                marker = None
            if type(marker) is type(spec["missing_value"]) and marker == spec["missing_value"]:
                return _MISSING
        require(200 <= status < 300, "官方接口请求失败")
        marker = at(payload, spec["success_key"])
        expected = spec["success_value"]
        require(type(marker) is type(expected) and marker == expected, "官方接口结果未确认")
        return at(payload, spec["data"])

    def read(self, record_id):
        return self._request("read", record_id)

    def cells(self, raw):
        spec = self.config["record"]
        values = at(raw, spec["cells"])
        require(isinstance(values, dict), "官方记录字段未确认")
        excluded = spec.get("metadata", [])
        require(isinstance(excluded, list) and all(receipt(key) for key in excluded),
                "官方记录元数据配置无效")
        return {key: value for key, value in values.items() if key not in excluded}

    def record(self, raw):
        spec = self.config["record"]
        record_id, name, state, revision = (at(raw, spec[key]) for key in ("id", "name", "status", "revision"))
        require(uuid_record(record_id) and receipt(name) and type(state) in (int, str)
                and type(revision) is int and revision >= 0, "官方记录标识、状态或版本未确认")
        return Record(record_id, name, self.config["status_map"][str(state)], revision, state)

    def _query(self, action, name):
        spec = self.config[action]
        params = {**spec.get("params", {}), spec["name_param"]: name}
        result = self._request(action, params=params)
        rows = at(result, spec["rows"])
        require(at(result, spec["has_more"]) is False and isinstance(rows, list), "官方查询结果不完整")
        named = [(at(row, spec["name"]), row) for row in rows]
        require(all(receipt(candidate) for candidate, _ in named), "官方查询返回无效名称")
        return [row for candidate, row in named if candidate == name]

    def find(self, name):
        spec = self.config["search"]
        ids = [at(row, spec["id"]) for row in self._query("search", name)]
        require(all(uuid_record(item) for item in ids), "名称查询记录编号未确认")
        return ids

    def tag(self, name):
        spec = self.config["tags"]
        rows = self._query("tags", name)
        require(len(rows) == 1, "用户标签查询未获得唯一同名记录，需人工")
        record_id = at(rows[0], spec["id"])
        require(uuid_record(record_id), "用户标签记录编号未确认")
        return record_id

    def history(self, current):
        spec = self.config["logs"]
        result = self._request("logs", current.record_id)
        rows = at(result, spec["rows"])
        require(at(result, spec["has_more"]) is False and at(result, spec["complete"]) is True
                and isinstance(rows, list) and bool(rows), "状态历史不能证明完整")
        last_state, last_time = None, None
        for expected_revision, row in enumerate(rows):
            revision = at(row, spec["revision"])
            before, after = at(row, spec["before"]), at(row, spec["after"])
            before = None if before is None else self.config["status_map"][str(before)]
            after = self.config["status_map"][str(after)]
            changed_at = utc(at(row, spec["time"]), platform=True)
            require(at(row, spec["id"]) == current.record_id and type(revision) is int
                    and revision == expected_revision and before == last_state
                    and (last_time is None or changed_at >= last_time), "状态历史不能证明完整")
            require(after in (_NEW, _PENDING) and (expected_revision or after == _NEW),
                    "状态历史曾进入待配置或后续状态")
            last_state, last_time = after, changed_at
        require(current.revision == len(rows) - 1 and current.status == last_state,
                "状态历史与当前记录不一致")

    def delete(self, current):
        spec = self.config["delete"]
        condition = spec["condition"]
        value = current.revision if spec.get("condition_value", "revision") == "revision" else current.platform_status
        self._request("delete", current.record_id, params={"permanent": "false"},
                      body={condition: value} if condition is not None else None)


def draft_values(draft):
    require(isinstance(draft, dict) and draft.keys() <= _DRAFT_FIELDS, "草稿字段无效")
    values = {}
    for key, field in draft.items():
        require(isinstance(field, dict) and "value" in field, "草稿取值结构无效")
        require(field.get("uncertainty") is None, f"草稿字段 {key} 待确认，需人工")
        if field["value"] is not None:
            values[key] = field["value"]
    require(all(key in values for key in _VARIABLE), "草稿缺少可变字段")
    name = values["policy_name"]
    require(receipt(name) and len(name) > len(_PREFIX) and not any(c in name for c in "\r\n<>"),
            "草稿政策名称格式无效")
    for key in ("start_date", "end_date"):
        value = values[key]
        require(isinstance(value, str) and date.fromisoformat(value).isoformat() == value, "草稿日期格式无效")
    require(values["start_date"] <= values["end_date"], "草稿开始日期晚于结束日期")
    require(all(type(values[key]) is int and values[key] >= 0 for key in ("recharge_days", "gift_days")),
            "草稿充值或赠送天数无效")
    tags = values["user_tags"]
    require(isinstance(tags, list) and all(receipt(tag) for tag in tags) and len(set(tags)) == len(tags),
            "草稿用户标签格式无效")
    packages = values["packages"]
    require(isinstance(packages, list) and bool(packages) and all(
        isinstance(row, dict) and set(row) == {"current_package", "gift_package"}
        and receipt(row["current_package"]) and (row["gift_package"] is None or receipt(row["gift_package"]))
        for row in packages
    ), "草稿节目包格式无效")
    return values


def unpack_array(value):
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = decode(value)
    require(isinstance(value, list), "数组单元格格式无效")
    return value


def plain_cell(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return compact(value)


def relation_item(item, target, source):
    if isinstance(item, dict):
        # 官方读接口与网页写接口可能使用不同的记录 ID 键。
        require(not (target in item and source in item and item[target] != item[source]),
                "关联单元格标识不一致")
        item = item.get(target, item.get(source))
    require(receipt(item), "关联单元格标识缺失")
    return {target: item}


def subtable_rows(value, *, metadata=()):
    result = []
    for row in unpack_array(value):
        if isinstance(row, list):
            require(all(isinstance(cell, dict) and receipt(cell.get("controlId")) and "value" in cell
                        for cell in row), "子表单元格格式无效")
            row_values = {cell["controlId"]: cell["value"] for cell in row}
            require(len(row_values) == len(row), "子表含重复字段")
            row = row_values
        require(isinstance(row, dict), "子表行格式无效")
        result.append({key: value for key, value in row.items() if key not in metadata})
    return result


def encode_cell(value, spec):
    kind = spec["type"]
    if kind not in _ARRAY:
        return plain_cell(value)
    if kind == 34:
        children = spec["children"]
        result = []
        for row in subtable_rows(value, metadata=spec.get("row_metadata", [])):
            require(row.keys() == children.keys(), "子表模板字段与类型配置不一致")
            result.append([{"controlId": key, "value": encode_cell(row[key], child)}
                           for key, child in children.items()])
        return compact(result)
    items = unpack_array(value)
    if kind in _SELECT:
        require(all(receipt(item) for item in items) and (kind == 10 or len(items) <= 1),
                "选项单元格格式无效")
    elif kind == 29:
        items = [relation_item(item, "sid", spec.get("relation_id_key", "rowid")) for item in items]
    else:
        items = [relation_item(item, "accountId", spec.get("member_id_key", "accountId")) for item in items]
    return compact(items)


def equivalent(left, right, spec):
    kind = spec["type"]
    if kind in (6, 8) and left and right:
        try:
            return Decimal(left) == Decimal(right)
        except InvalidOperation:
            return False
    if kind == 15 and left and right:
        try:
            def midnight(value):
                parsed = datetime.fromisoformat(value)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=_PLATFORM_TZ)
            return midnight(left) == midnight(right)
        except ValueError:
            return False
    if kind in (10, 26, 29):
        return sorted(map(compact, decode(left))) == sorted(map(compact, decode(right)))
    if kind == 34:
        expected, observed = subtable_rows(left), subtable_rows(right)
        return len(expected) == len(observed) and all(
            equivalent(row[key], actual[key], child)
            for row, actual in zip(expected, observed)
            for key, child in spec["children"].items()
        )
    return left == right


def package_names(rows, spec):
    def name(row, mapping):
        value = row[mapping["control"]]
        if value is None or value == "" or value == [] or value == "[]":
            return None
        path = mapping["name_path"]
        if path and isinstance(value, str):
            value = decode(value)
        return at(value, path)

    return [{"current_package": name(row, spec["current"]), "gift_package": name(row, spec["gift"])}
            for row in rows]


def cell_schema(fields_config, skip):
    require(isinstance(fields_config, dict) and isinstance(skip, list)
            and all(isinstance(key, str) for key in skip) and set(skip) <= fields_config.keys(),
            "字段类型或回读跳过配置无效")
    draft_controls = {}
    for control, spec in fields_config.items():
        require(receipt(control) and receipt(spec["label"]) and type(spec["type"]) is int and spec["type"] > 0,
                "字段类型配置无效")
        if spec["type"] == 34:
            children = spec["children"]
            require(isinstance(children, dict) and bool(children) and all(
                receipt(child_id) and type(child["type"]) is int and child["type"] > 0
                and child["type"] != 34 for child_id, child in children.items()
            ), "子表字段类型配置无效")
            metadata = spec.get("row_metadata", [])
            require(isinstance(metadata, list) and all(receipt(key) for key in metadata)
                    and not (set(metadata) & children.keys()), "子表元数据配置无效")
        key = spec.get("draft")
        if key is not None:
            require(key in _DRAFT_FIELDS and key not in draft_controls, "草稿与模板字段映射不唯一")
            draft_controls[key] = control
    require(set(_VARIABLE) | {"approval_url", "company", "maintain_mutual_exclusion_group"}
            <= draft_controls.keys(), "草稿与模板字段映射不全")
    require(all(draft_controls[key] not in skip for key in (*_VARIABLE, "approval_url")),
            "不可跳过建单关键字段的回读")
    expected_types = {"user_tags": {29}, "packages": {34}}
    require(all(fields_config[draft_controls[key]]["type"] not in _ARRAY
                for key in (*_VARIABLE[:5], "approval_url")), "草稿标量字段的控件类型无效")
    require(all(fields_config[draft_controls[key]]["type"] in kinds
                for key, kinds in expected_types.items()), "关键字段的控件类型无效")
    return draft_controls


def draft_cell(value, spec):
    options = spec.get("draft_values")
    if options is not None:
        # 选项名、成员名等业务值采用私有配置的确定映射。
        key = compact(value)
        require(key in options, f"字段 {spec['label']} 缺少草稿值映射，需人工")
        value = options[key]
    elif spec["type"] in _ARRAY and spec["type"] != 34:
        raise Stop(f"字段 {spec['label']} 缺少草稿值映射，需人工")
    return encode_cell(value, spec)


def assemble(values, template_cells, config, resolve_tag, *, is_test):
    schema = config["fields"]
    draft_controls = cell_schema(schema, config["readback_skip"])
    require(is_test is True, "正式单不得照抄任务审批链接，需人工")
    require(isinstance(template_cells, dict) and template_cells.keys() == schema.keys(),
            "模板字段不全或存在未配置类型的字段")
    approval_link = template_cells[draft_controls["approval_url"]]
    require(receipt(approval_link) and urlsplit(approval_link).scheme in ("http", "https")
            and bool(urlsplit(approval_link).hostname), "模板任务审批链接缺失或无效")
    country = config["template"]["country"]
    country_control = country["control"]
    require(encode_cell(country["value"], schema[country_control])
            == encode_cell(template_cells[country_control], schema[country_control]),
            "模板国家与登记不一致")
    require(encode_cell(country["value"], schema[country_control]) not in ("", "[]")
            and country_control not in (draft_controls[key] for key in _VARIABLE),
            "模板国家绑定无效")

    package_control = draft_controls["packages"]
    require(package_names(subtable_rows(template_cells[package_control]), schema[package_control])
            == values["packages"], "节目包与模板不同，需人工")
    for key in values.keys() - set(_VARIABLE):
        control = draft_controls.get(key)
        require(control is not None, f"草稿字段 {key} 无法核对模板，需人工")
        spec = schema[control]
        require(equivalent(draft_cell(values[key], spec), encode_cell(template_cells[control], spec), spec),
                f"字段 {spec['label']} 与模板不同，需人工")

    controls, summary = [], []
    for control, spec in schema.items():
        key = spec.get("draft")
        from_draft = key in _VARIABLE and key != "packages"
        source = "草稿" if from_draft else "模板"
        value = values[key] if from_draft else template_cells[control]
        if key == "user_tags":
            value = [resolve_tag(tag) for tag in value]
        encoded = encode_cell(value, spec)
        controls.append({"controlId": control, "type": spec["type"], "value": encoded})
        summary.append({"字段名": spec["label"], "类型": spec["type"], "来源": source,
                        "值指纹": digest(encoded.encode())[:12]})
    return controls, summary


def compare(expected, raw_cells, config):
    results = []
    for control in expected:
        key = control["controlId"]
        field = config["fields"][key]
        if key in config["readback_skip"]:
            result = "跳过"
        elif (actual := raw_cells.get(key, _NO_CELL)) is _NO_CELL:
            result = "缺失"
        else:
            try:
                matches = equivalent(control["value"], encode_cell(actual, field), field)
            except (Stop, ValueError, TypeError, KeyError, IndexError):
                matches = False
            result = "一致" if matches else "不一致"
        results.append({"字段名": field["label"], "结果": result})
    return results, Counter(row["结果"] for row in results)


class CreateRequest:
    """单跳、预建连，只有一个不可撤销的请求交付点。"""

    def __init__(self, spec, evidence, token, *, connect=http.client.HTTPSConnection):
        endpoint = spec["endpoint"]
        require(endpoint == {"host": _CREATE_HOST, "method": "POST", "path": _CREATE_PATH}
                and endpoint == evidence["create_endpoint"], "建单写入入口与授权登记不符")
        self.endpoint, self.spec, self._token = endpoint.copy(), spec, token
        self.connect = connect
        self.started = False

    def payload(self, controls):
        identifiers = self.spec["identifiers"]
        keys = ("worksheetId", "appId", "viewId", "projectId")
        require(set(identifiers) == set(keys) and all(receipt(identifiers[key]) for key in keys),
                "建单目标标识不全")
        return compact({**identifiers, "rowStatus": 1, "receiveControls": controls}).encode()

    @contextmanager
    def prepare(self, body):
        require(not self.started and len(body) <= _LIMIT, "建单请求已尝试或正文过长")
        connection = self.connect(self.endpoint["host"], timeout=5, context=ssl.create_default_context())
        connection.set_debuglevel(0)
        try:
            connection.connect()
            connection.auto_open = 0
            connection.putrequest(self.endpoint["method"], self.endpoint["path"], skip_accept_encoding=True)
            connection.putheader("Authorization", "md_pss_id " + self._token)
            connection.putheader("Accept-Encoding", "identity")
            connection.putheader("Content-Type", "application/json; charset=utf-8")
            connection.putheader("Content-Length", str(len(body)))

            def send():
                require(not self.started and connection.sock is not None, "建单传输不可用")
                self.started = True
                connection.endheaders(body)

            def record_id():
                response = connection.getresponse()
                raw = response.read(_LIMIT + 1)
                require(200 <= response.status < 300 and len(raw) <= _LIMIT, "建单响应未确认")
                record_id = at(decode(raw), self.spec["response_id"])
                require(uuid_record(record_id), "建单记录编号未确认")
                return record_id

            yield send, record_id
        finally:
            connection.close()


def card_summary(values, *, default_mutual_exclusion=False):
    tags = "、".join(values["user_tags"]) or "无"
    packages = "；".join(f"{row['current_package']} / {row['gift_package'] or '无赠送包'}"
                        for row in values["packages"])
    adjustment = "自动调整：互斥组按默认填是\n" if default_mutual_exclusion else ""
    return (f"拟建 X5 测试单 1 条\n政策名称：{values['policy_name']}\n"
            f"起止日期：{values['start_date']} 至 {values['end_date']}\n"
            f"充值 / 赠送天数：{values['recharge_days']} / {values['gift_days']}\n"
            f"用户标签：{tags}\n节目包（当前 / 赠送）：{packages}\n"
            f"{adjustment}同意后将建单并触发同国家政策重算。请在测试与清理期间保持该记录状态。")


def stop_if_interrupted(stop):
    if stop.is_set():
        raise Stop("审批监听已停止或执行中断")


def abandon(ledger, feishu, approval_id, binding, announce, reason):
    if approval_id is not None:
        feishu.close_create_approval(approval_id, binding)
    ledger.append("aborted_before_write", allowed_writes=0)
    ledger.append("approval_closed", reason=reason)
    announce(f"未建单（{reason}）")


def delete_test(api, ledger, record_id, evidence, announce, stop):
    def current():
        raw = api.read(record_id)
        require(raw is not _MISSING, "删除前记录不存在")
        result = api.record(raw)
        require((result.record_id, result.name, result.status) == (record_id, ledger.name, _NEW),
                "删除前名称、记录编号或状态回读不符")
        return result

    try:
        ledger.registered(record_id)
        require_cleanup_notice(evidence)
        announce("开始核验 D15 删除条件")
        earlier = current()
        api.history(earlier)
        ledger.append("delete_attempt", record_id)
        latest = current()
        require(latest == earlier, "状态历史与最新回读不一致")
        stop_if_interrupted(stop)
        api.delete(latest)
        require(api.read(record_id) is _MISSING, "删除后未收到登记的记录不存在信号")
        stop_if_interrupted(stop)
    except (Exception, KeyboardInterrupt) as error:
        ledger.append("delete_unconfirmed", record_id)
        reason = str(error) if type(error) is Stop else "删除状态未确认"
        announce(f"删除未获确认（{reason}），需人工核对")
        if isinstance(error, KeyboardInterrupt):
            raise
        raise Stop(reason) from None
    ledger.append("delete_verified", record_id)
    announce("删除已核验，官方接口回读记录不存在")


def run_order(ledger, api, create, feishu, window, payload, summary, controls, config, evidence,
              contact, login_check, stop, *, clock=time.time, pause=time.sleep):
    def announce(text):
        feishu.transport.announce(f"{ledger.name} · {text}\n{contact}")

    registration_hash = digest(ledger.registration.encode())
    binding = "create:" + registration_hash
    approval_id = None

    def before_send():
        stop_if_interrupted(stop)
        require(not api.find(ledger.name), "已存在同名记录，需人工")
        login_check()
        stop_if_interrupted(stop)
        ledger.append("create_attempt")

    try:
        while not feishu.approval_connection_ready():
            stop_if_interrupted(stop)
            require(window.contains(clock()), "审批到期")
            pause(0.05)
        approval_id = feishu.publish_approval(binding, summary)
        ledger.append("card_published")
        while (status := feishu.approval_status(approval_id, binding)) == "pending":
            stop_if_interrupted(stop)
            require(window.contains(clock()), "审批到期")
            pause(0.05)
        if status != "approve":
            raise Stop("群卡片已驳回" if status == "reject" else "审批到期或断线")
        with create.prepare(payload) as (transmit, read_id):
            def send():
                stop_if_interrupted(stop)
                transmit()

            require(feishu.send_first_create(approval_id, binding, before_send, send), "建单审批已关闭")
            record_id = read_id()
        ledger.append("created", record_id)
    except (Exception, KeyboardInterrupt) as error:
        interrupted = isinstance(error, KeyboardInterrupt)
        if create.started:
            ledger.append("create_unknown")
            try:
                matches = api.find(ledger.name)
                ledger.append("create_reconciled_read", matches[0] if len(matches) == 1 else None,
                              candidates=len(matches))
            except (Exception, KeyboardInterrupt):
                ledger.append("create_reconciled_read_unconfirmed")
            announce("建单结果未知，已冻结清理，需人工核对官方回读和私有台账")
            raise Stop("建单结果未知，需人工对账") from None
        reason = str(error) if type(error) in (Stop, ApprovalClosed) else "审批或建单准备未完成"
        if interrupted:
            reason = "执行中断"
        abandon(ledger, feishu, approval_id, binding, announce, reason)
        return False

    try:
        stop_if_interrupted(stop)
        raw = api.read(record_id)
        require(raw is not _MISSING and api.record(raw).record_id == record_id, "建单回读记录未确认")
        observed = api.cells(raw)
        rows, counts = compare(controls, observed, config)
        exclusive(ledger.directory / f"create-readback-{registration_hash}.json", rows)
        ledger.append("readback_compared", record_id, counts=dict(counts))
        announce(f"官方回读 一致 {counts['一致']} / 不一致 {counts['不一致']} / "
                 f"缺失 {counts['缺失']} / 跳过 {counts['跳过']}")
    except (Exception, KeyboardInterrupt) as error:
        ledger.append("readback_unconfirmed", record_id)
        announce("官方回读未获确认，已停止，需人工核对")
        if isinstance(error, KeyboardInterrupt):
            raise
        raise Stop("官方回读未获确认") from None
    delete_test(api, ledger, record_id, evidence, announce, stop)
    return not (counts["不一致"] or counts["缺失"])


class Arguments(argparse.ArgumentParser):
    def error(self, _message):
        self.exit(2, "参数无效，请查看 --help。\n")


def run(args, directory, *, clock=time.time):
    require(not directory.is_symlink() and directory.is_dir() and directory.stat().st_mode & 0o777 == 0o700,
            "运行目录下 .private 须为 0700 的私有目录")
    require(receipt(args.registration) and len(args.registration) <= 200, "登记编号无效")
    config_data = private_bytes(args.config)
    draft_data = private_bytes(args.draft)
    login_path = directory / "mingdao-state.json"
    login_data = private_bytes(login_path)
    config = decode(config_data)
    evidence = decode(private_bytes(directory / "create-authorizations.json"))[args.registration]
    window = preflight(evidence, config_data, draft_data, login_data, clock)
    values = draft_values(decode(draft_data))
    mutex = "maintain_mutual_exclusion_group"
    default_mutual_exclusion = mutex not in values
    values.setdefault(mutex, "是")
    require(values["policy_name"].startswith(_PREFIX), "X5 草稿名称缺少测试前缀")

    token = extract_token(decode(login_data), config["create"]["session_cookie"])
    with args.env_file.open(encoding="utf-8-sig") as stream:
        secrets = dotenv_values(stream=stream, interpolate=False)
    official_key = config["official"]["token_key"]
    require(isinstance(official_key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", official_key),
            "官方令牌配置键无效")
    official_token = secrets.get(official_key)
    require(secret_safe(official_token), "官方接口令牌缺失或格式无效")
    contact = mention(config["configurator"]["id"], config["configurator"]["label"])
    summary = card_summary(values, default_mutual_exclusion=default_mutual_exclusion)
    tracked = [summary, contact, args.registration, *(spec["label"] for spec in config["fields"].values())]
    require(not any(secret in item for item in tracked for secret in (token, official_token)),
            "展示字段与凭据冲突")

    def login_check():
        current = private_bytes(login_path)
        require(digest(current) == evidence["login_sha256"]
                and extract_token(decode(current), config["create"]["session_cookie"]) == token,
                "建单登录状态已变化")

    create = CreateRequest(config["create"], evidence, token)
    worksheet = config["create"]["identifiers"]["worksheetId"]
    require(config["official"]["search"]["path"].split("/")[-2:-1] == [worksheet],
            "官方读写与建单目标表不一致")
    settings = load_settings(args.env_file)
    with ledger_open(directory, args.registration, values["policy_name"]) as ledger:
        ledger.check_available()
        with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
            api = Official(config["official"], official_token, client)
            template_id = config["template"]["row_id"]
            require(uuid_record(template_id), "模板记录编号须为 UUID")
            template = api.read(template_id)
            require(template is not _MISSING and api.record(template).record_id == template_id,
                    "模板记录回读未确认")
            require(not (set(config["official"]["record"].get("metadata", [])) & config["fields"].keys()),
                    "官方记录元数据与控件配置重叠")
            cells = api.cells(template)
            registration_hash = digest(args.registration.encode())
            try:
                controls, fields_summary = assemble(values, cells, config, api.tag, is_test=evidence["is_test"])
            except Stop as error:
                exclusive(directory / f"create-preflight-{registration_hash}-{time.time_ns()}.json",
                          {"结果": "未建单", "原因": str(error)})
                raise
            payload = create.payload(controls)
            require(window.contains(clock()), "验收窗口已关闭")
            ledger.reserve()
            try:
                exclusive(directory / f"create-fields-{registration_hash}.json", fields_summary)
                service = FeishuService(settings, window, directory / "create-approvals.sqlite",
                                        process=None, clock=clock)
            except (Exception, KeyboardInterrupt):
                ledger.append("aborted_before_write", allowed_writes=0)
                raise
            return service.run_create(lambda stop: run_order(
                ledger, api, create, service, window, payload, summary, controls, config, evidence,
                contact, login_check, stop, clock=clock,
            ))


def main(argv=None):
    parser = Arguments(description="Trace #12 X5 群审批后建单及 D15 清理")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--registration", required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("draft", type=Path)
    args = parser.parse_args(argv)
    for name in ("Lark", "httpx", "httpcore", "websockets", "dotenv.main", "urllib3"):
        logging.getLogger(name).setLevel(logging.CRITICAL + 1)
    try:
        if run(args, Path(".private")):
            print("建单回读一致，D15 删除已核验。")
            return 0
        print("验收未通过，请核对群公告与私有报告。", file=sys.stderr)
    except Stop as error:
        print(f"已停止：{error}", file=sys.stderr)
    except KeyboardInterrupt:
        print("执行中断，请核对私有台账。", file=sys.stderr)
    except Exception:
        print("步骤未获确认，请核对私有台账。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
