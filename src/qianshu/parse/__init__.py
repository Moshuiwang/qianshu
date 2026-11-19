"""把申请正文解析成待校验的政策草稿。"""

import json
import re
from calendar import month_abbr, month_name
from collections.abc import Callable, Iterator, Mapping
from dataclasses import fields
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from qianshu.config import load_config
from qianshu.draft import Candidate, DraftField, PackageRow, PolicyDraft, SourceExcerpt, Uncertainty

from .model import ModelError, ParseModel, configured_model
from .scope import (
    CHINESE_DURATION_NUMBERS, DATE_PERIOD, DATE_TEXT, DURATION, PERIOD_CLAUSE_BREAKS, PolicyReference, Scope,
    clause_spans, company_mentioned, country_matches, duration_in_context, policy_scopes, quoted_company_heading,
    read_countries, without_batch_intro, without_list_prefix,
)

__all__ = ["ModelError", "ParseError", "parse_application"]

_FIELD_NAMES = {field.name for field in fields(PolicyDraft)}
_G152_FIELDS = {"policy_name", "trigger_once", "repeat_bonus", "effective_multiplier", "approval_attachments"}
_ATTACHMENTS_HINT = "审批件按默认留空"
_POLICY_TYPE_HINT = "政策类型按用户标签判定"
_POLICY_TYPE_STATEMENT = re.compile(
    r"(?:(?:本条|本|该条|该)?(?:政策|申请)类型\s*(?:[:：=]|为|是)\s*|"
    r"(?:(?:本次|本条|本|该条|该)(?:申请|活动|政策)?|这)\s*(?:均为|为|是|属于|定为)\s*|"
    r"(?:均为|为|是)\s*)?"
    r"(定向(?:政策)?|公开(?:政策)?)(?:\s*[（(][^）)]*[）)])?(?:\s*(?:并|且|适用|面向).+)?"
)
_POLICY_TYPE_HEADING = re.compile(r"[^：:，,。；;]{0,60}?(定向|公开)政策申请\s*[:：]")
_POLICY_TYPE_ALIASES = {"定向": "定向政策", "公开": "公开政策"}
_POLICY_NAME_LABEL = r"(?:优惠)?政策(?:名称|名)"
_POLICY_NAME_SEPARATOR = r"\s*(?:[:：=]|为|是)\s*"
_POLICY_NAME_MENTION = re.compile(rf"^{_POLICY_NAME_LABEL}|{_POLICY_NAME_LABEL}{_POLICY_NAME_SEPARATOR}")
_POLICY_NAME_ANSWER = re.compile(rf"(?:本条|本|该条|该)?{_POLICY_NAME_LABEL}{_POLICY_NAME_SEPARATOR}(.+)")
_ONCE = r"触发\s*[一1１]\s*次"
_ONCE_MENTION = re.compile(rf"{_ONCE}|(?:只|仅|限)\s*(?:能|可)?\s*触发|触发次数")
_ONCE_NO = re.compile(
    rf"(?:是否)?只\s*{_ONCE}\s*[:：=]\s*否|(?:不只|不止|不限于)\s*{_ONCE}|不限制?\s*触发次数"
)
_REPEAT = r"多买多[赠送]"
_REPEAT_SUBJECT = r"(?:(?:本(?:条)?政策|该(?:条)?政策|本活动|用户)\s*(?:均\s*)?)?"
_REPEAT_NEGATIVE = r"不允许|禁止|不可|不支持|不可以|不得|不能"
_REPEAT_MENTION = re.compile(rf"{_REPEAT}|多买不多[赠送]")
_REPEAT_YES = re.compile(rf"{_REPEAT_SUBJECT}(?:(?:允许|支持|可以|可)\s*)?{_REPEAT}|{_REPEAT}\s*[:：=]\s*是")
_REPEAT_NO = re.compile(
    rf"{_REPEAT_SUBJECT}(?:{_REPEAT_NEGATIVE})\s*{_REPEAT}|{_REPEAT}\s*(?:[:：=]\s*否|(?:[:：=]\s*)?(?:{_REPEAT_NEGATIVE}))"
    r"|多买不多[赠送]"
)
_MULTIPLIER_MENTION = re.compile(r"倍数|[\d零〇一二两三四五六七八九十百半]+\s*倍")
_ONE = r"(?:[1１](?:[.．][0０]+)?|一)"
_MULTIPLIER_ONE = re.compile(
    rf"(?:生效)?倍数\s*(?:[:：=]|为|是)?\s*{_ONE}\s*倍?|{_ONE}\s*倍|(?:[以按]\s*)?{_ONE}\s*倍\s*生效"
)
_RULE_QUALIFIER = re.compile(r"[\s，,、；;]*(?:暂定|待定|待确认|需确认|尚未确认|视情况|如有|如果|若)")
_DATES = {"start_date", "end_date"}
_OPTIONS = {"policy_type", "benefit_type"}
_MUTEX = "maintain_mutual_exclusion_group"
_MUTEX_NO_HINT = "申请要求不维护互斥组，需人工确认"
_BOUNDARY_HINT = "段落边界无法确定，需人工确认"
_COMPANY_TABLE_HINT = "公司对照表缺失或无效"
_DEFAULT_COMPANIES_FILE = Path(".private/companies.tsv")
_COMPANY_SUBJECT = re.compile(
    r"(?:(?:适用|申请)?(?:公司(?:名称)?|主体))\s*(?:[:：=]|为|是)\s*(.+)"
)
_COMPANY_SUBJECT_SEPARATOR = re.compile(r"\s*(?:或者?|以及|、|\s+[和与及]\s+)\s*")
type _CompanyTable = dict[str, dict[str, str]]
_MUTEX_NEGATIVE = r"不需要|不需|无需|不用"
_MUTEX_ANSWER = re.compile(
    r"(?:是否(?:需要)?|需不需要|需要)?(?:维护)?互斥组\s*[:：=]\s*"
    rf"(是|否|(?:{_MUTEX_NEGATIVE})(?:\s*维护(?:\s*互斥组)?)?|不\s*维护(?:\s*互斥组)?)"
    r"(?=$|\s|[（(.])"
)
_MUTEX_NO = re.compile(rf"(?:{_MUTEX_NEGATIVE}|不)\s*维护\s*互斥组")
_MUTEX_YES = re.compile(r"(?:需要|需|须|必须)维护互斥组")
_MUTEX_BREAKS = "。；;，,！!？?\r\n"
_DATE_LABELS = {
    "start_date": r"(?:申请|政策|活动)?(?:开始|起始)(?:日期|时间|日)?|"
                  r"(?:(?:application|policy|campaign)\s+)?start(?:\s+date)?|(?:活动)?自",
    "end_date": r"(?:申请|政策|活动)?(?:结[束止](?:日期|时间|日)?|截止(?:日期|时间|日)?)|"
                r"(?:(?:application|policy|campaign)\s+)?end(?:\s+date)?",
}
_LABEL_BOUNDARY = r"(?<![^\W_])"
_DATE_SEPARATOR = r"\s*[:：]?\s*"
_DATE_CLAUSE_BREAK = re.compile(r"[。！!？?；;]")
_DATE_PARENT = re.compile(
    r"(?:申请|政策|活动)(?:日期|时间|起止(?:日期|时间))?\s*[:：]|"
    r"(?:application|policy|campaign)(?:\s+dates?)?\s*[:：]",
    re.IGNORECASE,
)
_DATE_LINE = re.compile(
    rf"(?:{_DATE_LABELS['start_date']}|{_DATE_LABELS['end_date']}){_DATE_SEPARATOR}{DATE_TEXT}",
    re.IGNORECASE,
)
_INACTIVE_PERIOD = re.compile(r"作废|废止|停用|取消|撤销|参考|原定|旧|暂定|待定|罚停|停机")
_APPLICATION_HEADING = re.compile(r"[^：:，,。；;]{1,60}(?:定向|通用)?政策申请\s*[:：]")
_DAYS_ACTION = {"recharge_days": {"充", "充值"}, "gift_days": {"赠送", "赠", "送"}}
_MONTH_HINT = "充值 / 赠送天数按 1 月 = 30 天换算"
_ARCHIVE_LABEL = re.compile(
    rf"{_LABEL_BOUNDARY}(?:(?:本批(?:次)?|全批)(?:的)?)?"
    r"(?:存档|归档)(?:文档(?:链接)?|链接|网址|地址)?"
    r"\s*(?:见|为|在)?\s*[:：]?\s*(?:\[[^\]\r\n]+\]\()?$"
)
_WEB_LINK = re.compile(r"https?://[^\s，。；！？、)）】」]+")
_NO_GIFT_PACKAGE = re.compile(
    r"(?<![非并])无赠送(?:节目)?包|(?:不赠送|没有赠送)(?:节目)?包|赠送(?:节目)?包\s*[:：=]\s*无"
)
_TITLE_PACKAGE = re.compile(r"(.+?)\s*包\s*用户(?:\s*[（(].*[）)])?")
_GIFT_PACKAGE_LABEL = re.compile(r"赠送(?:节目)?包(?:为|是|\s*[:：=])\s*([^，,。；;\r\n]+)")
_PACKAGE_SEPARATOR = re.compile(r"、|[,，/／]|(?<=包)[和及与]|(?<=[A-Za-z0-9])[和及与](?=[A-Za-z0-9])|\s+(?:和|及|与|and)\s+")
_PACKAGE_CLAUSE_END = r"[,，][ \t]*(?=(?:[^,，:：=\r\n]{1,12}[:：=]|(?:用户)?充|(?:当前|赠送)(?:节目)?包(?:为|是)|允许|不限制))"
_CURRENT_PACKAGE_LABEL = re.compile(
    rf"(?:当前节目包|当前包)(?:为|是|\s*[:：=])[ \t]*([^。；;\r\n]+?)(?=$|[。；;\r\n]|{_PACKAGE_CLAUSE_END})"
)
_INTRO = re.compile(r"[^\[\]{}！!？?。.:：\u0060\r\n]+[！!？?。.:：]?(?:[ \t]*\r?\n|(?<=[！!？?。.:：])[ \t]*)")
_FENCE = re.compile(r"\u0060{3}(?:json)?[ \t]*\r?\n(.*?)\r?\n[ \t]*\u0060{3}", re.DOTALL | re.IGNORECASE)


class ParseError(ValueError):
    """解析规则不可读，或模型回复不符合结构约定。"""


class ParsedPolicyDraft(PolicyDraft):
    """解析入口返回的草稿，附带可显示在自动调整中的提示。"""

    _converted_months = False
    _company_table_unavailable = False

    @property
    def hints(self) -> list[str]:
        mutex = self.maintain_mutual_exclusion_group
        hints = []
        if mutex.value == "是" and not mutex.evidence:
            hints.append("互斥组按默认填是")
        if mutex.uncertainty is not None:
            if mutex.uncertainty.reason == _BOUNDARY_HINT:
                hints.append(_BOUNDARY_HINT)
            if any(candidate.value == "否" for candidate in mutex.uncertainty.candidates):
                hints.append(_MUTEX_NO_HINT)
        if self._converted_months:
            hints.append(_MONTH_HINT)
        if self._company_table_unavailable:
            hints.append(_COMPANY_TABLE_HINT)
        if self.trigger_once.value == "否":
            hints.append("是否只触发一次按默认填否")
        if self.repeat_bonus.value == "是" and not self.repeat_bonus.evidence:
            hints.append("多买多送按默认填是")
        if self.effective_multiplier.value == 1 and not self.effective_multiplier.evidence:
            hints.append("生效倍数按默认填 1")
        if self.approval_attachments.value == [] and self.approval_attachments.uncertainty is None:
            hints.append(_ATTACHMENTS_HINT)
        if self.policy_type.value is not None and self.policy_type.uncertainty is None:
            hints.append(_POLICY_TYPE_HINT)
        return hints

    def to_dict(self) -> dict[str, object]:
        return {**super().to_dict(), "hints": self.hints}


def parse_application(
    body: str,
    *,
    config: Mapping[str, str] | None = None,
    model: ParseModel | None = None,
    rules_dir: Path = Path("rules"),
    companies_file: Path = _DEFAULT_COMPANIES_FILE,
    on_reply: Callable[[str], None] | None = None,
) -> list[ParsedPolicyDraft]:
    """接收申请正文并返回草稿。on_reply 在校验模型回复前收到原始文本。"""
    if not body.strip():
        return []

    try:
        prompt = (rules_dir / "parse-prompt.md").read_text(encoding="utf-8")
        rules = (rules_dir / "parse-rules.md").read_text(encoding="utf-8")
        option_table = (rules_dir / "parse-options.tsv").read_text(encoding="utf-8")
        options = _read_options(option_table)
    except (OSError, UnicodeError, ValueError):
        raise ParseError("申请解析规则不可读或选项表格式无效。") from None
    try:
        countries = read_countries((rules_dir / "countries.txt").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise ParseError("申请解析国家名规则不可读或格式无效。") from None
    try:
        companies = _read_companies(companies_file.read_text(encoding="utf-8"), countries)
    except (OSError, UnicodeError, ValueError):
        companies = None

    if model is None:
        model = configured_model(config if config is not None else load_config())
    reply = model(
        system=f"{prompt}\n\n{rules}\n\n选项表（列以制表符分隔）\n\n{option_table}",
        user=json.dumps({"application": body}, ensure_ascii=False),
    )
    if on_reply is not None:
        on_reply(reply)

    try:
        policies = json.loads(_unwrap(reply), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise ParseError("申请解析模型回复不是有效的政策 JSON。") from None
    if not isinstance(policies, list):
        raise ParseError("申请解析模型回复应为政策数组。")

    references = []
    for policy in policies:
        _check_object(policy, _FIELD_NAMES)
        if not policy:
            raise ParseError("申请解析模型回复包含空白政策。")
        company = policy.get("company", {})
        _check_object(company, {"value", "evidence", "candidates"})
        quotes = company.get("evidence", [])
        _check_quotes(quotes)
        heading_quotes = list(quotes)
        company_quotes = list(quotes)
        for item in _candidate_items(company):
            candidate_quotes = item.get("evidence", [])
            _check_quotes(candidate_quotes)
            company_quotes.extend(candidate_quotes)
            heading_quotes.extend(candidate_quotes)
        for raw in policy.values():
            if isinstance(raw, dict) and isinstance(raw.get("evidence"), list):
                heading_quotes.extend(quote for quote in raw["evidence"] if isinstance(quote, str))
        references.append(PolicyReference(
            quoted_company_heading(body, company_quotes, countries),
            country_matches(company_quotes, countries),
            heading_quotes,
        ))

    drafts = []
    company_names = {country: list(rows.values()) for country, rows in (companies or {}).items()}
    scopes = policy_scopes(body, references, countries, company_names)
    for policy, scope in zip(policies, scopes, strict=True):
        draft_fields = {
            name: _draft_field(
                name, raw=policy.get(name, {}), body=body, scope=scope, options=options.get(name, {}),
            )
            for name in sorted(_FIELD_NAMES - {_MUTEX, "company", "policy_type"} - _G152_FIELDS)
        }
        for name in sorted(_G152_FIELDS):
            _validate_field_shape(policy.get(name, {}))
        draft_fields.update(_g152_fields(body, scope, draft_fields["user_tags"]))
        draft_fields["policy_type"] = _policy_type(
            policy.get("policy_type", {}), body, scope, draft_fields["user_tags"], options["policy_type"],
        )
        draft_fields["company"] = _company(body, scope, countries, companies)
        raw_mutex = policy.get(_MUTEX, {})
        _validate_field_shape(raw_mutex)
        draft = ParsedPolicyDraft(**draft_fields, maintain_mutual_exclusion_group=_mutex(body, scope))
        draft._company_table_unavailable = companies is None
        draft._converted_months = any(
            (field := draft_fields[name]).value is not None
            and _days_unit(name, field.value, [excerpt.text for excerpt in field.evidence], body, scope) == "月"
            for name in _DAYS_ACTION
        )
        drafts.append(draft)
    return drafts


def _unwrap(reply: str) -> str:
    reply = reply.strip()
    if reply.startswith("["):
        return reply
    if intro := _INTRO.match(reply):
        opening = intro[0].strip()
        payload = reply[intro.end():].lstrip()
        if (
            opening[0].isalpha()
            and opening not in {"null", "true", "false", "NaN", "Infinity"}
            and payload.startswith(("[", "\x60" * 3))
        ):
            reply = payload
    if fence := _FENCE.fullmatch(reply):
        return fence[1]
    return reply


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


def _read_companies(table: str, countries: Mapping[str, object]) -> _CompanyTable:
    companies: _CompanyTable = {}
    for line in table.splitlines():
        if line.lstrip().startswith("#") or ("\t" not in line and not line.strip()):
            continue
        cells = [cell.strip() for cell in line.split("\t")]
        if len(cells) not in (2, 3) or not all(cells):
            raise ValueError
        country, name, *keywords = cells
        if country not in countries:
            raise ValueError
        keyword = keywords[0].casefold() if keywords else ""
        rows = companies.setdefault(country, {})
        if keyword in rows:
            raise ValueError
        rows[keyword] = name
    if not companies:
        raise ValueError
    return companies


def _company(
    body: str, scope: Scope, countries: dict[str, re.Pattern[str]], companies: _CompanyTable | None,
) -> DraftField[str]:
    if companies is None:
        return DraftField(uncertainty=Uncertainty(reason=_COMPANY_TABLE_HINT))
    if scope.boundary_uncertain:
        return DraftField(uncertainty=Uncertainty(reason=_BOUNDARY_HINT))
    if len(scope.countries) != 1:
        return DraftField(uncertainty=Uncertainty(reason="原文无法确定国家。"))
    country, = scope.countries
    source = next((
        SourceExcerpt(text=body[left:right], start=left)
        for left, right in scope.country_sources
        if country_matches([body[left:right]], countries) == {country}
    ), None)
    evidence = [source] if source is not None else []
    rows = companies.get(country, {})
    # 批次共享说明也可能出现在首条政策标题之前。
    spans = scope.shared if scope.local is None else (scope.local, *scope.shared)
    excerpts = [
        SourceExcerpt(text=line[0], start=left + line.start())
        for left, right in sorted(set(spans))
        for line in re.finditer(r"[^\r\n]+", body[left:right])
    ]
    matches = {}
    for keyword in rows:
        if not keyword:
            continue
        for quote in excerpts:
            if company_mentioned(keyword, [quote.text.casefold()]):
                matches[keyword] = quote
                if quote not in evidence:
                    evidence.append(quote)
                break
    if len(matches) > 1:
        return DraftField(evidence=evidence, uncertainty=Uncertainty(
            reason="该国有多个公司关键词命中。",
            candidates=[Candidate(value=rows[keyword], evidence=[quote]) for keyword, quote in matches.items()],
        ))
    if matches:
        keyword, = matches
        name = rows[keyword]
        if conflict := _company_subject_conflict(body, scope, excerpts, countries, country, keyword, name):
            if conflict not in evidence:
                evidence.append(conflict)
            return DraftField(evidence=evidence, uncertainty=Uncertainty(reason="公司关键词与原文明确主体冲突。"))
        return DraftField(value=name, evidence=evidence)
    if default := rows.get(""):
        return DraftField(value=default, evidence=evidence)
    return DraftField(evidence=evidence, uncertainty=Uncertainty(reason="公司对照表未能确定该国公司。"))


def _company_subject_conflict(
    body: str, scope: Scope, excerpts: list[SourceExcerpt], countries: dict[str, re.Pattern[str]],
    country: str, keyword: str, name: str,
) -> SourceExcerpt | None:
    for quote in excerpts:
        for left, right in clause_spans(quote.text, punctuation="。；;，,！!？?"):
            clause = quote.text[left:right]
            batch_clause = without_batch_intro(clause)
            is_local_heading = left == 0 and scope.local is not None and quote.start == scope.local[0]
            if label := _COMPANY_SUBJECT.search(clause):
                subjects = _COMPANY_SUBJECT_SEPARATOR.split(label[1])
            elif is_local_heading and scope.title and (heading := quoted_company_heading(body, [scope.title], countries)):
                subjects = [heading]
            elif batch_clause != clause and (
                heading := quoted_company_heading(body, [clause], countries)
                or quoted_company_heading(body, [batch_clause], countries)
            ):
                subjects = [heading]
            else:
                continue
            for subject in subjects:
                subject = subject.strip()
                if (
                    subject and subject.casefold() != name.casefold()
                    and not company_mentioned(keyword, [subject.casefold()])
                    and country_matches([subject], countries, exact=True) != {country}
                ):
                    return quote
    return None


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


def _check_quotes(quotes: object) -> None:
    if not isinstance(quotes, list) or any(not isinstance(quote, str) for quote in quotes):
        raise ParseError("申请解析模型回复的原文依据应为字符串数组。")


def _candidate_items(raw: dict) -> list[dict]:
    proposed = raw.get("candidates", [])
    if not isinstance(proposed, list):
        raise ParseError("申请解析模型回复的候选值应为数组。")
    for item in proposed:
        _check_object(item, {"value", "evidence"})
    return proposed


def _validate_field_shape(raw: dict) -> None:
    _check_object(raw, {"value", "evidence", "candidates"})
    _check_quotes(raw.get("evidence", []))
    for item in _candidate_items(raw):
        _check_quotes(item.get("evidence", []))


def _g152_fields(body: str, scope: Scope, tags: DraftField[list[str]]) -> dict[str, DraftField]:
    if scope.boundary_uncertain:
        return {name: DraftField(uncertainty=Uncertainty(reason=_BOUNDARY_HINT)) for name in _G152_FIELDS}

    scoped_statements = {}
    for left, right in scope.spans():
        for start, end in clause_spans(body[left:right], punctuation="。；;，,、！!\r\n"):
            raw = body[left + start:left + end]
            clause = raw.strip()
            if not clause:
                continue
            statement = without_list_prefix(without_batch_intro(without_list_prefix(clause))).strip()
            excerpt = SourceExcerpt(text=clause, start=left + start + len(raw) - len(raw.lstrip()))
            qualified = bool(_RULE_QUALIFIER.match(body, left + end, right))
            key = (excerpt.start, excerpt.text)
            if key not in scoped_statements or qualified:
                scoped_statements[key] = (statement.removesuffix(".").rstrip(), excerpt, qualified)

    statements = list(scoped_statements.values())
    explicit_names = [
        (text, excerpt, qualified)
        for text, excerpt, qualified in statements if _POLICY_NAME_MENTION.search(text)
    ]
    if tags.value is None or len(tags.value) != 1 or tags.uncertainty is not None:
        policy_name = DraftField(uncertainty=Uncertainty(reason="用户标签未能确定为唯一一个。"))
    elif any(
        qualified or (answer := _POLICY_NAME_ANSWER.fullmatch(text)) is None or answer[1] != tags.value[0]
        for text, _, qualified in explicit_names
    ):
        policy_name = DraftField(
            evidence=[excerpt for _, excerpt, _ in explicit_names],
            uncertainty=Uncertainty(reason="原文政策名称不明确或与唯一用户标签冲突。"),
        )
    else:
        policy_name = DraftField(value=tags.value[0], evidence=tags.evidence)

    return {
        "policy_name": policy_name,
        "trigger_once": _g152_value(statements, _ONCE_MENTION, {"否": _ONCE_NO}, "否"),
        "repeat_bonus": _g152_value(statements, _REPEAT_MENTION, {"是": _REPEAT_YES, "否": _REPEAT_NO}, "是"),
        "effective_multiplier": _g152_value(statements, _MULTIPLIER_MENTION, {1: _MULTIPLIER_ONE}, 1),
        "approval_attachments": DraftField(value=[]),
    }


def _policy_type(
    raw: dict, body: str, scope: Scope, tags: DraftField[list[str]], options: dict[str, str],
) -> DraftField[str]:
    _validate_field_shape(raw)
    if scope.boundary_uncertain:
        return DraftField(uncertainty=Uncertainty(reason=_BOUNDARY_HINT))
    if tags.uncertainty is not None or tags.value is None:
        return DraftField(uncertainty=Uncertainty(reason="用户标签尚未确定，无法判定政策类型。"))

    expected = "定向政策" if tags.value else "公开政策"
    claims: dict[str, list[SourceExcerpt]] = {}
    for item in [raw, *raw.get("candidates", [])]:
        value = item.get("value")
        if not _text(value):
            continue
        value = options.get(value, _POLICY_TYPE_ALIASES.get(value, value))
        evidence, _ = _evidence(body, scope, item.get("evidence", []))
        terms = {value, *(term for term, option in (options | _POLICY_TYPE_ALIASES).items() if option == value)}
        known = claims.setdefault(value, [])
        known.extend(
            excerpt for excerpt in evidence if excerpt not in known and any(term in excerpt.text for term in terms)
        )

    for left, right in scope.spans():
        for start, end in clause_spans(body[left:right], punctuation="。；;，,！!？?\r\n"):
            raw_clause = body[left + start:left + end]
            clause = raw_clause.strip()
            statement = without_list_prefix(without_batch_intro(without_list_prefix(clause))).strip()
            if match := (
                _POLICY_TYPE_STATEMENT.fullmatch(statement.removesuffix("."))
                or _POLICY_TYPE_HEADING.fullmatch(statement)
            ):
                value = _POLICY_TYPE_ALIASES.get(match[1], match[1])
                excerpt = SourceExcerpt(text=clause, start=left + start + len(raw_clause) - len(raw_clause.lstrip()))
                known = claims.setdefault(value, [])
                if excerpt not in known:
                    known.append(excerpt)

    conflicts = [
        Candidate(value=value, evidence=evidence) for value, evidence in claims.items() if value != expected
    ]
    if conflicts:
        return DraftField(uncertainty=Uncertainty(
            reason="申请或模型给出的政策类型与用户标签判定冲突。",
            candidates=[Candidate(value=expected, evidence=tags.evidence), *conflicts],
        ))
    return DraftField(value=expected, evidence=claims.get(expected) or tags.evidence)


def _g152_value(
    statements: list[tuple[str, SourceExcerpt, bool]], mention: re.Pattern[str],
    choices: dict[str | int, re.Pattern[str]], default: str | int,
) -> DraftField:
    evidence = []
    answers = set()
    for text, excerpt, qualified in statements:
        if mention.search(text):
            evidence.append(excerpt)
            answer = None if qualified else next(
                (value for value, pattern in choices.items() if pattern.fullmatch(text)), None,
            )
            answers.add(answer)
    if not evidence:
        return DraftField(value=default)
    if len(answers) == 1 and None not in answers:
        value, = answers
        return DraftField(value=value, evidence=evidence)
    return DraftField(evidence=evidence, uncertainty=Uncertainty(reason="原文说明有冲突或无法确认。"))


def _mutex(body: str, scope: Scope) -> DraftField:
    answers: dict[str, list[SourceExcerpt]] = {}
    excerpts: dict[tuple[int, int], SourceExcerpt] = {}
    unresolved = False
    for left, right in scope.spans():
        for start, end in clause_spans(body[left:right], punctuation=_MUTEX_BREAKS):
            raw_clause = body[left + start:left + end]
            clause = raw_clause.strip()
            if "互斥组" not in clause:
                continue
            statement = without_list_prefix(without_batch_intro(without_list_prefix(clause))).strip()
            if answer := _MUTEX_ANSWER.match(statement):
                value = "是" if answer[1] == "是" else "否"
            elif answer := _MUTEX_NO.match(statement):
                value = "否"
            elif answer := _MUTEX_YES.match(statement):
                value = "是"
            else:
                value = None
            start += left + len(raw_clause) - len(raw_clause.lstrip())
            end += left
            bare = answer is not None and answer[0] == statement.removesuffix(".").rstrip()
            supplement = _mutex_supplement(body[end:right]) if value == "否" else 0
            clause = body[start:end + supplement].rstrip()
            excerpt = excerpts.setdefault((start, start + len(clause)), SourceExcerpt(text=clause, start=start))
            unresolved |= not bare
            if value is not None:
                evidence = answers.setdefault(value, [])
                if excerpt not in evidence:
                    evidence.append(excerpt)

    if not scope.boundary_uncertain and not unresolved:
        if not answers:
            return DraftField(value="是")
        if answers.keys() == {"是"}:
            return DraftField(value="是", evidence=answers["是"])
    return DraftField(
        evidence=list(excerpts.values()),
        uncertainty=Uncertainty(
            reason=_BOUNDARY_HINT if scope.boundary_uncertain else (
                _MUTEX_NO_HINT if "否" in answers else "原文对互斥组的说明有保留、存在矛盾或无法确认。"
            ),
            candidates=[Candidate(value=value, evidence=evidence) for value, evidence in answers.items()],
        ),
    )


def _mutex_supplement(tail: str) -> int:
    """保留紧接否定答复的问号、同一行补充说明或独立括注。"""
    prefix = re.match(r"(?:[。；;.!！?？]?\s*(?=[（(])|[，,；;][ \t]*)", tail)
    if prefix is None:
        return int(tail.startswith(("?", "？")))
    rest = tail[prefix.end():]
    if without_list_prefix(rest) != rest:
        return 0
    span = next(clause_spans(rest, punctuation="。；;！!？?\r\n"), None)
    if span is None or span[0] != 0:
        return 0
    end = span[1]
    if tail.startswith(("；", ";")):
        if re.match(r"[^，,（(]*[:：=]", rest[:end]) or "互斥组" in rest[:end]:
            return 0
        if rest[end:end + 1] in ("。", "！", "!", "？", "?"):
            end += 1
    return prefix.end() + end


def _evidence(body: str, scope: Scope, quotes: object, *, dates: bool = False) -> tuple[list[SourceExcerpt], bool]:
    _check_quotes(quotes)
    evidence = []
    for quote in dict.fromkeys(quotes):
        if excerpt := scope.excerpt(body, quote, dates=dates):
            evidence.append(excerpt)
    return evidence, len(evidence) == len(set(quotes))


def _draft_field(
    name: str, *, raw: dict, body: str, scope: Scope, options: dict[str, str],
) -> DraftField:
    _check_object(raw, {"value", "evidence", "candidates"})
    value_evidence, complete = _evidence(body, scope, raw.get("evidence", []), dates=name in _DATES)
    proposed = _candidate_items(raw)
    if name == "packages" and not scope.boundary_uncertain:
        if package_result := _title_package_field(body, scope, raw, proposed):
            for item in proposed:
                _check_quotes(item.get("evidence", []))
            return package_result
    evidence = list(value_evidence)
    value = _value(name, raw.get("value"))
    if name in _OPTIONS and value is not None:
        value = options.get(value, value)

    candidates = []
    for item in proposed:
        candidate_evidence, candidate_complete = _evidence(
            body, scope, item.get("evidence", []), dates=name in _DATES,
        )
        candidate_value = _value(name, item.get("value"))
        if name in _OPTIONS and candidate_value is not None:
            candidate_value = options.get(candidate_value, candidate_value)
        for excerpt in candidate_evidence:
            if excerpt not in evidence:
                evidence.append(excerpt)
        if candidate_complete and _supports_value(
            name, candidate_value, candidate_evidence, options, body=body, scope=scope, candidate=True,
        ):
            candidate = Candidate(value=candidate_value, evidence=candidate_evidence)
            if candidate not in candidates:
                candidates.append(candidate)

    if scope.boundary_uncertain:
        reason = _BOUNDARY_HINT
    elif name == "user_scope" and value is not None and value_evidence:
        return DraftField(value=value, evidence=value_evidence)
    elif proposed:
        reason = "申请存在待确认的候选值。"
    elif value is None:
        reason = "申请未提供明确且格式有效的取值。"
    elif not complete or not _supports_value(name, value, evidence, options, body=body, scope=scope):
        reason = "取值缺少可核对的原文依据或已知选项。"
    else:
        return DraftField(value=value, evidence=evidence)

    if complete and _supports_value(
        name, value, value_evidence, options, body=body, scope=scope, candidate=True,
    ):
        candidate = Candidate(value=value, evidence=value_evidence)
        if candidate not in candidates:
            candidates.insert(0, candidate)
    if (
        name == "approval_url" and not scope.boundary_uncertain and not candidates
        and not any(_archive_links(body[left:right]) for left, right in scope.spans())
    ):
        return DraftField()
    return DraftField(evidence=evidence, uncertainty=Uncertainty(reason=reason, candidates=candidates))


def _value(name: str, value: object) -> object:
    if name == "user_scope":
        return (value.strip() or None) if isinstance(value, str) else None
    if name == "cancel_on_downgrade":
        return value if value in ("是", "否") else None
    if name in {"recharge_days", "gift_days"}:
        return value if type(value) is int else None
    if name == "user_tags":
        return value if isinstance(value, list) and all(_text(item) for item in value) else None
    if name == "packages":
        if isinstance(value, list) and all(_package(item) for item in value):
            return [
                PackageRow(
                    current_package=row["current_package"],
                    gift_package=None if row["gift_package"] == "无" else row["gift_package"],
                )
                for row in value
            ]
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


def _supports_value(
    name: str, value: object, evidence: list[SourceExcerpt], options: dict[str, str], *,
    body: str, scope: Scope, candidate: bool = False,
) -> bool:
    if value is None or not evidence or (not candidate and name in _OPTIONS and value not in options.values()):
        return False
    excerpts = [item.text for item in evidence]
    if name in _DATES:
        return _supports_in_context(
            _date_pattern(name, value, candidate=candidate), excerpts, body, scope,
            lambda text, start, end: _date_context(text, start, end, scope.date_prefixes), dates=True,
        ) or _supports_period_date(name, value, excerpts, body, scope)
    if name in _OPTIONS:
        terms = {value, *(term for term, option in options.items() if option == value)}
        return any(term in snippet for term in terms for snippet in excerpts)
    if name == "user_scope":
        return any(value in excerpt for excerpt in excerpts)
    if name == "approval_url":
        return any(value in _archive_links(excerpt) for excerpt in excerpts)
    if name == "user_tags":
        return all(any(item in excerpt for excerpt in excerpts) for item in value)
    if name in _DAYS_ACTION:
        return _days_unit(name, value, excerpts, body, scope) is not None
    if name == "packages":
        return _supports_packages(value, excerpts, body, scope)
    return True


def _archive_links(text: str) -> list[str]:
    return [match[0] for match in _WEB_LINK.finditer(text) if _ARCHIVE_LABEL.search(text[:match.start()])]


def _supports_packages(rows: list[PackageRow], excerpts: list[str], body: str, scope: Scope) -> bool:
    title = _TITLE_PACKAGE.fullmatch(scope.title)
    if len(rows) != 1 or title is None or scope.local is None:
        return False
    current = title[1].strip()
    gift = f"{current}_copy"
    if rows[0].current_package != current or rows[0].gift_package != gift:
        return False
    title_start = body.find(scope.title, *scope.local)
    if title_start < 0 or not any(
        start <= title_start and title_start + len(scope.title) <= start + len(excerpt)
        for excerpt in excerpts
        for start in scope.occurrences(body, excerpt)
    ):
        return False
    local = body[slice(*scope.local)]
    return _gift_package_matches(local, gift)


def _gift_package_matches(local: str, gift: str) -> bool:
    return not _NO_GIFT_PACKAGE.search(local) and all(
        match[1].strip() == gift for match in _GIFT_PACKAGE_LABEL.finditer(local)
    )


def _title_package_field(body: str, scope: Scope, raw: dict, proposed: list[dict]) -> DraftField | None:
    title = _TITLE_PACKAGE.fullmatch(scope.title)
    if scope.local is None:
        return None
    left, right = scope.local
    sources: dict[str, list[SourceExcerpt]] = {}
    title_names = []
    if title:
        title_names = _PACKAGE_SEPARATOR.split(title[1].strip())
        if len(title_names) > 1:
            title_names = [name.strip().removesuffix("包").strip() for name in title_names]
        if (excerpt := scope.excerpt(body, scope.title)) is not None:
            for name in filter(None, title_names):
                sources[name] = [excerpt]

    body_names = set()
    for match in _CURRENT_PACKAGE_LABEL.finditer(body, left, right):
        excerpt = SourceExcerpt(text=match[0].rstrip(), start=match.start())
        for name in _PACKAGE_SEPARATOR.split(match[1]):
            name = name.strip()
            if name and name not in title_names:
                name = name.removesuffix("包").strip()
            if name:
                body_names.add(name)
                evidence = sources.setdefault(name, [])
                if excerpt not in evidence:
                    evidence.append(excerpt)

    if len(title_names) > 1 or len(body_names) > 1 or (title_names and body_names - set(title_names)):
        evidence = {(excerpt.start, excerpt.text): excerpt for items in sources.values() for excerpt in items}
        return DraftField(
            evidence=list(evidence.values()),
            uncertainty=Uncertainty(
                reason="标题或正文中的当前节目包不唯一或相互冲突。",
                candidates=[
                    Candidate(value=[PackageRow(current_package=name, gift_package=f"{name}_copy")], evidence=excerpts)
                    for name, excerpts in sources.items()
                ],
            ),
        )
    if title_names and raw.get("value") is None and not proposed:
        current = title_names[0]
        gift = f"{current}_copy"
        if current in sources and _gift_package_matches(body[left:right], gift):
            return DraftField(value=[PackageRow(current_package=current, gift_package=gift)], evidence=sources[current])
    return None


def _days_unit(name: str, value: int, excerpts: list[str], body: str, scope: Scope) -> str | None:
    units = set()
    for match in _duration_matches(excerpts, body, scope):
        if match["action"] not in _DAYS_ACTION[name]:
            continue
        try:
            amount = CHINESE_DURATION_NUMBERS.get(match["amount"]) or int(match["amount"])
        except ValueError:
            continue
        if match["unit"] == "天" and amount == value:
            units.add("天")
        elif match["unit"] != "天" and amount > 0 and amount * 30 == value:
            units.add("月")
    return "天" if "天" in units else "月" if "月" in units else None


def _duration_matches(excerpts: list[str], body: str, scope: Scope) -> Iterator[re.Match[str]]:
    for excerpt in excerpts:
        locations = tuple(scope.occurrences(body, excerpt))
        for match in DURATION.finditer(excerpt):
            if locations and all(
                duration_in_context(body, start + match.start(), start + match.end()) for start in locations
            ):
                yield match


def _supports_in_context(
    pattern: re.Pattern[str], excerpts: list[str], body: str, scope: Scope,
    context: Callable[[str, int, int], bool], *, dates: bool = False,
) -> bool:
    for excerpt in excerpts:
        locations = tuple(scope.occurrences(body, excerpt, dates=dates))
        for match in pattern.finditer(excerpt):
            if locations and all(
                context(body, start + match.start(), start + match.end())
                for start in locations
            ):
                return True
    return False


def _date_context(body: str, start: int, end: int, prefixes: tuple[tuple[int, int], ...]) -> bool:
    if re.match(r"\d|[-./]\d", body[end:]):
        return False
    before = body[:start]
    for left, right in prefixes:
        if right <= start:
            before = before[:left] + " " * (right - left) + before[right:]
    previous_lines = re.split(r"[\r\n]", before)
    prefix = _DATE_CLAUSE_BREAK.split(previous_lines.pop())[-1]
    pieces = re.split(r"[，,]", prefix)
    if label := without_batch_intro(without_list_prefix(pieces.pop())).strip():
        return _application_date_parent(label)
    for piece in reversed(pieces):
        label = without_batch_intro(without_list_prefix(piece)).strip()
        parent = re.match(r"[^：:]+[:：]", label)
        if parent and not _DATE_LINE.match(label):
            return _application_date_parent(parent[0])

    # 同一上级标签可引出连续的日期字段，空行或其他字段结束该上下文。
    for line in reversed(previous_lines):
        previous = without_list_prefix(_DATE_CLAUSE_BREAK.split(line)[-1]).strip()
        if previous.endswith(("：", ":")):
            return _application_date_parent(previous) or not without_batch_intro(previous).strip()
        if _DATE_LINE.match(previous):
            continue
        parent = re.match(r"[^：:]+[:：]", previous)
        if parent and _DATE_LINE.match(previous[parent.end():].lstrip()):
            return _application_date_parent(parent[0])
        break
    return True


def _application_date_parent(label: str) -> bool:
    return bool(_DATE_PARENT.fullmatch(label) or _APPLICATION_HEADING.fullmatch(label))


def _supports_period_date(name: str, value: str, excerpts: list[str], body: str, scope: Scope) -> bool:
    endpoints = []
    spans = scope.period_spans()
    # 从分句开头核对标签，括注内的标点和换行不形成新的标签开头。
    starts = {right for _, right in scope.date_prefixes}
    for left, right in clause_spans(body, punctuation=PERIOD_CLAUSE_BREAKS):
        clause = body[left:right]
        starts.add(left)
        starts.add(right - len(without_batch_intro(clause)))
    for clause_start in starts:
        period = DATE_PERIOD.match(body, clause_start)
        if period is None or _inactive_period_context(body, period.start(), period.end()):
            continue
        if not any(left <= period.start() and period.end() <= right for left, right in spans):
            continue
        start = _parse_period_date(period["start_date"])
        end = _parse_period_date(period["end_date"])
        if start is not None and end is not None and start <= end:
            endpoint = start if name == "start_date" else end
            if endpoint.isoformat() == value:
                endpoints.append(period.span(name))

    # 摘录必须覆盖本字段的完整日期，重复出现时每处都须有合规的周期上下文。
    for excerpt in excerpts:
        locations = tuple(scope.occurrences(body, excerpt, dates=True))
        if locations and all(
            any(location <= start and end <= location + len(excerpt) for start, end in endpoints)
            for location in locations
        ):
            return True
    return False


def _inactive_period_context(body: str, start: int, end: int) -> bool:
    previous_lines = body[:start].split("\n")
    sentence = _DATE_CLAUSE_BREAK.split(previous_lines.pop())[-1]
    if _INACTIVE_PERIOD.search(sentence + body[start:end]):
        return True
    if not sentence.strip() and previous_lines:
        parent = previous_lines[-1].strip()
        return parent.endswith(("：", ":")) and bool(_INACTIVE_PERIOD.search(parent))
    return False


def _parse_period_date(text: str) -> date | None:
    parts = re.split(r"[-./年月]\s*", text.removesuffix("日"))
    if len(parts) == 3 and all(part.isdecimal() for part in parts):
        if len(parts[0]) == 4:
            year, month, day = map(int, parts)
        else:
            first, second, year = map(int, parts)
            if first <= 12 and second <= 12 and first != second:
                return None
            month, day = (first, second) if first <= 12 else (second, first)
    elif text.isdecimal():
        year, month, day = int(text[:4]), int(text[4:6]), int(text[6:])
    else:
        first, second, year_text = text.split()
        day_text, month_text = (first, second) if first.isdecimal() else (second, first)
        month_text = month_text.rstrip(".").casefold()
        month = next(
            (index for index in range(1, 13) if month_text in
             (month_name[index].casefold(), month_abbr[index].casefold())),
            0,
        )
        year, day = int(year_text), int(day_text.rstrip(","))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _date_pattern(name: str, value: str, *, candidate: bool) -> re.Pattern[str]:
    year, month, day = map(int, value.split("-"))
    month_text = rf"(?:{month_name[month]}|{month_abbr[month]}\.?)"
    formats = [
        rf"{year:04d}[-./年]\s*0?{month}[-./月]\s*0?{day}",
        rf"{year:04d}{month:02d}{day:02d}",
        rf"0?{day}\s+{month_text}\s+{year:04d}",
        rf"{month_text}\s+0?{day},?\s+{year:04d}",
    ]
    # 年份在末尾且日、月都不超过 12 时，无法从数字本身区分两种顺序。
    if candidate or day > 12 or day == month:
        formats.extend([
            rf"0?{day}[-./]0?{month}[-./]{year:04d}",
            rf"0?{month}[-./]0?{day}[-./]{year:04d}",
        ])
    label = _DATE_LABELS[name]
    if name == "end_date":
        # 区间连接词只有紧接申请起始日期时才可作为结束标签。
        label += rf"|(?:{_DATE_LABELS['start_date']}){_DATE_SEPARATOR}{DATE_TEXT}\s*[至到~～–—]"
    return re.compile(
        rf"(?:{label}){_DATE_SEPARATOR}(?:{'|'.join(formats)})(?!\d|[-./]\d)",
        re.IGNORECASE,
    )
