"""优惠类型、时长格式与业务默认值。"""

from dataclasses import dataclass, replace
from datetime import date
from fractions import Fraction
from math import isfinite
from pathlib import Path

from pydantic import ConfigDict, TypeAdapter, ValidationError

from qianshu.draft import PolicyDraft, TariffReference

from .evidence import accept, can_use_absence, default, issue, verified_value
from .protocol import Duration, FieldResult, RequestedTariff, ValueSource

_TYPES = {"赠送时长", "升级权益", "特价优惠"}
_MONTH_HINT = "充值 / 赠送天数按 1 月 = 30 天换算"
_DOWNGRADE_HINT = "降包用户取消优惠未写，请在卡片确认具体取值"
_DURATION_FIELDS = ("recharge_days", "gift_days", "watch_days", "penalty_days")
_AFTER_OFFER_FIELDS = {"after_offer", "after_offer_package", "forbid_package_change"}
_TARIFF_FIELDS = {"tariff", "tariff_multiplier"}
_SPECIAL_FIELDS = {"amount_mode", "recharge_amount", "watch_days", "first_recharge_day_only", *_TARIFF_FIELDS}
_CHOICES = {
    "benefit_type": ("赠送时长", "升级权益", "特价优惠"),
    "amount_mode": ("自定义", "节目包资费"),
    "after_offer": ("恢复原包", "指定节目包"),
    "cancel_on_downgrade": ("是", "否"),
    "forbid_package_change": ("是", "否"),
    "first_recharge_day_only": ("是",),
}

OFFER_FIELD_LABELS = {
    "watch_days": "可看天数",
    "penalty_days": "罚停天数",
    "amount_mode": "指定金额方式",
    "recharge_amount": "充值金额",
    "tariff": "关联资费",
    "tariff_multiplier": "资费倍数",
    "after_offer": "优惠后是否升包",
    "after_offer_package": "优惠到期目标包",
    "forbid_package_change": "获得优惠后不允许换包",
    "first_recharge_day_only": "仅首充日享受",
}
DEFAULT_HINTS = {
    "cancel_on_downgrade": "降包用户取消优惠按默认填是",
    "after_offer": "优惠后是否升包按默认填恢复原包",
    "forbid_package_change": "获得优惠后不允许换包按默认填是",
}
ADJUSTMENT_HINTS = {
    _MONTH_HINT, _DOWNGRADE_HINT, *DEFAULT_HINTS.values(),
    "可看天数按 1 月 = 30 天换算", "罚停天数按 1 月 = 30 天换算",
}
CONFIRMATION_REASONS = {
    "downgrade_confirmation": _DOWNGRADE_HINT,
    "tariff_lookup_unresolved": "无法查到该基本包、价格计划和资费单元的唯一有效资费版本。",
    "tariff_amount_pending": "须核对关联资费及资费倍数后才能确认充值金额。",
    "tariff_amount_mismatch": "正文金额与资费乘倍数向下取整的结果不一致。",
    "after_offer_package_lookup_unresolved": "到期目标包无法在受信表中唯一确认。",
}


@dataclass(frozen=True, kw_only=True, repr=False)
class TariffRate:
    __pydantic_config__ = ConfigDict(extra="forbid", allow_inf_nan=False)

    base_package: str
    price_plan: str
    unit: str
    version: str
    amount: int | float
    valid_from: date
    valid_until: date | None = None


@dataclass(frozen=True, kw_only=True, repr=False)
class BenefitCatalog:
    """调用方提供的受信资费版本与节目包名称。"""

    __pydantic_config__ = ConfigDict(extra="forbid")

    tariffs: tuple[TariffRate, ...] = ()
    package_names: tuple[str, ...] = ()

    def rate(self, reference: TariffReference, on: date | None) -> TariffRate | None:
        if on is None:
            return None
        matches = [
            rate for rate in self.tariffs
            if (rate.base_package, rate.price_plan, rate.unit) == (
                reference["base_package"], reference["price_plan"], reference["unit"],
            )
            and (reference["version"] is None or rate.version == reference["version"])
            and rate.valid_from <= on and (rate.valid_until is None or on <= rate.valid_until)
        ]
        if len(matches) == 1 and matches[0].version.strip() and _number(matches[0].amount):
            return matches[0]
        return None


DEFAULT_CATALOG_FILE = Path(".private/benefit-catalog.json")
_CATALOG_SCHEMA = TypeAdapter(BenefitCatalog)


def load_benefit_catalog(path: Path = DEFAULT_CATALOG_FILE) -> BenefitCatalog:
    try:
        return _CATALOG_SCHEMA.validate_json(path.read_bytes(), strict=True)
    except (OSError, ValidationError):
        return BenefitCatalog()


def benefit_option(value: str, options: dict[str, str]) -> str | None:
    value = options.get(value, value)
    return value if value in _TYPES else None


def duration_days(value: Duration) -> int | None:
    if value.quantity < 0:
        return None
    return value.quantity * (30 if value.unit == "月" else 1)


def _number(value: object, *, positive: bool = False) -> bool:
    if type(value) not in (int, float) or (type(value) is float and not isfinite(value)):
        return False
    return value > 0 if positive else value >= 0


def normalize_benefit_value(name: str, value: object) -> object:
    if name in _DURATION_FIELDS:
        return duration_days(value)
    if name in ("recharge_amount", "tariff_multiplier"):
        return value if _number(value, positive=name == "tariff_multiplier") else None
    if name == "tariff":
        reference: RequestedTariff = value
        if not all(part.strip() for part in (reference.base_package, reference.price_plan, reference.unit)):
            return None
        if reference.version is not None and not reference.version.strip():
            return None
        return reference.model_dump()
    if isinstance(value, str):
        return value.strip() or None
    return value


def _inactive_fields(kind, amount_mode, after_offer) -> set[str]:
    if kind == "赠送时长":
        return _SPECIAL_FIELDS | _AFTER_OFFER_FIELDS
    if kind not in ("升级权益", "特价优惠"):
        return set()
    inactive = set(_SPECIAL_FIELDS if kind == "升级权益" else ("recharge_days", "gift_days"))
    if amount_mode == "自定义":
        inactive.update(_TARIFF_FIELDS)
    if after_offer == "恢复原包":
        inactive.add("after_offer_package")
    return inactive


def _required_fields(kind, amount_mode, after_offer) -> set[str]:
    required = {"recharge_days", "gift_days"} if kind != "特价优惠" else {
        "watch_days", "amount_mode", "recharge_amount",
    }
    if kind in ("升级权益", "特价优惠"):
        required.update(("after_offer", "forbid_package_change"))
        if after_offer == "指定节目包":
            required.add("after_offer_package")
    if kind == "特价优惠" and amount_mode == "节目包资费":
        required.update(_TARIFF_FIELDS)
    return required


def benefit_applicability(draft: PolicyDraft) -> tuple[set[str], set[str]]:
    def confirmed(name: str) -> object:
        field = getattr(draft, name, None)
        return field.value if field is not None and field.uncertainty is None else None

    values = (confirmed("benefit_type"), confirmed("amount_mode"), confirmed("after_offer"))
    return _inactive_fields(*values), _required_fields(*values)


def benefit_field_format(name: str, value: object) -> bool:
    if name in _DURATION_FIELDS:
        return type(value) is int and value >= 0
    if name in ("recharge_amount", "tariff_multiplier"):
        return _number(value, positive=name == "tariff_multiplier")
    if name == "tariff":
        return (
            isinstance(value, dict) and value.keys() == TariffReference.__required_keys__
            and all(isinstance(value[key], str) and value[key].strip() for key in value)
        )
    return name not in _CHOICES or value in _CHOICES[name]


def tariff_amount(unit_amount: int | float, multiplier: int | float) -> int:
    return (Fraction(str(unit_amount)) * Fraction(str(multiplier))) // 1


def _checked_value(results: dict[str, FieldResult], name: str) -> object:
    result = results[name]
    return result.value if result.has_value and not result.needs_confirmation else None


def _resolve_tariff(results: dict[str, FieldResult], catalog: BenefitCatalog, today: date | None) -> None:
    tariff = results["tariff"]
    amount = results["recharge_amount"]
    start = date.fromisoformat(results["start_date"].value) if verified_value(results["start_date"]) else None
    on = max(start, today) if start is not None and today is not None else start
    rate = catalog.rate(tariff.value, on) if verified_value(tariff) else None
    if rate is not None:
        results["tariff"] = accept(
            tariff, {**tariff.value, "version": rate.version}, ValueSource.LOOKUP,
            inputs=("start_date", "amount_mode", "tariff"),
        )
    elif verified_value(tariff):
        results["tariff"] = issue(tariff, "tariff_lookup_unresolved")
    multiplier = results["tariff_multiplier"]
    if rate is None or not verified_value(multiplier):
        if verified_value(amount) or can_use_absence(amount):
            results["recharge_amount"] = issue(amount, "tariff_amount_pending")
        return
    expected = tariff_amount(rate.amount, multiplier.value)
    if can_use_absence(amount):
        results["recharge_amount"] = accept(
            amount, expected, ValueSource.LOOKUP, rule="6.2",
            inputs=("start_date", "amount_mode", "tariff", "tariff_multiplier"),
            evidence=(*tariff.evidence, *multiplier.evidence),
        )
    elif verified_value(amount) and amount.value != expected:
        results["recharge_amount"] = issue(amount, "tariff_amount_mismatch")


def apply_benefit_rules(
    results: dict[str, FieldResult], catalog: BenefitCatalog, today: date | None,
) -> tuple[list[str], set[str]]:
    hints = []
    benefit = results["benefit_type"]
    downgrade = results["cancel_on_downgrade"]
    kind = benefit.value if verified_value(benefit) else None
    if kind in ("升级权益", "特价优惠"):
        for name, value in (
            ("cancel_on_downgrade", "是"), ("after_offer", "恢复原包"), ("forbid_package_change", "是"),
        ):
            results[name] = default(results[name], value)
            if results[name].source is ValueSource.DEFAULT:
                hints.append(DEFAULT_HINTS[name])
    elif kind == "赠送时长" and can_use_absence(downgrade):
        results["cancel_on_downgrade"] = issue(downgrade, "downgrade_confirmation")
        hints.append(_DOWNGRADE_HINT)

    amount_mode = _checked_value(results, "amount_mode")
    after_offer = _checked_value(results, "after_offer")
    inactive = _inactive_fields(kind, amount_mode, after_offer)
    required = _required_fields(kind, amount_mode, after_offer)
    if kind == "特价优惠" and amount_mode == "节目包资费":
        _resolve_tariff(results, catalog, today)
    if kind in ("升级权益", "特价优惠") and after_offer == "指定节目包":
        target = results["after_offer_package"]
        if verified_value(target):
            results["after_offer_package"] = (
                accept(target, target.value, ValueSource.LOOKUP, inputs=("after_offer", "after_offer_package"))
                if catalog.package_names.count(target.value) == 1
                else issue(target, "after_offer_package_lookup_unresolved")
            )

    for name in inactive:
        results[name] = replace(results[name], value=None, has_value=False, source=None)
    for name in OFFER_FIELD_LABELS.keys() - required - inactive:
        if can_use_absence(results[name]):
            results[name] = replace(results[name], needs_confirmation=False)
    for name in _DURATION_FIELDS:
        result = results[name]
        if verified_value(result) and result.statement.value is not None and result.statement.value.unit == "月":
            hints.append(
                _MONTH_HINT if name in ("recharge_days", "gift_days")
                else f"{OFFER_FIELD_LABELS[name]}按 1 月 = 30 天换算"
            )
    return list(dict.fromkeys(hints)), inactive
