"""日期格式、日历及模型声明的前言日期用途与适用对象。"""

from datetime import date

from .evidence import EvidenceReview, issue, verified_value
from .protocol import FieldResult, Policy

FIELDS = ("start_date", "end_date")

_ROLES = {"start_date": "起日", "end_date": "止日"}


def calendar_date(value: str) -> str | None:
    try:
        return value if date.fromisoformat(value).isoformat() == value else None
    except ValueError:
        return None


def verify_dates(policy: Policy, results: dict[str, FieldResult], review: EvidenceReview) -> None:
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
                (target.country is not None or target.company is not None)
                and (target.country is None or verified_value(results["country"])
                     and target.country == results["country"].value)
                and (target.company is None or not results["company"].needs_confirmation
                     and results["company"].has_value and target.company == results["company"].value)
                for target in declaration.targets
            ):
                results[field] = issue(result, "date_target_mismatch")
                break
    start, end = (results[name] for name in FIELDS)
    if verified_value(start) and verified_value(end) and start.value > end.value:
        for field in FIELDS:
            results[field] = issue(results[field], "reversed_date_range")
