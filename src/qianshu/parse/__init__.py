"""把申请正文解析成待校验的政策草稿。"""

import json
import math
import re
from calendar import month_abbr, month_name
from collections.abc import Mapping
from dataclasses import fields
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from qianshu.config import load_config
from qianshu.draft import Candidate, DraftField, PackageRow, PolicyDraft, SourceExcerpt, Uncertainty

from .model import ModelError, ParseModel, configured_model

__all__ = ["ModelError", "ParseError", "parse_application"]

_FIELD_NAMES = {field.name for field in fields(PolicyDraft)}
_PENDING = {"policy_name", "trigger_once", "repeat_bonus", "effective_multiplier", "approval_attachments"}
_YES_NO = {"maintain_mutual_exclusion_group", "trigger_once", "cancel_on_downgrade", "repeat_bonus"}
_LISTS = {"user_tags", "approval_attachments"}
_DATES = {"start_date", "end_date"}
_OPTIONS = {"policy_type", "benefit_type"}
_MUTEX = "maintain_mutual_exclusion_group"
_MUTEX_NO = re.compile(r"(?:是否需要维护互斥组\s*[：:]\s*否|(?:不需要|无需)维护互斥组)[。.]?")
_END_CUE = re.compile(
    r"结[束止]|[截终]止|截至|到期|有效期至|[至到]\s*[：:]?\s*\d|"
    r"\b(?:end(?:s|ing)?|expiry|expires?|expiration|until|through|to)\b|[~～–—]",
    re.IGNORECASE,
)


class ParseError(ValueError):
    """解析规则不可读，或模型回复不符合结构约定。"""


def parse_application(
    body: str,
    *,
    config: Mapping[str, str] | None = None,
    model: ParseModel | None = None,
    rules_dir: Path = Path("rules"),
) -> list[PolicyDraft]:
    """接收邮件或群消息正文，返回草稿。调用方可用录制回复注入 model。"""
    if not body.strip():
        return []

    try:
        prompt = (rules_dir / "parse-prompt.md").read_text(encoding="utf-8")
        rules = (rules_dir / "parse-rules.md").read_text(encoding="utf-8")
        option_table = (rules_dir / "parse-options.tsv").read_text(encoding="utf-8")
        options = _read_options(option_table)
    except (OSError, UnicodeError, ValueError):
        raise ParseError("申请解析规则不可读或选项表格式无效。") from None

    if model is None:
        model = configured_model(config if config is not None else load_config())
    reply = model(
        system=f"{prompt}\n\n{rules}\n\n选项表（列以制表符分隔）\n\n{option_table}",
        user=json.dumps({"application": body}, ensure_ascii=False),
    )

    try:
        policies = json.loads(reply, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise ParseError("申请解析模型回复不是有效的政策 JSON。") from None
    if not isinstance(policies, list):
        raise ParseError("申请解析模型回复应为政策数组。")

    drafts = []
    for policy in policies:
        _check_object(policy, _FIELD_NAMES)
        if not policy:
            raise ParseError("申请解析模型回复包含空白政策。")
        drafts.append(PolicyDraft(**{
            name: _draft_field(name, raw=policy.get(name, {}), body=body, options=options.get(name, {}))
            for name in sorted(_FIELD_NAMES)
        }))
    return drafts


def _read_options(table: str) -> dict[str, dict[str, str]]:
    options: dict[str, dict[str, str]] = {field: {} for field in _OPTIONS}
    for line in table.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        field, canonical, *aliases = (cell.strip() for cell in line.split("\t"))
        if field not in options or not canonical or any(not alias for alias in aliases):
            raise ValueError
        for term in [canonical, *aliases]:
            options[field][term] = canonical
    return options


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError
    return result


def _reject_constant(_: str) -> None:
    raise ValueError


def _check_object(obj: object, allowed: set[str]) -> None:
    if not isinstance(obj, dict) or obj.keys() - allowed:
        raise ParseError("申请解析模型回复包含无效对象或未知字段。")


def _evidence(body: str, quotes: object) -> tuple[list[SourceExcerpt], bool]:
    if not isinstance(quotes, list) or any(not isinstance(quote, str) for quote in quotes):
        raise ParseError("申请解析模型回复的原文依据应为字符串数组。")
    evidence = []
    for quote in dict.fromkeys(quotes):
        if quote.strip() and (start := body.find(quote)) >= 0:
            # 重复摘录仍可回读，字符下标在正文中唯一时才填写。
            unique = body.find(quote, start + 1) < 0
            evidence.append(SourceExcerpt(text=quote, start=start if unique else None))
    return evidence, len(evidence) == len(set(quotes))


def _draft_field(name: str, *, raw: dict, body: str, options: dict[str, str]) -> DraftField:
    _check_object(raw, {"value", "evidence", "candidates"})
    value_evidence, complete = _evidence(body, raw.get("evidence", []))
    evidence = list(value_evidence)
    value = _value(name, raw.get("value"))
    if name in _OPTIONS and value is not None:
        value = options.get(value, value)

    proposed = raw.get("candidates", [])
    if not isinstance(proposed, list):
        raise ParseError("申请解析模型回复的候选值应为数组。")
    candidates = []
    for item in proposed:
        _check_object(item, {"value", "evidence"})
        candidate_evidence, candidate_complete = _evidence(body, item.get("evidence", []))
        candidate_value = _value(name, item.get("value"))
        if name in _OPTIONS and candidate_value is not None:
            candidate_value = options.get(candidate_value, candidate_value)
        for excerpt in candidate_evidence:
            if excerpt not in evidence:
                evidence.append(excerpt)
        if candidate_complete and _supports_candidate(name, candidate_value, candidate_evidence, options):
            candidate = Candidate(value=candidate_value, evidence=candidate_evidence)
            if candidate not in candidates:
                candidates.append(candidate)

    if name == _MUTEX and raw.get("value") is None and not proposed and raw.get("evidence", []) == []:
        return DraftField(value="是")

    if name in _PENDING:
        reason = "取值规则待确认。"
    elif proposed:
        reason = "申请存在待确认的候选值。"
    elif value is None:
        reason = "申请未提供明确且格式有效的取值。"
    elif not evidence or not complete or not _supports(name, value, evidence, body, options):
        reason = "取值缺少可核对的原文依据或已知选项。"
    else:
        return DraftField(value=value, evidence=evidence)

    if complete and _supports_candidate(name, value, value_evidence, options):
        candidate = Candidate(value=value, evidence=value_evidence)
        if candidate not in candidates:
            candidates.insert(0, candidate)
    return DraftField(evidence=evidence, uncertainty=Uncertainty(reason=reason, candidates=candidates))


def _value(name: str, value: object) -> object:
    if name in _YES_NO:
        return value if value in ("是", "否") else None
    if name in {"recharge_days", "gift_days"}:
        return value if type(value) is int else None
    if name == "effective_multiplier":
        return value if type(value) is int or (type(value) is float and math.isfinite(value)) else None
    if name in _LISTS:
        return value if isinstance(value, list) and all(_text(item) for item in value) else None
    if name == "packages":
        if isinstance(value, list) and all(_package(item) for item in value):
            return [PackageRow(**row) for row in value]
        return None
    if not _text(value):
        return None
    if name in _DATES:
        try:
            return value if date.fromisoformat(value).isoformat() == value else None
        except ValueError:
            return None
    if name == "approval_url":
        try:
            url = urlsplit(value)
            if url.scheme not in {"https", "http"} or not url.hostname or any(c.isspace() for c in value):
                return None
        except ValueError:
            return None
    return value


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _package(value: object) -> bool:
    return (
        isinstance(value, dict)
        and value.keys() == {"current_package", "gift_package"}
        and _text(value["current_package"])
        and (value["gift_package"] is None or _text(value["gift_package"]))
    )


def _supports_candidate(
    name: str, value: object, evidence: list[SourceExcerpt], options: dict[str, str]
) -> bool:
    return value is not None and bool(evidence) and _supports_value(name, value, evidence, options)


def _supports(
    name: str, value: object, evidence: list[SourceExcerpt], body: str, options: dict[str, str]
) -> bool:
    if name == _MUTEX and value == "否":
        # R7 的「否」需要完整独立的原文表述。
        lines = {line.strip() for line in body.splitlines()}
        return any(item.text.strip() in lines and _MUTEX_NO.fullmatch(item.text.strip()) for item in evidence)

    if name in _OPTIONS and value not in options.values():
        return False
    return _supports_value(name, value, evidence, options)


def _supports_value(
    name: str, value: object, evidence: list[SourceExcerpt], options: dict[str, str]
) -> bool:
    excerpts = [item.text for item in evidence]
    if name in _DATES:
        return _date_evidence(name, value, excerpts)
    if name in _OPTIONS:
        terms = {value, *(term for term, option in options.items() if option == value)}
        return any(term in snippet for term in terms for snippet in excerpts)
    if name in {"policy_name", "company", "user_scope", "approval_url"}:
        return any(value in excerpt for excerpt in excerpts)
    if name in _LISTS:
        return all(any(item in excerpt for excerpt in excerpts) for item in value)
    if name in {"recharge_days", "gift_days", "effective_multiplier"}:
        number = re.escape(str(value))
        return any(re.search(rf"(?<![\d.]){number}(?!\d|\.\d)", excerpt) for excerpt in excerpts)
    if name == "packages":
        for row in value:
            if not any(row.current_package in excerpt for excerpt in excerpts):
                return False
            if row.gift_package is None or any(row.gift_package in excerpt for excerpt in excerpts):
                continue
            if row.gift_package != f"{row.current_package}_copy":
                return False
            if not any(row.current_package in excerpt and "附属包" in excerpt for excerpt in excerpts):
                return False
    return True


def _date_evidence(name: str, value: str, excerpts: list[str]) -> bool:
    year, month, day = map(int, value.split("-"))
    month_text = rf"(?:{month_name[month]}|{month_abbr[month]}\.?)"
    formats = [
        rf"{year:04d}[-./年]\s*0?{month}[-./月]\s*0?{day}",
        rf"{year:04d}{month:02d}{day:02d}",
        rf"0?{day}\s+{month_text}\s+{year:04d}",
        rf"{month_text}\s+0?{day},?\s+{year:04d}",
    ]
    # 年份在末尾且日、月都不超过 12 时，无法从数字本身区分两种顺序。
    if day > 12 or day == month:
        formats.extend([
            rf"0?{day}[-./]0?{month}[-./]{year:04d}",
            rf"0?{month}[-./]0?{day}[-./]{year:04d}",
        ])
    pattern = re.compile(rf"(?<!\d)(?:{'|'.join(formats)})(?!\d)", re.IGNORECASE)
    return any(
        name != "end_date" or _END_CUE.search(excerpt, 0, match.end())
        for excerpt in excerpts
        for match in pattern.finditer(excerpt)
    )
