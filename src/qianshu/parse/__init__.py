"""把结构化的模型声明核对成待校验的政策草稿。"""

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, fields, replace
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import ValidationError

from qianshu.draft import Candidate, DraftField, OfferPolicyDraft, SourceExcerpt, Uncertainty

from . import benefits, country_company, dates, names, other_fields, packages
from .companies import DEFAULT_COMPANIES_FILE, CompanyRules, load_company_rules
from .evidence import EvidenceReview, PolicyStructureIssue, Sources, issue
from .model import ModelError, ModelReplyError, ParseModel, configured_model, validate_reply_json
from .model import load_model_config as load_config
from .protocol import (
    ApplicationReply, Evidence, FieldResult, Finding, Policy, SourceBlock, Span, UnusedPassage,
)

__all__ = ["ModelError", "ParseError", "parse_application"]

EMPTY_DRAFTS_STATUS = "未提取到可办理的政策，请核对原文"

_ATTACHMENTS_HINT = other_fields.ATTACHMENTS_HINT
_POLICY_TYPE_HINT = names._POLICY_TYPE_HINT
_COMPANY_TABLE_HINT = "公司对照表缺失或无效"
_OPTIONS = ("policy_type", "benefit_type")
_BODY_ID = "body"
_CONFIRM = {
    **names.CONFIRMATION_REASONS,
    "invalid_unwritten_shape": "「原文未写」须同时满足空值、无证据和无候选。",
    "manual_rule": other_fields.RULE_PENDING_REASON,
    "mutex_opt_out": other_fields.MUTEX_NO_HINT,
    **benefits.CONFIRMATION_REASONS,
    "policy_type_conflict": "原文政策类型与用户标签判定冲突。",
    "package_title_required": "缺少本条政策的标题，请补充标题并确认当前节目包（R5）。",
    "policy_name_conflict": "原文政策名称与唯一的用户标签冲突。",
    "unique_tag_required": "需要一个确定的用户标签才能取优惠政策名称。",
    "company_lookup_unresolved": "公司对照表无法唯一确定公司。",
    "company_subject_conflict": "明示的公司主体与公司对照表不一致。",
}
_STRUCTURAL_ISSUES = {"invalid_evidence", "invalid_certain_evidence", "tag_evidence_mismatch"}


class ParseError(ValueError):
    """解析规则不可读或模型回复结构非法。"""

    def __init__(self, message: str, *, code: str = "parse_setup", path: tuple = ()):
        super().__init__(message)
        self.code = code
        self.path = path


class ParsedPolicyDraft(OfferPolicyDraft):
    """草稿及其协议证据，供卡片、私有记录和回归入口使用。"""

    hints: list[str]

    source_span: tuple[int, int] | None = None
    source_evidence_spans: tuple[tuple[int, int], ...] = ()
    policy_id: str | None = None
    scope: dict[str, object] | None = None
    field_results: dict[str, FieldResult]

    @property
    def policy_name_source(self) -> str | None:
        result = getattr(self, "field_results", {}).get("policy_name")
        return names.source_label(result) if result is not None else None

    def to_dict(self) -> dict[str, object]:
        return {**super().to_dict(), "hints": getattr(self, "hints", []), "policy_id": self.policy_id,
                "scope": self.scope, "policy_name_source": self.policy_name_source,
                "field_results": {
                    name: {
                        **asdict(result),
                        "statement": result.statement.model_dump(mode="json"),
                        "evidence": [quote.model_dump(mode="json") for quote in result.evidence],
                        "issues": list(result.issues),
                        "input_fields": list(result.input_fields),
                        "program_status": "缺证据" if _STRUCTURAL_ISSUES.intersection(result.issues) else None,
                    }
                    for name, result in getattr(self, "field_results", {}).items()
                }}


class ParseResult(list[ParsedPolicyDraft]):
    def __init__(
        self, drafts: Iterable[ParsedPolicyDraft] = (), *,
        structure_issues: Iterable[PolicyStructureIssue] = (), evidence_locations: Mapping[str, int] | None = None,
        unused_passages: Iterable[UnusedPassage] = (),
    ):
        super().__init__(drafts)
        self.structure_issues = tuple(structure_issues)
        self.evidence_locations = dict(evidence_locations or {})
        self.unused_passages = tuple(unused_passages)

    @property
    def requires_full_review(self) -> bool:
        return bool(self.structure_issues)


def system_message(prompt: str, rules: str, option_table: str) -> str:
    return f"{prompt}\n\n{rules}\n\n选项表（列以制表符分隔）\n\n{option_table}"


def parse_application(
    body: str,
    *,
    config: Mapping[str, str] | None = None,
    model: ParseModel | None = None,
    rules_dir: Path = Path("rules"),
    companies_file: Path = DEFAULT_COMPANIES_FILE,
    companies_text: str | None = None,
    legacy_companies: bool = False,
    on_reply: Callable[[str], None] | None = None,
    company_rules: CompanyRules | None = None,
    additional_blocks: Iterable[SourceBlock] = (),
    benefit_catalog: benefits.BenefitCatalog | None = None,
    today: date | None = None,
    country_codes: Mapping[str, str] | None = None,
    known_policy_names: Iterable[str] = (),
    recorded_reply: bool = False,
) -> ParseResult:
    """解析正文及已转成文本的附件。on_reply 收到私有重放所需的结构化回复。"""
    if recorded_reply and model is None:
        raise ParseError("旧回复投影须提供离线录制。")
    name_date = today if recorded_reply or today is not None else datetime.now(UTC).date()
    attachments = tuple(additional_blocks)
    if not body.strip() and not any(block.text.strip() for block in attachments):
        return ParseResult(structure_issues=(PolicyStructureIssue(None, "empty_material"),))

    try:
        prompt = (rules_dir / "parse-prompt.md").read_text(encoding="utf-8")
        rules = (rules_dir / "parse-rules.md").read_text(encoding="utf-8")
        option_table = (rules_dir / "parse-options.tsv").read_text(encoding="utf-8")
        options = _read_options(option_table)
    except (OSError, UnicodeError, ValueError):
        raise ParseError("申请解析规则不可读或选项表格式无效。") from None
    try:
        if country_codes is None:
            country_codes = names.country_codes((rules_dir / "countries.txt").read_text(encoding="utf-8"))
        if company_rules is None:
            company_rules = load_company_rules(
                rules_dir, companies_file, legacy=legacy_companies, companies_text=companies_text,
            )
    except (OSError, UnicodeError, ValueError):
        raise ParseError("申请解析国家名规则不可读或格式无效。") from None

    try:
        sources = Sources((SourceBlock(_BODY_ID, body, _BODY_ID), *attachments))
    except ValueError:
        raise ParseError("申请输入块映射无效。") from None
    user_input = {"application": body}
    if attachments:
        user_input["attachments"] = [{"id": block.id, "text": block.text} for block in attachments]
    if model is None:
        model = configured_model(config if config is not None else load_config())
    try:
        reply = model(
            system=system_message(prompt, rules, option_table),
            user=json.dumps(user_input, ensure_ascii=False),
        )
    except ModelReplyError as error:
        if on_reply is not None and error.reply is not None:
            on_reply(error.reply)
        raise
    if on_reply is not None and isinstance(reply, str):
        on_reply(reply)
    parsed = _decode(reply)
    review = EvidenceReview(parsed, sources)
    if benefit_catalog is None:
        benefit_catalog = benefits.load_benefit_catalog()
    resolved = [_resolve_fields(policy, review, company_rules, options, country_codes, name_date, benefit_catalog, today)
                for policy in parsed.policies]
    names.check_names([results for results, _ in resolved], known_policy_names)
    drafts = [_draft(policy, review, options, results, hints)
              for policy, (results, hints) in zip(parsed.policies, resolved, strict=True)]
    return ParseResult(drafts, structure_issues=review.structure_issues, evidence_locations=review.evidence_locations,
                       unused_passages=parsed.unused_passages or ())


def _decode(reply: str) -> ApplicationReply:
    try:
        validate_reply_json(reply)
    except (ValueError, TypeError, RecursionError):
        raise ParseError("申请解析模型回复不是有效的政策 JSON。", code="reply_json") from None
    try:
        return ApplicationReply.model_validate_json(reply)
    except ValidationError as error:
        path = error.errors(include_input=False, include_context=False, include_url=False)[0]["loc"]
        raise ParseError("申请解析模型回复结构不符合协议。", code="reply_schema", path=path) from None


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


def _normalized_value(name: str, value: object, options: dict[str, dict[str, str]]) -> object:
    if value is None:
        return None
    if name in dates.FIELDS:
        return dates.calendar_date(value)
    if name == "benefit_type":
        return benefits.benefit_option(value, options[name])
    if name == "policy_type":
        return names.policy_option(value, options[name])
    if name in ("recharge_days", "gift_days") or name in benefits.OFFER_FIELD_LABELS:
        return benefits.normalize_benefit_value(name, value)
    if name == "packages":
        return packages.package_rows(value)
    if name == "approval_url":
        return other_fields.web_url(value)
    if isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, list) and any(not item or item != item.strip() for item in value):
        return None
    return value.strip() if isinstance(value, str) else value


def _resolve_fields(
    policy: Policy, review: EvidenceReview, rules: CompanyRules, options: dict[str, dict[str, str]],
    codes: Mapping[str, str], name_date: date | None, benefit_catalog: benefits.BenefitCatalog, today: date | None,
) -> tuple[dict[str, FieldResult], list[str]]:
    results = {name: review.field(policy, name) for name in policy.fields.__class__.model_fields}
    for name, result in results.items():
        if result.has_value and not result.needs_confirmation:
            value = _normalized_value(name, result.value, options)
            explicit_empty = name in ("approval_url", "penalty_days", "first_recharge_day_only") and result.value is None
            if value is None and not explicit_empty:
                results[name] = issue(replace(result, value=None, has_value=False, source=None), "invalid_value_format")
            else:
                results[name] = replace(result, value=value)

    results["user_tags"] = names.verify_tags(results["user_tags"], review.sources)
    country_company.resolve_country_company(results, rules)
    dates.verify_dates(results)
    benefit_hints = benefits.apply_benefit_rules(results, benefit_catalog, today, policy.fields.model_fields_set)
    hints = [
        *names.derive_names(results, codes, name_date),
        *benefit_hints,
        *other_fields.apply_defaults(results),
    ]
    packages.resolve_packages(results, has_title=policy.has_title)
    if rules.table is None:
        hints.append(_COMPANY_TABLE_HINT)

    return results, hints


def _draft(
    policy: Policy, review: EvidenceReview, options: dict[str, dict[str, str]],
    results: dict[str, FieldResult], hints: list[str],
) -> ParsedPolicyDraft:
    draft = ParsedPolicyDraft(**{
        field.name: _project(results, field.name, review, options)
        for field in fields(OfferPolicyDraft)
    })
    draft.policy_id = policy.id
    corrected = policy.model_copy(update={
        "main": review.corrected_span(policy.main) or policy.main,
        "continuations": [review.corrected_span(span) or span for span in policy.continuations],
    })
    draft.scope = corrected.model_dump(mode="json", exclude={"fields"})
    draft.hints = hints
    draft.field_results = results
    draft.source_span = _body_offsets(review.main[id(policy)])
    draft.source_evidence_spans = tuple(sorted({
        # 短名参与拼名时，其引文已在 policy_name 中。独立短名不参与位置量尺。
        span for name, result in results.items() if name != "policy_short_name"
        for quote in result.evidence
        if (span := _body_offsets(review.sources.locate(quote.span))) is not None
    }))
    return draft


def _body_offsets(span: Span | None) -> tuple[int, int] | None:
    return (span.start, span.end) if span is not None and span.source_id == _BODY_ID else None


def _excerpt(quote: Evidence, sources: Sources) -> SourceExcerpt:
    offsets = _body_offsets(sources.locate(quote.span))
    return SourceExcerpt(text=quote.text, start=offsets[0] if offsets is not None else None)


def _project(results: dict[str, FieldResult], name: str, review: EvidenceReview, options) -> DraftField:
    result = results[name]
    evidence = [_excerpt(quote, review.sources) for quote in result.evidence]
    if name == "policy_name" and names.is_proposal(result):
        return DraftField(evidence=evidence, uncertainty=Uncertainty(
            reason="名称建议待产品负责人在群卡片上同意。",
            candidates=[Candidate(value=result.value, evidence=evidence)],
        ))
    if not result.needs_confirmation:
        return DraftField(value=result.value if result.has_value else None, evidence=evidence)

    candidates = []
    statement: Finding = result.statement
    for candidate in statement.candidates:
        value = _normalized_value(name, candidate.value, options)
        verified = [review.verified_quote(quote) for quote in candidate.evidence]
        if value is not None and verified and None not in verified:
            candidates.append(Candidate(value=value, evidence=[_excerpt(q, review.sources) for q in verified]))
    if statement.value is not None and evidence:
        value = _normalized_value(name, statement.value, options)
        if value is not None:
            candidates.append(Candidate(value=value, evidence=evidence))
    if "policy_type_conflict" in result.issues:
        tag_evidence = [_excerpt(quote, review.sources) for quote in results["user_tags"].evidence]
        candidates.append(Candidate(value=result.value, evidence=tag_evidence))
    reason = next((_CONFIRM[code] for code in result.issues if code in _CONFIRM), None)
    if reason is None:
        if _STRUCTURAL_ISSUES.intersection(result.issues):
            reason = "缺证据：逐字引文核对未通过。"
        elif result.issues:
            reason = "需核对字段取值及适用范围。"
        else:
            reason = f"原文状态：{statement.status.value}。"
    unique = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return DraftField(evidence=evidence, uncertainty=Uncertainty(reason=reason, candidates=unique))
