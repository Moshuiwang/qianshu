"""定位政策段落与明确共享的原文范围。"""

import re
from collections.abc import Iterator
from dataclasses import dataclass

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
    r"^(?:用户标签|用户范围|优惠政策名称|优惠类型|政策类型|审批件附件|当前节目包|"
    r"是否只触发一次|只触发一次|多买多送|生效倍数|降包用户取消优惠|充值天数|赠送天数)"
    r"\s*[:：]|"
    r"^(?:(?:是否(?:需要)?|需不需要|需要|无需|不需要|不需|不用|不)?(?:维护)?互斥组)",
    re.IGNORECASE,
)
_NUMBERED_DEAL = re.compile(r"^充(?:值)?\s*\d+\s*天\s*(?:送|赠送|赠)\s*\d+\s*天[。.]?$")
_BATCH_PREFIX = (
    r"以下各(?:条|项)(?:政策|申请)?(?:均|全部)?|"
    r"(?:本批(?:次)?|全批)(?:的)?(?:"
    r"(?:所有|各条|各项)(?:政策|申请)?(?:均|全部|统一|共同|共用|的)?|"
    r"统一|共同|共用|通用|适用|均)|"
    r"(?:以上|下列|所有|全部|各条|各项)(?:政策|申请)(?:均|全部|统一|共同|共用|适用|的)|"
    r"适用于(?:全批|本批(?:次)?(?:所有|各条)?(?:政策|申请)?)"
)
_SHARED = re.compile(rf"^\s*(?:{_BATCH_PREFIX})")
_BATCH_INTRO = re.compile(rf"^\s*(?:{_BATCH_PREFIX})\s*[:：、，,]?\s*")
_BATCH_ARCHIVE = re.compile(r"^\s*(?:本批(?:次)?|全批)(?:的)?(?:存档|归档)")


@dataclass(frozen=True)
class Scope:
    local: Span | None
    shared: tuple[Span, ...]
    boundary_uncertain: bool = False

    def spans(self) -> tuple[Span, ...]:
        return self.shared if self.local is None else (self.local, *self.shared)

    def excerpt(self, body: str, quote: str) -> SourceExcerpt | None:
        start = next(self.occurrences(body, quote), None)
        if start is None:
            return None
        first = body.find(quote)
        unique = body.find(quote, first + 1) < 0
        return SourceExcerpt(text=quote, start=start if unique else None)

    def occurrences(self, body: str, quote: str) -> Iterator[int]:
        if not quote.strip():
            return
        for left, right in self.spans():
            start = body.find(quote, left, right)
            while start >= 0:
                yield start
                start = body.find(quote, start + 1, right)


@dataclass(frozen=True)
class Heading:
    start: int
    text: str
    company: str | None
    numbered: bool
    prefixed: bool


def policy_scopes(body: str, companies: list[tuple[str | None, list[str]]]) -> list[Scope]:
    """按原文标题划分段落，用公司名绑定草稿，无法唯一绑定时保留待确认。"""
    company_names = {name for name, _ in companies if name is not None}
    headings = _headings(body, company_names)
    shared = _shared_spans(body, headings)
    batch_companies = {
        name for left, right in shared
        if (name := _batch_company(body[left:right], company_names)) is not None
    }
    if not headings:
        local = (0, len(body)) if len(companies) == 1 and len(batch_companies) <= 1 else None
        return [Scope(local, shared, boundary_uncertain=local is None) for _ in companies]

    ends = [heading.start for heading in headings[1:]] + [len(body)]
    uncertain_starts = set()
    for index, (heading, end) in enumerate(zip(headings, ends, strict=True)):
        # 共用公司下的编号标题须有对应草稿引用，才能与编号字段区分。
        if not heading.prefixed and (not heading.numbered or heading.company is not None):
            continue
        if not heading.prefixed and len(companies) > 1 and any(
            batch_companies == {company} and _quotes_heading(body, heading, end, quotes)
            for company, quotes in companies
        ):
            continue
        uncertain_starts.add(heading.start)
        if index:
            uncertain_starts.add(headings[index - 1].start)

    matches = []
    for company, quotes in companies:
        locations = [
            (heading, end) for heading, end in zip(headings, ends)
            if len(batch_companies) <= 1
            and (not batch_companies or batch_companies == {company})
            and (
                (company is not None and heading.company == company)
                or (heading.company is None and heading.numbered and batch_companies == {company})
                or (len(companies) == len(headings) == 1 and (heading.company is None or company is None))
            )
        ]
        if len(locations) > 1:
            locations = [
                (heading, end) for heading, end in locations
                if _quotes_heading(body, heading, end, quotes)
            ]
        matches.append((locations[0][0].start, locations[0][1]) if len(locations) == 1 else None)
    scopes = []
    for span in matches:
        local = span if matches.count(span) == 1 else None
        scopes.append(Scope(local, shared, boundary_uncertain=local is None or local[0] in uncertain_starts))
    return scopes


def _headings(body: str, company_names: set[str]) -> list[Heading]:
    headings = []
    for line in re.finditer(r"[^\r\n]+", body):
        if _SHARED.match(line[0]):
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
        company_label = _COMPANY.match(content)
        name_text = content[company_label.end():] if company_label else content
        if company_label or _COMPANY_HEADING.match(content) or any(
            _starts_with_company(content, name) for name in company_names
        ):
            heading = Heading(
                line.start(), content, _company_name(name_text, company_names), bool(item), prefixed or bool(bullet),
            )
        elif prefixed or (item and not _NUMBERED_FIELD.match(content)):
            heading = Heading(line.start(), content, None, bool(item), prefixed)
        else:
            continue
        headings.append(heading)
    # 充送行在明确的单条申请标题及编号字段之后时，可作为清单中的优惠描述。
    if (
        len(headings) == 2
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
    return headings


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
    text = heading.text
    if _COMPANY.match(heading.text):
        text = _COMPANY.sub("", text, count=1)
    title = re.split(r"[：:，,。；;]", text, maxsplit=1)[0].strip()
    title_start = body.find(title, heading.start, end)
    return bool(title) and any(
        title in quote and any(
            start <= title_start and start + len(quote) >= title_start + len(title)
            for start in Scope((heading.start, end), ()).occurrences(body, quote)
        )
        for quote in quotes
    )


def _shared_spans(body: str, headings: list[Heading]) -> tuple[Span, ...]:
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
        if not without_batch_intro(clause).strip() and clause.rstrip().endswith(("：", ":")):
            stops = [heading.start for heading in headings if heading.start > end]
            blank = re.search(r"\r?\n[ \t]*\r?\n", body[end:])
            if blank:
                stops.append(end + blank.start())
            end = min(stops, default=len(body))
        spans.append((start, end))
    return tuple(spans)


def without_batch_intro(clause: str) -> str:
    return _BATCH_INTRO.sub("", clause, count=1)


def without_list_prefix(clause: str) -> str:
    return _ITEM.sub("", _BULLET.sub("", clause, count=1), count=1)


def clause_spans(text: str, *, punctuation: str) -> Iterator[Span]:
    """括号内的标点属于括注，不切断原文。"""
    left = depth = 0
    for match in re.finditer(rf"[（）(){re.escape(punctuation)}]", text):
        if match[0] in "（(":
            depth += 1
        elif match[0] in "）)":
            depth = max(0, depth - 1)
        elif depth == 0:
            if left < match.start():
                yield left, match.start()
            left = match.end()
    if left < len(text):
        yield left, len(text)
