"""当前节目包、受赠包与 R6 的规则。"""

from qianshu.draft import PackageRow

from .evidence import accept, issue, verified_value
from .protocol import FieldResult, RequestedPackage, ValueSource


def package_rows(value: list[RequestedPackage]) -> list[PackageRow] | None:
    if len(value) != 1:
        return None
    row = value[0]
    if (not row.current_package or row.current_package != row.current_package.strip()
        or row.gift_package is not None and (
            not row.gift_package or row.gift_package != row.gift_package.strip()
        )):
        return None
    return [PackageRow(current_package=row.current_package, gift_package=row.gift_package)]


def resolve_packages(results: dict[str, FieldResult], *, has_title: bool) -> None:
    result = results["packages"]
    if not has_title:
        results["packages"] = issue(result, "package_title_required")
        return
    benefit = results["benefit_type"]
    if not verified_value(result):
        return
    if not verified_value(benefit):
        results["packages"] = issue(result, "package_benefit_pending")
        return
    row = result.value[0]
    base = row.current_package if benefit.value == "赠送时长" else row.gift_package
    if base is None:
        results["packages"] = issue(result, "gift_package_required")
        return
    gift = base if base.endswith("_copy") else f"{base}_copy"
    if benefit.value == "赠送时长" and row.gift_package not in (None, base, gift):
        results["packages"] = issue(result, "gift_package_conflict")
        return
    results["packages"] = accept(result, [PackageRow(current_package=row.current_package, gift_package=gift)],
                                 ValueSource.LOOKUP, rule="6.2.R6", inputs=("benefit_type", "packages"))
