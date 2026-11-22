from pathlib import Path

from qianshu.regression.runner import evaluate, markdown_report
from tests.parse.test_names import COMPANIES, TODAY, application, model_reply, parse_one
from tests.protocol_fixtures import fixed_reply
from tests.regression.test_regression import corpus, frozen, repo


def test_public_name_grade_uses_independent_gold():
    body, fields = application()
    reply = fixed_reply(body, [fields])
    model = model_reply(reply)
    draft = parse_one(body, reply)
    token, sample = "synthetic-document", "synthetic-policy"
    frozen = {
        "samples": {sample: {"country": "赞比亚", "benefit_type": "赠送时长", "policy_type": "公开政策"}},
        "pairs": {sample: {"id": sample, "token": token, "span": [0, len(body)],
                           "configuration": {"user_tags": {"value": []}, "start_date": {"value": "2099-01-10"}},
                           "source_status": {"user_tags": "确定", "policy_name": "原文未写"},
                           "policy_short_name": "合成加赠"}},
        "denominators": {"fields": {}, "whole_policy": []},
        "documents": {token: {"body": body, "complete": True, "other_spans": []}},
        "companies_text": COMPANIES.read_text(), "fingerprint": "synthetic", "strata": [],
    }
    report, details = evaluate(frozen, model, Path("rules"), today=TODAY)
    scored, = details["business"][0]["policies"]
    parsed = scored["parsed"]["policy_name"]
    assert parsed["value"] is None
    candidate, = parsed["uncertainty"]["candidates"]
    assert candidate["value"] == draft.policy_name.uncertainty.candidates[0].value
    assert scored["parsed"]["policy_name_source"] == draft.policy_name_source
    assert report["business"]["policy_names"]["公开政策"]["correct_proposal"] == 1
    assert "公开政策 可比 1，正确建议 1，错误建议 0，正确确定 0" in markdown_report(report).decode()

    def score(day):
        report, _ = evaluate(frozen, model, Path("rules"), today=day)
        return report["business"]["public_name_sources"]["按格式拼出，待同意"]

    assert score(TODAY)["correct_proposal"] == 1
    assert score(None)["unknown"] == 1
    frozen["pairs"][sample]["policy_short_name"] = "独立审定短名"
    assert score(TODAY)["incorrect_proposal"] == 1
    del frozen["pairs"][sample]["policy_short_name"]
    assert score(TODAY)["unknown"] == 1


def test_replay_keeps_recorded_utc_day(repo, frozen):
    from qianshu.regression.__main__ import main
    from tests.regression.test_regression import _replay_args, get_json, no_policies, run_args

    assert main([*run_args(frozen), "--today", TODAY.isoformat()], model=no_policies) == 0
    assert main(_replay_args(frozen)) == 0
    for directory in ("run", "replay"):
        record = get_json(repo / directory / "recording.json")
        assert record["configuration"]["today"] == TODAY.isoformat()
