"""调用解析入口，对齐原稿段落并生成公开计数和私有明细。"""

from collections import Counter, defaultdict
from dataclasses import fields
from pathlib import Path
from typing import get_origin

from qianshu.draft import DraftField, PolicyDraft
from qianshu.parse import ModelError, ParseError, parse_application
from qianshu.parse.companies import DEFAULT_COMPANIES_FILE, load_company_rules
from qianshu.parse.model import REPLY_FORMAT_ERRORS, ModelReplyError, ParseModel

from .business import COUNT_LABELS, DEFAULT_THRESHOLD, METRIC_LABELS, BusinessScore, pairing_audit
from .compare import BENEFIT_TYPES, EXCLUSIONS, FIELD_LABELS, POLICY_TYPES, STATUSES, compare_field
from .freeze import PACKAGE_STYLES
from .storage import RegressionError, digest, json_bytes

RUN_FILES = ("mismatches.json", "public-report.md", "public-report.json")
RULES = ("countries.txt", "parse-options.tsv", "parse-prompt.md", "parse-rules.md")
ALIGNMENTS = ("matched", "unmatched", "ambiguous", "model_failed", "parse_failed")
DOCUMENT_RESULTS = ("parsed", "model_failed", "parse_failed")
DRAFT_RESULTS = ("unplaced", "unselected", "extra", "unclassified", "ambiguous")
APPROVAL_FIELDS = ("approval_url", "approval_attachments")
NEW_FIELD_RESULTS = ("determined", "uncertain", "missing", "unaligned")


class IncompleteReply(ParseError):
    """已收到文本，回复未完整结束。"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__("回归模型回复不完整。")


def _overlaps(left, right) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def _contains(outer, inner) -> bool:
    return outer[0] <= inner[0] and inner[1] <= outer[1]


def _confirmed_targets(draft, targets: list[dict], body: str) -> list[str]:
    span = draft.source_span
    if len(targets) == 1 and _contains(targets[0]["span"], span):
        return [targets[0]["id"]]
    evidence = {item for item in draft.source_evidence_spans if _overlaps(span, item)}
    if not evidence:
        return []
    excerpts = (
        excerpt
        for field in fields(draft)
        if isinstance(value := getattr(draft, field.name), DraftField)
        for excerpt in value.evidence
    )
    for excerpt in excerpts:
        start = excerpt.start
        if start is None:
            # 用首末位置检查重复摘录是否全部落在同一审定段落。
            # 仅出现在共享范围的摘录沿用解析器的共享规则。
            start = body.find(excerpt.text, *span)
            if start < 0:
                continue
            end = body.rfind(excerpt.text, *span) + len(excerpt.text)
        else:
            end = start + len(excerpt.text)
        if _overlaps(span, (start, end)):
            evidence.add((start, end))
    return [pair["id"] for pair in targets if all(_contains(pair["span"], item) for item in evidence)]


def align(drafts, pairs: list[dict], document: dict) -> tuple[dict, list[dict]]:
    """仅用可信原稿段落对齐。样例和草稿两侧均需唯一。"""
    matches = defaultdict(list)
    disputed = set()
    diagnostics = []
    for position, draft in enumerate(drafts):
        span = draft.source_span
        others = bool(span and any(_overlaps(span, other) for other in document["other_spans"]))
        targets = [pair for pair in pairs if span and _overlaps(span, pair["span"])]
        if targets:
            confirmed = _confirmed_targets(draft, targets, document["body"])
            if len(confirmed) == 1:
                matches[confirmed[0]].append(position)
                continue
            disputed.update(pair["id"] for pair in targets)
            category = "ambiguous"
        elif span is None:
            category = "unplaced"
        elif others:
            category = "unselected"
        else:
            category = "extra" if document["complete"] else "unclassified"
        diagnostics.append({"draft": position, "status": category, "span": span})

    outcomes = {}
    for pair in pairs:
        sample_id = pair["id"]
        positions = matches[sample_id]
        if sample_id in disputed or len(positions) > 1:
            diagnostics.extend(
                {"draft": index, "status": "ambiguous", "span": drafts[index].source_span}
                for index in positions
            )
            outcomes[sample_id] = ("ambiguous", None)
        elif positions:
            outcomes[sample_id] = ("matched", drafts[positions[0]])
        else:
            outcomes[sample_id] = ("unmatched", None)
    return outcomes, diagnostics


def _guard_model(model: ParseModel) -> ParseModel:
    def call(*, system: str, user: str) -> str:
        try:
            reply = model(system=system, user=user)
        except Exception:
            raise ModelError("回归模型调用失败。") from None
        if not isinstance(reply, str):
            raise ModelError("回归模型返回无效。")
        return reply
    return call


def _rate_counts(counts: Counter) -> dict:
    total = sum(counts[status] for status in STATUSES)
    return {
        "denominator": total, **{status: counts[status] for status in STATUSES},
        "match_percent": round(100 * counts["match"] / total, 2) if total else None,
    }


def _new_field_status(draft, name: str) -> str:
    if draft is None:
        return "unaligned"
    field = getattr(draft, name, None)
    if not isinstance(field, DraftField):
        return "missing"
    if field.uncertainty is not None:
        return "uncertain"
    return "determined" if field.value is not None else "missing"


def evaluate(
    frozen: dict, model: ParseModel | None, rules_dir: Path, *, legacy_companies: bool = False,
    threshold: int = DEFAULT_THRESHOLD, recording=None,
) -> tuple[dict, dict]:
    samples = frozen["samples"]
    pairs = defaultdict(list)
    for pair in frozen["pairs"].values():
        pairs[pair["token"]].append(pair)
    denominator = frozen["denominators"]
    eligible = {name: set(field["ids"]) for name, field in denominator["fields"].items()}
    draft_fields = {field.name for field in fields(PolicyDraft) if get_origin(field.type) is DraftField}
    new_fields = sorted((draft_fields | FIELD_LABELS.keys()) - eligible.keys() - set(APPROVAL_FIELDS))
    new_field_counts = {name: Counter() for name in new_fields}
    full = set(denominator["whole_policy"])
    country_groups = {
        label: str(index) for index, label in enumerate(sorted({row["country"] for row in samples.values()}), start=1)
    }
    if recording is None:
        if model is None:
            raise RegressionError("回归解析须提供模型或已保存的回复。")
        model = _guard_model(model)
    rules_hash = digest(json_bytes({name: digest((rules_dir / name).read_bytes()) for name in RULES}))
    field_counts = defaultdict(Counter)
    country_counts = defaultdict(lambda: defaultdict(Counter))
    benefit_counts = defaultdict(lambda: defaultdict(Counter))
    policy_counts = defaultdict(lambda: defaultdict(Counter))
    documents, alignments, draft_counts = Counter(), Counter(), Counter()
    availability = {name: Counter() for name in APPROVAL_FIELDS}
    package_styles = Counter(document.get("package_style", "unreviewed") for document in frozen["documents"].values())
    whole_policy = Counter()
    details = {"country_groups": {group: key for key, group in country_groups.items()}, "samples": [], "documents": []}
    try:
        company_rules = load_company_rules(
            rules_dir, DEFAULT_COMPANIES_FILE, legacy=legacy_companies, companies_text=frozen["companies_text"],
        )
    except (OSError, UnicodeError, ValueError):
        company_rules = None
    company_names = company_rules.names if company_rules is not None else {}
    common_countries = defaultdict(set)
    for country, names in company_names.items():
        for name in names:
            common_countries[name].add(country)
    business = BusinessScore(frozen, threshold, {
        name: next(iter(countries)) for name, countries in common_countries.items() if len(countries) == 1
    }, company_table=company_rules.table if company_rules is not None else None,
        country_names=company_rules.countries if company_rules is not None else None)
    business_details = []
    reply_failures = dict.fromkeys(("reply_parse", "reply_incomplete", "reply_truncated"), 0)
    reply_failed_documents = 0

    for token, document in frozen["documents"].items():
        call = recording.for_document(token) if recording is not None else model
        received = False

        def on_reply(_reply: str) -> None:
            nonlocal received
            received = True

        drafts = []
        reply_failure = "reply_parse"
        failure_code, failure_path = None, ()
        try:
            drafts = parse_application(
                document["body"], model=call, rules_dir=rules_dir, companies_text=frozen["companies_text"],
                legacy_companies=legacy_companies, company_rules=company_rules, on_reply=on_reply,
            )
        except IncompleteReply as error:
            received = True
            status = "parse_failed"
            reply_failure = error.reason
            failure_code = error.reason
        except ModelReplyError as error:
            received = True
            status = "parse_failed"
            if error.code not in REPLY_FORMAT_ERRORS:
                reply_failure = error.code
            failure_code, failure_path = error.code, error.path
        except ModelError:
            status = "model_failed"
        except ParseError as error:
            status = "parse_failed"
            failure_code, failure_path = error.code, error.path
        except Exception:
            if recording is not None and received:
                recording.finish(token, "parse_failed", received)
            raise
        else:
            status = "parsed"
        failure_reasons = set()
        if status == "parse_failed" and received:
            failure_reasons.add(reply_failure)
        if recording is not None and recording.finish(
            token, status, received, code=failure_code, path=failure_path,
        ):
            failure_reasons.add("reply_truncated")
        reply_failed_documents += bool(failure_reasons)
        for reason in failure_reasons:
            reply_failures[reason] = reply_failures.get(reason, 0) + 1
        documents[status] += 1
        business_details.append({"token": token, **business.add(document, pairs[token], drafts)})
        if status == "parsed":
            outcomes, diagnostics = align(drafts, pairs[token], document)
            if diagnostics:
                draft_counts.update(item["status"] for item in diagnostics)
                details["documents"].append({"token": token, "drafts": [
                    {**item, "parsed": drafts[item["draft"]].to_dict()} for item in diagnostics
                ]})
        else:
            outcomes = {pair["id"]: (status, None) for pair in pairs[token]}
            details["documents"].append({"token": token, "status": status})

        for pair in pairs[token]:
            sample_id = pair["id"]
            sample = samples[sample_id]
            alignment, draft = outcomes[sample_id]
            alignments[alignment] += 1
            for name, counts in new_field_counts.items():
                counts[_new_field_status(draft, name)] += 1
            if draft is not None:
                for name, counts in availability.items():
                    field = getattr(draft, name)
                    counts["available" if field.value and field.uncertainty is None else "unavailable"] += 1
            mismatches = {}
            for name, ids in eligible.items():
                if sample_id not in ids:
                    continue
                expected = pair["configuration"][name]["value"]
                result = compare_field(name, expected, getattr(draft, name)) if draft is not None else (
                    "missing" if alignment == "unmatched" else "anomaly"
                )
                field_counts[name][result] += 1
                country_counts[country_groups[sample["country"]]][name][result] += 1
                benefit_counts[sample["benefit_type"]][name][result] += 1
                policy_counts[sample["policy_type"]][name][result] += 1
                if result != "match":
                    mismatches[name] = {"status": result, "expected": expected}
            if sample_id in full:
                whole_policy["error" if mismatches else "match"] += 1
            if mismatches:
                details["samples"].append({
                    "id": sample_id, "row_id": pair["row_id"], "token": token, "span": pair["span"],
                    "alignment": alignment, "fields": mismatches,
                    "parsed": draft.to_dict() if draft is not None else None,
                })

    excluded = {}
    for name, entry in denominator["fields"].items():
        counts = Counter(entry["excluded"].values())
        excluded[name] = {reason: counts[reason] for reason in EXCLUSIONS}
    report = {
        "freeze_sha256": frozen["fingerprint"],
        "rules_sha256": rules_hash,
        "samples": len(samples),
        "strata": frozen["strata"],
        "documents": {key: documents[key] for key in DOCUMENT_RESULTS},
        "sample_alignment": {key: alignments[key] for key in ALIGNMENTS},
        "draft_alignment": {key: draft_counts[key] for key in DRAFT_RESULTS},
        "by_field": {name: _rate_counts(field_counts[name]) for name in eligible},
        "new_field_counts": {
            name: {status: counts[status] for status in NEW_FIELD_RESULTS} for name, counts in new_field_counts.items()
        },
        "package_writing_by_document": {style: package_styles[style] for style in (*PACKAGE_STYLES, "unreviewed")},
        "excluded_by_field": excluded,
        "by_benefit_type": {
            label: {name: _rate_counts(benefit_counts[label][name]) for name in eligible}
            for label in BENEFIT_TYPES if label in benefit_counts
        },
        "by_policy_type": {
            label: {name: _rate_counts(policy_counts[label][name]) for name in eligible}
            for label in POLICY_TYPES if label in policy_counts
        },
        "by_country_group": {
            group: {name: _rate_counts(country_counts[group][name]) for name in eligible}
            for group in country_groups.values()
        },
        "whole_policy": _rate_counts(whole_policy),
        "approval_availability_among_matched": {
            name: {status: counts[status] for status in ("available", "unavailable")}
            for name, counts in availability.items()
        },
    }
    report["business"] = business.report(reply_failed_documents)
    report["business"]["reply_parse_failure_reasons"] = reply_failures
    details["business"] = business_details
    details["pairing_audit"] = pairing_audit(business_details)
    return report, details


def markdown_report(report: dict) -> bytes:
    failures = report["business"]["reply_parse_failure_reasons"]
    breakdown = f"（截断 {failures['reply_truncated']}，其他未完整结束 {failures['reply_incomplete']}，解码或解析 {failures['reply_parse']}）"
    if report.get("void"):
        return (
            "# 解析回归计数\n\n"
            f"- 冻结指纹 `{report['freeze_sha256']}`\n"
            f"- 运行配置指纹 `{report['configuration_sha256']}`\n"
            f"- 模型调用失败 {report['documents']['model_failed']} 篇\n"
            f"- 回复解析失败 {report['business']['counts']['reply_parse_failed']} 篇{breakdown}\n\n"
            "本次放行运行作废。\n"
        ).encode()

    def lines(title, data):
        yield f"## {title}"
        yield ""
        yield "| 分组 | 分母 | 一致 | 错误 | 不确定 | 缺失 | 异常 | 一致率 |"
        yield "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
        for label, counts in data.items():
            rate = counts["match_percent"]
            values = [counts["denominator"], *(counts[key] for key in STATUSES)]
            row = [label, *map(str, values), f"{rate:.2f}%" if rate is not None else "—"]
            yield "| " + " | ".join(row) + " |"
        yield ""

    result = [
        "# 解析回归计数", "",
        f"- 冻结指纹 `{report['freeze_sha256']}`",
        f"- 解析规则指纹 `{report['rules_sha256']}`",
        f"- 样例 {report['samples']} 条",
        f"- 原稿解析完成 {report['documents']['parsed']} 篇，模型调用失败 {report['documents']['model_failed']} 篇，"
        f"回复解析失败 {report['documents']['parse_failed']} 篇",
        f"- 唯一对齐 {report['sample_alignment']['matched']} 条，未对齐 {report['sample_alignment']['unmatched']} 条，"
        f"歧义 {report['sample_alignment']['ambiguous']} 条", "",
        "## 分层配额与缺口", "",
        "| 政策类型 | 优惠类型 | 配额 | 实际纳入 | 缺口 |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for layer in report["strata"]:
        result.append(
            f"| {layer['policy_type']} | {layer['benefit_type']} | {layer['quota']} | "
            f"{layer['included_count']} | {layer['shortfall']} |"
        )
    result.append("")
    result.extend(lines("字段", {FIELD_LABELS[name][0]: counts for name, counts in report["by_field"].items()}))
    result.extend(lines("整单", {"所有字段均可比的样例": report["whole_policy"]}))
    if report["new_field_counts"]:
        result.extend([
            "## 新增字段计数", "",
            "| 字段 | 已确定 | 不确定 | 缺失 | 未唯一对齐 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ])
        for name, counts in report["new_field_counts"].items():
            label = FIELD_LABELS[name][0] if name in FIELD_LABELS else name
            result.append("| " + " | ".join([label, *(str(counts[status]) for status in NEW_FIELD_RESULTS)]) + " |")
        result.append("")
    result.extend(["## 节目包写法（按原稿计数）", ""])
    for style, label in PACKAGE_STYLES.items():
        result.append(f"- {label} {report['package_writing_by_document'][style]} 篇")
    if unreviewed := report["package_writing_by_document"]["unreviewed"]:
        result.append(f"- 未登记写法 {unreviewed} 篇")
    result.append("")
    for title, key in (("优惠类型", "by_benefit_type"), ("政策类型", "by_policy_type"), ("国家分组", "by_country_group")):
        groups = {}
        for label, fields in report[key].items():
            counts = Counter()
            for field in fields.values():
                counts.update({status: field[status] for status in STATUSES})
            groups[label] = _rate_counts(counts)
        result.extend(lines(f"{title}（按字段项汇总）", groups))
    result.extend(["## 业务配对量尺", "", f"- 配对配置指纹 `{report['business']['pairing_sha256']}`"])
    result.append(f"- 量尺指纹 `{report['business']['measure_sha256']}`")
    for key, metric in report["business"].get("rates", {}).items():
        numerator, denominator, percent = (metric[key] for key in ("numerator", "denominator", "percent"))
        fraction = f"{numerator}/{denominator}" if denominator is not None else "—"
        value = f"{percent:.2f}%" if percent is not None else "不可判定"
        result.append(f"- {METRIC_LABELS[key]} {fraction}（{value}）")
    for key, label in COUNT_LABELS.items():
        suffix = breakdown if key == "reply_parse_failed" else ""
        result.append(f"- {label} {report['business']['counts'][key]}{suffix}")
    if "configuration_sha256" in report:
        result.extend(["", f"- 运行配置指纹 `{report['configuration_sha256']}`"])
        for key, label in (("input_tokens", "输入 token"), ("output_tokens", "输出 token"), ("elapsed_ms", "耗时毫秒")):
            entry = report["usage"][key]
            value = "未采集" if entry["total"] is None else f"{entry['total']}（{entry['captured_attempts']} 次采集）"
            result.append(f"- {label} {value}")
    result.append("")
    return "\n".join(result).encode("utf-8")
