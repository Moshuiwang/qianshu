"""#70 从固定合成回复走解析、卡片和离线模板选择。"""

import csv
import json
from dataclasses import asdict

import pytest

from qianshu import parse
from qianshu.__main__ import _mail_summary
from qianshu.mingdao.create_order import Stop, select_package_rows
from qianshu.mingdao.offer_fields import assemble_offer_fields
from qianshu.parse.package_levels import load_package_levels
from qianshu.regression.business import grade
from tests.parse.test_benefits import CATALOG, COMPANIES, FACTS, TODAY, card_pages, card_text, field, scenario
from tests.protocol_fixtures import quote, statement

KINDS = ("赠送时长", "升级权益", "特价优惠")
NAMES = ("占位甲包", "占位乙包", "占位丙包")


def details():
    rows = []
    for idx, name in enumerate(NAMES, 1):
        base = dict(record_type="OFFER", company_id="0001", company_name="占位公司甲",
                    offer_id=str(idx), offer_name=name, offer_type="基本", is_synchronized="否",
                    parent_offer_id="", offer_level_type="DTT", offer_level=str(idx), status="在售",
                    sale_start_date="2090-01-01", sale_end_date="2100-01-01")
        rows.extend([base, dict(base, offer_id=f"c{idx}", offer_name=name + "_copy",
                                offer_type="附属", is_synchronized="是", parent_offer_id=str(idx))])
    return rows


def load(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=details()[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    return load_package_levels(path)


def example(kind, pairs, **fields):
    return scenario(kind, packages=field(
        [dict(current_package=current, gift_package=gift) for current, gift in pairs],
        "政策标题", *[f"当前{current}，受赠{gift or current}" for current, gift in pairs],
    ), **fields)


def parse_one(body, reply, levels):
    draft, = parse.parse_application(body, model=lambda **_: json.dumps(reply), companies_file=COMPANIES,
                                     benefit_catalog=CATALOG, package_levels=levels, today=TODAY)
    return draft


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("count", [1, 2])
def test_multiple_explicit_package_rows_keep_verified_pairs(tmp_path, kind, count):
    pairs = [(name, None if kind == "赠送时长" else NAMES[2]) for name in NAMES[1::-1]][:count]
    draft = parse_one(*example(kind, pairs), load(tmp_path / "levels.csv", details()))
    assert [(row.current_package, row.gift_package) for row in draft.packages.value] == [
        (current, (gift or current) + "_copy") for current, gift in pairs
    ]


@pytest.mark.parametrize("case", ["duplicate", "conflict", "target_missing", "no_title"])
def test_unresolved_row_blocks_all_rows(tmp_path, case):
    kind = "升级权益" if case == "target_missing" else "赠送时长"
    pairs = [(NAMES[0], NAMES[0]), (NAMES[1], None)]
    if case == "duplicate":
        pairs[1] = pairs[0]
    elif case == "conflict":
        pairs[1] = (NAMES[1], NAMES[2])
    body, reply = example(kind, pairs)
    reply["policies"][0]["has_title"] = case != "no_title"
    draft = parse_one(body, reply, load(tmp_path / "levels.csv", details()))
    assert draft.packages.value is None and draft.packages.uncertainty


@pytest.mark.parametrize("defect", ["missing", "duplicate", "parent", "subject", "sync", "level", "expired", "off_sale", "extra_child", "no_level"])
def test_copy_requires_unique_catalog_relation_for_every_row(tmp_path, defect):
    rows = details()
    child = rows[3]
    if defect == "missing":
        rows.remove(child)
    elif defect == "duplicate":
        rows.append(child.copy())
    elif defect == "extra_child":
        rows.append(dict(child, offer_id="other", offer_name="占位其他附属包"))
    elif defect == "no_level":
        for row in rows[2:4]:
            row.update(offer_level_type="", offer_level="")
    else:
        key, value = {"parent": ("parent_offer_id", "3"), "subject": ("company_id", "0002"),
                      "sync": ("is_synchronized", "否"), "level": ("offer_level", "3"),
                      "expired": ("sale_end_date", "2098-12-31"), "off_sale": ("status", "停售")}[defect]
        child[key] = value
    draft = parse_one(*example("特价优惠", [(name, name) for name in NAMES[:2]]), load(tmp_path / "levels.csv", rows))
    assert draft.packages.value is None and draft.packages.uncertainty
    if defect == "level":
        assert draft.field_results["packages"].issues == ("package_copy_pending",)
    with pytest.raises(ValueError, match="待确认"):
        assemble_offer_fields(draft, today=TODAY, facts=FACTS, catalog=CATALOG)


def test_named_package_can_share_platform_and_level(tmp_path):
    rows = details()
    for row in rows[2:4]:
        row["offer_level"] = rows[0]["offer_level"]
    draft = parse_one(*example("特价优惠", [(NAMES[0], NAMES[1])]), load(tmp_path / "levels.csv", rows))
    assert draft.packages.uncertainty is None
    current, = draft.packages.value
    assert (current.current_package, current.gift_package) == (NAMES[0], NAMES[1] + "_copy")


@pytest.mark.parametrize("defect", ["missing", "header", "summary", "encoding"])
def test_package_level_table_failure_requires_review(tmp_path, defect, caplog):
    path = tmp_path / "levels.csv"
    if defect == "header":
        path.write_text("bad\nprivate-value\n")
    elif defect == "encoding":
        path.write_bytes(b"\xff")
    elif defect == "summary":
        load(path, [dict(details()[0], record_type="TIER_COUNT")])
    draft = parse_one(*example("特价优惠", [(NAMES[0], NAMES[0])]), load_package_levels(path))
    assert draft.packages.uncertainty and draft.packages.value is None
    assert draft.benefit_type.value == "特价优惠"
    assert all(record.message.startswith("销售品级别表第 ") and record.message.endswith(" 行")
               for record in caplog.records)


@pytest.mark.parametrize("bad_quote", [False, True])
def test_tier_only_shows_candidates_without_selection(tmp_path, monkeypatch, bad_quote):
    levels = load(tmp_path / "levels.csv", details()[:2])
    body, reply = scenario("特价优惠", packages=field(None, "仅当前高档"))
    tiers = statement("确定", value=[dict(role="当前包", tier="高", platform="DTT")],
                      evidence=[quote(body, "错误摘录" if bad_quote else "仅当前高档")])
    reply["policies"][0]["fields"]["package_tiers"] = tiers
    draft = parse_one(body, reply, levels)
    assert draft.packages.uncertainty and draft.packages.value is None
    monkeypatch.setattr(parse, "load_package_levels", lambda: levels)
    json.dumps(draft.to_dict())
    text = card_text(body, reply)
    if bad_quote:
        assert not draft.package_tier_candidates and "节目包档位候选" not in text
    else:
        assert len(draft.package_tier_candidates) == 1
        assert draft.package_tier_candidates[0] in text
        assert "档位原文　仅当前高档" in text


def test_offline_subtable_matches_all_package_pairs(tmp_path):
    pairs = [(name, name) for name in NAMES[1::-1]]
    draft = parse_one(*example("特价优惠", pairs), load(tmp_path / "levels.csv", details()))
    assemble_offer_fields(draft, today=TODAY, facts=FACTS, catalog=CATALOG)
    packages = [asdict(row) for row in draft.packages.value]
    rows = [dict(current=name, gift=name + "_copy") for name in NAMES[:2]]
    spec = dict(children={"current": {}, "gift": {}}, current=dict(control="current", name_path=[]),
                gift=dict(control="gift", name_path=[]))
    out = select_package_rows(rows, spec, packages)
    assert [[cell["value"] for cell in row] for row in out] == [[name, name + "_copy"] for name, _ in pairs]
    for invalid in [rows[:1], [*rows, rows[1]]]:
        with pytest.raises(Stop):
            select_package_rows(invalid, spec, packages)


def test_package_row_scoring_uses_frozen_denominator(tmp_path):
    levels = load(tmp_path / "levels.csv", details())
    mark = dict(inferable=True, auto_allowed=True)
    counts = {}
    for kind in KINDS:
        body, reply = example(kind, [(NAMES[0], NAMES[0])])
        draft = parse_one(body, reply, levels)
        gold = {"value": [dict(current_package=NAMES[0], gift_package=NAMES[0] + "_copy")]}
        counts[kind] = [grade("packages", entry, mark, draft, body) for entry in (gold, {})]
    assert counts == {kind: ["correct", "unknown"] for kind in KINDS}


def test_two_policies_share_one_snapshot_and_keep_their_own_rows(tmp_path, monkeypatch):
    from tests.protocol_fixtures import fixed_reply

    levels = load(tmp_path / "levels.csv", details())
    calls = []

    def snapshot():
        calls.append(1)
        return levels

    monkeypatch.setattr(parse, "load_package_levels", snapshot)
    texts = [f"政策{index}：{name}，赞比亚，赠送时长，2099-01-10" for index, name in enumerate(NAMES[:2])]
    body = "\n".join(texts)
    declarations = [dict(country=field("赞比亚", text), benefit_type=field("赠送时长", text),
                         start_date=field("2099-01-10", text),
                         packages=field([dict(current_package=name, gift_package=None)], text))
                    for name, text in zip(NAMES, texts)]
    reply = fixed_reply(body, declarations, policy_ranges=[(body.index(text), body.index(text) + len(text))
                                                           for text in texts])
    drafts = parse.parse_application(body, model=lambda **_: json.dumps(reply), companies_file=COMPANIES)
    assert calls == [1]
    assert [[row.current_package for row in draft.packages.value] for draft in drafts] == [[NAMES[0]], [NAMES[1]]]


@pytest.mark.parametrize("missing", ["none", "package_tiers", "null", "bad_shape", "value", "status",
                                     "evidence", "role", "tier", "platform"])
def test_tier_optional_parts_only_affect_tier(tmp_path, missing):
    body, reply = example("赠送时长", [(NAMES[0], None)])
    fields = reply["policies"][0]["fields"]
    item = dict(role="当前包", tier="高", platform="DTT")
    tier = dict(status="确定", value=[item], evidence=[quote(body, "政策标题")])
    fields["package_tiers"] = tier
    if missing == "package_tiers":
        del fields[missing]
    elif missing in {"null", "bad_shape"}:
        fields["package_tiers"] = None if missing == "null" else [42]
    elif missing in item:
        del item[missing]
    elif missing in tier:
        del tier[missing]
    draft = parse_one(body, reply, load(tmp_path / "levels.csv", details()))
    assert draft.packages.uncertainty is None and draft.packages.value[0].current_package == NAMES[0]
    if missing == "package_tiers":
        assert draft.tier_field is None
    elif missing in {"none", "platform"}:
        assert draft.package_tier_candidates and draft.tier_field.uncertainty is None
    else:
        assert draft.tier_field.uncertainty and not draft.package_tier_candidates


@pytest.mark.parametrize("key", ["role", "tier"])
def test_unknown_tier_enum_only_requires_tier_confirmation(tmp_path, key):
    body, reply = example("赠送时长", [(NAMES[0], None)])
    item = dict(role="当前包", tier="高", platform="DTT")
    item[key] = "枚举外占位值"
    reply["policies"][0]["fields"]["package_tiers"] = statement(
        "确定", value=[item], evidence=[quote(body, "政策标题")],
    )
    draft = parse_one(body, reply, load(tmp_path / "levels.csv", details()))
    assert draft.tier_field.uncertainty and not draft.package_tier_candidates
    assert draft.packages.uncertainty is None
    row, = draft.packages.value
    assert (row.current_package, row.gift_package) == (NAMES[0], NAMES[0] + "_copy")
    assert draft.benefit_type.value == "赠送时长"


@pytest.mark.parametrize("status", ["歧义", "冲突", "带条件", "缺证据", "确定"])
def test_unverified_tier_does_not_clear_verified_rows(tmp_path, monkeypatch, status):
    pairs = [(name, None) for name in NAMES[:2]]
    body, reply = example("赠送时长", pairs, cancel_on_downgrade=field("是", "降包取消"))
    levels = load(tmp_path / "levels.csv", details())
    monkeypatch.setattr(parse, "load_package_levels", lambda: levels)
    del reply["policies"][0]["fields"]["package_tiers"]
    assert "解析完成，待群卡片同意" in card_text(body, reply)
    reply["policies"][0]["fields"]["package_tiers"] = statement(
        status, value=[dict(role="当前包", tier="高")],
        evidence=[quote(body, "错误摘录" if status == "确定" else "政策标题")],
    )
    draft = parse_one(body, reply, levels)
    assert [row.current_package for row in draft.packages.value] == list(NAMES[:2])
    assert draft.packages.uncertainty is None and draft.tier_field.uncertainty
    pages = card_pages(body, reply)
    text = "\n".join(element.get("text", {}).get("content", "") for page in pages for element in page.elements)
    assert "政策 1 · 处理状态\n解析完成，待群卡片同意" in text
    summary, = _mail_summary(pages, approved=True)
    assert any("通过校验 1 条，需人工核对 0 条" in element.get("text", {}).get("content", "")
               for element in summary.elements)
    assert "节目包档位 · 待确认" in text
    assert ("缺证据" if status == "确定" else status) in text


def test_tier_card_masks_registered_fragments_in_catalog_and_model_text(tmp_path, monkeypatch):
    secret = "合成ABCD保护串"
    rows = details()[:2]
    for row in rows:
        row["offer_name"] = row["offer_name"].replace(NAMES[0], "独立ABCD包")
    body, reply = scenario("特价优惠", packages=field(None, "仅当前高档", secret))
    tier = statement("确定", value=[dict(role="当前包", tier="高")], evidence=[quote(body, "仅当前高档")])
    reply["policies"][0]["fields"]["package_tiers"] = dict(tier, explanation="候选ABCD说明")
    levels = load(tmp_path / "levels.csv", rows)
    monkeypatch.setattr(parse, "load_package_levels", lambda: levels)
    text = card_text(body, reply, sensitive_values=[secret])
    assert "节目包档位候选" in text and "独立[已脱敏]包" in text
    assert "ABCD" not in text


@pytest.mark.parametrize("same_name", [True, False])
def test_historical_offer_does_not_collide_with_active_name_or_level(tmp_path, same_name):
    rows = details()
    rows.append(dict(rows[0], status="停售", offer_id="old", offer_name=NAMES[0] if same_name else "历史占位包"))
    draft = parse_one(*example("赠送时长", [(NAMES[0], None)]), load(tmp_path / "levels.csv", rows))
    assert draft.packages.uncertainty is None and draft.packages.value[0].gift_package == NAMES[0] + "_copy"


def test_default_run_date_excludes_expired_packages(tmp_path):
    rows = [dict(row, sale_start_date="2000-01-01", sale_end_date="2001-01-01") for row in details()]
    levels = load(tmp_path / "levels.csv", rows)
    body, reply = scenario("赠送时长", start_date=field("2000-01-10", "开始日期 2000-01-10"))
    draft, = parse.parse_application(body, model=lambda **_: json.dumps(reply), companies_file=COMPANIES,
                                     benefit_catalog=CATALOG, package_levels=levels)
    assert draft.field_results["packages"].issues == ("package_lookup_pending",)
    assert draft.packages.uncertainty and draft.packages.value is None


def test_two_catalog_candidates_share_card_fragment_check(tmp_path, monkeypatch):
    rows = details()[:4]
    for row in rows[2:]:
        row["offer_name"] = row["offer_name"].replace(NAMES[1], "占位AB")
    body, reply = scenario("特价优惠", packages=field(None, "仅当前高档"))
    reply["policies"][0]["fields"]["package_tiers"] = dict(
        status="确定", value=[dict(role="当前包", tier="高")], evidence=[quote(body, "仅当前高档")],
    )
    levels = load(tmp_path / "levels.csv", rows)
    monkeypatch.setattr(parse, "load_package_levels", lambda: levels)
    text = card_text(body, reply, sensitive_values=["xAB当前y"])
    assert "占位AB" not in text
    assert "占位[已脱敏]" in text and "[已脱敏]包 · 高档" in text


@pytest.mark.parametrize("role,expected", [("当前包", NAMES[:2]), ("受赠目标", NAMES[1:2])])
def test_only_gift_tier_requires_a_synchronized_copy(tmp_path, role, expected):
    levels = load(tmp_path / "levels.csv", [details()[0], *details()[2:4]])
    body, reply = scenario("升级权益", packages=field(None, "仅列出档位"))
    reply["policies"][0]["fields"]["package_tiers"] = statement(
        "确定", value=[dict(role=role, tier="低")], evidence=[quote(body, "仅列出档位")],
    )
    draft = parse_one(body, reply, levels)
    assert tuple(item.split(" · ")[-1] for item in draft.package_tier_candidates) == expected
    assert draft.packages.value is None


@pytest.mark.parametrize("missing", ["company", "start_date", "benefit_type"])
@pytest.mark.parametrize("certain", [True, False])
def test_unresolved_inputs_keep_only_evidenced_names_without_lookup(monkeypatch, missing, certain):
    current, gift, expiry = NAMES
    body, reply = example("升级权益", [(current, gift)],
                          after_offer=field("指定节目包", "到期转包"),
                          after_offer_package=field(expiry, f"到期转入{expiry}"))
    fields = reply["policies"][0]["fields"]
    fields[missing] = statement("歧义")
    fields["package_tiers"] = statement("确定", value=[dict(role="当前包", tier="高")],
                                         evidence=[quote(body, current)])
    rows = [dict(current_package=current, gift_package=gift),
            dict(current_package="合成无引文包", gift_package=None)]
    evidence = [quote(body, f"当前{current}，受赠{gift}")]
    if certain:
        fields["packages"] = statement("确定", value=rows, evidence=evidence)
    else:
        fields["packages"] = statement("歧义", candidates=[
            dict(value=rows, evidence=evidence),
            dict(value=[dict(current_package=expiry, gift_package=None)], evidence=[quote(body, "无效摘录")]),
        ])

    monkeypatch.setattr(parse, "load_package_levels", lambda: pytest.fail("前提未定时不得加载销售品表"))
    draft = parse_one(body, reply, None)
    result = draft.field_results["packages"]
    assert not result.has_value and result.value is None and result.source is None
    assert result.statement.model_dump(mode="json", exclude_unset=True) == fields["packages"]
    assert not draft.package_tier_candidates and draft.packages.value is None
    candidate, = draft.packages.uncertainty.candidates
    assert candidate.value == [{"节目包行": "1", "当前包": current, "活动期受赠包": gift, "状态": "未查表、不可建单"}]
    assert [item.text for item in candidate.evidence] == [evidence[0]["text"]]
    assert "未查表、不可建单" in card_text(body, reply)
    with pytest.raises(ValueError):
        assemble_offer_fields(draft, today=TODAY, facts=FACTS, catalog=CATALOG)
