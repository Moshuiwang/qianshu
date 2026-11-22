"""多行节目包与 R6。同一政策的所有行均通过后才投影。"""

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime

from qianshu.draft import PackageRow

from .companies import CompanyRules
from .evidence import accept, issue, verified_value
from .package_levels import Offer, PackageLevels, copy_for, unique_offer
from .protocol import Evidence, FieldResult, RequestedPackage, RequestedTier, ValueSource


class _PendingRows(list[RequestedPackage]):
    """保持原声明的行与序列化内容，附上待核投影所用的引文。"""

    def __init__(self, rows: Sequence[RequestedPackage], quotes: Sequence[Evidence]):
        super().__init__(rows)
        self.quotes = quotes

    def display(self) -> list[dict[str, str]]:
        shown = []
        for index, row in enumerate(self, 1):
            names = {
                role: name for role, name in (("当前包", row.current_package), ("活动期受赠包", row.gift_package))
                if name and name.strip() == name and any(name in quote.text for quote in self.quotes)
            }
            if names:
                shown.append({"节目包行": str(index), **names, "状态": "未查表、不可建单"})
        return shown


def _unlooked_up(result: FieldResult) -> FieldResult:
    statement = result.statement
    # 字段用已定位的引文；独立候选还会在投影时分别通过 verified_quote。
    value = _PendingRows(statement.value, result.evidence) if statement.value is not None else None
    candidates = [candidate.model_copy(update={"value": _PendingRows(candidate.value, candidate.evidence)})
                  for candidate in statement.candidates]
    return replace(result, statement=statement.model_copy(update={"value": value, "candidates": candidates}))


def package_rows(value: list[RequestedPackage]) -> list[PackageRow] | list[dict[str, str]] | None:
    if isinstance(value, _PendingRows):
        return value.display() or None
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
        eligible = [row for row in rows if row.active(day) and row.level is not None and row.kind == "基本"
                    and (tier.platform is None or tier.platform == row.platform)
                    and unique_offer(rows, row.name, "基本", day)
                    and (tier.role == "当前包" or copy_for(rows, row, day))]
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
    candidates = ()

    def pending(code: str, *, unlooked_up: bool = False) -> tuple[str, ...]:
        cleared = replace(result, value=None, has_value=False, source=None)
        results["packages"] = issue(_unlooked_up(cleared) if unlooked_up else cleared, code)
        return candidates

    if not has_title:
        return pending("package_title_required", unlooked_up=True)
    tiers = results.get("package_tiers")
    verified_tier = tiers is not None and verified_value(tiers)
    company = results["company"]
    if company.needs_confirmation or not verified_value(results["start_date"]):
        return pending("package_lookup_pending", unlooked_up=True)
    benefit = results["benefit_type"]
    if not verified_value(benefit):
        return pending("package_benefit_pending", unlooked_up=True)
    if not verified_value(result) and not verified_tier:
        results["packages"] = _unlooked_up(result)
        return ()
    numbers = {row.internal_number for row in (rules.table or {}).get(results["country"].value, {}).values()
               if row.display_name == company.value}
    if len(numbers) != 1 or None in numbers:
        return pending("package_lookup_pending", unlooked_up=True)
    rows = levels_snapshot().for_company(company.value, numbers.pop())
    day = date.fromisoformat(results["start_date"].value)
    day = max(today or datetime.now(UTC).date(), day)
    candidates = _candidates(rows, tiers.value, day) if verified_tier else ()
    if not verified_value(result):
        return pending("package_tier_pending", unlooked_up=True)
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
    return candidates
