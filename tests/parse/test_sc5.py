import json
from pathlib import Path

import pytest

from qianshu.draft import PackageRow
from qianshu.parse import parse_application
from qianshu.parse.scope import _preamble_policy_field

MONTH_HINT = "充值 / 赠送天数按 1 月 = 30 天换算"
FIELD_RULE_HINTS = {
    "是否只触发一次按默认填否", "多买多送按默认填是", "生效倍数按默认填 1",
    "审批件按默认留空", "政策类型按用户标签判定",
}
COMPANIES_FILE = Path(__file__).parent / "fixtures" / "companies.tsv"
INTERVAL = "2099.2.1-2099.3.31"


def field(value, *evidence):
    return {"value": value, "evidence": list(evidence)}


def dates(evidence):
    return {
        "start_date": field("2099-02-01", evidence),
        "end_date": field("2099-03-31", evidence),
    }


def parse(body, *reply, companies_file=COMPANIES_FILE, **kwargs):
    return parse_application(body, model=lambda **_: json.dumps(reply), companies_file=companies_file, **kwargs)


def assert_uncertain(draft, *names):
    for name in names:
        result = getattr(draft, name)
        assert result.value is None
        assert result.uncertainty is not None
        assert result.uncertainty.candidates == []


def assert_dates(draft):
    assert draft.start_date.value == "2099-02-01"
    assert draft.end_date.value == "2099-03-31"
    assert draft.start_date.uncertainty is draft.end_date.uncertainty is None


@pytest.mark.parametrize("first", [
    "一、占位包A用户\n赠送15天",
    "- 占位包A用户\n赠送15天",
    "占位包A用户：赠送15天",
])
@pytest.mark.parametrize("count", [1, 2])
def test_missed_first_heading_cannot_lend_its_gift_days(first, count):
    second = "2.占位包B用户"
    title = "赞比亚占位申请"
    body = f"{title}\n申请时间周期：\n{INTERVAL}\n{first}\n{second}\n赠送30天"
    reply = {
        "company": field(None, title),
        "policy_name": field(None, second),
        "gift_days": field(15, "赠送15天"),
        **dates(INTERVAL),
    }
    rows = ([{"company": field(None, title), "gift_days": field(15, "赠送15天")}] if count == 2 else [])
    drafts = parse(body, *rows, reply)
    for draft in drafts:
        assert_uncertain(draft, "gift_days")
        if count == 1:
            assert_dates(draft)
        else:
            assert_uncertain(draft, "start_date", "end_date")


@pytest.mark.parametrize(("line", "name", "value", "quote"), [
    ("用户标签：占位标签甲", "user_tags", ["占位标签甲"], "占位标签甲"),
    ("当前节目包：占位包甲", "packages", [{"current_package": "占位包甲", "gift_package": "占位包甲_copy"}], "占位包甲"),
    ("充值两个月", "recharge_days", 60, "充值两个月"),
])
def test_policy_field_in_preamble_allows_dates_but_cannot_supply_its_value(line, name, value, quote):
    heading = "1.占位包用户（赞比亚）"
    body = f"赞比亚占位申请\n申请周期：{INTERVAL}\n{line}\n{heading}\n赠送30天"
    draft, = parse(body, {
        "policy_name": field(None, heading), name: field(value, quote), **dates(INTERVAL),
    })
    assert_uncertain(draft, name)
    assert_dates(draft)


@pytest.mark.parametrize(("line", "is_field", "dates_certain"), [
    ("拟给占位青竹包用户外呼(已休眠满8个月),联络计划由占位小组另行排定：", False, True),
    ("占位回访将在周四启动(已休眠满7个月),对象为占位银叶包的待访用户：", False, True),
    ("占位说明，Nova包用户：", False, True),
    ("占位说明。Nova包用户：", False, True),
    ("占位说明(Nova)包用户：", False, True),
    ("占位甲乙丙丁戊己庚辛壬癸子Nova包用户：", False, True),
    ("占位安排另行补充，1个月后联系；占位资源另行赠送（两个月后登记）。", False, True),
    ("1.占位Nova包用户：", True, False),
    ("- 1.占位甲乙丙丁戊己庚辛壬癸 Nova.2_-+包用户：", True, False),
    ("占位Nova包用户：", True, True),
    ("充1个月送1个月", True, True),
])
def test_preamble_dates_require_a_certain_first_heading(line, is_field, dates_certain):
    assert _preamble_policy_field(line) is is_field
    heading = "2.占位包用户（赞比亚）"
    body = f"赞比亚占位申请\n\n{line}\n申请周期：{INTERVAL}\n{heading}\n赠送30天"
    draft, = parse(body, {"policy_name": field(None, heading), **dates(INTERVAL)})
    if dates_certain:
        assert_dates(draft)
    else:
        assert_uncertain(draft, "start_date", "end_date")


def test_non_date_preamble_evidence_does_not_affect_local_fields():
    heading = "1.占位包用户（赞比亚）"
    body = f"赞比亚占位申请\n定向政策。\n申请周期：{INTERVAL}\n{heading}\n赠送30天"
    draft, = parse(body, {
        "policy_name": field(None, heading), "policy_type": field("定向政策", "定向政策"),
        "gift_days": field(30, "赠送30天"), **dates(INTERVAL),
    })
    assert_uncertain(draft, "policy_type")
    assert draft.gift_days.value == 30
    assert_dates(draft)


@pytest.mark.parametrize(("preamble", "start", "end", "matching"), [
    (f"赞比亚专用安排\n申请时间周期：\n{INTERVAL}", INTERVAL, INTERVAL, True),
    (f"赞比亚专用安排\n一、申请时间周期：\n{INTERVAL}", INTERVAL, INTERVAL, True),
    (f"一、赞比亚专用安排：\n\n申请时间周期：{INTERVAL}", INTERVAL, INTERVAL, True),
    (f"赞比亚：申请时间周期：{INTERVAL}", INTERVAL, INTERVAL, True),
    (f"赞比亚申请时间周期：\n{INTERVAL}", INTERVAL, INTERVAL, True),
    ("仅限赞比亚：申请开始日期 2099-02-01，申请结束日期 2099-03-31",
     "申请开始日期 2099-02-01", "申请结束日期 2099-03-31", True),
    ("申请开始日期 2099-02-01（仅赞比亚），申请结束日期 2099-03-31",
     "申请开始日期 2099-02-01", "申请结束日期 2099-03-31", True),
    (f"某公司专用安排\n申请时间周期：{INTERVAL}", INTERVAL, INTERVAL, False),
])
def test_country_qualified_preamble_is_checked_against_policy(preamble, start, end, matching):
    headings = ["1.占位甲包用户（赞比亚）", "2.占位乙包用户（肯尼亚）"]
    body = f"赞比亚和肯尼亚占位申请\n{preamble}\n{headings[0]}\n赠送30天\n{headings[1]}\n赠送15天"
    zambia, kenya = parse(body, *[
        {"policy_name": field(None, title),
         "start_date": field("2099-02-01", start), "end_date": field("2099-03-31", end)}
        for title in headings
    ])
    assert_uncertain(kenya, "start_date", "end_date")
    if matching:
        assert_dates(zambia)
    else:
        assert_uncertain(zambia, "start_date", "end_date")


def test_company_qualified_preamble_requires_a_matching_company_in_the_body(tmp_path):
    headings = ["1.占位甲包用户", "2.占位乙包用户", "3.占位丙包用户"]
    body = (
        f"赞比亚占位申请\nPlaceholder Co A专用安排：\n申请时间周期：{INTERVAL}\n"
        f"{headings[0]}\n公司为 Placeholder Co A\n{headings[1]}\n公司为 Placeholder Co B\n{headings[2]}\n赠送30天"
    )
    table = tmp_path / "table.tsv"
    table.write_text("赞比亚\tPlaceholder Co A\tPlaceholder Co A\n赞比亚\tPlaceholder Co B\tPlaceholder Co B\n", encoding="utf-8")
    matching, other, unknown = parse(body, *[
        {"policy_name": field(None, title), "company": field(name, f"公司为 {name}"), **dates(INTERVAL)}
        for title, name in zip(headings, ("Placeholder Co A", "Placeholder Co B", "Placeholder Co A"), strict=True)
    ], companies_file=table)
    assert_dates(matching)
    assert matching.company.value == "Placeholder Co A"
    assert other.company.value == "Placeholder Co B"
    assert_uncertain(unknown, "company")
    assert_uncertain(other, "start_date", "end_date")
    assert_uncertain(unknown, "start_date", "end_date")


@pytest.mark.parametrize("label", [
    "已作废的申请时间周期", "废止申请周期", "停用政策时段", "取消的活动时段", "撤销的活动期间",
    "仅供参考，申请时间周期", "活动参考时段", "原定政策期间", "旧申请期间", "暂定的申请周期", "待定申请周期",
    "罚停政策时段", "停机政策时段", "仅供参考：\n申请时间周期",
])
@pytest.mark.parametrize("titled", [False, True])
def test_inactive_periods_are_rejected_with_and_without_title(label, titled):
    heading = "1.占位甲包用户"
    body = f"{label}：\n{INTERVAL}"
    if titled:
        body = f"赞比亚占位申请\n{body}\n{heading}\n赠送30天"
    draft, = parse(body, {"policy_name": field(None, heading), **dates(INTERVAL)})
    assert_uncertain(draft, "start_date", "end_date")


@pytest.mark.parametrize(("label", "valid"), [
    ("申请时间周期", False),
    ("罚停政策时段", False),
    ("停机政策时段", False),
    ("停用政策时段", False),
    ("以下各条均适用：申请时间周期", True),
    ("以下各条均适用的申请时间周期", True),
    ("本批统一：\n申请时间周期", True),
])
def test_policy_local_period_is_rejected_and_batch_shared_period_is_accepted(label, valid):
    heading = "1.占位甲包用户（赞比亚）"
    body = f"赞比亚占位申请\n{heading}\n赠送30天\n{label}：{INTERVAL}"
    draft, = parse(body, {"policy_name": field(None, heading), **dates(INTERVAL)})
    if valid:
        assert_dates(draft)
    else:
        assert_uncertain(draft, "start_date", "end_date")


def test_batch_shared_period_may_continue_on_the_next_line():
    heading = "1.占位甲包用户（赞比亚）"
    body = f"赞比亚占位申请\n{heading}\n赠送30天\n本批统一申请时间周期：\n{INTERVAL}"
    draft, = parse(body, {"policy_name": field(None, heading), **dates(INTERVAL)})
    assert_dates(draft)


@pytest.mark.parametrize(("amount", "months"), [
    ("1", 1), ("13", 13), ("一", 1), ("二", 2), ("两", 2), ("三", 3), ("四", 4), ("五", 5),
    ("六", 6), ("七", 7), ("八", 8), ("九", 9), ("十", 10), ("十一", 11), ("十二", 12),
])
def test_month_amounts_convert_and_add_one_hint(amount, months):
    body = f"充值 {amount} 个月，赠 {amount} 月"
    draft, = parse(body, {
        "recharge_days": field(months * 30, body), "gift_days": field(months * 30, body),
    })
    assert draft.recharge_days.value == draft.gift_days.value == months * 30
    assert draft.hints.count(MONTH_HINT) == 1


@pytest.mark.parametrize(("body", "evidence", "value"), [
    ("不充值两个月", "充值两个月", 60), ("无需赠送两个月", "赠送两个月", 60),
    ("充值约两个月", "充值约两个月", 60), ("赠送12个月", "赠送12个月", 60),
    ("赠送连续两个月", "赠送连续两个月", 60), ("赠送两个月", "两个月", 60),
    ("赠送2月份政策", "赠送2月份", 60), ("充2\n个月", "充2\n个月", 60),
])
def test_months_require_matching_adjacent_action_number_and_unit(body, evidence, value):
    draft, = parse(body, {name: field(value, evidence) for name in ("recharge_days", "gift_days")})
    assert_uncertain(draft, "recharge_days", "gift_days")
    assert MONTH_HINT not in draft.hints


@pytest.mark.parametrize(("current", "gift", "extra", "body_tail", "valid"), [
    ("占位 A", "占位 A_copy", [], "", True),
    ("占位 A", "占位 A_copy", [], "\n赠送包：占位 A_copy", True),
    ("占位 A", "占位 A_copy", [], "\n赠送包：占位乙_copy", False),
    ("占位 A", "占位 A_copy", [], "\n无赠送包", False),
    ("占位 A", "占位 A_copy", [{"current_package": "占位乙", "gift_package": "占位乙_copy"}], "", False),
    ("占位乙", "占位乙_copy", [], "", False),
    ("占位", "占位_copy", [], "", False),
    ("占位 A", "占位乙_copy", [], "", False),
    ("占位 A", None, [], "", False),
])
def test_packages_match_title_and_derived_gift_package(current, gift, extra, body_tail, valid):
    heading = "1. 占位 A 包用户（赞比亚）"
    detail = "赠送一个月，允许充其他包，充占位乙包亦享"
    body = f"赞比亚占位申请\n{heading}\n{detail}{body_tail}"
    draft, = parse(body, {
        "packages": field([{"current_package": current, "gift_package": gift}, *extra], heading, detail),
    })
    if valid:
        assert draft.packages.value == [PackageRow(current_package=current, gift_package=gift)]
    else:
        assert_uncertain(draft, "packages")


@pytest.mark.parametrize("local_days", [True, False])
@pytest.mark.parametrize("other_keyword", ["", "赞比亚\t占位公司乙\tPLACEHOLDER\n"])
def test_six_numbered_policies_share_dates_and_keep_their_own_packages(local_days, other_keyword, tmp_path):
    title = "赞比亚占位外呼安排如下："
    preamble = "占位前言：换包不限，拟充2个月后赠送2个月。"
    lines = [
        title, "占位批次说明。", preamble, "一、占位批次申请时间周期：", INTERVAL, "",
        "二、占位政策清单：", "待设占位标签如下：",
    ]
    detail = (
        "充2个月赠送2个月，允许多买多赠，换包不限，占位云朵包也参加"
        if local_days else "赠送当前包的附属包"
    )
    day_evidence = detail if local_days else preamble
    replies = []
    names = [f"占位{index}" for index in range(1, 7)]
    for index, name in enumerate(names, 1):
        heading = f"{index}.{name}包用户"
        lines.extend([heading, f"用户标签：占位标签{index}", detail])
        replies.append({
            "company": field(None, title),
            "recharge_days": field(60, day_evidence),
            "gift_days": field(60, day_evidence),
            "packages": field([{"current_package": name, "gift_package": f"{name}_copy"}], heading, detail),
            **dates(INTERVAL),
        })
    lines.extend(["三、备注", "1.本次占位用户活动结束后停在原包。", "2.请占位人员协助。"])
    table = tmp_path / "companies.tsv"
    table.write_text("赞比亚\t占位公司甲\n" + other_keyword, encoding="utf-8")
    drafts = parse("\n".join(lines), *replies, companies_file=table)
    for draft, name in zip(drafts, names, strict=True):
        assert_dates(draft)
        assert draft.packages.value == [PackageRow(current_package=name, gift_package=f"{name}_copy")]
        assert "packages" not in draft.uncertain_fields()
        if local_days:
            assert draft.recharge_days.value == draft.gift_days.value == 60
            assert {"recharge_days", "gift_days"}.isdisjoint(draft.uncertain_fields())
            assert [hint for hint in draft.hints if hint not in FIELD_RULE_HINTS] == ["互斥组按默认填是", MONTH_HINT]
        else:
            assert_uncertain(draft, "recharge_days", "gift_days")
            assert [hint for hint in draft.hints if hint not in FIELD_RULE_HINTS] == ["互斥组按默认填是"]
        assert draft.company.value == "占位公司甲"
        assert draft.company.uncertainty is None
        assert [item.text for item in draft.company.evidence] == [title]


@pytest.mark.parametrize("value", [None, "赞比亚", "Placeholder Unmapped Co", "占位虚构公司"])
def test_country_only_application_uses_default_despite_model_names(value):
    reply = field(value, "赞比亚")
    reply["candidates"] = [field("占位虚构公司", "赞比亚"), field("占位模型候选公司", "赞比亚")]
    draft, = parse("赞比亚占位申请", {"company": reply})
    assert draft.company.value == "占位公司甲"
    assert draft.company.uncertainty is None


def test_non_keyword_company_mentions_do_not_override_default():
    body = "赞比亚占位申请，适用公司为占位公司甲或占位公司乙。"
    draft, = parse(body, {"company": field("占位公司乙", "占位公司乙")})
    assert draft.company.value == "占位公司甲"
    assert draft.company.uncertainty is None


@pytest.mark.parametrize(("country", "body", "expected"), [
    ("赞比亚", "赞比亚占位申请", "占位公司甲"),
    ("赞比亚", "赞比亚占位申请，Placeholder Unmapped Co", "占位公司甲"),
    ("莫桑比克", "莫桑比克TMT占位申请", "占位 TMT 公司"),
    ("莫桑比克", "莫桑比克占位申请\nTMT", "占位 TMT 公司"),
    ("莫桑比克", "莫桑比克占位申请，tMt\nTMT", "占位 TMT 公司"),
    ("莫桑比克", "莫桑比克占位申请", "占位莫桑比克默认公司"),
    ("莫桑比克", "莫桑比克TMTX占位申请，1TMT、TMT2、XTMT", "占位莫桑比克默认公司"),
    ("莫桑比克", "莫桑比克占位申请，STNB", "占位莫桑比克默认公司"),
    ("布隆迪", "布隆迪STNB占位申请", "占位 STNB 公司"),
    ("布隆迪", "布隆迪占位申请\nstnb", "占位 STNB 公司"),
    ("布隆迪", "布隆迪占位申请", "占位布隆迪默认公司"),
    ("布隆迪", "布隆迪占位申请，STNBX", "占位布隆迪默认公司"),
    ("加纳", "加纳占位申请，PLACEHOLDER_GH", "占位加纳公司"),
    ("马拉维", "马拉维占位申请，MWC", "占位马拉维公司"),
])
def test_company_keyword_or_country_default(country, body, expected):
    draft, = parse(body, {"company": field("占位模型公司", country)})
    assert draft.company.value == expected
    assert draft.company.uncertainty is None
    assert draft.company.evidence
    assert all(body.startswith(quote.text, quote.start) for quote in draft.company.evidence)


@pytest.mark.parametrize("body", ["TMT", "马拉维占位申请"])
def test_country_and_company_rule_are_required(body):
    draft, = parse(body, {"company": field("占位模型公司", body)})
    assert_uncertain(draft, "company")


def test_multiple_keywords_yield_only_matched_company_candidates():
    body = "莫桑比克占位申请，TMT 与 MOZB"
    draft, = parse(body, {"company": field("占位 TMT 公司", "莫桑比克")})
    assert draft.company.value is None
    assert [candidate.value for candidate in draft.company.uncertainty.candidates] == [
        "占位 TMT 公司", "占位莫桑比克乙公司",
    ]


@pytest.mark.parametrize("subject", ["适用公司为占位主体甲", "主体：占位主体甲", "适用公司为TMT或占位主体甲"])
def test_keyword_conflicting_with_explicit_subject_is_uncertain(subject):
    body = f"莫桑比克TMT占位申请，{subject}。"
    draft, = parse(body, {"company": field("占位 TMT 公司", "莫桑比克")})
    assert_uncertain(draft, "company")
    assert draft.company.uncertainty.reason == "公司关键词与原文明确主体冲突。"


@pytest.mark.parametrize("clause", ["适用主体为占位 TMT 公司", "请告知占位公司甲"])
def test_compatible_or_incidental_company_mention_keeps_keyword_match(clause):
    body = f"莫桑比克TMT占位申请，{clause}。"
    draft, = parse(body, {"company": field(None, "莫桑比克")})
    assert draft.company.value == "占位 TMT 公司"


@pytest.mark.parametrize(("preamble", "expected"), [
    ("TMT 仅作占位说明。", ["占位莫桑比克乙公司", "占位莫桑比克默认公司"]),
    ("本批均适用 TMT。", [None, "占位 TMT 公司"]),
    ("本批共同适用主体为占位主体甲。", [None, "占位莫桑比克默认公司"]),
    ("以下各条均为占位公司甲的定向政策。", [None, "占位莫桑比克默认公司"]),
])
def test_company_keywords_stay_within_policy_or_explicit_batch(preamble, expected):
    title = "莫桑比克占位申请"
    headings = ["1.占位甲包用户", "2.占位乙包用户"]
    body = f"{title}\n{preamble}\n{headings[0]}\nMOZB\n赠送30天\n{headings[1]}\n赠送15天"
    drafts = parse(body, *[
        {"policy_name": field(None, heading), "company": field("占位模型公司", title)}
        for heading in headings
    ])
    assert [draft.company.value for draft in drafts] == expected
    if preamble.startswith("本批均"):
        assert preamble in [item.text for item in drafts[1].company.evidence]


@pytest.mark.parametrize("data", [
    None, "unreadable", b"\xff", b"", "赞比亚\t".encode(), "占位国\t占位公司".encode(),
    "赞比亚\t占位公司甲\tKEY\tEXTRA".encode(), "赞比亚".encode(), "赞比亚\t占位公司甲\t".encode(),
    "赞比亚\t占位公司甲\n\t".encode(),
    "赞比亚\t占位公司甲\n赞比亚\t占位公司乙".encode(),
    "赞比亚\t占位公司甲\tKEY\n赞比亚\t占位公司乙\tkey".encode(),
])
def test_missing_or_invalid_company_rules_leave_only_company_uncertain(tmp_path, data):
    table = tmp_path / "companies.tsv"
    if isinstance(data, bytes):
        table.write_bytes(data)
    elif data == "unreadable":
        table.mkdir()
    draft, = parse("赞比亚占位申请，赠送30天", {
        "company": field("Placeholder Unmapped Co", "赞比亚"), "gift_days": field(30, "赠送30天"),
    }, companies_file=table)
    assert_uncertain(draft, "company")
    assert draft.gift_days.value == 30
    assert "公司对照表缺失或无效" in draft.to_dict()["hints"]


def test_single_company_rule_uses_country_as_original_evidence(tmp_path):
    table = tmp_path / "companies.tsv"
    table.write_text("肯尼亚\t占位公司甲\n", encoding="utf-8")
    for name in (None, "占位公司甲", "Placeholder Unmapped Co", "占位虚构公司"):
        draft, = parse("肯尼亚占位申请", {"company": field(name, "肯尼亚")}, companies_file=table)
        assert draft.company.value == "占位公司甲"
        assert draft.company.uncertainty is None
        assert [item.text for item in draft.company.evidence] == ["肯尼亚占位申请"]
    draft, = parse("占位公司甲", {"company": field("占位公司甲", "占位公司甲")}, companies_file=table)
    assert_uncertain(draft, "company")


def test_prompt_and_rules_allow_preamble_dates_alongside_non_date_clauses():
    expected = "前言中的充值、赠送、用户标签或节目包等内容不作为任何字段的依据。"

    def model(*, system, user):
        assert system.count(expected) == 2
        assert "前言日期也保留不确定" not in system
        assert "前言不得为日期提供依据" not in system
        assert "公司由程序按国家查表决定" in system
        return "[]"

    assert parse_application("占位申请", model=model, companies_file=COMPANIES_FILE) == []


@pytest.mark.parametrize("placement", ["before", "after"])
def test_shared_non_date_evidence_is_limited_to_after_the_first_title(placement):
    title = "1.Nova包用户（赞比亚）"
    shared = "本批统一：赠送两个月"
    body = "\n".join([shared, title] if placement == "before" else [title, shared])
    reply = {"policy_name": field(None, title), "gift_days": field(60, "赠送两个月")}

    draft, = parse(body, reply)
    if placement == "before":
        assert_uncertain(draft, "gift_days")
        assert draft.gift_days.evidence == []
        assert MONTH_HINT not in draft.hints
    else:
        assert draft.gift_days.value == 60
        assert [item.text for item in draft.gift_days.evidence] == ["赠送两个月"]
        assert MONTH_HINT in draft.hints

    reply["gift_days"] = {**field(None), "candidates": [field(60, "赠送两个月")]}
    draft, = parse(body, reply)
    assert draft.gift_days.value is None
    assert MONTH_HINT not in draft.hints
    if placement == "before":
        assert draft.gift_days.evidence == draft.gift_days.uncertainty.candidates == []
    else:
        candidate, = draft.gift_days.uncertainty.candidates
        assert candidate.value == 60
        assert candidate.evidence == draft.gift_days.evidence


@pytest.mark.parametrize("section_titles", [
    ("【赞比亚】", "【肯尼亚】"),
    ("赞比亚：", "肯尼亚："),
    ("■赞比亚", "■肯尼亚"),
    ("一、赞比亚", "二、肯尼亚"),
])
@pytest.mark.parametrize("swapped", [False, True])
def test_standalone_country_sections_do_not_share_cycles(section_titles, swapped):
    zambia_title, kenya_title = section_titles
    kenya_period = "2099.4.1-2099.5.31"
    zambia_policy = "1.Nova包用户（赞比亚）"
    kenya_policy = "2.Basic包用户（肯尼亚）"
    body = (
        f"占位批次\n{zambia_title}\n占位分节说明。\n申请时间周期：\n{INTERVAL}\n"
        f"{kenya_title}\n申请时间周期：{kenya_period}\n"
        f"{zambia_policy}\n充一个月赠送一个月\n{kenya_policy}\n充值30天送30天"
    )
    zambia_dates = dates(INTERVAL)
    kenya_dates = {"start_date": field("2099-04-01", kenya_period), "end_date": field("2099-05-31", kenya_period)}
    if swapped:
        zambia_dates, kenya_dates = kenya_dates, zambia_dates
    zambia, kenya = parse(
        body, {"policy_name": field(None, zambia_policy), **zambia_dates},
        {"policy_name": field(None, kenya_policy), **kenya_dates},
    )
    if swapped:
        assert_uncertain(zambia, "start_date", "end_date")
        assert_uncertain(kenya, "start_date", "end_date")
    else:
        assert_dates(zambia)
        assert kenya.start_date.value == "2099-04-01"
        assert kenya.end_date.value == "2099-05-31"
        assert kenya.start_date.uncertainty is kenya.end_date.uncertainty is None


@pytest.mark.parametrize("section", ["【占位未知】", "■占位未知", "二、赞比亚和肯尼亚", "国家待定：", "占位未知："])
def test_unresolved_preamble_section_does_not_inherit_previous_country(section):
    title = "1.Nova包用户（赞比亚）"
    body = f"【赞比亚】\n申请周期：2099.1.1-2099.1.31\n{section}\n申请周期：{INTERVAL}\n{title}"
    draft, = parse(body, {"policy_name": field(None, title), **dates(INTERVAL)})
    assert_uncertain(draft, "start_date", "end_date")
    assert draft.start_date.evidence == draft.end_date.evidence == []


def test_country_sections_also_scope_individually_labeled_dates():
    title = "1.Nova包用户（赞比亚）"
    start, end = "申请开始日期 2099-02-01", "申请结束日期 2099-03-31"
    draft, = parse(f"赞比亚：\n{start}\n{end}\n{title}", {
        "policy_name": field(None, title),
        "start_date": field("2099-02-01", start), "end_date": field("2099-03-31", end),
    })
    assert_dates(draft)


@pytest.mark.parametrize(("text", "name", "quote", "amount"), [
    ("需补充7个月的罚停记录", "recharge_days", "充7个月", 210),
    ("补充7天假期", "recharge_days", "充7天", 7),
    ("需补充7天。", "recharge_days", "充7天", 7),
    ("赠送2个月饼礼盒", "gift_days", "赠送2个月", 60),
    ("赠送2月饼礼盒", "gift_days", "赠送2月", 60),
    ("赠送2天线套装", "gift_days", "赠送2天", 2),
    ("配赠送30天", "gift_days", "赠送30天", 30),
    ("发送30天", "gift_days", "送30天", 30),
    ("赠送30天线", "gift_days", "送30天", 30),
])
def test_duration_requires_whole_action_and_unit_even_when_model_trims_the_quote(text, name, quote, amount):
    draft, = parse(text, {name: {**field(amount, quote), "candidates": [field(amount, quote)]}})
    assert_uncertain(draft, name)
    assert MONTH_HINT not in draft.hints


@pytest.mark.parametrize(("body", "recharge", "gift", "month_hint"), [
    ("充一个月赠送一个月", "充一个月", "赠送一个月", True),
    ("充值30天送30天", "充值30天", "送30天", False),
    ("充值一个月送30天", "充值一个月", "送30天", True),
])
def test_combined_duration_actions_need_no_punctuation(body, recharge, gift, month_hint):
    draft, = parse(body, {"recharge_days": field(30, recharge), "gift_days": field(30, gift)})
    assert draft.recharge_days.value == draft.gift_days.value == 30
    assert (MONTH_HINT in draft.hints) is month_hint


@pytest.mark.parametrize(("title", "detail", "expected"), [
    ("1.Nova包和Basic包用户", "", ["Nova", "Basic"]),
    ("1.Nova包，Basic包用户", "", ["Nova", "Basic"]),
    ("1.Nova包用户", "当前节目包：Nova、Max", ["Nova", "Max"]),
    ("1.Nova包用户", "当前节目包：Max", ["Nova", "Max"]),
    ("1.Nova包用户", "当前节目包：Nova\n当前包为Max", ["Nova", "Max"]),
    ("1.Nova包用户", "当前节目包：Nova，当前包为Max", ["Nova", "Max"]),
])
def test_conflicting_current_packages_are_checked_against_the_whole_policy(title, detail, expected):
    body = f"赞比亚占位申请\n{title}\n{detail}"
    proposed = [{"current_package": "Nova", "gift_package": "Nova_copy"}]
    draft, = parse(body, {"packages": field(proposed, title)})
    assert draft.packages.value is None
    candidates = draft.packages.uncertainty.candidates
    assert [item.value[0].current_package for item in candidates] == expected
    for candidate in candidates:
        assert all(item.text in body for item in candidate.evidence)
        assert candidate.evidence


@pytest.mark.parametrize("detail", [
    "", "充其他包亦享", "当前节目包：Nova\n用户充其他包亦享",
    "当前节目包：Nova，用户充其他包亦享", "当前节目包：Nova，赠送包：Nova_copy",
])
def test_title_alone_supplies_one_current_and_gift_package(detail):
    title = "1.Nova包用户（赞比亚）"
    draft, = parse(f"{title}\n{detail}", {"policy_name": field(None, title)})
    assert draft.packages.value == [PackageRow(current_package="Nova", gift_package="Nova_copy")]
    assert draft.packages.uncertainty is None
    assert title.endswith(draft.packages.evidence[0].text)


def test_omitted_draft_cannot_supply_another_drafts_fields():
    title = "赞比亚占位申请"
    first, second, third = "1.Nova包用户", "2.Basic包用户", "3.Max包用户"
    body = f"{title}\n{first}\n赠送15天\n{second}\n用户标签：占位标签乙\n赠送30天\n{third}\n赠送45天"
    nova, max_package = parse(
        body,
        {"company": field(None, title), "policy_name": field(None, first), "gift_days": field(30, "赠送30天"),
         "user_tags": field(["占位标签乙"], "占位标签乙")},
        {"company": field(None, title), "policy_name": field(None, third), "gift_days": field(45, "赠送45天")},
    )
    assert_uncertain(nova, "gift_days", "user_tags")
    assert nova.gift_days.evidence == nova.user_tags.evidence == []
    assert max_package.gift_days.value == 45


def test_repository_company_example_is_not_a_mapping(monkeypatch, tmp_path):
    rules_dir = Path("rules").resolve()
    assert (rules_dir / "companies.txt").read_text(encoding="utf-8").splitlines()[-1] == "赞比亚\tZambia Corp."
    monkeypatch.chdir(tmp_path)
    draft, = parse_application(
        "赞比亚占位申请", model=lambda **_: '[{"company":{"evidence":["赞比亚"]}}]', rules_dir=rules_dir,
    )
    assert_uncertain(draft, "company")
    assert [hint for hint in draft.hints if hint not in FIELD_RULE_HINTS] == ["互斥组按默认填是", "公司对照表缺失或无效"]
