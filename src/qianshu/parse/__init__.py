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
from .scope import Scope, policy_scopes, without_batch_intro

__all__ = ["ModelError", "ParseError", "parse_application"]

_FIELD_NAMES = {field.name for field in fields(PolicyDraft)}
_PENDING = {"policy_name", "trigger_once", "repeat_bonus", "effective_multiplier", "approval_attachments"}
_YES_NO = {"trigger_once", "cancel_on_downgrade", "repeat_bonus"}
_LISTS = {"user_tags", "approval_attachments"}
_DATES = {"start_date", "end_date"}
_OPTIONS = {"policy_type", "benefit_type"}
_MUTEX = "maintain_mutual_exclusion_group"
_MUTEX_ANSWER = re.compile(
    r"(?:是否(?:需要)?|需不需要|需要)?(?:维护)?互斥组\s*[:：=]\s*([是否])"
    r"(?:\s*[（(][^（）()]+[）)])?"
)
_MUTEX_NO = re.compile(r"(?:不需要|不需|无需|不用|不)维护互斥组")
_MUTEX_YES = re.compile(r"(?:需要|需|须|必须)维护互斥组")
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
_ARCHIVE_LABEL = re.compile(
    rf"{_LABEL_BOUNDARY}(?:(?:本批(?:次)?|全批)(?:的)?)?"
    r"(?:存档|归档)(?:文档(?:链接)?|链接|网址|地址)?"
    r"\s*(?:见|为|在)?\s*[:：]?\s*(?:\[[^\]\r\n]+\]\()?$"
)
_NO_GIFT_PACKAGE = re.compile(
    r"(?<![非并])无赠送(?:节目)?包|(?:不赠送|没有赠送)(?:节目)?包|赠送(?:节目)?包\s*[:：=]\s*无"
)
_INTRO = re.compile(r"[^\[\]{}！!？?。.:：\u0060\r\n]+[！!？?。.:：]?(?:[ \t]*\r?\n|(?<=[！!？?。.:：])[ \t]*)")
_FENCE = re.compile(r"\u0060{3}json[ \t]*\r?\n(.*?)\r?\n[ \t]*\u0060{3}", re.DOTALL | re.IGNORECASE)


class ParseError(ValueError):
    """解析规则不可读，或模型回复不符合结构约定。"""


class ParsedPolicyDraft(PolicyDraft):
    """解析入口返回的草稿，附带可显示在自动调整中的提示。"""

    @property
    def hints(self) -> list[str]:
        mutex = self.maintain_mutual_exclusion_group
        return ["互斥组按默认填是"] if mutex.value == "是" and not mutex.evidence else []

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
        companies.append((_value("company", company.get("value")), quotes))

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
    if scope.local is None:
        return DraftField(uncertainty=Uncertainty(reason="无法唯一定位政策段落以核对互斥组。"))

    answers: dict[str, list[SourceExcerpt]] = {}
    for left, right in scope.spans():
        for match in re.finditer(r"[^。；;，,！!.\r\n]+", body[left:right]):
            clause = match[0].strip()
            statement = without_batch_intro(clause)
            if answer := _MUTEX_ANSWER.fullmatch(statement):
                value = answer[1]
            elif _MUTEX_NO.fullmatch(statement):
                value = "否"
            elif _MUTEX_YES.fullmatch(statement):
                value = "是"
            else:
                continue
            start = left + match.start() + len(match[0]) - len(match[0].lstrip())
            excerpt = SourceExcerpt(text=clause, start=start)
            evidence = answers.setdefault(value, [])
            if excerpt not in evidence:
                evidence.append(excerpt)

    if not answers:
        return DraftField(value="是")
    if len(answers) == 1:
        value, evidence = next(iter(answers.items()))
        return DraftField(value=value, evidence=evidence)
    return DraftField(
        evidence=[excerpt for excerpts in answers.values() for excerpt in excerpts],
        uncertainty=Uncertainty(
            reason="原文对互斥组有相互矛盾的说明。",
            candidates=[Candidate(value=value, evidence=evidence) for value, evidence in answers.items()],
        ),
    )


def _evidence(body: str, scope: Scope, quotes: object, *, at_boundary: bool = False) -> tuple[list[SourceExcerpt], bool]:
    _check_quotes(quotes)
    evidence = []
    for quote in dict.fromkeys(quotes):
        if excerpt := scope.excerpt(body, quote, at_boundary=at_boundary):
            evidence.append(excerpt)
    return evidence, len(evidence) == len(set(quotes))


def _draft_field(name: str, *, raw: dict, body: str, scope: Scope, options: dict[str, str]) -> DraftField:
    _check_object(raw, {"value", "evidence", "candidates"})
    value_evidence, complete = _evidence(body, scope, raw.get("evidence", []), at_boundary=name in _DATES)
    evidence = list(value_evidence)
    value = _value(name, raw.get("value"))
    if name in _OPTIONS and value is not None:
        value = options.get(value, value)

    proposed = _candidate_items(raw)
    candidates = []
    for item in proposed:
        candidate_evidence, candidate_complete = _evidence(
            body, scope, item.get("evidence", []), at_boundary=name in _DATES,
        )
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

    if name in _PENDING:
        reason = "取值规则待确认。"
    elif proposed:
        reason = "申请存在待确认的候选值。"
    elif value is None:
        reason = "申请未提供明确且格式有效的取值。"
    elif not evidence or not complete or not _supports(name, value, evidence, options):
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


def _supports_candidate(
    name: str, value: object, evidence: list[SourceExcerpt], options: dict[str, str]
) -> bool:
    return value is not None and bool(evidence) and _supports_value(name, value, evidence, options, candidate=True)


def _supports(
    name: str, value: object, evidence: list[SourceExcerpt], options: dict[str, str]
) -> bool:
    if name in _OPTIONS and value not in options.values():
        return False
    return _supports_value(name, value, evidence, options)


def _supports_value(
    name: str, value: object, evidence: list[SourceExcerpt], options: dict[str, str], *, candidate: bool = False,
) -> bool:
    excerpts = [item.text for item in evidence]
    if name in _DATES:
        return _date_evidence(name, value, excerpts, candidate=candidate)
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
    if name == "gift_days":
        return any(
            re.search(rf"(?:赠送|赠|(?<![配运发寄推赠])送)(?:时长|天数)?\s*[:：]?\s*{value}(?!\d|\.\d)", excerpt)
            for excerpt in excerpts
        )
    if name in {"recharge_days", "effective_multiplier"}:
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


def _date_evidence(name: str, value: str, excerpts: list[str], *, candidate: bool) -> bool:
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
    pattern = re.compile(
        rf"{_LABEL_BOUNDARY}(?:{label}){_DATE_SEPARATOR}(?:{'|'.join(formats)})(?!\d|[-./]\d)",
        re.IGNORECASE,
    )
    return any(pattern.search(without_batch_intro(excerpt)) for excerpt in excerpts)
