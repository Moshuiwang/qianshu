import csv
import json
import logging
import os
import shutil
import subprocess
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from qianshu import parse
from qianshu.draft import DraftField, PackageRow, Uncertainty
from qianshu.regression import freeze as freeze_module, runner
from qianshu.regression.__main__ import main
from qianshu.regression.compare import FIELD_LABELS, compare_field, normalize
from qianshu.regression.corpus import CANDIDATES, DOCUMENTS, FIELDS, PAIRS, SEED, body_hashes, make_plan, select
from qianshu.regression.runner import align
from qianshu.regression.storage import PrivateFiles, RegressionError, digest, json_bytes
from tests.protocol_fixtures import fixed_reply, quote, serialized_empty

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


def no_policies(*, system, user):
    return serialized_empty()


def shaped_reply(body, rows, **options):
    return json.dumps(fixed_reply(body, rows, **options), ensure_ascii=False)


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
    (repo / "companies.tsv").write_text(
        "国家\t关键词\t公司内部编号\t公司显示名\n赞比亚\t\t0001\t占位公司甲\n莫桑比克\t\t0002\t占位公司乙\n",
        encoding="utf-8-sig",
    )
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
    del reviews[timing["candidates"][-1]["row_id"]]
    with pytest.raises(RegressionError, match="抽样顺序中仍有未审定候选，当前层尚需 1 条"):
        select(plan, reviews)


def test_holdout_plan_reapportions_quotas_after_both_document_hash_matches(repo):
    tokens = ["doc-exact"] * 2 + ["doc-normalized"] + [f"doc-{i}" for i in range(5)] + ["doc-normalized", "doc-8"]
    rows = [
        (f"row{i}", token, "synthetic-country-a", "定向政策", "赠送时长" if i < 6 else "升级权益")
        for i, token in enumerate(tokens)
    ]
    root = repo / CORPUS
    catalogs(root, rows)
    exact_body = "占位原稿甲"
    normalized_body = "占位　Ａ\n"
    for token in set(tokens):
        body = {"doc-exact": exact_body, "doc-normalized": normalized_body}.get(token, f"占位正文 {token}")
        put_json(root / DOCUMENTS / f"{token}.json", {
            "complete": True, "token": token, "plain_text": body,
            "policy_row_ids": [row_id for row_id, row_token, *_ in rows if row_token == token],
        })
    exact, _ = body_hashes(exact_body)
    _, normalized = body_hashes("占位A")
    assert normalized == body_hashes(normalized_body)[1]
    unrelated = digest(b"synthetic nonmatching hash")
    exclusions = {"exam": (digest(b"exam"), [(exact, unrelated)]),
                  "packages": (digest(b"packages"), [(unrelated, normalized)])}
    with PrivateFiles(repo.parent).open() as files:
        exam = make_plan(files, CORPUS, 3)
        plan = make_plan(files, CORPUS, 3, dataset="blind", exclusions=exclusions)
    assert "dataset" not in exam and "excluded_sets_sha256" not in exam
    assert [layer["quota"] for layer in exam["strata"]] == [1, 2]
    assert plan["dataset"] == "blind" and plan["candidate_count"] == 6
    assert plan["excluded_sets_sha256"] == {"exam": digest(b"exam"), "packages": digest(b"packages")}
    assert [layer["quota"] for layer in plan["strata"]] == [2, 1]
    for original, filtered in zip(exam["strata"], plan["strata"], strict=True):
        assert filtered["candidates"] == [
            row for row in original["candidates"] if row["token"] not in {"doc-exact", "doc-normalized"}
        ]


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


def test_sample_freeze_records_reviewed_stratum_shortfall(repo, corpus, capsys):
    assert main(["plan", "--corpus", *PLAN, "--size", "3"]) == 0
    review_path = repo / "review.json"
    review = get_json(review_path)
    del review["reviews"]["policy-b"]
    put_json(review_path, review)
    assert main(["freeze", *FREEZE]) == 1
    assert "抽样顺序中仍有未审定候选" in capsys.readouterr().err
    assert not (repo / "frozen").exists()

    review["reviews"]["policy-b"] = {"exclude": "insufficient_evidence"}
    put_json(review_path, review)
    assert main(["freeze", *FREEZE]) == 0
    fingerprint = capsys.readouterr().out.strip()
    strata = [
        {"policy_type": "公开政策", "benefit_type": "升级权益", "quota": 1, "included_count": 1, "shortfall": 0},
        {"policy_type": "定向政策", "benefit_type": "赠送时长", "quota": 2, "included_count": 1, "shortfall": 1},
    ]
    manifest_path = repo / "frozen" / "manifest.json"
    manifest = get_json(manifest_path)
    assert manifest["target"] == 3
    assert manifest["count"] == 2
    assert manifest["strata"] == [{**strata[0], "candidate_count": 1}, {**strata[1], "candidate_count": 2}]
    assert digest(manifest_path.read_bytes()) == fingerprint
    assert main(["freeze", *FREEZE[:-1], ".private/recreated"]) == 0
    assert capsys.readouterr().out.strip() == fingerprint
    assert main(["verify", "--frozen", ".private/frozen", "--sources"]) == 0
    assert capsys.readouterr().out.strip() == fingerprint

    assert main(run_args(fingerprint), model=no_policies) == 0
    report = get_json(repo / "run" / "public-report.json")
    assert report["strata"] == strata
    assert sum(layer["included_count"] for layer in report["strata"]) == report["samples"] == 2
    assert report["by_field"]["user_scope"]["denominator"] == 2
    markdown = (repo / "run" / "public-report.md").read_text()
    assert "| 公开政策 | 升级权益 | 1 | 1 | 0 |" in markdown
    assert "| 定向政策 | 赠送时长 | 2 | 1 | 1 |" in markdown
    capsys.readouterr()

    manifest["strata"].pop()
    manifest_path.write_bytes(json_bytes(manifest))
    verify_args = ["verify", "--frozen", ".private/frozen"]
    for args in (verify_args, [*verify_args, "--sources"]):
        assert main(args) == 1
        result = capsys.readouterr()
        assert not result.out
        assert "分层合计" in result.err

    # 将目标配额改成剩余层的配额，单独验证纳入数合计仍会阻止报告。
    manifest["target"] = manifest["strata"][0]["quota"]
    manifest_path.write_bytes(json_bytes(manifest))
    calls = []
    tampered_fingerprint = digest(manifest_path.read_bytes())
    assert main(run_args(tampered_fingerprint, ".private/tampered-run"), model=lambda **kw: calls.append(kw)) == 1
    assert "分层合计" in capsys.readouterr().err
    assert calls == []
    assert not (repo / "tampered-run").exists()


@pytest.mark.parametrize("change", ["missing-layer", "candidate-count", "duplicate-layer"])
def test_verify_checks_all_plan_layers_even_without_included_samples(repo, corpus, capsys, change):
    assert main(["plan", "--corpus", *PLAN, "--size", "1"]) == 0
    assert main(["freeze", *FREEZE]) == 0
    capsys.readouterr()

    manifest_path = repo / "frozen" / "manifest.json"
    manifest = get_json(manifest_path)
    zero_quota = next(layer for layer in manifest["strata"] if layer["quota"] == 0)
    if change == "missing-layer":
        manifest["strata"].remove(zero_quota)
    elif change == "candidate-count":
        zero_quota["candidate_count"] += 1
    else:
        manifest["strata"].append(zero_quota)
    manifest_path.write_bytes(json_bytes(manifest))

    verify_args = ["verify", "--frozen", ".private/frozen"]
    for args in (verify_args, [*verify_args, "--sources"]):
        assert main(args) == 1
        result = capsys.readouterr()
        assert not result.out
        assert "分层" in result.err


def test_loads_previous_manifest_fingerprint_without_package_style(repo, frozen, capsys):
    snapshot = repo / "frozen"
    manifest = get_json(snapshot / "manifest.json")
    manifest["schema"] = 1
    manifest.update(get_json(snapshot / "audit.json"))
    for layer in manifest["strata"]:
        del layer["included_count"]
        del layer["shortfall"]
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
    assert main(run_args(fingerprint), model=no_policies) == 0
    report = get_json(repo / "run" / "public-report.json")
    assert [(layer["included_count"], layer["shortfall"]) for layer in report["strata"]] == [(1, 0), (2, 0)]
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
    assert main(run_args(fingerprint), model=no_policies) == 0
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
    assert main(run_args(fingerprint), model=no_policies) == 0

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
        model=lambda **kw: shaped_reply(body, [{"user_scope": {"value": "占位用户", "evidence": quotes}}]),
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
    reply = fixed_reply(body, [{
        "benefit_type": {"value": "赠送时长", "evidence": ["赠送时长"]},
        "recharge_days": {"value": 20, "evidence": ["充值 20 天"]},
    }])
    if first_duration == later_duration:
        extra = quote(body, "充值 20 天", occurrence=1)
        reply["policies"][0]["fields"]["recharge_days"]["evidence"].append(extra)
    drafts = parse.parse_application(body, rules_dir=RULES, companies_text="", model=lambda **_: json.dumps(reply))
    assert drafts[0].source_span == (0, len(body))
    assert drafts[0].recharge_days.value == 20
    assert drafts[0].recharge_days.evidence[0].start == body.index("充值 20 天")
    outcomes, diagnostics = align(
        drafts, [{"id": "policy-a", "span": [0, len(first)]}],
        {"body": body, "other_spans": [], "complete": False},
    )
    assert outcomes["policy-a"] == (expected, drafts[0] if expected == "matched" else None)
    assert [item["status"] for item in diagnostics] == ([] if expected == "matched" else ["ambiguous"])


@pytest.mark.parametrize("legacy", [False, True], ids=["four-columns", "legacy"])
@pytest.mark.parametrize(("country", "grade"), [("ZM", "correct"), ("XX", "false_determined")])
def test_cli_uses_real_parser_once_per_frozen_original_and_never_shrinks_denominators(
    repo, corpus, capsys, caplog, legacy, country, grade,
):
    catalogs(corpus, [
        ("policy-a", "doc-batch", country, "定向政策", "赠送时长"),
        ("policy-b", "doc-batch", "莫桑比克", "定向政策", "赠送时长"),
        ("policy-c", "doc-single", country, "公开政策", "升级权益"),
    ])
    if legacy:
        (repo / "companies.tsv").write_text("赞比亚\t占位公司甲\n莫桑比克\t占位公司乙\n", encoding="utf-8")
    assert main(["plan", "--corpus", *PLAN, "--all"]) == 0
    assert main(["freeze", *FREEZE]) == 0
    frozen = capsys.readouterr().out.splitlines()[-1]
    (repo / "companies.tsv").unlink()
    calls = []

    def model(*, system, user):
        body = json.loads(user)["application"]
        calls.append(body)
        if body != BATCH:
            logging.warning("占位单条原稿")
            raise RuntimeError("占位接口回包")
        split = BATCH.index(SECOND)
        return shaped_reply(body, [
            {"country": {"value": "莫桑比克", "evidence": [SECOND]},
             "user_scope": {"value": "占位金标乙", "evidence": ["占位正文乙"]}},
            {"country": {"value": "赞比亚", "evidence": [FIRST]},
             "user_scope": {"value": "占位不同值", "evidence": ["占位正文甲"]}},
        ], policy_ranges=[(split, len(body)), (0, split)])

    args = run_args(frozen)
    if legacy:
        args.append("--legacy-companies")
    assert main(args, model=model) == 0
    assert sorted(calls) == sorted([BATCH, "占位单条原稿\n"])
    output = repo / "run"
    report = get_json(output / "public-report.json")
    assert report["freeze_sha256"] == frozen
    assert report["documents"] == {"parsed": 1, "model_failed": 1, "parse_failed": 0}
    assert report["sample_alignment"]["matched"] == 2
    assert report["by_field"]["company"]["match"] == 1
    assert report["by_field"]["company"]["error"] == 1
    assert report["business"]["by_field"]["company"]["correct"] == int(grade == "correct")
    assert report["business"]["by_field"]["company"]["false_determined"] == 1 + (grade == "false_determined")
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


def test_full_document_review_counts_cards_and_keeps_field_scores(repo, corpus, capsys):
    catalogs(corpus, [
        ("policy-a", "doc-batch", "赞比亚", "定向政策", "赠送时长"),
        ("policy-b", "doc-batch", "莫桑比克", "定向政策", "赠送时长"),
        ("policy-c", "doc-single", "赞比亚", "公开政策", "升级权益"),
    ])
    assert main(["plan", "--corpus", *PLAN, "--all"]) == 0
    assert main(["freeze", *FREEZE]) == 0
    frozen = capsys.readouterr().out.splitlines()[-1]

    def model(*, system, user):
        body = json.loads(user)["application"]
        if body != BATCH:
            return serialized_empty()
        reply = fixed_reply(body, [
            {"country": {"value": "赞比亚", "evidence": [FIRST]},
             "user_scope": {"value": "占位金标甲", "evidence": ["占位正文甲"]}},
            {"country": {"value": "莫桑比克", "evidence": [SECOND]},
             "user_scope": {"value": "占位金标乙", "evidence": ["占位正文乙"]}},
        ], policy_ranges=[(0, BATCH.index(SECOND)), (BATCH.index(SECOND), len(body))])
        reply["policies"][0]["continuations"] = [{"source_id": "body", "start": len(body), "end": len(body) + 1}]
        return json.dumps(reply, ensure_ascii=False)

    assert main(run_args(frozen), model=model) == 0
    report = get_json(repo / "run/public-report.json")
    assert report["full_document_review"] == {"cards": 1, "policies": 2}
    assert report["by_field"]["user_scope"]["match"] == 2
    assert report["business"]["by_field"]["user_scope"]["correct"] == 2
    markdown = (repo / "run/public-report.md").read_text()
    assert "需核对全文的卡片数 1（政策数 2）" in markdown
    assert report["full_document_review_reasons"]["unlocated_policy_continuation"] == 1


def test_no_live_flag_never_constructs_a_model(repo, frozen, monkeypatch):
    def fail(*args):
        pytest.fail("unexpected model construction")
    monkeypatch.setattr("qianshu.regression.__main__.configured_model", fail)
    assert main(run_args(frozen)) == 1
    assert not (repo / "run").exists()


def test_frozen_baseline_runs_with_an_expanded_parser_and_reports_new_field_counts(repo, frozen, monkeypatch):
    def model(*, system, user):
        body = json.loads(user)["application"]
        if body != BATCH:
            return serialized_empty()
        return shaped_reply(body, [
            {"country": {"value": "赞比亚", "evidence": [FIRST]}},
            {"country": {"value": "莫桑比克", "evidence": [SECOND]}},
        ], policy_ranges=[(0, BATCH.index(SECOND)), (BATCH.index(SECOND), len(BATCH))])


    assert main(run_args(frozen), model=model) == 0
    original = get_json(repo / "run" / "public-report.json")
    denominators = repo / "frozen" / "denominators.json"
    original_denominators = denominators.read_bytes()

    @dataclass(kw_only=True)
    class ExtendedDraft(parse.ParsedPolicyDraft):
        synthetic_offer: DraftField[str]

    def expanded_parser(*args, **kwargs):
        result = parse.parse_application(*args, **kwargs)
        for index, draft in enumerate(result):
            extended = ExtendedDraft(
                **{field.name: getattr(draft, field.name) for field in fields(draft)},
                synthetic_offer=DraftField(value="占位正文甲") if index == 0
                else DraftField(uncertainty=Uncertainty(reason="占位待确认")),
            )
            extended.__dict__.update(draft.__dict__)
            result[index] = extended
        return result

    monkeypatch.setattr(runner, "parse_application", expanded_parser)
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
        body = json.loads(user)["application"]
        draft = {"country": {"value": "赞比亚", "evidence": [FIRST]}}
        return shaped_reply(body, [draft, draft], policy_ranges=[(0, BATCH.index(SECOND))] * 2)

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
        return serialized_empty()

    assert main(run_args(frozen), model=model) == 1
    assert moved.is_dir()
    assert list(moved.iterdir()) == []
    assert list((repo / "run").iterdir()) == []


def _replay_args(fingerprint, source=".private/run", out=".private/replay"):
    return ["replay", "--recording", source, *run_args(fingerprint, out)[1:]]


def test_release_records_and_replays_reply_without_tool_use(repo, frozen, monkeypatch):
    import anthropic
    from anthropic.types import TextBlock, ThinkingBlock

    blocks = [
        ThinkingBlock(type="thinking", thinking="占位思考", signature="placeholder"),
        TextBlock(type="text", text=serialized_empty()),
    ]
    reply = SimpleNamespace(stop_reason="end_turn", content=blocks)

    efforts = []

    def stream(**payload):
        efforts.append(payload["output_config"]["effort"])
        assert "tool_choice" not in payload
        assert "必须调用 `submit_application_reply` 一次" in payload["system"]
        assert "不要用文字回答" in payload["system"]
        return nullcontext([SimpleNamespace(type="message_start", message=reply)])

    client = SimpleNamespace(messages=SimpleNamespace(create=stream))
    monkeypatch.setattr(anthropic, "Anthropic", lambda **_: nullcontext(client))
    monkeypatch.setenv("AI_PLATFORM", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder-key")
    monkeypatch.delenv("ANTHROPIC_EFFORT", raising=False)
    assert main([*run_args(frozen), "--live", "--release", "--retries", "1"]) == 0
    assert efforts == ["medium", "medium"]
    report = get_json(repo / "run/public-report.json")
    assert report["documents"] == {"model_failed": 0, "parse_failed": 2, "parsed": 0}
    assert report["business"]["counts"]["reply_parse_failed"] == 2
    record = get_json(repo / "run/document-00001.json")
    attempt, = record["attempts"]
    assert attempt["effort"] == record["configuration"]["effort"] == "medium"
    assert attempt["failure_stage"] == record["reason_code"] == "reply_tool_missing"
    assert json.loads(attempt["reply"]) == [block.model_dump(exclude_unset=True) for block in blocks]
    monkeypatch.setenv("ANTHROPIC_EFFORT", "high")
    assert main([*run_args(frozen, ".private/high"), "--live", "--release", "--retries", "1"]) == 0
    assert efforts == ["medium", "medium", "high", "high"]
    high_record = get_json(repo / "high/document-00001.json")
    assert high_record["attempts"][0]["effort"] == high_record["configuration"]["effort"] == "high"
    high_report = get_json(repo / "high/public-report.json")
    assert high_report["configuration_sha256"] != report["configuration_sha256"]

    monkeypatch.setenv("ANTHROPIC_EFFORT", "unsupported")
    assert main([*run_args(frozen, ".private/invalid"), "--live", "--release"]) == 1
    assert not (repo / "invalid").exists()
    assert main(_replay_args(frozen)) == 0
    assert efforts == ["medium", "medium", "high", "high"]
    for name in runner.RUN_FILES:
        assert (repo / "replay" / name).read_bytes() == (repo / "run" / name).read_bytes()


@pytest.mark.parametrize(("stop", "reason"), [
    ("max_tokens", "reply_truncated"),
    ("stop_sequence", "reply_incomplete"),
])
def test_incomplete_reply_is_counted_even_when_release_is_void(repo, frozen, monkeypatch, stop, reason):
    from qianshu.parse.model import CallResult, ModelError

    def configured(_settings, *, on_result, max_retries):
        assert max_retries == 0

        def model(*, system, user):
            if json.loads(user)["application"] == BATCH:
                on_result(CallResult(stop, (("text", 1),), 8, 1, 4, "placeholder-model", "[", "model_incomplete"))
            else:
                on_result(CallResult(None, (), None, None, 4, "placeholder-model", None, "model_call"))
            raise ModelError("占位错误")
        return model

    monkeypatch.setattr("qianshu.regression.__main__.configured_model", configured)
    assert main([*run_args(frozen), "--live", "--release"]) == 0
    report = get_json(repo / "run/public-report.json")
    assert report["void"] is True
    assert report["documents"] == {"model_failed": 1, "parse_failed": 1, "parsed": 0}
    assert report["business"]["counts"]["reply_parse_failed"] == 1
    assert report["business"]["reply_parse_failure_reasons"][reason] == 1
    record = get_json(repo / "run/document-00001.json")
    assert record["failure_stage"] == reason
    attempt, = record["attempts"]
    assert (attempt["reply"], attempt["stop_reason"]) == ("[", stop)
    assert main(_replay_args(frozen)) == 0
    for name in runner.RUN_FILES:
        assert (repo / "replay" / name).read_bytes() == (repo / "run" / name).read_bytes()


@pytest.mark.parametrize("status", [503, 307])
def test_release_retry_limit_matches_actual_sdk_http_requests(repo, frozen, monkeypatch, status):
    import anthropic
    import httpx

    requests = Counter()

    def reply(request):
        requests[json.loads(request.content)["messages"][0]["content"]] += 1
        return httpx.Response(status, headers={"Location": "/redirected"})

    monkeypatch.setattr(anthropic, "DefaultHttpxClient", lambda **kwargs: httpx.Client(
        transport=httpx.MockTransport(reply), **kwargs,
    ))
    monkeypatch.setenv("AI_PLATFORM", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://model.example.invalid")
    assert main([*run_args(frozen), "--live", "--release", "--retries", "1"]) == 0
    report = get_json(repo / "run/public-report.json")
    assert len(requests) == 2 and set(requests.values()) == {2}
    assert report["usage"]["attempts"] == sum(requests.values())
    assert report["void"] is True


def _stream_reply_events(text, *, interrupted=False):
    events = [
        {"type": "message_start", "message": {
            "id": "msg_placeholder", "role": "assistant", "type": "message", "model": "placeholder-model",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 0},
        }},
    ]
    if interrupted:
        events.extend([
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        ])
    else:
        events.append({"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "id": "toolu_placeholder", "name": "submit_application_reply", "input": {},
        }})
        split = len(text) // 2
        events.extend(
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": chunk}}
            for chunk in (text[:split], text[split:])
        )
        events.extend([
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
             "usage": {"output_tokens": 1}},
            {"type": "message_stop"},
        ])
    for event in events:
        yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


@pytest.mark.parametrize("damage", ["missing_brace", "duplicate_key", "nonfinite", "invalid_character"])
def test_release_records_invalid_tool_json_and_replays_parse_failure(repo, frozen, monkeypatch, damage):
    import anthropic
    import httpx

    valid = serialized_empty()
    invalid = {
        "missing_brace": valid.removesuffix("}"),
        "duplicate_key": valid.removesuffix("}") + ', "policies": []}',
        "nonfinite": valid.removesuffix("}") + ', "nonfinite": NaN}',
        "invalid_character": valid + "!",
    }[damage]

    def reply(request):
        user = json.loads(request.content)["messages"][0]["content"]
        body = json.loads(user)["application"]
        text = invalid if body == BATCH else serialized_empty()
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=b"".join(_stream_reply_events(text)))

    monkeypatch.setattr(anthropic, "DefaultHttpxClient", lambda **kwargs: httpx.Client(
        transport=httpx.MockTransport(reply), **kwargs,
    ))
    monkeypatch.setenv("AI_PLATFORM", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://model.example.invalid")
    assert main([*run_args(frozen), "--live", "--release"]) == 0
    report = get_json(repo / "run/public-report.json")
    assert report["void"] is False
    assert report["documents"] == {"model_failed": 0, "parse_failed": 1, "parsed": 1}
    assert report["business"]["counts"]["reply_parse_failed"] == 1
    assert report["business"]["reply_parse_failure_reasons"]["reply_parse"] == 1
    record = get_json(repo / "run/document-00001.json")
    attempt, = record["attempts"]
    assert attempt["failure_stage"] == record["reason_code"] == "tool_input_json"
    assert (attempt["reply"], attempt["stop_reason"]) == (invalid, "tool_use")
    assert main(_replay_args(frozen)) == 0
    for name in runner.RUN_FILES:
        assert (repo / "replay" / name).read_bytes() == (repo / "run" / name).read_bytes()


@pytest.mark.parametrize(("interruption", "retry_reply"), [
    ("timeout", None), ("disconnect", None), ("eof", None),
    ("timeout", "valid"), ("timeout", "invalid"),
])
def test_release_retries_interrupted_partial_text(repo, frozen, monkeypatch, interruption, retry_reply):
    import anthropic
    import httpx

    class ReplyStream(httpx.SyncByteStream):
        def __init__(self, text, interrupted):
            self.text = text
            self.interrupted = interrupted

        def __iter__(self):
            yield from _stream_reply_events(self.text, interrupted=self.interrupted)
            if self.interrupted and interruption == "timeout":
                raise httpx.ReadTimeout("占位超时")
            if self.interrupted and interruption == "disconnect":
                raise httpx.RemoteProtocolError("占位断线")

    requests = Counter()

    def reply(request):
        user = json.loads(request.content)["messages"][0]["content"]
        body = json.loads(user)["application"]
        requests[body] += 1
        final_reply = {None: None, "valid": serialized_empty(), "invalid": "{}"}[retry_reply]
        text = (final_reply if requests[body] > 1 else None) if body == BATCH else serialized_empty()
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=ReplyStream("[" if text is None else text, text is None))

    monkeypatch.setattr(anthropic, "DefaultHttpxClient", lambda **kwargs: httpx.Client(
        transport=httpx.MockTransport(reply), **kwargs,
    ))
    monkeypatch.setenv("AI_PLATFORM", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://model.example.invalid")
    assert main([*run_args(frozen), "--live", "--release", "--retries", "1"]) == 0
    assert requests == {BATCH: 2, "占位单条原稿\n": 1}
    report = get_json(repo / "run/public-report.json")
    assert report["usage"]["attempts"] == 3
    assert report["void"] is (retry_reply is None)
    assert report["documents"] == {
        "model_failed": int(retry_reply is None), "parse_failed": int(retry_reply == "invalid"),
        "parsed": 1 + int(retry_reply == "valid"),
    }
    assert report["business"]["counts"]["reply_parse_failed"] == 1
    assert report["business"]["reply_parse_failure_reasons"] == {
        "reply_truncated": 1, "reply_parse": int(retry_reply == "invalid"), "reply_incomplete": 0,
    }
    record = get_json(repo / "run/document-00001.json")
    first, second = record["attempts"]
    partial_stage = "model_incomplete" if interruption == "eof" else "reply_truncated"
    assert (first["failure_stage"], first["reply"]) == (partial_stage, "[")
    if retry_reply is None:
        assert (second["failure_stage"], second["reply"]) == (partial_stage, "[")
    elif retry_reply == "valid":
        assert second["failure_stage"] is None
        assert json.loads(second["reply"]) == json.loads(serialized_empty())
    else:
        assert second["failure_stage"] == record["reason_code"] == "reply_schema"
        assert record["structure_path"] == ["policies"]
        assert second["reply"] == "{}"
    assert main(_replay_args(frozen)) == 0
    assert requests == {BATCH: 2, "占位单条原稿\n": 1}
    for name in runner.RUN_FILES:
        assert (repo / "replay" / name).read_bytes() == (repo / "run" / name).read_bytes()


def test_valid_replacement_references_original_void_recording(repo, frozen):
    def unavailable(**_):
        raise RuntimeError("占位调用失败")

    assert main([*run_args(frozen), "--release"], model=unavailable) == 0
    original = (repo / "run/recording.json").read_bytes()
    assert main([*run_args(frozen, ".private/replacement"), "--release", "--replaces-recording", ".private/run"],
                model=no_policies) == 0
    replacement = get_json(repo / "replacement/recording.json")
    original_id = json.loads(original)["run_id"]
    assert replacement["replaces_run_id"] == original_id != replacement["run_id"]
    assert (repo / "run/recording.json").read_bytes() == original
    assert get_json(repo / "replacement/public-report.json")["void"] is False
    assert "replaces_run_id" not in get_json(repo / "replacement/public-report.json")
    assert main([*run_args(frozen, ".private/invalid"), "--release", "--replaces-recording", ".private/replacement"],
                model=no_policies) == 1
    assert not (repo / "invalid").exists()


def test_recorded_calls_replay_byte_for_byte_without_model_access(repo, frozen, monkeypatch, capsys):
    from qianshu.parse.model import CallResult
    from qianshu.regression.__main__ import _MODEL_SETTINGS

    for key in _MODEL_SETTINGS:
        monkeypatch.delenv(key, raising=False)

    attempts = Counter()
    batch = fixed_reply(BATCH, [{"country": {"value": "赞比亚", "evidence": [FIRST, "占位正文甲", "包", "占位缺失引文"]}}])
    _, shifted, tied, _ = batch["policies"][0]["fields"]["country"]["evidence"]
    shifted["span"]["start"] += 1
    tied["span"]["start"] = (BATCH.index("包") + BATCH.rindex("包")) // 2
    expected_locations = {
        "numerator": 2, "denominator": 4, "percent": 50.0,
        "counts": {"exact": 1, "relocated": 1, "ambiguous": 1, "not_found": 1},
    }
    responses = {
        BATCH: json.dumps(batch, ensure_ascii=False),
        "占位单条原稿\n": "占位无效回包",
    }

    def configured(_settings, *, on_result, max_retries):
        assert max_retries == 0
        def model(*, system, user):
            body = json.loads(user)["application"]
            attempts[body] += 1
            if attempts[body] == 1:
                on_result(CallResult(None, (), None, None, 7, "synthetic-model", None, "model_call", effort="medium"))
                raise RuntimeError("占位保密失败详情")
            reply = responses[body]
            on_result(CallResult("end_turn", (("thinking", 5), ("text", len(reply))),
                                 12, 3, 20, "synthetic-model", reply, effort="medium"))
            return reply
        return model

    monkeypatch.setattr("qianshu.regression.__main__.configured_model", configured)
    assert main([*run_args(frozen), "--live", "--release", "--threshold", "3"]) == 0
    assert attempts == {BATCH: 2, "占位单条原稿\n": 2}
    recording = get_json(repo / "run" / "recording.json")
    assert recording["threshold"] == 3
    assert recording["schema"] == 1 and len(recording["documents_by_user_sha256"]) == 2
    for name, checksum in recording["artifact_sha256"].items():
        path = repo / "run" / name
        assert digest(path.read_bytes()) == checksum
        record = get_json(path)
        assert record["run_id"] == recording["run_id"]
        assert record["configuration"] == recording["configuration"]
        assert len(record["user_sha256"]) == len(record["system_sha256"]) == 64
        for kind in ("freeze", "code", "rules", "companies", "prompt", "model", "pairing", "measure"):
            assert len(record["configuration"][f"{kind}_sha256"]) == 64
        failed, final = record["attempts"]
        assert failed["failure_stage"] == "model_call"
        assert failed["effort"] == "medium"
        assert final == {
            "stop_reason": "end_turn", "blocks": [{"type": "thinking", "length": 5},
                                                 {"type": "text", "length": len(final["reply"])}],
            "input_tokens": 12, "output_tokens": 3, "elapsed_ms": 20,
            "model": "synthetic-model", "reply": final["reply"], "failure_stage": None, "effort": "medium",
        }
        assert final["reply"] in responses.values()
        assert record["failure_stage"] == ("reply_parse" if final["reply"] == responses["占位单条原稿\n"] else None)
        if record["failure_stage"] == "reply_parse":
            assert record["evidence_location"]["denominator"] == 0
            assert record["evidence_location"]["percent"] is None
        else:
            assert record["evidence_location"] == expected_locations
        assert "占位保密失败详情" not in path.read_text()
        assert path.stat().st_mode & 0o777 == 0o600

    def forbidden(*args, **kwargs):
        pytest.fail("offline replay attempted model access")

    monkeypatch.setattr("qianshu.regression.__main__.configured_model", forbidden)
    assert main(_replay_args(frozen), model=forbidden) == 0
    for name in runner.RUN_FILES:
        assert (repo / "replay" / name).read_bytes() == (repo / "run" / name).read_bytes()
    report = get_json(repo / "run" / "public-report.json")
    assert report["evidence_location"] == expected_locations
    assert report["business"]["counts"]["reply_parse_failed"] == 1
    assert report["documents"]["parse_failed"] == 1 and report["void"] is False
    assert report["usage"]["input_tokens"] == {"captured_attempts": 2, "total": 24}
    assert report["usage"]["elapsed_ms"] == {"captured_attempts": 4, "total": 54}
    public = (repo / "run" / "public-report.md").read_text() + (repo / "run" / "public-report.json").read_text()
    assert "唯一对齐" in public and "可比字段全对率" in public
    assert "证据定位成功率 2/4（50.00%），计字段、候选及带引文的政策锚点" in public
    assert "程序重新定位 1" in public
    for label in ("模型坐标原样正确", "多处不可分", "引文不存在"):
        assert f"{label} 1" in public
    for private in ("占位无效回包", "占位保密失败详情", "占位缺失引文", FIRST, "synthetic-model"):
        assert private not in public + capsys.readouterr().err

    name = next(iter(recording["artifact_sha256"]))
    (repo / "run" / name).unlink()
    assert main(_replay_args(frozen, out=".private/missing"), model=forbidden) == 1
    assert "录制缺少对应的回复文件" in capsys.readouterr().err


def test_import_uses_probe_format_and_private_paths(repo, frozen, capsys):
    def probe(*, system, user):
        user_sha = digest(user.encode())
        reply = serialized_empty()
        put_json(repo / "probe" / f"{user_sha}.json", {
            "system_sha": digest(system.encode()), "user_sha": user_sha,
            "reply": reply, "error": None, "secs": 0.2,
        })
        return reply

    assert main(run_args(frozen), model=probe) == 0
    arguments = ["import", "--probe", ".private/probe", "--model-id", "synthetic",
                 *run_args(frozen, ".private/imported")[1:]]
    assert main(arguments) == 0
    imported = get_json(repo / "imported" / "recording.json")
    assert imported["origin"] == "probe" and imported["source_code_measured"] is False
    for name in imported["artifact_sha256"]:
        record = get_json(repo / "imported" / name)
        assert record["attempts"] == [{
            "reply": serialized_empty(), "failure_stage": None, "model": "synthetic", "stop_reason": None, "blocks": None,
            "input_tokens": None, "output_tokens": None, "elapsed_ms": None,
        }]
    assert "未采集" in (repo / "imported" / "public-report.md").read_text()
    assert main(_replay_args(frozen, ".private/imported")) == 0
    assert (repo / "imported" / "public-report.json").read_bytes() == (repo / "replay" / "public-report.json").read_bytes()

    for unsafe in ("probe", str(repo / "probe"), ".private/../probe"):
        arguments[2] = unsafe
        assert main(arguments) == 1
        assert ".private" in capsys.readouterr().err
    link = repo / "linked"
    link.symlink_to(repo / "probe", target_is_directory=True)
    arguments[2] = ".private/linked"
    arguments[-1] = ".private/link-output"
    assert main(arguments) == 1
    assert not (repo / "link-output" / "recording.json").exists()


def test_summarize_uses_exact_ratios_and_does_not_count_replay_or_void(repo, frozen, capsys):
    def one_failed_reply(*, system, user):
        if json.loads(user)["application"] == BATCH:
            raise RuntimeError("占位失败")
        return "占位无法解码"

    for name, model in (("first", no_policies), ("second", no_policies), ("void", one_failed_reply)):
        assert main([*run_args(frozen, f".private/{name}"), "--release"], model=model) == 0
    void = get_json(repo / "void" / "public-report.json")
    assert void["void"] and void["business"] == {
        "counts": {"reply_parse_failed": 1},
        "reply_parse_failure_reasons": {"reply_parse": 1, "reply_incomplete": 0, "reply_truncated": 0},
    }
    assert "sample_alignment" not in void
    args = ["summarize", "--recording", ".private/first", "--recording", ".private/second",
            "--recording", ".private/void", "--out", ".private/summary"]
    assert main(args) == 0
    summary = get_json(repo / "summary" / "public-summary.json")
    assert (summary["runs"], summary["valid_runs"], summary["void_runs"]) == (3, 2, 1)
    assert summary["business_counts"]["reply_parse_failed"] == {"values": [0, 0, 1], "mean": 1 / 3, "min": 0, "max": 1}
    assert summary["old_alignment"]["unmatched"] == {"values": [3, 3], "mean": 3, "min": 3, "max": 3}
    assert summary["business_rates_percent"]["all_correct"]["fractions"] == [
        {"numerator": 0, "denominator": 3}, {"numerator": 0, "denominator": 3},
    ]

    assert main(_replay_args(frozen, ".private/first")) == 0
    args[4], args[-1] = ".private/replay", ".private/duplicated"
    assert main(args) == 1
    assert "不能计为独立运行" in capsys.readouterr().err
    assert main([*run_args(frozen, ".private/other"), "--release", "--threshold", "3"], model=no_policies) == 0
    args[4] = ".private/other"
    assert main(args) == 1
    assert "同一冻结" in capsys.readouterr().err


@pytest.mark.parametrize(("scores", "matches", "ambiguous_policies", "ambiguous_drafts"), [
    ([[9, 8], [7, None]], {0: 1, 1: 0}, set(), set()),
    ([[5, 5, None], [5, 5, None], [None, None, 4]], {2: 2}, {0, 1}, {0, 1}),
    ([[None, 5], [None, 5], [None, None]], {}, {0, 1}, {1}),
    ([[5, 5, 5, None]], {}, {0}, {0, 1, 2}),
    ([[5, 3], [2, None]], {}, {0, 1}, {0, 1}),
    ([[], []], {}, set(), set()),
    ([], {}, set(), set()),
])
def test_business_pairing_maximizes_whole_assignment(scores, matches, ambiguous_policies, ambiguous_drafts):
    from qianshu.regression.business import optimal_pairs

    assert optimal_pairs(scores) == (matches, ambiguous_policies, ambiguous_drafts)


@pytest.mark.parametrize("name", ["packages", "user_tags"])
@pytest.mark.parametrize("excluded", [False, True])
def test_business_pairing_reliable_key_outweighs_other_matches(name, excluded):
    from qianshu.draft import PackageRow
    from qianshu.regression.business import BusinessScore, WEIGHTS

    values = (
        [[{"current_package": f"占位包{letter}", "gift_package": None}] for letter in ("甲", "乙")]
        if name == "packages" else [[f"占位标签{letter}"] for letter in ("甲", "乙")]
    )
    pairs = []
    drafts = []
    for index, value in enumerate(values):
        keys = {"recharge_days": 30 + index}
        if name == "packages" or index == 1:
            keys["gift_days"] = 15 + index
        pairs.append({
            "id": str(index),
            "configuration": {
                **{key: {"value": number} for key, number in keys.items()},
                name: {"exclude": "insufficient_evidence"} if excluded else {"value": value},
            },
            **({"pairing": {name: value}} if excluded else {}),
        })
        opposite = values[1 - index]
        derived = [PackageRow(**row) for row in opposite] if name == "packages" else opposite
        drafts.append(SimpleNamespace(
            to_dict=lambda: {},
            **{key: DraftField(value=number) for key, number in keys.items()},
            **{name: DraftField(value=derived)},
        ))
    frozen = {
        "samples": {pair["id"]: {"country": "占位地区", "benefit_type": "赠送时长"} for pair in pairs},
        "denominators": {"fields": {}},
    }
    score = BusinessScore(frozen, 2, {})

    result = score.add({"body": "占位"}, pairs, drafts)

    assert [row["draft"] for row in result["policies"]] == [1, 0]
    old_weight = 4 if name == "packages" else 3
    assert sum(row["positive_scores"][index]["score"] for index, row in enumerate(result["policies"])) == 2 * old_weight
    assert all(row["positive_scores"][1 - index] == {"draft": 1 - index, "score": WEIGHTS[name], "keys": [name]}
               for index, row in enumerate(result["policies"]))
    assert result["ambiguous_drafts"] == []


@pytest.mark.parametrize(("gold_tags", "draft_tags"), [
    (["占位Beta", "占位Abc"], ["　占位ＡＢＣ＿ＩＭＡＧＥ ", "占位BETA"]),
    (["　占位ＡＢＣ＿ＩＭＡＧＥ ", "占位beta"], ["占位Abc", "占位Beta"]),
])
def test_business_pairing_normalizes_tags_and_grades_original_values(gold_tags, draft_tags):
    from qianshu.draft import SourceExcerpt
    from qianshu.regression.business import BusinessScore, WEIGHTS

    pair = {"id": "policy", "configuration": {"user_tags": {"value": sorted(gold_tags)}}}
    frozen = {
        "samples": {"policy": {"country": "占位地区", "benefit_type": "赠送时长"}},
        "denominators": {"fields": {"user_tags": {"ids": ["policy"]}}},
    }
    body = "占位原文"
    draft = SimpleNamespace(
        user_tags=DraftField(value=draft_tags, evidence=[SourceExcerpt(text=body, start=0)]),
        to_dict=lambda: {},
    )

    result = BusinessScore(frozen, 2, {}).add({"body": body}, [pair], [draft])

    policy = result["policies"][0]
    assert policy["draft"] == 0
    assert policy["positive_scores"] == [{"draft": 0, "score": WEIGHTS["user_tags"], "keys": ["user_tags"]}]
    assert policy["fields"] == {"user_tags": "false_determined"}
    assert draft.user_tags.value == draft_tags


def test_tag_pairing_key_removes_only_known_suffix_and_fingerprints_it(monkeypatch):
    from qianshu.regression import business

    assert business.key_value("user_tags", ["Abc", " ＡＢＣ＿ＩＭＡＧＥ "]) == ["abc"]
    assert business.key_value("user_tags", ["Abc_image_other", "Abc_other"]) == ["abc_image_other", "abc_other"]
    assert business.key_value("user_tags", ["Abc_image_image", "_image"]) == ["_image", "abc_image"]
    assert business.key_value("user_tags", []) == []
    assert business.key_value("user_tags", None) is None

    fingerprint = business.pairing_fingerprint(2)
    monkeypatch.setattr(business, "SHADOW_TAG_SUFFIXES", (*business.SHADOW_TAG_SUFFIXES, "_other"))
    assert business.pairing_fingerprint(2) != fingerprint


@pytest.mark.parametrize(("entry", "mark", "value", "confirmation", "start", "status"), [
    ({"value": 30}, {"inferable": True, "auto_allowed": True}, 30, False, 0, "correct"),
    ({"value": 30}, {"inferable": True, "auto_allowed": True}, 5, False, 0, "false_determined"),
    ({"value": 30}, {"inferable": True, "auto_allowed": True}, 30, False, 1, "false_determined"),
    ({"value": 30}, {"inferable": False, "auto_allowed": True}, 30, False, 0, "false_determined"),
    ({"value": 30}, {"inferable": True, "auto_allowed": False}, 30, False, 0, "false_determined"),
    ({"value": 30}, {"inferable": True, "auto_allowed": True}, None, True, 0, "overasked"),
    ({"value": 30}, {"inferable": False, "auto_allowed": True}, None, True, 0, "justified_confirmation"),
    ({"value": 30}, {"inferable": True, "auto_allowed": False}, None, True, 0, "justified_confirmation"),
    ({"value": 30}, {"inferable": True, "auto_allowed": True}, None, None, 0, "missing"),
    ({"value": 30}, {"inferable": True}, None, None, 0, "unknown"),
    ({}, {"inferable": True, "auto_allowed": True}, 30, False, 0, "unknown"),
])
def test_business_grades_all_categories(entry, mark, value, confirmation, start, status):
    from qianshu.draft import SourceExcerpt
    from qianshu.regression.business import grade

    field = DraftField(value=value, evidence=[SourceExcerpt(text="占位 30 天", start=start)],
                       uncertainty=Uncertainty(reason="占位待确认") if confirmation else None)
    draft = SimpleNamespace(recharge_days=field) if confirmation is not None else None
    assert grade("recharge_days", entry, mark, draft, "占位 30 天") == status


def test_business_maps_included_gold_and_separates_extras_from_unknown():
    from qianshu.draft import SourceExcerpt
    from qianshu.regression.business import BusinessScore, grade, pairing_audit

    mark = {"inferable": True, "auto_allowed": True}
    default = SimpleNamespace(trigger_once=DraftField(value="否"))
    assert grade("trigger_once", {"value": "否"}, mark, default, "") == "correct"
    assert grade("trigger_once", {"value": "否"}, {**mark, "default_allowed": False}, default, "") == "false_determined"

    def draft(name, value=None):
        return SimpleNamespace(
            company=DraftField(value=name),
            recharge_days=DraftField(value=value, evidence=[SourceExcerpt(text="占位 30 天", start=0)]),
            to_dict=lambda: {},
        )

    pairs = [
        {"id": name, "configuration": {"company": {"value": company}, "recharge_days": {"value": 30}},
         "business": {"recharge_days": marks}}
        for name, company, marks in (("first", "占位甲", mark), ("unknown", "占位乙", {}), ("missing", "占位丙", mark))
    ]
    snapshot = {
        "samples": {pair["id"]: {"country": "占位地区", "benefit_type": "赠送时长"} for pair in pairs},
        "denominators": {"fields": {"recharge_days": {"ids": ["first", "unknown", "missing"]}}},
    }
    score = BusinessScore(snapshot, 2, {})
    complete = score.add({"body": "占位 30 天", "complete": True}, pairs[:2],
                         [draft("占位甲", 30), draft("占位甲", 30), draft("占位乙", 7), draft("占位丁", 8)])
    partial = score.add({"body": "占位 30 天", "complete": False}, pairs[2:], [draft("占位丁", 8)])
    report = score.report(reply_parse_failed=1)
    assert report["counts"] == {
        "unknown_fields": 0, "missing_policies": 1, "extra_policies": 1, "unclassified_drafts": 1,
        "tied_pairs": 1, "ambiguous_drafts": 2, "matched_policies": 1, "comparable_policies": 1, "reply_parse_failed": 1,
    }
    assert (report["rates"]["all_correct"]["numerator"], report["rates"]["all_correct"]["denominator"]) == (0, 3)
    assert report["rates"]["safe_confirmation"]["numerator"] == 0
    assert report["rates"]["false_determined"]["denominator"] == 3
    assert report["rates"]["overasked"]["denominator"] == 3
    assert report["rates"]["zero_edit"]["percent"] is None
    assert report["field_categories"] == {
        "correct": 0, "false_determined": 1, "overasked": 0, "justified_confirmation": 0, "missing": 2, "unknown": 0,
    }
    audit = pairing_audit([{"token": "complete", **complete}, {"token": "partial", **partial}])
    assert audit == {
        "matched": [{"token": "complete", "id": "unknown", "draft": 2}],
        "missing": [{"token": "partial", "id": "missing"}],
        "extra_or_unknown": [
            {"token": "complete", "draft": 3, "kind": "extra_policies"},
            {"token": "partial", "draft": 0, "kind": "unclassified_drafts"},
        ],
        "ambiguous": [{"token": "complete", "policies": ["first"], "drafts": [0, 1]}],
    }


def test_pairing_audit_caps_ambiguous_groups_in_frozen_order():
    from qianshu.regression.business import BusinessScore, pairing_audit

    tokens = ["f", "e", "d", "c", "b", "a"]
    ids_by_token = {token: [f"{token}-2", f"{token}-1"] for token in tokens}
    snapshot = {
        "samples": {
            policy_id: {"country": "占位地区", "benefit_type": "赠送时长"}
            for ids in ids_by_token.values() for policy_id in ids
        },
        "denominators": {"fields": {}},
    }
    score = BusinessScore(snapshot, 2, {})
    draft = SimpleNamespace(recharge_days=DraftField(value=30), to_dict=lambda: {})
    documents = []
    for token in tokens:
        pairs = [
            {"id": policy_id, "configuration": {"recharge_days": {"value": 30}}}
            for policy_id in ids_by_token[token]
        ]
        documents.append({"token": token, **score.add({"body": "占位"}, pairs, [draft, draft])})

    assert pairing_audit(documents)["ambiguous"] == [
        {"token": token, "policies": ids_by_token[token], "drafts": [0, 1]}
        for token in tokens[:5]
    ]
    report = score.report(reply_parse_failed=0)
    assert report["counts"]["tied_pairs"] == 12
    assert report["rates"]["all_correct"] == {"numerator": 0, "denominator": 12, "percent": 0.0}


@pytest.mark.parametrize("dataset", ["exam", "blind", "packages"])
def test_g285_uses_frozen_facts_defaults_and_denominators(dataset, monkeypatch):
    from qianshu.draft import SourceExcerpt
    from qianshu.regression.business import BusinessScore, G285_MAPPING, measure_fingerprint

    values = {
        "trigger_once": "是", "effective_multiplier": "2", "cancel_on_downgrade": "否",
        "maintain_mutual_exclusion_group": "是", "repeat_bonus": "是", "recharge_days": 30, "user_scope": "占位",
    }
    names = [*values, "gift_days"]
    pair = {
        "id": "policy", "configuration": {name: {"value": value} for name, value in values.items()},
        "source_status": {"cancel_on_downgrade": "原文未写", "maintain_mutual_exclusion_group": "歧义"},
        "business": {"recharge_days": {"inferable": False, "auto_allowed": False}},
    }
    pair["configuration"]["gift_days"] = {"exclude": "historical_rule"}
    frozen = {
        "dataset": dataset,
        "samples": {"policy": {"country": "占位地区", "benefit_type": "赠送时长"}},
        "denominators": {"fields": {name: {"ids": ["policy"] if name != "gift_days" else []} for name in names}},
    }
    confirmed = DraftField(uncertainty=Uncertainty(reason="占位待确认"))
    default = SimpleNamespace(value="是", evidence=[], uncertainty=None, source_status="原文未写",
                              value_source="默认规则", evidence_verified=True)
    draft = SimpleNamespace(
        trigger_once=confirmed, effective_multiplier=DraftField(value=2), cancel_on_downgrade=confirmed,
        maintain_mutual_exclusion_group=confirmed, repeat_bonus=default,
        recharge_days=DraftField(value=30, evidence=[SourceExcerpt(text="占位", start=0)]),
        user_scope=confirmed, gift_days=DraftField(value=90), benefit_type=DraftField(value="赠送时长"),
        to_dict=lambda: {},
    )
    score = BusinessScore(frozen, 2, {})
    result = score.add({"body": "占位"}, [pair], [draft])
    assert result["policies"][0]["fields"] == {
        "trigger_once": "justified_confirmation", "effective_multiplier": "false_determined",
        "cancel_on_downgrade": "justified_confirmation", "maintain_mutual_exclusion_group": "justified_confirmation",
        "repeat_bonus": "correct", "recharge_days": "correct", "user_scope": "overasked",
    }
    report = score.report(0)
    assert report["rates"]["false_determined"]["denominator"] == len(values)
    assert report["counts"]["unknown_fields"] == 0
    assert report["measure_sha256"] == measure_fingerprint(2)
    monkeypatch.setitem(G285_MAPPING, "version", "占位版本")
    assert report["measure_sha256"] != measure_fingerprint(2)


@pytest.mark.parametrize(("tags", "tag_status", "policy_type", "name_status", "name_review", "type_review"), [
    (["占位标签甲", "占位标签乙"], "确定", "定向政策", None, True, False),
    (["占位标签甲"], "歧义", "定向政策", None, True, True),
    (["占位标签甲"], "确定", "定向政策", "冲突", True, False),
    (["占位标签甲"], "确定", "定向政策", None, False, False),
    (["占位标签乙"], "确定", "定向政策", None, True, False),
    (["占位标签甲"], "确定", "公开政策", None, False, True),
    ([], "确定", "公开政策", None, True, False),
    ([], "原文未写", "公开政策", None, True, True),
    ([], "原文未写", "定向政策", None, True, True),
    (None, None, "定向政策", None, True, True),
    ("historical_rule", None, "定向政策", None, False, False),
    ("source_anomaly", "确定", "公开政策", None, False, False),
    ("historical_rule", "原文未写", "定向政策", None, True, True),
    ("source_anomaly", "歧义", "定向政策", None, True, True),
    ("historical_rule", "确定", "定向政策", "冲突", True, False),
])
def test_g285_rates_name_and_type_by_frozen_tags(tags, tag_status, policy_type, name_status, name_review, type_review):
    from qianshu.draft import SourceExcerpt
    from qianshu.regression.business import BusinessScore

    values = {"policy_name": "占位标签甲", "policy_type": policy_type}
    sample = {"country": "占位地区", "benefit_type": "赠送时长", "policy_type": policy_type}
    pair = {
        "id": "policy", "configuration": {name: {"value": value} for name, value in values.items()},
        "source_status": {name: status for name, status in {"user_tags": tag_status, "policy_name": name_status}.items()
                          if status is not None},
    }
    if isinstance(tags, str):
        pair["configuration"]["user_tags"] = {"exclude": tags}
    elif tags is not None:
        pair["configuration"]["user_tags"] = {"value": tags}
    frozen = {
        "samples": {"policy": sample},
        "denominators": {"fields": {name: {"ids": ["policy"]} for name in values}},
    }
    document = {"body": "占位原文"}
    score = BusinessScore(frozen, 2, {})
    review = {"policy_name": name_review, "policy_type": type_review}
    for confirmed in (False, True):
        draft = SimpleNamespace(benefit_type=DraftField(value="赠送时长"), to_dict=lambda: {})
        for name, value in values.items():
            field = (DraftField(value=value, evidence=[SourceExcerpt(text=document["body"], start=0)]) if confirmed
                     else DraftField(uncertainty=Uncertainty(reason="占位待确认")))
            setattr(draft, name, field)
        result = score.add(document, [pair], [draft])
        expected = ({True: "false_determined", False: "correct"} if confirmed
                    else {True: "justified_confirmation", False: "overasked"})
        assert result["policies"][0]["fields"] == {name: expected[review[name]] for name in values}


@pytest.mark.parametrize(("confirm", "expected"), [
    (False, "false_determined"),
    (True, "justified_confirmation"),
])
def test_g285_uses_frozen_pairing_tags_when_tag_gold_is_excluded(confirm, expected):
    from qianshu.draft import SourceExcerpt
    from qianshu.regression.business import BusinessScore

    tags = ["占位标签甲", "占位标签乙"]
    configuration = {
        "policy_name": {"value": tags[0]},
        "policy_type": {"value": "公开政策"},
        "user_tags": {"exclude": "historical_rule"},
    }
    pair = {
        "id": "policy", "configuration": configuration,
        "source_status": {"user_tags": "确定"},
        "pairing": {"user_tags": tags},
    }
    sample = {"country": "占位地区", "benefit_type": "赠送时长", "policy_type": "公开政策"}
    frozen = {
        "samples": {"policy": sample},
        "denominators": freeze_module._denominators([pair], configuration),
    }
    body = "占位原文"
    uncertainty = Uncertainty(reason="占位待确认") if confirm else None
    evidence = [SourceExcerpt(text=body, start=0)]
    draft = SimpleNamespace(
        benefit_type=DraftField(value=sample["benefit_type"]),
        policy_name=DraftField(value=tags[0], evidence=evidence, uncertainty=uncertainty),
        policy_type=DraftField(value="公开政策", evidence=evidence, uncertainty=uncertainty),
        user_tags=DraftField(value=tags, evidence=evidence),
        to_dict=lambda: {},
    )

    score = BusinessScore(frozen, 2, {})
    result = score.add({"body": body}, [pair], [draft])
    assert result["policies"][0]["fields"] == {"policy_name": expected, "policy_type": expected}
    assert score.report(0)["rates"]["false_determined"]["denominator"] == 2


def test_g285_rates_derived_company_packages_and_downgrade_by_frozen_inputs():
    from qianshu.parse.companies import CompanyRow
    from qianshu.regression.business import _g285_mark

    sample = {"country": "占位地区", "benefit_type": "特价优惠"}
    rows = [{"current_package": "占位包甲", "gift_package": "占位包乙"}]
    values = {"company": "占位公司", "packages": rows, "cancel_on_downgrade": "是"}
    pair = {
        "configuration": {name: {"value": value} for name, value in values.items()},
        "source_status": {"cancel_on_downgrade": "原文未写"},
    }
    company_table = {"占位地区": {"": CompanyRow("占位公司", None)}}
    for name in values:
        assert _g285_mark(name, pair, sample, "name_only", company_table)["auto_allowed"] is True
    assert _g285_mark("company", pair, sample, "name_only", {})["auto_allowed"] is False
    assert _g285_mark("packages", pair, sample, "tier_only", company_table)["auto_allowed"] is False
    pair["configuration"]["packages"] = {"value": [{**rows[0], "gift_package": None}]}
    assert _g285_mark("packages", pair, sample, "name_only", company_table)["auto_allowed"] is False
    pair["configuration"]["packages"] = {"value": [*rows, {**rows[0], "current_package": "占位包丙"}]}
    assert _g285_mark("packages", pair, sample, "name_only", company_table)["auto_allowed"] is False
    pair["configuration"]["packages"] = {"value": rows}
    pair["source_status"]["benefit_type"] = "冲突"
    for name in ("packages", "cancel_on_downgrade"):
        assert _g285_mark(name, pair, sample, "name_only", company_table)["auto_allowed"] is False


@pytest.mark.parametrize(("company_status", "keywords", "default_name", "review"), [
    ("原文未写", None, None, True),
    ("原文未写", None, "占位公司甲", False),
    ("原文未写", [], None, True),
    ("原文未写", [], "占位公司甲", False),
    ("原文未写", [], "占位公司乙", True),
    ("原文未写", ["SYNTHETIC-A"], None, False),
    ("原文未写", ["synthetic-a", "synthetic-b"], "占位公司甲", True),
    ("冲突", [], "占位公司甲", True),
])
def test_g285_rates_company_using_r10_table_and_frozen_facts(company_status, keywords, default_name, review):
    from qianshu.draft import SourceExcerpt
    from qianshu.parse.companies import CompanyRow
    from qianshu.regression.business import BusinessScore

    sample = {"country": "占位地区", "benefit_type": "赠送时长"}
    frozen = {"samples": {"policy": sample}, "denominators": {"fields": {"company": {"ids": ["policy"]}}}}
    company = "占位公司甲"
    rows = {"synthetic-a": CompanyRow(company, None), "synthetic-b": CompanyRow("占位公司乙", None)}
    if default_name:
        rows[""] = CompanyRow(default_name, None)
    pair = {"id": "policy", "configuration": {"company": {"value": company}},
            "source_status": {"company": company_status}}
    if keywords is not None:
        pair["company_keywords"] = keywords
    # 正文含其他政策的关键词。评分只取本条独立审定的命中结果。
    body = "占位地区\n其他政策 synthetic-a\n"
    score = BusinessScore(frozen, 2, {}, company_table={sample["country"]: rows})

    def rate(field):
        draft = SimpleNamespace(company=field, benefit_type=DraftField(value="赠送时长"), to_dict=lambda: {})
        return score.add({"body": body}, [pair], [draft])["policies"][0]["fields"]["company"]

    assert rate(DraftField(uncertainty=Uncertainty(reason="占位人工确认"))) == (
        "justified_confirmation" if review else "overasked"
    )
    assert rate(DraftField(value=company, evidence=[SourceExcerpt(text="占位地区", start=0)])) == (
        "false_determined" if review else "correct"
    )
    if not keywords:
        draft = SimpleNamespace(value=company, evidence=[], uncertainty=None, source_status="原文未写",
                                value_source="查表派生", evidence_verified=True)
        assert rate(draft) == ("false_determined" if review else "correct")
        assert rate(SimpleNamespace(**{**vars(draft), "value_source": "默认规则"})) == "false_determined"
        draft.evidence_verified = False
        assert rate(draft) == "false_determined"
        assert rate(DraftField(value=company)) == ("false_determined" if review else "correct")


@pytest.mark.parametrize("name", ["recharge_days", "gift_days"])
def test_g285_duration_fields_require_applicable_benefit_type(name):
    from qianshu.regression.business import _g285_mark

    pair = {"configuration": {name: {"value": 30}}}
    sample = {"benefit_type": "升级权益"}
    assert _g285_mark(name, pair, sample, None, None)["auto_allowed"] is True
    assert _g285_mark(name, pair, {"benefit_type": "特价优惠"}, None, None)["auto_allowed"] is False
    pair["source_status"] = {"benefit_type": "歧义"}
    assert _g285_mark(name, pair, sample, None, None)["auto_allowed"] is False


def freeze_disjoint_exam(corpus):
    assert main(["plan", "--corpus", *PLAN, "--all"]) == 0
    originals = {file: get_json(file) for file in (corpus / "wiki-corpus/docs").glob("*.json")}
    for path, document in originals.items():
        put_json(path, {**document, "plain_text": document["plain_text"].replace("占位", "排除")})
    assert main(["freeze", *FREEZE[:-1], ".private/exam"]) == 0
    for path, document in originals.items():
        put_json(path, document)


def peer_list(repo, dataset, bodies):
    put_json(repo / "peer.json", {
        "dataset": dataset,
        "documents": [body_hashes(body) for body in bodies],
    })
    return ["--exclude-frozen", ".private/exam", "--exclude-holdout-list", ".private/peer.json"]


def plan_holdout(dataset, exclusions, out=None):
    out = out or f".private/{dataset}-plan"
    sampling = ["--all"] if dataset == "blind" else []
    return main(["plan", "--corpus", PLAN[0], "--dataset", dataset, "--out", out, *sampling, *exclusions])


def test_freeze_packages_accepts_any_reviewed_subset_of_targeted_pool(repo, corpus, capsys):
    freeze_disjoint_exam(corpus)
    blind_bodies = ["占位单条原稿　\n", "其他盲测候选原稿"]
    exclusions = peer_list(repo, "blind", blind_bodies)
    assert plan_holdout("packages", exclusions) == 0
    path = repo / "packages-plan/plan.json"
    plan = get_json(path)
    assert plan["mode"] == "targeted" and plan["candidate_count"] == 2
    assert set(plan["candidates"]) == {"policy-a", "policy-b"}
    assert not {"size", "seed", "strata"} & plan.keys()
    assert all("rank_hash" not in candidate for candidate in plan["candidates"].values())

    # A changed candidate pool is rejected, and key order has no effect on the comparison.
    reversed_pool = dict(reversed(list(plan["candidates"].items())))
    items = {
        "policy-b": {
            "token": "doc-batch", "span": [BATCH.index(SECOND), len(BATCH)], "mention": "只写档位",
            "rows": [], "complete": False, "basis": "占位审定",
        },
    }
    review_path = repo / "package-review.json"
    put_json(review_path, {"schema": 1, "items": items})
    args = ["freeze-packages", "--corpus", PLAN[0], "--plan", ".private/packages-plan/plan.json",
            "--reviews", ".private/package-review.json", "--companies", ".private/companies.tsv",
            "--out", ".private/packages"]
    put_json(path, {**plan, "candidates": {"policy-b": reversed_pool["policy-b"]}, "candidate_count": 1})
    assert main([*args, *exclusions]) == 1
    assert "当前语料不符" in capsys.readouterr().err
    put_json(path, {**plan, "candidates": reversed_pool})

    different = peer_list(repo, "blind", [*blind_bodies, "新增盲测候选原稿"])
    assert main([*args, *different]) == 1
    assert "排除集指纹不符" in capsys.readouterr().err

    exclusions = peer_list(repo, "blind", blind_bodies)
    put_json(review_path, {"schema": 1, "items": {
        **items, "policy-c": {**items["policy-b"], "token": "doc-single", "span": [0, 4]},
    }})
    assert main([*args, *exclusions]) == 1
    assert "候选池之外" in capsys.readouterr().err
    assert not (repo / "packages").exists()
    put_json(review_path, {"schema": 1, "items": items})
    assert main([*args, *exclusions]) == 0
    with PrivateFiles(Path.cwd()).open() as files:
        frozen = freeze_module.load_freeze(files, Path("packages"), verify_sources=True)
    assert {sample["row_id"] for sample in frozen["samples"].values()} == {"policy-b"}


@pytest.mark.parametrize(("ambiguous_benefit", "ambiguous_pairing"), [(False, False), (True, False), (True, True)])
def test_freeze_and_replay_two_package_styles(repo, corpus, capsys, monkeypatch, ambiguous_benefit, ambiguous_pairing):
    from qianshu.draft import SourceExcerpt

    review = {
        "schema": 1,
        "items": {
            "policy-a": {"token": "doc-batch", "span": [0, BATCH.index(SECOND)], "mention": "具体包名",
                         "rows": [{"package": "占位包甲", "gift": "占位赠包甲"}],
                         "complete": True, "basis": "占位审定"},
            "policy-b": {"token": "doc-batch", "span": [BATCH.index(SECOND), len(BATCH)], "mention": "只写档位",
                         "rows": [], "complete": True, "basis": "占位审定"},
        },
    }
    if ambiguous_benefit:
        review["items"]["policy-a"]["source_status"] = {"benefit_type": "歧义"}
    use_reviewed_tags = ambiguous_benefit and not ambiguous_pairing
    if use_reviewed_tags:
        for item, tag in zip(review["items"].values(), ("占位标签甲", "占位标签乙"), strict=True):
            item["pairing"] = {"user_tags": [tag]}
    put_json(repo / "package-review.json", review)
    args = ["freeze-packages", "--corpus", ".private/archive", "--plan", ".private/packages-plan/plan.json",
            "--reviews", ".private/package-review.json",
            "--companies", ".private/companies.tsv", "--out", ".private/packages"]
    freeze_disjoint_exam(corpus)
    blind_exclusions = peer_list(repo, "packages", [BATCH])
    assert plan_holdout("blind", blind_exclusions) == 0
    blind_review = get_json(repo / "review.json")
    blind_review["reviews"] = {"policy-c": blind_review["reviews"]["policy-c"]}
    put_json(repo / "blind-review.json", blind_review)
    blind_inputs = [*FREEZE]
    blind_inputs[blind_inputs.index(".private/plan/plan.json")] = ".private/blind-plan/plan.json"
    blind_inputs[blind_inputs.index(".private/review.json")] = ".private/blind-review.json"
    blind_inputs[-1] = ".private/blind"
    assert main(["freeze", *blind_inputs, "--dataset", "blind", *blind_exclusions]) == 0
    blind_sha = capsys.readouterr().out.splitlines()[-1]
    assert main(args) == 1
    assert main([*args, *peer_list(repo, "blind", [])]) == 1
    package_exclusions = ["--exclude-frozen", ".private/exam", "--exclude-frozen", ".private/blind"]
    assert plan_holdout("packages", package_exclusions) == 0
    assert main([*args, *peer_list(repo, "blind", ["占位单条原稿\n"])]) == 1
    assert "排除集指纹不符" in capsys.readouterr().err
    assert main([*args, *package_exclusions]) == 0
    fingerprint = capsys.readouterr().out.strip()
    manifest = get_json(repo / "packages/manifest.json")
    assert manifest["excluded_sets_sha256"]["blind"] == blind_sha
    assert manifest["plan_sha256"] == digest((repo / "packages-plan/plan.json").read_bytes())
    assert main(["verify", "--frozen", ".private/packages", "--sources"]) == 0
    with PrivateFiles(Path.cwd()).open() as files:
        frozen = freeze_module.load_freeze(files, Path("packages"))
    pair = next(pair for pair in frozen["pairs"].values() if pair["row_id"] == "policy-a")
    assert pair.get("source_status") == ({"benefit_type": "歧义"} if ambiguous_benefit else None)
    assert frozen["denominators"]["fields"].keys() == {"packages"}

    def parsed(body, *, model, on_reply, **kwargs):
        on_reply(model(system="占位提示", user=json.dumps({"application": body})))
        base = {name: DraftField() for name in parse.ParsedPolicyDraft.__dataclass_fields__}
        named_package = DraftField(value=[PackageRow(current_package="占位包甲", gift_package="占位赠包甲")],
                                   evidence=[SourceExcerpt(text=FIRST, start=BATCH.index(FIRST))])
        if ambiguous_benefit:
            named_package = DraftField(uncertainty=Uncertainty(reason="占位优惠类型待确认"))
        return [parse.ParsedPolicyDraft(**{
            **base,
            "company": DraftField(value=company),
            "benefit_type": DraftField(value="赠送时长"),
            "packages": field,
            "user_tags": DraftField(value=[tag] if use_reviewed_tags else None),
        }) for company, field, tag in (
            ("占位默认公司", named_package, "占位标签甲"),
            ("占位默认公司", DraftField(uncertainty=Uncertainty(reason="占位仅档位")), "占位标签乙"),
        )]

    monkeypatch.setattr(runner, "parse_application", parsed)
    args = ["--frozen", ".private/packages", "--expect-sha256", fingerprint]
    assert main(["run", *args, "--out", ".private/package-run"], model=no_policies) == 0
    assert main(["replay", *args, "--recording", ".private/package-run", "--out", ".private/package-replay"]) == 0
    report = get_json(repo / "package-replay" / "public-report.json")
    assert report["business"]["rates"]["package_rows_exact"] == {
        "numerator": int(not ambiguous_benefit), "denominator": 1, "percent": 0.0 if ambiguous_benefit else 100.0,
    }
    assert report["business"]["rates"]["package_tier_confirmation"] == {
        "numerator": int(not ambiguous_pairing), "denominator": 1, "percent": 0.0 if ambiguous_pairing else 100.0,
    }
    assert report["business"]["by_field"]["packages"]["correct"] == int(not ambiguous_benefit)
    assert report["business"]["by_field"]["packages"]["justified_confirmation"] == (
        0 if ambiguous_pairing else 1 + ambiguous_benefit
    )
    assert report["business"]["by_field"]["packages"]["overasked"] == 0
    counts = report["business"]["counts"]
    expected_ambiguity = 2 if ambiguous_pairing else 0
    assert counts["tied_pairs"] == counts["ambiguous_drafts"] == expected_ambiguity
    markdown = (repo / "package-replay" / "public-report.md").read_text()
    assert f"配对歧义（政策数） {expected_ambiguity}" in markdown


def test_blind_freeze_persists_reviewed_business_marks_and_excludes_duplicate_documents(repo, corpus, capsys):
    freeze_disjoint_exam(corpus)
    review = get_json(repo / "review.json")
    review["reviews"]["policy-a"].update(
        business={"recharge_days": {"inferable": True, "auto_allowed": False}},
        pairing={"recharge_amount": "19.90"},
        card_complete=False,
        source_status={"effective_multiplier": "原文未写"},
    )
    put_json(repo / "review.json", review)
    assert main(["freeze", *FREEZE, "--dataset", "blind",
                 *peer_list(repo, "packages", ["排除用占位正文"])]) == 1
    assert "数据集或排除集指纹不符" in capsys.readouterr().err
    blind_inputs = [*FREEZE]
    blind_inputs[blind_inputs.index(".private/plan/plan.json")] = ".private/blind-plan/plan.json"
    args = ["freeze", *blind_inputs, "--dataset", "blind"]
    assert main(args) == 1
    assert main([*args, "--exclude-frozen", ".private/exam"]) == 1
    assert main([*args, *peer_list(repo, "packages", [])]) == 1
    assert plan_holdout("blind", peer_list(repo, "packages", ["排除用占位正文"])) == 0
    different = peer_list(repo, "packages", ["另一份排除占位正文"])
    assert plan_holdout("blind", different, out=".private/other-plan") == 0
    plan = get_json(repo / "blind-plan/plan.json")
    other = get_json(repo / "other-plan/plan.json")
    assert plan["strata"] == other["strata"] and plan["excluded_sets_sha256"] != other["excluded_sets_sha256"]
    assert main([*args, *different]) == 1
    assert "排除集指纹不符" in capsys.readouterr().err
    assert not (repo / "frozen").exists()
    exclusions = peer_list(repo, "packages", ["排除用占位正文"])
    path = repo / "blind-plan/plan.json"
    raw = path.read_bytes()
    max(plan["strata"], key=lambda layer: len(layer["candidates"]))["candidates"].reverse()
    put_json(path, plan)
    assert main([*args, *exclusions]) == 1
    assert "当前语料不符" in capsys.readouterr().err
    path.write_bytes(raw)
    assert main([*args, *exclusions]) == 0
    checksum = capsys.readouterr().out.splitlines()[-1]
    files = PrivateFiles(Path.cwd())
    with files.open():
        frozen = freeze_module.load_freeze(files, Path("frozen"))
    assert frozen["dataset"] == "blind" and frozen["fingerprint"] == checksum
    pair = next(pair for pair in frozen["pairs"].values() if pair["row_id"] == "policy-a")
    assert pair["business"] == {"recharge_days": {"inferable": True, "auto_allowed": False}}
    assert pair["pairing"]["recharge_amount"] == "19.90" and not pair["card_complete"]
    assert pair["source_status"] == {"effective_multiplier": "原文未写"}
    assert get_json(repo / "frozen/manifest.json")["excluded_sets_sha256"] == {
        "exam": digest((repo / "exam/manifest.json").read_bytes()),
        "packages": digest((repo / "peer.json").read_bytes()),
    }

    assert main(["run", *run_args(checksum)[1:]], model=no_policies) == 0
    assert main(_replay_args(checksum)) == 0
    assert (repo / "run/public-report.json").read_bytes() == (repo / "replay/public-report.json").read_bytes()

def test_amount_dates_and_candidates_only_count_derived_business_keys(monkeypatch):
    from qianshu.draft import Candidate, PackageRow
    from qianshu.regression.business import WEIGHTS, _draft_keys, _gold_keys, _score, pairing_fingerprint

    packages = [PackageRow(current_package="占位包甲")]
    gold = _gold_keys({
        "configuration": {
            "user_tags": {"value": ["占位标签乙", "占位标签甲"]},
            "packages": {"exclude": "gold_missing"},
            "benefit_type": {"exclude": "source_anomaly"},
            "gift_days": {"value": 30},
            "start_date": {"value": "2099-01-01"},
            "end_date": {"value": "2099-01-15"},
        },
        "pairing": {
            "user_tags": ["占位标签甲", "占位标签乙"],
            "packages": [{"current_package": "占位包甲", "gift_package": None}],
            "recharge_amount": "19.90",
        },
    }, {"country": "占位", "benefit_type": "赠送时长"})
    assert gold["user_tags"] == ["占位标签乙", "占位标签甲"]
    draft = SimpleNamespace(
        benefit_type=DraftField(value="赠送时长"),
        recharge_amount=DraftField(value=19.9),
        gift_days=DraftField(uncertainty=Uncertainty(reason="占位", candidates=[Candidate(value=30)])),
        user_tags=DraftField(value=gold["user_tags"], uncertainty=Uncertainty(reason="占位")),
        packages=DraftField(uncertainty=Uncertainty(reason="占位", candidates=[Candidate(value=packages)])),
        start_date=DraftField(value="2099-01-15"), end_date=DraftField(value="2099-02-01"),
    )
    assert _score(gold, _draft_keys(draft, {})) == ["recharge_amount", "date_overlap"]
    fingerprint = pairing_fingerprint(2)
    assert fingerprint != pairing_fingerprint(3)
    monkeypatch.setitem(WEIGHTS, "packages", 4)
    assert fingerprint != pairing_fingerprint(2)


def test_legacy_replay_projects_only_retired_shape(repo, frozen, monkeypatch, capsys):
    reply = fixed_reply(BATCH, [{"country": {"value": "赞比亚", "evidence": [FIRST]}}])
    fields = reply["policies"][0]["fields"]
    source_quote = fields["country"]["evidence"][0]
    source_quote["clause_id"] = "legacy"
    fields["user_scope"].update(status="歧义", candidates=[{
        "value": "合成范围", "evidence": [{**source_quote}],
    }], checked=[])
    reply["policies"][0]["shared_clause_ids"] = ["legacy"]
    reply.update(clauses=[], coverage=[], pending=[], document={"status": "材料不足", "reason_code": "fixture"})
    raw = json.dumps(reply, ensure_ascii=False)

    def model(*, system, user):
        return raw if json.loads(user)["application"] == BATCH else json.dumps({"policies": [], "document": None})

    assert main(run_args(frozen), model=model) == 0
    recording = repo / "run"
    manifest_path = recording / "recording.json"
    manifest = get_json(manifest_path)
    assert main([*_replay_args(frozen, out=".private/current-structure"), "--legacy-recording"]) == 1
    assert "指定的录制已使用当前回复结构" in capsys.readouterr().err
    manifest["configuration"].pop("reply_structure")
    names = {get_json(recording / name)["user_sha256"]: name for name in manifest["artifact_sha256"]}
    batch_name = names[digest(json.dumps({"application": BATCH}, ensure_ascii=False).encode())]
    batch_path = recording / batch_name
    batch = get_json(batch_path)
    second_name, = set(manifest["artifact_sha256"]) - {batch_name}
    second_path = recording / second_name
    second = get_json(second_path)
    second.update(reason_code="reply_document", structure_path=["document"])

    def save(path, record, source):
        record["attempts"][-1]["reply"] = source
        record["configuration"] = manifest["configuration"]
        put_json(path, record)
        manifest["artifact_sha256"][path.name] = digest(path.read_bytes())
        put_json(manifest_path, manifest)

    batch.update(failure_stage=None, reason_code=None, structure_path=[])
    save(batch_path, batch, raw)
    save(second_path, second, second["attempts"][-1]["reply"])
    source_bytes = batch_path.read_bytes()
    denominators = (repo / "frozen" / "denominators.json").read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("offline replay attempted model access")

    monkeypatch.setattr("qianshu.regression.__main__.configured_model", forbidden)
    assert main(_replay_args(frozen, out=".private/strict"), model=forbidden) == 0
    assert get_json(repo / "strict" / "public-report.json")["documents"]["parse_failed"] == 2

    assert main([*_replay_args(frozen), "--legacy-recording"], model=forbidden) == 0
    diagnostic = repo / "replay"
    report = get_json(diagnostic / "public-report.json")
    assert report["documents"] == {"parsed": 1, "parse_failed": 1, "model_failed": 0}
    assert report["samples"] == 3
    assert report["diagnostic_replay"] == {
        "reply_structure": "thin", "source_reply_structure": "w1d",
        "source_configuration_sha256": digest(json_bytes(manifest["configuration"])),
    }
    replay_manifest = get_json(diagnostic / "recording.json")
    assert replay_manifest["origin"] == "replay" and replay_manifest["origin_run_id"] == manifest["run_id"]
    assert get_json(diagnostic / batch_name)["attempts"][-1]["reply"] == raw
    assert get_json(diagnostic / second_name)["reason_code"] == "reply_document"
    assert batch_path.read_bytes() == source_bytes
    assert (repo / "frozen" / "denominators.json").read_bytes() == denominators

    batch_path.write_bytes(source_bytes + b"\n")
    assert main([*_replay_args(frozen, out=".private/changed-artifact"), "--legacy-recording"], model=forbidden) == 1
    assert "录制的回复文件指纹不一致" in capsys.readouterr().err
    batch_path.write_bytes(source_bytes)

    args = ["summarize", "--recording", ".private/replay", "--out", ".private/diagnostic"]
    assert main(args) == 1
    assert main([*args, "--diagnostic-replay"]) == 0
    assert get_json(repo / "diagnostic" / "public-summary.json")["diagnostic_replay"] is True

    wrong_core = json.loads(raw)
    wrong_core["policies"][0]["fields"]["country"]["value"] = 42
    for index, (response, code) in enumerate([
        (raw.removesuffix("}") + ', "extra": 1}', "reply_schema"),
        (raw.replace('"checked": []', '"checked": [], "checked": []'), "reply_json"),
        (raw.replace('"checked": []', '"checked": [1e999]'), "reply_json"),
        (json.dumps(wrong_core), "reply_schema"),
    ]):
        save(batch_path, batch, response)
        output = f"malformed-{index}"
        assert main([*_replay_args(frozen, out=f".private/{output}"), "--legacy-recording"], model=forbidden) == 0
        assert get_json(repo / output / batch_name)["reason_code"] == code

    prompt = repo.parent / "rules" / "parse-prompt.md"
    prompt.write_text(prompt.read_text() + "\n合成规则变更\n")
    assert main([*_replay_args(frozen, out=".private/changed-rules"), "--legacy-recording"], model=forbidden) == 1
    assert "录制时的规则文件" in capsys.readouterr().err
