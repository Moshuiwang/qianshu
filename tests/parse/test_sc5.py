import json
from pathlib import Path

import pytest

from qianshu.parse import parse_application
from qianshu.parse.companies import load_company_rules
from tests.protocol_fixtures import fixed_reply, serialized_empty, statement

MONTH_HINT = "充值 / 赠送天数按 1 月 = 30 天换算"
FIELD_RULE_HINTS = {
    "是否只触发一次按默认填否", "多买多送按默认填是", "生效倍数按默认填 1",
    "审批件按默认留空", "政策类型按用户标签判定",
}
COMPANIES_FILE = Path(__file__).parent / "fixtures" / "companies.tsv"
COMPANY_HEADER = "国家\t关键词\t公司内部编号\t公司显示名\n"


def field(value, *evidence):
    return {"value": value, "evidence": list(evidence)}


def parse(body, *reply, companies_file=COMPANIES_FILE, **kwargs):
    rows = []
    for original in reply:
        row = dict(original)
        if row.get("company") == field(None, "赞比亚"):
            # 冻结的日志测试用旧 company 回包携带国家引文。
            del row["company"]
            row["country"] = field("赞比亚", "赞比亚")
        rows.append(row)
    return parse_application(body, model=lambda **_: json.dumps(fixed_reply(body, rows)), companies_file=companies_file, **kwargs)


def assert_uncertain(draft, *names):
    for name in names:
        result = getattr(draft, name)
        assert result.value is None
        assert result.uncertainty is not None
        assert result.uncertainty.candidates == []


@pytest.mark.parametrize(("amount", "months"), [
    ("1", 1), ("13", 13), ("一", 1), ("二", 2), ("两", 2), ("三", 3), ("四", 4), ("五", 5),
    ("六", 6), ("七", 7), ("八", 8), ("九", 9), ("十", 10), ("十一", 11), ("十二", 12),
])
def test_month_amounts_convert_and_add_one_hint(amount, months):
    body = f"充值 {amount} 个月，赠 {amount} 月"
    draft, = parse(body, {
        "recharge_days": {"value": months, "unit": "月", "evidence": [body]},
        "gift_days": {"value": months, "unit": "月", "evidence": [body]},
    })
    assert draft.recharge_days.value == draft.gift_days.value == months * 30
    assert draft.hints.count(MONTH_HINT) == 1


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
        "country": field("赞比亚", "赞比亚"), "gift_days": field(30, "赠送30天"),
    }, companies_file=table, legacy_companies=True)
    assert_uncertain(draft, "company")
    assert draft.gift_days.value == 30
    assert "公司对照表缺失或无效" in draft.to_dict()["hints"]


@pytest.mark.parametrize("delimiter", [",", "\t"])
def test_editable_company_table_retains_number_and_uses_display_name(tmp_path, delimiter):
    table = tmp_path / "companies"
    header = COMPANY_HEADER.replace("\t", delimiter)
    table.write_text(header + delimiter.join(("赞比亚", "", "0009", '"占位,""甲""公司"')) + "\n"
                     + delimiter.join(("赞比亚", "SYNTHKEY", "0010", "占位关键词公司")) + "\n", encoding="utf-8-sig")
    rules = load_company_rules(Path("rules"), table)
    assert rules.table["赞比亚"][""].internal_number == "0009"
    for body, name in [("赞比亚占位申请", '占位,"甲"公司'), ("赞比亚占位申请 synthKEY", "占位关键词公司")]:
        draft, = parse(body, {"country": field("赞比亚", body),
                               **({"company_keyword": field(["synthKEY"], "synthKEY")} if "synthKEY" in body else {})}, companies_file=table)
        assert draft.company.value == name
        assert "0009" not in json.dumps(draft.to_dict()) and "0010" not in json.dumps(draft.to_dict())


@pytest.mark.parametrize(("data", "line"), [
    pytest.param("国家\t关键词\t公司内部编号\t占位错误表头\n", 1, id="wrong_header"),
    pytest.param("国家\t公司显示名\n", 1, id="short_header"),
    pytest.param("地区\t编号\n赞比亚\t0009\n", 1, id="two_column_header_with_number"),
    pytest.param(COMPANY_HEADER + "赞比亚\t\t0009\t占位公司甲\n赞比亚\t\t0010\t占位公司乙", 3, id="two_defaults"),
    pytest.param(COMPANY_HEADER + "赞比亚\t\t\t占位公司甲", 2, id="missing_number"),
    pytest.param(COMPANY_HEADER + "赞比亚\t\t0009\t", 2, id="missing_display_name"),
    pytest.param(COMPANY_HEADER + "\t\t0009\t占位公司甲", 2, id="missing_country"),
    pytest.param(COMPANY_HEADER + "赞比亚\tSYNTHKEY\t0009\t占位公司甲\n赞比亚\tsynthkey\t0010\t占位公司乙",
                 3, id="duplicate_keyword"),
    pytest.param(COMPANY_HEADER + "赞比亚\tSYNTHKEY\t0009\t占位公司甲\n赞比亚\tSYNTHKEY ZZ\t0010\t占位公司乙",
                 3, id="overlapping_keywords"),
    pytest.param(COMPANY_HEADER + "赞比亚\t\t0009", 2, id="wrong_column_count"),
    pytest.param(COMPANY_HEADER + '赞比亚\t\t0009\t"占位未闭合', 2, id="unclosed_quote"),
    pytest.param(COMPANY_HEADER.encode() + b"\xff", 2, id="invalid_utf8"),
])
def test_editable_company_table_errors_only_log_line_number(tmp_path, caplog, data, line):
    table = tmp_path / "companies.tsv"
    table.write_bytes(data.encode() if isinstance(data, str) else data)
    draft, = parse("赞比亚占位申请，赠送30天", {
        "company": field(None, "赞比亚"), "gift_days": field(30, "赠送30天"),
    }, companies_file=table)
    assert_uncertain(draft, "company")
    assert draft.gift_days.value == 30
    assert caplog.messages == [f"公司对照表第 {line} 行"]
    assert "0009" not in json.dumps(draft.to_dict())


@pytest.mark.parametrize(("legacy", "unknown"), [
    (False, "占位未收录国\t\t0009\t占位未知公司\n"),
    (True, "占位未收录国\t占位未知公司\n"),
])
def test_unknown_country_skips_only_that_row(tmp_path, caplog, legacy, unknown):
    table = tmp_path / "companies.tsv"
    known = "赞比亚\t占位已知公司\n" if legacy else "赞比亚\t\t0010\t占位已知公司\n"
    header = "" if legacy else COMPANY_HEADER
    options = {"companies_file": table, "legacy_companies": legacy}
    table.write_text(header + unknown + known, encoding="utf-8")
    known_draft, = parse("赞比亚占位申请", {"country": field("赞比亚", "赞比亚")}, **options)
    assert known_draft.company.value == "占位已知公司"
    assert caplog.messages == [f"公司对照表第 {1 if legacy else 2} 行"]
    unknown_draft, = parse("占位未收录国申请", {"country": field("占位未收录国", "占位未收录国")}, **options)
    assert_uncertain(unknown_draft, "company")
    table.write_text(header + unknown, encoding="utf-8")
    draft, = parse("赞比亚占位申请", {"country": field("赞比亚", "赞比亚")}, **options)
    assert_uncertain(draft, "company")
    assert "公司对照表缺失或无效" in draft.hints


@pytest.mark.parametrize(("legacy", "bad_row"), [
    pytest.param(False, "占位未收录国\t占位未知公司", id="two_columns"),
    pytest.param(False, "赞比亚,SYNTHKEY,0009,占位关键词公司", id="mixed_delimiters"),
    pytest.param(False, "赞比亚SYNTHKEY\t0009\t占位关键词公司", id="missing_delimiter"),
    pytest.param(False, "赞比\t亚\tSYNTHKEY\t0009\t占位关键词公司", id="extra_delimiter"),
    pytest.param(False, "赞比亚SYNTHKEY", id="truncated_row"),
    pytest.param(False, "# 占位说明", id="comment"),
    pytest.param(False, "占位未收录国\tSYNTHKEY\t\t占位关键词公司", id="missing_number"),
    pytest.param(False, "占位未收录国\tSYNTHKEY\t0009\t", id="missing_display_name"),
    pytest.param(True, "占位未收录国", id="legacy_truncated_row"),
    pytest.param(True, "占位未收录国\t\tSYNTHKEY", id="legacy_missing_display_name"),
])
def test_malformed_company_row_prevents_default_fallback(tmp_path, caplog, legacy, bad_row):
    table = tmp_path / "companies.tsv"
    prefix = "赞比亚\t占位默认公司\n" if legacy else COMPANY_HEADER + "赞比亚\t\t0010\t占位默认公司\n"
    table.write_text(prefix + bad_row + "\n", encoding="utf-8")
    body = "赞比亚占位申请 SYNTHKEY，赠送30天"
    draft, = parse(body, {"country": field("赞比亚", "赞比亚"), "gift_days": field(30, "赠送30天")},
                   companies_file=table, legacy_companies=legacy)
    assert_uncertain(draft, "company")
    assert "公司对照表缺失或无效" in draft.hints
    assert draft.gift_days.value == 30
    assert caplog.messages == [f"公司对照表第 {2 if legacy else 3} 行"]


@pytest.mark.parametrize("unknown_first", [True, False])
def test_legacy_company_row_with_header_word_is_skipped_in_either_order(tmp_path, caplog, unknown_first):
    table = tmp_path / "companies.tsv"
    known = "赞比亚\t占位已知公司\n"
    unknown = "占位未收录国\t占位未知公司\t关键词\n"
    table.write_text(unknown + known if unknown_first else known + unknown, encoding="utf-8")
    draft, = parse("赞比亚占位申请", {"country": field("赞比亚", "赞比亚")},
                   companies_file=table, legacy_companies=True)
    assert draft.company.value == "占位已知公司"
    assert draft.company.uncertainty is None
    assert caplog.messages == [f"公司对照表第 {1 if unknown_first else 2} 行"]


def test_prompt_and_rules_allow_preamble_dates_alongside_non_date_clauses():
    def model(*, system, user):
        assert "前言只可供起止日期取证" in system
        assert "限定对象" in system
        assert "按 R10 对确定的国家" in system
        return serialized_empty()

    assert parse_application("占位申请", model=model, companies_file=COMPANIES_FILE) == []


def test_repository_company_example_is_not_a_mapping(monkeypatch, tmp_path):
    rules_dir = Path("rules").resolve()
    assert (rules_dir / "companies.txt").read_text(encoding="utf-8") == COMPANY_HEADER + "赞比亚\t\t0001\t占位示例公司\n"
    monkeypatch.chdir(tmp_path)
    body = "赞比亚占位申请"
    reply = fixed_reply(body, [{"country": field("赞比亚", "赞比亚")}])
    draft, = parse_application(body, model=lambda **_: json.dumps(reply), rules_dir=rules_dir)
    assert_uncertain(draft, "company")
    assert [hint for hint in draft.hints if hint not in FIELD_RULE_HINTS] == ["互斥组按默认填是", "公司对照表缺失或无效"]


@pytest.mark.parametrize(("country", "expected"), [("赞比亚", "2099-03-31"), ("肯尼亚", None)])
def test_country_qualified_preamble_preserves_model_field_status(country, expected):
    preamble = "仅赞比亚：截止 2099-03-31"
    body = f"{preamble}\n{country}占位申请"
    reply = fixed_reply(body, [{
        "country": field(country, country),
        "end_date": field("2099-03-31", preamble),
    }], policy_ranges=[(len(preamble) + 1, len(body))])
    if expected is None:
        reply["policies"][0]["fields"]["end_date"] = statement("缺证据")
    draft, = parse_application(body, companies_file=COMPANIES_FILE, model=lambda **_: json.dumps(reply))
    assert draft.end_date.value == expected
    assert draft.field_results["country"].value == country
    assert draft.field_results["end_date"].needs_confirmation == (expected is None)


@pytest.mark.parametrize(("company", "expected"), [("占位公司甲", "2099-03-31"), ("占位公司乙", None)])
def test_company_qualified_shared_date_preserves_model_field_status(company, expected):
    preamble = "占位公司甲：合成申请周期到 2099-03-31"
    body = f"{preamble}\n赞比亚，{company}"
    reply = fixed_reply(body, [{
        "country": field("赞比亚", "赞比亚"),
        "end_date": field("2099-03-31", preamble),
    }], policy_ranges=[(len(preamble) + 1, len(body))])
    if expected is None:
        reply["policies"][0]["fields"]["end_date"] = statement("缺证据")
    draft, = parse_application(body, companies_file=COMPANIES_FILE, model=lambda **_: json.dumps(reply))
    assert draft.end_date.value == expected
    assert draft.company.value == "占位公司甲"


def test_model_can_source_date_but_leave_other_fields_unconfirmed():
    body = "合成前言内容\n合成条款"
    first = body.index("\n") + 1
    reply = fixed_reply(body, [{
        "end_date": field("2099-03-31", "合成前言内容"),
    }], policy_ranges=[(first, len(body))])
    reply["policies"][0]["fields"]["benefit_type"] = statement("缺证据")
    draft, = parse_application(body, companies_file=COMPANIES_FILE, model=lambda **_: json.dumps(reply))
    assert draft.end_date.value == "2099-03-31"
    assert draft.benefit_type.value is None
    assert draft.benefit_type.uncertainty is not None


@pytest.mark.parametrize(("end", "expected"), [("2099-02-29", None), ("2099-01-01", None), ("2099-02-15", "2099-02-15")])
def test_invalid_or_reversed_dates_require_confirmation(end, expected):
    body = "合成申请起止日"
    draft, = parse(body, {"start_date": field("2099-02-01", body), "end_date": field(end, body)})
    assert draft.end_date.value == expected
    if end == "2099-01-01":
        assert draft.start_date.value is None


@pytest.mark.parametrize(
    ("clues", "subject", "expected"),
    [
        (None, None, "占位默认公司"),
        (["SYNTH-A"], None, "占位甲公司"),
        (["synth-A业务"], None, "占位甲公司"),
        (["XSYNTH-A"], None, None),
        (["SYNTH-A2"], None, None),
        (["SYNTH-C"], None, None),
        (["SYNTH-A", "SYNTH-B"], None, None),
        (["SYNTH-A"], "占位甲公司", "占位甲公司"),
        (["SYNTH-A"], "占位乙公司", None),
    ],
)
def test_company_keyword_or_country_default(clues, subject, expected):
    body = "，".join(["合成申请", "赞比亚", *(clues or []), *([subject] if subject else [])])
    table = (COMPANY_HEADER + "赞比亚\t\t0001\t占位默认公司\n"
             "赞比亚\tSYNTH-A\t0002\t占位甲公司\n赞比亚\tSYNTH-B\t0003\t占位乙公司\n")
    fixed = {"country": field("赞比亚", "赞比亚")}
    if clues is not None:
        fixed["company_keyword"] = field(clues, *clues)
    if subject is not None:
        fixed["company"] = field(subject, subject)
    draft, = parse(body, fixed, companies_text=table)
    assert draft.company.value == expected
    result = draft.field_results["company"]
    assert result.needs_confirmation is (expected is None)
    assert (result.source == "查表派生") == (expected is not None)
    if clues == ["SYNTH-C"]:
        assert result.issues == ("company_lookup_unresolved",)


@pytest.mark.parametrize("accepted", [False, True])
def test_application_cycle_preserves_model_field_status(accepted):
    body = "合成申请周期"
    reply = fixed_reply(body, [{"end_date": field("2099-03-31", body)}])
    if not accepted:
        reply["policies"][0]["fields"]["end_date"] = statement("缺证据")
    draft, = parse_application(body, model=lambda **_: json.dumps(reply), companies_file=COMPANIES_FILE)
    assert (draft.end_date.value == "2099-03-31") == accepted
