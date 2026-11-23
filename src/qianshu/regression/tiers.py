"""从已验指纹的冻结考题按整篇选择快检集。"""

from collections import defaultdict

from .freeze import _denominators
from .storage import RegressionError, digest, json_bytes

SIZE = 25
SEED = "040-quick-v1"


def select(snapshot, annotations):
    if snapshot["dataset"] != "exam":
        raise RegressionError("快检只能从冻结考题选篇。")
    docs = snapshot["documents"]
    if set(annotations) - docs.keys():
        raise RegressionError("标签登记引用了其他题集的篇目。")
    groups = defaultdict(set)
    samples = defaultdict(list)
    for row in snapshot["samples"].values():
        token = row["token"]
        samples[token].append(row)
        groups["组合", row["policy_type"], row["benefit_type"]].add(token)
    mandatory = set()
    for token, doc in docs.items():
        rows = samples[token]
        labels = annotations.get(token, {})
        if labels.get("required"):
            mandatory.add(token)
        for key, value in (
            ("国家", "多" if len({row["country"] for row in rows}) > 1 else "单"),
            ("政策", "多" if len(rows) > 1 else "单"),
            ("附件", labels.get("attachment", "未知")),
            ("节目包", labels.get("package_style", doc.get("package_style", "未知"))),
        ):
            groups[key, value].add(token)
        for value in labels.get("history", ["未知"]):
            groups["历史", value].add(token)
    def rank(token):
        return digest(json_bytes([SEED, token]))
    selected = set(sorted(mandatory, key=rank)[:SIZE])
    for _, group in sorted(groups.items(), key=lambda item: (len(item[1]), item[0])):
        if len(selected) < SIZE and not selected.intersection(group):
            selected.add(min(group, key=rank))
    selected.update(sorted(docs.keys() - selected, key=rank)[:SIZE - len(selected)])
    coverage = [{"layer": key, "available": len(group), "selected": len(group & selected)}
                for key, group in sorted(groups.items())]
    return {"seed": SEED, "parent": snapshot["fingerprint"], "documents": sorted(selected),
            "samples": sorted(row["id"] for token in selected for row in samples[token]),
            "labels_sha256": digest(json_bytes(annotations)), "coverage": coverage,
            "required_missing": len(mandatory - selected)}


def project(snapshot, selection):
    if (selection["parent"] != snapshot["fingerprint"] or snapshot["dataset"] != "exam"
        or selection["seed"] != SEED or len(selection["documents"]) != SIZE
        or len(set(selection["documents"])) != SIZE or selection["required_missing"]
        or any(not row["selected"] for row in selection["coverage"])):
        raise RegressionError("快检父指纹、篇数、必选篇或分层覆盖未通过。")
    return _subset(snapshot, selection)


def attachment_subset(snapshot, selection):
    docs = selection.get("documents")
    if (snapshot["dataset"] != "exam" or selection.get("parent") != snapshot["fingerprint"]
            or not isinstance(docs, list) or not 1 <= len(docs) <= 5
            or len(set(docs)) != len(docs) or set(docs) - snapshot["documents"].keys()):
        raise RegressionError("附件快检需从同一冻结题本指定一至五篇。")
    return _subset(snapshot, selection)


def trial(snapshot, size):
    tokens = sorted(snapshot["documents"])[:size]
    if not tokens:
        raise RegressionError("试通没有篇目。")
    return _subset(snapshot, {"parent": snapshot["fingerprint"], "trial": size, "documents": tokens,
                              "samples": sorted(k for k, v in snapshot["samples"].items() if v["token"] in tokens)})


def _subset(snapshot, selection):
    docs = {token: snapshot["documents"][token] for token in selection["documents"]}
    samples = {k: v for k, v in snapshot["samples"].items() if v["token"] in docs}
    if sorted(samples) != selection["samples"]:
        raise RegressionError("快检样例与整篇选择不一致。")
    pairs = {k: snapshot["pairs"][k] for k in samples}
    strata = []
    for layer in snapshot["strata"]:
        count = sum((r["policy_type"], r["benefit_type"]) == (layer["policy_type"], layer["benefit_type"])
                    for r in samples.values())
        strata.append({**layer, "quota": count, "included_count": count, "shortfall": 0})
    return {**snapshot, "fingerprint": digest(json_bytes(selection)), "documents": docs,
            "samples": samples, "pairs": pairs, "denominators": _denominators(
                list(pairs.values()), snapshot["denominators"]["fields"]),
            "strata": strata}
