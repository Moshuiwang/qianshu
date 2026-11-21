"""定位政策段落与明确共享的原文范围。"""

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from unicodedata import normalize

from qianshu.draft import SourceExcerpt

type Span = tuple[int, int]

_ITEM = re.compile(r"^[ \t]*(?:[①-⑳]|\d{1,3}[、)）]|\d{1,3}[.．](?!\d)|[（(]\d{1,3}[)）])[ \t]*")
_BULLET = re.compile(r"^[ \t]*[-+*•][ \t]+")
_TITLE_PREFIX = re.compile(r"^(?:#{1,6}(?=[ \t]|$)|>+|第[一二三四五六七八九十百\d]+[条项节章])\s*")
_TITLE_WRAPPER = re.compile(r"^(?:(\*\*|__)(.+?)\1|【([^】]+)】)\s*[:：]?\s*$")
_COMPANY = re.compile(r"^\s*(?:(?:申请|适用)公司(?:名称)?|公司(?:名称)?)\s*[:：]\s*")
_COMPANY_HEADING = re.compile(
    r"^\s*[\w&· -]{1,40}?公司(?:[甲乙丙丁戊己庚辛壬癸\da-zA-Z]+)?"
    r"(?=\s*(?:的?(?:定向|通用)?政策(?:申请)?)?(?:[：:，,。；;…]|$))"
)
_COMPANY_APPLICATION = re.compile(r"^\s*的?(?:定向|通用)?政策(?:申请|[：:，,。；;…]|$)")
_NUMBERED_FIELD = re.compile(
    r"^(?:(?:申请|政策|活动)?(?:开始|起始|结束|终止|截止)(?:日期|时间|日)|"
    r"(?:(?:application|policy|campaign)\s+)?(?:start|end)\s+date)\s*(?=[:：\d])|"
    r"^(?:用户标签|用户范围|(?:优惠)?政策(?:名称|名)|优惠类型|政策类型|审批件附件|当前节目包|"
    r"(?:是否)?只触发一次|触发次数|多买多[送赠]|生效倍数|降包用户取消优惠|充值天数|赠送天数)"
    r"\s*[:：]|"
    r"^(?:(?:是否(?:需要)?|需不需要|需要|无需|不需要|不需|不用|不)?(?:维护)?互斥组)",
    re.IGNORECASE,
)
_NUMBERED_DEAL = re.compile(r"^充(?:值)?\s*\d+\s*天\s*(?:送|赠送|赠)\s*\d+\s*天[。.]?$")
_BATCH_PREFIX = (
    r"以下各(?:条|项)(?:政策|申请)?(?:均|全部)?(?:统一|共同|共用|适用)?|"
    r"(?:本批(?:次)?|全批)(?:的)?(?:"
    r"(?:所有|各条|各项)(?:政策|申请)?(?:均|全部|统一|共同|共用|的)?|"
    r"统一|共同|共用|通用|适用|均)|"
    r"(?:以上|下列|所有|全部|各条|各项)(?:政策|申请)(?:均|全部|统一|共同|共用|适用|的)|"
    r"适用于(?:全批|本批(?:次)?(?:所有|各条)?(?:政策|申请)?)"
)
_SHARED = re.compile(rf"^\s*(?:{_BATCH_PREFIX})")
_BATCH_INTRO = re.compile(rf"^\s*(?:{_BATCH_PREFIX})\s*[:：、，,]?\s*")
_BATCH_ARCHIVE = re.compile(r"^\s*(?:本批(?:次)?|全批)(?:的)?(?:存档|归档)")
_CHAPTER = re.compile(r"^(?:[一二三四五六七八九十百]+[、.．)）]|[（(][一二三四五六七八九十百]+[)）])[ \t]*")
_REMARKS = re.compile(r"(?:备注|说明|附注|注释|注意事项|其他(?:事项|说明)?)\s*[:：。.]?")
_REMARK_ITEM = re.compile(r"^(?:本次|本批|此次|请|详见|如有|如需)")
_POLICY_TITLE = re.compile(r"(?:政策(?:申请)?|(?:适用|包)用户)\s*(?:[（(].+[)）])?\s*$")
DATE_TEXT = (
    r"(?:\d{4}[-./年]\s*\d{1,2}[-./月]\s*\d{1,2}(?:日)?|\d{8}|"
    r"\d{1,2}[-./]\d{1,2}[-./]\d{4}|\d{1,2}\s+[A-Za-z]+\.?\s+\d{4}|"
    r"[A-Za-z]+\.?\s+\d{1,2},?\s+\d{4})"
)
_LINE_SPACE = r"[^\S\r\n]"
_PERIOD_DATE = DATE_TEXT.replace(r"\s", _LINE_SPACE)
PERIOD_CLAUSE_BREAKS = "。！？!?；;，,\r\n"
DATE_PERIOD = re.compile(
    rf"(?:[^：:（）(){re.escape(PERIOD_CLAUSE_BREAKS)}]*?"
    r"(?:申请|政策|活动)(?:时间|日期)?(?:周期|期间|时段|起止(?:日期|时间)?)|"
    rf"{_LINE_SPACE}*起止(?:日期|时间))"
    rf"{_LINE_SPACE}*[:：]{_LINE_SPACE}*(?:(?:\r\n?|\n){_LINE_SPACE}*)*"
    rf"(?P<start_date>{_PERIOD_DATE}){_LINE_SPACE}*(?:——|[—–\-~～至到]){_LINE_SPACE}*"
    rf"(?P<end_date>{_PERIOD_DATE}){_LINE_SPACE}*(?=[\r\n]|\Z)"
)
_REMARK_FIELD = re.compile(
    r"^(?:(?:充(?:值)?|赠送|赠|送)\s*[:：]?\s*\d|"
    r"(?:活动|申请|政策)?(?:日期区间|日期|时间|起止(?:日期|时间)?)\s*[:：]|"
    r"\d{4}(?:[-/.年])\d{1,2}[-/.月]\d{1,2}|(?:标签(?:名称)?|使用场景)\s*[:：])"
)
_PREAMBLE_PACKAGE_TITLE = re.compile(rf"^(?P<name>[^:：]+)包用户{_LINE_SPACE}*[:：]")
_PREAMBLE_FIELD_LABEL = re.compile(
    r"^(?:用户标签|标签(?:名称)?|充值(?:天数)?|赠送(?:天数)?|"
    r"(?:当前|赠送)?节目包|当前包|赠送包|用户范围|使用场景)\s*[:：]"
)
_QUALIFIER = re.compile(r"专用|专属|单独|特定|仅(?:限|适用)?|适用(?:于)?|限定")
_QUALIFIED_DATE_LABEL = re.compile(
    r"申请(?:时间|日期)?(?:周期|期间|时段|起止)|"
    r"(?:申请|政策|活动)?(?:开始|起始|结束|终止|截止)(?:日期|时间)",
)
_TITLE_QUALIFIER = re.compile(r"[（(]([^（）()]*(?:[（(][^（）()]*[)）][^（）()]*)*)[)）]\s*$")
_HAN_RANGES = r"\u3400-\u9fff\U00020000-\U000323af"
_HAN = re.compile(rf"[{_HAN_RANGES}]")
_IDENTIFIER_CHAR = rf"[^\W{_HAN_RANGES}]"
_COUNTRY_PUNCTUATION = str.maketrans("‘’ʼ‐‑‒–—−", "'''------")
CHINESE_DURATION_NUMBERS = {
    name: number for number, name in enumerate(
        ("一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二"), 1,
    )
} | {"两": 2}
DURATION = re.compile(
    rf"(?P<action>充值|充|赠送|赠|送)[ \t]*(?P<amount>\d+|{'|'.join(CHINESE_DURATION_NUMBERS)})"
    r"[ \t]*(?P<unit>天|个[ \t]*月|月)"
)
_DURATION_NEGATION = re.compile(r"[不无未免非否]|取消|停止|拒绝|避免")
_DURATION_PREFIX = re.compile(r"(?:用户|客户|申请|采用|统一|共同|自动|额外|追加|后|则|即|均|可|再|或|并|就|每|须|需|要)$")
_DURATION_JOIN = re.compile(r"(?:或者|或|并且|并)?(?=充值|充|赠送|赠|送)")


def _normalize_country_text(text: str) -> str:
    text = " ".join(normalize("NFKC", text).translate(_COUNTRY_PUNCTUATION).split())
    return re.sub(r"\s*([()'\-])\s*", r"\1", text)


def read_countries(table: str) -> dict[str, re.Pattern[str]]:
    countries = {}
    for line in table.splitlines():
        if not line.strip():
            continue
        name, *aliases = (cell.strip() for cell in line.split("\t"))
        if not _HAN.search(name) or name in countries or any(not alias for alias in aliases):
            raise ValueError
        terms = []
        for term in map(_normalize_country_text, (name, *aliases)):
            escaped = re.escape(term)
            if _HAN.search(term):
                terms.append(escaped)
                continue
            code = term.upper() if re.fullmatch(r"[A-Za-z]{2}", term) else None
            alias = code if code is not None else rf"(?i:{escaped})"
            # 商业智能的缩写与布隆迪代码相同，报表语境中不能据此认定国家。
            context = r"(?!\s*(?:报表|报告|看板|仪表盘))" if code == "BI" else ""
            terms.append(rf"(?<!{_IDENTIFIER_CHAR}){alias}(?!{_IDENTIFIER_CHAR}){context}")
        countries[name] = re.compile("|".join(terms))
    if not countries:
        raise ValueError
    return countries


def country_matches(
    texts: Iterable[str], countries: dict[str, re.Pattern[str]], *, exact: bool = False,
) -> set[str]:
    texts = tuple(map(_normalize_country_text, texts))
    return {
        name for name, pattern in countries.items()
        if any((pattern.fullmatch if exact else pattern.search)(text) for text in texts)
    }


def company_mentioned(name: str, texts: Iterable[str]) -> bool:
    pattern = re.compile(rf"(?<!{_IDENTIFIER_CHAR}){re.escape(name)}(?!{_IDENTIFIER_CHAR})", re.IGNORECASE)
    return any(pattern.search(text) for text in texts)


def quoted_company_heading(body: str, quotes: list[str], countries: dict[str, re.Pattern[str]]) -> str | None:
    names = set()
    for quote in quotes:
        if quote not in body:
            continue
        text = _heading_title(_COMPANY.sub("", without_list_prefix(quote.strip()), count=1))
        if country_matches([text], countries, exact=True):
            names.add(text)
        elif name := _batch_company(quote, set()) or _company_name(text, set()):
            names.add(name)
    return next(iter(names)) if len(names) == 1 else None


def _content_lines(text: str) -> list[re.Match[str]]:
    return [line for line in re.finditer(r"[^\r\n]+", text) if line[0].strip()]


def _outside_preamble_dates(body: str, quote: str, end: int, ranges: tuple[Span, ...]) -> bool:
    start = body.find(quote, 0, end)
    while start >= 0:
        if not any(left <= start and start + len(quote) <= right for left, right in ranges):
            return True
        start = body.find(quote, start + 1, end)
    return False


def _preamble_policy_field(line: str) -> bool:
    content = _CHAPTER.sub("", without_list_prefix(line).strip(), count=1)
    if title := _PREAMBLE_PACKAGE_TITLE.match(content):
        name = title["name"]
        if len(_HAN.findall(name)) <= 12 and all(
            char.isalpha() or char.isdecimal() or char in " ._-+" for char in name
        ):
            return True
    content = without_batch_intro(content).strip()
    return bool(_PREAMBLE_FIELD_LABEL.match(content)) or any(
        duration_in_context(content, *match.span()) for match in DURATION.finditer(content)
    )


def _date_qualifier(
    line: str, countries: dict[str, re.Pattern[str]], company_names: set[str],
) -> tuple[int, set[str], set[str]] | None:
    if _SHARED.match(line):
        return None
    content = _CHAPTER.sub("", without_list_prefix(line).strip(), count=1)
    content = _TITLE_PREFIX.sub("", content, count=1).strip()
    label = re.split(r"[:：，,]", content, maxsplit=1)[0].strip()
    if not label or _POLICY_TITLE.search(label):
        return None
    named_countries = country_matches([label], countries)
    named_companies = {name for name in company_names if company_mentioned(name, [label])}
    marker = _QUALIFIER.search(content)
    if marker and marker.start() >= len(label):
        # 限定词也可写在日期行的补充说明或括注中。
        qualifier_clause = next(
            part for part in re.split(r"[:：，,。；;（）()]", content)
            if _QUALIFIER.search(part)
        )
        named_countries |= country_matches([qualifier_clause], countries)
        named_companies |= {name for name in company_names if company_mentioned(name, [qualifier_clause])}
    exact_name = bool(country_matches([label], countries, exact=True)) or label in named_companies
    date_label = _QUALIFIED_DATE_LABEL.search(label)
    named_period = bool(
        date_label and (named_countries or named_companies or marker)
        and (date_label.start() or marker)
    )
    if not (marker or exact_name or named_period):
        return None
    # 标题后的冒号或逗号可引出本行的申请字段；日期标签内部的冒号保留。
    delimiter = len(label)
    offset = line.find(label)
    prefix = re.match(r"\s*[:：，,][ \t]*", line[offset + delimiter:])
    if named_period:
        payload = offset + date_label.start() if date_label.start() else 0
    else:
        payload = offset + delimiter + prefix.end() if prefix and (exact_name or _QUALIFIER.search(label)) else 0
    if marker and (re.search(r"其他|某|部分|除外|不适用", content) or not (named_countries or named_companies)):
        named_countries, named_companies = set(), set()
    return payload, named_countries, named_companies


def _country_section(
    line: str, countries: dict[str, re.Pattern[str]], *, followed_by_date: bool,
) -> set[str] | None:
    content = line.strip()
    chapter = _CHAPTER.match(content)
    if chapter:
        content = content[chapter.end():]
    wrapper = _TITLE_WRAPPER.fullmatch(content)
    marked = bool(chapter or wrapper or content.startswith("■") or content.endswith((":", "：")))
    if wrapper:
        label = wrapper[2] or wrapper[3]
    else:
        label = content.removeprefix("■").rstrip(":：").strip()
    names = country_matches([label], countries, exact=True)
    if names:
        return names
    # 无法识别的分节标记也截断前一节。普通申请字段和批次标题沿用原判据。
    if (
        not _SHARED.match(line) and not _QUALIFIED_DATE_LABEL.search(label)
        and not (_POLICY_TITLE.search(label) or _COMPANY_HEADING.match(label) or _QUALIFIER.search(label))
        and (content.startswith("■") or followed_by_date and (
            wrapper or re.search(r"国家|某国|地区", label) or marked and _possible_country_label(label)
        ))
    ):
        return set()
    return None


def _possible_country_label(label: str) -> bool:
    return not (
        _REMARKS.fullmatch(label) or re.fullmatch(r"(?:申请|政策|活动)(?:日期|时间)?", label)
    ) and bool(
        re.fullmatch(rf"[{_HAN_RANGES}]{{1,8}}|[A-Za-z][A-Za-z .'-]*", label)
        or len(label) <= 24 and re.search(r"[及和与、/]", label)
    )


def _starts_date_field(line: str) -> bool:
    return bool(_QUALIFIED_DATE_LABEL.search(line) or DATE_PERIOD.match(line))


def _preamble_date_spans(
    preamble: str, countries: dict[str, re.Pattern[str]], company_names: set[str],
    target_countries: set[str], target_companies: set[str],
) -> tuple[tuple[Span, ...], tuple[Span, ...]]:
    spans = []
    prefixes = []
    section_allowed = True
    numbered_section = False
    next_line_allowed = None
    lines = _content_lines(preamble)
    for index, match in enumerate(lines):
        line = match[0]
        chapter = _CHAPTER.match(line.strip()) is not None
        allowed = section_allowed if next_line_allowed is None else next_line_allowed
        next_line_allowed = None
        section = _country_section(
            line, countries, followed_by_date=index + 1 < len(lines) and _starts_date_field(lines[index + 1][0]),
        )
        if section is not None:
            section_allowed = allowed = len(target_countries) == 1 and target_countries == section
            numbered_section = False
            if allowed:
                prefixes.append(match.span())
        elif qualifier := _date_qualifier(line, countries, company_names):
            payload, named_countries, named_companies = qualifier
            allowed = bool(named_countries or named_companies) and (
                not named_countries or len(target_countries) == 1 and target_countries <= named_countries
            ) and (
                not named_companies or len(target_companies) == 1 and target_companies <= named_companies
            )
            tail = line[payload:].strip() if payload else ""
            inline = bool(tail or re.search(r"\d{4}(?:[-./年]|\d{4})", line) or _QUALIFIED_DATE_LABEL.search(line))
            if not inline:
                section_allowed = allowed
                numbered_section = chapter
            elif line.rstrip().endswith((":", "：")):
                next_line_allowed = allowed
            if allowed and payload:
                prefixes.append((match.start(), match.start() + payload))
        elif chapter and numbered_section:
            section_allowed = allowed = True
            numbered_section = False
        if allowed:
            if spans and not preamble[spans[-1][1]:match.start()].strip():
                spans[-1] = (spans[-1][0], match.end())
            else:
                spans.append(match.span())
    return tuple(spans), tuple(prefixes)


@dataclass(frozen=True)
class Scope:
    local: Span | None
    shared: tuple[Span, ...]
    boundary_uncertain: bool = False
    first_heading: int | None = None
    preamble_dates: tuple[Span, ...] = ()
    date_prefixes: tuple[Span, ...] = ()
    title: str = ""
    countries: frozenset[str] = frozenset()
    country_sources: tuple[Span, ...] = ()
    explicit_shared: tuple[Span, ...] = ()
    duplicate_span: Span | None = None

    def spans(self, *, dates: bool = False) -> tuple[Span, ...]:
        local = () if self.local is None else (self.local,)
        if dates and self.first_heading is not None:
            return (*local, *self.preamble_dates, *self._after_first_heading(self.shared))
        return (*local, *self._after_first_heading(self.shared))

    def period_spans(self) -> tuple[Span, ...]:
        if self.first_heading is None:
            # 无标题的单条申请没有独立政策标题，申请正文可提供批次周期。
            local = () if self.local is None else (self.local,)
            return (*local, *self.explicit_shared)
        return (*self.preamble_dates, *self._after_first_heading(self.explicit_shared))

    def _after_first_heading(self, spans: tuple[Span, ...]) -> tuple[Span, ...]:
        if self.first_heading is None:
            return spans
        return tuple((max(left, self.first_heading), right) for left, right in spans if right > self.first_heading)

    def excerpt(self, body: str, quote: str, *, dates: bool = False) -> SourceExcerpt | None:
        start = next(self.occurrences(body, quote, dates=dates), None)
        if start is None:
            return None
        first = body.find(quote)
        unique = body.find(quote, first + 1) < 0
        return SourceExcerpt(text=quote, start=start if unique else None)

    def occurrences(self, body: str, quote: str, *, dates: bool = False) -> Iterator[int]:
        if not quote.strip():
            return
        for left, right in self.spans(dates=dates):
            if dates and self.first_heading is not None and right <= self.first_heading:
                # 摘录也见于后文或归属不同的前言时，无法确认引用的是这部分前言。
                if body.find(quote, self.first_heading) >= 0 or _outside_preamble_dates(
                    body, quote, self.first_heading, self.preamble_dates,
                ):
                    continue
            start = body.find(quote, left, right)
            while start >= 0:
                yield start
                start = body.find(quote, start + 1, right)


@dataclass(frozen=True)
class Heading:
    start: int
    text: str
    company: str | None
    countries: set[str]
    numbered: bool
    prefixed: bool
    country_title: bool
    after_remarks: bool
    remark_title: bool
    line_span: Span


@dataclass(frozen=True)
class Remarks:
    span: Span
    shared: bool


@dataclass(frozen=True)
class PolicyReference:
    company: str | None
    countries: set[str]
    quotes: list[str]


def policy_layout(
    body: str, *, companies_file: Path | None = None,
) -> tuple[list[Heading], tuple[Span, ...]]:
    """独立识别标题，另返回不用于证明单条政策覆盖的共享段和备注范围。"""
    from . import _DEFAULT_COMPANIES_FILE, _read_companies

    countries = read_countries(Path("rules/countries.txt").read_text(encoding="utf-8"))
    try:
        companies = _read_companies(
            (companies_file or _DEFAULT_COMPANIES_FILE).read_text(encoding="utf-8"), countries,
        )
    except (OSError, UnicodeError, ValueError):
        companies = {}
    configured_names = {name for rows in companies.values() for name in rows.values()}
    headings, remarks, _ = _headings(body, set(), countries, configured_names)
    stops = sorted([heading.start for heading in headings] + [remark.span[0] for remark in remarks])
    return headings, (*_shared_spans(body, stops), *(remark.span for remark in remarks))


def numbered_field(content: str) -> bool:
    """识别编号下的字段项，供主线排除弱边界。"""
    return _NUMBERED_FIELD.match(content) is not None


def policy_scopes(
    body: str, references: list[PolicyReference], countries: dict[str, re.Pattern[str]],
    companies: dict[str, list[str]] | None = None,
) -> list[Scope]:
    """按原文标题划段，只有能唯一绑定时才确定草稿的段落。"""
    companies = companies or {}
    configured_names = {name for names in companies.values() for name in names}
    company_names = {ref.company for ref in references if ref.company is not None}
    headings, remarks, first_chapter = _headings(body, company_names, countries, configured_names)
    stops = sorted([heading.start for heading in headings] + [remark.span[0] for remark in remarks])
    explicit_shared = tuple(
        (left, right) for left, right in _shared_spans(body, stops)
        if not any(not remark.shared and remark.span[0] <= left < remark.span[1] for remark in remarks)
    )
    shared = (*explicit_shared, *(remark.span for remark in remarks if remark.shared))
    batch_companies = {
        name for left, right in explicit_shared
        if (name := _batch_company(body[left:right], company_names)) is not None
    }

    def company_conflicts(ref: PolicyReference) -> bool:
        return len(batch_companies) > 1 or (
            bool(batch_companies) and ref.company is not None
            and not country_matches([ref.company], countries, exact=True)
            and batch_companies != {ref.company}
        )

    if not headings:
        return [
            Scope(
                (0, stops[0] if stops else len(body)), shared,
                countries=frozenset(country_matches([body], countries)),
                country_sources=tuple(line.span() for line in _content_lines(body)),
                explicit_shared=explicit_shared,
            )
            if len(references) == 1 and not company_conflicts(ref)
            else Scope(None, shared, boundary_uncertain=True, explicit_shared=explicit_shared)
            for ref in references
        ]

    first_heading = headings[0].start
    # 批次国家沿用总标题和显式共享段的判据。
    batch_sources = (
        *(line.span() for line in _content_lines(body[:min(first_chapter, first_heading)])
          if _date_qualifier(line[0], countries, company_names | configured_names) is None
          and _country_section(line[0], countries, followed_by_date=False) is None),
        *((left, min(right, first_heading)) for left, right in shared if left < first_heading),
    )
    batch_countries = country_matches((body[left:right] for left, right in batch_sources), countries)
    next_stop = dict(zip(stops, [*stops[1:], len(body)], strict=True))
    ends = [next_stop[heading.start] for heading in headings]
    single = len(references) == len(headings) == 1

    def matches_heading(ref: PolicyReference, heading: Heading) -> bool:
        if company_conflicts(ref):
            return False
        if len(ref.countries) == 1 and any(
            len(known) == 1 and known != ref.countries for known in (batch_countries, heading.countries)
        ):
            return False
        if heading.company is not None or not heading.numbered:
            return (
                (ref.company is not None and heading.company == ref.company)
                or (single and (heading.company is None or ref.company is None))
            )
        return batch_companies == {ref.company} or (
            len(ref.countries) == 1 and (
                batch_countries == ref.countries
                or (heading.country_title and heading.countries == ref.countries)
            )
        ) or (
            not ref.countries
            and (len(batch_countries) == 1 or heading.country_title and len(heading.countries) == 1)
            and _quotes_heading(body, heading, next_stop[heading.start], ref.quotes)
        )

    matches = []
    for ref in references:
        locations = [
            (heading, end) for heading, end in zip(headings, ends)
            if matches_heading(ref, heading)
        ]
        if len(locations) > 1:
            locations = [
                (heading, end) for heading, end in locations
                if _quotes_heading(body, heading, end, ref.quotes)
            ]
        matches.append((locations[0][0].start, locations[0][1]) if len(locations) == 1 else None)

    uncertain_starts = set()
    for index, (heading, end) in enumerate(zip(headings, ends, strict=True)):
        if heading.remark_title:
            span = (heading.start, end)
            if matches.count(span) == 1 and _quotes_heading(
                body, heading, end, references[matches.index(span)].quotes,
            ):
                continue
        if not heading.prefixed and not heading.after_remarks:
            if heading.company is not None or heading.country_title or not heading.numbered:
                continue
            # 普通编号标题需有绑定依据，并由摘录确认。正文中提及国家的编号行仍有歧义。
            if not heading.countries and any(
                matches_heading(ref, heading) and _quotes_heading(body, heading, end, ref.quotes)
                for ref in references
            ):
                continue
        uncertain_starts.add(heading.start)
        if index:
            uncertain_starts.add(headings[index - 1].start)
        elif heading.after_remarks and len(headings) > 1:
            # 说明在首条政策之前时，首个可疑边界的相邻政策在后面。
            uncertain_starts.add(headings[1].start)

    preamble_end = min(
        (remark.span[0] for remark in remarks if not remark.shared and remark.span[0] < first_heading),
        default=first_heading,
    )
    preamble = body[:preamble_end]
    preamble_safe = first_heading not in uncertain_starts

    scopes = []
    headings_by_start = {heading.start: heading for heading in headings}
    for span in matches:
        local = span if matches.count(span) == 1 else None
        boundary_uncertain = local is None or local[0] in uncertain_starts
        heading = headings_by_start[local[0]] if local is not None else None
        target_countries = heading.countries if heading and heading.countries else batch_countries
        company_texts = [body[slice(*local)]] if local is not None else []
        company_texts.extend(body[left:right] for left, right in shared)
        target_companies = {
            name for name in configured_names if company_mentioned(name, company_texts)
        }
        target_companies.update(batch_companies)
        if heading and heading.company and not country_matches([heading.company], countries, exact=True):
            target_companies.add(heading.company)
        title = _heading_title(_COMPANY.sub("", heading.text, count=1)) if heading else ""
        country_sources = batch_sources
        if heading and heading.countries:
            start = body.find(title, *heading.line_span)
            country_sources = ((start, start + len(title)),) if start >= 0 else (heading.line_span,)
        date_ranges, date_prefixes = (), ()
        if not boundary_uncertain and preamble_safe:
            date_ranges, date_prefixes = _preamble_date_spans(
                preamble, countries, company_names | configured_names, target_countries, target_companies,
            )
        scopes.append(Scope(
            local, shared, boundary_uncertain, first_heading=first_heading,
            preamble_dates=date_ranges, date_prefixes=date_prefixes,
            title=_policy_heading_title(heading.text) if heading else "",
            countries=frozenset(target_countries), country_sources=country_sources,
            explicit_shared=explicit_shared, duplicate_span=span if local is None else None,
        ))
    return scopes


def _headings(
    body: str, company_names: set[str], countries: dict[str, re.Pattern[str]], configured_names: set[str],
) -> tuple[list[Heading], list[Remarks], int]:
    headings = []
    remarks = []
    remark_start = None
    first_chapter = len(body)
    known_companies = {name for name in company_names if not country_matches([name], countries, exact=True)}
    lines = _content_lines(body)
    first_policy_title = next((
        line.start() for line in lines
        if _POLICY_TITLE.search(_policy_heading_title(without_list_prefix(line[0]).strip()))
        and (_ITEM.match(line[0]) or _CHAPTER.match(line[0].strip()))
    ), len(body))
    for index, line in enumerate(lines):
        if _SHARED.match(line[0]):
            continue
        if not headings:
            followed_by_date = index + 1 < len(lines) and _starts_date_field(lines[index + 1][0])
            if (followed_by_date or line.start() < first_policy_title < len(body)) and (
                _country_section(line[0], countries, followed_by_date=followed_by_date) is not None
            ):
                first_chapter = min(first_chapter, line.start())
                continue
            qualifier = _date_qualifier(line[0], countries, company_names | configured_names)
            if qualifier and (
                _QUALIFIED_DATE_LABEL.search(line[0])
                or _QUALIFIER.search(line[0]) and not _preamble_policy_field(line[0])
            ):
                continue
        bullet = _BULLET.match(line[0])
        content = line[0][bullet.end():] if bullet else line[0].lstrip()
        item = _ITEM.match(content)
        if item:
            content = content[item.end():]
        prefix = _TITLE_PREFIX.match(content)
        wrapper = _TITLE_WRAPPER.fullmatch(content)
        if prefix:
            content = content[prefix.end():].lstrip("：:、.． \t")
        elif wrapper:
            content = wrapper[2] or wrapper[3]
        prefixed = prefix is not None or wrapper is not None
        chapter = _CHAPTER.match(content)
        if chapter:
            content = content[chapter.end():]
            first_chapter = min(first_chapter, line.start())
        if not item and _REMARKS.fullmatch(content.rstrip()):
            first_chapter = min(first_chapter, line.start())
            if remark_start is None:
                remark_start = line.start()
            continue
        numbered_title = bool(item) and not _NUMBERED_FIELD.match(content)
        company_label = _COMPANY.match(content)
        company_title = company_label or _COMPANY_HEADING.match(content) or any(
            _starts_with_company(content, name) for name in company_names
        )
        if not (company_title or numbered_title or prefixed or chapter):
            continue
        name_text = content[company_label.end():] if company_label else content
        title = _heading_title(name_text)
        heading_countries = country_matches([title], countries)
        if not (company_title or numbered_title or prefixed or (chapter and heading_countries)):
            continue
        country_title = bool(heading_countries) and (
            _POLICY_TITLE.search(title) is not None or bool(country_matches([title], countries, exact=True))
        )
        company = _company_name(name_text, company_names) if company_title else None
        after_remarks = False
        remark_title = False
        if remark_start is not None:
            if numbered_title:
                if not company_label and company and (
                    _REMARK_ITEM.match(content)
                    or any(name in company and not company.startswith(name) for name in known_companies)
                ):
                    company = None
                country_title, company = _remark_title_names(name_text, company, known_companies, countries)
                remark_title = country_title or company is not None or bool(company_label and name_text.strip())
                after_remarks = remark_title or _remark_fields_follow(
                    lines, index, name_text, company_names, numbered=True,
                )
                if not after_remarks:
                    continue
            elif chapter:
                after_remarks = (
                    prefixed or country_title
                    or (not _REMARK_ITEM.match(content) and bool(company_title or heading_countries))
                    or _remark_fields_follow(lines, index, name_text, company_names)
                )
                if not after_remarks:
                    continue
            elif not (company_title or prefixed):
                continue
            remarks.append(Remarks((remark_start, line.start()), shared=not after_remarks))
            remark_start = None
        headings.append(Heading(
            line.start(), content, company, heading_countries, bool(item or chapter),
            prefixed or bool(bullet and company_title), country_title, after_remarks, remark_title, line.span(),
        ))
    if remark_start is not None:
        remarks.append(Remarks((remark_start, len(body)), shared=True))
    # 充送行在明确的单条申请标题及编号字段之后时，可作为清单中的优惠描述。
    if (
        len(headings) == 2
        and not remarks
        and headings[0].company is not None
        and not headings[0].numbered
        and re.search(r"政策申请\s*[:：]\s*$", headings[0].text)
        and headings[1].numbered
        and not headings[1].prefixed
        and _NUMBERED_DEAL.fullmatch(headings[1].text.rstrip())
        and any(
            _NUMBERED_FIELD.match(line[item.end():])
            for line in body[headings[0].start:headings[1].start].splitlines()
            if (item := _ITEM.match(line))
        )
    ):
        headings.pop()
    return headings, remarks, first_chapter


def _remark_title_names(
    text: str, company: str | None, company_names: set[str], countries: dict[str, re.Pattern[str]],
) -> tuple[bool, str | None]:
    qualifier = _TITLE_QUALIFIER.search(text)
    qualifier_text = qualifier[1].strip() if qualifier else ""
    normalized = _normalize_country_text(text)
    country_title = any(pattern.match(normalized) for pattern in countries.values()) or bool(
        country_matches([qualifier_text], countries, exact=True)
    )
    company_prefix = max(
        (name for name in company_names if text.startswith(name)), key=len, default="",
    )
    if not company_prefix and qualifier_text and _company_name(qualifier_text, company_names) == qualifier_text:
        return country_title, qualifier_text
    if len(company_prefix) > len(company or ""):
        company = company_prefix
    return country_title, company


def _remark_fields_follow(
    lines: list[re.Match[str]], index: int, text: str, company_names: set[str], *, numbered: bool = False,
) -> bool:
    def is_field(content: str) -> bool:
        return bool(_NUMBERED_FIELD.match(content) or _REMARK_FIELD.match(content))

    if not numbered:
        title = next(clause_spans(text, punctuation="：:，,。；;"), None)
        if title and is_field(text[title[1] + 1:].lstrip()):
            return True
    limit = len(lines) if numbered else min(index + 5, len(lines))
    for next_index in range(index + 1, limit):
        line = lines[next_index]
        item = _ITEM.match(_BULLET.sub("", line[0], count=1))
        if numbered and item:
            break
        content = without_list_prefix(line[0]).strip()
        if is_field(content):
            return True
        if (
            item or _CHAPTER.match(content)
            or _REMARKS.fullmatch(content) or _SHARED.match(content)
            or _TITLE_PREFIX.match(content) or _TITLE_WRAPPER.fullmatch(content)
            or _COMPANY.match(content) or _COMPANY_HEADING.match(content)
            or any(_starts_with_company(content, name) for name in company_names)
        ):
            break
    return False


def _heading_title(text: str) -> str:
    start, end = next(clause_spans(text, punctuation="：:，,。；;"), (0, 0))
    return text[start:end].strip()


def _policy_heading_title(text: str) -> str:
    title = _COMPANY.sub("", text, count=1).strip()
    if re.search(r"包\s*用户(?:\s*[（(].*[）)])?$", title) and not re.search(r"[:：；;。]", title):
        return title
    return _heading_title(title)


def _company_name(text: str, names: set[str]) -> str | None:
    matching = [name for name in names if _starts_with_company(text, name)]
    if matching:
        return max(matching, key=len)
    if match := _COMPANY_HEADING.match(text):
        return match[0].strip()
    return None


def _starts_with_company(heading: str, company: str) -> bool:
    return heading.startswith(company) and (
        re.match(r"$|[\s，,、。；;：:（(—-]", heading[len(company):]) is not None
        or _COMPANY_APPLICATION.match(heading[len(company):]) is not None
    )


def _batch_company(clause: str, names: set[str]) -> str | None:
    if not _SHARED.match(clause):
        return None
    content = re.sub(r"^(?:均)?(?:为|是|属于|适用于?)\s*", "", without_batch_intro(clause).lstrip())
    if label := _COMPANY.match(content):
        return _company_name(content[label.end():], names)
    company = _company_name(content, names)
    return company if company and _COMPANY_APPLICATION.match(content[len(company):]) else None


def _quotes_heading(body: str, heading: Heading, end: int, quotes: list[str]) -> bool:
    title = _heading_title(_COMPANY.sub("", heading.text, count=1))
    title_start = body.find(title, heading.start, end)
    return bool(title) and any(
        title in quote and any(
            start <= title_start and start + len(quote) >= title_start + len(title)
            for start in Scope((heading.start, end), ()).occurrences(body, quote)
        )
        for quote in quotes
    )


def _shared_spans(body: str, boundaries: list[int]) -> tuple[Span, ...]:
    # 存档链接也可写在单条政策之后。独立的批次标题可引出一个共享段落。
    spans = []
    for start, end in clause_spans(body, punctuation="。！？；;\r\n"):
        clause = body[start:end]
        if end < len(body) and body[end] in "。！？；;":
            end += 1
        if _BATCH_ARCHIVE.match(clause):
            comma = re.search(r"[，,]", clause)
            spans.append((start, start + comma.start() if comma else end))
            continue
        if not _SHARED.match(clause):
            continue
        remainder = without_batch_intro(clause)
        if not remainder.strip() and clause.rstrip().endswith(("：", ":")):
            stops = [boundary for boundary in boundaries if boundary > end]
            blank = re.search(r"\r?\n[ \t]*\r?\n", body[end:])
            if blank:
                stops.append(end + blank.start())
            end = min(stops, default=len(body))
        elif period := DATE_PERIOD.match(body, start + len(clause) - len(remainder)):
            if not any(end <= boundary < period.end() for boundary in boundaries):
                end = max(end, period.end())
        spans.append((start, end))
    return tuple(spans)


def without_batch_intro(clause: str) -> str:
    return _BATCH_INTRO.sub("", clause, count=1)


def without_list_prefix(clause: str) -> str:
    return _ITEM.sub("", _BULLET.sub("", clause, count=1), count=1)


def duration_in_context(body: str, start: int, end: int) -> bool:
    for left, right in clause_spans(body, punctuation="。；;，,！!？?\r\n"):
        if not left <= start < end <= right:
            continue
        if _DURATION_NEGATION.search(body, left, right):
            return False
        previous_end = None
        for match in DURATION.finditer(body, left, end):
            offset = match.start()
            prefix = body[left:offset]
            if not (
                previous_end == offset or not prefix or _DURATION_PREFIX.search(prefix)
                or not (prefix[-1].isalnum() or prefix[-1] == "_")
            ):
                continue
            if match.span() == (start, end):
                # 连写的动作必须完整；普通汉字紧接单位会使单位含义不明。
                while (
                    (join := _DURATION_JOIN.match(body, end, right))
                    and (next_duration := DURATION.match(body, join.end(), right))
                ):
                    end = next_duration.end()
                return end == right or not _HAN.match(body, end)
            previous_end = match.end()
        return False
    return False


def clause_spans(text: str, *, punctuation: str) -> Iterator[Span]:
    """只有成对括号内的标点不切断原文。"""
    openings = []
    pairs = {}
    for match in re.finditer(r"[（）()]", text):
        index = match.start()
        if match[0] in "（(":
            openings.append(index)
        elif openings:
            pairs[openings.pop()] = 1
            pairs[index] = -1

    left = depth = 0
    for match in re.finditer(rf"[（）(){re.escape(punctuation)}]", text):
        if match[0] in "（）()":
            depth += pairs.get(match.start(), 0)
        elif depth == 0:
            if left < match.start():
                yield left, match.start()
            left = match.end()
    if left < len(text):
        yield left, len(text)
