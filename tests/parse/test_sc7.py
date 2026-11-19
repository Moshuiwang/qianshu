import json
from pathlib import Path

import pytest

from qianshu.draft import PackageRow
from qianshu.parse import parse_application
from qianshu.parse.scope import clause_spans


COMPANIES_FILE = Path(__file__).parent / "fixtures" / "companies.tsv"
ONCE_HINT = "是否只触发一次按默认填否"
REPEAT_HINT = "多买多送按默认填是"
MULTIPLIER_HINT = "生效倍数按默认填 1"
ATTACHMENTS_HINT = "审批件按默认留空"
POLICY_TYPE_HINT = "政策类型按用户标签判定"


def field(value, *quotes):
    return {"value": value, "evidence": list(quotes)}


def parse(body, *reply):
    return parse_application(body, model=lambda **_: json.dumps(reply), companies_file=COMPANIES_FILE)


@pytest.mark.parametrize(("body", "expected"), [
    ("占位（甲,乙）,丙", ["占位（甲,乙）", "丙"]),
    ("占位（甲,(乙,丙）),丁", ["占位（甲,(乙,丙）)", "丁"]),
    ("占位）甲,（乙,丙", ["占位）甲", "（乙", "丙"]),
    ("占位（甲,（乙,丙）,丁", ["占位（甲", "（乙,丙）", "丁"]),
])
def test_only_paired_parentheses_protect_punctuation(body, expected):
    assert [body[start:end] for start, end in clause_spans(body, punctuation=",")] == expected


def test_unpaired_parenthesis_does_not_swallow_following_policy_clauses():
    terms = "占位青竹包试用条件： 充两个月赠送两个月,换包不限,允许多买多赠,占位银叶包也适用"
    headings = ["1.占位甲包用户（赞比亚）", "2.占位乙包用户（莫桑比克）"]
    body = "\n".join(
        f"{heading}\n3-4个月停机 （ （2099.4.6-2099.5.25）\n占位计数 {count} 户。\n{terms}"
        for count, heading in enumerate(headings, 8)
    )
    drafts = parse(body, *[
        {
            "company": field(None, heading),
            "recharge_days": field(60, "充两个月"),
            "gift_days": field(60, "赠送两个月"),
        }
        for heading in headings
    ])
    first, second = drafts
    for draft in (first, second):
        for result in (draft.recharge_days, draft.gift_days):
            assert result.value == 60
            assert result.uncertainty is None


@pytest.mark.parametrize(("tags", "name_line", "expected"), [
    (["占位标签甲"], "", "占位标签甲"),
    (["占位政策名称标签甲"], "", "占位政策名称标签甲"),
    (["占位标签甲"], "优惠政策名称：占位标签甲", "占位标签甲"),
    (["占位标签甲"], "优惠政策名称：占位名称乙", None),
    (["占位标签甲"], "优惠政策名称：占位标签甲；政策名为占位名称乙", None),
    (["占位标签甲", "占位标签乙"], "", None),
    ([], "", None),
    (None, "", None),
])
def test_policy_name_requires_one_confirmed_tag_without_a_conflicting_name(tags, name_line, expected):
    tag_line = "用户标签：" + "、".join(tags or []) if tags else "占位说明"
    draft, = parse(f"{tag_line}\n{name_line}", {
        "user_tags": field(tags, tag_line), "policy_name": field("占位模型猜测"),
    })
    assert draft.policy_name.value == expected
    if expected is None:
        assert draft.policy_name.uncertainty is not None
    else:
        assert draft.policy_name.evidence == draft.user_tags.evidence
        assert draft.policy_name.uncertainty is None


@pytest.mark.parametrize(("name", "statement", "expected", "shows_hint"), [
    ("trigger_once", "", "否", True),
    ("trigger_once", "是否只触发一次：否", "否", True),
    ("trigger_once", "仅能触发一次", None, False),
    ("trigger_once", "是否只触发一次：是", None, False),
    ("trigger_once", "触发次数：1", None, False),
    ("trigger_once", "只触发一次：否；仅触发一次", None, False),
    ("repeat_bonus", "", "是", True),
    ("repeat_bonus", "允许多买多赠", "是", False),
    ("repeat_bonus", "允许多买多送", "是", False),
    ("repeat_bonus", "本政策允许多买多赠", "是", False),
    ("repeat_bonus", "不允许多买多赠", "否", False),
    ("repeat_bonus", "不允许多买多送", "否", False),
    ("repeat_bonus", "本政策不允许多买多送", "否", False),
    ("repeat_bonus", "多买不多送", "否", False),
    ("repeat_bonus", "允许多买多赠；不允许多买多送", None, False),
    ("repeat_bonus", "允许多买多送？", None, False),
    ("repeat_bonus", "允许多买多赠，待确认", None, False),
    ("effective_multiplier", "", 1, True),
    ("effective_multiplier", "生效倍数：1", 1, False),
    ("effective_multiplier", "按1倍生效", 1, False),
    ("effective_multiplier", "按2倍生效", None, False),
    ("effective_multiplier", "生效倍数：1；生效倍数：2", None, False),
    ("effective_multiplier", "生效倍数待确定", None, False),
])
def test_g152_values_follow_policy_text_even_when_model_omits_the_statements(name, statement, expected, shows_hint):
    body = f"占位说明。\n{statement}"
    draft, = parse(body, {"user_scope": field("占位说明", "占位说明")})
    result = getattr(draft, name)
    assert result.value == expected
    assert (result.uncertainty is None) == (expected is not None)
    default_hint = {"trigger_once": ONCE_HINT, "repeat_bonus": REPEAT_HINT, "effective_multiplier": MULTIPLIER_HINT}[name]
    assert (default_hint in draft.hints) == shows_hint
    if statement:
        assert result.evidence
        assert all(body.startswith(quote.text, quote.start) for quote in result.evidence)
    else:
        assert result.evidence == []


@pytest.mark.parametrize("attachment", ["", "审批件附件：占位审批截图.png"])
def test_approval_attachments_default_to_confirmed_empty(attachment):
    draft, = parse(f"占位说明。\n{attachment}", {
        "approval_attachments": field(["占位审批截图.png"], attachment),
    })
    assert draft.approval_attachments.value == []
    assert draft.approval_attachments.uncertainty is None
    assert draft.approval_attachments.evidence == []
    assert ATTACHMENTS_HINT in draft.hints


@pytest.mark.parametrize(("tags", "statement", "expected"), [
    (["占位标签甲"], "用户标签：占位标签甲", "定向政策"),
    ([], "用户标签：无", "公开政策"),
    (None, "用户标签：待确认", None),
])
def test_policy_type_follows_confirmed_user_tags(tags, statement, expected):
    draft, = parse(statement, {"user_tags": field(tags, statement)})
    result = draft.policy_type
    assert result.value == expected
    assert (result.uncertainty is None) == (expected is not None)
    assert (POLICY_TYPE_HINT in draft.hints) == (expected is not None)


@pytest.mark.parametrize(("statement", "model_type"), [
    ("政策类型：公开", {}),
    ("申请类型：公开政策", {}),
    ("本次申请为公开政策", {}),
    ("本次申请为公开政策并面向占位用户", {}),
    ("本条政策为公开政策（占位范围说明）", {}),
    ("占位公司公开政策申请：", {}),
    ("政策类型：公开政策", {"policy_type": field("公开政策", "政策类型：公开政策")}),
    ("占位说明", {"policy_type": field("公开")}),
    ("占位说明", {"policy_type": {"candidates": [field("公开政策")]}}),
])
def test_policy_type_conflict_reports_the_tag_rule_and_claim(statement, model_type):
    tag_line = "用户标签：占位标签甲"
    draft, = parse(f"{tag_line}。\n{statement}", {
        "user_tags": field(["占位标签甲"], tag_line), **model_type,
    })
    assert draft.policy_type.value is None
    derived, conflicting = draft.policy_type.uncertainty.candidates
    assert derived.value == "定向政策"
    assert [quote.text for quote in derived.evidence] == [tag_line]
    assert conflicting.value == "公开政策"
    assert [quote.text for quote in conflicting.evidence] == (
        [statement] if statement != "占位说明" else []
    )
    assert POLICY_TYPE_HINT not in draft.hints


def test_unrelated_reference_url_is_left_for_the_create_stage():
    url = "https://reference.example.invalid/placeholder"
    statement = f"参考链接：{url}"
    draft, = parse(statement, {"approval_url": field(url, statement)})
    assert draft.approval_url.value is None
    assert draft.approval_url.uncertainty is None


def test_conflicting_archive_urls_remain_uncertain():
    urls = [f"https://archive.example.invalid/{suffix}" for suffix in ("first", "second")]
    statements = [f"存档链接：{url}" for url in urls]
    draft, = parse("。\n".join(statements), {
        "approval_url": {"candidates": [field(url, statement) for url, statement in zip(urls, statements, strict=True)]},
    })
    assert draft.approval_url.value is None
    assert [item.value for item in draft.approval_url.uncertainty.candidates] == urls


def test_g152_conflicts_are_limited_to_applicable_policy_and_shared_text():
    headings = ["1.占位甲包用户（赞比亚）", "2.占位乙包用户（莫桑比克）"]
    body = (
        f"不允许多买多赠。\n{headings[0]}\n仅触发一次。\n多买多赠：否。\n"
        f"{headings[1]}\n本批统一允许多买多赠。"
    )
    first, second = parse(body, *[{"company": field(None, heading)} for heading in headings])
    assert first.trigger_once.uncertainty is not None
    assert first.repeat_bonus.uncertainty is not None
    assert second.trigger_once.value == "否"
    assert second.repeat_bonus.value == "是"
    assert [quote.text for quote in second.repeat_bonus.evidence] == ["本批统一允许多买多赠"]


def test_field_defaults_are_blocked_when_policy_boundary_is_uncertain():
    tag_line = "用户标签：占位标签甲"
    reply = {"user_tags": field(["占位标签甲"], tag_line)}
    drafts = parse(f"{tag_line}。", reply, reply)
    for draft in drafts:
        for name in ("policy_name", "trigger_once", "repeat_bonus", "effective_multiplier",
                     "approval_attachments", "policy_type"):
            result = getattr(draft, name)
            assert result.value is None
            assert result.uncertainty.reason == "段落边界无法确定，需人工确认"
        assert {ONCE_HINT, REPEAT_HINT, MULTIPLIER_HINT, ATTACHMENTS_HINT, POLICY_TYPE_HINT}.isdisjoint(draft.hints)


def test_synthetic_policy_resolves_all_fields_and_defers_archive_url():
    heading = "1.占位星光包用户（赞比亚）"
    interval = "2099.2.1-2099.3.31"
    tag = "占位标签甲"
    body = (
        f"赞比亚占位申请\n申请周期：{interval}\n{heading}\n"
        f"用户标签：{tag}。用户范围：占位休眠用户。\n"
        "占位星光包充一送一。可充1个月，再赠送1个月；不限制升降包，换到占位云朵包仍可参加。"
    )
    draft, = parse(body, {
        "company": field(None, heading),
        "start_date": field("2099-02-01", interval),
        "end_date": field("2099-03-31", interval),
        "benefit_type": field("赠送时长", "充一送一"),
        "user_tags": field([tag], f"用户标签：{tag}"),
        "user_scope": field("占位休眠用户", "用户范围：占位休眠用户"),
        "cancel_on_downgrade": field("否", "不限制升降包"),
        "recharge_days": field(30, "充1个月"),
        "gift_days": field(30, "赠送1个月"),
    })
    assert draft.uncertain_fields() == []
    assert draft.policy_type.value == "定向政策"
    assert draft.approval_url.value is None
    assert draft.approval_attachments.value == []
    assert draft.policy_name.value == tag
    assert draft.company.value == "占位公司甲"
    assert draft.packages.value == [PackageRow(current_package="占位星光", gift_package="占位星光_copy")]
    assert draft.start_date.value == "2099-02-01"
    assert draft.end_date.value == "2099-03-31"
    assert draft.benefit_type.value == "赠送时长"
    assert draft.user_tags.value == [tag]
    assert draft.user_scope.value == "占位休眠用户"
    assert draft.cancel_on_downgrade.value == "否"
    assert draft.recharge_days.value == draft.gift_days.value == 30
    assert draft.maintain_mutual_exclusion_group.value == "是"
    assert draft.trigger_once.value == "否"
    assert draft.repeat_bonus.value == "是"
    assert draft.effective_multiplier.value == 1
    assert {ONCE_HINT, REPEAT_HINT, MULTIPLIER_HINT, ATTACHMENTS_HINT, POLICY_TYPE_HINT} <= set(draft.to_dict()["hints"])
