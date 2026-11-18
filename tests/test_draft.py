import json
from dataclasses import replace

import pytest

from qianshu.draft import (
    Candidate,
    DraftField,
    PackageRow,
    PolicyDraft,
    SourceExcerpt,
    Uncertainty,
)


@pytest.fixture
def draft():
    promotion = SourceExcerpt(text="充 30 送 15", start=0)
    return PolicyDraft(
        policy_name=DraftField(value="占位政策"),
        maintain_mutual_exclusion_group=DraftField(value="是"),
        company=DraftField(value="占位公司"),
        policy_type=DraftField(value="定向政策"),
        start_date=DraftField(value="2099-01-01"),
        end_date=DraftField(value="2099-01-31"),
        benefit_type=DraftField(value="赠送时长", evidence=[promotion]),
        trigger_once=DraftField(value="否"),
        cancel_on_downgrade=DraftField(value="否"),
        recharge_days=DraftField(value=30, evidence=[promotion]),
        gift_days=DraftField(
            value=15,
            evidence=[promotion, SourceExcerpt(text="赠送十五天")],
        ),
        repeat_bonus=DraftField(value="否"),
        effective_multiplier=DraftField(value=1),
        user_tags=DraftField(value=["占位标签甲", "占位标签乙"]),
        packages=DraftField(value=[
            PackageRow(current_package="占位包甲", gift_package="占位包甲_copy"),
            PackageRow(current_package="占位包乙"),
        ]),
        user_scope=DraftField(value="占位用户范围"),
        approval_url=DraftField(value="https://example.invalid/placeholder"),
        approval_attachments=DraftField(value=[]),
    )


def test_policy_fields_and_evidence_are_json_serializable(draft):
    payload = draft.to_dict()
    assert json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False)) == payload
    assert set(payload) == {
        "policy_name", "maintain_mutual_exclusion_group", "company", "policy_type",
        "start_date", "end_date", "benefit_type", "trigger_once", "cancel_on_downgrade",
        "recharge_days", "gift_days", "repeat_bonus", "effective_multiplier", "user_tags",
        "packages", "user_scope", "approval_url", "approval_attachments",
    }
    assert all(set(item) == {"value", "evidence", "uncertainty"} for item in payload.values())
    assert payload["gift_days"] == {
        "value": 15,
        "evidence": [
            {"text": "充 30 送 15", "start": 0},
            {"text": "赠送十五天", "start": None},
        ],
        "uncertainty": None,
    }
    assert payload["user_tags"]["value"] == ["占位标签甲", "占位标签乙"]
    assert payload["packages"]["value"] == [
        {"current_package": "占位包甲", "gift_package": "占位包甲_copy"},
        {"current_package": "占位包乙", "gift_package": None},
    ]
    assert payload["approval_attachments"]["value"] == []
    assert draft.uncertain_fields() == []


def test_missing_end_date_and_pending_fields_can_be_reported(draft):
    draft = replace(
        draft,
        end_date=DraftField(uncertainty=Uncertainty(reason="正文未提供结束日期")),
        effective_multiplier=DraftField(uncertainty=Uncertainty(
            reason="生效倍数规则待确认",
            candidates=[Candidate(value=1)],
        )),
        approval_attachments=DraftField(uncertainty=Uncertainty(
            reason="是否上传待确认",
            candidates=[Candidate(value=["占位截图"]), Candidate(value=[])],
        )),
    )

    assert draft.uncertain_fields() == ["end_date", "effective_multiplier", "approval_attachments"]
    payload = json.loads(json.dumps(draft.to_dict()))
    assert payload["end_date"] == {
        "value": None,
        "evidence": [],
        "uncertainty": {"reason": "正文未提供结束日期", "candidates": []},
    }
    assert payload["effective_multiplier"]["value"] is None
    assert payload["effective_multiplier"]["uncertainty"]["candidates"] == [
        {"value": 1, "evidence": []},
    ]
    assert payload["approval_attachments"]["uncertainty"]["candidates"] == [
        {"value": ["占位截图"], "evidence": []},
        {"value": [], "evidence": []},
    ]


def test_candidate_evidence_is_preserved_without_exposing_it_in_repr(draft):
    first = SourceExcerpt(text="占位公司甲", start=0)
    second = SourceExcerpt(text="占位公司乙", start=6)
    uncertainty = Uncertainty(
        reason="正文列了两家公司",
        candidates=[
            Candidate(value="占位公司甲", evidence=[first]),
            Candidate(value="占位公司乙", evidence=[second]),
        ],
    )
    draft = replace(draft, company=DraftField(evidence=[first, second], uncertainty=uncertainty))

    company = draft.to_dict()["company"]
    assert company["value"] is None
    assert company["evidence"] == [
        {"text": "占位公司甲", "start": 0},
        {"text": "占位公司乙", "start": 6},
    ]
    assert company["uncertainty"]["candidates"] == [
        {"value": "占位公司甲", "evidence": [{"text": "占位公司甲", "start": 0}]},
        {"value": "占位公司乙", "evidence": [{"text": "占位公司乙", "start": 6}]},
    ]
    for item in (draft, draft.company, uncertainty, *uncertainty.candidates, first, second):
        assert "占位公司" not in repr(item)
