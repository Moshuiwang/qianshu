"""050 日报。stdout 只含固定指标；观察窗口为 UTC 半开区间。"""

import argparse
import fcntl
import json
import os
import re
import secrets
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from uuid import UUID

import httpx

from qianshu.mail.trial_state import BODY_NOTICE_COUNTS_SQL, FORMAT_VERSION, ledger_matches
from qianshu.mingdao.create_order import Official, Stop, creation_state, no_log_continuation, private_bytes, utc


UNKNOWN = "未知"


class Reason(StrEnum):
    ID = "标识格式无法核实"
    RANGE = "回读字段范围无法核实"
    SCOPE = "覆盖范围无独立证据"
    LEDGER = "台账缺失或格式不认识"
    PENDING = "建单结果未确认"
    TEST = "测试登记无法核实"
    CONFLICT = "登记冲突"
    MANIFEST = "字段清单无独立来源"
    SNAPSHOT = "字段快照核对不完整"
    APPROVAL = "建单同意证据不完整"
    READ = "回读未完成"
    COUNTRY = "建单国家归属无法核实"
    CREATOR = "建单身份无法核实"
    LINK = "申请关联核对不完整"
    CARD = "卡片与单据无法唯一核对"
    FIELD = "卡片字段核对不完整"
    HISTORY = "已核历史证据本次未能重证"
    SEAL = "用量账摘要不符"
    USAGE = "调用用量不完整"
    MODEL = "调用模型无法定价"
    CHARGE = "用量归档冲突"
    REJECTIONS = "审批清单无法核对完整"


ERRORS = (OSError, ValueError, TypeError, KeyError, Stop)
EVENTS = {
    "create_reserved", "card_published", "create_attempt", "aborted_before_write",
    "approval_closed", "created", "create_unknown", "create_reconciled_read",
    "create_reconciled_read_unconfirmed", "create_reconciled_candidate",
    "readback_compared", "readback_unconfirmed", "delete_attempt", "delete_unconfirmed", "delete_verified",
}


@contextmanager
def reader(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        yield db


def normalized_id(raw, kind):
    if kind == "registration" and type(raw) is int and raw > 0:
        raw = str(raw)
    if not isinstance(raw, str):
        raise ValueError(Reason.ID)
    value = raw.strip()
    try:
        canonical = str(UUID(value))
        if canonical == value.lower():
            return canonical
    except ValueError:
        pass
    formats = {"registration": r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}",
               "approval": r"[A-Za-z0-9_-]{43}", "message": r"om_[0-9a-fA-F]{32}",
               "business": r"create:[0-9a-f]{64}"}
    if kind in formats and re.fullmatch(formats[kind], value):
        return value
    raise ValueError(Reason.ID)


def optional_id(raw, kind):
    try:
        return normalized_id(raw, kind)
    except ValueError:
        return None


def indexed_metadata(mapping, kind):
    result = {}
    for raw, value in mapping.items():
        key = optional_id(raw, kind)
        if key is not None:
            result[key] = {} if key in result else value
    return result


def readback_complete(row, expected):
    items = row.get("逐项核对")
    if (not expected or type(row.get("total")) is not int or row["total"] != len(expected)
            or not isinstance(items, list) or len(items) != len(expected)
            or not all(isinstance(item, dict) and isinstance(item.get("字段名"), str)
                       and item.get("结果") in ("一致", "不一致") for item in items)):
        return False
    counts = Counter(item["结果"] for item in items)
    return ({item["字段名"] for item in items} == expected and same(row.get("counts"), counts)
            and row.get("结论") == ("写错" if counts["不一致"] else "一致"))


@dataclass(frozen=True)
class Order:
    created: datetime
    countries: frozenset
    wrong: bool
    reasons: frozenset
    card_uuid: str | None
    covered: bool
    fields: dict | None
    written: dict | None


@dataclass
class OrderLedger:
    orders: dict = field(default_factory=dict)
    tests: set = field(default_factory=set)
    gaps: int = 0
    unreadable: bool = False
    pending: list = field(default_factory=list)

    published: list = field(default_factory=list)
    reasons: set = field(default_factory=set)
    conflicts: dict = field(default_factory=dict)

    def possible_attempt(self, rid, stamp, registration):
        reg = optional_id(registration, "registration")
        return self.unreadable or any(
            (begin is None or begin <= stamp) and (target is None or target == rid)
            and (reg is None or reg == owner) for owner, begin, target in self.pending
        )


def read_orders(path, start, end):
    """归并原始事件。每条登记独立保留已确认差异，任何损坏都不能清空已读证据。"""
    context = defaultdict(dict)
    records = {}
    ids = defaultdict(set)
    owners = defaultdict(set)
    result = OrderLedger()
    wrong_readbacks = set()
    test_events = defaultdict(list)
    pending = defaultdict(list)
    gaps = 0
    try:
        lines = private_bytes(path).splitlines()
    except ERRORS:
        return OrderLedger(gaps=1, unreadable=True, reasons={Reason.LEDGER})

    for line in lines:
        try:
            row = json.loads(line)
            stamp = utc(row["utc"])
            if not start <= stamp < end:
                continue
            event, reg, test = row["event"], row["registration"], row["is_test"]
            if event not in EVENTS or not isinstance(reg, str) or not reg.strip() or type(test) is not bool:
                raise ValueError()
            reg = row["registration"] = normalized_id(reg, "registration")
            for key, kind in (("record_id", "record"), ("card_uuid", "card")):
                if row.get(key) is not None:
                    row[key] = normalized_id(row[key], kind)
            identity = (reg, test)
            info = context[identity]
            rid = row.get("record_id")
            if rid is not None and (not isinstance(rid, str) or not rid.strip()):
                raise ValueError()
            if (rid is not None and event in {"create_reserved", "create_attempt", "approval_closed",
                                              "card_published", "create_unknown", "aborted_before_write"}):
                raise ValueError()
            if event == "card_published" and not test:
                result.published.append(row)
            if test:
                test_events[identity].append(row)
            if event == "create_attempt":
                info.pop("expected", None)
                manifest = row.get("request_fields")
                if (isinstance(manifest, list) and manifest and all(isinstance(k, str) and k for k in manifest)
                        and len(set(manifest)) == len(manifest)):
                    info["expected"] = set(manifest)
                if "attempt" in info:
                    info["uncertain"] = True
                info["attempt"] = stamp
            if rid and event in {"readback_compared", "readback_unconfirmed"} and row.get("结论") == "写错":
                wrong_readbacks.add(rid)
            if rid:
                if event == "created":
                    if (identity, rid) in records:
                        records[identity, rid]["uncertain"] = True
                    else:
                        records[identity, rid] = deepcopy(info)
                    ids[reg].add(rid)
                    owners[rid].add(identity)
                    pending.pop(identity, None)
                if (identity, rid) not in records:
                    raise ValueError()
                info = records[identity, rid]
            if event.startswith("delete_") and (not test or not rid):
                raise ValueError()
            if event == "created":
                if not rid:
                    raise ValueError()
                info.setdefault("created", stamp)
            if event in {"create_attempt", "create_unknown", "create_reconciled_candidate",
                         "create_reconciled_read", "create_reconciled_read_unconfirmed"}:
                if event == "create_attempt" or identity not in pending:
                    pending[identity].append((reg, info.get("attempt"), optional_id(row.get("target_id"), "record")))
            if event == "aborted_before_write":
                if type(row.get("allowed_writes")) is not int or row["allowed_writes"] != 0:
                    raise ValueError()
                pending.pop(identity, None)
            if event in {"readback_compared", "readback_unconfirmed"}:
                if not rid:
                    raise ValueError()
                verdict = row.get("结论")
                complete = event == "readback_compared" and readback_complete(row, info.get("expected"))
                info["compared"] = complete
                if complete:
                    info["covered"] = True
                if verdict == "写错":
                    info["wrong"] = True
                info["range_gap"] = event == "readback_compared" and not complete
            if test:
                continue
            for key in ("country", "approved", "written", "displayed", "card_uuid", "fields"):
                if key not in row or event == "create_attempt" and key in {"displayed", "written"}:
                    continue
                if key == "country" and event not in {"created", "create_reserved", "create_attempt", "card_published"}:
                    continue
                if key == "country" and isinstance(row[key], str) and row[key].strip():
                    info.setdefault("countries", set()).add(row[key])
                if key in info and not same(info[key], row[key]):
                    info["uncertain"] = True
                info.setdefault(key, row[key])
                if key == "approved" and row[key] is False:
                    info["wrong"] = True
            if event == "create_attempt":
                continue
            written, displayed = row.get("written", info.get("written")), row.get("displayed", info.get("displayed"))
            if isinstance(written, dict) and isinstance(displayed, dict):
                if any(not same(written[k], displayed[k]) for k in written.keys() & displayed.keys()):
                    info["wrong"] = True
        except ERRORS:
            gaps += 1

    if gaps:
        result.reasons.add(Reason.LEDGER)
    if pending:
        result.reasons.add(Reason.PENDING)
    result.unreadable = bool(gaps)
    result.pending = [attempt for attempts in pending.values() for attempt in attempts]
    result.conflicts = {reg: sorted(rids) for reg, rids in ids.items() if len(rids) > 1}
    if result.conflicts:
        result.reasons.add(Reason.CONFLICT)
    for rid, identities in owners.items():
        formal = [records[i, rid] for i in sorted(identities) if not i[1]]
        if not formal:
            identity = next(iter(identities))
            events = test_events[identity]
            if (len(identities) == 1 and events[0]["event"] == "create_reserved"
                    and creation_state(events) in {"used", "compared"}):
                result.tests.add(rid)
            else:
                result.reasons.add(Reason.TEST)
                gaps += 1
            continue
        info = formal[0]
        uncertain = (len(identities) > 1 or any(reg in result.conflicts for reg, _ in identities)
                     or any(r.get("uncertain") for r in formal))
        reasons = {Reason.CONFLICT} if uncertain else set()
        card = info.get("card_uuid")
        expected = info.get("expected")
        if not info.get("compared"):
            reasons.add(Reason.RANGE if info.get("range_gap") else Reason.READ)
        snapshots = [info.get(k) for k in ("written", "displayed")]
        if not expected:
            reasons.add(Reason.MANIFEST)
        if (not all(isinstance(v, dict) for v in snapshots)
                or snapshots[0].keys() != snapshots[1].keys()
                or expected and expected != snapshots[0].keys()):
            reasons.add(Reason.SNAPSHOT)
        if type(info.get("approved")) is not bool:
            reasons.add(Reason.APPROVAL)
        result.orders[rid] = Order(
            info["created"], frozenset().union(*(r.get("countries", ()) for r in formal)),
            rid in wrong_readbacks or any(r.get("wrong") for r in formal), frozenset(reasons), card,
            bool(info.get("covered")) and not reasons - {Reason.READ, Reason.RANGE},
            info.get("fields"), info.get("written"))
    result.gaps = gaps + len(pending) + len(result.conflicts)
    return result


def same(left, right):
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def field_counts(card, baseline, final):
    """只比较完整且可归属的字段集合。多问比较候选值，改动比较实际写入/人工建单草稿。"""
    if not all(isinstance(x, dict) and x for x in (card, baseline, final)):
        return None
    if baseline.keys() != card.keys() or not card.keys() <= final.keys():
        return None
    changes = certain = questions = 0
    for key, field in card.items():
        if not isinstance(field, dict) or field.get("status") not in {"确定", "需确认"} or "candidate" not in field:
            return None
        changed = not same(baseline[key], final[key])
        changes += changed
        certain += changed and field["status"] == "确定"
        questions += field["status"] == "需确认" and same(field["candidate"], final[key])
    return changes, certain, questions


def history(api, row, spec, end):
    """只认完整日志；当前字段值仅在首次待审批事务就是最后更新时可作历史值。"""
    rid = row["rowId"]
    record = api.record(row)
    result = api._request("logs", rid, params={"pageIndex": 1, "pageSize": 50})
    logs = result["logs"]
    if not no_log_continuation(result) or result.keys() - {"logs", "flag", "lastMark"} or not 0 < len(logs) < api.log_page_limit:
        raise ValueError()
    submitted, returns = [], []
    edits = Counter()
    for log in logs:
        content = log["content"]
        when = utc(content["_createdAt"], platform=True)
        edits[when] += 1
        for change in content["logData"]:
            if change["id"] != api.config["status_control"]:
                continue
            if change.get("isDeleted") or not (change.get("newText") or change.get("newValue")):
                raise ValueError()
            def status(prefix):
                raw = change.get(prefix + "Value")
                label = change.get(prefix + "Text")
                mapping = api.config["status_map"]
                names = {mapping[v] for v in (raw, label) if v in mapping}
                if len(names) == 1:
                    return names.pop()
                if not names and not raw and not label:
                    return None
                raise ValueError()

            old, new = status("old"), status("new")
            if new == "待审批" and old != new:
                submitted.append(when)
            if new == "新建" and old in {"待审批", "待配置"}:
                # 状态回到新建也可能是人工撤回，须有独立退回节点证据。
                if content.get(spec["node_key"]) in spec["return_nodes"]:
                    returns.append((when, content.get(spec["reason_key"]) or "原因未读到，请到明道云查看"))
                elif not content.get(spec["node_key"]):
                    raise ValueError()
    final = None
    if (submitted and min(submitted) == record.updated_at < end
            and edits[record.updated_at] == 1 and record.status == "待审批"):
        final = row
    return final, returns


def normalized_page(result):
    return result | {"rows": [row | {"rowId": normalized_id(row["rowId"], "record")}
                              for row in result["rows"]]}


def linked_rows(api, spec, link):
    """仅检索已登记的精确任务链接，再逐条校验返回的关联。"""
    condition = {"type": "condition", "field": spec["link"], "operator": "eq", "value": link}
    found = set()
    for view in api.config["search"]["view_ids"]:
        def fetch(page):
            result = api._request("search", body={
                "viewId": view, "pageSize": api.page_size, "pageIndex": page,
                "filter": {"type": "group", "logic": "AND", "children": [condition]},
            })
            if "total" not in result and type(result.get("hasMore")) is not bool:
                raise ValueError()
            return normalized_page(result)
        for row in api.page_rows(fetch, api.page_size):
            if row.get(spec["link"]) != link:
                raise ValueError()
            found.add(row["rowId"])
    return found


def scan_bot(api, scope, account, trial, end):
    """先产出可见记录，分页完成后再与独立的整表匹配数核对。"""
    if (not isinstance(scope, dict) or scope.get("coverage") != "full_table"
            or scope.get("view") not in api.config["search"]["view_ids"]):
        raise ValueError()
    filters = [dict(type="condition", field=key, operator=op, value=value)
               for key, op, value in (("_createdBy", "eq", account), ("_createdAt", "gte", trial.isoformat()),
                                      ("_createdAt", "lt", end.isoformat()))]

    snapshot = None
    matched_count = None
    observed = 0

    def fetch(page):
        nonlocal snapshot, matched_count
        result = api._request("search", body={"viewId": scope["view"], "pageSize": api.page_size,
                              "pageIndex": page, "filter": dict(type="group", logic="AND", children=filters)})
        result = dict(result)
        # 仅接受服务端同一响应对同一过滤条件的整表计数，普通 total 只是视图计数。
        table_count = result.pop("wholeTableMatchedCount", None)
        if page == 1:
            matched_count = table_count
        elif table_count != matched_count:
            matched_count = None
        if type(result.get("total")) is not int and type(result.get("hasMore")) is not bool:
            raise ValueError()
        if page == 1:
            snapshot = result.get("snapshotVersion")
        elif not snapshot or result.get("snapshotVersion") != snapshot:
            raise ValueError()
        return normalized_page(result)

    for row in api.page_rows(fetch, api.page_size):
        if creator_id(row) != account or not trial <= utc(row["_createdAt"], platform=True) < end:
            raise ValueError()
        observed += 1
        yield row["rowId"]
    if type(matched_count) is not int or matched_count != observed:
        raise ValueError()


def creator_id(row):
    creator = row.get("_createdBy")
    if isinstance(creator, dict):
        creator = creator.get("accountId")
    return creator if isinstance(creator, str) and creator else None


DAY = timedelta(days=1)
RATES = {"sonnet": (2000, 10000), "haiku": (100, 500)}


def check_window(trial, start, end):
    if (trial.time() != datetime.min.time() or not trial <= start < end <= trial + 14 * DAY
            or (start - trial) % DAY or end - start != DAY and (start, end) != (trial, trial + 14 * DAY)):
        raise ValueError("日报窗口无效")


def locations(directory, start, whole):
    slot = "summary" if whole else str(start.date())
    name = "trial-summary.json" if whole else f"trial-report-{slot}.json"
    return directory / name, directory / "reports" / slot / name


@dataclass
class Evidence:
    rejected: set = field(default_factory=set)
    charges: dict = field(default_factory=dict)

    reasons: dict = field(default_factory=lambda: defaultdict(set))
    disputed_models: set = field(default_factory=set)

    def merge(self, rejected, charges, disputes):
        if not isinstance(rejected, list):
            raise ValueError("驳回归档无效")
        for raw in rejected:
            try:
                self.rejected.add(normalized_id(raw, "approval"))
            except ValueError:
                self.reasons["卡片驳回"].add(Reason.ID)
        if not isinstance(disputes, list) or any(m not in RATES for m in disputes):
            raise ValueError("费用归档无效")
        self.disputed_models.update(disputes)
        if not isinstance(charges, dict):
            raise ValueError()
        for call, amounts in charges.items():
            if not isinstance(amounts, dict) or not amounts or amounts.keys() - RATES.keys():
                raise ValueError()
            saved = self.charges.setdefault(call, {})
            if saved and saved != amounts:
                self.disputed_models.update(saved.keys() | amounts.keys())
            for model, amount in amounts.items():
                if type(amount) is not int or amount < 0:
                    raise ValueError()
                saved[model] = max(saved.get(model, 0), amount)

    def amounts(self):
        def dollars(value):
            return format(Decimal(value).scaleb(-9), "f")
        totals = {m + "费用美元已核": dollars(sum(row.get(m, 0) for row in self.charges.values())) for m in RATES}
        totals["总费用美元已核"] = dollars(sum(max(row.values()) for row in self.charges.values()))
        return totals


def archive_windows(directory, trial, end):
    day = trial
    while day < end:
        for path in locations(directory, day, False):
            yield day, day + DAY, path
        day += DAY


def previous_errors(directory, trial, start, end, evidence):
    earlier, confirmed, historical = set(), set(), set()
    windows = list(archive_windows(directory, trial, end))
    if end - start == 14 * DAY:
        windows.extend((start, end, p) for p in locations(directory, start, True))
    for day, until, path in windows:
        try:
            previous = json.loads(private_bytes(path))
        except FileNotFoundError:
            continue
        if (not isinstance(previous, dict) or previous.get("试运行起点") != trial.isoformat()
                or previous.get("窗口起点") != day.isoformat() or previous.get("窗口终点") != until.isoformat()):
            raise ValueError("历史日报窗口无法核对")
        ids, old = previous["写错"], previous["新发现的历史异常"]
        if not all(isinstance(items, list) for items in (ids, old)):
            raise ValueError("历史日报异常记录无法核对")
        normalized = []
        for items in (ids, old):
            known = set()
            for raw in items:
                try:
                    known.add(normalized_id(raw, "record"))
                except ValueError:
                    evidence.reasons["明道云写错"].add(Reason.ID)
            normalized.append(known)
        ids, old = normalized
        if not old <= ids:
            raise ValueError("历史日报异常记录无法核对")
        if day < start:
            earlier.update(ids)
        else:
            confirmed.update(ids)
            historical.update(old)
            evidence.merge(previous.get("已核驳回", []), previous.get("已核用量", {}), previous.get("费用证据冲突", []))
    return earlier, confirmed, historical


def charge_counts(rows, evidence=None):
    evidence = evidence if evidence is not None else Evidence()
    missing = Counter()
    for index, row in enumerate(rows):
        model = row["model"]
        if model not in RATES:
            missing["other"] += 1
            continue
        i, o = row["inputs"], row["outputs"]
        if not all(type(n) is int and n >= 0 for n in (i, o)):
            missing[model] += 1
            continue
        a, b = RATES[model]
        key = str(row["id"]) if "id" in row.keys() else str(index)
        evidence.charges[key] = {model: i * a + o * b}
    amounts = evidence.amounts()
    result = dict(amounts)
    for model in RATES:
        result[model + "费用美元"] = UNKNOWN if missing[model] or missing["other"] else amounts[model + "费用美元已核"]
        result[model + "用量未知调用"] = missing[model]
    result["费用未知模型调用"] = missing["other"]
    result["总费用美元"] = UNKNOWN if any(missing.values()) else amounts["总费用美元已核"]
    for model in RATES:
        if missing[model]:
            evidence.reasons[model + "费用美元"].add(Reason.USAGE)
            evidence.reasons["总费用美元"].add(Reason.USAGE)
    if missing["other"]:
        for label in (*RATES, "总"):
            evidence.reasons[label + "费用美元"].add(Reason.MODEL)
    return result


def rejection_counts(directory, config, cards, published, start, end, evidence, ledger_ok):
    sent = {c["uuid"] for c in cards if c["status"] == "sent"}
    complete = (ledger_ok and all(c["status"] in {"sent", "pending"} and c["uuid"] is not None for c in cards)
                and len({c["uuid"] for c in cards}) == len(cards))
    bindings = set()
    businesses = set()
    matched = set()
    try:
        with reader(directory / config["entry_db"]) as db:
            by_approval = defaultdict(list)
            by_message = defaultdict(list)
            for row in db.execute("SELECT approval_id,message_id,record_id,status,decided_at "
                                  "FROM feishu_approvals WHERE scope=?", (config["entry_scope"],)):
                try:
                    approval = normalized_id(row["approval_id"], "approval")
                    message = normalized_id(row["message_id"], "message")
                    business = normalized_id(row["record_id"], "business")
                except ValueError:
                    complete = False
                    continue
                by_approval[approval].append((message, business, row["status"], row["decided_at"]))
                by_message[message].append(approval)
            for event in published:
                try:
                    card = normalized_id(event.get("card_uuid"), "card")
                    approval = normalized_id(event.get("approval_id"), "approval")
                    message = normalized_id(event.get("message_id"), "message")
                    business = normalized_id(event.get("business_id"), "business")
                except ValueError:
                    complete = False
                    continue
                proof = config.get("cards", {}).get(card, {})
                expected_business = optional_id(proof.get("business_id", card), "business")
                rows = by_approval[approval]
                if (card not in sent or len(rows) != 1 or by_message[message] != [approval]
                        or business != expected_business or rows[0][:2] != (message, business)):
                    complete = False
                    continue
                complete &= approval not in bindings and card not in matched and business not in businesses
                businesses.add(business)
                matched.add(card)
                bindings.add(approval)
                _, _, status, at = rows[0]
                if status == "reject":
                    if type(at) not in (int, float):
                        complete = False
                    elif start.timestamp() <= at < end.timestamp():
                        evidence.rejected.add(approval)
                elif status not in {"pending", "approve", "closed", "claimed", "used"}:
                    complete = False
        declared = config.get("approval_ids")
        complete &= (bool(bindings) and isinstance(declared, list) and len(declared) == len(bindings)
                     and {normalized_id(v, "approval") for v in declared} == bindings and matched == sent)
    except (sqlite3.Error, KeyError, TypeError, OSError, ValueError):
        complete = False
    return len(evidence.rejected) if complete else UNKNOWN


def recommendation(wrong, uncertain, applications, cards, slight, unscoped):
    if wrong:
        return "回退"
    if not uncertain and applications >= 3 and cards >= 10 and slight * 2 > cards + unscoped:
        return "扩大"
    return "维持"


def local_counts(directory, config, trial, start, end, published, ledger_ok):
    evidence = Evidence()
    def inside(value):
        return start <= utc(value) < end

    def observed(value):
        return trial <= utc(value) < end

    with reader(directory / "trial-050.sqlite3") as db:
        if db.execute("PRAGMA user_version").fetchone()[0] != FORMAT_VERSION:
            raise ValueError("登记库版本未知")
        registrations = {}
        for row in db.execute("SELECT * FROM registration"):
            if observed(row["created"]):
                reg = normalized_id(row["id"], "registration")
                registrations[reg] = dict(row, id=reg)
        cards = [dict(row, uuid=optional_id(row["uuid"], "card"),
                      registration=optional_id(row["registration"], "registration"))
                 for row in db.execute("SELECT * FROM card_intent") if observed(row["at"])]
        notices = list(db.execute("SELECT kind,status,at,registration FROM notice"))
        interruptions = {normalized_id(r["registration"], "registration"): r["at"] for r in notices
                         if r["kind"] == "interrupted" and optional_id(r["registration"], "registration") is not None}
        notices = [r for r in notices if inside(r["at"])]
        waiting, review = db.execute(BODY_NOTICE_COUNTS_SQL).fetchone()
        if ledger_matches(db):
            costs = charge_counts([r for r in db.execute("SELECT id,model,at,inputs,outputs FROM charge")
                                   if inside(r["at"])], evidence)
        else:
            costs = dict.fromkeys(charge_counts([]), UNKNOWN)
            for key in costs:
                evidence.reasons[key].add(Reason.SEAL)
    today = [r for r in registrations.values() if inside(r["created"])]
    apps = {n for n, r in registrations.items() if r["verdict"] in {"是", "拿不准"}}
    meta = config.get("registrations", {})
    build = config["build_country"]
    if not isinstance(build, str) or not build:
        raise ValueError()
    def country(reg):
        return meta.get(reg, {}).get("country")

    scope = {r["id"] for r in today if r["id"] in apps}
    sent = [c for c in cards if c["status"] == "sent"]
    daily = [c for c in sent if inside(c["at"])]
    public = {
        "收到申请": len(scope), "申请判定未知": sum(r["verdict"] not in {"是", "否", "拿不准"} for r in today),
        "建单国家申请": sum(country(n) == build for n in scope),
        "申请国家未知": sum(not country(n) for n in scope),
        "出卡": len(daily), "建单国家卡片": sum(country(c["registration"]) == build for c in daily),
        "卡片国家未知": sum(not country(c["registration"]) for c in daily),
        "卡片送达未知": sum(c["status"] == "unknown" and inside(c["at"]) for c in cards),
        "解析中途停止已转人工": sum(
            inside(interruptions.get(n, r["created"])) for n, r in registrations.items()
            if r["reason"] == "correction_interrupted" or r["status"] == "correction_pending"),
        "待发提示": waiting, "需人工核对": review, **costs,
    }
    for verdict in ("是", "否", "拿不准"):
        public["分拣" + verdict] = sum(r["verdict"] == verdict for r in today)
    public["未解析附件批次"] = sum(r["attachments"] != 0 for r in today if r["id"] in apps)
    for status, label in (("pending", "待发"), ("sent", "已发"), ("unknown", "结果未知")):
        public["冻结通知" + label] = sum(r["kind"] == "freeze" and r["status"] == status for r in notices)
    public["卡片驳回"] = rejection_counts(directory, config, cards, published, start, end, evidence, ledger_ok)
    if public["卡片驳回"] == UNKNOWN:
        evidence.reasons["卡片驳回"].add(Reason.REJECTIONS)
    return public, apps, cards, daily, evidence


def report(directory, config, api, start, end):
    config = config | {"registrations": indexed_metadata(config.get("registrations", {}), "registration"),
                       "cards": indexed_metadata(config.get("cards", {}), "card")}
    trial = utc(config["trial_start"])
    check_window(trial, start, end)
    ledger = read_orders(directory / "trial-orders.jsonl", trial, end)
    public, apps, cards, daily, evidence = local_counts(
        directory, config, trial, start, end, ledger.published, ledger.gaps == 0)
    meta = config.get("registrations", {})
    build = config["build_country"]

    def country(reg):
        return meta.get(reg, {}).get("country")

    if not isinstance(config["bot_account"], str) or not config["bot_account"]:
        raise ValueError("机器人账号未登记")
    spec = config["controls"]
    proofs = config.get("cards", {})
    links = {meta.get(n, {}).get("link") for n in apps}
    failed_links = {link for link in links if not link}
    gaps = sum(not meta.get(n, {}).get("link") for n in apps)
    record_links = defaultdict(set)
    build_links = {meta.get(n, {}).get("link") for n in apps if country(n) in (None, "", build)}
    build_gaps = sum(not meta.get(n, {}).get("link") for n in apps if country(n) in (None, "", build))
    records = set(ledger.orders)
    scanned = set()
    scan_gap = 0
    try:
        scanned.update(scan_bot(api, config.get("bot_scan"), config["bot_account"], trial, end))
    except ERRORS:
        scan_gap = 1
    records.update(scanned)
    reasons = evidence.reasons
    reasons["卡片驳回"].update(ledger.reasons)
    current_rejected = evidence.rejected.copy()
    current_amounts = evidence.amounts()
    seen, archived, historical = previous_errors(directory, trial, start, end, evidence)
    public["卡片驳回已核"] = len(evidence.rejected)
    if Reason.ID in reasons["卡片驳回"]:
        public["卡片驳回"] = UNKNOWN
    if current_rejected != evidence.rejected:
        public["卡片驳回"] = UNKNOWN
        reasons["卡片驳回"].add(Reason.HISTORY)
    amounts = evidence.amounts()
    public.update(amounts)
    for label in (*RATES, "总"):
        if current_amounts[label + "费用美元已核"] != amounts[label + "费用美元已核"]:
            reasons[label + "费用美元"].add(Reason.HISTORY)
            public[label + "费用美元"] = UNKNOWN
        if label in evidence.disputed_models or label == "总" and evidence.disputed_models:
            reasons[label + "费用美元"].add(Reason.CHARGE)
            public[label + "费用美元"] = UNKNOWN
    wrong = set()
    for link in filter(None, links):
        try:
            for rid in linked_rows(api, spec, link):
                record_links[rid].add(link)
                records.add(rid)
        except ERRORS:
            failed_links.add(link)
            build_gaps += link in build_links
            gaps += 1
    differences = {}
    missed, created = set(), set()
    create_reasons = set(ledger.reasons)
    if build_gaps:
        create_reasons.add(Reason.LINK)
    if scan_gap:
        create_reasons.add(Reason.SCOPE)
    write_reasons = create_reasons.copy()
    miss_reasons = {Reason.LINK} if gaps else set()
    card_reasons = set()
    unknown_writes = build_gaps + ledger.gaps + scan_gap
    if Reason.ID in reasons["明道云写错"]:
        unknown_writes += 1
        write_reasons.add(Reason.ID)
    unknown_creates = build_gaps + ledger.gaps + scan_gap
    unknown_misses = gaps
    observations, announcements = defaultdict(list), []
    unknown_returns = build_gaps + ledger.gaps + scan_gap
    policy_cards = defaultdict(list)
    incomplete_links = set()
    for c in cards:
        link = meta.get(c["registration"], {}).get("link")
        policy = proofs.get(c["uuid"], {}).get("policy")
        if c["status"] == "sent" and policy:
            policy_cards[link, policy].append(c["uuid"])
        elif c["status"] != "pending":
            incomplete_links.add(link)
    for rid in sorted(records):
        order = ledger.orders.get(rid)
        if rid in ledger.tests:
            continue
        row = {}
        try:
            raw = api.read(rid)
            row = api.detail(raw | {"rowId": normalized_id(raw["rowId"], "record")}, rid)
            stamp = utc(row["_createdAt"], platform=True)
            if order is None:
                if rid in scanned and (creator_id(row) != config["bot_account"] or not trial <= stamp < end):
                    raise ValueError()
                if rid not in scanned and row.get(spec["link"]) not in record_links[rid]:
                    raise ValueError()
                if not trial <= stamp < end:
                    continue
        except ERRORS:
            failed_links.update(record_links[rid])
            if order is None:
                unknown_misses += 1
                miss_reasons.add(Reason.READ)
                if rid in scanned or record_links[rid] & build_links:
                    unknown_writes += 1
                    create_reasons.add(Reason.READ)
                    write_reasons.add(Reason.READ)
                    unknown_creates += 1
                    unknown_returns += 1
                continue
            row = {}
        current = row.get(spec["country"])
        where = current if isinstance(current, str) and current else None
        if order is not None:
            where = build if build in order.countries else next(iter(order.countries), None)
            stamp = order.created
            issues = []
            if order.reasons:
                write_reasons.update(order.reasons)
                issues.append("台账身份或字段证据待核对")
            if len(order.countries) != 1 or current != where:
                write_reasons.add(Reason.COUNTRY)
                if not row:
                    write_reasons.add(Reason.READ)
                issues.append("国家归属待核对")
            if row and creator_id(row) != config["bot_account"]:
                write_reasons.add(Reason.CREATOR)
                issues.append("建单身份待核对")
            if issues:
                differences[rid] = issues
                unknown_writes += 1
        in_period = start <= stamp < end
        creator = creator_id(row)
        bot = creator == config["bot_account"]
        if order is None and bot:
            # 台账外记录只有在详情仍为创建时版本时才能从详情证明最初归属。
            try:
                creation_version = utc(row["_updatedAt"], platform=True) == stamp
            except ERRORS:
                creation_version = False
            if not creation_version:
                where = None
                differences[rid] = ["台账外记录缺少创建时国家证据"]
        if bot and rid not in scanned and not scan_gap:
            scan_gap = 1
            create_reasons.add(Reason.SCOPE)
            write_reasons.add(Reason.SCOPE)
            unknown_writes += 1
            unknown_creates += 1
            unknown_returns += 1
        if in_period and (not where or where == build and not creator and order is None):
            create_reasons.add(Reason.COUNTRY if not where else Reason.CREATOR)
            unknown_creates += 1
        if where == build and (order is not None or bot):
            if in_period:
                created.add(rid)
            if order is None and ledger.possible_attempt(rid, stamp, row.get("registration")):
                unknown_writes += 1
            elif (order is None or order.wrong) and (in_period or rid not in seen):
                wrong.add(rid)
                if not in_period:
                    historical.add(rid)
        elif order is None and (not where or where == build and not creator):
            write_reasons.add(Reason.COUNTRY if not where else Reason.CREATOR)
            unknown_writes += 1
        if not row:
            unknown_returns += int(order is not None and where in (None, build))
            continue
        key = (row.get(spec["link"]), row.get(spec["policy"]))
        if any(not isinstance(value, str) for value in key):
            key = (None, None)
        if order is None and not bot and in_period:
            if not creator or not all(key) or key[0] in incomplete_links and not policy_cards[key]:
                if not creator:
                    miss_reasons.add(Reason.CREATOR)
                if not all(key) or key[0] in incomplete_links and not policy_cards[key]:
                    miss_reasons.add(Reason.CARD)
                unknown_misses += 1
            elif not policy_cards[key]:
                missed.add(rid)
        try:
            final, returned = history(api, row, spec, end)
            if where == build and (order is not None or bot):
                for at, reason in returned:
                    if start <= at < end:
                        announcements.append(dict(record_id=rid, item=api.record(row).name, reason=reason,
                                                  utc=at.isoformat(), notify="配置负责人"))
        except ERRORS:
            final = None
            unknown_returns += int(where in (None, build) and (order is not None or bot))
        observations[key].append((order, final, where, creator, bot, in_period))
    for rid in archived - wrong:
        write_reasons.add(Reason.HISTORY)
        differences.setdefault(rid, []).append("已确认写错，本次未能重证")
        unknown_writes += 1
    wrong.update(archived)
    for key, matches in observations.items():
        if policy_cards[key] and len(matches) > 1:
            ambiguous = sum(order is None and not bot and bool(creator) and in_period
                                  for order, _, _, creator, bot, in_period in matches)

            if ambiguous:
                unknown_misses += ambiguous
                miss_reasons.add(Reason.CARD)

    totals = Counter()
    details = []
    for c in daily:
        proof = proofs.get(c["uuid"], {})
        key = (meta.get(c["registration"], {}).get("link"), proof.get("policy"))
        rows = observations.get(key, [])
        counts = None
        if (all(key) and key[0] not in failed_links and key[0] not in incomplete_links
                and len(rows) == len(policy_cards[key]) == 1):
            order, final, where, creator, bot, _ = rows[0]
            fields = proof.get("fields")
            if order is not None and order.card_uuid is not None:
                fields = (order.fields or fields) if order.card_uuid == c["uuid"] else None
            baseline = order.written if order is not None else proof.get("draft")
            if where == country(c["registration"]) and (
                    order is not None or (where and where != build and creator and not bot)):
                counts = field_counts(fields, baseline, final)
                if counts is None:
                    if final is None:
                        card_reasons.add(Reason.READ)
                    if final is not None or not fields or not baseline:
                        card_reasons.add(Reason.FIELD)
            else:
                card_reasons.add(Reason.CARD)
        else:
            if not key[0] or key[0] in failed_links:
                card_reasons.add(Reason.LINK)
            if not all(key) or key[0] in incomplete_links or len(rows) != 1 or len(policy_cards[key]) != 1:
                card_reasons.add(Reason.CARD)
        details.append(dict(card=c["uuid"], counts=counts))
        if counts is None:
            totals["未知"] += 1
        else:
            for label, value in zip(("人工改动项", "错判确定", "多问"), counts):
                totals[label] += value
            totals["无需修改"] += counts[0] == 0
            totals["至多两处"] += counts[0] <= 2
            if country(c["registration"]) == build and counts[0] <= 2:
                totals["建单国家至多两处"] += 1
    for k in ("人工改动项", "错判确定", "多问", "无需修改", "至多两处"):
        reasons[k].update(card_reasons)
        public[k + "已核"] = totals[k]
        public[k] = UNKNOWN if totals["未知"] else totals[k]
    public.update({"卡片核对未知": totals["未知"], "漏条已核": len(missed), "漏条未知": unknown_misses,
                   "漏条": UNKNOWN if unknown_misses else len(missed),
                   "建单已核": len(created), "新发现的历史异常已核": len(historical),
                   "新发现的历史异常": UNKNOWN if unknown_writes else len(historical),
                   "明道云写错已核": len(wrong), "写入核对未知": unknown_writes,
                   "明道云写错": UNKNOWN if unknown_writes else len(wrong),
                   "建单": UNKNOWN if unknown_creates else len(created),
                   "退回公告条目": len(announcements), "退回核对未知": unknown_returns})
    reasons["建单"].update(create_reasons)
    reasons["明道云写错"].update(write_reasons)
    reasons["新发现的历史异常"].update(write_reasons)
    reasons["漏条"].update(miss_reasons)

    period_orders = [order for order in ledger.orders.values() if start <= order.created < end]
    coverage = {}
    for prefix in ("", "建单国家"):
        eligible = [o for o in period_orders if not prefix or o.countries == {build}]
        for label, verified in (("已回读核对单数", True), ("机器人建单台账总单数", False)):
            key = prefix + label
            if Reason.LEDGER in ledger.reasons:
                reasons[key].add(Reason.LEDGER)
            if Reason.TEST in ledger.reasons:
                reasons[key].add(Reason.TEST)
            if prefix and any((not o.countries or build in o.countries and len(o.countries) > 1)
                              and (not verified or o.covered) for o in period_orders):
                reasons[key].add(Reason.COUNTRY)
            coverage[key] = UNKNOWN if reasons[key] else sum(not verified or o.covered for o in eligible)
    public.update(coverage)
    private_metrics = dict(coverage)
    for key, value in list(public.items()):
        if value == UNKNOWN:
            phrases = sorted(reasons[key])
            public[key + "原因"] = phrases
            private_metrics.update({key: value, key + "原因": phrases})
    advice = recommendation(wrong, unknown_writes or unknown_creates, public["建单国家申请"],
                            public["建单国家卡片"], totals["建单国家至多两处"],
                            public["卡片国家未知"] + public["卡片送达未知"])
    return public, {"指标": private_metrics, "建议": advice, "未知卡片数": totals["未知"], "卡片样本数": len(daily), "卡片": details, "退回公告": announcements,
                    "试运行起点": trial.isoformat(), "窗口起点": start.isoformat(),
                    "窗口终点": end.isoformat(), "写入差异核对": differences, "登记冲突": ledger.conflicts,
                    "已核用量": evidence.charges, "费用证据冲突": sorted(evidence.disputed_models),
                    "已核驳回": sorted(evidence.rejected),
                    "新发现的历史异常": sorted(historical), "漏条": sorted(missed), "写错": sorted(wrong)}


def isolated_target(directory, config, trial, start, whole, temporary=None):
    _, target = locations(directory, start, whole)
    dest = target.parent
    if dest.resolve() != dest or target.is_symlink():
        raise ValueError("私有输出路径冲突")
    for folder in (dest.parent, dest):
        if folder.exists() and (not folder.is_dir() or folder.stat().st_mode & 0o077):
            raise ValueError("输出目录须独立且私有")
    if dest.exists() and any(child not in (target, temporary) for child in dest.iterdir()):
        raise ValueError("窗口目录含有其他文件")
    sources = [directory / name for name in ("trial-report.json", "trial-orders.jsonl")]
    for database in (directory / config["entry_db"], directory / "trial-050.sqlite3"):
        for base in {database, database.resolve()}:
            sources.extend(Path(str(base) + suffix) for suffix in ("", "-wal", "-shm", "-journal"))

    def collision(left, right):
        return (left.resolve() == right.resolve()
                or left.exists() and right.exists() and left.samefile(right))

    archives = [p for _, _, p in archive_windows(directory, trial, trial + 14 * DAY)]
    archives.extend(locations(directory, start, True))
    for path in archives:
        if path.is_symlink() or any(collision(path, source) for source in sources):
            raise ValueError("归档路径与输入冲突")
    for source in sources + [p for p in archives if p != target]:
        if (source.resolve().is_relative_to(dest) or collision(target, source)
                or source == dest or dest in source.parents):
            raise ValueError("输出目录与输入冲突")
    return target, {path: file_identity(path) for path in set(sources + archives)}


def file_identity(path, **kwargs):
    try:
        stat = os.stat(path, **kwargs)
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino


@contextmanager
def output_transaction(directory, config, trial, start, whole):
    args = directory, config, trial, start, whole
    target, inputs = isolated_target(*args)
    dest = target.parent
    original_dirs = {d: file_identity(d) for d in (directory, dest.parent, dest)}
    dest.parent.mkdir(mode=0o700, exist_ok=True)
    dest.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(dest, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        stat = os.fstat(fd)
        pinned = stat.st_dev, stat.st_ino
        dirs = {d: file_identity(d) for d in original_dirs}
        if any(before is not None and before != dirs[d] for d, before in original_dirs.items()):
            raise ValueError("私有输出身份变化")

        def verify(temporary=None):
            _, current = isolated_target(*args, temporary=temporary)
            if (any(file_identity(d) != identity for d, identity in dirs.items()) or dirs[dest] != pinned
                    or current != inputs or pinned in inputs.values()
                    or file_identity(target.name, dir_fd=fd, follow_symlinks=False) != inputs[target]):
                raise ValueError("私有输出身份变化")

        def publish(private):
            verify()
            name = ".report-" + secrets.token_hex(16)
            out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
            try:
                with os.fdopen(out, "w", encoding="utf8") as stream:
                    json.dump(private, stream, ensure_ascii=False)
                    stream.flush()
                    verify(dest / name)
                    if not os.path.samestat(os.fstat(stream.fileno()), os.stat(name, dir_fd=fd, follow_symlinks=False)):
                        raise ValueError("私有输出身份变化")
                    os.replace(name, target.name, src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                try:
                    os.unlink(name, dir_fd=fd)
                except FileNotFoundError:
                    pass

        verify()
        yield publish
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--start", help="UTC 窗口起点")
    parser.add_argument("--end", help="UTC 窗口终点（默认最近已结束的试运行日）")
    parser.add_argument("--whole-trial", action="store_true", help="按完整两周重算")
    args = parser.parse_args()
    directory = args.private_dir.resolve()
    if directory.stat().st_mode & 0o077:
        raise ValueError("须使用私有目录")
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        generate(directory, args)
    finally:
        os.close(fd)


def generate(directory, args):
    config = json.loads(private_bytes(directory / "trial-report.json"))
    trial = utc(config["trial_start"])
    limit = trial + timedelta(days=14)
    now = datetime.now(UTC)
    end = utc(args.end) if args.end else min(trial + timedelta(days=(now - trial).days), limit)
    start = trial if args.whole_trial else max(trial, end - timedelta(days=1))
    if args.start is not None:
        start = utc(args.start)
    check_window(trial, start, end)
    if not args.whole_trial and end - start != DAY:
        raise ValueError("日报须为一个完整日")
    if end > now or args.whole_trial and end != limit:
        raise ValueError("观察窗口尚未结束")
    with output_transaction(directory, config, trial, start, args.whole_trial) as publish:
        requests = 0
        def count(_request):
            nonlocal requests
            requests += 1
        spec = config["official"]
        with httpx.Client(timeout=10, event_hooks={"request": [count]}) as client:
            api = Official(spec, os.environ[spec["token_key"]], os.environ[spec["app_id_key"]], client)
            public, private = report(directory, config, api, start, end)
        public["明道云只读请求数"] = requests
        publish(private)
    print(json.dumps(public, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (*ERRORS, sqlite3.Error):
        raise SystemExit("日报生成失败，请核对私有配置与数据源") from None
