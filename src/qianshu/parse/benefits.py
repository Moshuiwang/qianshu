"""优惠类型、时长格式与业务默认值。"""

from .evidence import default, issue, verified_value
from .protocol import Duration, FieldResult, StatementStatus, ValueSource

_TYPES = {"赠送时长", "升级权益", "特价优惠"}
_MONTH_HINT = "充值 / 赠送天数按 1 月 = 30 天换算"
_DOWNGRADE_HINT = "降包用户取消优惠未写，请在卡片确认具体取值"


def benefit_option(value: str, options: dict[str, str]) -> str | None:
    value = options.get(value, value)
    return value if value in _TYPES else None


def duration_days(value: Duration) -> int | None:
    if value.quantity < 0:
        return None
    return value.quantity * (30 if value.unit == "月" else 1)


def apply_benefit_rules(results: dict[str, FieldResult]) -> list[str]:
    hints = [
        _MONTH_HINT for name in ("recharge_days", "gift_days")
        if verified_value(results[name]) and results[name].statement.value.unit == "月"
    ]
    benefit = results["benefit_type"]
    downgrade = results["cancel_on_downgrade"]
    if verified_value(benefit):
        if benefit.value in ("升级权益", "特价优惠"):
            results["cancel_on_downgrade"] = default(downgrade, "是")
            if results["cancel_on_downgrade"].source is ValueSource.DEFAULT:
                hints.append("降包用户取消优惠按默认填是")
        elif downgrade.statement.status is StatementStatus.UNWRITTEN:
            results["cancel_on_downgrade"] = issue(downgrade, "downgrade_confirmation")
            hints.append(_DOWNGRADE_HINT)
    return list(dict.fromkeys(hints))
