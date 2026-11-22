"""适用国家查表、公司线索及明示主体的 R10 核对。"""

from .companies import CompanyRules
from .evidence import EvidenceReview, accept, can_use_absence, issue, verified_value
from .protocol import FieldResult, Policy, ValueSource
from .scope import company_mentioned, country_matches


FIELDS = ("country", "company", "company_keyword")


def _verify_country(policy: Policy, country: FieldResult, review: EvidenceReview, rules: CompanyRules) -> FieldResult:
    certain = verified_value(country)
    mentions = policy.fields.country.mentions
    applicable, excluded = set(), set()
    roles = {}
    supporting_locations = {review.sources.locate(q.span) for q in country.evidence}
    for mention in mentions or ():
        if mention.role is None or mention.evidence is None:
            return issue(country, "invalid_evidence")
        applies = mention.role == "适用地"
        quote = review.verified_quote(mention.evidence)
        if quote is None:
            return issue(country, "invalid_evidence")
        location = review.sources.locate(quote.span)
        if certain and not applies and location in supporting_locations:
            return issue(country, "invalid_evidence")
        if roles.setdefault(location, mention.role) != mention.role:
            return issue(country, "country_role_conflict")
        if certain and applies:
            names = country_matches([quote.text], rules.countries, exact=True)
            if len(names) != 1:
                return issue(country, "unknown_country")
            applicable.update(names)
            if len(applicable) > 1:
                return issue(country, "invalid_evidence")
        elif certain and mention.role == "排除对象":
            excluded.update(country_matches([quote.text], rules.countries, exact=True))
    if not certain:
        return country
    matches = country_matches([country.value], rules.countries, exact=True)
    if len(matches) != 1:
        return issue(country, "unknown_country")
    if mentions is not None and (applicable != matches or matches & excluded):
        return issue(country, "country_role_conflict")
    return accept(country, next(iter(matches)), ValueSource.LOOKUP, rule="R10")


def resolve_country_company(
    policy: Policy, results: dict[str, FieldResult], review: EvidenceReview, rules: CompanyRules,
) -> None:
    country, subject, clues = (results[name] for name in FIELDS)
    country = results["country"] = _verify_country(policy, country, review, rules)

    if not verified_value(country) or not (verified_value(clues) or can_use_absence(clues)):
        results["company"] = issue(subject, "company_inputs_pending")
        return
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
        return
    if row is None:
        results["company"] = issue(subject, "company_lookup_unresolved")
    elif verified_value(subject) and subject.value.casefold() != row.display_name.casefold():
        results["company"] = issue(subject, "company_subject_conflict")
    elif verified_value(subject) or can_use_absence(subject):
        subject_evidence = (*subject.evidence, *clues.evidence, *country.evidence)
        results["company"] = accept(subject, row.display_name, ValueSource.LOOKUP,
                                    rule=rule, inputs=FIELDS, evidence=subject_evidence)
