"""从 X5 已登记的表与模板读取 050 配置草稿。"""
import codecs
import logging
import os
import re
import sys
from datetime import UTC, datetime
from functools import partial
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from . import create_order as x5

FILES = ("order-config.draft.json", "create-authorization.draft.json", "sources.json")
HANDOFF = {"FEISHU_HANDOFF_OWNER_OPEN_ID", "FEISHU_HANDOFF_MEMBER_OPEN_IDS", "FEISHU_MENTION_LOG"}
OWNER = "待产品负责人确认"
MISSING = "待人工核对："
SCOPES = ("tag", "concurrent", "tariffs", "tasks", "products", "plans", "company_rows")
RULE_PARAMETERS = {"singlePurchaseTime": ["empty_input"], "文本": ["text"], "billingCycle": ["cycles"],
                   "升包执行任务状态值": ["inactive_value", "column"], "companyId": ["column"],
                   "同期有效优惠政策": ["child"], "标签与适用条件关系": ["default"]}


def now():
    return datetime.now(UTC).isoformat()


def fingerprint(value):
    return x5.digest(x5.compact(value).encode())[:12]


def wire_check(api, request):
    """最后一跳只接受官方行详情、关联行和指定视图的分页读取。"""
    url = request.url
    reason = "请求超出只读范围"
    x5.require(str(url.copy_with(path="/", query=None)) == api.origin + "/", reason)
    if request.method == "POST":
        body = x5.decode(request.content)
        paths = {api.config[key]["path"] for key in ("search", "tag_search", "tariffs") if key in api.config}
        x5.require(url.path in paths and re.fullmatch(r"/v3/app/worksheets/[\w-]+/rows/list", url.path)
                   and not url.query and set(body) == {"viewId", "pageIndex", "pageSize"}
                   and x5.receipt(body["viewId"]) and all(type(body[k]) is int and body[k] > 0
                                                         for k in ("pageIndex", "pageSize")), reason)
    else:
        match = re.fullmatch(r"/v3/app/worksheets/[\w-]+/rows/([\w-]+)(/relations/[\w-]+)?", url.path)
        x5.require(request.method == "GET" and match and x5.uuid_record(match[1])
                   and not request.content and set(url.params) <= {"pageIndex", "pageSize"}, reason)


class Sources:
    def __init__(self):
        self.entries = []

    def add(self, path, value, kind, stamp, evidence, *, children=True):
        # 私有 JSON 键也可能是内部标识。用路径数组的规范 JSON 指纹定位每项。
        missing = isinstance(value, str) and (value.startswith(MISSING) or value == OWNER)
        self.entries.append({"item_sha256_prefix": fingerprint(path), "kind": kind, "read_at": stamp,
                             "missing": missing, "count": 0 if missing else (
                                 len(value) if isinstance(value, (dict, list)) else 1),
                             "source_sha256_prefix": evidence})
        if children and isinstance(value, (dict, list)):
            for key, child in (value.items() if isinstance(value, dict) else enumerate(value)):
                self.add([*path, key], child, kind, stamp, evidence)

    def record(self, path, value, kind, source, *, stamp=None):
        self.add(path, value, kind, stamp or now(), fingerprint(source))
        return value


def select(mapping, keys):
    return {key: mapping[key] for key in keys.split() if key in mapping}


def x5_sections(base):
    # 仅读取 X5 识别的结构。忽略旁注、额外的响应元数据与联系人信息。
    cfg = select(base, "applicant_control readback_skip")
    create = cfg["create"] = select(base["create"], "response_id session_cookie")
    endpoint = base["create"]["endpoint"]
    x5.require(endpoint == {"host": x5._CREATE_HOST, "method": "POST", "path": x5._CREATE_PATH},
               "建单目标未核实")
    create["endpoint"] = endpoint
    create["identifiers"] = select(base["create"]["identifiers"], "worksheetId viewId appId projectId")
    cfg["fixed"] = {key: base["fixed"][key] for key in (cfg["applicant_control"], base["official"]["status_control"])}
    official = cfg["official"] = select(base["official"], "origin token_key app_id_key status_control status_map page_limit page_size")
    for key, keys in {
        "read": "path missing_status missing_key missing_value", "search": "path name_control view_ids",
        "delete": "path method condition condition_value", "logs": "path page_limit",
        "tag_search": "path view name_control", "tag_relation": "name_keys", "tariffs": "path",
        "products": "path", "company_rows": "path", "tasks": "path", "plans": "path",
    }.items():
        if key in base["official"]:
            official[key] = select(base["official"][key], keys)
    member = cfg["applicant_control"]
    member_spec = base["fields"][member]
    cfg["fixed"][member] = [item["accountId"] for item in x5.decode(x5.encode_cell(cfg["fixed"][member], member_spec))]
    fields = cfg["fields"] = {}
    for control, spec in base["fields"].items():
        field = fields[control] = select(spec, "label type draft display_name_key row_metadata member_id_key "
                                             "relation_id_key price_plan_control price_plan_id_column")
        if "draft_values" in spec and spec["type"] != 26:
            field["draft_values"] = spec["draft_values"]
        for role in ("current", "gift"):
            if role in spec:
                field[role] = select(spec[role], "control name_path")
        if "children" in spec:
            field["children"] = {k: select(v, "label type relation_id_key member_id_key")
                                 for k, v in spec["children"].items()}
    return cfg


def generate(api, base, scopes, template_id, sources, config_time):
    cfg = x5_sections(base)
    for key, value in cfg.items():
        sources.record(["config", key], value, "X5控件配置", value, stamp=config_time)
    controls = x5.cell_schema(cfg["fields"], cfg["readback_skip"])
    x5.fixed_cells(cfg, cfg["fields"], controls)
    raw = api.detail(api.read(template_id), template_id)
    template_time = now()
    country = base["template"]["country"]["control"]
    x5.require(raw[country] == base["template"]["country"]["value"], "同国家模板未确认")
    cfg["template"] = sources.record(["config", "template"], {
        "row_id": template_id, "country": {"control": country, "value": raw[country]}}, "模板", raw, stamp=template_time)
    for key in ("notice_recipients", "configurator"):
        cfg[key] = sources.record(["config", key], MISSING + "需私下登记通知对象", "通知名单", None)

    snapshot = cfg["order_snapshot"] = {}
    def put(key, value, kind, source):
        if key in ("tags", "tariffs", "upgrade_tasks") and isinstance(value, list):
            x5.require(all(isinstance(row, dict) and all(not isinstance(v, (dict, list)) for v in row.values())
                           for row in value), "业务列包含未确认的复合数据")
        snapshot[key] = sources.record(["config", "order_snapshot", key], value, kind, source)

    rules = snapshot["web_rules"] = {}
    for name, kind in x5.WEB_TYPES.items():
        matches = [key for key, spec in cfg["fields"].items()
                   if spec["label"] == name and spec["type"] == kind and key in raw]
        rule = {"control": matches[0] if len(matches) == 1 else MISSING + "模板控件未唯一定位",
                "formula": MISSING + "X5 未提供网页公式定义读取接口"}
        for parameter in RULE_PARAMETERS.get(name, ()):
            rule[parameter] = MISSING + "缺网页定义依据"
        rules[name] = sources.record(["config", "order_snapshot", "web_rules", name], rule,
                                     "模板控件与公式", matches, stamp=template_time)

    for key, reason in {
        "verified_combinations": "须核对真实测试单回读记录", "condition_scenario": "须核对适用条件场景",
        "member_names": "须私下核对成员",
        "companies": "须核对公司目录及关键字", "package_levels": "须核对节目包级别目录",
        "version": "须核对完整快照版本",
    }.items():
        put(key, MISSING + reason, "缺失", None)

    other_controls = set(cfg["fields"]) - set(controls.values()) - cfg["fixed"].keys() - {country}
    template_sources = {
        key: {**select(cfg["fields"][key], "label type"), "source": MISSING + "须核对模板复制及工作流归属"}
        for key in sorted(other_controls) if key in raw and cfg["fields"][key]["label"] not in x5.WEB_TYPES
    }
    put("template_sources", template_sources, "模板控件", template_sources)
    relations = {}
    for field in ("packages", "user_tags", "company"):
        control = controls[field]
        if cfg["fields"][control]["type"] in (29, 34):
            rows = x5.template_relation_rows(raw, control, lambda c: api.relations(template_id, c), field)
            relations[field] = rows
            sources.record(["relations", field], len(rows), "模板关联行", rows)
            sources.entries[-1]["count"] = len(rows)

    tag_scope = scopes.get("tag_scope")
    if tag_scope:
        index = api.tag_index(tag_scope)
        tags = [api.tag_facts(tag_scope, row["rowId"]) for row in relations["user_tags"]]
        x5.require(all(index.get(tag["name"]) == [tag["id"].lower()] for tag in tags), "标签全名未唯一匹配")
        put("tags", tags, "标签详情", tags)
    else:
        rows = relations["user_tags"]
        keys = cfg["official"]["tag_relation"]["name_keys"]
        tags = []
        for row in rows:
            names = {row[k] for k in keys if isinstance(row.get(k), str) and row[k].strip()}
            tags.append({"id": row["rowId"], "name": names.pop() if len(names) == 1 else MISSING + "标签全名不唯一",
                         **dict.fromkeys(("end", "shadow", "complete"), MISSING + "缺标签详情读取范围")})
        put("tags", tags, "模板标签关联行", rows)
    if scope := scopes.get("concurrent_scope"):
        rows = api.concurrent_rows(scope)
        x5.require(all(all(isinstance(v, str) for v in row.values()) for row in rows), "同期政策列未核实")
        put("concurrent_policies", {"rows": rows, "total": len(rows), "complete": True,
                                   "as_of": now(), "valid_until": MISSING + "须核对有效期"}, "同期全表查询", rows)
    else:
        put("concurrent_policies", MISSING + "缺同期全表读取范围", "缺失", None)
    if scope := scopes.get("tariffs_scope"):
        rows = api.current_tariffs(scope)
        put("tariffs", rows, "资费全表查询", rows)
    else:
        put("tariffs", MISSING + "缺资费表读取范围", "缺失", None)
    if (scope := scopes.get("tasks_scope")) and scope.get("record_id"):
        column = scope["columns"]["status"]
        row = api.current_task(scope, scope["record_id"], column)
        put("upgrade_task_id", row["id"], "任务详情", row)
        row[column] = row.pop("status")
        put("upgrade_tasks", [row], "任务详情", row)
    else:
        for key in ("upgrade_task_id", "upgrade_tasks"):
            put(key, MISSING + "缺任务记录定位", "缺失", None)
    for path, value in [(["config", "order_snapshot", "web_rules"], rules),
                        (["config", "order_snapshot"], snapshot)]:
        sources.add(path, value, "汇总", now(), fingerprint(value), children=False)
    # 即使上游把人员姓名混入了业务列，也不允许导出能从模板/私有名单识别出的实名。
    names = [entry.get("label") for entry in base.get("notice_recipients", [])]
    names.append(base.get("configurator", {}).get("label"))
    for key, field in cfg["fields"].items():
        if field["type"] == 26:
            names.extend(member.get("name") for member in x5.unpack_array(raw.get(key)) if isinstance(member, dict))
    wire = x5.compact(cfg)
    x5.require(all(name not in wire for name in names if x5.receipt(name)), "草稿含人员姓名")
    return cfg


def authorization(cfg, scopes, app, sources):
    evidence = {}
    def put(key, value, kind, source):
        evidence[key] = sources.record(["authorization", key], value, kind, source)
    put("is_test", False, "正式登记", False)
    put("config_sha256", x5.digest((x5.compact(cfg) + "\n").encode()), "配置完整字节", cfg)
    rules = cfg["order_snapshot"]["web_rules"]
    put("web_rules_sha256", x5.digest(x5.compact(rules).encode()), "公式规范JSON", rules)
    put("create_endpoint", cfg["create"]["endpoint"], "X5配置", cfg["create"]["endpoint"])
    for key in x5._EVIDENCE:
        put(key, OWNER, "待确认回执", None)
    put("notices", OWNER, "待确认回执", None)
    put("window", dict.fromkeys(("start", "end"), MISSING + "须核对授权窗口"), "授权窗口", None)
    put("two_reviews", {"internal": OWNER, "external": OWNER}, "待确认回执", None)
    for name in SCOPES:
        key = name + "_scope"
        scope = scopes.get(key)
        value = MISSING + "缺独立范围登记"
        if scope:
            x5.require(scope.get("app_sha256") == x5.digest(app.encode()), "读取范围应用不符")
            # 路径、列、视图、行号均可能含内部 ID，仅将应用指纹带入登记。
            value = {k: MISSING + "须私下核对范围原值" for k in
                     ("origin", "path", "columns", "view", "snapshot", "coverage", "record_id",
                      "unique_until", "name_control") if k in scope}
            if scope.get("coverage") == "full_table":
                value["coverage"] = "full_table"
            value["app_sha256"] = scope["app_sha256"]
        put(key, value, "独立读取范围", scope)
    return evidence


def handoff_key(name):
    return name in HANDOFF or name.startswith("FEISHU_HANDOFF_")


def official_bindings(stream, *, names):
    """只为选中的赋值构造 dotenv 输入。跳过值时只扫描分隔符和引号。"""
    selected = StringIO()
    chars = codecs.iterdecode(iter(partial(stream.read, 1), b""), "utf-8-sig")
    for first in chars:
        char = first
        header = []
        while char and char not in "=#\r\n":
            header.append(char)
            char = next(chars, "")
        key = "".join(header).strip()
        if key:
            match = re.fullmatch(r"(?:export\s+)?(?:'([^']+)'|([^\s']+))", key)
            x5.require(match, "官方接口环境配置键无效")
            key = match.group(1) or match.group(2)
            x5.require(not handoff_key(key), "凭据文件含转交配置")
        keep = key in names
        if keep:
            selected.write(key)
        if char == "=":
            if keep:
                selected.write("=")
            char = next(chars, "")
            while char and char.isspace() and char not in "\r\n":
                if keep:
                    selected.write(char)
                char = next(chars, "")
            if char in ("'", '"'):
                quote = char
                if keep:
                    selected.write(quote)
                escaped = False
                for char in chars:
                    if keep:
                        selected.write(char)
                    if char == quote and not escaped:
                        break
                    escaped = char == "\\" and not escaped
                else:
                    raise x5.Stop("官方接口鉴权配置无效")
                char = next(chars, "")
        while char and char not in "\r\n":
            if keep:
                selected.write(char)
            char = next(chars, "")
        if keep:
            selected.write("\n")
    selected.seek(0)
    return selected


def run(args):
    x5.require(not any(map(handoff_key, os.environ)), "环境含转交配置")
    directory = args.out_dir
    x5.require_private_directory(directory, "输出目录须为私有目录")
    for name in FILES:
        x5.require(not x5.path_present(directory / name, "无法检查输出"), "输出已存在")
    base = x5.decode(x5.private_bytes(args.config))
    config_time = now()
    scopes = x5.decode(x5.private_bytes(args.read_scopes)) if args.read_scopes else {}
    template = args.template_row_id or base["template"]["row_id"]
    x5.check_search_binding(base, base["official"])
    sources = Sources()
    with x5.outlet_scope() as guard:
        names = {base["official"][field] for field in ("token_key", "app_id_key")}
        secrets = x5.read_x5_settings(args.env_file, filter_stream=partial(official_bindings, names=names))
        _, setting = x5.configure_x5_outlet(guard, secrets, base, {"cookies": []})
        guard.check(scopes)
        app = setting("app_id_key")
        with x5.official_reader(base["official"], setting("token_key"), app, check=wire_check) as api:
            cfg = generate(api, base, scopes, template, sources, config_time)
        auth = authorization(cfg, scopes, app, sources)
        outputs = (cfg, auth, {"items": sources.entries})
        guard.check(outputs)
        with TemporaryDirectory(dir=directory) as stage:
            written = []
            try:
                for name, value in zip(FILES, outputs, strict=True):
                    x5.exclusive(Path(stage) / name, value)
                for name in FILES:
                    os.link(Path(stage) / name, directory / name)
                    written.append(directory / name)
                x5.sync_directory(directory)
            except BaseException:
                for path in written:
                    path.unlink()
                raise


def main(argv=None):
    parser = x5.Arguments(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--template-row-id")
    parser.add_argument("--read-scopes", type=Path)
    args = parser.parse_args(argv)
    # HTTPX 的常规请求日志包含原始 URL。独立入口整个读取期间禁止日志输出。
    previous = logging.root.manager.disable
    logging.disable(sys.maxsize)
    try:
        run(args)
    except Exception:
        return 1
    finally:
        logging.disable(previous)
    return 0


if __name__ == "__main__":
    sys.exit(main())
