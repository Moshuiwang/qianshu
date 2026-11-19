"""Trace #12 X3 官方接口只读比对。"""

import argparse
import json
import logging
import math
import os
import re
from collections import Counter
from dataclasses import fields
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from dotenv import dotenv_values

from qianshu.draft import PolicyDraft
from qianshu.mingdao.create_order import (
    Arguments, Official, Stop, at, bad_constant, https_origin, package_names,
    private_bytes, receipt, require, secret_safe, subtable_rows, unique_object,
    unpack_array, uuid_record,
)

_RECORDS = 6
_PAGE_SIZE = 50
_MAX_RESPONSE = 8 * 1024 * 1024
_DRAFT_FIELDS = {field.name for field in fields(PolicyDraft)}
_TYPES = {
    2: "text", 6: "number", 8: "number", 9: "single_select", 11: "single_select",
    15: "date", 16: "date", 29: "user_tags", 34: "packages",
}
_KINDS = set(_TYPES.values()) | {"unsupported"}
_RELATIONS = {"user_tags", "packages"}
_RESULTS = ("一致", "不一致", "草稿不确定", "人工为空", "无法比较")
_LOCAL_ERRORS = (Stop, ValueError, TypeError, KeyError, IndexError, RecursionError)


def _finite_float(value):
    number = float(value)
    require(math.isfinite(number), "JSON 含无效数值")
    return number


def _decode(data):
    return json.loads(data, object_pairs_hook=unique_object, parse_constant=bad_constant,
                      parse_float=_finite_float)


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]+", value) is not None


def _path(value):
    return isinstance(value, list) and all(
        receipt(item) or (type(item) is int and item >= 0) for item in value
    )


def _configuration(config, drafts):
    require(isinstance(drafts, list) and len(drafts) == _RECORDS
            and all(isinstance(draft, dict) for draft in drafts), "草稿须为六条对象")
    require(_identifier(config["worksheet_id"]), "表标识无效")
    records = config["records"]
    require(isinstance(records, list) and len(records) == _RECORDS
            and all(uuid_record(row["rowId"]) and type(row["draft"]) is int for row in records)
            and {row["draft"] for row in records} == set(range(1, _RECORDS + 1))
            and len({row["rowId"].lower() for row in records}) == _RECORDS, "记录对应关系无效")
    configured = config["fields"]
    require(isinstance(configured, dict) and bool(configured), "字段配置为空")
    specs = {}
    for control, spec in configured.items():
        require(_identifier(control) and receipt(spec["label"]) and spec["draft"] in _DRAFT_FIELDS,
                "字段映射无效")
        kind = spec["type"]
        if type(kind) is int:
            require(kind > 0, "字段类型无效")
            kind = _TYPES.get(kind, "unsupported")
        require(kind in _KINDS, "字段类型无效")
        if kind == "single_select":
            mapping = spec["status_map"]
            require(isinstance(mapping, dict) and bool(mapping)
                    and all(receipt(key) and receipt(value) for key, value in mapping.items()),
                    "单选映射无效")
        if kind == "user_tags":
            require(_path(spec["relations"]["name_path"]), "标签名称路径无效")
        if kind == "packages":
            relation = spec["relations"]
            for column in ("current", "gift"):
                require(_identifier(relation[column]["control"]) and _path(relation[column]["name_path"]),
                        "节目包名称路径无效")
            require(relation["current"]["control"] != relation["gift"]["control"], "节目包列映射重复")
        specs[control] = {**spec, "type": kind}
    return records, specs


class Reader:
    """传输固定发 GET，调用方只传记录及控件标识。"""

    def __init__(self, client, origin, worksheet, token, app_id):
        require(_identifier(worksheet) and secret_safe(token) and secret_safe(app_id), "读取配置无效")
        self._client = client
        self._rows_url = https_origin(origin) + f"/v3/app/worksheets/{worksheet}/rows"
        self._headers = {"Authorization": "Bearer " + token, "HAP-Appid": app_id}

    def _get(self, row_id, control=None):
        require(uuid_record(row_id), "记录编号无效")
        url = f"{self._rows_url}/{row_id}"
        params = None
        if control is not None:
            require(_identifier(control), "关联控件标识无效")
            url += f"/relations/{control}"
            params = {"pageIndex": 1, "pageSize": _PAGE_SIZE}
        with self._client.stream("GET", url, params=params, headers=self._headers,
                                  follow_redirects=False) as response:
            require(200 <= response.status_code < 300, "官方读取失败")
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                require(len(body) <= _MAX_RESPONSE, "官方响应超出限制")
        payload = _decode(body)
        require(isinstance(payload, dict) and payload.get("success") is True, "官方结果未确认")
        return payload["data"]

    def detail(self, row_id):
        raw = self._get(row_id)
        # create_order 的详情检查只检查响应类型、官方 _id 和请求 UUID。
        return Official.detail(self, raw, row_id)

    def relations(self, row_id, control):
        result = self._get(row_id, control)
        require(isinstance(result, dict) and isinstance(rows := result.get("rows"), list)
                and type(total := result.get("total")) is int and total == len(rows) < _PAGE_SIZE
                and all(isinstance(row, dict) for row in rows), "关联响应结构或条数未确认")
        return rows


def _name(value):
    require(receipt(value), "名称无效")
    return value.strip().casefold().replace(" ", "_")


def _manual(reader, record, specs):
    detail = reader.detail(record)
    values = {}
    for control, spec in specs.items():
        kind = spec["type"]
        if kind in _RELATIONS:
            rows = reader.relations(record, control)
            relation = spec["relations"]
            if kind == "user_tags":
                value = [at(row, relation["name_path"]) for row in rows]
            else:
                value = package_names(subtable_rows(rows), relation)
            # 官方关联数据缺失名称时无法确认本条读取结果。
            _normalize(value, spec)
        else:
            value = detail[control]
        values[control] = value
    return values


def _empty(value):
    return value is None or value == [] or (isinstance(value, str) and not value.strip())


def _option(value, mapping, *, draft):
    if isinstance(value, str) and value.lstrip().startswith("["):
        value = unpack_array(value)
    if isinstance(value, list):
        if not value:
            return None
        require(len(value) == 1, "单选含多个选项")
        value = value[0]
    if isinstance(value, dict):
        choices = {mapping[item.strip()] for key in ("key", "value")
                   if isinstance(item := value.get(key), str) and item.strip() in mapping}
        require(len(choices) == 1, "单选映射缺失或冲突")
        return choices.pop()
    require(type(value) in (str, int), "单选值无效")
    key = str(value).strip()
    if draft and key in mapping.values():
        return key
    return mapping[key]


def _normalize(value, spec, *, draft=False):
    kind = spec["type"]
    if kind == "text":
        require(isinstance(value, str), "文本值无效")
        return value.strip()
    if kind == "number":
        require(type(value) in (int, float, str), "数值无效")
        number = Decimal(str(value).strip())
        require(number.is_finite(), "数值无效")
        return number
    if kind == "date":
        require(isinstance(value, str), "日期值无效")
        value = value.strip()
        require(re.match(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[T ]|$)", value), "日期值无效")
        return date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value).date()
    if kind == "single_select":
        return _option(value, spec["status_map"], draft=draft)
    if kind == "user_tags":
        require(isinstance(value, list), "标签列表无效")
        return Counter(_name(item) for item in value)
    if kind == "packages":
        require(isinstance(value, list), "节目包列表无效")
        return Counter(
            (_name(row["current_package"]), None if _empty(row["gift_package"]) else _name(row["gift_package"]))
            for row in value
        )
    raise Stop("字段无比较规则")


def _comparison(field, manual, spec):
    draft = field.get("value")
    if draft is None or field.get("uncertainty") is not None:
        return "草稿不确定"
    if _empty(manual):
        if spec["type"] in _RELATIONS and draft == []:
            return "一致"
        return "人工为空"
    try:
        observed = _normalize(manual, spec)
        if observed is None:
            return "人工为空"
        expected = _normalize(draft, spec, draft=True)
    except (*_LOCAL_ERRORS, InvalidOperation):
        return "无法比较"
    if expected is None:
        return "无法比较"
    return "一致" if expected == observed else "不一致"


def _report(reader, records, drafts, specs, secrets):
    counts = {control: dict.fromkeys(_RESULTS, 0) for control in specs}
    results = []
    for record in records:
        row_id, index = record["rowId"], record["draft"]
        draft = drafts[index - 1]
        result = {"rowId": _redact(row_id, secrets), "草稿序号": index, "读取": "成功", "字段": []}
        results.append(result)
        try:
            manual = _manual(reader, row_id, specs)
        except (*_LOCAL_ERRORS, httpx.HTTPError):
            result["读取"] = "读取失败"
            continue
        for control, spec in specs.items():
            field = draft.get(spec["draft"])
            field = field if isinstance(field, dict) else {}
            verdict = _comparison(field, manual[control], spec)
            result["字段"].append({
                "字段名": _redact(spec["label"], secrets),
                "草稿值": _redact(field.get("value"), secrets),
                "人工值": _redact(manual[control], secrets), "结论": verdict,
            })
            counts[control][verdict] += 1
    failed = sum(result["读取"] == "读取失败" for result in results)
    summary = []
    for control, counts_for_field in counts.items():
        same = counts_for_field["一致"]
        compared = same + counts_for_field["不一致"]
        summary.append({
            "字段名": _redact(specs[control]["label"], secrets), **counts_for_field, "读取失败": failed,
            "可比较数": compared, "记录数": len(records), "一致率": same / len(records),
        })
    return {"记录": results, "字段汇总": summary, "读取成功": len(results) - failed, "读取失败": failed}


def _redact(report, secrets):
    if isinstance(report, str):
        for secret in secrets:
            report = report.replace(secret, "[已隐藏]")
    elif isinstance(report, list):
        return [_redact(item, secrets) for item in report]
    elif isinstance(report, dict):
        return {_redact(key, secrets): _redact(value, secrets) for key, value in report.items()}
    return report


def _cell(value):
    rendered = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    safe = re.sub(r"[\r\n|\\`*_{}\[\]()!~]", lambda match: f"&#{ord(match[0])};", escape(rendered))
    return f"<code>{safe}</code>"


def _markdown(report):
    lines = ["# X3 只读比对报告", ""]
    for record in report["记录"]:
        lines.extend([
            f'## 草稿 {record["草稿序号"]}', "",
            f'记录 {_cell(record["rowId"])} · {record["读取"]}', "",
            "| 字段名 | 草稿值 | 人工值 | 结论 |", "| --- | --- | --- | --- |",
        ])
        for field in record["字段"]:
            cells = [_cell(field[key]) for key in ("字段名", "草稿值", "人工值", "结论")]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
    lines.extend([
        "## 字段一致率", "",
        f'每个字段以全部 {len(report["记录"])} 条记录为分母。', "",
        "| 字段名 | 一致 | 不一致 | 草稿不确定 | 人工为空 | 无法比较 | 读取失败 | 一致率 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for field in report["字段汇总"]:
        displayed = f'{field["一致率"]:.1%}'
        counts = " | ".join(str(field[key]) for key in (*_RESULTS, "读取失败"))
        lines.append(f'| {_cell(field["字段名"])} | {counts} | {displayed} |')
    return "\n".join(lines) + "\n"


def _write(output, markdown, report):
    contents = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", _markdown(report))
    with TemporaryDirectory(dir=output.parent) as directory:
        staged = []
        for name, content in zip(("report.json", "report.md"), contents):
            path = Path(directory) / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", errors="backslashreplace") as stream:
                stream.write(content)
                stream.flush()
                os.fchmod(stream.fileno(), 0o600)
                os.fsync(stream.fileno())
            staged.append(path)
        os.link(staged[0], output)
        try:
            os.link(staged[1], markdown)
        except OSError:
            output.unlink()
            raise


def _credentials(env_path, official):
    # dotenv 的语法警告可含行号，终端统一由入口输出计数。
    with env_path.open(encoding="utf-8-sig") as stream:
        secrets = dotenv_values(stream=stream, interpolate=False)
    names = [official[key] for key in ("token_key", "app_id_key")]
    require(all(isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                for name in names), "鉴权环境键名无效")
    values = [secrets.get(name) for name in names]
    require(all(secret_safe(value) for value in values), "官方鉴权配置无效")
    return values


def _run(args):
    config = _decode(private_bytes(args.config))
    drafts = _decode(private_bytes(args.drafts))
    records, specs = _configuration(config, drafts)
    official = config["official"]
    token, app_id = _credentials(args.env_file, official)
    output = args.output
    markdown = output.with_suffix(".md") if output.suffix == ".json" else output.with_name(output.name + ".md")
    require(output.parent.is_dir() and not any(os.path.lexists(path) for path in (output, markdown)),
            "报告路径须为新路径")
    with httpx.Client(trust_env=False, timeout=httpx.Timeout(30, connect=10)) as client:
        reader = Reader(client, official["origin"], config["worksheet_id"], token, app_id)
        secrets = sorted({token, app_id}, key=len, reverse=True)
        report = _report(reader, records, drafts, specs, secrets)
    _write(output, markdown, report)
    return report["读取成功"], report["读取失败"]


def main(argv=None):
    parser = Arguments(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--drafts", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--env-file", default=Path(".env"), type=Path)
    args = parser.parse_args(argv)
    # 屏蔽 HTTP 客户端及 dotenv 诊断，避免在终端显示私有路径或值。
    previous = logging.root.manager.disable
    logging.disable(max(previous, logging.CRITICAL))
    try:
        read, failed = _run(args)
    except (*_LOCAL_ERRORS, OSError):
        print("入口失败 1")
        return 1
    finally:
        logging.disable(previous)
    print(f"读取成功 {read}，读取失败 {failed}")
    return int(failed > 0)


if __name__ == "__main__":
    raise SystemExit(main())
