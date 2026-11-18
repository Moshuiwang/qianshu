"""把申请正文解析成待校验的政策草稿。"""

import json
import math
import re
from calendar import month_abbr, month_name
from collections.abc import Callable, Mapping
from dataclasses import fields
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from qianshu.config import load_config
from qianshu.draft import Candidate, DraftField, PackageRow, PolicyDraft, SourceExcerpt, Uncertainty

from .model import ModelError, ParseModel, configured_model
from .scope import Scope, clause_spans, policy_scopes, without_batch_intro, without_list_prefix

__all__ = ["ModelError", "ParseError", "parse_application"]

_FIELD_NAMES = {field.name for field in fields(PolicyDraft)}
_PENDING = {"policy_name", "trigger_once", "repeat_bonus", "effective_multiplier", "approval_attachments"}
_YES_NO = {"trigger_once", "cancel_on_downgrade", "repeat_bonus"}
_LISTS = {"user_tags", "approval_attachments"}
_DATES = {"start_date", "end_date"}
_OPTIONS = {"policy_type", "benefit_type"}
_MUTEX = "maintain_mutual_exclusion_group"
_MUTEX_NO_HINT = "申请要求不维护互斥组，需人工确认"
_BOUNDARY_HINT = "段落边界无法确定，需人工确认"
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
_DATE_TEXT = (
    r"(?:\d{4}[-./年]\s*\d{1,2}[-./月]\s*\d{1,2}(?:日)?|\d{8}|"
    r"\d{1,2}[-./]\d{1,2}[-./]\d{4}|\d{1,2}\s+[A-Za-z]+\.?\s+\d{4}|"
    r"[A-Za-z]+\.?\s+\d{1,2},?\s+\d{4})"
)
_LABEL_BOUNDARY = r"(?<![^\W_])"
_DATE_SEPARATOR = r"\s*[:：]?\s*"
_DATE_CLAUSE_BREAK = re.compile(r"[。！!？?；;]")
_DATE_PARENT = re.compile(
    r"(?:申请|政策|活动)(?:日期|时间|起止(?:日期|时间))?\s*[:：]|"
    r"(?:application|policy|campaign)(?:\s+dates?)?\s*[:：]",
    re.IGNORECASE,
)
_DATE_LINE = re.compile(
    rf"(?:{_DATE_LABELS['start_date']}|{_DATE_LABELS['end_date']}){_DATE_SEPARATOR}{_DATE_TEXT}",
    re.IGNORECASE,
)
_APPLICATION_HEADING = re.compile(r"[^：:，,。；;]{1,60}(?:定向|通用)?政策申请\s*[:：]")
_DAYS_ACTION = {"recharge_days": r"充(?:值)?", "gift_days": r"赠送|赠|(?<![配运发寄推赠])送"}
_DAYS_NEGATION = re.compile(r"[不无未免非否]|取消|停止|拒绝|避免")
_ARCHIVE_LABEL = re.compile(
    rf"{_LABEL_BOUNDARY}(?:(?:本批(?:次)?|全批)(?:的)?)?"
    r"(?:存档|归档)(?:文档(?:链接)?|链接|网址|地址)?"
    r"\s*(?:见|为|在)?\s*[:：]?\s*(?:\[[^\]\r\n]+\]\()?$"
)
_NO_GIFT_PACKAGE = re.compile(
    r"(?<![非并])无赠送(?:节目)?包|(?:不赠送|没有赠送)(?:节目)?包|赠送(?:节目)?包\s*[:：=]\s*无"
)
_INTRO = re.compile(r"[^\[\]{}！!？?。.:：\u0060\r\n]+[！!？?。.:：]?(?:[ \t]*\r?\n|(?<=[！!？?。.:：])[ \t]*)")
_FENCE = re.compile(r"\u0060{3}(?:json)?[ \t]*\r?\n(.*?)\r?\n[ \t]*\u0060{3}", re.DOTALL | re.IGNORECASE)


class ParseError(ValueError):
    """解析规则不可读，或模型回复不符合结构约定。"""


class ParsedPolicyDraft(PolicyDraft):
    """解析入口返回的草稿，附带可显示在自动调整中的提示。"""

    @property
    def hints(self) -> list[str]:
        mutex = self.maintain_mutual_exclusion_group
        if mutex.value == "是" and not mutex.evidence:
            return ["互斥组按默认填是"]
        hints = []
        if mutex.uncertainty is not None:
            if mutex.uncertainty.reason == _BOUNDARY_HINT:
                hints.append(_BOUNDARY_HINT)
            if any(candidate.value == "否" for candidate in mutex.uncertainty.candidates):
                hints.append(_MUTEX_NO_HINT)
        return hints

    def to_dict(self) -> dict[str, object]:
        return {**super().to_dict(), "hints": self.hints}


def parse_application(
    body: str,
    *,
    config: Mapping[str, str] | None = None,
    model: ParseModel | None = None,
    rules_dir: Path = Path("rules"),
) -> list[ParsedPolicyDraft]:
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
        policies = json.loads(_unwrap(reply), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise ParseError("申请解析模型回复不是有效的政策 JSON。") from None
    if not isinstance(policies, list):
        raise ParseError("申请解析模型回复应为政策数组。")

    companies = []
    for policy in policies:
        _check_object(policy, _FIELD_NAMES)
        if not policy:
            raise ParseError("申请解析模型回复包含空白政策。")
        company = policy.get("company", {})
        _check_object(company, {"value", "evidence", "candidates"})
        quotes = company.get("evidence", [])
        _check_quotes(quotes)
        heading_quotes = list(quotes)
        for raw in policy.values():
            if isinstance(raw, dict) and isinstance(raw.get("evidence"), list):
                heading_quotes.extend(quote for quote in raw["evidence"] if isinstance(quote, str))
        companies.append((_value("company", company.get("value")), heading_quotes))

    drafts = []
    for policy, scope in zip(policies, policy_scopes(body, companies), strict=True):
        draft_fields = {
            name: _draft_field(name, raw=policy.get(name, {}), body=body, scope=scope, options=options.get(name, {}))
            for name in sorted(_FIELD_NAMES - {_MUTEX})
        }
        raw_mutex = policy.get(_MUTEX, {})
        _validate_mutex_shape(raw_mutex)
        drafts.append(ParsedPolicyDraft(**draft_fields, maintain_mutual_exclusion_group=_mutex(body, scope)))
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


def _validate_mutex_shape(raw: dict) -> None:
    _check_object(raw, {"value", "evidence", "candidates"})
    _check_quotes(raw.get("evidence", []))
    for item in _candidate_items(raw):
        _check_quotes(item.get("evidence", []))


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
            limit = right if (left, right) == scope.local else len(body)
            supplement = _mutex_supplement(body[end:limit]) if value == "否" else 0
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


def _evidence(body: str, scope: Scope, quotes: object) -> tuple[list[SourceExcerpt], bool]:
    _check_quotes(quotes)
    evidence = []
    for quote in dict.fromkeys(quotes):
        if excerpt := scope.excerpt(body, quote):
            evidence.append(excerpt)
    return evidence, len(evidence) == len(set(quotes))


def _draft_field(name: str, *, raw: dict, body: str, scope: Scope, options: dict[str, str]) -> DraftField:
    _check_object(raw, {"value", "evidence", "candidates"})
    value_evidence, complete = _evidence(body, scope, raw.get("evidence", []))
    evidence = list(value_evidence)
    value = _value(name, raw.get("value"))
    if name in _OPTIONS and value is not None:
        value = options.get(value, value)

    proposed = _candidate_items(raw)
    candidates = []
    for item in proposed:
        candidate_evidence, candidate_complete = _evidence(body, scope, item.get("evidence", []))
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
    elif name in _PENDING:
        reason = "取值规则待确认。"
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
        return _supports_in_context(_date_pattern(name, value, candidate=candidate), excerpts, body, scope, _date_context)
    if name in _OPTIONS:
        terms = {value, *(term for term, option in options.items() if option == value)}
        return any(term in snippet for term in terms for snippet in excerpts)
    if name in {"policy_name", "company", "user_scope"}:
        return any(value in excerpt for excerpt in excerpts)
    if name == "approval_url":
        return any(
            _ARCHIVE_LABEL.search(excerpt[:match.start()])
            for excerpt in excerpts
            for match in re.finditer(r"https?://[^\s，。；！？、)）】」]+", excerpt)
            if match[0] == value
        )
    if name in _LISTS:
        return all(any(item in excerpt for excerpt in excerpts) for item in value)
    if name in _DAYS_ACTION:
        pattern = re.compile(rf"(?:{_DAYS_ACTION[name]})[ \t]*{value}[ \t]*天")
        return _supports_in_context(
            pattern, excerpts, body, scope, _days_context,
        )
    if name == "effective_multiplier":
        number = re.escape(str(value))
        return any(re.search(rf"(?<![\d.]){number}(?!\d|\.\d)", excerpt) for excerpt in excerpts)
    if name == "packages":
        for row in value:
            if not any(row.current_package in excerpt for excerpt in excerpts):
                return False
            if row.gift_package is None:
                if not any(
                    row.current_package in clause and _NO_GIFT_PACKAGE.search(clause)
                    for excerpt in excerpts
                    for clause in re.split(r"[。；;\r\n]|[，,](?=\s*当前)", excerpt)
                ):
                    return False
                continue
            if any(row.gift_package in excerpt for excerpt in excerpts):
                continue
            if row.gift_package != f"{row.current_package}_copy":
                return False
            if not any(row.current_package in excerpt and "附属包" in excerpt for excerpt in excerpts):
                return False
    return True


def _supports_in_context(
    pattern: re.Pattern[str], excerpts: list[str], body: str, scope: Scope,
    context: Callable[[str, int, int], bool],
) -> bool:
    for excerpt in excerpts:
        locations = tuple(scope.occurrences(body, excerpt))
        for match in pattern.finditer(excerpt):
            if locations and all(
                context(body, start + match.start(), start + match.end())
                for start in locations
            ):
                return True
    return False


def _days_context(body: str, start: int, end: int) -> bool:
    return any(
        left <= start and end <= right and not _DAYS_NEGATION.search(body, left, right)
        for left, right in clause_spans(body, punctuation=_MUTEX_BREAKS)
    )


def _date_context(body: str, start: int, end: int) -> bool:
    if re.match(r"\d|[-./]\d", body[end:]):
        return False
    previous_lines = re.split(r"[\r\n]", body[:start])
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
        label += rf"|(?:{_DATE_LABELS['start_date']}){_DATE_SEPARATOR}{_DATE_TEXT}\s*[至到~～–—]"
    return re.compile(
        rf"(?:{label}){_DATE_SEPARATOR}(?:{'|'.join(formats)})(?!\d|[-./]\d)",
        re.IGNORECASE,
    )
