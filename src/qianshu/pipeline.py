"""将通过飞书门禁的申请接到解析、校验与汇报。"""

import json
import re
from bisect import bisect_right
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, replace
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path

from . import parse, validate
from .draft import Candidate, DraftField, PackageRow, PolicyDraft, SourceExcerpt, Uncertainty
from .feishu.cards import CardLine, DetailPage, detail_pages
from .feishu.entrance import Application
from .parse.model import ParseModel
from .parse.scope import Span, clause_spans, numbered_field, policy_layout, without_list_prefix

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
}
_MUTEX_NO = "申请要求不维护互斥组，需人工确认"
_BOUNDARY_HINT = "段落边界无法确定，需人工确认"
_RED_HINTS = {_MUTEX_NO, _BOUNDARY_HINT}
_RULE_PENDING_FIELDS = (
    "policy_name", "trigger_once", "repeat_bonus", "effective_multiplier",
)
_RULE_PENDING_REASON = "取值规则待确认。"
_CHINESE_ORDINAL = r"[一二三四五六七八九十百两]+"
_ORDINAL = rf"(?:\d+|{_CHINESE_ORDINAL})"
_POLICY_ORDINAL = rf"政策[ \t]*{_ORDINAL}(?=$|[\s：:、.．)），,。！？；;])"
_POLICY_START_RULES = (
    r"另(?:外)?[，,、]?\s*(?:再)?(?:申请|(?:新增|追加|补充)\s*(?:一|1|壹)?\s*(?:条|项|份))",
    r"(?:另(?:外)?|还有)\s*(?:一|1|壹)\s*(?:条|项)",
    rf"第\s*{_ORDINAL}\s*条|第\s*{_ORDINAL}\s*项\s*(?:独立)?(?:政策|申请)",
    _POLICY_ORDINAL,
    r"(?:新增|追加|补充|额外|另需|另行|还想|也申请)[^，,：:\r\n]{0,16}?(?:一条|一项|政策)",
    r"(?:再|还要|还需|同时)\s*申请",
)
_POLICY_START = re.compile("|".join(_POLICY_START_RULES))
_POSSIBLE_POLICY_START = re.compile(
    r"(?:另外|另有|此外|还有|其[二三四五六七八九十]|[一二三四五六七八九十]+[、.．])|"
    r"(?:还(?:需|要)|再(?:加|提|补)|同时)[^\r\n]{0,24}?(?:政策|申请|一条|一项)|"
    r"[^：:，,。！？；;]{1,40}政策申请\s*[:：]"
)
_PARAGRAPH = re.compile(r"(?:\r?\n[^\S\r\n]*){2,}(?=\S)")
_FIELD_START = re.compile(rf"(?:{'|'.join(map(re.escape, _FIELD_LABELS.values()))})\s*[:：]")
# 行首只有半角空格和 Tab 形成缩进，全角空格可出现在它们之前。
_UNINDENTED_PREFIX = r"[^\S\r\n \t]*"
_INDENTED_LINE = re.compile(rf"{_UNINDENTED_PREFIX}[ \t]")


def _suspected_policy_count(body: str) -> int:
    numbered_line = (
        rf"^{_UNINDENTED_PREFIX}(?:[①-⑳]|\d+(?:[.．](?!\d)|[、)）])|{_CHINESE_ORDINAL}、|{_POLICY_ORDINAL})"
    )
    return max(
        body.count("标签名称"),
        len(re.findall(r"^标签[ \t]*[:：]", body, re.MULTILINE)),
        len(re.findall(numbered_line, body, re.MULTILINE)),
    )


def _rule_pending(draft: PolicyDraft, name: str) -> bool:
    uncertainty = getattr(draft, name).uncertainty
    return name in _RULE_PENDING_FIELDS and uncertainty is not None and uncertainty.reason == _RULE_PENDING_REASON


def _policy_boundaries(
    body: str, *, companies_file: Path | None = None,
) -> tuple[list[int], dict[int, Span], set[int], tuple[Span, ...]]:
    headings, excluded = policy_layout(body, companies_file=companies_file)
    heading_starts = {heading.start for heading in headings}
    explicit = set()
    uncertain = set()
    for line in re.finditer(r"[^\r\n]+", body):
        for left, right in clause_spans(line[0], punctuation="。！？；;"):
            content = without_list_prefix(line[0][left:right].lstrip())
            start = line.start() + left
            if match := _POLICY_START.search(content):
                prefix = content[:match.start()]
                if "，" in prefix or "," in prefix:
                    start = line.start() + right - len(content) + match.start()
                explicit.add(start)
            elif start not in heading_starts and _POSSIBLE_POLICY_START.match(content):
                # 单换行也可能开始一段新政策。
                uncertain.add(start)

    paragraphs = list(_PARAGRAPH.finditer(body))
    if paragraphs and heading_starts and paragraphs[0].start() < min(heading_starts):
        first = len(body) - len(body.lstrip())
        if first < paragraphs[0].start() and not any(left <= first < right for left, right in excluded):
            uncertain.add(0)
    for paragraph in paragraphs:
        start = body.rfind("\n", 0, paragraph.end()) + 1
        if start in heading_starts or start in explicit or any(left <= start < right for left, right in excluded):
            continue
        content = without_list_prefix(body[paragraph.end():].partition("\n")[0])
        if not (numbered_field(content) or _FIELD_START.match(content)):
            uncertain.add(start)

    coverage_stops = sorted(heading_starts | explicit | {len(body)})
    indented = {start for start in heading_starts if _INDENTED_LINE.match(body, start)}
    required = (heading_starts - indented) | explicit
    starts = heading_starts | explicit | uncertain
    if starts and body[:min(starts)].strip() and min(starts) not in heading_starts:
        # 「另申请」前没有可识别的标题时，前文仍可能包含第一条政策。
        starts.add(0)
    display_starts = sorted(starts or {0})
    spans = {
        index: (start, coverage_stops[bisect_right(coverage_stops, start)])
        for index, start in enumerate(display_starts, 1) if start in required
    }
    return display_starts, spans, (uncertain | indented) - required, excluded


def _exact_excerpt_span(body: str, excerpt: SourceExcerpt) -> Span | None:
    if not excerpt.text.strip():
        return None
    start = excerpt.start
    if start is None:
        start = body.find(excerpt.text)
        if start < 0 or body.find(excerpt.text, start + 1) >= 0:
            return None
    if start >= 0 and body.startswith(excerpt.text, start):
        return start, start + len(excerpt.text)
    return None


def _uncovered_required_segments(
    body: str, drafts: Sequence[PolicyDraft], required: Mapping[int, Span], excluded: tuple[Span, ...],
) -> list[int]:
    covered = set()
    for draft in drafts:
        local = []
        for field in fields(draft):
            for excerpt in getattr(draft, field.name).evidence:
                if (span := _exact_excerpt_span(body, excerpt)) is not None:
                    left, right = span
                    if not any(left < stop and begin < right for begin, stop in excluded):
                        local.append(span)
        matches = {
            index for index, (start, end) in required.items()
            if any(start <= left and right <= end for left, right in local)
        }
        # 同一份草稿的摘录落入多个必核段时，无法确认其唯一归属。
        if len(matches) == 1:
            covered.update(matches)
    return [index for index in required if index not in covered]


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
        on_reply: Callable[[Application, str], None] | None = None,
    ) -> None:
        self.model = model
        self.tag_dates = tag_dates
        self.today = today
        self.companies_file = companies_file
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

    def __call__(self, application: Application) -> tuple[DetailPage, ...]:
        source = "飞书群申请"
        try:
            body = application.text
            suspected = _suspected_policy_count(body)
            starts, required, uncertain, excluded = _policy_boundaries(body, companies_file=self.companies_file)
            reminders = self._boundary_reminders(body, starts, uncertain)
            parse_failed = False
            try:
                options = {} if self.companies_file is None else {"companies_file": self.companies_file}
                on_reply = None if self._on_reply is None else partial(self._on_reply, application)
                drafts = parse.parse_application(body, model=self.model, on_reply=on_reply, **options)
            except Exception:
                if len(starts) == 1 and not uncertain and not suspected:
                    raise
                drafts = ()
                parse_failed = True
            parsed_count = len(drafts)
            uncovered = _uncovered_required_segments(body, drafts, required, excluded)
            if parsed_count < suspected or uncovered or parse_failed:
                status = "解析不完整，需人工确认"
                if parse_failed:
                    status += "\n停在解析，请核对申请正文或解析配置。"
                if parsed_count < suspected:
                    status += f"\n原文疑似 {suspected} 条，解析出 {parsed_count} 条"
                return detail_pages(source, [
                    CardLine("处理状态", status, red=True),
                    CardLine("未确认覆盖的段落序号", "、".join(map(str, uncovered)) or "无"),
                    *reminders,
                    CardLine("后续处理", "请核对段落与草稿对应关系。未校验。"),
                ])
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
            CardLine("处理状态", status),
            *reminders,
            *(policy.status_line(f"政策 {index}") for index, policy in enumerate(policies, 1)),
            *(line for index, policy in enumerate(policies, 1) for line in policy.lines(f"政策 {index}")),
        ]
        if self._sensitive:
            lines = [replace(line, text=self._sensitive.sub("[已脱敏]", line.text)) for line in lines]
        return detail_pages(source, lines)
