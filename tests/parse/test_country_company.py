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


@pytest.mark.parametrize("name", COMPANY_COUNTRIES)
def test_country_list_covers_company_table_countries(name):
    canonical = {"刚果布": "刚果（布）", "刚果金": "刚果（金）"}.get(name, name)
    reply = fixed_reply(name, [{"country": _field(name, name)}])
    draft, = _parse(name, reply, HEADER + f"{name}\t\t0001\t占位公司\n")
    assert draft.field_results["country"].value == canonical
    assert draft.company.value == "占位公司"


@pytest.mark.parametrize(("name", "code"), LEGACY_CODES)
def test_original_country_codes_resolve_exactly(name, code):
    reply = fixed_reply(code, [{"country": _field(code, code)}])
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
    assert [d.company.value for d in _parse(body, reply, table, legacy=legacy)] == ["占位甲公司", "占位乙公司"]
    assert [d.company.value for d in _parse(body, reply, table + duplicate, legacy=legacy)] == [None, None]


@pytest.mark.parametrize("name", ["刚果", "Congo", "The Congo"])
def test_unspecified_congo_requires_country_and_company_confirmation(name):
    table = HEADER + "刚果（金）\t\t0001\t占位甲公司\n刚果（布）\t\t0002\t占位乙公司\n"
    reply = fixed_reply(name, [{"country": _field(name, name)}])
    draft, = _parse(name, reply, table)
    country = draft.field_results["country"]
    assert country.needs_confirmation
    assert "unknown_country" in country.issues
    assert draft.field_results["company"].needs_confirmation


@pytest.mark.parametrize(("name", "canonical"), [
    ("刚果金", "刚果（金）"),
    ("刚果民主共和国", "刚果（金）"),
    ("Democratic Republic of the Congo", "刚果（金）"),
    ("刚果布", "刚果（布）"),
    ("刚果共和国", "刚果（布）"),
    ("Republic of the Congo", "刚果（布）"),
])
def test_specific_congo_alias_resolves_to_canonical_country(name, canonical):
    reply = fixed_reply(name, [{"country": _field(name, name)}])
    draft, = _parse(name, reply)
    country = draft.field_results["country"]
    assert (country.value, country.needs_confirmation) == (canonical, False)


def test_company_table_skips_unspecified_congo_row():
    from pathlib import Path
    from qianshu.parse.companies import load_company_rules

    table = HEADER + "刚果\t\t0001\t占位甲公司\n贝宁\t\t0002\t占位乙公司\n"
    rules_dir = Path(__file__).resolve().parents[2] / "rules"
    rules = load_company_rules(rules_dir, Path("unused.tsv"), companies_text=table)
    assert rules.names == {"贝宁": ["占位乙公司"]}


@pytest.mark.parametrize("status", ["歧义", "冲突", "带条件", "缺证据"])
@pytest.mark.parametrize("name", ["country", "company_keyword", "company"])
def test_r10_does_not_fill_from_unverified_inputs(name, status):
    body = "赞比亚占位申请"
    reply = fixed_reply(body, [{"country": _field("赞比亚", "赞比亚")}])
    reply["policies"][0]["fields"][name] = statement(status)
    draft, = _parse(body, reply)
    assert draft.company.value is None
    assert draft.company.uncertainty is not None
    assert draft.field_results[name].statement.status == status


@pytest.mark.parametrize(("clues", "subject", "default", "expected", "rule"), [
    (["甲线"], None, True, "占位甲公司", "R10.keyword"),
    (["甲线"], "占位乙公司", True, None, None),
    (["甲线"], "占位甲公司", True, "占位甲公司", "R10.keyword"),
    (["其他"], None, True, None, None),
    (None, "占位默认公司", True, None, None),
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
    draft, = _parse(body, reply, table)
    assert draft.company.value == expected
    result = draft.field_results["company"]
    assert result.needs_confirmation is (expected is None)
    assert result.rule == rule


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
