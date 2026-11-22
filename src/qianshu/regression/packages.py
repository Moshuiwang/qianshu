"""冻结独立审定的节目包补充集。"""

from collections import Counter, defaultdict
from pathlib import Path

from .compare import BENEFIT_TYPES, POLICY_TYPES, normalize
from .corpus import PAIRS, document_body, identifier, table
from .freeze import _check_business, _check_spans, check_excluded, write_snapshot
from .storage import PrivateFiles, RegressionError, digest, json_bytes, read_json

MENTIONS = {
    "具体包名": "name_only", "只写包名": "name_only", "name_only": "name_only",
    "只写档位": "tier_only", "仅档位": "tier_only", "tier_only": "tier_only",
    "两类并写": "both", "both": "both",
}


def freeze_packages(
    files: PrivateFiles, corpus: Path, review: Path, companies_path: Path, destination: Path,
    *, exclusions: dict,
) -> str:
    sources = {}

    def read(path: Path) -> bytes:
        content = files.read(path)
        sources[path.as_posix()] = digest(content)
        return content

    reviewed = read_json(read(review))
    if reviewed["schema"] != 1 or not isinstance(reviewed["items"], dict) or not reviewed["items"]:
        raise RegressionError("节目包补充审定文件为空或版本无效。")
    index = defaultdict(set)
    for row in table(read(corpus / PAIRS)):
        index[row["政策 rowId"], row["原稿 token"]].add((row["国家"], row["政策类型"], row["优惠类型"]))
    by_token = defaultdict(list)
    samples, pairs, documents = [], [], []
    for row_id, item in reviewed["items"].items():
        identifier(row_id)
        token = identifier(item["token"])
        matches = index[row_id, token]
        if len(matches) != 1:
            raise RegressionError("节目包审定政策不能唯一对应语料索引。")
        (country, policy, benefit), = matches
        if not country or policy not in POLICY_TYPES or benefit not in BENEFIT_TYPES:
            raise RegressionError("节目包审定政策的分层信息无效。")
        style = MENTIONS.get(item["mention"])
        if style is None or type(item["complete"]) is not bool or not isinstance(item["basis"], str) or not item["basis"].strip():
            raise RegressionError("节目包补充审定的写法、枚举标记或依据无效。")
        rows = normalize("packages", [
            {"current_package": row["package"], "gift_package": row["gift"]} for row in item["rows"]
        ])
        if not rows and style != "tier_only":
            raise RegressionError("具体包名的节目包审定须提供行集合。")
        sample_id = digest(json_bytes([token, row_id]))
        samples.append({
            "id": sample_id, "row_id": row_id, "token": token, "country": country,
            "policy_type": policy, "benefit_type": benefit,
        })
        pair = {
            "id": sample_id, "row_id": row_id, "token": token, "span": item["span"], "basis": item["basis"],
            "configuration": {"packages": {"value": rows}},
            "package_style": style,
            "pairing": item.get("pairing", {}),
            **{key: item[key] for key in ("business", "source_status") if key in item},
        }
        _check_business(pair, {"packages"})
        pairs.append(pair)
        by_token[token].append((pair, item["complete"]))

    for token, records in by_token.items():
        doc = read_json(read(corpus / "wiki-corpus/docs" / f"{token}.json"))
        body = document_body(doc, token, {pair["row_id"] for pair, _ in records})
        _check_spans([pair["span"] for pair, _ in records], body)
        complete = {mark for _, mark in records}
        if len(complete) != 1:
            raise RegressionError("同篇节目包审定的全文枚举标记冲突。")
        styles = {pair["package_style"] for pair, _ in records}
        documents.append({
            "token": token, "body": body, "other_spans": [], "complete": complete.pop(),
            "package_style": styles.pop() if len(styles) == 1 else "both",
        })
    excluded_set_hashes = check_excluded(documents, "packages", exclusions)
    companies = read(companies_path)
    if not companies.decode().strip():
        raise RegressionError("冻结所用的公司对照表为空。")
    counts = Counter((sample["policy_type"], sample["benefit_type"]) for sample in samples)
    manifest = {
        "schema": 3, "comparison_version": 1, "dataset": "packages", "count": len(samples),
        "target": len(samples), "documents": len(documents), "source_sha256": sources,
        "excluded_sets_sha256": excluded_set_hashes,
        "strata": [{"policy_type": policy, "benefit_type": benefit, "quota": count}
                   for (policy, benefit), count in sorted(counts.items())],
    }
    return write_snapshot(files, destination, manifest, samples, pairs, documents, companies, ("packages",))
