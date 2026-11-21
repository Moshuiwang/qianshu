"""读取私有语料索引、按层排名，并从审定过的来源提取可比配置。"""

import csv
import io
import re
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from .compare import BENEFIT_TYPES, EXCLUSIONS, FIELD_LABELS, POLICY_TYPES, normalize
from .storage import PrivateFiles, RegressionError, digest, read_json

SEED = "qianshu-r4-v1"
CANDIDATES = Path("policy-corpus/analysis/exam-candidates.tsv")
PAIRS = Path("wiki-corpus/pairs.tsv")
FIELDS = Path("policy-corpus/analysis/field-reference.json")
_ID = re.compile(r"[A-Za-z0-9_-]+")
_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)")
_RELATION_ID_KEYS = {"rowId", "rowid", "sid"}
_COMPANY_MAPPING_CONFLICT = object()


def identifier(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise RegressionError("语料索引含无效标识。")
    return value


def table(raw: bytes):
    return csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), delimiter="\t")


def make_plan(files: PrivateFiles, corpus: Path, size: int | None) -> dict:
    candidate_data = files.read(corpus / CANDIDATES)
    pair_data = files.read(corpus / PAIRS)
    links = defaultdict(set)
    for row in table(pair_data):
        links[row["政策 rowId"], row["原稿 token"]].add(
            (row["国家"], row["政策类型"], row["优惠类型"]),
        )

    strata = defaultdict(list)
    seen = set()
    for row in table(candidate_data):
        row_id, token = identifier(row["政策 rowId"]), identifier(row["原稿 token"])
        country = row["国家"]
        policy, benefit = row["类型"].split(" × ")
        if (
            row_id in seen
            or not country or policy not in POLICY_TYPES or benefit not in BENEFIT_TYPES
            or links[row_id, token] != {(country, policy, benefit)}
        ):
            raise RegressionError("候选清单与原稿配对索引冲突或含重复政策。")
        seen.add(row_id)
        strata[policy, benefit].append({
            "row_id": row_id, "token": token, "country": country, "shape": row["原稿形态"],
            "rank_hash": digest(f"{SEED}\0{row_id}".encode()),
        })

    total = len(seen)
    target = total if size is None else size
    if not 0 < target <= total:
        raise RegressionError(f"抽样规模必须介于 1 和候选总数 {total} 之间。")

    # 最大余数法，用整数计算余数，层名打破并列。
    quotas = {key: target * len(rows) // total for key, rows in strata.items()}
    remainders = sorted(strata, key=lambda key: (-(target * len(strata[key]) % total), key))
    for key in remainders[:target - sum(quotas.values())]:
        quotas[key] += 1

    return {
        "schema": 1, "seed": SEED, "mode": "all" if size is None else "sample",
        "size": target, "candidate_count": total,
        "source_hashes": {str(CANDIDATES): digest(candidate_data), str(PAIRS): digest(pair_data)},
        "strata": [
            {"policy_type": key[0], "benefit_type": key[1], "quota": quotas[key],
             "candidates": sorted(rows, key=lambda row: (row["rank_hash"], row["row_id"]))}
            for key, rows in sorted(strata.items())
        ],
    }


def select(plan: dict, reviews: dict) -> tuple[list[dict], list[dict]]:
    selected = []
    rejected = []
    candidates = {row["row_id"] for layer in plan["strata"] for row in layer["candidates"]}
    if not reviews.keys() <= candidates:
        raise RegressionError("审定记录含候选清单之外的政策。")
    for layer in plan["strata"]:
        chosen = 0
        for rank, row in enumerate(layer["candidates"], start=1):
            if plan["mode"] == "sample" and chosen == layer["quota"]:
                break
            review = reviews.get(row["row_id"])
            if review is None:
                pending = layer["quota"] - chosen
                raise RegressionError(f"抽样顺序中仍有未审定候选，当前层尚需 {pending} 条。")
            if "exclude" in review:
                reason = review["exclude"]
                if reason not in EXCLUSIONS:
                    raise RegressionError("候选排除原因无效。")
                rejected.append({"row_id": row["row_id"], "rank": rank, "reason": reason})
                continue
            chosen += 1
            selected.append({
                **row, "policy_type": layer["policy_type"], "benefit_type": layer["benefit_type"],
                "rank": rank, "quota": layer["quota"],
                "selection": "replacement" if rank > layer["quota"] else "included",
            })
    if not selected:
        raise RegressionError("没有审定纳入的考题。")
    return selected, rejected


def _packed(raw: object) -> object:
    if isinstance(raw, str) and raw.lstrip().startswith(("[", "{")):
        return read_json(raw.encode())
    return raw


def _options(field: dict, overrides: dict) -> dict:
    registered = {}
    for option in field.get("options", []):
        key = str(option["key"])
        if key in registered:
            raise RegressionError("字段字典含重复选项。")
        registered[key] = option["value"]
    if any(key in registered and registered[key] != val for key, val in overrides.items()):
        raise RegressionError("字段私有选项映射与字典冲突。")
    return {**registered, **overrides}


def _count(raw: object) -> int | None:
    raw = _packed(raw)
    if type(raw) is int and raw >= 0:
        return raw
    if isinstance(raw, str) and re.fullmatch(r"0|[1-9][0-9]*", raw):
        return int(raw)
    if isinstance(raw, list):
        if len(raw) == 1 and isinstance(raw[0], dict) and "count" in raw[0]:
            return _count(raw[0]["count"])
        return len(raw)
    if isinstance(raw, dict):
        for key in ("count", "total"):
            if key in raw:
                return _count(raw[key])
        if raw.keys() & _RELATION_ID_KEYS:
            return 1
    return None


def _relation_id(value: object) -> str:
    return identifier(value).lower()


def _relation_ids(raw: object, name: str) -> set[str] | None:
    if not isinstance(raw, (list, dict)):
        return None
    items = raw if isinstance(raw, list) else [raw]
    if not any(
        isinstance(item, str) or isinstance(item, dict) and item.keys() & _RELATION_ID_KEYS
        for item in items
    ):
        return None
    ids = set()
    for item in items:
        keys = item.keys() & _RELATION_ID_KEYS if isinstance(item, dict) else set()
        values = {_relation_id(item)} if isinstance(item, str) else {_relation_id(item[key]) for key in keys}
        if len(values) != 1 or values & ids:
            raise RegressionError(f"字段 {name} 的主记录关联 ID 不完整或冲突，请复核。")
        ids.update(values)
    return ids


def _linked_name(raw: object) -> str | None:
    raw = _packed(raw)
    if raw == []:
        return None
    if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], dict):
        value = raw[0].get("name") or raw[0].get("title")
        if isinstance(value, str) and value.strip():
            return value
    raise RegressionError("节目包关联未能确定唯一完整名称。")


class Corpus:
    def __init__(self, files: PrivateFiles, root: Path, review: dict):
        self.files = files
        self.root = root
        self.mapping = review.get("mapping", {})
        self.company_names = {}
        for row_id, name in self.mapping.get("company_names", {}).items():
            company_id = _relation_id(row_id)
            if self.company_names.setdefault(company_id, name) != name:
                self.company_names[company_id] = _COMPANY_MAPPING_CONFLICT
        self.read_hashes = {}
        reference = read_json(self.read(FIELDS))
        self.fields = defaultdict(list)
        for field in reference["fields"]:
            self.fields[field["name"], field["type"]].append(field)

    def read(self, relative: Path) -> bytes:
        path = self.root / relative
        raw = self.files.read(path)
        checksum = digest(raw)
        prior = self.read_hashes.setdefault(path.as_posix(), checksum)
        if prior != checksum:
            raise RegressionError("冻结期间语料来源发生变化。")
        return raw

    def document(self, token: str, ids: set[str]) -> str:
        doc = read_json(self.read(Path("wiki-corpus/docs") / f"{token}.json"))
        if (
            doc["complete"] is not True or doc["token"] != token
            or not ids <= set(doc["policy_row_ids"]) or not isinstance(doc["plain_text"], str)
            or not doc["plain_text"].strip()
        ):
            raise RegressionError("原稿快照缺失、不完整或与审定配对不符。")
        return doc["plain_text"]

    def configuration(self, sample: dict, decisions: dict) -> dict:
        row_id = sample["row_id"]
        raw = self.read(Path("policy-corpus/rows") / f"{row_id}.json")
        row = read_json(raw, parse_float=Decimal)
        if row["complete"] is not True or row["data"].get("rowId") != row_id:
            raise RegressionError("人工配置快照不完整或与样例不符。")
        if decisions.keys() != FIELD_LABELS.keys():
            raise RegressionError("每条纳入的样例须逐一审定所有可比字段。")

        result = {}
        company_conflict = False
        for name, decision in decisions.items():
            if decision in EXCLUSIONS:
                result[name] = {"exclude": decision}
                continue
            if decision is not True:
                raise RegressionError(f"字段 {name} 的审定须为 true 或预定义的排除原因。")
            matches = self.fields[FIELD_LABELS[name]]
            if len(matches) != 1:
                raise RegressionError(f"字段 {name} 无唯一的字典映射。")
            field = matches[0]
            source = row["data"].get(identifier(field["id"]))
            value = self.decode(name, row_id, field, source)
            if value is _COMPANY_MAPPING_CONFLICT:
                company_conflict = True
                result[name] = {"exclude": "source_anomaly"}
                continue
            try:
                value = normalize(name, value)
            except ValueError:
                raise RegressionError(f"字段 {name} 的配置值不可比，请复核审定。") from None
            if name in {"policy_type", "benefit_type"} and value != sample[name]:
                raise RegressionError(f"字段 {name} 的配置快照与抽样分层冲突。")
            result[name] = {"value": value}
        if not company_conflict and not any("value" in entry for entry in result.values()):
            raise RegressionError("纳入样例至少须有一个可比字段。")
        return result

    def decode(self, name: str, row_id: str, field: dict, raw: object) -> object:
        kind = field["type"]
        if kind in ("SingleSelect", "Dropdown"):
            encoded = _packed(raw)
            options = _options(field, self.mapping.get("options", {}).get(name, {}))
            if isinstance(encoded, list) and len(encoded) == 1:
                encoded = encoded[0]
            if isinstance(encoded, dict):
                label = options.get(str(encoded.get("key")))
                if "value" in encoded and encoded["value"] != label:
                    return None
                return label
            if type(encoded) in (str, int):
                return options.get(str(encoded))
            return None
        if kind in ("Relation", "SubTable"):
            return self.relation(name, row_id, field["id"], raw)
        if name in ("recharge_days", "gift_days") and isinstance(raw, str) and _INTEGER.fullmatch(raw):
            return int(raw)
        return raw

    def relation(self, name: str, row_id: str, field_id: str, raw: object) -> object:
        raw = _packed(raw)
        count = _count(raw)
        expected_ids = _relation_ids(raw, name)
        if expected_ids is not None and len(expected_ids) != count:
            raise RegressionError(f"字段 {name} 的主记录关联 ID 与计数不符，请复核。")
        if count == 0:
            return [] if name in {"user_tags", "packages"} else None
        if count is None:
            return None
        relative = Path("policy-corpus/relations") / f"{row_id}.{field_id}.json"
        link = read_json(self.read(relative))
        rows = link["rows"]
        if (
            link["complete"] is not True or not isinstance(rows, list) or len(rows) != count
            or any(type(link[key]) is not int or link[key] != count for key in ("total", "expected_on_detail"))
        ):
            raise RegressionError(f"字段 {name} 的关联分页不完整或计数不符。")
        if expected_ids is not None:
            actual_ids = {_relation_id(row.get("rowId")) for row in rows if isinstance(row, dict)}
            if actual_ids != expected_ids:
                raise RegressionError(f"字段 {name} 的关联明细与主记录 ID 不一致，请复核。")
        if name == "company":
            if count != 1:
                return None
            return self.company_names.get(_relation_id(rows[0]["rowId"]))
        columns = self.mapping.get("relation_columns", {})
        if name == "user_tags":
            return [row[columns["user_tags"]] for row in rows]
        return [
            {"current_package": _linked_name(row[columns["current_package"]]),
             "gift_package": _linked_name(row[columns["gift_package"]])}
            for row in rows
        ]
