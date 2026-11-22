import json
from pathlib import Path

import pytest

from qianshu.parse import parse_application
from tests.protocol_fixtures import clause, document, fixed_reply, policy, quote, span, statement


COMPANIES_FILE = Path(__file__).parent / "fixtures" / "companies.tsv"
ONCE_HINT = "是否只触发一次按默认填否"
MULTIPLIER_HINT = "生效倍数按默认填 1"
ATTACHMENTS_HINT = "审批件按默认留空"
POLICY_TYPE_HINT = "政策类型按用户标签判定"


def field(value, *quotes):
    return {"value": value, "evidence": list(quotes)}


def parse(body, *reply):
    return parse_application(body, model=lambda **_: json.dumps(fixed_reply(body, reply)), companies_file=COMPANIES_FILE)


@pytest.mark.parametrize("attachment", ["", "审批件附件：占位审批截图.png"])
def test_approval_attachments_default_to_confirmed_empty(attachment):
    draft, = parse(f"占位说明。\n{attachment}", {
        "approval_attachments": field(None),
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
    draft, = parse(statement, {"user_tags": field(tags, *(tags or [statement]))})
    result = draft.policy_type
    assert result.value == expected
    assert (result.uncertainty is None) == (expected is not None)
    assert (POLICY_TYPE_HINT in draft.hints) == (expected is not None)


@pytest.mark.parametrize(
    ("value", "tag", "expected"),
    [("占位名称", "占位标签甲", None), ("占位标签甲", "占位标签甲", "占位标签甲"), (None, "占位标签甲", "占位标签甲")],
)
def test_policy_name_requires_one_confirmed_tag_without_a_conflicting_name(value, tag, expected):
    body = "占位名称，标签占位标签甲。"
    draft, = parse(body, {
        "policy_name": field(value, value) if value else field(None),
        "user_tags": field([tag], tag),
    })
    assert draft.policy_name.value == expected


def test_policy_type_conflict_reports_the_tag_rule_and_claim():
    body = "公开政策，明确标签占位标签甲。"
    draft, = parse(body, {
        "policy_type": field("公开政策", "公开政策"),
        "user_tags": field(["占位标签甲"], "占位标签甲"),
    })
    assert draft.policy_type.value is None
    assert {item.value for item in draft.policy_type.uncertainty.candidates} == {"公开政策", "定向政策"}


@pytest.mark.parametrize(("trigger", "multiplier", "trigger_result", "multiplier_result"), [
    (None, None, "否", 1), ("是", 2, None, None), ("否", 1, "否", 1),
])
def test_explicit_trigger_and_multiplier_obey_product_rules(
    trigger, multiplier, trigger_result, multiplier_result,
):
    body = "合成触发声明。合成倍数声明。"
    draft, = parse(body, {
        "trigger_once": field(trigger, "合成触发声明"),
        "effective_multiplier": field(multiplier, "合成倍数声明"),
    })
    assert draft.trigger_once.value == trigger_result
    assert draft.effective_multiplier.value == multiplier_result
    assert (ONCE_HINT in draft.hints) == (trigger is None)
    assert (MULTIPLIER_HINT in draft.hints) == (multiplier is None)


def test_unrelated_reference_url_is_left_for_the_create_stage():
    body = "本批没有存档链接。参考链接：https://source.example.invalid/"
    source = quote(body, "本批没有存档链接", "url")
    main = span(0, len(body))
    p = policy("p1", main, {"approval_url": statement("确定", evidence=[source])})
    reply = document(body, [p], [clause("url", [source["span"]], ["approval_url"], ["p1"])])
    draft, = parse_application(body, model=lambda **_: json.dumps(reply), companies_file=COMPANIES_FILE)
    assert draft.approval_url.value is None
    assert draft.approval_url.uncertainty is None
    assert draft.field_results["approval_url"].has_value
    assert draft.field_results["approval_url"].source == "原文"
