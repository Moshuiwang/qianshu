import json
from datetime import date
from pathlib import Path

import pytest

from qianshu.feishu import Message
from qianshu.feishu.entrance import Application
from qianshu.parse import parse_application
from qianshu.pipeline import Pipeline
from tests.mingdao.test_create_order import case
from tests.protocol_fixtures import fixed_reply, statement

COMPANIES = Path(__file__).parent / "fixtures" / "companies.tsv"
TODAY = date(2099, 1, 12)
COMPOSED = "ZM_合成加赠_20990112"


def field(value, excerpt):
    return {"value": value, "evidence": [excerpt]}


def application(name=None):
    body = ("赞比亚占位甲包优惠，面向全体且用户标签留空。"
            "合成加赠：充30天送30天，升降包均可。"
            "起日2099-01-10，止日2099-02-15。"
            "仅限占位新用户。")
    fields = {
        "country": field("赞比亚", "赞比亚"),
        "user_tags": field([], "面向全体且用户标签留空"),
        "policy_short_name": field("合成加赠", "合成加赠：充30天送30天"),
        "start_date": field("2099-01-10", "起日2099-01-10"),
        "end_date": field("2099-02-15", "止日2099-02-15"),
        "benefit_type": field("赠送时长", "充30天送30天"),
        "recharge_days": field(30, "充30天"),
        "gift_days": field(30, "送30天"),
        "cancel_on_downgrade": field("否", "升降包均可"),
        "packages": field([{"current_package": "占位甲包", "gift_package": None}], "占位甲包"),
        "user_scope": field("占位新用户", "仅限占位新用户"),
    }
    if name is not None:
        body = body.replace("仅限占位新用户。", f"名称用「{name}」，仅限占位新用户。")
        fields["policy_name"] = field(name, name)
    return body, fields


def model_reply(reply):
    return lambda **_: json.dumps(reply, ensure_ascii=False)


def parse_one(body, reply, **options):
    draft, = parse_application(body, model=model_reply(reply), companies_file=COMPANIES, today=TODAY, **options)
    return draft


def card_text(body, model, today=TODAY):
    message = Message("合成消息", "合成群", "合成发送者", 0, "text", "")
    pipeline = Pipeline(model=model, tag_dates={}, companies_file=COMPANIES, today=lambda: today)
    return "\n".join(element["text"]["content"] for page in pipeline(Application(message, body, ()))
                     for element in page.elements if element["tag"] == "div")


def test_copies_explicit_name_without_overwriting_or_consuming_restriction():
    name = " 合成原名甲 "
    body, fields = application(name)
    reply = fixed_reply(body, [fields])
    draft = parse_one(body, reply)
    assert draft.policy_name.value == name
    assert draft.policy_name_source == "申请原文"
    assert draft.policy_name.evidence[0].text == name
    assert draft.user_scope.value == "占位新用户"
    card = card_text(body, model_reply(reply))
    assert f"优惠政策名称\n值　{name}" in card
    assert "优惠政策名称 · 名称来源\n申请原文" in card
    assert "原文出处　「仅限占位新用户」" in card


def test_parser_regression_and_card_show_same_r1_name_and_source():
    body, fields = application()
    model = model_reply(fixed_reply(body, [fields]))
    draft, = parse_application(body, model=model, companies_file=COMPANIES, today=TODAY)
    assert draft.policy_name.value is None
    assert draft.policy_name.uncertainty.candidates[0].value == COMPOSED
    assert draft.policy_name_source == "按格式拼出，待同意"
    assert draft.field_results["policy_name"].statement.status == "原文未写"
    assert draft.field_results["policy_name"].needs_confirmation
    assert draft.start_date.value == "2099-01-10"

    card = card_text(body, model)
    assert f"优惠政策名称 · 候选 1\n值　{COMPOSED}" in card
    assert "优惠政策名称 · 名称来源\n按格式拼出，待同意" in card
    assert "开始日期\n值　2099-01-12" in card
    assert "名称建议待产品负责人在群卡片上同意" in card


@pytest.mark.parametrize("status", ["原文未写", "歧义", "冲突", "带条件", "缺证据", "旧回复无短名字段"])
def test_unavailable_short_name_goes_to_manual_review(status):
    body, fields = application()
    fields.pop("policy_short_name")
    reply = fixed_reply(body, [fields])
    declarations = reply["policies"][0]["fields"]
    if status == "旧回复无短名字段":
        declarations.pop("policy_short_name")
    elif status != "原文未写":
        declarations["policy_short_name"] = statement(status)
    draft = parse_one(body, reply, recorded_reply=status == "旧回复无短名字段")
    assert draft.policy_name.value is None and draft.policy_name_source is None
    assert "请人工" in draft.policy_name.uncertainty.reason


@pytest.mark.parametrize("status", ["歧义", "冲突", "带条件", "缺证据"])
def test_unresolved_name_is_never_replaced_and_same_sentence_limit_remains(status):
    body, fields = application("合成待定名称")
    reply = fixed_reply(body, [fields])
    name = reply["policies"][0]["fields"]["policy_name"]
    name.update(status=status, value=None, candidates=[{"value": name["value"], "evidence": name["evidence"]}])
    name["evidence"] = []
    draft = parse_one(body, reply)
    assert draft.policy_name.value is None and draft.policy_name_source is None
    assert draft.field_results["policy_name"].statement.status == status
    assert draft.policy_name.uncertainty.candidates[0].value == "合成待定名称"
    assert draft.user_scope.value == "占位新用户"


@pytest.mark.parametrize("input_field", ["country", "start_date", "user_tags", "policy_type", "policy_name"])
def test_proposal_requires_verified_inputs_and_valid_absence(input_field):
    body, fields = application()
    fields.pop(input_field, None)
    reply = fixed_reply(body, [fields])
    if input_field in ("policy_type", "policy_name"):
        reply["policies"][0]["fields"][input_field]["value"] = "合成占位"
    draft = parse_one(body, reply)
    assert draft.policy_name.value is None and draft.policy_name.uncertainty


def test_explicit_name_requires_a_matching_name_excerpt():
    body, fields = application("合成名称")
    fields["policy_name"]["value"] = "合成另一名称"
    draft = parse_one(body, fixed_reply(body, [fields]))
    assert draft.policy_name.value is None
    assert "逐字匹配" in draft.policy_name.uncertainty.reason


@pytest.mark.parametrize("name,known,issue", [
    ("名" * 50, (), None), ("名" * 51, (), "policy_name_too_long"),
    ("合成已知名", ("合成已知名",), "policy_name_duplicate"),
    (None, (COMPOSED,), "policy_name_duplicate"),
])
def test_name_length_and_known_name_collisions(name, known, issue):
    body, fields = application(name)
    draft = parse_one(body, fixed_reply(body, [fields]), known_policy_names=known)
    if issue:
        assert draft.policy_name.value is None and draft.policy_name.uncertainty
        assert issue in draft.field_results["policy_name"].issues
    else:
        assert draft.policy_name.value == name


def test_batch_detects_collision_between_copied_and_composed_names():
    first = f"占位政策名：{COMPOSED}，所有占位账号均可，无需标签。\n"
    second, second_fields = application()
    body = first + second
    first_fields = {"policy_name": field(COMPOSED, COMPOSED), "user_tags": field([], "无需标签")}
    reply = fixed_reply(body, [first_fields, second_fields], policy_ranges=[(0, len(first)), (len(first), len(body))])
    drafts = parse_application(body, model=model_reply(reply), companies_file=COMPANIES, today=TODAY)
    assert len(drafts) == 2
    for draft in drafts:
        assert draft.policy_name.value is None and draft.policy_name.uncertainty
        assert "policy_name_duplicate" in draft.field_results["policy_name"].issues


@pytest.mark.parametrize("tags", [["标签甲"], ["标签甲", "标签乙"]])
def test_targeted_names_require_one_tag(tags):
    body = "合成定向政策包含 " + " 和 ".join(tags)
    fields = {"user_tags": {"value": tags, "evidence": tags}}
    draft = parse_one(body, fixed_reply(body, [fields]))
    if len(tags) == 1:
        assert draft.policy_name.value == tags[0]
        assert draft.policy_name_source == "用户标签"
    else:
        assert draft.policy_name.value is None
        assert draft.policy_name.uncertainty


def test_future_date_and_lookup_ambiguity():
    from qianshu.parse.names import country_codes

    body, fields = application()
    reply = fixed_reply(body, [fields])
    draft, = parse_application(body, model=model_reply(reply), today=date(2099, 1, 1), companies_file=COMPANIES)
    assert draft.policy_name.uncertainty.candidates[0].value == "ZM_合成加赠_20990110"
    ambiguous = country_codes("赞比亚\tZM\n占位国家\tZM\tXX")
    assert parse_one(body, reply, country_codes=ambiguous).policy_name.value is None


@pytest.mark.parametrize("damage", ["wrong_block", "candidates", "type_conflict"])
def test_proposal_requires_verified_short_name_and_public_type(damage):
    body, fields = application()
    reply = fixed_reply(body, [fields])
    values = reply["policies"][0]["fields"]
    short = values["policy_short_name"]
    if damage == "wrong_block":
        short["evidence"][0]["span"]["source_id"] = "missing"
    elif damage == "candidates":
        short["candidates"] = [{"value": value, "evidence": short["evidence"]} for value in ("甲", "乙")]
    else:
        values["policy_type"] = statement("确定", value="定向政策", evidence=values["user_tags"]["evidence"])
    draft = parse_one(body, reply)
    assert draft.policy_name.value is None and draft.policy_name.uncertainty


def test_new_reply_requires_short_name_declaration():
    from pydantic import ValidationError
    from qianshu.parse.protocol import ApplicationReply

    body, fields = application()
    reply = fixed_reply(body, [fields])
    del reply["policies"][0]["fields"]["policy_short_name"]
    with pytest.raises(ValidationError) as error:
        ApplicationReply.model_validate_json(json.dumps(reply))
    assert error.value.errors()[0]["loc"][-1] == "policy_short_name"
    assert parse_one(body, reply).field_results["policy_short_name"].statement.status == "缺证据"


def test_offline_name_matches_card_and_keeps_existing_public_order_gate(case):
    from dataclasses import asdict
    from qianshu.mingdao import create_order

    body, fields = application()
    reply = fixed_reply(body, [fields])
    draft = parse_one(body, reply)
    with pytest.raises(create_order.Stop, match="policy_name 待确认"):
        create_order.draft_values(asdict(draft))

    values = {**case.values, "user_tags": [], "policy_name": draft.policy_name.uncertainty.candidates[0].value}
    controls, _ = create_order.assemble(values, case.cells, case.config, case.relations.__getitem__, is_test=True)
    name, = [row["value"] for row in controls if row["controlId"] == case.control["policy_name"]]
    assert name == COMPOSED
    assert f"优惠政策名称 · 候选 1\n值　{name}" in card_text(body, model_reply(reply))
