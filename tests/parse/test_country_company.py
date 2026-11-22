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


def mention(role, text, block="body"):
    return {"role": role, "evidence": {"source_id": block, "text": text}}


@pytest.mark.parametrize("role", ["比较或背景", "排除对象"])
@pytest.mark.parametrize(("support", "pending"), [
    ("贝宁比赞", True), ("贝宁比赞比亚", True), ("赞比亚", True),
    ("贝宁比", False), ("贝宁", False),
])
def test_non_applicable_intersection_uses_text_even_with_wrong_coordinates(role, support, pending):
    body = "贝宁比赞比亚"
    reply = fixed_reply(body, [{"country": _field("贝宁", support)}])
    country = reply["policies"][0]["fields"]["country"]
    country["mentions"] = [mention("适用地", "贝宁"), mention(role, "赞比亚")]
    # 把自报坐标移出真实相交处，或移入真实分离处。
    start = 0 if pending else body.index("赞比亚")
    country["evidence"][0]["span"].update(start=start, end=start + 1)
    draft, = _parse(body, reply)
    for name in ("country", "company"):
        assert draft.field_results[name].needs_confirmation is pending


@pytest.mark.parametrize("same_source", [False, True])
def test_role_intersection_maps_block_offset_and_source(same_source):
    from qianshu.parse.protocol import SourceBlock

    body = "合成政策甲乙贝宁比赞比亚"
    reply = fixed_reply(body, [{"country": _field("贝宁", "贝宁比赞")}])
    country = reply["policies"][0]["fields"]["country"]
    country["mentions"] = [mention("适用地", "贝宁"), mention("比较或背景", "赞比亚", "excerpt")]
    block = SourceBlock("excerpt", "赞比亚", "body" if same_source else "other", body.index("赞比亚"))
    draft, = parse_application(body, model=lambda **_: json.dumps(reply), companies_text=COMPANIES,
                               additional_blocks=[block])
    for name in ("country", "company"):
        assert draft.field_results[name].needs_confirmation is same_source


def test_equal_offsets_in_distinct_sources_do_not_conflict():
    from qianshu.parse.protocol import SourceBlock

    body = "贝宁"
    reply = fixed_reply(body, [{"country": _field("贝宁", body)}])
    reply["policies"][0]["fields"]["country"]["mentions"].append(mention("比较或背景", "南非", "attachment"))
    draft, = parse_application(body, model=lambda **_: json.dumps(reply), companies_text=COMPANIES,
                               additional_blocks=[SourceBlock("attachment", "南非", "attachment")])
    assert draft.field_results["country"].value == body and not draft.field_results["country"].needs_confirmation
    assert draft.company.value == "占位乙公司"


@pytest.mark.parametrize(("support", "extra"), [
    ("贝宁", mention("比较或背景", "赞比亚")),  # 重复的角色引文
    ("赞比亚", mention("比较或背景", "参照")),  # 重复的国家支持引文
    ("贝宁", mention("比较或背景", "无此国")),
    ("贝宁", mention("适用地", "参照赞比亚")),
    ("贝宁", mention("排除对象", "BJ")),
    ("贝宁", mention("适用地", "参照赞比亚", "missing")),
])
def test_unresolved_or_contradictory_roles_need_confirmation(support, extra):
    body = "贝宁 BJ 参照赞比亚，又提赞比亚"
    reply = fixed_reply(body, [{"country": _field("贝宁", support)}])
    reply["policies"][0]["fields"]["country"]["mentions"] = [mention("适用地", "贝宁"), extra]
    draft, = _parse(body, reply)
    assert all(draft.field_results[name].needs_confirmation for name in ("country", "company"))


@pytest.mark.parametrize("conditional", ["country", "company"])
def test_complete_roles_cannot_override_conditional_statement(conditional):
    body = "贝宁，甲线，占位甲公司"
    reply = fixed_reply(body, [dict(country=_field("贝宁", "贝宁"), company=_field("占位甲公司", "占位甲公司"),
                                   company_keyword=_field(["甲线"], "甲线"))])
    reply["policies"][0]["fields"][conditional]["status"] = "带条件"
    draft, = _parse(body, reply, HEADER + "贝宁\t甲线\t01\t占位甲公司\n")
    result = draft.field_results[conditional]
    assert result.statement.status == "带条件" and result.needs_confirmation
    assert draft.field_results["country"].needs_confirmation is (conditional == "country")
    assert draft.field_results["company"].needs_confirmation


@pytest.mark.parametrize("missing", ["mentions", "role", "evidence", "source_id", "text", "empty", "empty_item", "mixed"])
def test_missing_role_evidence_changes_only_country_company_and_their_derivations(missing):
    from unittest.mock import Mock

    from qianshu.parse.package_levels import load_package_levels
    from tests.parse.test_names import COMPOSED, COMPANIES as TABLE, TODAY, application

    body, fields = application()
    first_end = len(body)
    body += "甲线。另一条适用贝宁。"
    fields["company_keyword"] = _field(["甲线"], "甲线")
    reply = fixed_reply(body, [fields, {"country": _field("贝宁", "贝宁")}],
                        policy_ranges=[(0, first_end + 3), (first_end + 3, len(body))])
    model = Mock(side_effect=lambda **_: json.dumps(reply))
    options = dict(model=model, companies_text=HEADER + "赞比亚\t甲线\t0001\t占位公司甲\n贝宁\t\t0002\t占位乙公司\n",
                   package_levels=load_package_levels(TABLE.with_name("package-levels.csv")), today=TODAY)
    complete, other = parse_application(body, **options)
    country = reply["policies"][0]["fields"]["country"]
    item, = country["mentions"]
    if missing == "mentions":
        del country["mentions"]
    elif missing == "empty":
        country["mentions"] = []
    elif missing == "empty_item":
        country["mentions"] = [{}]
    elif missing == "mixed":
        country["mentions"].append({})
    elif missing in item:
        del item[missing]
    else:
        del item["evidence"][missing]

    incomplete, other_after = parse_application(body, **options)
    assert model.call_count == 2
    assert other.field_results["country"].value == "贝宁" and not other.field_results["company"].needs_confirmation
    assert other.field_results == other_after.field_results
    before, after = complete.field_results, incomplete.field_results
    assert before.keys() == after.keys()
    for name in before.keys() - {"country", "company", "policy_name", "packages"}:
        assert before[name] == after[name]
    for name, value in [("country", "赞比亚"), ("company", "占位公司甲")]:
        assert before[name].value == value and not before[name].needs_confirmation
        assert after[name].needs_confirmation
    name = before["policy_name"]
    assert (name.value, name.needs_confirmation) == (COMPOSED, True)
    assert [q.text for q in name.evidence] == ["面向全体且用户标签留空", "合成加赠：充30天送30天", "赞比亚", "起日2099-01-10"]
    name = after["policy_name"]
    assert (name.value, name.evidence, name.needs_confirmation) == (None, (), True)
    packages = before["packages"]
    row, = packages.value
    assert (row.current_package, row.gift_package) == ("占位甲包", "占位甲包_copy")
    assert not packages.needs_confirmation and [q.text for q in packages.evidence] == ["占位甲包"]
    packages = after["packages"]
    assert (packages.value, packages.evidence, packages.needs_confirmation) == (None, before["packages"].evidence, True)


@pytest.mark.parametrize(("excluded", "pending"), [
    ("赤道几内亚", False), ("排除赤道几内亚", False), ("排除赤道几内亚和几内亚", True),
])
def test_role_lookup_counts_only_complete_country_names(excluded, pending):
    body = "适用几内亚；" + excluded
    reply = fixed_reply(body, [{"country": _field("几内亚", "适用几内亚")}])
    reply["policies"][0]["fields"]["country"]["mentions"].append(mention("排除对象", excluded))
    draft, = _parse(body, reply, HEADER + "几内亚\t\t0001\t占位公司\n")
    if pending:
        assert "适用地是否也被排除" in draft.company.uncertainty.reason
    for name, value in [("country", "几内亚"), ("company", "占位公司")]:
        result = draft.field_results[name]
        assert result.needs_confirmation is pending
        if not pending:
            assert result.value == value
