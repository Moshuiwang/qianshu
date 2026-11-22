"""按已核验的字段生成政策名称和政策类型。"""

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import date

from qianshu.validate import effective_start_date

from .evidence import Sources, accept, can_use_absence, issue, verified_value
from .protocol import FieldResult, ValueSource

_POLICY_TYPE_HINT = "政策类型按用户标签判定"
_TAG_CHAR = re.compile(r"[A-Za-z0-9_.-]")
_PUBLIC_NAME_RULE = "6.2.policy_name.public"
_TARGETED_NAME_RULE = "6.2.policy_name"
MAX_NAME_LENGTH = 50
ORIGINAL_NAME_LABEL = "申请原文"
PROPOSED_NAME_LABEL = "按格式拼出，待同意"
CONFIRMATION_REASONS = {
    "name_type_pending": "政策类型尚未确认，请人工核对名称。",
    "policy_name_evidence_mismatch": "缺少逐字匹配的政策名称引文，请人工核对。",
    "public_name_short_name_pending": "无法确认活动或优惠短名，请人工确定政策名称。",
    "public_name_inputs_pending": "无法确认国家代码或生效开始日，请人工确定政策名称。",
    "policy_name_too_long": "优惠政策名称超过 50 字符，请人工确认。",
    "policy_name_duplicate": "优惠政策名称与已知名称重名，请人工确认。",
}


def country_codes(table: str) -> dict[str, str]:
    """取受信国家表中唯一的两位国家代码。"""
    options_by_country = {}
    for line in table.splitlines():
        country, *aliases = (cell.strip() for cell in line.split("\t"))
        options = {alias.upper() for alias in aliases if re.fullmatch(r"[A-Za-z]{2}", alias)}
        options_by_country.setdefault(country, set()).update(options)
    counts = Counter(code for options in options_by_country.values() for code in options)
    return {country: code for country, options in options_by_country.items() if len(options) == 1
            for code in options if counts[code] == 1}


def verify_tags(result: FieldResult, sources: Sources) -> FieldResult:
    if not verified_value(result):
        return result
    for tag in result.value:
        if not any(q.text == tag and _tag_boundary(q.span, sources) for q in result.evidence):
            return issue(result, "tag_evidence_mismatch")
    return result


def _tag_boundary(span, sources: Sources) -> bool:
    location = sources.locate(span)
    for block in sources.blocks.values():
        if block.source_id != location.source_id:
            continue
        for offset in (location.start - 1, location.end):
            pos = offset - block.start
            if 0 <= pos < len(block.text) and _TAG_CHAR.fullmatch(block.text[pos]):
                return False
    return True


def policy_option(value: str, options: dict[str, str]) -> str | None:
    value = options.get(value, value)
    return value if value in ("公开政策", "定向政策") else None


def derive_names(results: dict[str, FieldResult], codes: Mapping[str, str], today: date | None) -> list[str]:
    tags = results["user_tags"]
    name = results["policy_name"]
    kind = results["policy_type"]
    if not verified_value(tags):
        results["policy_name"] = issue(name, "tag_input_pending")
        results["policy_type"] = issue(kind, "tag_input_pending")
        return []

    derived = "定向政策" if tags.value else "公开政策"
    if verified_value(kind) and kind.value != derived:
        results["policy_type"] = issue(
            accept(kind, derived, ValueSource.LOOKUP, rule="R8", inputs=("user_tags",)),
            "policy_type_conflict",
        )
    elif verified_value(kind) or can_use_absence(kind):
        results["policy_type"] = accept(kind, derived, ValueSource.LOOKUP,
                                        rule="R8", inputs=("user_tags",), evidence=(*kind.evidence, *tags.evidence))

    if not tags.value and results["policy_type"].needs_confirmation:
        results["policy_name"] = issue(name, "name_type_pending")
    elif not tags.value:
        results["policy_name"] = _public_name(results, codes, today)
    elif len(tags.value) != 1:
        results["policy_name"] = issue(name, "unique_tag_required")
    elif verified_value(name) and name.value != tags.value[0]:
        results["policy_name"] = issue(name, "policy_name_conflict")
    elif verified_value(name) or can_use_absence(name):
        results["policy_name"] = accept(name, tags.value[0], ValueSource.LOOKUP,
                                        rule=_TARGETED_NAME_RULE, inputs=("user_tags",),
                                        evidence=(*name.evidence, *tags.evidence))
    return [_POLICY_TYPE_HINT] if not results["policy_type"].needs_confirmation else []


def _public_name(results: dict[str, FieldResult], codes: Mapping[str, str], today: date | None) -> FieldResult:
    name = results["policy_name"]
    if verified_value(name):
        original = name.statement.value
        if any(quote.text == original for quote in name.evidence):
            return replace(name, value=original)
        return issue(name, "policy_name_evidence_mismatch")
    if not can_use_absence(name):
        return name

    short_name = results["policy_short_name"]
    if not verified_value(short_name):
        return issue(name, "public_name_short_name_pending")
    country, start = results["country"], results["start_date"]
    code = codes.get(country.value) if verified_value(country) else None
    if today is None or not code or not verified_value(start):
        return issue(name, "public_name_inputs_pending")
    value = compose_public_name(code, short_name.value, date.fromisoformat(start.value), today)
    proposal = accept(name, value, ValueSource.LOOKUP, rule=_PUBLIC_NAME_RULE,
                      inputs=("user_tags", "policy_type", "country", "policy_short_name", "start_date"),
                      evidence=(*results["policy_type"].evidence, *short_name.evidence, *country.evidence, *start.evidence))
    return replace(proposal, needs_confirmation=True)


def compose_public_name(country_code: str, short_name: str, start: date, today: date) -> str:
    effective = effective_start_date(start, today)
    return f"{country_code}_{short_name}_{effective.isoformat().replace('-', '')}"


def is_proposal(result: FieldResult) -> bool:
    return result.rule == _PUBLIC_NAME_RULE and result.needs_confirmation and not result.issues


def source_label(result: FieldResult) -> str | None:
    if is_proposal(result):
        return PROPOSED_NAME_LABEL
    if result.needs_confirmation:
        return None
    if result.rule == _TARGETED_NAME_RULE:
        return "用户标签"
    return ORIGINAL_NAME_LABEL


def check_names(results: Sequence[dict[str, FieldResult]], known_names: Iterable[str]) -> None:
    """核对草稿候选的字符长度及与已知名称、同批名称的逐字重复。"""
    named = [row for row in results if source_label(row["policy_name"]) is not None]
    counts = Counter(row["policy_name"].value for row in named)
    known = set(known_names)
    for row in named:
        name = row["policy_name"]
        if len(name.value) > MAX_NAME_LENGTH:
            name = issue(name, "policy_name_too_long")
        if name.value in known or counts[name.value] > 1:
            name = issue(name, "policy_name_duplicate")
        row["policy_name"] = name
