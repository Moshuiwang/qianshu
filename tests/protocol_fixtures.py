"""合成固定回包的坐标构造器。"""

import json

from qianshu.parse.protocol import Findings

DURATIONS = ("recharge_days", "gift_days")


def span(start, end, source_id="body"):
    return {"source_id": source_id, "start": start, "end": end}


def statement(status, *, value=None, evidence=(), candidates=()):
    return {"status": status, "reason_code": "fixture", "value": value,
            "evidence": list(evidence), "candidates": list(candidates)}


def quote(body, text, *, occurrence=0):
    start = 0
    for _ in range(occurrence + 1):
        start = body.find(text, start) + 1
        if not start:
            return {"span": span(0, len(text)), "text": text}
    start -= 1
    return {"span": span(start, start + len(text)), "text": text}


def policy(policy_id, main, values=None, *, continuations=(), has_title=True):
    fields = {name: statement("原文未写") for name in Findings.model_fields}
    fields.update(values or {})
    return {"id": policy_id, "main": main, "continuations": list(continuations),
            "has_title": has_title, "fields": fields}


def serialized_empty():
    return json.dumps({"policies": []})


def fixed_reply(body, rows, *, policy_ranges=None):
    ranges = policy_ranges or [(0, len(body))] * len(rows)
    policies = []
    for index, (row, (start, end)) in enumerate(zip(rows, ranges, strict=True), 1):
        assertions = {}
        for name, raw in row.items():
            value = raw.get("value")
            if name in DURATIONS and isinstance(value, int):
                value = {"quantity": value, "unit": raw.get("unit", "天")}
            if value is None and not raw.get("candidates"):
                continue
            items = [quote(body, text) for text in raw.get("evidence", [])]
            candidates = [
                {"value": item["value"], "evidence": [quote(body, text) for text in item["evidence"]]}
                for item in raw.get("candidates", [])
            ]
            assertions[name] = statement("歧义" if candidates else "确定", value=value,
                                         evidence=items, candidates=candidates)
        policies.append(policy(f"p{index}", span(start, end), assertions))
    return {"policies": policies}
