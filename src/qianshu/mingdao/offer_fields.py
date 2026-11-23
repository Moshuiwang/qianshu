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


# 050 对照网页公式的业务字段全集。私有配置只提供控件绑定与已核实的公式参数。
WEB_TYPES = {
    "promotionName": 2, "mem": 2, "promotionStartDate": 15, "promotionEndDate": 15,
    "原优惠政策到期日期": 15, "companyId": 6, "singlePurchaseTime": 6, "cycleGivingTime": 6,
    "effectiveMinMultiple": 6, "标签与适用条件关系": 11, "文本": 2, "同期有效优惠政策": 34,
    "billingCycle": 6, "升包执行任务状态值": 2,
}
_WEB_COPY = {
    "promotionName": "policy_name", "mem": "user_scope", "promotionStartDate": "start_date",
    "promotionEndDate": "end_date", "原优惠政策到期日期": "end_date",
}


def web_value(name, rule, values, company_row, snapshot):
    """仅从已核的本条申请、网页定义和有版本的上游材料派生。"""
    def need(condition):
        if not condition:
            raise ValueError(f"网页字段 {name} 缺少已核实的适用来源")

    if name in _WEB_COPY:
        return values[_WEB_COPY[name]]
    kind = values["benefit_type"]
    if name == "companyId":
        number = company_row.get(rule["column"])
        need(type(number) in (int, str) and str(number).isascii() and str(number).isdecimal())
        return number
    if name == "singlePurchaseTime":
        if kind == "特价优惠" and values.get("recharge_days") is None:
            need(rule["empty_input"] in ("", None))
            return rule["empty_input"]
        return values["recharge_days"]
    if name == "cycleGivingTime":
        return values["watch_days" if kind == "特价优惠" else "gift_days"]
    if name == "effectiveMinMultiple":
        need(values["effective_multiplier"] == 1)
        return 1
    if name == "标签与适用条件关系":
        option = rule["default"]
        scenario = snapshot["condition_scenario"]
        need(isinstance(scenario, dict) and scenario.get("policy") == values["policy_name"]
             and scenario.get("condition") == option["value"] == "无适用条件"
             and isinstance(option["key"], str) and option["key"].strip())
        return [option["key"]]
    if name == "文本":
        need(isinstance(rule["text"], str) and rule["text"].strip())
        return rule["text"]
    if name == "billingCycle":
        cycles = rule["cycles"]
        need(cycles.get("日") == 1 and all(type(value) is int and value == int(unit == "日")
                                          for unit, value in cycles.items()))
        selected = {row["cycle"] for row in snapshot["tariffs"]}
        need(len(selected) == 1)
        return selected.pop()
    if name == "同期有效优惠政策":
        candidates = snapshot["concurrent_policies"]
        need(candidates["complete"] is True and isinstance(candidates["rows"], list))
        rows = candidates["rows"]
        need(len({row["id"] for row in rows}) == len(rows))
        return [{rule["child"]: [row["id"]]} for row in rows if row["company"] == values["company"]
                and date.fromisoformat(row["end"]) >= date.fromisoformat(values["start_date"])]
    if name == "升包执行任务状态值":
        if kind == "升级权益" and values["after_offer"] == "指定节目包":
            tasks = [row for row in snapshot["upgrade_tasks"] if row["id"] == snapshot["upgrade_task_id"] and row["package"] == values["after_offer_package"]
                     and row["company"] == values["company"]]
            need(len(tasks) == 1 and isinstance(status := tasks[0][rule["column"]], str) and status.strip())
            return status
        need(rule["inactive_value"] in (None, ""))
        return rule["inactive_value"]
    raise ValueError("网页字段未知")
