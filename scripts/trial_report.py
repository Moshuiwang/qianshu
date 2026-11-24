"""050 日报。stdout 只含固定指标；观察窗口为 UTC 半开区间。"""

import argparse
import json
import os
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile

import httpx

from qianshu.mail.trial_state import BODY_NOTICE_COUNTS_SQL, FORMAT_VERSION
from qianshu.mingdao.create_order import Official, Stop, no_log_continuation, private_bytes, utc


UNKNOWN = "未知"
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


def read_orders(path, start, end):
    """#154 唯一适配处。每行 event/utc/registration/is_test；created 带 record_id，
    readback_compared/unconfirmed 带结论。可选 country、written（实际写入字段）、
    approved（有效同意）、displayed（同意展示字段）、card_uuid/fields（卡片字段与候选值）取同一登记号的事件。
    返回正式创建记录及未决请求数。缺失/未知版本不能证明台账外写入。
    """
    orders, pending, context = {}, set(), defaultdict(dict)
    try:
        for line in private_bytes(path).splitlines():
            row = json.loads(line)
            stamp = utc(row["utc"])
            if not start <= stamp < end:
                continue
            event, reg = row["event"], row["registration"]
            if event not in EVENTS or type(row["is_test"]) is not bool or not reg:
                return None, None
            if row["is_test"]:
                if event == "created":
                    orders[row["record_id"]] = {"test": True, "created": stamp}
                continue
            info = context[reg]
            for k in ("country", "written", "approved", "displayed", "card_uuid", "fields"):
                if k in row:
                    if k in info and info[k] != row[k]:
                        return None, None
                    info[k] = row[k]
            if event in {"create_attempt", "create_unknown"}:
                pending.add(reg)
            if event == "aborted_before_write" and row.get("allowed_writes") == 0:
                pending.discard(reg)
            if event == "created":
                rid = row["record_id"]
                if not isinstance(rid, str) or not rid or rid in orders or "created" in info:
                    return None, None
                info["created"] = stamp
                orders[rid] = info
                pending.discard(reg)
            if event in {"readback_compared", "readback_unconfirmed"} and row.get("record_id"):
                if orders.get(row["record_id"]) is not info:
                    return None, None
                if info.get("comparison") != "写错":
                    info["comparison"] = row.get("结论")
                    info["compared_at"] = stamp
        return orders, len(pending)
    except ERRORS:
        return None, None


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
            return result
        for row in api.page_rows(fetch, api.page_size):
            if row.get(spec["link"]) != link:
                raise ValueError()
            found.add(row["rowId"])
    return found


def charge_counts(rows):
    rates = {"sonnet": (2000, 10000), "haiku": (100, 500)}
    result = {}
    for model, (a, b) in rates.items():
        total = missing = 0
        for row in rows:
            if row["model"] != model:
                continue
            i, o = row["inputs"], row["outputs"]
            if all(type(n) is int and n >= 0 for n in (i, o)):
                total += i * a + o * b
            else:
                missing += 1
        result[model + "费用美元"] = UNKNOWN if missing else format(Decimal(total).scaleb(-9), "f")
        result[model + "用量未知调用"] = missing
    result["费用未知模型调用"] = sum(r["model"] not in rates for r in rows)
    result["总费用美元"] = UNKNOWN
    if not any(result[k] for k in ("费用未知模型调用", "sonnet用量未知调用", "haiku用量未知调用")):
        result["总费用美元"] = format(sum(Decimal(result[m + "费用美元"]) for m in rates), "f")
    return result


def recommendation(wrong, uncertain, applications, cards, slight, unscoped):
    if wrong:
        return "回退"
    if not uncertain and applications >= 3 and cards >= 10 and slight * 2 > cards + unscoped:
        return "扩大"
    return "维持"


def local_counts(directory, config, trial, start, end):
    def inside(value):
        return start <= utc(value) < end

    def observed(value):
        return trial <= utc(value) < end

    with reader(directory / "trial-050.sqlite3") as db:
        if db.execute("PRAGMA user_version").fetchone()[0] != FORMAT_VERSION:
            raise ValueError("登记库版本未知")
        registrations = {r["id"]: r for r in db.execute("SELECT * FROM registration") if observed(r["created"])}
        cards = [r for r in db.execute("SELECT * FROM card_intent") if observed(r["at"])]
        notices = list(db.execute("SELECT kind,status,at,registration FROM notice"))
        interruptions = {r["registration"]: r["at"] for r in notices if r["kind"] == "interrupted"}
        notices = [r for r in notices if inside(r["at"])]
        waiting, review = db.execute(BODY_NOTICE_COUNTS_SQL).fetchone()
        costs = charge_counts([r for r in db.execute("SELECT model,at,inputs,outputs FROM charge") if inside(r["at"])])
    today = [r for r in registrations.values() if inside(r["created"])]
    apps = {n for n, r in registrations.items() if r["verdict"] in {"是", "拿不准"}}
    meta = config.get("registrations", {})
    build = config["build_country"]
    if not isinstance(build, str) or not build:
        raise ValueError()
    def country(reg):
        return meta.get(str(reg), {}).get("country")

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
    try:
        with reader(directory / config["entry_db"]) as db:
            public["卡片驳回"] = db.execute("""
                SELECT count(*) FROM feishu_approvals WHERE scope=? AND status='reject'
                AND decided_at>=? AND decided_at<? AND approval_id IN (SELECT value FROM json_each(?))
            """, (config["entry_scope"], start.timestamp(), end.timestamp(),
                  json.dumps(config["approval_ids"]))).fetchone()[0]
    except (sqlite3.Error, KeyError):
        public["卡片驳回"] = UNKNOWN

    return public, apps, cards, daily


def report(directory, config, api, start, end):
    trial = utc(config["trial_start"])
    if not trial <= start < end <= trial + timedelta(days=14):
        raise ValueError("窗口超出试运行")
    public, apps, cards, daily = local_counts(directory, config, trial, start, end)
    meta = config.get("registrations", {})
    build = config["build_country"]

    def country(reg):
        return meta.get(str(reg), {}).get("country")

    orders, pending = read_orders(directory / "trial-orders.jsonl", trial, end)
    if not isinstance(config["bot_account"], str) or not config["bot_account"]:
        raise ValueError("机器人账号未登记")
    spec = config["controls"]
    proofs = config.get("cards", {})
    links = {meta.get(str(n), {}).get("link") for n in apps}
    failed_links = {link for link in links if not link}
    gaps = sum(not meta.get(str(n), {}).get("link") for n in apps)
    record_links = defaultdict(set)
    build_links = {meta.get(str(n), {}).get("link") for n in apps if country(n) in (None, "", build)}
    build_gaps = sum(not meta.get(str(n), {}).get("link") for n in apps if country(n) in (None, "", build))
    records = {rid for rid, order in (orders or {}).items() if not order.get("test")}
    for link in filter(None, links):
        try:
            for rid in linked_rows(api, spec, link):
                record_links[rid].add(link)
                records.add(rid)
        except ERRORS:
            failed_links.add(link)
            build_gaps += link in build_links
            gaps += 1
    known = orders or {}
    wrong, missed, created = set(), set(), set()
    unknown_writes = build_gaps + (pending or 0) + int(orders is None)
    unknown_creates = build_gaps + (pending or 0) + int(orders is None)
    unknown_misses = gaps
    observations, announcements = defaultdict(list), []
    unknown_returns = build_gaps + (pending or 0) + int(orders is None)
    policy_cards = defaultdict(list)
    incomplete_links = set()
    for c in cards:
        link = meta.get(str(c["registration"]), {}).get("link")
        policy = proofs.get(c["uuid"], {}).get("policy")
        if c["status"] == "sent" and policy:
            policy_cards[link, policy].append(c["uuid"])
        elif c["status"] != "pending":
            incomplete_links.add(link)
    for rid in sorted(records):
        order = known.get(rid)
        if order and order.get("test"):
            continue
        row = {}
        try:
            row = api.detail(api.read(rid), rid)
            stamp = utc(row["_createdAt"], platform=True)
            if order is None:
                if row.get(spec["link"]) not in record_links[rid]:
                    raise ValueError()
                if not trial <= stamp < end:
                    continue
        except ERRORS:
            failed_links.update(record_links[rid])
            if order is None:
                unknown_misses += 1
                if record_links[rid] & build_links:
                    unknown_writes += 1
                    unknown_creates += 1
                    unknown_returns += 1
                continue
            row = {}
        where = row.get(spec["country"])
        if not isinstance(where, str) or not where:
            where = (order or {}).get("country")
        if order is not None:
            stamp = order["created"]
        in_period = (order is not None or bool(row)) and start <= stamp < end
        compared_today = order is not None and start <= order.get("compared_at", stamp) < end
        creator = row.get("_createdBy")
        if isinstance(creator, dict):
            creator = creator.get("accountId")
        creator = creator if isinstance(creator, str) and creator else None
        bot = creator is not None and creator == config["bot_account"]
        if in_period and (not where or (where == build and not creator and order is None)):
            unknown_creates += 1
        if where == build and order is not None and in_period:
            created.add(rid)
        if (in_period or compared_today) and where == build and (bot or order is not None):
            if order is None:
                created.add(rid)
                if orders is None or pending:
                    unknown_writes += 1
                else:
                    wrong.add(rid)
            else:
                compared = order.get("comparison")
                written, display = order.get("written"), order.get("displayed")
                comparable = isinstance(written, dict) and written and isinstance(display, dict) and display
                if order.get("approved") is False or compared == "写错" or comparable and not same(written, display):
                    wrong.add(rid)
                elif not (order.get("approved") is True and (comparable or compared == "一致")):
                    unknown_writes += 1
        elif in_period and (not where or where == build and not creator):
            unknown_writes += 1
        if not row:
            unknown_returns += int(order is not None and where in (None, build))
            continue
        key = (row.get(spec["link"]), row.get(spec["policy"]))
        if any(not isinstance(value, str) for value in key):
            key = (None, None)
        if order is None and not bot and in_period:
            if not creator or not all(key) or key[0] in incomplete_links and not policy_cards[key]:
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
    for key, matches in observations.items():
        if policy_cards[key] and len(matches) > 1:
            unknown_misses += sum(order is None and not bot and bool(creator) and in_period
                                  for order, _, _, creator, bot, in_period in matches)

    totals = Counter()
    details = []
    for c in daily:
        proof = proofs.get(c["uuid"], {})
        key = (meta.get(str(c["registration"]), {}).get("link"), proof.get("policy"))
        rows = observations.get(key, [])
        counts = None
        if (all(key) and key[0] not in failed_links and key[0] not in incomplete_links
                and len(rows) == len(policy_cards[key]) == 1):
            order, final, where, creator, bot, _ = rows[0]
            fields = proof.get("fields")
            if order is not None and order.get("card_uuid") is not None:
                fields = order.get("fields", fields) if order["card_uuid"] == c["uuid"] else None
            baseline = order.get("written") if order is not None else proof.get("draft")
            if where == country(c["registration"]) and (
                    order is not None or (where and where != build and creator and not bot)):
                counts = field_counts(fields, baseline, final)
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
        public[k + "已核"] = totals[k]
        public[k] = UNKNOWN if totals["未知"] else totals[k]
    public.update({"卡片核对未知": totals["未知"], "漏条已核": len(missed), "漏条未知": unknown_misses,
                   "漏条": UNKNOWN if unknown_misses else len(missed),
                   "建单已核": len(created), "明道云写错已核": len(wrong), "写入核对未知": unknown_writes,
                   "明道云写错": UNKNOWN if unknown_writes else len(wrong),
                   "建单": UNKNOWN if unknown_creates else len(created),
                   "退回公告条目": len(announcements), "退回核对未知": unknown_returns})
    advice = recommendation(wrong, unknown_writes or unknown_creates, public["建单国家申请"],
                            public["建单国家卡片"], totals["建单国家至多两处"],
                            public["卡片国家未知"] + public["卡片送达未知"])
    return public, {"建议": advice, "未知卡片数": totals["未知"], "卡片样本数": len(daily), "卡片": details, "退回公告": announcements,
                    "漏条": sorted(missed), "写错": sorted(wrong)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--end", help="UTC 窗口终点（默认最近已结束的试运行日）")
    parser.add_argument("--whole-trial", action="store_true", help="按完整两周重算")
    args = parser.parse_args()
    directory = args.private_dir.resolve()
    if directory.stat().st_mode & 0o077:
        raise ValueError("须使用私有目录")
    config = json.loads(private_bytes(directory / "trial-report.json"))
    trial = utc(config["trial_start"])
    limit = trial + timedelta(days=14)
    now = datetime.now(UTC)
    end = utc(args.end) if args.end else min(trial + timedelta(days=(now - trial).days), limit)
    start = trial if args.whole_trial else max(trial, end - timedelta(days=1))
    if end > now or args.whole_trial and end != limit:
        raise ValueError("观察窗口尚未结束")
    requests = 0
    def count(_request):
        nonlocal requests
        requests += 1
    spec = config["official"]
    with httpx.Client(timeout=10, event_hooks={"request": [count]}) as client:
        api = Official(spec, os.environ[spec["token_key"]], os.environ[spec["app_id_key"]], client)
        public, private = report(directory, config, api, start, end)
    public["明道云只读请求数"] = requests
    target = directory / ("trial-summary.json" if args.whole_trial else f"trial-report-{start.date()}.json")
    with NamedTemporaryFile(dir=directory, mode="w", encoding="utf8", delete=False) as stream:
        json.dump(private, stream, ensure_ascii=False)
    os.replace(stream.name, target)
    print(json.dumps(public, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (*ERRORS, sqlite3.Error):
        raise SystemExit("日报生成失败，请核对私有配置与数据源") from None
