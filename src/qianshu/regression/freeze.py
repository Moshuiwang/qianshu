"""冻结考题、配对的人工配置和各字段的分母。"""

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from .compare import BENEFIT_TYPES, EXCLUSIONS, FIELD_LABELS, POLICY_TYPES, normalize
from .corpus import Corpus, make_plan, select
from .storage import PrivateFiles, RegressionError, digest, json_bytes, read_json

ARTIFACTS = ("samples.jsonl", "pairs.jsonl", "documents.jsonl", "denominators.json", "companies.tsv")
FREEZE_FILES = (*ARTIFACTS, "manifest.json", "audit.json")
PACKAGE_STYLES = {
    "tier_only": "只写档位",
    "name_only": "只写包名",
    "both": "两类并写",
    "neither_recognized": "两类都没认出",
}


def _json_lines(items, key: str) -> bytes:
    return b"".join(json_bytes(item) for item in sorted(items, key=lambda item: item[key]))


def _valid_span(span: list[int], body: str) -> bool:
    return (
        isinstance(span, list) and len(span) == 2 and all(type(offset) is int for offset in span)
        and 0 <= span[0] < span[1] <= len(body) and bool(body[slice(*span)].strip())
    )


def _check_spans(spans: list[list[int]], body: str) -> None:
    if not all(_valid_span(span, body) for span in spans):
        raise RegressionError("审定段落偏移超出原稿范围或正文为空。")
    ordered = sorted(spans)
    if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
        raise RegressionError("同一原稿的审定政策段落重叠。")


def _denominators(pairs: list[dict], fields: Iterable[str]) -> dict:
    result = {}
    for name in fields:
        eligible, excluded = [], {}
        for pair in sorted(pairs, key=lambda pair: pair["id"]):
            field = pair["configuration"][name]
            if "value" in field:
                eligible.append(pair["id"])
            else:
                excluded[pair["id"]] = field["exclude"]
        result[name] = {"ids": eligible, "excluded": excluded}
    return {
        "fields": result,
        "whole_policy": sorted(set.intersection(*(set(entry["ids"]) for entry in result.values()))),
    }


def freeze(files: PrivateFiles, corpus_root: Path, plan_path: Path, review_path: Path,
           companies_path: Path, destination: Path) -> str:
    plan_raw = files.read(plan_path)
    plan = read_json(plan_raw)
    current = make_plan(files, corpus_root, None if plan["mode"] == "all" else plan["size"])
    if plan != current:
        raise RegressionError("候选计划与当前语料不符，请复核后重新生成计划。")
    review_raw = files.read(review_path)
    review = read_json(review_raw)
    if review["schema"] != 1 or not isinstance(review["reviewer_role"], str) or not review["reviewer_role"].strip():
        raise RegressionError("审定资料须含受支持的版本及审定人角色。")
    selected, rejected = select(plan, review["reviews"])
    companies = files.read(companies_path)
    if not companies.decode("utf-8").strip():
        raise RegressionError("冻结所用的公司对照表为空。")

    corpus = Corpus(files, corpus_root, review)
    by_token = defaultdict(list)
    for row in selected:
        by_token[row["token"]].append(row)

    samples, pairs, documents = [], [], []
    for token, rows in sorted(by_token.items()):
        original = corpus.document(token, {row["row_id"] for row in rows})
        document_review = review.get("documents", {}).get(token, {})
        other_spans = document_review.get("other_spans", [])
        complete = document_review.get("complete", False)
        package_style = document_review.get("package_style")
        if package_style not in PACKAGE_STYLES:
            raise RegressionError("每篇纳入的原稿须审定节目包写法类别。")
        if type(complete) is not bool or not isinstance(other_spans, list):
            raise RegressionError("批次段落审定格式无效。")
        spans = [review["reviews"][row["row_id"]]["span"] for row in rows]
        _check_spans([*spans, *other_spans], original)
        documents.append({
            "token": token, "body": original, "other_spans": sorted(other_spans), "complete": complete,
            "package_style": package_style,
        })
        for row, span in zip(rows, spans, strict=True):
            row_id = row["row_id"]
            audit = review["reviews"][row_id]
            if not isinstance(audit["basis"], str) or not audit["basis"].strip():
                raise RegressionError("配对审定须注明独立核对的依据。")
            materials = sorted(set(audit.get("materials", [])))
            for path in materials:
                corpus.read(Path(path))
            configuration = corpus.configuration(row, audit["fields"])
            sample_id = digest(json_bytes([token, row_id]))
            samples.append({"id": sample_id, **row})
            pairs.append({
                "id": sample_id, "row_id": row_id, "token": token, "span": span, "basis": audit["basis"],
                "materials": materials, "configuration": configuration,
            })

    source_hashes = {str(corpus_root / name): checksum for name, checksum in plan["source_hashes"].items()}
    source_hashes.update(corpus.read_hashes)
    source_hashes.update({
        str(plan_path): digest(plan_raw), str(review_path): digest(review_raw), str(companies_path): digest(companies),
    })
    artifacts = {
        "samples.jsonl": _json_lines(samples, "id"),
        "pairs.jsonl": _json_lines(pairs, "id"),
        "documents.jsonl": _json_lines(documents, "token"),
        "denominators.json": json_bytes(_denominators(pairs, FIELD_LABELS)),
        "companies.tsv": companies,
    }
    manifest = {
        "schema": 2, "comparison_version": 1,
        "reviewer_role": review["reviewer_role"], "mode": plan["mode"], "seed": plan["seed"],
        "plan_sha256": digest(plan_raw), "count": len(samples), "documents": len(documents),
        "candidate_count": plan["candidate_count"], "target": plan["size"],
        "strata": [
            {"policy_type": layer["policy_type"], "benefit_type": layer["benefit_type"],
             "quota": layer["quota"], "candidate_count": len(layer["candidates"])}
            for layer in plan["strata"]
        ],
        "rejected": rejected, "source_sha256": source_hashes,
        "artifact_sha256": {name: digest(raw) for name, raw in artifacts.items()},
    }
    manifest_raw = json_bytes(manifest)
    with files.new_output(destination, FREEZE_FILES) as output:
        for name, raw in artifacts.items():
            output.write(name, raw)
        output.write("audit.json", json_bytes({"created_utc": datetime.now(UTC).isoformat(timespec="seconds")}))
        output.write("manifest.json", manifest_raw)
    return digest(manifest_raw)


def _indexed(raw: bytes, key: str) -> dict:
    lines = [read_json(line) for line in raw.splitlines()]
    indexed = {item[key]: item for item in lines}
    if not lines or len(indexed) != len(lines) or _json_lines(lines, key) != raw:
        raise RegressionError("冻结清单为空、含重复标识或文件格式不符。")
    return indexed


def load_freeze(files: PrivateFiles, directory: Path, *, verify_sources: bool = False) -> dict:
    manifest_raw = files.read(directory / "manifest.json")
    manifest = read_json(manifest_raw)
    if manifest["schema"] not in (1, 2) or manifest["comparison_version"] != 1 or json_bytes(manifest) != manifest_raw:
        raise RegressionError("冻结清单版本或清单格式不受支持。")
    raw = {name: files.read(directory / name) for name in ARTIFACTS}
    if {name: digest(data) for name, data in raw.items()} != manifest["artifact_sha256"]:
        raise RegressionError("冻结产物指纹校验失败。")

    samples = _indexed(raw["samples.jsonl"], "id")
    pairs = _indexed(raw["pairs.jsonl"], "id")
    documents = _indexed(raw["documents.jsonl"], "token")
    denominators = read_json(raw["denominators.json"])
    frozen_fields = denominators["fields"].keys()
    if not frozen_fields or not frozen_fields <= FIELD_LABELS.keys():
        raise RegressionError("冻结的可比字段不受支持。")
    if (
        samples.keys() != pairs.keys()
        or {row["token"] for row in samples.values()} != documents.keys()
        or len(samples) != manifest["count"] or len(documents) != manifest["documents"]
    ):
        raise RegressionError("冻结的样例、配对与文档计数或引用不一致。")
    by_token = defaultdict(list)
    for sample_id, pair in pairs.items():
        sample = samples[sample_id]
        if (
            (sample["row_id"], sample["token"]) != (pair["row_id"], pair["token"])
            or pair["configuration"].keys() != frozen_fields
            or sample["benefit_type"] not in BENEFIT_TYPES or sample["policy_type"] not in POLICY_TYPES
            or not isinstance(sample["country"], str) or not sample["country"]
        ):
            raise RegressionError("冻结的配置配对与样例清单不一致。")
        by_token[pair["token"]].append(pair["span"])
        for name, field in pair["configuration"].items():
            if field.keys() == {"value"} and normalize(name, field["value"]) == field["value"]:
                continue
            if field.keys() != {"exclude"} or field["exclude"] not in EXCLUSIONS:
                raise RegressionError("冻结的配置字段值或排除标记无效。")
    for token, doc in documents.items():
        _check_spans([*by_token[token], *doc["other_spans"]], doc["body"])
        if manifest["schema"] == 2 and doc.get("package_style") not in PACKAGE_STYLES:
            raise RegressionError("冻结的节目包写法类别无效。")

    if json_bytes(_denominators(list(pairs.values()), frozen_fields)) != raw["denominators.json"]:
        raise RegressionError("冻结的字段分母与审定配置不一致。")
    if verify_sources:
        for name, checksum in manifest["source_sha256"].items():
            if digest(files.read(name)) != checksum:
                raise RegressionError("冻结的语料来源指纹已变化。")
    return {
        "fingerprint": digest(manifest_raw), "samples": samples, "pairs": pairs, "documents": documents,
        "denominators": denominators, "companies_text": raw["companies.tsv"].decode("utf-8"),
    }
