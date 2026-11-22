"""将通过飞书门禁的申请接到解析、校验与汇报。"""

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, replace
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path

from . import parse, validate
from .draft import Candidate, DraftField, PackageRow, PolicyDraft, SourceExcerpt, Uncertainty
from .feishu.cards import CardLine, DetailPage, detail_pages
from .feishu.entrance import Application
from .parse.companies import DEFAULT_COMPANIES_FILE, load_company_rules
from .parse.evidence import ReviewRange
from .parse.model import ParseModel

_FIELD_LABELS = {
    "policy_name": "优惠政策名称",
    "maintain_mutual_exclusion_group": "是否需要维护互斥组",
    "company": "公司",
    "policy_type": "政策类型",
    "start_date": "开始日期",
    "end_date": "结束日期",
    "benefit_type": "优惠类型",
    "trigger_once": "是否只触发一次",
    "cancel_on_downgrade": "降包用户取消优惠",
    "recharge_days": "充值天数",
    "gift_days": "赠送天数",
    "repeat_bonus": "多买多送",
    "effective_multiplier": "生效倍数",
    "user_tags": "用户标签",
    "packages": "节目包",
    "user_scope": "用户范围",
    "approval_url": "任务审批链接",
    "approval_attachments": "审批件附件",
    "user_count": "用户数",
}
_DEFAULT_MUTEX = "互斥组按默认填是"
_DEFAULT_HINTS = {
    _DEFAULT_MUTEX, "是否只触发一次按默认填否", "多买多送按默认填是", "生效倍数按默认填 1",
    parse._ATTACHMENTS_HINT, parse._POLICY_TYPE_HINT,
    "充值 / 赠送天数按 1 月 = 30 天换算", "降包用户取消优惠按默认填是",
    "降包用户取消优惠未写，请在卡片确认具体取值",
}
_MUTEX_NO = "申请要求不维护互斥组，需人工确认"
_BOUNDARY_HINT = "段落边界无法确定，需人工确认"
_RED_HINTS = {_MUTEX_NO, _BOUNDARY_HINT}
_RULE_PENDING_FIELDS = (
    "policy_name", "trigger_once", "repeat_bonus", "effective_multiplier",
)
_RULE_PENDING_REASON = "取值规则待确认。"


def _rule_pending(draft: PolicyDraft, name: str) -> bool:
    uncertainty = getattr(draft, name).uncertainty
    return name in _RULE_PENDING_FIELDS and uncertainty is not None and uncertainty.reason == _RULE_PENDING_REASON


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _review_mutex(draft: PolicyDraft) -> PolicyDraft:
    field = draft.maintain_mutual_exclusion_group
    if field.value != "否":
        return draft
    # 即使上游返回确定的「否」，也将其列为待人工确认的候选。
    return replace(draft, maintain_mutual_exclusion_group=replace(
        field, value=None, uncertainty=Uncertainty(
            reason=_MUTEX_NO,
            candidates=[Candidate(value="否", evidence=field.evidence)],
        ),
    ))


def _facts(draft: PolicyDraft, tag_dates: Mapping[str, date]) -> validate.ValidationFacts:
    tags = draft.user_tags.value or []
    if draft.user_tags.uncertainty is None and tags and all(tag in tag_dates for tag in tags):
        return validate.ValidationFacts(latest_tag_end_date=max(tag_dates[tag] for tag in tags))
    return validate.ValidationFacts()


def _value(value: object) -> str:
    if value is None:
        return "未确定"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=_package_row)


def _package_row(row: PackageRow) -> dict:
    return {"当前节目包": row.current_package, "赠送节目包": row.gift_package}


def _evidence(excerpts: Iterable[SourceExcerpt]) -> str:
    return "\n".join(f"「{excerpt.text}」" for excerpt in excerpts) or "无原文摘录"


def _field_lines(prefix: str, field: DraftField) -> Iterable[CardLine]:
    yield CardLine(prefix, f"值　{_value(field.value)}\n原文出处　{_evidence(field.evidence)}")
    if uncertainty := field.uncertainty:
        yield CardLine(f"{prefix} · 待确认", uncertainty.reason)
        if not uncertainty.candidates:
            yield CardLine(f"{prefix} · 候选", "无")
        for index, candidate in enumerate(uncertainty.candidates, 1):
            yield CardLine(
                f"{prefix} · 候选 {index}",
                f"值　{_value(candidate.value)}\n原文出处　{_evidence(candidate.evidence)}",
            )


@dataclass(frozen=True)
class _Policy:
    draft: PolicyDraft
    hints: tuple[str, ...]
    validation: validate.ValidationResult | None = None

    @property
    def approval_url_deferred(self) -> bool:
        field = self.draft.approval_url
        return field.value is None and field.uncertainty is None

    def parsing_errors(self) -> Iterable[validate.Finding]:
        if self.validation is not None:
            for item in self.validation.errors:
                # 空链接在建单时补齐，建单校验仍保留 R9。
                if (item.rule, item.field) != ("R9", "approval_url") or not self.approval_url_deferred:
                    yield item

    def status_line(self, prefix: str) -> CardLine:
        draft = self.draft
        if self.validation is None:
            status = "停在校验。"
        elif (any(self.parsing_errors()) or draft.uncertain_fields()
              or draft.maintain_mutual_exclusion_group.value != "是"):
            pending = [_FIELD_LABELS[name] for name in _RULE_PENDING_FIELDS if _rule_pending(draft, name)]
            if pending:
                status = (
                    f"停在校验：{len(pending)} 个字段的取值规则待定，需人工确认（产品文档 6.2）\n"
                    f"待定字段：{'、'.join(pending)}"
                )
            else:
                status = "停在校验，需人工确认或请申请人补充。"
        else:
            status = "解析完成，待群卡片同意"
        return CardLine(f"{prefix} · 处理状态", status)

    def lines(self, prefix: str) -> Iterable[CardLine]:
        result = self.validation
        hints = self.hints
        # 校验可调整互斥组默认值，保留解析时的所有提示并补上校验产生的默认提示。
        if result and any(item.rule == "R7" for item in result.adjustments):
            hints = (*hints, _DEFAULT_MUTEX)
        for hint in dict.fromkeys(hints):
            section = "自动调整" if hint in _DEFAULT_HINTS else "提醒项"
            yield CardLine(f"{prefix} · {section}", hint, red=hint in _RED_HINTS)
        if result:
            for item in self.parsing_errors():
                if item.rule == "R5" and _rule_pending(self.draft, item.field):
                    continue
                yield CardLine(f"{prefix} · 需追问 · {_FIELD_LABELS[item.field]}", item.reason)
            for item in result.adjustments:
                if item.rule != "R7":
                    yield CardLine(f"{prefix} · 自动调整 · {_FIELD_LABELS[item.field]}", item.reason)
            for item in result.warnings:
                yield CardLine(f"{prefix} · 提醒项 · {_FIELD_LABELS[item.field]}", item.reason)
        for field in fields(self.draft):
            label = _FIELD_LABELS[field.name]
            title = f"{prefix} · {label}"
            if field.name == "approval_url" and self.approval_url_deferred:
                yield CardLine(title, "建单时填写")
            else:
                yield from _field_lines(title, getattr(self.draft, field.name))


class Pipeline:
    def __init__(
        self, *,
        model: ParseModel,
        tag_dates: Mapping[str, date],
        today: Callable[[], date] = _utc_today,
        sensitive_values: Iterable[str] = (),
        companies_file: Path | None = None,
        legacy_companies: bool = False,
        on_reply: Callable[[Application, str], None] | None = None,
    ) -> None:
        self.model = model
        self.tag_dates = tag_dates
        self.today = today
        self.companies_file = companies_file
        self.legacy_companies = legacy_companies
        self._on_reply = on_reply
        values = sorted({
            literal for value in sensitive_values if value
            for literal in (value, json.dumps(value, ensure_ascii=False)[1:-1])
        }, key=len, reverse=True)
        self._sensitive = re.compile("|".join(map(re.escape, values))) if values else None

    def _boundary_reminders(self, body: str, starts: list[int], uncertain: set[int]) -> list[CardLine]:
        reminders = []
        for index, (start, end) in enumerate(zip(starts, [*starts[1:], len(body)], strict=True), 1):
            if start not in uncertain:
                continue
            segment = body[start:end]
            if self._sensitive:
                segment = self._sensitive.sub("[已脱敏]", segment)
            preview = " ".join(segment.lstrip()[:12].splitlines()).strip()
            reminders.append(f"段落 {index}：{preview}…，段落边界未确认，请审批人核对")
        return [CardLine("提醒项", "\n".join(reminders))] if reminders else []

    def _pending_range_reminders(self, body: str, pending: Iterable[ReviewRange]) -> list[CardLine]:
        reminders = []
        sensitive_spans = [match.span() for match in self._sensitive.finditer(body)] if self._sensitive else []
        for index, item in enumerate(pending, 1):
            span = item.span
            location = f"{span.source_id} [{span.start}, {span.end})" if span else "无可定位坐标"
            if item.block_id is not None:
                location += f"（输入块 {item.block_id} 缺少覆盖说明）"
            location += f"\n理由码　{item.code}"
            if item.reason_code:
                location += f"（{item.reason_code}）"
            if span and span.source_id == "body" and span.end <= len(body):
                start, end = span.start, span.end
                crosses_edge = any(left < start < right or left < end < right for left, right in sensitive_spans)
                if not crosses_edge:
                    preview, = self._boundary_reminders(body[start:end], [0], {0})
                    location += "\n" + preview.text.removeprefix("段落 1：")
            reminders.append(CardLine(f"待确认范围 {index}", location, red=True))
        return reminders

    def __call__(self, application: Application) -> tuple[DetailPage, ...]:
        source = "飞书群申请"
        try:
            body = application.text
            company_rules = load_company_rules(
                Path("rules"), self.companies_file or DEFAULT_COMPANIES_FILE, legacy=self.legacy_companies,
            )
            on_reply = None if self._on_reply is None else partial(self._on_reply, application)
            drafts = parse.parse_application(body, model=self.model, on_reply=on_reply, company_rules=company_rules)
            policies = []
            for parsed in drafts:
                draft = _review_mutex(parsed)
                hints = tuple(getattr(parsed, "hints", ()))
                if parsed.maintain_mutual_exclusion_group.value == "否":
                    hints = (*hints, _MUTEX_NO)
                policies.append(_Policy(draft, hints))
        except Exception:
            return detail_pages(source, [
                CardLine(
                    "处理状态",
                    "解析不完整，需人工确认\n停在解析，请核对申请正文或解析配置。未校验。",
                    red=True,
                ),
            ])

        pending = getattr(drafts, "pending", ())
        document = getattr(drafts, "document", None)
        requires_full_review = getattr(drafts, "requires_full_review", False)
        reminders = []
        if document:
            reminders.append(CardLine("文档判定", document.status.value))
        reminders.extend(self._pending_range_reminders(body, pending))

        if requires_full_review:
            status = "需人工核对全文\n停在校验。请核对申请全文、待确认范围和已提取的草稿。未校验。"
        elif pending:
            status = "解析不完整，需人工确认\n请核对待确认范围与已提取的草稿。未校验。"
        elif document and not policies:
            status = document.status.value
        else:
            try:
                results = validate.validate_batch(
                    [(policy.draft, _facts(policy.draft, self.tag_dates)) for policy in policies],
                    today=self.today(),
                )
                policies = [
                    replace(policy, draft=draft, validation=result)
                    for policy, (draft, result) in zip(policies, results, strict=True)
                ]
            except Exception:
                status = "解析完成，校验执行异常。"
            else:
                status = f"解析完成，共 {len(policies)} 条政策。"

        lines = [
            CardLine("处理状态", status, red=requires_full_review or bool(pending)),
            *reminders,
            *(policy.status_line(f"政策 {index}") for index, policy in enumerate(policies, 1)),
            *(line for index, policy in enumerate(policies, 1) for line in policy.lines(f"政策 {index}")),
        ]
        if self._sensitive:
            lines = [replace(line, text=self._sensitive.sub("[已脱敏]", line.text)) for line in lines]
        return detail_pages(source, lines)
