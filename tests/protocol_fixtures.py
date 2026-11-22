"""合成固定回包的坐标构造器。测试提供每个值、归属和用途。"""

import json

from qianshu.parse.protocol import Findings

DURATIONS = ("recharge_days", "gift_days")


def span(start, end, source_id="body"):
    return {"source_id": source_id, "start": start, "end": end}


def statement(status, *, value=None, evidence=(), candidates=(), checked=()):
    return {"status": status, "reason_code": "fixture", "value": value, "evidence": list(evidence),
            "candidates": list(candidates), "checked": list(checked)}


def quote(body, text, clause_id, *, occurrence=0):
    start = 0
    for _ in range(occurrence + 1):
        start = body.find(text, start) + 1
        if not start:
            return {"clause_id": clause_id, "span": span(0, len(text)), "text": text}
    start -= 1
    return {"clause_id": clause_id, "span": span(start, start + len(text)), "text": text}


def clause(clause_id, locations, fields, policy_ids, *, kind="正文", scope="局部",
           status="确定", disposition="生效", date=None):
    return {"id": clause_id, "kind": kind, "spans": locations, "field_ids": list(fields),
            "status": status, "disposition": disposition, "scope": scope,
            "policy_ids": list(policy_ids), "date": date}


def date_use(role, *, usable=True, targets=None):
    return {"role": role, "usable": usable, "targets": targets}


def policy(policy_id, main, values=None, *, continuations=(), shared=(), checked=None, has_title=True):
    ranges = [main, *continuations]
    fields = {name: statement("原文未写", checked=ranges if checked is None else checked)
              for name in Findings.model_fields}
    fields.update(values or {})
    return {"id": policy_id, "main": main, "continuations": list(continuations),
            "shared_clause_ids": list(shared), "has_title": has_title, "fields": fields}


def document(body, policies=(), clauses=(), *, coverage=None, pending=(), status=None):
    if coverage is None:
        coverage = [{"span": span(0, len(body)), "use": "其他文本"}] if body else []
    return {"policies": list(policies), "clauses": list(clauses), "coverage": coverage,
            "pending": list(pending), "document": {"status": status, "reason_code": "fixture"} if status else None}


def serialized_empty(body):
    return json.dumps(document(body, status="经检查无政策"), ensure_ascii=False)


def fixed_reply(body, rows, *, policy_ranges=None, shared_dates=False, shared_fields=()):
    """为以显式取值及引文维护的旧合成 fixture 填写协议字段。"""
    if not rows:
        return document(body, status="经检查无政策")
    ranges = policy_ranges or [(0, len(body))] * len(rows)
    policies, clauses = [], []
    for index, (raw_row, (start, end)) in enumerate(zip(rows, ranges, strict=True), 1):
        policy_id = f"p{index}"
        main = span(start, end)
        row = dict(raw_row)
        assertions = {}
        shared = []
        for name, raw in row.items():
            value = raw.get("value")
            evidence = raw.get("evidence", [])
            if name in DURATIONS and isinstance(value, int):
                value = {"quantity": value, "unit": raw.get("unit", "天")}
            if value is None and not raw.get("candidates"):
                continue
            clause_id = f"{policy_id}-{name}"
            items = [quote(body, text, clause_id) for text in evidence]
            candidates = [
                {"value": item["value"], "evidence": [quote(body, text, clause_id) for text in item["evidence"]]}
                for item in raw.get("candidates", [])
            ]
            all_quotes = [*items, *(q for item in candidates for q in item["evidence"])]
            date = (date_use("起日" if name == "start_date" else "止日")
                    if name in ("start_date", "end_date") else None)
            shared_clause = (bool(date) and shared_dates) or name in shared_fields
            if shared_clause:
                shared.append(clause_id)
            clauses.append(clause(
                clause_id, [item["span"] for item in all_quotes] or [main], (name,), (policy_id,),
                scope="共享" if shared_clause else "局部", kind="批次说明" if shared_clause else "正文", date=date,
            ))
            assertions[name] = statement("歧义" if candidates else "确定", value=value,
                                         evidence=items, candidates=candidates)
        policies.append(policy(policy_id, main, assertions, shared=shared))
    return document(body, policies, clauses)
