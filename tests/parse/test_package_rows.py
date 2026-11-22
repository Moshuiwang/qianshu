"""#70 从固定合成回复走解析、卡片和离线模板选择。"""

import csv
import json
from dataclasses import asdict

import pytest

from qianshu import parse
from qianshu.mingdao.create_order import Stop, select_package_rows
from qianshu.mingdao.offer_fields import assemble_offer_fields
from qianshu.parse.package_levels import load_package_levels
from qianshu.regression.business import grade
from tests.parse.test_benefits import CATALOG, COMPANIES, FACTS, TODAY, card_text, field, scenario
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


def example(kind, pairs):
    return scenario(kind, packages=field(
        [dict(current_package=current, gift_package=gift) for current, gift in pairs],
        "政策标题", *[f"当前{current}，受赠{gift or current}" for current, gift in pairs],
    ))


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
    with pytest.raises(ValueError, match="待确认"):
        assemble_offer_fields(draft, today=TODAY, facts=FACTS, catalog=CATALOG)


@pytest.mark.parametrize("defect", ["missing", "header", "summary", "duplicate_level", "encoding"])
def test_package_level_table_failure_requires_review(tmp_path, defect, caplog):
    path = tmp_path / "levels.csv"
    if defect == "header":
        path.write_text("bad\nprivate-value\n")
    elif defect == "encoding":
        path.write_bytes(b"\xff")
    elif defect == "summary":
        load(path, [dict(details()[0], record_type="TIER_COUNT")])
    elif defect == "duplicate_level":
        rows = details()
        rows[2]["offer_level"] = rows[0]["offer_level"]
        load(path, rows)
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
