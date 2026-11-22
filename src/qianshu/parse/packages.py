"""多行节目包与 R6。同一政策的所有行均通过后才投影。"""

from collections.abc import Callable
from dataclasses import replace
from datetime import date

from qianshu.draft import PackageRow

from .companies import CompanyRules
from .evidence import accept, can_use_absence, issue, verified_value
from .package_levels import Offer, PackageLevels, copy_for, unique_offer
from .protocol import FieldResult, RequestedPackage, RequestedTier, ValueSource


def package_rows(value: list[RequestedPackage]) -> list[PackageRow] | None:
    if not value:
        return None
    rows = []
    for row in value:
        if any(not name or name.strip() != name for name in (row.current_package, row.gift_package)
               if name is not None):
            return None
        rows.append(PackageRow(current_package=row.current_package, gift_package=row.gift_package))
    return rows


def _candidates(rows: tuple[Offer, ...], tiers: list[RequestedTier], day: date) -> tuple[str, ...]:
    candidates = []
    for tier in tiers:
        eligible = [row for row in rows if row.level is not None and row.kind == "基本"
                    and (tier.platform is None or tier.platform == row.platform)
                    and unique_offer(rows, row.name, "基本", day) and copy_for(rows, row, day)]
        for platform in sorted({row.platform for row in eligible}):
            group = sorted((row for row in eligible if row.platform == platform), key=lambda row: row.level)
            # 档位只影响展示顺序，保留组内所有可核对的候选。
            if tier.tier in {"高", "最高"}:
                group.reverse()
            elif tier.tier == "中":
                midpoint = (len(group) - 1) / 2
                group = [row for _, row in sorted(enumerate(group), key=lambda item: abs(item[0] - midpoint))]
            candidates.extend(f"{tier.role} · {tier.tier}档 · {row.platform} 级别 {row.level} · {row.name}"
                              for row in group)
    return tuple(dict.fromkeys(candidates))


def resolve_packages(results: dict[str, FieldResult], *, has_title: bool, levels_snapshot: Callable[[], PackageLevels],
                     rules: CompanyRules, today: date | None) -> tuple[str, ...]:
    result = results["packages"]

    def pending(code: str) -> tuple[str, ...]:
        results["packages"] = issue(replace(result, value=None, has_value=False, source=None), code)
        return ()

    if not has_title:
        return pending("package_title_required")
    tiers = results.get("package_tiers")
    tier_pending = tiers is not None and not can_use_absence(tiers)
    if not verified_value(result) and not tier_pending:
        return ()
    company = results["company"]
    if company.needs_confirmation or not verified_value(results["start_date"]):
        return pending("package_lookup_pending")
    numbers = {row.internal_number for row in (rules.table or {}).get(results["country"].value, {}).values()
               if row.display_name == company.value}
    if len(numbers) != 1 or None in numbers:
        return pending("package_lookup_pending")
    rows = levels_snapshot().for_company(company.value, numbers.pop())
    day = date.fromisoformat(results["start_date"].value)
    if today is not None:
        day = max(today, day)
    if tier_pending:
        pending("package_tier_pending")
        return _candidates(rows, tiers.value, day) if verified_value(tiers) else ()
    benefit = results["benefit_type"]
    if not verified_value(benefit):
        return pending("package_benefit_pending")
    selected = []
    seen = set()
    for row in result.value:
        current = unique_offer(rows, row.current_package, "基本", day)
        if current is None or current.id in seen:
            return pending("package_lookup_pending")
        seen.add(current.id)
        if benefit.value == "赠送时长":
            base = current
            if row.gift_package not in (None, base.name, base.name + "_copy"):
                return pending("gift_package_conflict")
        else:
            if row.gift_package is None:
                return pending("gift_package_required")
            base = unique_offer(rows, row.gift_package, "基本", day)
        gift = copy_for(rows, base, day) if base else None
        if gift is None:
            return pending("package_copy_pending")
        selected.append(PackageRow(current_package=current.name, gift_package=gift.name))
    results["packages"] = accept(result, selected, ValueSource.LOOKUP, rule="6.2.R6",
                                 inputs=("benefit_type", "company", "start_date", "packages"))
    return ()
