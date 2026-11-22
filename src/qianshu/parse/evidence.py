"""核对政策锚点、逐字证据和字段状态。"""

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from itertools import combinations

from .protocol import ApplicationReply, Evidence, FieldResult, Finding, Policy, SourceBlock, Span, StatementStatus, ValueSource

STRUCTURE_REASONS = {
    "duplicate_policy_id": "政策编号重复",
    "unlocated_policy_main": "政策主段无法定位",
    "unlocated_policy_continuation": "政策续行无法定位",
    "overlapping_policy_mains": "政策主段相交",
    "empty_material": "申请材料为空",
}


@dataclass(frozen=True, repr=False)
class PolicyStructureIssue:
    span: Span | None
    code: str
    policy_ids: tuple[str, ...] = ()


class Sources:
    def __init__(self, blocks: Iterable[SourceBlock]):
        items = tuple(blocks)
        self.blocks = {block.id: block for block in items}
        if len(self.blocks) != len(items) or any(block.start < 0 for block in items):
            raise ValueError
        by_source = defaultdict(list)
        for block in items:
            for previous in by_source[block.source_id]:
                start = max(block.start, previous.start)
                end = min(block.start + len(block.text), previous.start + len(previous.text))
                if start < end and (
                    block.text[start - block.start:end - block.start]
                    != previous.text[start - previous.start:end - previous.start]
                ):
                    raise ValueError
            by_source[block.source_id].append(block)

    def locate(self, span: Span) -> Span | None:
        block = self.blocks.get(span.source_id)
        if block is None or not 0 <= span.start < span.end <= len(block.text):
            return None
        return Span(source_id=block.source_id, start=block.start + span.start, end=block.start + span.end)

    def locate_quote(self, span: Span, text: str) -> tuple[Span | None, str]:
        block = self.blocks.get(span.source_id)
        if block is None or not text:
            return None, "not_found"
        start = block.text.find(text)
        if start < 0:
            return None, "not_found"
        nearest = start
        tied = False
        while (start := block.text.find(text, start + 1)) >= 0:
            distance = abs(start - span.start)
            best = abs(nearest - span.start)
            if distance < best:
                nearest, tied = start, False
            elif distance == best:
                tied = True
        if tied:
            return None, "ambiguous"
        end = nearest + len(text)
        outcome = "exact" if (nearest, end) == (span.start, span.end) else "relocated"
        return span.model_copy(update={"start": nearest, "end": end, "text": None}), outcome


def intersect(left: Span | None, right: Span | None) -> Span | None:
    if left is None or right is None or left.source_id != right.source_id:
        return None
    start, end = max(left.start, right.start), min(left.end, right.end)
    return Span(source_id=left.source_id, start=start, end=end) if start < end else None


def issue(result: FieldResult, code: str) -> FieldResult:
    return replace(result, needs_confirmation=True, issues=(*result.issues, code))


def verified_value(result: FieldResult) -> bool:
    return (result.statement.status is StatementStatus.CERTAIN and result.has_value
            and not result.needs_confirmation and not result.issues)


def can_use_absence(result: FieldResult) -> bool:
    return result.statement.status is StatementStatus.UNWRITTEN and not result.issues


def accept(
    result: FieldResult, value: object, source: ValueSource, *,
    rule: str | None = None, inputs: tuple[str, ...] = (), evidence: tuple[Evidence, ...] | None = None,
) -> FieldResult:
    eligible = verified_value(result) or can_use_absence(result)
    return replace(result, value=value, has_value=True, source=source, needs_confirmation=not eligible,
                   rule=rule, input_fields=inputs, evidence=result.evidence if evidence is None else evidence)


def default(result: FieldResult, value: object, rule: str = "6.2") -> FieldResult:
    return accept(result, value, ValueSource.DEFAULT, rule=rule) if can_use_absence(result) else result


def _quotes(statement: Finding) -> Iterable[Evidence]:
    yield from statement.evidence
    for candidate in statement.candidates:
        yield from candidate.evidence


def _declared_quotes(reply: ApplicationReply) -> Iterable[tuple[Span, str]]:
    for policy in reply.policies:
        for span in (policy.main, *policy.continuations):
            if span.text is not None:
                yield span, span.text
        for mention in policy.fields.country.mentions or ():
            if mention.evidence is not None:
                yield mention.evidence.span, mention.evidence.text
        for name in policy.fields.__class__.model_fields:
            statement = getattr(policy.fields, name)
            for quote in _quotes(statement):
                yield quote.span, quote.text


class EvidenceReview:
    def __init__(self, reply: ApplicationReply, sources: Sources):
        self.sources = sources
        self.evidence_locations: Counter[str] = Counter()
        self._locations: dict[tuple[Span, str], tuple[Span | None, str]] = {}
        for span, text in _declared_quotes(reply):
            key = span, text
            if key not in self._locations:
                self._locations[key] = sources.locate_quote(span, text)
            self.evidence_locations[self._locations[key][1]] += 1
        self.main = {id(policy): self._locate(policy.main) for policy in reply.policies}
        self.structure_issues = tuple(dict.fromkeys(self._check_structure(reply.policies)))

    def corrected_span(self, span: Span) -> Span | None:
        return span if span.text is None else self._locations[span, span.text][0]

    def _locate(self, span: Span) -> Span | None:
        corrected = self.corrected_span(span)
        return self.sources.locate(corrected) if corrected is not None else None

    def _locate_all(self, spans: Iterable[Span]) -> tuple[Span, ...]:
        return tuple(located for span in spans if (located := self._locate(span)) is not None)

    def _check_structure(self, policies: list[Policy]) -> Iterable[PolicyStructureIssue]:
        ids = Counter(policy.id for policy in policies)
        for policy in policies:
            main = self.main[id(policy)]
            if ids[policy.id] > 1:
                yield PolicyStructureIssue(main, "duplicate_policy_id", (policy.id,))
            if main is None:
                yield PolicyStructureIssue(None, "unlocated_policy_main", (policy.id,))
            if len(self._locate_all(policy.continuations)) != len(policy.continuations):
                yield PolicyStructureIssue(None, "unlocated_policy_continuation", (policy.id,))
        for first, second in combinations(policies, 2):
            if overlap := intersect(self.main[id(first)], self.main[id(second)]):
                yield PolicyStructureIssue(overlap, "overlapping_policy_mains", tuple(dict.fromkeys((first.id, second.id))))

    def verified_quote(self, quote: Evidence) -> Evidence | None:
        span = self._locations[quote.span, quote.text][0]
        return quote.model_copy(update={"span": span}) if span is not None else None

    def field(self, policy: Policy, name: str) -> FieldResult:
        statement = getattr(policy.fields, name)
        quotes = [self.verified_quote(quote) for quote in _quotes(statement)]
        valid = tuple(quote for quote in quotes[:len(statement.evidence)] if quote is not None)
        codes = ["invalid_evidence"] if None in quotes else []
        status = statement.status
        if status is StatementStatus.UNWRITTEN and (statement.evidence or statement.candidates or statement.value is not None):
            codes.append("invalid_unwritten_shape")
        if status is StatementStatus.CERTAIN and (not valid or statement.candidates):
            codes.append("invalid_certain_evidence")
        verified = status is StatementStatus.CERTAIN and not codes
        return FieldResult(
            statement=statement, value=statement.value if verified else None, has_value=verified,
            source=ValueSource.ORIGINAL if verified else None, needs_confirmation=not verified,
            issues=tuple(codes), evidence=valid,
        )
