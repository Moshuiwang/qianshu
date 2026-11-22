import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from qianshu import parse
from qianshu.feishu import Message
from qianshu.feishu.entrance import Application
from qianshu.mingdao.create_order import _DRAFT_FIELDS, Stop, draft_values
from qianshu.mingdao.offer_fields import assemble_offer_fields
from qianshu.parse.benefits import BenefitCatalog, TariffRate
from qianshu.parse.protocol import StatementStatus, ValueSource
from qianshu.pipeline import Pipeline
from qianshu.regression.recording import _project_legacy_reply
from qianshu.validate import ValidationFacts, validate_draft
from tests.protocol_fixtures import fixed_reply

TODAY = date(2099, 1, 1)
TAG_END = date(2099, 3, 1)
FACTS = ValidationFacts(latest_tag_end_date=TAG_END)
COMPANIES = Path(__file__).parent / "fixtures" / "companies.tsv"
TARIFF = {"base_package": "占位甲包", "price_plan": "占位计划", "unit": "占位月资费", "version": None}
RATE = TariffRate(
    **{**TARIFF, "version": "占位版本"}, amount=20.35, valid_from=date(2099, 1, 1), valid_until=date(2099, 1, 19),
)
CATALOG = BenefitCatalog(tariffs=(RATE,), package_names=("占位丙包",))


def field(value, *evidence):
    return {"value": value, "evidence": evidence}


def scenario(kind, **offer_fields):
    terms = {
        "赠送时长": "充一个月送一个月，继续看占位甲包",
        "升级权益": "充一个月占位甲包，送七天占位乙包",
        "特价优惠": "支付四十个占位货币，可看两个月占位乙包",
    }[kind]
    if offer_fields.get("amount_mode", {}).get("value") == "节目包资费":
        terms = "按下述资费可看两个月占位乙包"
    end = "结束日期 2099-02-15"
    start = "开始日期 2099-01-10"
    url = "https://archive.example.invalid/placeholder"
    rows = {
        "country": field("赞比亚", "赞比亚"),
        "user_tags": field(["占位标签"], "占位标签"),
        "user_scope": field("占位用户", "适用占位用户"),
        "approval_url": field(url, url),
        "start_date": field("2099-01-10", start),
        "end_date": field("2099-02-15", end),
        "benefit_type": field(kind, terms),
        "packages": field([{"current_package": "占位甲包", "gift_package": "占位乙包" if kind != "赠送时长" else None}],
                          "① 占位甲包", terms),
    }
    if kind == "特价优惠":
        rows.update(amount_mode=field("自定义", terms), recharge_amount=field(40, terms),
                    watch_days=field({"quantity": 2, "unit": "月"}, terms))
    else:
        rows.update(recharge_days=field(30, terms), gift_days=field(30 if kind == "赠送时长" else 7, terms))
    rows.update(offer_fields)
    body = "\n".join(dict.fromkeys(text for row in rows.values() for text in row["evidence"]))
    return body, fixed_reply(body, [rows])


def parsed(body, reply, *, catalog=CATALOG, today=TODAY):
    draft, = parse.parse_application(body, model=lambda **_: json.dumps(reply), companies_file=COMPANIES,
                                     benefit_catalog=catalog, today=today)
    return draft


def card_text(body, reply):
    pipeline = Pipeline(
        model=lambda **_: json.dumps(reply), today=lambda: TODAY, tag_dates={"占位标签": TAG_END},
        companies_file=COMPANIES, benefit_catalog=CATALOG,
    )
    message = Message("占位消息", "占位群", "占位发送者", 0, "text", "")
    return "\n".join(
        element["text"]["content"] if element["tag"] == "div" else element["content"]
        for page in pipeline(Application(message, body, ())) for element in page.elements
    )


@pytest.mark.parametrize(("kind", "expected", "inapplicable"), [
    ("升级权益", {"recharge_days": 30, "gift_days": 7, "after_offer": "恢复原包"},
     {"amount_mode", "watch_days", "after_offer_package", "first_recharge_day_only"}),
    ("特价优惠", {"watch_days": 60, "amount_mode": "自定义", "recharge_amount": 40, "after_offer": "恢复原包"},
     {"recharge_days", "gift_days", "tariff", "tariff_multiplier", "after_offer_package"}),
])
def test_offer_applicability_validation_assembly_and_card(kind, expected, inapplicable):
    body, reply = scenario(
        kind, penalty_days=field({"quantity": 1, "unit": "月"}, "罚停一个月"),
        tariff_multiplier=field(5, "旧方案资费倍数五"),
    )
    draft = parsed(body, reply)
    _, validation = validate_draft(draft, today=TODAY, facts=FACTS)
    assert validation.can_create
    values = assemble_offer_fields(draft, today=TODAY, facts=FACTS, catalog=CATALOG)
    assert {key: values[key] for key in expected} == expected
    assert inapplicable.isdisjoint(values)
    assert all(getattr(draft, name).value is None for name in inapplicable)
    assert values["forbid_package_change"] == values["cancel_on_downgrade"] == "是"
    assert values["penalty_days"] == 30
    assert draft.packages.value[0].gift_package == "占位乙包_copy"
    content = card_text(body, reply)
    assert "解析完成，待群卡片同意" in content
    assert "优惠类型\n值　" + kind in content
    assert "自动调整\n优惠后是否升包按默认填恢复原包" in content
    assert "自动调整\n获得优惠后不允许换包按默认填是" in content
    assert "自动调整\n罚停天数按 1 月 = 30 天换算" in content
    if kind == "特价优惠":
        assert "充值金额\n值　40" in content
        assert "可看天数\n值　60" in content
        assert "充值天数\n值　" not in content
        assert "仅首充日享受" not in content
    else:
        assert "赠送天数\n值　7" in content
        assert "指定金额方式" not in content


def tariff_scenario(**overrides):
    return scenario("特价优惠", **{
        "amount_mode": field("节目包资费", "按占位甲包的占位计划月资费"),
        "tariff": field(TARIFF, "按占位甲包的占位计划月资费"),
        "tariff_multiplier": field(2, "取两倍资费"),
        "recharge_amount": field(None),
        "after_offer": field("指定节目包", "到期转入占位丙包"),
        "after_offer_package": field("占位丙包", "到期转入占位丙包"),
        "forbid_package_change": field("否", "优惠期间允许换包"),
        "first_recharge_day_only": field("是", "仅首次充值当天可享受"),
        **overrides,
    })


def test_tariff_lookup_explicit_end_target_and_decimal_floor(tmp_path, monkeypatch):
    body, reply = tariff_scenario()
    content = card_text(body, reply)
    assert "指定金额方式\n值　节目包资费" in content
    assert "资费倍数\n值　2" in content
    assert "优惠到期目标包\n值　占位丙包" in content
    assert "仅首充日享受\n值　是" in content
    private = tmp_path / ".private"
    private.mkdir()
    (private / "benefit-catalog.json").write_text(json.dumps({
        "tariffs": [{**TARIFF, "version": "占位版本", "amount": 20.35,
                     "valid_from": "2099-01-01", "valid_until": "2099-01-19"}],
        "package_names": ["占位丙包"],
    }), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    draft, = parse.parse_application(
        body, model=lambda **_: json.dumps(reply), companies_file=COMPANIES,
        rules_dir=COMPANIES.parents[3] / "rules", today=TODAY,
    )
    assert draft.field_results["recharge_amount"].statement.status is StatementStatus.UNWRITTEN
    assert draft.field_results["recharge_amount"].source is ValueSource.LOOKUP
    assert draft.tariff.value == {**TARIFF, "version": "占位版本"}
    values = assemble_offer_fields(draft, today=TODAY, facts=FACTS)
    assert values["recharge_amount"] == 40
    assert values["effective_multiplier"] == 1 and values["tariff_multiplier"] == 2
    assert values["after_offer_package"] == "占位丙包"
    assert values["after_offer"] == "指定节目包"
    assert values["forbid_package_change"] == "否"
    assert values["first_recharge_day_only"] == "是"


@pytest.mark.parametrize(("catalog", "today", "extra", "pending"), [
    (BenefitCatalog(package_names=("占位丙包",)), TODAY, {}, {"tariff", "recharge_amount"}),
    (replace(CATALOG, tariffs=(RATE, RATE)), TODAY, {}, {"tariff", "recharge_amount"}),
    (CATALOG, date(2099, 1, 20), {}, {"tariff", "recharge_amount"}),
    (CATALOG, TODAY, {"tariff": field({**TARIFF, "version": "占位过期版本"}, "按占位过期版本资费")},
     {"tariff", "recharge_amount"}),
    (CATALOG, TODAY, {"recharge_amount": field(41, "实付四十一")}, {"recharge_amount"}),
    (CATALOG, TODAY, {"recharge_amount": field(40, "实付四十"), "tariff_multiplier": field(None)},
     {"tariff_multiplier", "recharge_amount"}),
    (replace(CATALOG, package_names=("占位丙包", "占位丙包")), TODAY, {}, {"after_offer_package"}),
])
def test_tariff_and_end_target_need_unique_verified_inputs(catalog, today, extra, pending):
    body, reply = tariff_scenario(**extra)
    draft = parsed(body, reply, catalog=catalog, today=today)
    _, validation = validate_draft(draft, today=today, facts=FACTS)
    assert set(draft.uncertain_fields()) == pending
    assert {error.field for error in validation.errors} == pending
    with pytest.raises(ValueError, match="待确认"):
        assemble_offer_fields(draft, today=today, facts=FACTS, catalog=catalog)


@pytest.mark.parametrize("kind", ["赠送时长", "升级权益", "特价优惠"])
@pytest.mark.parametrize("explicit", [False, True])
def test_downgrade_uses_explicit_value_or_type_specific_absence(kind, explicit):
    body, reply = scenario(kind, **({"cancel_on_downgrade": field("否", "不限制升降包")} if explicit else {}))
    draft = parsed(body, reply)
    result = draft.field_results["cancel_on_downgrade"]
    content = card_text(body, reply)
    if explicit:
        assert draft.cancel_on_downgrade.value == "否"
        assert result.source is ValueSource.ORIGINAL
        assert "降包用户取消优惠按默认" not in content
    elif kind == "赠送时长":
        assert draft.cancel_on_downgrade.uncertainty is not None
        assert "自动调整\n降包用户取消优惠未写，请在卡片确认具体取值" in content
        assert "停在校验，需人工确认" in content
    else:
        assert draft.cancel_on_downgrade.value == "是"
        assert result.source is ValueSource.DEFAULT
        assert "自动调整\n降包用户取消优惠按默认填是" in content


@pytest.mark.parametrize("status", ["歧义", "冲突", "带条件", "缺证据", "原文未写"])
def test_defaults_require_verified_absence_for_each_field(status):
    body, reply = scenario("升级权益")
    names = ("cancel_on_downgrade", "after_offer", "forbid_package_change")
    for name in names:
        finding = reply["policies"][0]["fields"][name]
        finding["status"] = status
        if status == "原文未写":
            finding["value"] = "指定节目包" if name == "after_offer" else "否"
    draft = parsed(body, reply)
    assert set(draft.uncertain_fields()) == set(names)
    for name in names:
        result = draft.field_results[name]
        assert result.statement.status == status
        assert result.source is None and not result.has_value and result.needs_confirmation


@pytest.mark.parametrize(("kind", "text", "other"), [
    ("赠送时长", "充一个月送一个月，还是按一个月的钱看两个月？", "特价优惠"),
    ("特价优惠", "付一个月的钱看两个月\n或充值一个月再送一个月，方案待定", "赠送时长"),
])
def test_nearby_offer_readings_keep_model_ambiguity_and_candidates(kind, text, other):
    body, reply = scenario(kind, benefit_type=field(kind, text))
    model_field = reply["policies"][0]["fields"]["benefit_type"]
    evidence = model_field["evidence"]
    model_field.update(status="歧义", value=None, evidence=[],
                       candidates=[{"value": value, "evidence": evidence} for value in (kind, other)])
    draft = parsed(body, reply)
    assert draft.benefit_type.value is None
    assert [item.value for item in draft.benefit_type.uncertainty.candidates] == [kind, other]
    assert draft.cancel_on_downgrade.uncertainty is not None
    assert "需追问 · 优惠类型" in card_text(body, reply)


@pytest.mark.parametrize(("name", "value", "excerpt", "status"), [
    ("recharge_amount", 40, "按四十实付\n仅名单确认后生效", "带条件"),
    ("recharge_amount", 40, "四十是券面值，并非实付", "缺证据"),
    ("recharge_amount", 1, "实付一万五还是一千五，待定", "歧义"),
    ("watch_days", {"quantity": 30, "unit": "天"}, "可看三十至六十天", "歧义"),
    ("trigger_once", "是", "客服仅受理一次，优惠本身是否限次待确认", "歧义"),
])
def test_offer_variants_keep_model_status_and_do_not_fill_pending_values(name, value, excerpt, status):
    body, reply = scenario("特价优惠", **{name: field(value, excerpt)})
    finding = reply["policies"][0]["fields"][name]
    finding.update(status=status, value=None, candidates=[{"value": value, "evidence": finding["evidence"]}],
                   evidence=[])
    draft = parsed(body, reply)
    result = draft.field_results[name]
    assert result.statement.status == status and result.source is None
    assert not result.has_value and getattr(draft, name).uncertainty is not None
    _, validation = validate_draft(draft, today=TODAY, facts=FACTS)
    assert {error.field for error in validation.errors} == {name}


def test_model_can_report_chinese_magnitude_and_leave_customer_service_count_out_of_trigger():
    body, reply = scenario("特价优惠", recharge_amount=field(15000, "实付一万五千占位货币，客服只登记一次"))
    draft = parsed(body, reply)
    assert draft.recharge_amount.value == 15000
    assert draft.field_results["recharge_amount"].source is ValueSource.ORIGINAL
    assert draft.trigger_once.value == "否"
    assert draft.field_results["trigger_once"].source is ValueSource.DEFAULT


@pytest.mark.parametrize("kind", ["升级权益", "特价优惠"])
def test_legacy_reply_with_new_offer_type_requires_missing_fields(kind):
    body, reply = scenario(kind)
    fields = reply["policies"][0]["fields"]
    for name in set(fields) - _DRAFT_FIELDS - {"country", "company_keyword"}:
        del fields[name]
    with pytest.raises(parse.ParseError, match="结构"):
        parsed(body, reply)
    _project_legacy_reply(reply)
    draft = parsed(body, reply)
    _, validation = validate_draft(draft, today=TODAY, facts=FACTS)
    assert not validation.can_create
    for name in ("after_offer", "forbid_package_change"):
        assert draft.field_results[name].statement.status is StatementStatus.UNSUPPORTED
        assert getattr(draft, name).uncertainty is not None


def test_real_entry_field_whitelist_matches_main_and_rejects_offline_fields():
    assert _DRAFT_FIELDS == {
        "policy_name", "maintain_mutual_exclusion_group", "company", "policy_type", "start_date", "end_date",
        "benefit_type", "trigger_once", "cancel_on_downgrade", "recharge_days", "gift_days", "repeat_bonus",
        "effective_multiplier", "user_tags", "packages", "user_scope", "approval_url", "approval_attachments",
    }
    body, reply = scenario("特价优惠")
    draft = parsed(body, reply)
    with pytest.raises(Stop, match="草稿字段无效"):
        draft_values({"watch_days": {"value": draft.watch_days.value}})


@pytest.mark.parametrize("name", ["after_offer", "forbid_package_change", "cancel_on_downgrade"])
def test_invalid_certain_quote_prevents_default(name):
    value = "恢复原包" if name == "after_offer" else "是"
    body, reply = scenario("升级权益", **{name: field(value, "合成条款")})
    finding = reply["policies"][0]["fields"][name]
    finding["evidence"][0]["text"] = "缺失的合成引文"
    draft = parsed(body, reply)
    assert draft.field_results[name].source is None
    assert getattr(draft, name).uncertainty is not None
    with pytest.raises(ValueError, match="待确认"):
        assemble_offer_fields(draft, today=TODAY, facts=FACTS, catalog=CATALOG)


def test_offer_counts_by_verified_kind():
    from collections import Counter

    from qianshu.regression.business import grade

    counts = {}
    for kind in ("赠送时长", "升级权益", "特价优惠", "类型未知"):
        body, reply = scenario("特价优惠" if kind == "类型未知" else kind)
        if kind == "类型未知":
            reply["policies"][0]["fields"]["benefit_type"]["status"] = "歧义"
        draft = parsed(body, reply)
        group = draft.benefit_type.value if draft.benefit_type.uncertainty is None else "类型未知"
        mark = {"inferable": True, "auto_allowed": True}
        grades = Counter(grade(name, gold, mark, draft, body) for name, gold in (
            ("benefit_type", {"value": kind if kind != "类型未知" else "特价优惠"}),
            ("user_tags", {"value": ["另一个标签"]}),
            ("user_scope", {}),
        ))
        counts[group] = (1, sum(grades.values()) - grades["unknown"], grades["correct"],
                         grades["false_determined"], grades["overasked"], grades["unknown"])
    assert counts == {
        "赠送时长": (1, 2, 1, 1, 0, 1),
        "升级权益": (1, 2, 1, 1, 0, 1),
        "特价优惠": (1, 2, 1, 1, 0, 1),
        "类型未知": (1, 2, 0, 1, 1, 1),
    }


def test_ambiguous_tariff_multiplier_keeps_candidates_and_blocks_only_dependent_amount():
    body, reply = tariff_scenario(tariff_multiplier=field(2, "两倍或三倍资费"))
    finding = reply["policies"][0]["fields"]["tariff_multiplier"]
    finding.update(status="歧义", value=None, candidates=[
        {"value": value, "evidence": finding["evidence"]} for value in (2, 3)
    ], evidence=[])
    draft = parsed(body, reply)
    assert [(candidate.value, candidate.evidence[0].text)
            for candidate in draft.tariff_multiplier.uncertainty.candidates] == [
        (2, "两倍或三倍资费"), (3, "两倍或三倍资费"),
    ]
    assert draft.recharge_amount.uncertainty is not None
    assert draft.effective_multiplier.value == 1


def test_thin_reply_with_one_missing_required_field():
    body, reply = scenario("特价优惠")
    del reply["policies"][0]["fields"]["watch_days"]
    with pytest.raises(parse.ParseError) as error:
        parsed(body, reply)
    assert error.value.path[-1] == "watch_days"
    _project_legacy_reply(reply)
    draft = parsed(body, reply)
    assert draft.watch_days.uncertainty is not None
    assert draft.field_results["watch_days"].statement.reason_code == "field_not_reported"
    assert draft.recharge_amount.value == 40
