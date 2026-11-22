"""按已核验标签派生政策名称和政策类型。"""

import re

from .evidence import Sources, accept, can_use_absence, issue, verified_value
from .protocol import FieldResult, ValueSource

_POLICY_TYPE_HINT = "政策类型按用户标签判定"
_TAG_CHAR = re.compile(r"[A-Za-z0-9_.-]")


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


def derive_names(results: dict[str, FieldResult]) -> list[str]:
    tags = results["user_tags"]
    name = results["policy_name"]
    kind = results["policy_type"]
    if not verified_value(tags):
        results["policy_name"] = issue(name, "tag_input_pending")
        results["policy_type"] = issue(kind, "tag_input_pending")
        return []

    if len(tags.value) != 1:
        results["policy_name"] = issue(name, "unique_tag_required")
    elif verified_value(name) and name.value != tags.value[0]:
        results["policy_name"] = issue(name, "policy_name_conflict")
    elif verified_value(name) or can_use_absence(name):
        results["policy_name"] = accept(name, tags.value[0], ValueSource.LOOKUP,
                                        rule="6.2.policy_name", inputs=("user_tags",),
                                        evidence=(*name.evidence, *tags.evidence))

    derived = "定向政策" if tags.value else "公开政策"
    if verified_value(kind) and kind.value != derived:
        results["policy_type"] = issue(
            accept(kind, derived, ValueSource.LOOKUP, rule="R8", inputs=("user_tags",)),
            "policy_type_conflict",
        )
    elif verified_value(kind) or can_use_absence(kind):
        results["policy_type"] = accept(kind, derived, ValueSource.LOOKUP,
                                        rule="R8", inputs=("user_tags",), evidence=(*kind.evidence, *tags.evidence))
        return [_POLICY_TYPE_HINT]
    return []
