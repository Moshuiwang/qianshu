"""解析值的严格比较。"""

from datetime import date
from decimal import Decimal, InvalidOperation

from qianshu.draft import DraftField, PackageRow

from .storage import RegressionError

FIELD_LABELS = {
    "policy_name": ("优惠政策名称", "Text"),
    "company": ("公司", "Relation"),
    "policy_type": ("政策类型", "SingleSelect"),
    "benefit_type": ("优惠类型", "SingleSelect"),
    "start_date": ("开始日期", "Date"),
    "end_date": ("结束日期", "Date"),
    "recharge_days": ("充值天数", "Number"),
    "gift_days": ("赠送天数", "Number"),
    "maintain_mutual_exclusion_group": ("是否需要维护互斥组", "Dropdown"),
    "trigger_once": ("是否只触发一次", "Dropdown"),
    "cancel_on_downgrade": ("降包用户取消优惠", "Dropdown"),
    "repeat_bonus": ("多买多送", "Dropdown"),
    "effective_multiplier": ("生效倍数", "Number"),
    "user_tags": ("用户标签", "Relation"),
    "packages": ("节目包", "SubTable"),
    "user_scope": ("用户范围", "Text"),
}
POLICY_TYPES = ("公开政策", "定向政策")
BENEFIT_TYPES = ("升级权益", "特价优惠", "赠送时长")
EXCLUSIONS = ("not_applicable", "gold_missing", "historical_rule", "insufficient_evidence", "source_anomaly")
STATUSES = ("match", "error", "uncertain", "missing", "anomaly")


def normalize(name: str, value: object) -> object:
    """验证字段类型，以与 JSON 兼容的值表示。字符串保留原样。"""
    kind = FIELD_LABELS[name][1]
    if name in ("company", "policy_name", "user_scope"):
        if isinstance(value, str) and value.strip():
            return value
    elif kind in ("Dropdown", "SingleSelect"):
        allowed = {"policy_type": POLICY_TYPES, "benefit_type": BENEFIT_TYPES}.get(name, ("是", "否"))
        if value in allowed:
            return value
    elif kind == "Date":
        if isinstance(value, str) and date.fromisoformat(value).isoformat() == value:
            return value
    elif name in ("recharge_days", "gift_days"):
        if type(value) is int:
            return value
    elif name == "effective_multiplier":
        if type(value) in (int, float, str, Decimal):
            try:
                number = Decimal(str(value))
            except InvalidOperation:
                pass
            else:
                if number.is_finite():
                    return str(number)
    elif name == "user_tags":
        if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
            if len(set(value)) == len(value):
                return sorted(value)
    elif name == "packages" and isinstance(value, list):
        rows = []
        for item in value:
            if isinstance(item, PackageRow):
                item = {"current_package": item.current_package, "gift_package": item.gift_package}
            if not isinstance(item, dict) or item.keys() != {"current_package", "gift_package"}:
                break
            current, gift = item["current_package"], item["gift_package"]
            if not isinstance(current, str) or not current.strip() or not (gift is None or isinstance(gift, str)):
                break
            rows.append((current, gift))
        else:
            if len(set(rows)) == len(rows):
                return [
                    {"current_package": current, "gift_package": gift}
                    for current, gift in sorted(rows, key=lambda row: (row[0], row[1] is not None, row[1] or ""))
                ]
    raise RegressionError(f"字段 {name} 的配置值缺失、格式无效或有重复值。")


def compare_field(name: str, expected: object, actual: DraftField) -> str:
    if actual.uncertainty is not None:
        return "uncertain"
    if actual.value is None:
        return "missing"
    if name == "effective_multiplier" and type(actual.value) not in (int, float):
        return "anomaly"
    try:
        value = normalize(name, actual.value)
    except ValueError:
        return "anomaly"
    if name == "effective_multiplier":
        return "match" if Decimal(expected) == Decimal(value) else "error"
    return "match" if expected == value else "error"
