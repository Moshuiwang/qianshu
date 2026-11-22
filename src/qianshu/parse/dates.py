"""日期格式、日历及模型声明的前言日期用途与适用对象。"""

from datetime import date

from .companies import CompanyRules
from .evidence import EvidenceReview, issue, verified_value
from .protocol import DateTarget, FieldResult, Policy
from .scope import country_matches

FIELDS = ("start_date", "end_date")

_ROLES = {"start_date": "起日", "end_date": "止日"}


def calendar_date(value: str) -> str | None:
    try:
        return value if date.fromisoformat(value).isoformat() == value else None
    except ValueError:
        return None


def _matches_target(target: DateTarget, results: dict[str, FieldResult], rules: CompanyRules) -> bool:
    country = results["country"]
    if not verified_value(country):
        return False
    if target.country is not None and country_matches([target.country], rules.countries, exact=True) != {country.value}:
        return False
    if target.company is None:
        return target.country is not None

    company = results["company"]
    name = target.company.strip().casefold()
    if not name or company.needs_confirmation or not company.has_value:
        return False
    rows = (rules.table or {}).get(country.value, {})
    matches = {row.display_name for keyword, row in rows.items()
               if name in (row.display_name.casefold(), keyword.casefold())}
    if verified_value(company) and name == company.statement.value.strip().casefold():
        matches.add(company.value)
    return matches == {company.value}


def verify_dates(policy: Policy, results: dict[str, FieldResult], review: EvidenceReview, rules: CompanyRules) -> None:
    for field in FIELDS:
        result = results[field]
        for quote in result.evidence:
            clause = review.clauses[quote.clause_id]
            declaration = clause.date
            if declaration is None or not declaration.usable:
                results[field] = issue(result, "invalid_date_use")
                break
            if declaration.role == "申请周期":
                if not (clause.kind == "前言" or clause.scope == "共享"
                        or len(review.reply.policies) == 1 and not policy.has_title):
                    results[field] = issue(result, "invalid_date_use")
                    break
            elif declaration.role != _ROLES[field]:
                results[field] = issue(result, "invalid_date_use")
                break
            if declaration.targets is not None and not any(
                _matches_target(target, results, rules) for target in declaration.targets
            ):
                results[field] = issue(result, "date_target_mismatch")
                break
    start, end = (results[name] for name in FIELDS)
    if verified_value(start) and verified_value(end) and start.value > end.value:
        for field in FIELDS:
            results[field] = issue(results[field], "reversed_date_range")
