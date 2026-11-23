"""将通过飞书门禁的申请接到解析、校验与汇报。"""

import json
import re
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, date, datetime
from functools import partial
from itertools import accumulate
from pathlib import Path

from . import parse, validate
from .draft import Candidate, DraftField, PackageRow, PolicyDraft, SourceExcerpt, Uncertainty
from .feishu.cards import CardLine, DetailPage, detail_pages
from .feishu.entrance import Application
from .parse.benefits import ADJUSTMENT_HINTS, OFFER_FIELD_LABELS, BenefitCatalog, benefit_applicability
from .parse.companies import DEFAULT_COMPANIES_FILE, load_company_rules
from .parse.evidence import STRUCTURE_REASONS, PolicyStructureIssue
from .parse.model import ParseModel
from .parse.protocol import RequestedTier
from .parse.names import PROPOSED_NAME_LABEL

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
    **OFFER_FIELD_LABELS,
}
_DEFAULT_MUTEX = "互斥组按默认填是"
_DEFAULT_HINTS = {
    _DEFAULT_MUTEX, "是否只触发一次按默认填否", "多买多送按默认填是", "生效倍数按默认填 1",
    parse._ATTACHMENTS_HINT, parse._POLICY_TYPE_HINT,
    *ADJUSTMENT_HINTS,
}
_MUTEX_NO = "申请要求不维护互斥组，需人工确认"
_BOUNDARY_HINT = "段落边界无法确定，需人工确认"
_RED_HINTS = {_MUTEX_NO, _BOUNDARY_HINT}
_RULE_PENDING_FIELDS = (
    "policy_name", "trigger_once", "repeat_bonus", "effective_multiplier",
)
_RULE_PENDING_REASON = "取值规则待确认。"
_INPUT_LABELS = {"country": "国家", "company_keyword": "公司关键词", "policy_short_name": "政策短名"}


def _mask_spans(text: str, spans: Iterable[tuple[int, int]]) -> str:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(merged[-1][1], end)
        else:
            merged.append((start, end))
    parts = []
    offset = 0
    for start, end in merged:
        parts.extend((text[offset:start], "[已脱敏]"))
        offset = end
    return "".join((*parts, text[offset:]))


@dataclass(frozen=True, repr=False)
class _ModelText:
    text: str
    role: str
    quoted: bool = False
    start: int | None = None


@dataclass(frozen=True, repr=False)
class _ModelLine(CardLine):
    parts: tuple[str | _ModelText, ...] = field(kw_only=True, repr=False)


class _CardRedaction:
    def __init__(self, body: str, sensitive: re.Pattern[str] | None, registered_fragments: set[str]) -> None:
        self.body = body
        self.sensitive = re.compile(f"(?=({sensitive.pattern}))") if sensitive else None
        self.registered_fragments = registered_fragments
        self.spans = self._matches(self.sensitive, body)
        self.fragments = None
        if self.spans:
            # 自由文本没有坐标。短而又出现在非敏感原文里的字面值无法仅凭字面归属。
            # 四字及以上的复制片段即使在别处重复，也按敏感文字遮蔽。
            public_parts = _mask_spans(body, self.spans).split("[已脱敏]")
            fragments = set()
            for start, end in self.spans:
                for index in range(start, end):
                    for size in range(1, min(4, end - index) + 1):
                        fragment = body[index:index + size]
                        if size == 4 or not any(fragment in part for part in public_parts):
                            fragments.add(fragment)
            if fragments:
                alternatives = "|".join(map(re.escape, sorted(fragments, key=len, reverse=True)))
                self.fragments = re.compile(f"(?=({alternatives}))")

    @staticmethod
    def _matches(pattern: re.Pattern[str] | None, text: str) -> tuple[tuple[int, int], ...]:
        return tuple(match.span(1) for match in pattern.finditer(text)) if pattern else ()

    def _overlaps(self, start: int, length: int) -> Iterable[tuple[int, int]]:
        for left, right in self.spans:
            if start < right and left < start + length:
                yield max(0, left - start), min(length, right - start)

    def display(
        self, text: str, start: int | None = None, *, free_text_spans: Iterable[tuple[int, int]] | None = None,
    ) -> str:
        if not text or self.sensitive is None:
            return text
        if start is not None and self.body.startswith(text, start):
            return _mask_spans(text, self._overlaps(start, len(text)))
        matches = [*self._matches(self.sensitive, text), *self._matches(self.fragments, text)]
        if free_text_spans is not None:
            # 自由文本的短公共片段不能只凭同字摘录归到敏感坐标。
            return _mask_spans(text, (*matches, *free_text_spans))
        length = len(text)
        for left, right in self.spans:
            # 只检查能与敏感坐标相交的原文匹配，保留单字摘录等短片段的坐标保护。
            stop = right + length - 1
            offset = self.body.find(text, max(0, left - length + 1), stop)
            while offset >= 0:
                matches.append((max(0, left - offset), min(length, right - offset)))
                offset = self.body.find(text, offset + 1, stop)
        return _mask_spans(text, matches)

    def free_texts(self, texts: Sequence[str]) -> Iterator[str]:
        if not self.sensitive or not texts:
            yield from texts
            return
        joined = "".join(texts)
        offsets = list(accumulate(map(len, texts), initial=0))
        spans: list[list[tuple[int, int]]] = [[] for _ in texts]
        # 重叠的四字窗口覆盖任意更长的片段，完整值匹配也覆盖短于四字的登记值。
        matches = [*self._matches(self.sensitive, joined), *(
            (start, start + 4) for start in range(len(joined) - 3)
            if joined[start:start + 4] in self.registered_fragments
        )]
        for start, end in matches:
            for index in range(bisect_right(offsets, start) - 1, bisect_left(offsets, end)):
                left, right = offsets[index], offsets[index + 1]
                spans[index].append((max(start, left) - left, min(end, right) - left))
        for text, matches in zip(texts, spans, strict=True):
            yield self.display(text, free_text_spans=matches)


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


def _evidence(excerpts: Iterable[SourceExcerpt], redact: _CardRedaction) -> str:
    return "\n".join(f"「{redact.display(excerpt.text, excerpt.start)}」" for excerpt in excerpts) or "无原文摘录"


@dataclass(frozen=True, repr=False)
class _FreeTextLine(CardLine):
    free_text: str = field(kw_only=True, repr=False)


def _field_lines(
    prefix: str, field: DraftField, explanation: str | None, redact: _CardRedaction, *, offer: bool = False,
) -> Iterable[CardLine]:
    def value_line(label, value, evidence):
        if offer:
            empty = "留空（确定）" if field.uncertainty is None and evidence else "未确定"
            return _value_line(label, value, evidence, empty=empty)
        return CardLine(label, f"值　{redact.display(_value(value))}\n原文出处　{_evidence(evidence, redact)}")

    yield value_line(prefix, field.value, field.evidence)
    if uncertainty := field.uncertainty:
        if explanation:
            yield _FreeTextLine(f"{prefix} · 待确认", f"{uncertainty.reason}\n模型说明　", free_text=explanation)
        else:
            yield CardLine(f"{prefix} · 待确认", uncertainty.reason)
        if not uncertainty.candidates:
            yield CardLine(f"{prefix} · 候选", "无")
        for index, candidate in enumerate(uncertainty.candidates, 1):
            yield value_line(f"{prefix} · 候选 {index}", candidate.value, candidate.evidence)
    elif explanation:
        yield _FreeTextLine(f"{prefix} · 模型说明", "", free_text=explanation)


def _value_parts(value: object, *, nested: bool = False) -> Iterator[str | _ModelText]:
    if isinstance(value, RequestedTier):
        value = value.model_dump(exclude_none=True)
    if isinstance(value, (dict, list)):
        mapping = isinstance(value, dict)
        yield "{" if mapping else "["
        for index, (key, item) in enumerate(value.items() if mapping else enumerate(value)):
            if index:
                yield ", "
            if mapping:
                yield json.dumps(key, ensure_ascii=False) + ": "
            yield from _value_parts(item, nested=True)
        yield "}" if mapping else "]"
    elif isinstance(value, str):
        yield _ModelText(value, "value", quoted=nested)
    else:
        yield _ModelText(json.dumps(value, ensure_ascii=False), "value")


def _value_line(label: str, value: object, evidence: list[SourceExcerpt], *, empty: str = "未确定") -> CardLine:
    parts = [empty] if value is None else list(_value_parts(value))
    parts.append("\n原文出处　")
    for index, excerpt in enumerate(evidence):
        parts.extend(("\n「" if index else "「", _ModelText(excerpt.text, "evidence", start=excerpt.start), "」"))
    if not evidence:
        parts.append("无原文摘录")
    return _ModelLine(label, "值　", parts=tuple(parts))


def _redact_card_lines(lines: Sequence[CardLine], redact: _CardRedaction) -> list[CardLine]:
    """统一遮蔽优惠字段、政策名称和说明，覆盖整卡及连续摘出的值与引文。"""
    parts = []
    for line in lines:
        if isinstance(line, _ModelLine):
            parts.extend(part for part in line.parts if isinstance(part, _ModelText))
        elif isinstance(line, _FreeTextLine):
            parts.append(_ModelText(line.free_text, "advisory"))

    # 每个投影使用原始片段。一个投影先遮掉的字符可能也属于另一个投影的命中。
    groups = [range(len(parts))]
    for roles in (
        {"value", "name_value"}, {"evidence", "name_evidence"},
        {"value"}, {"evidence"}, {"name_value"}, {"name_evidence"}, {"advisory"},
        {"value", "advisory"}, {"value", "name_value", "advisory"},
        {"advisory", "name_value", "name_evidence"},
    ):
        groups.append([i for i, part in enumerate(parts) if part.role in roles])

    def visible(part, text):
        return json.dumps(text, ensure_ascii=False)[1:-1] if part.quoted else text

    raw = [part.text for part in parts]
    rendered = [visible(part, part.text) for part in parts]
    alternatives = [set() for _ in parts]
    representations = (raw,) if raw == rendered else (raw, rendered)
    for group in groups:
        for texts in representations:
            checked = redact.free_texts([texts[i] for i in group])
            for i, text in zip(group, checked, strict=True):
                if text != texts[i]:
                    alternatives[i].add(visible(parts[i], text) if texts is raw else text)

    for i, part in enumerate(parts):
        if part.role != "advisory":
            text = redact.display(part.text, part.start)
            if text != part.text:
                alternatives[i].add(visible(part, text))
    # 多次核对给出不同展示结果时，整段留给人工对照已脱敏的申请原文。
    for i, choices in enumerate(alternatives):
        if choices:
            rendered[i] = choices.pop() if len(choices) == 1 else "[已脱敏]"
    displayed = iter(rendered)
    result = []
    for line in lines:
        if isinstance(line, _FreeTextLine):
            line = CardLine(line.label, line.text + next(displayed), line.red)
        elif isinstance(line, _ModelLine):
            pieces = [line.text]
            for part in line.parts:
                if isinstance(part, str):
                    pieces.append(part)
                else:
                    text = next(displayed)
                    pieces.append(f'"{text}"' if part.quoted else text)
            text = redact.display("".join(pieces))
            line = CardLine(line.label, text, line.red)
        result.append(line)
    return result


@dataclass(frozen=True)
class _Policy:
    draft: PolicyDraft
    hints: tuple[str, ...]
    explanations: Mapping[str, str]
    validation: validate.ValidationResult | None = None
    tier_candidates: tuple[str, ...] = ()
    tier_field: DraftField | None = None
    policy_name_source: str | None = None

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

    def lines(self, prefix: str, redact: _CardRedaction) -> Iterable[CardLine]:
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
                if (item.rule == "R5" and item.field == "policy_name"
                        and self.policy_name_source == PROPOSED_NAME_LABEL):
                    yield CardLine(f"{prefix} · 需同意 · 优惠政策名称", "请产品负责人核对卡片上的名称建议。")
                    continue
                if item.rule == "R5" and _rule_pending(self.draft, item.field):
                    continue
                yield CardLine(f"{prefix} · 需追问 · {_FIELD_LABELS[item.field]}", item.reason)
            for item in result.adjustments:
                if item.rule != "R7":
                    yield CardLine(f"{prefix} · 自动调整 · {_FIELD_LABELS[item.field]}", item.reason)
            for item in result.warnings:
                yield CardLine(f"{prefix} · 提醒项 · {_FIELD_LABELS[item.field]}", item.reason)
        if self.tier_field:
            yield from _field_lines(f"{prefix} · 节目包档位", self.tier_field, self.explanations.get("package_tiers"), redact, offer=True)
        for candidate in self.tier_candidates:
            yield _ModelLine(f"{prefix} · 节目包档位候选", "", parts=(_ModelText(candidate, "value"),))
        if self.tier_candidates and self.tier_field:
            parts = tuple(_ModelText(q.text, "evidence", start=q.start) for q in self.tier_field.evidence)
            yield _ModelLine(f"{prefix} · 节目包档位候选", "档位原文　", parts=parts)
        inactive, required = benefit_applicability(self.draft)
        for field in fields(self.draft):
            value = getattr(self.draft, field.name)
            if (field.name in inactive and value.uncertainty is None) or (
                field.name in OFFER_FIELD_LABELS and field.name not in required
                and value.value is None and value.uncertainty is None and not value.evidence
                and not self.explanations.get(field.name)
            ):
                continue
            label = _FIELD_LABELS[field.name]
            title = f"{prefix} · {label}"
            explanation = self.explanations.get(field.name)
            if field.name == "approval_url" and self.approval_url_deferred:
                yield CardLine(title, "建单时填写")
                if explanation:
                    yield _FreeTextLine(f"{title} · 模型说明", "", free_text=explanation)
            elif field.name == "policy_name":
                name = self.draft.policy_name
                if pending := name.uncertainty:
                    if explanation:
                        yield _FreeTextLine(f"{title} · 待确认", f"{pending.reason}\n模型说明　", free_text=explanation)
                    else:
                        yield CardLine(f"{title} · 待确认", pending.reason)
                    if not pending.candidates:
                        yield CardLine(f"{title} · 候选", "无")
                elif explanation:
                    yield _FreeTextLine(f"{title} · 模型说明", "", free_text=explanation)
                if self.policy_name_source:
                    yield CardLine(f"{title} · 名称来源", self.policy_name_source)
            else:
                yield from _field_lines(title, value, explanation, redact, offer=field.name in OFFER_FIELD_LABELS)
        for name, label in _INPUT_LABELS.items():
            if explanation := self.explanations.get(name):
                yield _FreeTextLine(f"{prefix} · {label} · 模型说明", "", free_text=explanation)

    def name_entries(self, prefix: str) -> Iterable[tuple[str, DraftField | Candidate]]:
        title = f"{prefix} · 优惠政策名称"
        name = self.draft.policy_name
        yield title, name
        if name.uncertainty:
            for index, candidate in enumerate(name.uncertainty.candidates, 1):
                yield f"{title} · 候选 {index}", candidate


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
        benefit_catalog: BenefitCatalog | None = None,
    ) -> None:
        self.model = model
        self.tag_dates = tag_dates
        self.today = today
        self.companies_file = companies_file
        self.legacy_companies = legacy_companies
        self._on_reply = on_reply
        self.benefit_catalog = benefit_catalog
        registered = {value for value in sensitive_values if value}
        values = sorted({
            literal for value in registered
            for literal in (value, json.dumps(value, ensure_ascii=False)[1:-1])
        }, key=len, reverse=True)
        self._sensitive = re.compile("|".join(map(re.escape, values))) if values else None
        self._sensitive_fragments = {
            value[start:start + 4] for value in registered for start in range(len(value) - 3)
        }

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

    def _structure_reminders(
        self, body: str, issues: Iterable[PolicyStructureIssue], sensitive_spans: Sequence[tuple[int, int]],
        display: Callable[[str], str] = str,
    ) -> Iterable[CardLine]:
        for index, item in enumerate(issues, 1):
            span = item.span
            location = f"{display(span.source_id)} [{span.start}, {span.end})" if span else "无可定位坐标"
            location += f"\n{STRUCTURE_REASONS[item.code]}（{item.code}）"
            if span and span.source_id == "body" and span.end <= len(body):
                start, end = span.start, span.end
                crosses_edge = any(left < start < right or left < end < right for left, right in sensitive_spans)
                if not crosses_edge:
                    segment = _mask_spans(body[start:end], (
                        (left - start, right - start) for left, right in sensitive_spans if start <= left and right <= end
                    ))
                    preview, = self._boundary_reminders(segment, [0], {0})
                    location += "\n" + preview.text.removeprefix("段落 1：")
            yield CardLine(f"政策结构问题 {index}", location, red=True)

    def _detail_pages(self, body: str, lines: Sequence[CardLine], redact: _CardRedaction) -> tuple[DetailPage, ...]:
        sections = _redact_card_lines(lines, redact)
        sections.append(CardLine("申请原文", body))
        if self._sensitive:
            sections = [replace(line, text=self._sensitive.sub("[已脱敏]", line.text)) for line in sections]
        return detail_pages("飞书群申请", sections)

    def __call__(self, application: Application, *, additional_blocks=()) -> tuple[DetailPage, ...]:
        body = application.text
        redact = _CardRedaction(body, self._sensitive, self._sensitive_fragments)
        display_body = redact.display(body, 0)

        try:
            company_rules = load_company_rules(
                Path("rules"), self.companies_file or DEFAULT_COMPANIES_FILE, legacy=self.legacy_companies,
            )
            on_reply = None if self._on_reply is None else partial(self._on_reply, application)
            today = self.today()
            drafts = parse.parse_application(
                body, additional_blocks=additional_blocks, model=self.model, on_reply=on_reply, company_rules=company_rules,
                benefit_catalog=self.benefit_catalog, today=today,
            )
            policies = []
            for parsed in drafts:
                draft = _review_mutex(parsed)
                hints = tuple(getattr(parsed, "hints", ()))
                if parsed.maintain_mutual_exclusion_group.value == "否":
                    hints = (*hints, _MUTEX_NO)
                explanations = {
                    name: explanation
                    for name, result in getattr(parsed, "field_results", {}).items()
                    if (explanation := result.statement.explanation) and explanation.strip()
                }
                policies.append(_Policy(draft, hints, explanations,
                                        tier_candidates=getattr(parsed, "package_tier_candidates", ()),
                                        tier_field=getattr(parsed, "tier_field", None),
                                        policy_name_source=getattr(parsed, "policy_name_source", None)))
        except Exception:
            return self._detail_pages(display_body, [
                CardLine(
                    "处理状态",
                    "解析不完整，需人工确认\n停在解析，请核对申请正文或解析配置。未校验。",
                    red=True,
                ),
            ], redact)

        requires_full_review = getattr(drafts, "requires_full_review", False)
        if requires_full_review:
            status = "需人工核对全文\n停在校验。请核对申请全文、政策结构问题和已提取的草稿。未校验。"
        elif not policies:
            status = parse.EMPTY_DRAFTS_STATUS
        else:
            try:
                results = validate.validate_batch(
                    [(policy.draft, _facts(policy.draft, self.tag_dates)) for policy in policies],
                    today=today,
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
            CardLine("处理状态", status, red=requires_full_review),
            *(CardLine("附件待核", label) for label in getattr(drafts, "material_warnings", ())),
            *(_FreeTextLine("没用上的原文段落（对照下方申请原文）", "理由　", free_text=item.reason)
              for item in getattr(drafts, "unused_passages", ())),
            *self._structure_reminders(body, getattr(drafts, "structure_issues", ()), redact.spans, redact.display),
            *(policy.status_line(f"政策 {index}") for index, policy in enumerate(policies, 1)),
            *(line for index, policy in enumerate(policies, 1) for line in policy.lines(f"政策 {index}", redact)),
        ]
        # 说明、名称取值、引文各自连续展示，每段单独交给 W1 的整卡自由文本入口。
        names = [entry for index, policy in enumerate(policies, 1) for entry in policy.name_entries(f"政策 {index}")]
        for title, entry in names:
            if entry.value is None:
                lines.append(CardLine(title, "值　未确定"))
            else:
                lines.append(_ModelLine(title, "值　", parts=(_ModelText(entry.value, "name_value"),)))
        for title, entry in names:
            if not entry.evidence:
                lines.append(CardLine(f"{title} · 原文出处", "无原文摘录"))
            for index, excerpt in enumerate(entry.evidence, 1):
                lines.append(_ModelLine(f"{title} · 原文出处 {index}", "",
                                        parts=(_ModelText(excerpt.text, "name_evidence", start=excerpt.start),)))
        # 追加到既有引文之后，使既有片段仍按原顺序连续参与整卡脱敏。
        for index, policy in enumerate(policies, 1):
            if policy.approval_url_deferred:
                for number, excerpt in enumerate(policy.draft.approval_url.evidence, 1):
                    lines.append(_ModelLine(f"政策 {index} · 任务审批链接 · 原文出处 {number}", "",
                                            parts=(_ModelText(excerpt.text, "evidence", start=excerpt.start),)))
        return self._detail_pages(display_body, lines, redact)
