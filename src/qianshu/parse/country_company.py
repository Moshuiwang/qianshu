"""适用国家查表、公司线索及明示主体的 R10 核对。"""

from .companies import CompanyRules
from .evidence import accept, can_use_absence, issue, verified_value
from .protocol import FieldResult, ValueSource
from .scope import country_matches


FIELDS = ("country", "company", "company_keyword")

def resolve_country_company(results: dict[str, FieldResult], rules: CompanyRules) -> None:
    country, subject, clues = (results[name] for name in FIELDS)
    if verified_value(country):
        matches = country_matches([country.value], rules.countries, exact=True)
        if len(matches) != 1:
            country = issue(country, "unknown_country")
        else:
            country = accept(country, next(iter(matches)), ValueSource.LOOKUP, rule="R10")
        results["country"] = country

    if not verified_value(country) or not (verified_value(clues) or can_use_absence(clues)):
        results["company"] = issue(subject, "company_inputs_pending")
        return
    rows = (rules.table or {}).get(country.value, {})
    keywords = {clue.casefold() for clue in clues.value} if verified_value(clues) else set()
    matches = [row for keyword, row in rows.items() if keyword and keyword in keywords]
    row = matches[0] if len(matches) == 1 else rows.get("") if not matches else None
    if row is None:
        results["company"] = issue(subject, "company_lookup_unresolved")
    elif verified_value(subject) and subject.value.casefold() != row.display_name.casefold():
        results["company"] = issue(subject, "company_subject_conflict")
    elif verified_value(subject) or can_use_absence(subject):
        rule = "R10.keyword" if matches else "R10.default_company"
        subject_evidence = (*subject.evidence, *clues.evidence, *country.evidence)
        results["company"] = accept(subject, row.display_name, ValueSource.LOOKUP,
                                    rule=rule, inputs=FIELDS, evidence=subject_evidence)
