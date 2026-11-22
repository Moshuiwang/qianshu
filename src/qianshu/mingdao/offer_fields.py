"""生成离线核对用的优惠字段值。"""

from datetime import date

from qianshu.draft import PolicyDraft
from qianshu.parse.benefits import (
    OFFER_FIELD_LABELS, BenefitCatalog, benefit_applicability, load_benefit_catalog, tariff_amount,
)
from qianshu.validate import ValidationFacts, validate_draft

_FIELDS = (
    "benefit_type", "trigger_once", "cancel_on_downgrade", "recharge_days", "gift_days",
    "repeat_bonus", "effective_multiplier", *OFFER_FIELD_LABELS,
)


def assemble_offer_fields(
    draft: PolicyDraft, *, today: date, facts: ValidationFacts = ValidationFacts(),
    catalog: BenefitCatalog | None = None,
) -> dict[str, object]:
    """核验草稿并返回适用且有值的业务字段，供离线核对。"""
    draft, validation = validate_draft(draft, today=today, facts=facts)
    if not validation.can_create:
        raise ValueError("草稿存在缺失或待确认字段。")
    if catalog is None:
        catalog = load_benefit_catalog()
    inactive, _ = benefit_applicability(draft)
    values = {
        name: field.value for name in _FIELDS
        if name not in inactive and (field := getattr(draft, name, None)) is not None and field.value is not None
    }
    if values.get("after_offer") == "指定节目包":
        if catalog.package_names.count(values["after_offer_package"]) != 1:
            raise ValueError("到期目标包无法在受信表中唯一确认。")
    if values.get("amount_mode") == "节目包资费":
        rate = catalog.rate(values["tariff"], date.fromisoformat(draft.start_date.value))
        if rate is None or values["recharge_amount"] != tariff_amount(rate.amount, values["tariff_multiplier"]):
            raise ValueError("资费版本或充值金额无法确认。")
    return values
