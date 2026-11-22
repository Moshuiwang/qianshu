"""适用国家查表、公司线索及明示主体的 R10 核对。"""

from .companies import CompanyRules
from .evidence import Sources, accept, can_use_absence, intersect, issue, verified_value
from .protocol import CountryFinding, FieldResult, Span, ValueSource
from .scope import company_mentioned, country_matches


FIELDS = ("country", "company", "company_keyword")


ROLE_CONFIRMATION_REASONS = {
    "country_role_incomplete": "请补齐国家角色、来源块和短引文，并核对本条适用地。",
    "country_evidence_unlocated": "请核对国家支持引文和角色短引文，每段引文都须能在指定来源块中唯一定位。",
    "country_role_mismatch": "请核对适用地短引文，它须唯一查到本条所报国家。",
    "country_role_conflict": "请核对适用地、背景与排除引文，检查适用地是否也被排除，或支持引文是否与非适用地引文重叠。",
}


def _unique_location(sources: Sources, block_id: str | None, text: str | None) -> Span | None:
    block = sources.blocks.get(block_id)
    if block is None or not text:
        return None
    start = block.text.find(text)
    if start < 0 or block.text.find(text, start + 1) >= 0:
        return None
    return sources.locate(Span(source_id=block.id, start=start, end=start + len(text)))


def _role_issue(country: CountryFinding, canonical: str, rules: CompanyRules, sources: Sources) -> str | None:
    support = []
    for quote in country.evidence:
        location = _unique_location(sources, quote.span.source_id, quote.text)
        if location is None:
            return "country_evidence_unlocated"
        support.append(location)

    has_applicable = False
    other_locations = []
    for mention in country.mentions or ():
        evidence = mention.evidence
        if mention.role is None or evidence is None:
            return "country_role_incomplete"
        location = _unique_location(sources, evidence.source_id, evidence.text)
        if location is None:
            return "country_evidence_unlocated"
        if mention.role != "适用地":
            other_locations.append(location)
        if mention.role == "比较或背景":
            continue
        matches = country_matches([evidence.text], rules.countries)
        if mention.role == "适用地":
            if matches != {canonical}:
                return "country_role_mismatch"
            has_applicable = True
            support.append(location)
        elif canonical in matches:
            return "country_role_conflict"

    if not has_applicable:
        return "country_role_incomplete"
    if any(intersect(left, right) for left in support for right in other_locations):
        return "country_role_conflict"
    return None


def resolve_country_company(results: dict[str, FieldResult], rules: CompanyRules, sources: Sources) -> str | None:
    """返回仅因角色核对而待确认的规范国名，供同批名称查重。"""
    pending_country = None
    country, subject, clues = (results[name] for name in FIELDS)
    if verified_value(country):
        matches = country_matches([country.value], rules.countries, exact=True)
        if len(matches) != 1:
            country = issue(country, "unknown_country")
        else:
            canonical = matches.pop()
            problem = _role_issue(country.statement, canonical, rules, sources)
            if problem:
                pending_country = canonical
                country = issue(country, problem)
            else:
                country = accept(country, canonical, ValueSource.LOOKUP, rule="R10")
        results["country"] = country

    if not verified_value(country) or not (verified_value(clues) or can_use_absence(clues)):
        results["company"] = issue(subject, "company_inputs_pending")
        return pending_country
    rows = (rules.table or {}).get(country.value, {})
    keywords = clues.value if verified_value(clues) else ()
    matches = [row for keyword, row in rows.items()
               if keyword and company_mentioned(keyword, keywords)]
    if len(matches) == 1:
        row, rule = matches[0], "R10.keyword"
    elif can_use_absence(clues) and not verified_value(subject):
        row, rule = rows.get(""), "R10.default_company"
    else:
        results["company"] = issue(subject, "company_lookup_unresolved")
        return None
    if row is None:
        results["company"] = issue(subject, "company_lookup_unresolved")
    elif verified_value(subject) and subject.value.casefold() != row.display_name.casefold():
        results["company"] = issue(subject, "company_subject_conflict")
    elif verified_value(subject) or can_use_absence(subject):
        subject_evidence = (*subject.evidence, *clues.evidence, *country.evidence)
        results["company"] = accept(subject, row.display_name, ValueSource.LOOKUP,
                                    rule=rule, inputs=FIELDS, evidence=subject_evidence)
    return None
