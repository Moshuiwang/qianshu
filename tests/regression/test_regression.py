import csv
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from qianshu import parse
from qianshu.draft import DraftField, PackageRow, Uncertainty
from qianshu.regression import freeze as freeze_module, runner
from qianshu.regression.__main__ import main
from qianshu.regression.compare import FIELD_LABELS, compare_field, normalize
from qianshu.regression.corpus import CANDIDATES, FIELDS, PAIRS, SEED, make_plan, select
from qianshu.regression.runner import align
from qianshu.regression.storage import PrivateFiles, RegressionError, digest, json_bytes

RULES = Path(__file__).resolve().parents[2] / "rules"
CORPUS = Path("archive")
FIRST = "1.占位甲包用户（赞比亚）"
SECOND = "2.占位乙包用户（莫桑比克）"
BATCH = f"{FIRST}\n占位正文甲\n{SECOND}\n占位正文乙\n"
PLAN = [".private/archive", "--out", ".private/plan"]
FREEZE = [
    "--corpus", ".private/archive", "--plan", ".private/plan/plan.json",
    "--reviews", ".private/review.json", "--companies", ".private/companies.tsv",
    "--out", ".private/frozen",
]


def git(*args):
    return subprocess.run(["git", *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def put_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def get_json(path):
    return json.loads(path.read_bytes())


def tsv(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        csv.writer(stream, delimiter="\t", lineterminator="\n").writerows([header, *rows])


def catalogs(root, candidates):
    tsv(root / CANDIDATES, ["政策 rowId", "原稿 token", "国家", "类型", "原稿形态"], [
        (row, token, country, f"{policy} × {benefit}", "文字")
        for row, token, country, policy, benefit in candidates
    ])
    tsv(root / PAIRS, ["政策 rowId", "原稿 token", "国家", "政策类型", "优惠类型"], candidates)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    git("init", "-q")
    Path(".gitignore").write_text(".private/\n")
    private = tmp_path / ".private"
    private.mkdir(mode=0o700)
    shutil.copytree(RULES, "rules")
    return private


@pytest.fixture
def corpus(repo):
    root = repo / CORPUS
    candidates = [
        ("policy-a", "doc-batch", "synthetic-country-a", "定向政策", "赠送时长"),
        ("policy-b", "doc-batch", "synthetic-country-b", "定向政策", "赠送时长"),
        ("policy-c", "doc-single", "synthetic-country-a", "公开政策", "升级权益"),
    ]
    catalogs(root, candidates)
    put_json(root / FIELDS, {
        "fields": [{"name": label, "type": kind, "id": name} for name, (label, kind) in FIELD_LABELS.items()],
    })
    expected = {
        "policy-a": ("占位金标甲", 31, "2099-01-02", "gift", "related-company"),
        "policy-b": ("占位金标乙", 30, "2099-01-02", "gift", "related-company"),
        "policy-c": ("占位金标丙", 60, "2099-01-03", "upgrade", "related-company"),
    }
    for row, (name, days, begin, benefit, company) in expected.items():
        put_json(root / f"policy-corpus/rows/{row}.json", {
            "complete": True, "data": {
                "rowId": row,
                "user_scope": name, "recharge_days": str(days), "start_date": begin,
                "benefit_type": json.dumps([{"key": benefit, "value": {"gift": "赠送时长", "upgrade": "升级权益"}[benefit]}]),
                "company": json.dumps([{"sid": company}]), "user_tags": 0, "packages": 2,
                "effective_multiplier": 1,
            },
        })
        links = {
            "company": [{"rowId": company}],
            "packages": [
                {"current": [{"name": "占位包乙"}], "gift": [{"name": "占位包丙"}]},
                {"current": [{"name": "占位包甲"}], "gift": []},
            ],
        }
        for field, rows in links.items():
            put_json(root / f"policy-corpus/relations/{row}.{field}.json", {
                "complete": True, "expected_on_detail": len(rows), "total": len(rows), "rows": rows,
            })
    put_json(root / "wiki-corpus/docs/doc-batch.json", {
        "complete": True, "token": "doc-batch", "policy_row_ids": ["policy-a", "policy-b"], "plain_text": BATCH,
    })
    body = "占位单条原稿\n"
    put_json(root / "wiki-corpus/docs/doc-single.json", {
        "complete": True, "token": "doc-single", "policy_row_ids": ["policy-c"], "plain_text": body,
    })
    materials = "policy-corpus/attachments/material.txt"
    (root / materials).parent.mkdir()
    (root / materials).write_text("占位补充资料")
    checked = set(FIELD_LABELS) & {
        "user_scope", "recharge_days", "start_date", "benefit_type", "company", "user_tags", "packages",
        "effective_multiplier",
    }
    spans = [[0, BATCH.index(SECOND)], [BATCH.index(SECOND), len(BATCH)], [0, len(body)]]
    review = {
        "schema": 1, "reviewer_role": "合成资料复核者",
        "documents": {
            "doc-batch": {"package_style": "name_only"},
            "doc-single": {"package_style": "tier_only"},
        },
        "mapping": {
            "options": {"benefit_type": {"gift": "赠送时长", "upgrade": "升级权益"}},
            "company_names": {"related-company": "占位公司甲"},
            "relation_columns": {"current_package": "current", "gift_package": "gift"},
        },
        "reviews": {
            item[0]: {
                "span": span, "basis": "核对占位录入记录和原稿", "materials": [materials],
                "fields": {name: True if name in checked else "not_applicable" for name in FIELD_LABELS},
            }
            for item, span in zip(candidates, spans, strict=True)
        },
    }
    put_json(repo / "review.json", review)
    (repo / "companies.tsv").write_text("赞比亚\t占位公司甲\n莫桑比克\t占位公司乙\n")
    return root


@pytest.fixture
def frozen(repo, corpus, capsys):
    assert main(["plan", "--corpus", *PLAN, "--all"]) == 0
    assert main(["freeze", *FREEZE]) == 0
    output = capsys.readouterr().out
    checksum = output.splitlines()[-1]
    assert len(checksum) == 64
    return checksum


def run_args(fingerprint, output=".private/run"):
    return ["run", "--frozen", ".private/frozen", "--expect-sha256", fingerprint, "--out", output]


def test_stratifies_by_largest_remainder_and_replaces_only_within_layer(repo):
    rows = [
        (f"row{i}", f"doc{i}", "synthetic-country-a", "定向政策", "赠送时长" if i < 5 else "升级权益")
        for i in range(7)
    ]
    catalogs(repo / CORPUS, rows)
    with PrivateFiles(repo.parent).open() as files:
        plan = make_plan(files, CORPUS, 4)
        catalogs(repo / CORPUS, list(reversed(rows)))
        reordered = make_plan(files, CORPUS, 4)
    assert plan["strata"] == reordered["strata"]
    bonus, timing = plan["strata"]
    assert (bonus["quota"], timing["quota"]) == (1, 3)
    assert [row["rank_hash"] for row in timing["candidates"]] == sorted(
        digest(f"{SEED}\0{row[0]}".encode()) for row in rows[:5]
    )
    reviews = {row[0]: {} for row in rows}
    rejected_id = timing["candidates"][0]["row_id"]
    reviews[rejected_id] = {"exclude": "insufficient_evidence"}
    selected, rejected = select(plan, reviews)
    assert [row["row_id"] for row in selected[1:]] == [row["row_id"] for row in timing["candidates"][1:4]]
    assert rejected == [{"row_id": rejected_id, "rank": 1, "reason": "insufficient_evidence"}]
    assert selected[-1]["selection"] == "replacement"
    reviews[timing["candidates"][-2]["row_id"]] = {"exclude": "historical_rule"}
    reviews[timing["candidates"][-1]["row_id"]] = {"exclude": "historical_rule"}
    with pytest.raises(RegressionError, match="缺 1 条"):
        select(plan, reviews)


def test_freeze_records_reviewed_configuration_and_fingerprints(repo, corpus, frozen, capsys):
    destination = repo / "frozen"
    manifest = get_json(destination / "manifest.json")
    assert manifest["count"] == 3
    assert manifest["documents"] == 2
    assert manifest["mode"] == "all"
    assert "created_utc" not in manifest
    assert get_json(destination / "audit.json")["created_utc"].endswith("+00:00")
    assert all(digest((destination / name).read_bytes()) == checksum for name, checksum in manifest["artifact_sha256"].items())
    assert manifest["source_sha256"]["archive/policy-corpus/attachments/material.txt"] == digest(
        (corpus / "policy-corpus/attachments/material.txt").read_bytes()
    )
    pairs = [json.loads(line) for line in (destination / "pairs.jsonl").read_bytes().splitlines()]
    pair_a = next(pair for pair in pairs if pair["row_id"] == "policy-a")
    assert pair_a["configuration"]["company"] == {"value": "占位公司甲"}
    assert pair_a["configuration"]["recharge_days"] == {"value": 31}
    assert pair_a["configuration"]["user_tags"] == {"value": []}
    assert pair_a["configuration"]["packages"] == {"value": [
        {"current_package": "占位包乙", "gift_package": "占位包丙"},
        {"current_package": "占位包甲", "gift_package": None},
    ]}
    key = pair_a["id"]
    fixed = get_json(destination / "denominators.json")
    assert key in fixed["fields"]["user_scope"]["ids"]
    assert fixed["fields"]["gift_days"]["excluded"][key] == "not_applicable"
    assert fixed["whole_policy"] == []
    assert main(["verify", "--frozen", ".private/frozen", "--sources"]) == 0
    assert capsys.readouterr().out.strip() == frozen
    assert main(["freeze", *FREEZE]) == 1
    assert digest((destination / "manifest.json").read_bytes()) == frozen


def test_independent_freezes_have_identical_content_fingerprints(repo, frozen, monkeypatch, capsys):
    class LaterDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return super().now(tz) + timedelta(days=1)

    monkeypatch.setattr(freeze_module, "datetime", LaterDateTime)
    assert main(["freeze", *FREEZE[:-1], ".private/recreated"]) == 0
    assert capsys.readouterr().out.strip() == frozen
    first, second = repo / "frozen", repo / "recreated"
    for name in (*freeze_module.ARTIFACTS, "manifest.json"):
        assert (first / name).read_bytes() == (second / name).read_bytes()
    assert get_json(first / "audit.json") != get_json(second / "audit.json")


def test_loads_previous_manifest_fingerprint_without_package_style(repo, frozen, capsys):
    snapshot = repo / "frozen"
    manifest = get_json(snapshot / "manifest.json")
    manifest["schema"] = 1
    manifest.update(get_json(snapshot / "audit.json"))
    docs = snapshot / "documents.jsonl"
    records = [json.loads(line) for line in docs.read_bytes().splitlines()]
    for record in records:
        del record["package_style"]
    docs.write_bytes(b"".join(json_bytes(record) for record in records))
    manifest["artifact_sha256"][docs.name] = digest(docs.read_bytes())
    old_manifest = json_bytes(manifest)
    (snapshot / "manifest.json").write_bytes(old_manifest)
    fingerprint = digest(old_manifest)
    assert main(["verify", "--frozen", ".private/frozen"]) == 0
    assert capsys.readouterr().out.strip() == fingerprint
    assert main(run_args(fingerprint), model=lambda **kw: "[]") == 0
    report = get_json(repo / "run" / "public-report.json")
    assert report["package_writing_by_document"]["unreviewed"] == 2
    assert report["package_writing_by_document"]["neither_recognized"] == 0


@pytest.mark.parametrize("name", ["samples.jsonl", "pairs.jsonl", "denominators.json", "companies.tsv", "documents.jsonl"])
def test_tampered_artifact_stops_run_before_model(repo, frozen, name, capsys):
    path = repo / "frozen" / name
    path.write_bytes(path.read_bytes() + b" ")
    calls = []
    assert main(run_args(frozen), model=lambda **kwargs: calls.append(kwargs)) == 1
    assert "指纹校验失败" in capsys.readouterr().err
    assert calls == []
    assert not (repo / "run").exists()


def test_can_check_changed_source_separately_from_replay(repo, corpus, frozen, capsys):
    name = corpus / "policy-corpus/rows/policy-a.json"
    name.write_bytes(name.read_bytes() + b" ")
    assert main(["verify", "--frozen", ".private/frozen"]) == 0
    assert capsys.readouterr().out.strip() == frozen
    assert main(["verify", "--frozen", ".private/frozen", "--sources"]) == 1
    assert "来源指纹已变化" in capsys.readouterr().err


@pytest.mark.parametrize("failure", ["incomplete", "unknown-option", "overlap"])
def test_freeze_rejects_incomplete_or_conflicting_gold(repo, corpus, failure):
    assert main(["plan", "--corpus", *PLAN, "--all"]) == 0
    if failure == "incomplete":
        path = corpus / "policy-corpus/relations/policy-a.packages.json"
        link = get_json(path)
        link["complete"] = False
        put_json(path, link)
    else:
        path = repo / "review.json"
        review = get_json(path)
        if failure == "unknown-option":
            review["mapping"]["options"]["benefit_type"].pop("gift")
        else:
            review["reviews"]["policy-a"]["span"] = [0, len(BATCH)]
        put_json(path, review)
    assert main(["freeze", *FREEZE]) == 1
    assert not (repo / "frozen").exists()


@pytest.mark.parametrize("company_cell", [
    [{"sid": "synthetic-company-a"}],
    [{"rowId": "synthetic-company-a"}],
    [{"rowid": "synthetic-company-a"}],
    ["synthetic-company-a"],
], ids=["sid", "rowId", "rowid", "string-array"])
def test_equal_relation_counts_do_not_accept_a_different_company(repo, corpus, company_cell, capsys):
    path = corpus / "policy-corpus/rows/policy-a.json"
    row = get_json(path)
    row["data"]["company"] = json.dumps(company_cell)
    put_json(path, row)
    path = repo / "review.json"
    review = get_json(path)
    review["mapping"]["company_names"]["synthetic-company-a"] = "占位公司乙"
    put_json(path, review)

    assert main(["plan", "--corpus", *PLAN, "--all"]) == 0
    assert main(["freeze", *FREEZE]) == 1
    assert "关联明细与主记录 ID 不一致" in capsys.readouterr().err
    assert not (repo / "frozen").exists()


def test_freeze_accepts_company_uuid_with_different_case(repo, corpus, capsys):
    company_id = "abcdef01-0000-4000-8000-000000000000"
    path = corpus / "policy-corpus/rows/policy-a.json"
    row = get_json(path)
    row["data"]["company"] = json.dumps([{"sid": company_id.upper()}])
    put_json(path, row)
    path = corpus / "policy-corpus/relations/policy-a.company.json"
    link = get_json(path)
    link["rows"][0]["rowId"] = company_id
    put_json(path, link)
    path = repo / "review.json"
    review = get_json(path)
    review["mapping"]["company_names"][company_id] = "占位公司丙"
    put_json(path, review)

    assert main(["plan", "--corpus", *PLAN, "--all"]) == 0
    assert main(["freeze", *FREEZE]) == 0, capsys.readouterr().err
    pairs = [json.loads(line) for line in (repo / "frozen/pairs.jsonl").read_bytes().splitlines()]
    pair = next(pair for pair in pairs if pair["row_id"] == "policy-a")
    assert pair["configuration"]["company"] == {"value": "占位公司丙"}


def test_freeze_normalizes_non_uuid_relation_ids(repo, corpus, capsys):
    path = corpus / "policy-corpus/rows/policy-a.json"
    row = get_json(path)
    row["data"]["company"] = json.dumps([{"sid": "RELATED-COMPANY", "rowid": "related-company"}])
    row["data"]["packages"] = json.dumps(["PACKAGE-A", "package-b"])
    put_json(path, row)
    path = corpus / "policy-corpus/relations/policy-a.company.json"
    company = get_json(path)
    company["rows"][0]["rowId"] = "Related-Company"
    put_json(path, company)
    path = corpus / "policy-corpus/relations/policy-a.packages.json"
    packages = get_json(path)
    packages["rows"][0]["rowId"] = "package-a"
    packages["rows"][1]["rowId"] = "PACKAGE-B"
    put_json(path, packages)
    path = repo / "review.json"
    review = get_json(path)
    review["mapping"]["company_names"]["RELATED-COMPANY"] = "占位公司甲"
    put_json(path, review)

    assert main(["plan", "--corpus", *PLAN, "--all"]) == 0
    assert main(["freeze", *FREEZE]) == 0, capsys.readouterr().err
    pairs = [json.loads(line) for line in (repo / "frozen/pairs.jsonl").read_bytes().splitlines()]
    pair = next(pair for pair in pairs if pair["row_id"] == "policy-a")
    assert pair["configuration"]["company"] == {"value": "占位公司甲"}
    assert len(pair["configuration"]["packages"]["value"]) == 2


def test_conflicting_company_id_case_marks_only_affected_company_uncertain(repo, corpus, capsys):
    path = corpus / "policy-corpus/rows/policy-a.json"
    row = get_json(path)
    row["data"]["company"] = 1
    put_json(path, row)
    path = corpus / "policy-corpus/relations/policy-a.company.json"
    company = get_json(path)
    company["rows"][0]["rowId"] = "conflict-company"
    put_json(path, company)
    path = repo / "review.json"
    review = get_json(path)
    review["mapping"]["company_names"].update({"CONFLICT-COMPANY": "占位公司乙", "Conflict-Company": "占位公司丙"})
    review["reviews"]["policy-a"]["fields"] = {
        name: True if name == "company" else "not_applicable" for name in FIELD_LABELS
    }
    put_json(path, review)

    assert main(["plan", "--corpus", *PLAN, "--all"]) == 0
    assert main(["freeze", *FREEZE]) == 0, capsys.readouterr().err
    pairs = {pair["row_id"]: pair for pair in map(json.loads, (repo / "frozen/pairs.jsonl").read_bytes().splitlines())}
    assert pairs["policy-a"]["configuration"]["company"] == {"exclude": "source_anomaly"}
    assert pairs["policy-b"]["configuration"]["company"] == {"value": "占位公司甲"}
    assert pairs["policy-c"]["configuration"]["company"] == {"value": "占位公司甲"}

    fingerprint = digest((repo / "frozen/manifest.json").read_bytes())
    assert main(run_args(fingerprint), model=lambda **kw: "[]") == 0
    report = get_json(repo / "run/public-report.json")
    assert report["samples"] == 3
    assert report["by_field"]["company"]["denominator"] == 2
    assert report["excluded_by_field"]["company"]["source_anomaly"] == 1


def test_counts_each_package_style_once_per_original(repo, corpus):
    path = repo / "review.json"
    review = get_json(path)
    candidates = [
        (row, doc, "synthetic-country-a", "定向政策", "赠送时长")
        for row, doc in (("policy-a", "doc-batch"), ("policy-b", "doc-batch"),
                         ("policy-c", "doc-single"), ("policy-d", "doc-d"), ("policy-e", "doc-e"))
    ]
    candidates[2] = ("policy-c", "doc-single", "synthetic-country-a", "公开政策", "升级权益")
    catalogs(corpus, candidates)
    for row, token, style, body in (
        ("policy-d", "doc-d", "both", "占位稿丙：占位档位、占位节目包"),
        ("policy-e", "doc-e", "neither_recognized", "占位稿丁：详见补充材料"),
    ):
        gold = get_json(corpus / "policy-corpus/rows/policy-a.json")
        gold["data"]["rowId"] = row
        put_json(corpus / f"policy-corpus/rows/{row}.json", gold)
        for field in ("company", "packages"):
            directory = corpus / "policy-corpus/relations"
            shutil.copyfile(directory / f"policy-a.{field}.json", directory / f"{row}.{field}.json")
        put_json(corpus / f"wiki-corpus/docs/{token}.json", {
            "complete": True, "token": token, "policy_row_ids": [row], "plain_text": body,
        })
        review["reviews"][row] = {**review["reviews"]["policy-a"], "span": [0, len(body)]}
        review["documents"][token] = {"package_style": style}
    put_json(path, review)
    assert main(["plan", "--corpus", *PLAN, "--all"]) == 0
    assert main(["freeze", *FREEZE]) == 0
    fingerprint = digest((repo / "frozen" / "manifest.json").read_bytes())
    assert main(run_args(fingerprint), model=lambda **kw: "[]") == 0

    report = get_json(repo / "run" / "public-report.json")
    assert report["samples"] == 5
    assert report["package_writing_by_document"] == {
        "tier_only": 1, "name_only": 1, "both": 1, "neither_recognized": 1, "unreviewed": 0,
    }
    markdown = (repo / "run" / "public-report.md").read_text()
    assert all(f"{label} 1 篇" in markdown for label in freeze_module.PACKAGE_STYLES.values())
    assert "占位稿丁" not in markdown


@pytest.mark.parametrize(("field", "expected", "actual", "status"), [
    ("policy_name", "占位名称", "占位名称附加", "error"),
    ("company", "占位公司", "占位公司 ", "error"),
    ("policy_type", "定向政策", "公开政策", "error"),
    ("start_date", "2099-01-02", "2099-01-03", "error"),
    ("start_date", "2099-01-02", "2099-1-2", "anomaly"),
    ("gift_days", 30, 30, "match"),
    ("gift_days", 30, "30", "anomaly"),
    ("gift_days", 1, True, "anomaly"),
    ("effective_multiplier", "1.00", 1, "match"),
    ("effective_multiplier", "1.00", "1", "anomaly"),
    ("user_tags", ["占位甲", "占位乙"], ["占位乙", "占位甲"], "match"),
    ("user_tags", [], [], "match"),
    ("user_tags", [], None, "missing"),
    ("packages", [{"current_package": "占位甲", "gift_package": None}], [PackageRow(current_package="占位甲", gift_package="")], "error"),
    ("packages", [{"current_package": "占位甲", "gift_package": None}], [
        PackageRow(current_package="占位甲"), PackageRow(current_package="占位甲"),
    ], "anomaly"),
])
def test_strict_field_comparison(field, expected, actual, status):
    assert compare_field(field, normalize(field, expected), DraftField(value=actual)) == status


def test_packages_ignore_row_order_and_uncertainty_is_never_agreement():
    first = PackageRow(current_package="占位甲")
    second = PackageRow(current_package="占位乙", gift_package="占位丙")
    gold = normalize("packages", [first, second])
    assert compare_field("packages", gold, DraftField(value=[second, first])) == "match"
    uncertain = DraftField(value=[first, second], uncertainty=Uncertainty(reason="待确认"))
    assert compare_field("packages", gold, uncertain) == "uncertain"


def test_batch_matching_is_by_source_spans_and_rejects_ambiguity():
    pairs = [{"id": "a", "span": [0, 10]}, {"id": "b", "span": [20, 30]}]
    document = {"body": "", "other_spans": [[35, 40]], "complete": True}
    a, b = SimpleNamespace(source_span=(0, 5)), SimpleNamespace(source_span=(21, 29))
    other, extra = SimpleNamespace(source_span=(36, 38)), SimpleNamespace(source_span=(41, 45))
    outcomes, notes = align([b, a, other, extra], pairs, document)
    assert outcomes == {"a": ("matched", a), "b": ("matched", b)}
    assert [note["status"] for note in notes] == ["unselected", "extra"]

    outcomes, notes = align([a, a, SimpleNamespace(source_span=None)], pairs, document)
    assert outcomes == {"a": ("ambiguous", None), "b": ("unmatched", None)}
    assert sorted(note["status"] for note in notes) == ["ambiguous", "ambiguous", "unplaced"]


@pytest.mark.parametrize(("quotes", "expected"), [
    (["仅限占位用户甲"], "matched"),
    (["仅限占位用户乙"], "ambiguous"),
    (["仅限占位用户甲", "仅限占位用户乙"], "ambiguous"),
    ([], "ambiguous"),
])
def test_broad_real_draft_needs_evidence_in_the_only_sampled_paragraph(quotes, expected):
    first = "仅限占位用户甲，充值 10 天。\n"
    body = first + "第二次仅限占位用户乙，充值 20 天。\n"
    drafts = parse.parse_application(
        body, rules_dir=RULES, companies_text="",
        model=lambda **kw: json.dumps([{"user_scope": {"value": "占位用户", "evidence": quotes}}]),
    )
    assert drafts[0].source_span == (0, len(body))
    outcomes, diagnostics = align(
        drafts, [{"id": "policy-a", "span": [0, len(first)]}],
        {"body": body, "other_spans": [], "complete": False},
    )
    assert outcomes["policy-a"] == (expected, drafts[0] if expected == "matched" else None)
    assert [item["status"] for item in diagnostics] == ([] if expected == "matched" else ["ambiguous"])


@pytest.mark.parametrize(("first_duration", "later_duration", "expected"), [
    ("充值 10 天。", "充值 20 天。", "ambiguous"),
    ("充值 20 天。", "充值 20 天。", "ambiguous"),
    ("充值 20 天。充值 20 天。", "充值 10 天。", "matched"),
], ids=["outside", "cross-paragraph", "inside"])
def test_broad_real_draft_checks_repeated_evidence_in_sampled_paragraph(first_duration, later_duration, expected):
    first = "占位甲申请为赠送时长，" + first_duration + "\n"
    body = first + f"占位乙申请，{later_duration}\n占位丙申请，{later_duration}\n"
    drafts = parse.parse_application(
        body, rules_dir=RULES, companies_text="",
        model=lambda **kw: json.dumps([{
            "benefit_type": {"value": "赠送时长", "evidence": ["赠送时长"]},
            "recharge_days": {"value": 20, "evidence": ["充值 20 天"]},
        }]),
    )
    assert drafts[0].source_span == (0, len(body))
    assert drafts[0].recharge_days.value == 20
    assert drafts[0].recharge_days.evidence[0].start is None
    outcomes, diagnostics = align(
        drafts, [{"id": "policy-a", "span": [0, len(first)]}],
        {"body": body, "other_spans": [], "complete": False},
    )
    assert outcomes["policy-a"] == (expected, drafts[0] if expected == "matched" else None)
    assert [item["status"] for item in diagnostics] == ([] if expected == "matched" else ["ambiguous"])


def test_cli_uses_real_parser_once_per_frozen_original_and_never_shrinks_denominators(repo, frozen, capsys, caplog):
    calls = []

    def model(*, system, user):
        body = json.loads(user)["application"]
        calls.append(body)
        if body != BATCH:
            logging.warning("占位单条原稿")
            raise RuntimeError("占位接口回包")
        return json.dumps([
            {"company": {"evidence": [SECOND]}, "user_scope": {"value": "占位金标乙", "evidence": ["占位正文乙"]}},
            {"company": {"evidence": [FIRST]}, "user_scope": {"value": "占位不同值", "evidence": ["占位正文甲"]}},
        ], ensure_ascii=False)

    assert main(run_args(frozen), model=model) == 0
    assert sorted(calls) == sorted([BATCH, "占位单条原稿\n"])
    output = repo / "run"
    report = get_json(output / "public-report.json")
    assert report["freeze_sha256"] == frozen
    assert report["documents"] == {"parsed": 1, "model_failed": 1, "parse_failed": 0}
    assert report["sample_alignment"]["matched"] == 2
    assert report["by_field"]["user_scope"] == {
        "denominator": 3, "match": 1, "error": 1, "uncertain": 0,
        "missing": 0, "anomaly": 1, "match_percent": 33.33,
    }
    assert report["by_field"]["recharge_days"]["uncertain"] == 2
    assert report["by_benefit_type"]["赠送时长"]["user_scope"]["match_percent"] == 50
    assert report["by_policy_type"]["公开政策"]["user_scope"]["anomaly"] == 1
    assert report["by_country_group"]["1"]["user_scope"]["denominator"] == 2
    details = get_json(output / "mismatches.json")
    assert {item["row_id"] for item in details["samples"]} == {"policy-a", "policy-b", "policy-c"}
    private = (output / "mismatches.json").read_text()
    assert "占位不同值" in private
    public = (output / "public-report.json").read_text() + (output / "public-report.md").read_text()
    capture = capsys.readouterr()
    for secret in ("占位金标甲", "占位正文甲", "policy-a", "doc-batch", "synthetic-country-a", "占位接口回包"):
        assert secret not in public + capture.out + capture.err + caplog.text
    assert output.stat().st_mode & 0o777 == 0o700
    assert all(file.stat().st_mode & 0o777 == 0o600 for file in output.iterdir())


def test_no_live_flag_never_constructs_a_model(repo, frozen, monkeypatch):
    def fail(*args):
        pytest.fail("unexpected model construction")
    monkeypatch.setattr("qianshu.regression.__main__.configured_model", fail)
    assert main(run_args(frozen)) == 1
    assert not (repo / "run").exists()


def test_frozen_baseline_runs_with_an_expanded_parser_and_reports_new_field_counts(repo, frozen, monkeypatch):
    def model(*, system, user):
        if json.loads(user)["application"] != BATCH:
            return "[]"
        data = [
            {"company": {"evidence": [FIRST]}},
            {"company": {"evidence": [SECOND]}},
        ]
        if "synthetic_offer" in parse._FIELD_NAMES:
            data[0]["synthetic_offer"] = {"value": "占位正文甲", "evidence": ["占位正文甲"]}
        return json.dumps(data)

    assert main(run_args(frozen), model=model) == 0
    original = get_json(repo / "run" / "public-report.json")
    denominators = repo / "frozen" / "denominators.json"
    original_denominators = denominators.read_bytes()

    @dataclass(kw_only=True)
    class ExtendedDraft(parse.ParsedPolicyDraft):
        synthetic_offer: DraftField[str]

    monkeypatch.setattr(parse, "ParsedPolicyDraft", ExtendedDraft)
    monkeypatch.setattr(parse, "_FIELD_NAMES", parse._FIELD_NAMES | {"synthetic_offer"})
    monkeypatch.setattr(runner, "PolicyDraft", ExtendedDraft)
    monkeypatch.setitem(FIELD_LABELS, "synthetic_offer", ("占位新增字段", "Text"))

    assert main(run_args(frozen, ".private/expanded"), model=model) == 0
    expanded = get_json(repo / "expanded" / "public-report.json")
    for key in ("freeze_sha256", "by_field", "by_benefit_type", "by_policy_type", "by_country_group",
                "excluded_by_field", "whole_policy"):
        assert expanded[key] == original[key]
    assert denominators.read_bytes() == original_denominators
    assert expanded["new_field_counts"] == {
        "synthetic_offer": {"determined": 1, "uncertain": 1, "missing": 0, "unaligned": 1},
    }
    assert "| 占位新增字段 | 1 | 1 | 0 | 1 |" in (repo / "expanded" / "public-report.md").read_text()


def test_private_root_can_hold_both_existing_archives(repo, corpus):
    for name in ("policy-corpus", "wiki-corpus"):
        (corpus / name).rename(repo / name)
    assert main(["plan", "--corpus", ".private", "--out", ".private/plan", "--size", "2"]) == 0
    plan = get_json(repo / "plan" / "plan.json")
    assert plan["mode"] == "sample"
    assert [layer["quota"] for layer in plan["strata"]] == [1, 1]


def test_malformed_reply_and_duplicate_source_scopes_are_counted(repo, frozen):
    def model(*, system, user):
        if json.loads(user)["application"] != BATCH:
            return "占位原始回包，无法解析"
        draft = {"company": {"evidence": [FIRST]}}
        return json.dumps([draft, draft], ensure_ascii=False)

    assert main(run_args(frozen), model=model) == 0
    report = get_json(repo / "run" / "public-report.json")
    assert report["documents"] == {"parsed": 1, "model_failed": 0, "parse_failed": 1}
    assert report["sample_alignment"]["ambiguous"] == 1
    assert report["sample_alignment"]["unmatched"] == 1
    assert report["by_field"]["user_scope"]["denominator"] == 3
    assert report["by_field"]["user_scope"]["missing"] == 1
    assert report["by_field"]["user_scope"]["anomaly"] == 2
    assert "占位原始回包" not in (repo / "run" / "mismatches.json").read_text()


@pytest.mark.parametrize("attack", ["public", "escape", "ignored-outside-private", "symlink", "root-symlink", "tracked", "unignored", "permissions"])
def test_output_boundary_rejects_before_parsing(repo, frozen, attack, capsys):
    output = ".private/run"
    if attack == "public":
        output = "visible"
    elif attack == "escape":
        output = ".private/../visible"
    elif attack == "ignored-outside-private":
        Path(".gitignore").write_text(".private/\nignored/\n")
        output = "ignored/run"
    elif attack == "symlink":
        (repo / "link").symlink_to(repo.parent, target_is_directory=True)
        output = ".private/link/visible"
    elif attack == "root-symlink":
        repo.rename(repo.parent / "moved")
        repo.symlink_to(repo.parent / "moved", target_is_directory=True)
    elif attack == "tracked":
        (repo / "placeholder").touch()
        git("add", "-f", ".private/placeholder")
    elif attack == "unignored":
        Path(".gitignore").write_text("")
    elif attack == "permissions":
        repo.chmod(0o755)

    calls = []
    assert main(run_args(frozen, output), model=lambda **kw: calls.append(kw)) == 1
    assert calls == []
    assert not (repo.parent / "visible").exists()
    assert not (repo.parent / "ignored" / "run").exists()
    assert not (repo / "run").exists()
    assert capsys.readouterr().err


def test_source_hardlink_is_refused(repo, corpus, capsys):
    path = corpus / CANDIDATES
    os.link(path, repo.parent / "fixture-copy")
    assert main(["plan", "--corpus", *PLAN, "--all"]) == 1
    assert "硬链接" in capsys.readouterr().err
    assert not (repo / "plan").exists()


def test_nested_git_index_blocks_recreating_a_tracked_report_directory(repo, frozen, capsys):
    nested = repo / "nested"
    git("init", "-q", str(nested))
    (nested / "run").mkdir()
    (nested / "run/mismatches.json").write_text("{}")
    git("-C", str(nested), "add", "run/mismatches.json")
    shutil.rmtree(nested / "run")
    assert git("-C", str(nested), "ls-files").stdout.strip() == b"run/mismatches.json"
    calls = []
    assert main(run_args(frozen, ".private/nested/run"), model=lambda **kw: calls.append(kw)) == 1
    assert not calls
    assert not (nested / "run").exists()
    assert "当前 Git 工作树" in capsys.readouterr().err


def test_output_moved_during_model_run_is_not_written(repo, frozen):
    moved = repo.parent / "moved-output"

    def model(**kwargs):
        if not moved.exists():
            (repo / "run").rename(moved)
            (repo / "run").mkdir(mode=0o700)
        return "[]"

    assert main(run_args(frozen), model=model) == 1
    assert moved.is_dir()
    assert list(moved.iterdir()) == []
    assert list((repo / "run").iterdir()) == []
