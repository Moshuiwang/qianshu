import json

import pytest

from qianshu.parse import parse_application
from tests.protocol_fixtures import fixed_reply, quote, statement


HEADER = "国家\t关键词\t公司内部编号\t公司显示名\n"
COMPANIES = HEADER + "赞比亚\t\t0001\t占位甲公司\n贝宁\t\t0002\t占位乙公司\n"
COMPANY_COUNTRIES = (
    "中非", "乍得", "冈比亚", "几内亚", "几内亚比绍", "刚果布", "刚果金", "利比里亚", "加纳",
    "加蓬", "南非", "博茨瓦纳", "卢旺达", "厄立特里亚", "喀麦隆", "坦桑尼亚", "埃塞俄比亚",
    "塞内加尔", "塞拉利昂", "尼日利亚", "布基纳法索", "布隆迪", "斯威士兰", "津巴布韦",
    "乌干达", "科特迪瓦", "索马里", "纳米比亚", "肯尼亚", "莫桑比克", "莱索托", "贝宁",
    "赞比亚", "赤道几内亚", "马拉维", "马里", "马达加斯加",
)
LEGACY_CODES = (
    ("赞比亚", "ZM"), ("肯尼亚", "KE"), ("乌干达", "UG"), ("坦桑尼亚", "TZ"), ("卢旺达", "RW"),
    ("布隆迪", "BI"), ("尼日利亚", "NG"), ("加纳", "GH"), ("南非", "ZA"), ("莫桑比克", "MZ"),
    ("马拉维", "MW"), ("刚果（金）", "CD"), ("刚果（布）", "CG"), ("喀麦隆", "CM"),
    ("科特迪瓦", "CI"), ("塞内加尔", "SN"), ("马达加斯加", "MG"),
)


def _field(value, *texts):
    return {"value": value, "evidence": list(texts)}


def _parse(body, reply, companies=COMPANIES, *, legacy=False):
    return parse_application(body, model=lambda **_: json.dumps(reply), companies_text=companies, legacy_companies=legacy)


def _declare_country_roles(reply, body, policy_index, context=()):
    policy = reply["policies"][policy_index]
    country = policy["fields"]["country"]
    country["mentions"] = [{"role": "适用地", "evidence": item} for item in country["evidence"]]
    for role, text in context:
        item = quote(body, text)
        country["mentions"].append({"role": role, "evidence": item})
    return country


@pytest.mark.parametrize("name", COMPANY_COUNTRIES)
def test_country_list_covers_company_table_countries(name):
    canonical = {"刚果布": "刚果（布）", "刚果金": "刚果（金）"}.get(name, name)
    reply = fixed_reply(name, [{"country": _field(name, name)}])
    _declare_country_roles(reply, name, 0)
    draft, = _parse(name, reply, HEADER + f"{name}\t\t0001\t占位公司\n")
    assert draft.field_results["country"].value == canonical
    assert draft.company.value == "占位公司"


@pytest.mark.parametrize(("name", "code"), LEGACY_CODES)
def test_original_country_codes_resolve_exactly(name, code):
    reply = fixed_reply(code, [{"country": _field(code, code)}])
    _declare_country_roles(reply, code, 0)
    draft, = _parse(code, reply)
    result = draft.field_results["country"]
    assert (result.value, result.needs_confirmation) == (name, False)


@pytest.mark.parametrize("legacy", [False, True])
def test_congo_old_names_normalize_before_uniqueness_checks(legacy):
    if legacy:
        table = "刚果金\t占位甲公司\n刚果布\t占位乙公司\n"
        duplicate = "刚果（金）\t占位重复公司\n"
    else:
        table = HEADER + "刚果金\t\t0001\t占位甲公司\n刚果布\t\t0002\t占位乙公司\n"
        duplicate = "刚果（金）\t\t0003\t占位重复公司\n"
    body = "刚果(金)；刚果(布)"
    split = body.index("刚果(布)")
    reply = fixed_reply(body, [{"country": _field("刚果民主共和国", "刚果(金)")},
                              {"country": _field("刚果共和国", "刚果(布)")}],
                        policy_ranges=[(0, split), (split, len(body))])
    for index in range(2):
        _declare_country_roles(reply, body, index)
    assert [d.company.value for d in _parse(body, reply, table, legacy=legacy)] == ["占位甲公司", "占位乙公司"]
    assert [d.company.value for d in _parse(body, reply, table + duplicate, legacy=legacy)] == [None, None]


@pytest.mark.parametrize(("body", "context"), [
    pytest.param("占位说明：由 GM 审核，编号 NA", (), id="01-code-collision"),
    pytest.param("占位名单中非活跃用户", (), id="02-country-substring"),
    pytest.param("用户范围：\n赞比亚过往用户", (("比较或背景", "赞比亚"),), id="03-continuation"),
    pytest.param("赞比亚仅作参考。\n占位申请", (("比较或背景", "赞比亚"),), id="07-ordinary-first-line"),
    pytest.param("占位申请。\n用户范围：\n贝宁旧名单", (("比较或背景", "贝宁"),), id="09-rephrased-continuation"),
])
def test_context_does_not_supply_a_country_or_new_policy(body, context):
    reply = fixed_reply(body, [{"user_scope": _field("占位范围", body)}])
    _declare_country_roles(reply, body, 0, context)
    draft, = _parse(body, reply)
    assert draft.source_span == (0, len(body))
    assert draft.user_scope.value == "占位范围"
    assert draft.field_results["country"].statement.status == "原文未写"
    assert draft.company.value is None
    assert draft.company.uncertainty is not None


@pytest.mark.parametrize(("first", "second"), [
    pytest.param("赞比亚：占位包甲，赠10天。\n", "贝宁：占位包乙，赠5天。", id="04-country-and-days"),
    pytest.param("1. 赞比亚，赠10天\n备注：\n", "2. 贝宁，赠5天", id="06-empty-field-and-title"),
    pytest.param("1. 占位包甲\n国家：赞比亚\n赠10天\n", "2. 占位包乙\n国家：贝宁\n赠5天", id="08-country-field-and-boundary"),
])
def test_country_fields_preserve_policy_boundaries_and_durations(first, second):
    body = first + second
    ranges = [(0, len(first)), (len(first), len(body))]
    reply = fixed_reply(body, [{"country": _field("赞比亚", "赞比亚"), "gift_days": _field(10, "赠10天")},
                              {"country": _field("贝宁", "贝宁"), "gift_days": _field(5, "赠5天")}],
                        policy_ranges=ranges)
    _declare_country_roles(reply, body, 0)
    second_country = _declare_country_roles(reply, body, 1)
    drafts = _parse(body, reply)
    assert [draft.source_span for draft in drafts] == ranges
    assert [draft.gift_days.value for draft in drafts] == [10, 5]
    assert [draft.company.value for draft in drafts] == ["占位甲公司", "占位乙公司"]

    reply["policies"][0]["fields"]["country"]["mentions"].extend(second_country["mentions"])
    drafts = _parse(body, reply)
    assert [draft.source_span for draft in drafts] == ranges
    assert [draft.gift_days.value for draft in drafts] == [10, 5]
    assert [draft.company.value for draft in drafts] == [None, "占位乙公司"]
    assert drafts[0].field_results["country"].issues == ("invalid_evidence",)


@pytest.mark.parametrize("applicable", [False, True])
def test_preamble_period_preserves_declared_date_status(applicable):
    preamble = "适用期限到 2099-03-31。\n"
    body = preamble + "贝宁占位申请"
    reply = fixed_reply(body, [{"country": _field("贝宁", "贝宁"),
                                "end_date": _field("2099-03-31", "2099-03-31")}],
                        policy_ranges=[(len(preamble), len(body))])
    if not applicable:
        reply["policies"][0]["fields"]["end_date"] = statement("歧义")
    _declare_country_roles(reply, body, 0)
    draft, = _parse(body, reply)
    assert draft.end_date.value == ("2099-03-31" if applicable else None)
    assert draft.company.value == "占位乙公司"


@pytest.mark.parametrize(("value", "names", "context", "expected"), [
    ("贝宁", ("BJ",), (("排除对象", "赞比亚"), ("比较或背景", "几内亚")), "占位乙公司"),
    ("贝宁", ("BJ", "Benin"), (), "占位乙公司"),
    ("贝宁", ("BJ", "赞比亚"), (), None),
    ("赞比亚", ("BJ",), (), None),
    ("贝宁", ("BJ",), (("排除对象", "Benin"),), None),
    ("贝宁", ("BJ",), (("比较或背景", "BJ"),), None),
    ("贝宁", ("BJ", "占位未收录地"), (), None),
    ("贝宁", ("BJ", "稿外引文"), (), None),
    ("贝宁", ("BJEND",), (), None),
    ("中非", ("名单中非活跃",), (), None),
])
def test_country_role_evidence_and_unique_lookup(value, names, context, expected):
    body = "；".join(("赞比亚", "几内亚", "BJ", "Benin", "占位未收录地", "BJEND", "名单中非活跃"))
    reply = fixed_reply(body, [{"country": _field(value, *names)}])
    _declare_country_roles(reply, body, 0, context)
    draft, = _parse(body, reply)
    result = draft.field_results["country"]
    assert draft.company.value == expected
    assert result.needs_confirmation is (expected is None)
    assert result.statement.status == "确定"


@pytest.mark.parametrize("status", ["歧义", "冲突", "带条件", "缺证据"])
@pytest.mark.parametrize("name", ["country", "company_keyword", "company"])
def test_r10_does_not_fill_from_unverified_inputs(name, status):
    body = "赞比亚占位申请"
    reply = fixed_reply(body, [{"country": _field("赞比亚", "赞比亚")}])
    reply["policies"][0]["fields"][name] = statement(status)
    country = reply["policies"][0]["fields"]["country"]
    country["mentions"] = [{"role": "适用地", "evidence": item} for item in country["evidence"]]
    draft, = _parse(body, reply)
    assert draft.company.value is None
    assert draft.company.uncertainty is not None
    assert draft.field_results[name].statement.status == status


@pytest.mark.parametrize(("start", "block", "accepted"), [(1, "body", True), (2, "body", False), (0, "other", False)])
def test_country_mention_quote_must_locate_uniquely(start, block, accepted):
    body = "BJ  BJ。赞比亚"
    reply = fixed_reply(body, [{"country": _field("贝宁", "BJ")}, {"country": _field("赞比亚", "赞比亚")}],
                        policy_ranges=[(0, 7), (7, len(body))])
    country = _declare_country_roles(reply, body, 0)
    country["mentions"][0]["evidence"] = {"span": {"start": start, "end": start + 2, "source_id": block}, "text": "BJ"}
    first, second = _parse(body, reply)
    assert first.company.value == ("占位乙公司" if accepted else None)
    assert second.company.value == "占位甲公司"


@pytest.mark.parametrize(("clues", "subject", "default", "expected", "rule"), [
    (["甲线"], None, True, "占位甲公司", "R10.keyword"),
    (["其他"], None, True, "占位默认公司", "R10.default_company_no_match"),
    (None, None, True, "占位默认公司", "R10.default_company"),
    (["甲线", "乙线"], None, True, None, None),
    (["其他"], None, False, None, None),
    (["其他"], "占位甲公司", True, None, None),
])
def test_r10_keyword_default_and_subject_conflict(clues, subject, default, expected, rule):
    table = HEADER + "贝宁\t甲线\t01\t占位甲公司\n贝宁\t乙线\t02\t占位乙公司\n"
    if default:
        table += "贝宁\t\t03\t占位默认公司\n"
    fields = {"country": _field("贝宁", "贝宁")}
    if clues:
        fields["company_keyword"] = _field(clues, *clues)
    if subject:
        fields["company"] = _field(subject, subject)
    body = " ".join(["贝宁", *(clues or []), subject or ""])
    reply = fixed_reply(body, [fields])
    _declare_country_roles(reply, body, 0)
    draft, = _parse(body, reply, table)
    assert draft.company.value == expected
    result = draft.field_results["company"]
    assert result.needs_confirmation is (expected is None)
    assert result.rule == rule


def test_company_regression_counts_exclude_unknown():
    from qianshu.regression.business import BusinessScore, grade

    body = "赞比亚"
    reply = fixed_reply(body, [{"country": _field(body, body)}])
    draft, = _parse(body, reply)
    score = BusinessScore({"samples": {}, "denominators": {"fields": {}}}, 1, {})
    mark = {"inferable": True, "auto_allowed": True, "default": draft.company.value}
    for entry in ({"value": draft.company.value}, {"value": "占位乙公司"}, {}):
        category = grade("company", entry, mark, draft, body)
        score.field_counts["company"][category] += 1
    report = score.report(0)
    assert report["by_field"]["company"]["unknown"] == 1
    assert report["rates"]["false_determined"] == {"numerator": 1, "denominator": 2, "percent": 50.0}


def test_country_aliases_resolve_to_a_single_standard_name():
    from pathlib import Path
    from qianshu.parse.scope import country_matches, read_countries

    text = (Path(__file__).resolve().parents[2] / "rules" / "countries.txt").read_text(encoding="utf-8")
    countries = read_countries(text)
    for line in text.splitlines():
        name, *aliases = line.split("\t")
        for term in (name, *aliases):
            assert country_matches([term], countries, exact=True) == {name}


@pytest.mark.parametrize("name", ["company_keyword", "company"])
def test_invalid_unwritten_statement_cannot_select_default(name):
    body = "贝宁"
    reply = fixed_reply(body, [{"country": _field("贝宁", body)}])
    reply["policies"][0]["fields"][name] = statement("原文未写", evidence=[quote(body, body)])
    draft, = _parse(body, reply)
    assert draft.field_results["company"].needs_confirmation
    assert draft.company.value is None


@pytest.mark.parametrize("mentions", [
    [{"role": "适用地"}], [{"role": "适用地", "evidence": None}],
    [{"evidence": None}], [None], {"role": "适用地"}, [{"role": "未知"}],
])
def test_incomplete_country_role_only_affects_its_country_and_company(mentions):
    first = "贝宁，赠5天。"
    body = first + "赞比亚，赠10天。"
    reply = fixed_reply(body, [
        {"country": _field("贝宁", "贝宁"), "gift_days": _field(5, "赠5天")},
        {"country": _field("赞比亚", "赞比亚"), "gift_days": _field(10, "赠10天")},
    ], policy_ranges=[(0, len(first)), (len(first), len(body))])
    original_first, original_second = _parse(body, reply)
    reply["policies"][0]["fields"]["country"]["mentions"] = mentions
    changed, second = _parse(body, reply)
    for name, result in changed.field_results.items():
        if name in ("country", "company"):
            assert result.needs_confirmation
        else:
            assert result == original_first.field_results[name]
    assert second.field_results == original_second.field_results
    assert second.company.value == "占位甲公司"


@pytest.mark.parametrize("field_quotes", [("贝宁", "本条仅适用于贝宁"), ("本条仅适用于贝宁",)])
def test_applicable_country_role_with_longer_field_evidence(field_quotes):
    body = "本条仅适用于贝宁。"
    reply = fixed_reply(body, [{"country": _field("贝宁", *field_quotes)}])
    reply["policies"][0]["fields"]["country"]["mentions"] = [{"role": "适用地", "evidence": quote(body, "贝宁")}]
    draft, = _parse(body, reply)
    country = draft.field_results["country"]
    assert country.value == "贝宁" and country.evidence_verified
    assert draft.company.value == "占位乙公司" and draft.field_results["company"].evidence_verified
