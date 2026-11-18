"""定位政策段落与明确共享的原文范围。"""

import re
from dataclasses import dataclass

from qianshu.draft import SourceExcerpt

type Span = tuple[int, int]

_ITEM = re.compile(r"^\s*(?:[①-⑳]|\d{1,3}[、)）]|\d{1,3}[.．](?!\d)|[（(]\d{1,3}[)）])\s*")
_COMPANY = re.compile(r"^\s*(?:(?:申请|适用)公司(?:名称)?|公司(?:名称)?)\s*[:：]\s*")
_COMPANY_HEADING = re.compile(r"^\s*[\w&· -]{1,40}?公司(?:[甲乙丙丁戊己庚辛壬癸\da-zA-Z]+)?(?=[：:，,。；;]|$)")
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

    def spans(self) -> tuple[Span, ...]:
        return self.shared if self.local is None else (self.local, *self.shared)

    def excerpt(self, body: str, quote: str, *, at_boundary: bool = False) -> SourceExcerpt | None:
        if not quote.strip():
            return None
        for left, right in self.spans():
            start = body.find(quote, left, right)
            while start >= 0:
                if not at_boundary or start == 0 or not body[start - 1].isalnum():
                    first = body.find(quote)
                    unique = body.find(quote, first + 1) < 0
                    return SourceExcerpt(text=quote, start=start if unique else None)
                start = body.find(quote, start + 1, right)
        return None


def policy_scopes(body: str, companies: list[tuple[str | None, list[str]]]) -> list[Scope]:
    """按原文标题划分段落，用公司名绑定草稿，无法唯一绑定时保留待确认。"""
    lines = list(re.finditer(r"[^\r\n]+", body))
    headings = [
        (line.start(), line.start() + marker.end())
        for line in lines
        if (marker := _ITEM.match(line[0]))
    ]
    if not headings:
        company_names = {name for name, _ in companies if name is not None}
        for line in lines:
            if marker := _COMPANY.match(line[0]):
                headings.append((line.start(), line.start() + marker.end()))
            elif _COMPANY_HEADING.match(line[0]) or any(
                _starts_with_company(line[0].lstrip(), name) for name in company_names
            ):
                headings.append((line.start(), line.start()))

    shared = _shared_spans(body, headings)
    if len(companies) == 1 and len(headings) <= 1:
        left = headings[0][0] if headings else 0
        return [Scope((left, len(body)), shared)]

    ends = [start for start, _ in headings[1:]] + [len(body)]
    matches = []
    for company, quotes in companies:
        locations = []
        if company is not None:
            for (start, content), end in zip(headings, ends):
                heading = body[content:end].lstrip()
                if marker := _COMPANY.match(heading):
                    heading = heading[marker.end():]
                if _starts_with_company(heading, company) or any(
                    _starts_with_company(line[marker.end():], company)
                    for line in heading.splitlines()
                    if (marker := _COMPANY.match(line))
                ):
                    locations.append((start, end))
        if len(locations) > 1:
            locations = [
                (left, right) for left, right in locations
                if any(company in quote and body.find(quote, left, right) >= 0 for quote in quotes)
            ]
        matches.append(locations[0] if len(locations) == 1 else None)
    return [Scope(span if matches.count(span) == 1 else None, shared) for span in matches]


def _starts_with_company(heading: str, company: str) -> bool:
    return re.match(rf"{re.escape(company)}(?=$|[\s，,、。；;：:（(—-])", heading) is not None


def _shared_spans(body: str, headings: list[Span]) -> tuple[Span, ...]:
    # 存档链接也可写在单条政策之后。独立的批次标题可引出一个共享段落。
    spans = []
    for match in re.finditer(r"[^。\r\n！？；;]+[。！？；;]?", body):
        if _BATCH_ARCHIVE.match(match[0]):
            comma = re.search(r"[，,]", match[0])
            spans.append((match.start(), match.start() + comma.start() if comma else match.end()))
            continue
        if not _SHARED.match(match[0]):
            continue
        start, end = match.span()
        if not without_batch_intro(match[0]).strip() and match[0].rstrip().endswith(("：", ":")):
            stops = [left for left, _ in headings if left > end]
            blank = re.search(r"\r?\n[ \t]*\r?\n", body[end:])
            if blank:
                stops.append(end + blank.start())
            end = min(stops, default=len(body))
        spans.append((start, end))
    return tuple(spans)


def without_batch_intro(clause: str) -> str:
    return _BATCH_INTRO.sub("", clause, count=1)
