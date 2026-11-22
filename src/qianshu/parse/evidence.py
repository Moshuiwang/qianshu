"""共用核对：坐标映射、条款归属、证据、覆盖和字段状态。"""

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from itertools import chain, combinations

from .protocol import (
    ApplicationReply, Clause, DocumentStatus, Evidence, FieldResult, Finding, Findings,
    Policy, SourceBlock, Span, StatementStatus, ValueSource,
)

FIELD_IDS = frozenset(Findings.model_fields)


@dataclass(frozen=True, repr=False)
class ReviewRange:
    span: Span | None
    code: str
    policy_ids: tuple[str, ...] = ()
    fields_by_policy: dict[str, frozenset[str]] | None = None
    block_id: str | None = None
    reason_code: str | None = None

    @property
    def requires_full_review(self) -> bool:
        fields_by_policy = self.fields_by_policy or {}
        return not self.policy_ids or any(not fields_by_policy.get(policy_id) for policy_id in self.policy_ids)

    def affects_field(self, policy_id: str, name: str) -> bool:
        return name in (self.fields_by_policy or {}).get(policy_id, ())


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


def uncovered(span: Span, spans: Iterable[Span]) -> Iterable[Span]:
    intervals = sorted((part.start, part.end) for other in spans if (part := intersect(span, other)))
    start = span.start
    for left, right in intervals:
        if start < left:
            yield Span(source_id=span.source_id, start=start, end=left)
        start = max(start, right)
    if start < span.end:
        yield Span(source_id=span.source_id, start=start, end=span.end)


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
        for name in policy.fields.__class__.model_fields:
            statement = getattr(policy.fields, name)
            for quote in _quotes(statement):
                yield quote.span, quote.text
            for span in statement.checked:
                if span.text is not None:
                    yield span, span.text
    for span in chain(
        (span for clause in reply.clauses for span in clause.spans),
        (item.span for item in reply.coverage),
        (item.span for item in reply.pending),
    ):
        if span.text is not None:
            yield span, span.text


class EvidenceReview:
    """核对坐标和模型声明，并按声明关联待确认字段。"""

    def __init__(self, reply: ApplicationReply, sources: Sources):
        self.reply = reply
        self.sources = sources
        self.evidence_locations: Counter[str] = Counter()
        self._locations: dict[tuple[Span, str], tuple[Span | None, str]] = {}
        for span, text in _declared_quotes(reply):
            key = span, text
            if key not in self._locations:
                self._locations[key] = sources.locate_quote(span, text)
            self.evidence_locations[self._locations[key][1]] += 1
        self.pending: list[ReviewRange] = []
        self.policies: dict[str, list[Policy]] = defaultdict(list)
        for policy in reply.policies:
            self.policies[policy.id].append(policy)
        self.clauses = {clause.id: clause for clause in reply.clauses}
        self.duplicate_policy_ids = {key for key, policies in self.policies.items() if len(policies) > 1}
        self.invalid_clauses = {key for key, count in Counter(c.id for c in reply.clauses).items() if count > 1}
        self.main = {id(p): self._locate(p.main) for p in reply.policies}
        self.continuations = {id(p): self._locate_all(p.continuations) for p in reply.policies}
        self.local = {id(p): tuple(filter(None, (self.main[id(p)], *self.continuations[id(p)])))
                      for p in reply.policies}
        self.clause_spans = {c.id: self._locate_all(c.spans) for c in reply.clauses}
        self.applicable: dict[str, list[Clause]] = defaultdict(list)
        self._check_structure()
        self._check_coverage()
        for pending in reply.pending:
            span = self._locate(pending.span)
            by_policy = {item.policy_id: frozenset(item.field_ids) for item in pending.fields_by_policy}
            if (set(pending.policy_ids) - self.policies.keys()
                or len(by_policy) != len(pending.fields_by_policy) or set(by_policy) - set(pending.policy_ids)
                or any(ids - FIELD_IDS for ids in by_policy.values())):
                self.pending.append(ReviewRange(span, "invalid_pending_reference", reason_code=pending.reason_code))
            else:
                code = "declared_pending" if span is not None else "invalid_pending_reference"
                self.pending.append(ReviewRange(span, code, tuple(pending.policy_ids), by_policy,
                                                reason_code=pending.reason_code))
        if reply.document:
            if reply.document.status is not DocumentStatus.NO_POLICY:
                self.pending.append(ReviewRange(None, "document_pending", reason_code=reply.document.reason_code))
            elif reply.policies:
                self.pending.append(ReviewRange(None, "document_policy_conflict", reason_code=reply.document.reason_code))
        unique = dict.fromkeys(
            (item.span, item.code, item.policy_ids, tuple(sorted((item.fields_by_policy or {}).items())),
             item.block_id, item.reason_code)
            for item in self.pending
        )
        self.pending = [
            ReviewRange(span, code, ids, dict(by_policy), block_id, reason_code)
            for span, code, ids, by_policy, block_id, reason_code in unique
        ]

    def corrected_span(self, span: Span) -> Span | None:
        return span if span.text is None else self._locations[span, span.text][0]

    def _locate(self, span: Span) -> Span | None:
        corrected = self.corrected_span(span)
        return self.sources.locate(corrected) if corrected is not None else None

    def _locate_all(self, spans: Iterable[Span]) -> tuple[Span, ...]:
        return tuple(located for span in spans if (located := self._locate(span)) is not None)

    def _check_structure(self) -> None:
        for policy in self.reply.policies:
            main = self.main[id(policy)]
            problems = []
            if policy.id in self.duplicate_policy_ids:
                problems.append((main, "duplicate_policy_id"))
            if main is None:
                problems.append((None, "unlocated_policy_main"))
            if len(self.continuations[id(policy)]) != len(policy.continuations):
                problems.append((None, "unlocated_policy_continuation"))
            if (len(set(policy.shared_clause_ids)) != len(policy.shared_clause_ids)
                or set(policy.shared_clause_ids) - self.clauses.keys()):
                problems.append((main, "invalid_policy_shared_clause_ids"))
            self.pending.extend(ReviewRange(span, "invalid_policy", (policy.id,), reason_code=code)
                                for span, code in problems)
        for first, second in combinations(self.reply.policies, 2):
            if overlap := intersect(self.main[id(first)], self.main[id(second)]):
                ids = tuple(dict.fromkeys((first.id, second.id)))
                self.pending.append(ReviewRange(overlap, "overlapping_policy_mains", ids))

        references = defaultdict(set)
        field_references = defaultdict(lambda: defaultdict(set))
        for policy in self.reply.policies:
            for clause_id in policy.shared_clause_ids:
                references[clause_id].add(policy.id)
            for name in FIELD_IDS:
                for quote in _quotes(getattr(policy.fields, name)):
                    field_references[quote.clause_id][policy.id].add(name)
        for clause in self.reply.clauses:
            locations = tuple(self._locate(span) for span in clause.spans)
            spans = tuple(filter(None, locations))
            ids = set(clause.policy_ids)
            invalid = (
                clause.id in self.invalid_clauses or not clause.spans
                or any(span.text is None and self.sources.locate(span) is None for span in clause.spans)
                or not ids or len(ids) != len(clause.policy_ids) or bool(ids - self.policies.keys())
                or bool(set(clause.field_ids) - FIELD_IDS)
            )
            if clause.scope == "局部":
                invalid |= len(ids) != 1 or bool(references[clause.id]) or not any(
                    self._local_clause_fits(policy, spans) for owner in ids for policy in self.policies.get(owner, ())
                )
            else:
                invalid |= ids != references[clause.id]
            if (clause.kind == "备注" and clause.scope == "局部" and len(clause.policy_ids) == 1
                and len(self.policies.get(clause.policy_ids[0], ())) == 1):
                self._check_remark(clause, spans)
            if invalid or len(spans) != len(clause.spans):
                self.invalid_clauses.add(clause.id)
                owning_ids = (ids | references[clause.id]) & self.policies.keys()
                by_policy = {policy_id: set() for policy_id in owning_ids}
                for policy_id in owning_ids:
                    for name in FIELD_IDS.intersection(clause.field_ids):
                        if invalid or any(getattr(p.fields, name).status is StatementStatus.UNWRITTEN
                                          for p in self.policies[policy_id]):
                            by_policy[policy_id].add(name)
                for policy_id, names in field_references[clause.id].items():
                    by_policy.setdefault(policy_id, set()).update(names)
                fields = {policy_id: frozenset(names) for policy_id, names in by_policy.items()}
                reason = None if invalid else "unlocated_clause_quote"
                self.pending.extend(ReviewRange(location, "invalid_clause", tuple(sorted(fields)), fields,
                                                reason_code=reason) for location in locations or (None,))
                continue
            corrected = clause.model_copy(update={"spans": [self.corrected_span(span) for span in clause.spans]})
            for policy_id in clause.policy_ids:
                self.applicable[policy_id].append(corrected)

    def _local_clause_fits(self, policy: Policy, spans: tuple[Span, ...]) -> bool:
        # 政策坐标不完整由整卡处理；完整坐标才能证明条款越界。
        local = self.local[id(policy)]
        return len(local) != 1 + len(policy.continuations) or not any(
            any(uncovered(span, local)) for span in spans
        )

    def _check_remark(self, clause: Clause, spans: tuple[Span, ...]) -> None:
        owner = clause.policy_ids[0]
        owning_policy, = self.policies[owner]
        for policy in self.reply.policies:
            if policy.id == owner:
                continue
            reference_spans = tuple(
                part for name in FIELD_IDS for part in self._reference_spans(getattr(policy.fields, name))
            )
            for remark in spans:
                collisions = {overlap for span in (*self.local[id(policy)], *reference_spans)
                              if (overlap := intersect(remark, span))}
                for overlap in sorted(collisions, key=lambda span: (span.source_id, span.start, span.end)):
                    by_policy = {p.id: self._fields_at(p, overlap) for p in (owning_policy, policy)}
                    self.pending.append(ReviewRange(overlap, "local_remark_overlap", (owner, policy.id), by_policy))

    def _reference_spans(self, statement: Finding) -> Iterable[Span]:
        for quote in _quotes(statement):
            if span := self._locations[quote.span, quote.text][0]:
                yield self.sources.locate(span)
                yield from self.clause_spans.get(quote.clause_id, ())

    def _fields_at(self, policy: Policy, span: Span) -> frozenset[str]:
        return frozenset(
            name for name in FIELD_IDS
            if any(intersect(span, part) for part in self._reference_spans(getattr(policy.fields, name)))
        )

    def _check_coverage(self) -> None:
        support = defaultdict(list)
        for policy in self.reply.policies:
            if self.main[id(policy)]:
                support["政策条款"].append(self.main[id(policy)])
            support["续行"].extend(self.continuations[id(policy)])
        for clause in self.reply.clauses:
            use = "政策条款" if clause.kind == "正文" else clause.kind
            # 已列为待核的结构无效条款仍能说明已定位坐标的覆盖用途。
            spans = self._locate_all(clause.spans) if clause.id in self.invalid_clauses else self.clause_spans[clause.id]
            support[use].extend(spans)

        coverage = []
        described_blocks = set()
        declared_pending = self._locate_all(pending.span for pending in self.reply.pending)
        for item in self.reply.coverage:
            span = self._locate(item.span)
            if span is None:
                self.pending.append(ReviewRange(None, "invalid_coverage"))
                continue
            described_blocks.add(item.span.source_id)
            if item.use in ("其他文本", "待确认范围"):
                coverage.append(span)
                if item.use == "待确认范围":
                    for gap in uncovered(span, declared_pending):
                        self.pending.append(self._inferred_range(gap, "unbound_pending_coverage"))
            else:
                supported = [part for item_span in support[item.use] if (part := intersect(span, item_span))]
                coverage.extend(supported)
                for gap in uncovered(span, supported):
                    self.pending.append(self._inferred_range(gap, "unsupported_coverage_use"))
        for block in self.sources.blocks.values():
            if block.text:
                span = Span(source_id=block.source_id, start=block.start, end=block.start + len(block.text))
                if block.id not in described_blocks:
                    self.pending.append(ReviewRange(span, "missing_block_coverage", block_id=block.id))
                for gap in uncovered(span, coverage):
                    self.pending.append(self._inferred_range(gap, "coverage_gap"))

    def _inferred_range(self, span: Span, code: str) -> ReviewRange:
        fields_by_policy = {}
        for policy in self.reply.policies:
            names = frozenset(
                name for name in FIELD_IDS
                if not any(uncovered(span, self._reference_spans(getattr(policy.fields, name))))
            )
            applicable = (*self.local[id(policy)], *(
                part for clause in self.reply.clauses if policy.id in clause.policy_ids
                for part in self.clause_spans[clause.id]
            ))
            if names or any(intersect(span, part) for part in applicable):
                fields_by_policy[policy.id] = fields_by_policy.get(policy.id, frozenset()) | names
        return ReviewRange(span, code, tuple(fields_by_policy), fields_by_policy)

    def _pending_codes(self, policy: Policy, name: str) -> Iterable[str]:
        if policy.id in self.duplicate_policy_ids:
            return
        for pending in self.pending:
            if pending.affects_field(policy.id, name):
                yield pending.code

    def verified_quote(self, policy: Policy, name: str, quote: Evidence) -> Evidence | None:
        span = self._locations[quote.span, quote.text][0]
        clause = self.clauses.get(quote.clause_id)
        if (span is not None and clause is not None and clause.id not in self.invalid_clauses
            and policy.id in clause.policy_ids and name in clause.field_ids
            and (clause.kind != "前言" or name in ("start_date", "end_date"))
            and (clause.scope != "局部" or self._local_clause_fits(policy, self.clause_spans[clause.id]))
            and not any(uncovered(self.sources.locate(span), self.clause_spans[clause.id]))):
            return quote.model_copy(update={"span": span})
        return None

    def field(self, policy: Policy, name: str) -> FieldResult:
        statement = getattr(policy.fields, name)
        codes = list(self._pending_codes(policy, name))
        quotes = list(_quotes(statement))
        verified_quotes = [self.verified_quote(policy, name, quote) for quote in quotes]
        valid = tuple(q for q in verified_quotes[:len(statement.evidence)] if q is not None)
        if None in verified_quotes:
            codes.append("invalid_evidence")
        if any(self.clauses[q.clause_id].status != "确定" or self.clauses[q.clause_id].disposition != "生效"
               for q in quotes if q.clause_id in self.clauses):
            codes.append("uncertain_clause")

        status = statement.status
        if status is StatementStatus.UNWRITTEN:
            checked = self._locate_all(statement.checked)
            applicable = [clause for clause in self.applicable[policy.id] if name in clause.field_ids
                          and (clause.scope != "局部" or self._local_clause_fits(policy, self.clause_spans[clause.id]))]
            required = (*self.local[id(policy)], *(
                span for clause in applicable for span in self.clause_spans[clause.id]
            ))
            if (quotes or statement.candidates or statement.value is not None or not checked
                or len(checked) != len(statement.checked) or any(any(uncovered(span, checked)) for span in required)):
                codes.append("invalid_checked_range")
            if any(clause.status != "确定" or clause.disposition != "作废" for clause in applicable):
                codes.append("uncertain_clause")
        elif status is StatementStatus.CERTAIN and (not valid or statement.candidates):
            codes.append("invalid_certain_evidence")
        verified = status is StatementStatus.CERTAIN and not codes
        return FieldResult(
            statement=statement, value=statement.value if verified else None, has_value=verified,
            source=ValueSource.ORIGINAL if verified else None, needs_confirmation=not verified,
            issues=tuple(dict.fromkeys(codes)), evidence=valid,
        )
