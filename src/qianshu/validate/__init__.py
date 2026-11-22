"""按产品文档 6.3 在建单前校验政策草稿。"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, fields, replace
from datetime import date
from urllib.parse import urlsplit

from qianshu.draft import DraftField, PolicyDraft

__all__ = ["Finding", "ValidationFacts", "ValidationResult", "validate_batch", "validate_draft"]


@dataclass(frozen=True)
class ValidationFacts:
    """草稿以外的已确认信息，由调用方提供。

    用户数用于同批比较。标签日期是所选标签的最晚结束日期。
    邮件明确要求不维护互斥组时，传入确认标记并在草稿字段保留对应依据。
    """

    user_count: int | None = None
    latest_tag_end_date: date | None = None
    shadow_activity: bool = False
    explicit_mutual_exclusion_opt_out: bool = False


@dataclass(frozen=True)
class Finding:
    rule: str
    field: str
    reason: str


@dataclass(frozen=True)
class ValidationResult:
    errors: tuple[Finding, ...]
    warnings: tuple[Finding, ...] = ()
    adjustments: tuple[Finding, ...] = ()

    @property
    def can_create(self) -> bool:
        return not self.errors


def _value[T](draft_field: DraftField[T]) -> T | None:
    return draft_field.value if draft_field.uncertainty is None else None


def _date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def effective_start_date(start: date, today: date) -> date:
    """按 R1 取当前可执行的生效日。"""
    return max(start, today)


def _is_web_url(value: str) -> bool:
    if "\\" in value or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return False
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        return False
    return (
        url.scheme in {"http", "https"}
        and bool(url.hostname)
        and url.username is None
        and (port is None or port > 0)
    )


def _required_fields(draft: PolicyDraft, errors: list[Finding]) -> None:
    from qianshu.parse.benefits import OFFER_FIELD_LABELS, benefit_applicability, benefit_field_format

    inactive_benefits, required_benefits = benefit_applicability(draft)
    for item in fields(draft):
        field = getattr(draft, item.name)
        if field.uncertainty is not None:
            errors.append(Finding("R5", item.name, "字段尚未确认，请申请人确认。"))
        elif item.name in inactive_benefits:
            continue
        elif item.name in OFFER_FIELD_LABELS and item.name not in required_benefits and field.value is None:
            continue
        elif item.name not in {
            "maintain_mutual_exclusion_group", "company", "approval_url",
            "user_scope", "approval_attachments", "user_tags",
        }:
            value = field.value
            if value is None or (isinstance(value, str) and not value.strip()):
                errors.append(Finding("R5", item.name, "必填字段缺失，请申请人补充。"))
            elif not benefit_field_format(item.name, value):
                errors.append(Finding("R5", item.name, "优惠字段格式无效，请申请人核对。"))
    for name in sorted(required_benefits):
        if not hasattr(draft, name):
            errors.append(Finding("R5", name, "必填字段缺失，请申请人补充。"))

    packages = _value(draft.packages)
    if packages is not None and (
        not packages or any(not row.current_package.strip() for row in packages)
    ):
        errors.append(Finding("R5", "packages", "请申请人补充各行的当前节目包。"))


def _validate_tags(
    draft: PolicyDraft, end: date | None, facts: ValidationFacts, errors: list[Finding],
) -> None:
    if draft.user_tags.uncertainty is not None:
        return

    tags = draft.user_tags.value or []
    if any(not tag.strip() for tag in tags):
        errors.append(Finding("R5", "user_tags", "标签名称缺失，请申请人补充。"))
    shadow_activity = facts.shadow_activity or any(tag.endswith("_image") for tag in tags)
    if shadow_activity and len(tags) != 1:
        errors.append(Finding(
            "R3", "user_tags", "影子标签类活动须选择一个标签，请申请人确认。",
        ))

    if not tags:
        if _value(draft.policy_type) == "定向政策":
            errors.append(Finding("R8", "user_tags", "定向政策须选择用户标签，请申请人补充。"))
        return

    latest_end = facts.latest_tag_end_date
    if latest_end is None:
        errors.append(Finding("R8", "end_date", "标签最晚结束日期尚未确认，请申请人补充。"))
    elif end is not None and end > latest_end:
        errors.append(Finding(
            "R8", "end_date", "政策结束日期晚于标签最晚结束日期，请申请人确认。",
        ))


def validate_draft(
    draft: PolicyDraft,
    *,
    today: date,
    facts: ValidationFacts = ValidationFacts(),
) -> tuple[PolicyDraft, ValidationResult]:
    """返回调整后的草稿和可汇报的结果，调用方须先检查 can_create。

    today 为调用方传入的 UTC 日期。公司匹配尚无定论时，调用方须在草稿中保留
    company.uncertainty。公司值只有在匹配已确定时才能填写。
    """
    errors: list[Finding] = []
    warnings: list[Finding] = []
    adjustments: list[Finding] = []
    _required_fields(draft, errors)

    start_value = _value(draft.start_date)
    end_value = _value(draft.end_date)
    start = _date(start_value)
    end = _date(end_value)
    if start is None and start_value and start_value.strip():
        errors.append(Finding("R1", "start_date", "请申请人提供 YYYY-MM-DD 格式的有效开始日期。"))
    if end is None and end_value and end_value.strip():
        errors.append(Finding("R8", "end_date", "请申请人提供 YYYY-MM-DD 格式的有效结束日期。"))
    effective = effective_start_date(start, today) if start is not None else None
    if effective is not None and effective != start:
        start = effective
        draft = replace(draft, start_date=replace(draft.start_date, value=start.isoformat()))
        adjustments.append(Finding("R1", "start_date", "开始日期早于今天，已调整为今天。"))
    if start == today:
        warnings.append(Finding("R1", "start_date", "BOSS 配置截止时间尚未确认，生效日期暂不顺延。"))
    if start is not None and end is not None and start > end:
        errors.append(Finding("R1", "start_date", "生效开始日期晚于结束日期，请申请人确认活动周期。"))

    _validate_tags(draft, end, facts, errors)

    mutual_exclusion = draft.maintain_mutual_exclusion_group
    if mutual_exclusion.uncertainty is None:
        explicit_opt_out = facts.explicit_mutual_exclusion_opt_out and any(
            excerpt.text.strip() for excerpt in mutual_exclusion.evidence
        )
        choice = mutual_exclusion.value
        if choice is None or (choice == "否" and not explicit_opt_out):
            draft = replace(draft, maintain_mutual_exclusion_group=DraftField(value="是"))
            adjustments.append(Finding(
                "R7", "maintain_mutual_exclusion_group", "缺少明确的免维护说明，已默认维护互斥组。",
            ))
        elif choice not in ("是", "否"):
            errors.append(Finding(
                "R7", "maintain_mutual_exclusion_group", "请申请人确认是否维护互斥组。",
            ))

    if draft.approval_url.uncertainty is None and not _is_web_url(draft.approval_url.value or ""):
        errors.append(Finding("R9", "approval_url", "请申请人补充有效的任务审批网址。"))
    if draft.user_scope.uncertainty is None and not (draft.user_scope.value or "").strip():
        errors.append(Finding("R9", "user_scope", "请申请人补充用户范围。"))

    if not (_value(draft.company) or "").strip():
        errors.append(Finding("R10", "company", "无法确定适用公司，请申请人明确公司名称。"))

    return draft, ValidationResult(tuple(errors), tuple(warnings), tuple(adjustments))


def validate_batch(
    policies: Sequence[tuple[PolicyDraft, ValidationFacts]],
    *,
    today: date,
) -> list[tuple[PolicyDraft, ValidationResult]]:
    """按输入顺序返回每条政策的建单草稿及结果。用户数已知时参与 R4 比较。"""
    counts = Counter(facts.user_count for _, facts in policies if facts.user_count is not None)
    results = []
    for draft, facts in policies:
        normalized, result = validate_draft(draft, today=today, facts=facts)
        if counts[facts.user_count] > 1:
            result = replace(result, warnings=(*result.warnings, Finding(
                "R4", "user_count", "同批有多条政策的用户数相同，可能选重了标签。",
            ),))
        results.append((normalized, result))
    return results
