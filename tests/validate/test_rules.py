from dataclasses import asdict, replace
from datetime import date

import pytest

from qianshu.draft import Candidate, DraftField, PackageRow, PolicyDraft, SourceExcerpt, Uncertainty
from qianshu.validate import Finding, ValidationFacts, validate_batch, validate_draft


TODAY = date(2099, 1, 10)
FACTS = ValidationFacts(latest_tag_end_date=date(2099, 1, 31))
CUTOFF_WARNING = Finding("R1", "start_date", "BOSS 配置截止时间尚未确认，生效日期暂不顺延。")


@pytest.fixture
def draft():
    return PolicyDraft(
        policy_name=DraftField(value="占位政策"),
        maintain_mutual_exclusion_group=DraftField(value="是"),
        company=DraftField(value="占位公司"),
        policy_type=DraftField(value="定向政策"),
        start_date=DraftField(value="2099-01-10"),
        end_date=DraftField(value="2099-01-31"),
        benefit_type=DraftField(value="赠送时长"),
        trigger_once=DraftField(value="否"),
        cancel_on_downgrade=DraftField(value="否"),
        recharge_days=DraftField(value=30),
        gift_days=DraftField(value=0),
        repeat_bonus=DraftField(value="否"),
        effective_multiplier=DraftField(value=1),
        user_tags=DraftField(value=["占位标签"]),
        packages=DraftField(value=[PackageRow(current_package="占位节目包")]),
        user_scope=DraftField(value="占位用户范围"),
        approval_url=DraftField(value="https://example.invalid/placeholder"),
        approval_attachments=DraftField(),
    )


@pytest.mark.parametrize("value, warnings", [
    ("2099-01-10", (CUTOFF_WARNING,)),
    ("2099-01-11", ()),
])
def test_r1_keeps_today_or_future_without_cutoff(draft, value, warnings):
    source = replace(draft, start_date=DraftField(value=value))
    normalized, result = validate_draft(source, today=TODAY, facts=FACTS)

    assert normalized.start_date.value == value
    assert result.can_create and not result.adjustments
    assert result.warnings == warnings


def test_r1_advances_past_date_without_mutating_source(draft):
    source = replace(draft, start_date=DraftField(value="2099-01-09"))
    normalized, result = validate_draft(source, today=TODAY, facts=FACTS)

    assert source.start_date.value == "2099-01-09"
    assert normalized.start_date.value == "2099-01-10"
    assert result.can_create
    assert result.adjustments == (Finding("R1", "start_date", "开始日期早于今天，已调整为今天。"),)
    assert result.warnings == (CUTOFF_WARNING,)


def test_r1_rejects_start_after_end(draft):
    source = replace(draft, end_date=DraftField(value="2099-01-09"))
    _, result = validate_draft(source, today=TODAY, facts=FACTS)

    assert not result.can_create
    assert [(item.rule, item.field) for item in result.errors] == [("R1", "start_date")]


@pytest.mark.parametrize("start", ["2099-01-32", "20990111"])
def test_r1_rejects_invalid_or_noncanonical_start_date(draft, start):
    source = replace(draft, start_date=DraftField(value=start))
    _, result = validate_draft(source, today=TODAY, facts=FACTS)

    assert not result.can_create
    assert result.errors == (
        Finding("R1", "start_date", "请申请人提供 YYYY-MM-DD 格式的有效开始日期。"),
    )


def test_r3_accepts_one_shadow_tag(draft):
    source = replace(draft, user_tags=DraftField(value=["占位标签_image"]))
    _, result = validate_draft(source, today=TODAY, facts=FACTS)

    assert result.can_create


@pytest.mark.parametrize("tags, shadow_activity", [
    (["占位标签_image", "另一个占位标签"], False),
    (["占位标签甲", "占位标签乙"], True),
    ([], True),
])
def test_r3_rejects_other_tag_counts_for_shadow_activity(draft, tags, shadow_activity):
    source = replace(draft, user_tags=DraftField(value=tags))
    facts = replace(FACTS, shadow_activity=shadow_activity)
    _, result = validate_draft(source, today=TODAY, facts=facts)

    assert not result.can_create
    assert Finding("R3", "user_tags", "影子标签类活动须选择一个标签，请申请人确认。") in result.errors


def test_r4_different_or_unavailable_counts_do_not_trigger_duplicate_warning(draft):
    policies = [(draft, replace(FACTS, user_count=count)) for count in (0, 1, None, None)]

    results = validate_batch(policies, today=TODAY)

    assert all(result.can_create and result.warnings == (CUTOFF_WARNING,) for _, result in results)


def test_r4_duplicate_user_counts_warn_on_each_matching_policy(draft):
    policies = [(draft, replace(FACTS, user_count=count)) for count in (0, 1, 0)]
    results = validate_batch(policies, today=TODAY)
    warning = Finding("R4", "user_count", "同批有多条政策的用户数相同，可能选重了标签。")

    assert all(result.can_create for _, result in results)
    assert [result.warnings for _, result in results] == [
        (CUTOFF_WARNING, warning), (CUTOFF_WARNING,), (CUTOFF_WARNING, warning),
    ]


def test_r5_accepts_zero_and_missing_optional_attachment(draft):
    _, result = validate_draft(draft, today=TODAY, facts=FACTS)

    assert result.can_create


@pytest.mark.parametrize("field, value", [
    ("gift_days", DraftField()),
    ("policy_name", DraftField(value="  ")),
    ("packages", DraftField(value=[])),
    ("packages", DraftField(value=[PackageRow(current_package="  ")])),
    ("gift_days", DraftField(value=0, uncertainty=Uncertainty(reason="占位待确认原因"))),
    ("approval_attachments", DraftField(uncertainty=Uncertainty(reason="占位待确认原因"))),
    ("user_tags", DraftField(uncertainty=Uncertainty(reason="占位待确认原因"))),
])
def test_r5_rejects_missing_required_or_uncertain_fields(draft, field, value):
    _, result = validate_draft(replace(draft, **{field: value}), today=TODAY, facts=FACTS)

    assert not result.can_create
    assert [(item.rule, item.field) for item in result.errors] == [("R5", field)]
    assert "占位待确认原因" not in repr(result)


@pytest.mark.parametrize("choice, evidence, explicit", [
    (None, [], False),
    ("否", [], False),
    ("否", [], True),
    ("否", [SourceExcerpt(text="占位活动说明")], False),
])
def test_r7_defaults_to_maintain_without_explicit_opt_out(draft, choice, evidence, explicit):
    field = DraftField(value=choice, evidence=evidence)
    source = replace(draft, maintain_mutual_exclusion_group=field)
    facts = replace(FACTS, explicit_mutual_exclusion_opt_out=explicit)
    normalized, result = validate_draft(source, today=TODAY, facts=facts)

    assert source.maintain_mutual_exclusion_group.value == choice
    assert normalized.maintain_mutual_exclusion_group.value == "是"
    assert not normalized.maintain_mutual_exclusion_group.evidence
    assert result.can_create
    assert [(item.rule, item.field) for item in result.adjustments] == [
        ("R7", "maintain_mutual_exclusion_group"),
    ]


def test_r7_keeps_explicit_opt_out_with_evidence(draft):
    choice = DraftField(value="否", evidence=[SourceExcerpt(text="占位说明：不维护互斥组")])
    source = replace(draft, maintain_mutual_exclusion_group=choice)
    facts = replace(FACTS, explicit_mutual_exclusion_opt_out=True)
    normalized, result = validate_draft(source, today=TODAY, facts=facts)

    assert normalized.maintain_mutual_exclusion_group.value == "否"
    assert result.can_create and not result.adjustments


def test_r7_uncertain_choice_stays_blocked(draft):
    choice = DraftField(value="否", uncertainty=Uncertainty(reason="占位待确认原因"))
    source = replace(draft, maintain_mutual_exclusion_group=choice)
    normalized, result = validate_draft(source, today=TODAY, facts=FACTS)

    assert normalized.maintain_mutual_exclusion_group is choice
    assert not result.can_create and not result.adjustments


def test_r7_rejects_choice_other_than_yes_or_no(draft):
    source = replace(draft, maintain_mutual_exclusion_group=DraftField(value="占位选项"))
    _, result = validate_draft(source, today=TODAY, facts=FACTS)

    assert not result.can_create
    assert [(item.rule, item.field) for item in result.errors] == [
        ("R7", "maintain_mutual_exclusion_group"),
    ]


def test_r8_accepts_targeted_policy_ending_on_latest_tag_end_date(draft):
    source = replace(draft, user_tags=DraftField(value=["占位标签甲", "占位标签乙"]))
    _, result = validate_draft(source, today=TODAY, facts=FACTS)

    assert result.can_create


@pytest.mark.parametrize("tags", [None, []])
def test_r8_accepts_public_policy_with_no_tags(draft, tags):
    source = replace(
        draft, policy_type=DraftField(value="公开政策"), user_tags=DraftField(value=tags),
    )
    _, result = validate_draft(source, today=TODAY)

    assert result.can_create


@pytest.mark.parametrize("tags, latest, field", [
    (None, None, "user_tags"),
    ([], None, "user_tags"),
    (["占位标签"], date(2099, 1, 30), "end_date"),
    (["占位标签"], None, "end_date"),
])
def test_r8_rejects_missing_tags_or_unverified_date_bounds(draft, tags, latest, field):
    source = replace(draft, user_tags=DraftField(value=tags))
    _, result = validate_draft(source, today=TODAY, facts=ValidationFacts(latest_tag_end_date=latest))

    assert not result.can_create
    assert [(item.rule, item.field) for item in result.errors] == [("R8", field)]


def test_r8_rejects_invalid_end_date(draft):
    source = replace(draft, end_date=DraftField(value="2099/01/31"))
    _, result = validate_draft(source, today=TODAY, facts=FACTS)

    assert not result.can_create
    assert result.errors == (
        Finding("R8", "end_date", "请申请人提供 YYYY-MM-DD 格式的有效结束日期。"),
    )


def test_r9_accepts_a_web_url_and_user_scope(draft):
    _, result = validate_draft(draft, today=TODAY, facts=FACTS)

    assert result.can_create


@pytest.mark.parametrize("field, value", [
    ("approval_url", None),
    ("approval_url", "占位审批链接"),
    ("approval_url", "https:///placeholder"),
    ("approval_url", "https://example.invalid:placeholder"),
    ("approval_url", "https://example.invalid/占位 内容"),
    ("approval_url", "https://placeholder@example.invalid/placeholder"),
    ("approval_url", "ftp://example.invalid/placeholder"),
    ("user_scope", "  "),
])
def test_r9_rejects_missing_or_invalid_url_or_scope(draft, field, value):
    _, result = validate_draft(
        replace(draft, **{field: DraftField(value=value)}), today=TODAY, facts=FACTS,
    )

    assert not result.can_create
    assert [(item.rule, item.field) for item in result.errors] == [("R9", field)]
    assert "占位" not in repr(result)


def test_r10_accepts_company_determined_from_the_application(draft):
    choice = DraftField(value="占位公司乙", evidence=[SourceExcerpt(text="占位申请为公司乙")])
    _, result = validate_draft(replace(draft, company=choice), today=TODAY, facts=FACTS)

    assert result.can_create


def test_r10_blocks_ambiguous_company_and_reports_only_diagnostic_fields(draft):
    choice = DraftField(value="占位公司甲", uncertainty=Uncertainty(
        reason="占位待确认原因",
        candidates=[Candidate(value="占位公司甲"), Candidate(value="占位公司乙")],
    ))
    _, result = validate_draft(replace(draft, company=choice), today=TODAY, facts=FACTS)

    assert not result.can_create
    assert asdict(result) == {
        "errors": (
            {"rule": "R5", "field": "company", "reason": "字段尚未确认，请申请人确认。"},
            {"rule": "R10", "field": "company", "reason": "无法确定适用公司，请申请人明确公司名称。"},
        ),
        "warnings": (asdict(CUTOFF_WARNING),),
        "adjustments": (),
    }
