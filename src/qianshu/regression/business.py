"""按冻结业务键配对，并按独立审定的字段分母评分。"""

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation

from qianshu.parse.companies import CompanyTable

from .compare import compare_field, normalize
from .storage import RegressionError, digest, json_bytes

WEIGHTS = {
    "country_or_company": 4, "benefit_type": 2, "packages": 4, "recharge_days": 2,
    "gift_days": 2, "recharge_amount": 2, "user_tags": 3, "date_overlap": 2,
}
DEFAULT_THRESHOLD = 2
CATEGORIES = ("correct", "false_determined", "overasked", "justified_confirmation", "missing", "unknown")
LEGACY_DEFAULTS = {
    "maintain_mutual_exclusion_group": "是", "trigger_once": "否",
    "repeat_bonus": "是", "effective_multiplier": 1,
}
G285_MAPPING = {
    "version": "G285",
    "datasets": ["exam", "blind", "packages"],
    "inferable": "included_frozen_fields",
    "excluded": "outside_denominator",
    "confirmation": {
        "source_statuses": ["歧义", "冲突", "带条件", "缺证据"],
        "unwritten_input_status": "原文未写",
        "required_without_default": ["start_date", "end_date", "recharge_days", "gift_days", "packages", "user_scope"],
        "directed_missing_tags": "定向政策",
        "policy_name": {"input": "user_tags", "required_count": 1, "must_match_tag": True},
        "policy_type": {"input": "user_tags", "empty": "公开政策", "nonempty": "定向政策"},
        "company": {
            "table_key": "country", "keyword_matches": "company_keywords", "default_key": "",
            "unwritten_without_matches": "requires_default", "default_source": "查表派生",
        },
        "trigger_once": "是",
        "effective_multiplier": {"only_automatic_value": 1},
        "cancel_on_downgrade": {"input": "benefit_type", "benefit_type": "赠送时长", "source_status": "原文未写"},
        "days_by_benefit": {
            "input": "benefit_type", "fields": ["recharge_days", "gift_days"], "types": ["赠送时长", "升级权益"],
        },
        "packages": {"input": "benefit_type", "package_style": "tier_only", "required_rows": 1, "required_gift": True},
    },
    "defaults": {
        **LEGACY_DEFAULTS,
        "cancel_on_downgrade": {"升级权益": "是", "特价优惠": "是"},
    },
    "verified_matching_default": "correct",
}
SOURCE_STATUSES = frozenset((*G285_MAPPING["confirmation"]["source_statuses"], "确定", "原文未写"))
METRIC_LABELS = {
    "all_correct": "可比字段全对率",
    "safe_confirmation": "安全可确认率",
    "false_determined": "错判确定率",
    "overasked": "多问率",
    "zero_edit": "卡片零修改可确认率",
    "package_rows_exact": "具体包名的行集合完全一致率",
    "package_tier_confirmation": "仅档位的需人工确认率",
}
COUNT_LABELS = {
    "unknown_fields": "未知字段项",
    "missing_policies": "漏条",
    "extra_policies": "多条",
    "unclassified_drafts": "额外草稿归属未知",
    "tied_pairs": "同分配对",
    "matched_policies": "业务配对成功",
    "comparable_policies": "配对成功且有可评分字段",
    "reply_parse_failed": "回复解析失败篇数",
}
PAIR_FIELDS = ("company", "benefit_type", "packages", "recharge_days", "gift_days", "recharge_amount",
               "user_tags", "start_date", "end_date")


def pairing_fingerprint(threshold: int) -> str:
    if not 1 <= threshold <= sum(WEIGHTS.values()):
        raise RegressionError("业务配对阈值超出支持范围。")
    return digest(json_bytes({
        "version": 1, "weights": WEIGHTS, "threshold": threshold,
        "tie_order": ["frozen_policy", "draft", "unmatched"],
    }))


def measure_fingerprint(threshold: int) -> str:
    return digest(json_bytes({"pairing_sha256": pairing_fingerprint(threshold), "mapping": G285_MAPPING}))


def _assignment(scores: list[list[int | None]], *, ordered: bool = False) -> dict[int, int]:
    """矩形 Hungarian。每条金标额外有一个零分的空位可供使用。"""
    rows = len(scores)
    if not rows or not scores[0]:
        return {}
    drafts = len(scores[0])
    columns = drafts + rows
    base = drafts + 1
    scale = base ** rows if ordered else 1
    places = [base ** (rows - i - 1) if ordered else 0 for i in range(rows)]

    def cost(row: int, col: int) -> int:
        score = scores[row][col] if col < drafts else None
        if score is None:
            return 0
        return -(score * scale + (drafts - col) * places[row])

    row_potential = [0] * (rows + 1)
    column_potential = [0] * (columns + 1)
    owners = [0] * (columns + 1)
    previous = [0] * (columns + 1)
    for row in range(1, rows + 1):
        owners[0] = row
        col = 0
        distance = [None] * (columns + 1)
        used = [False] * (columns + 1)
        while True:
            used[col] = True
            active = owners[col]
            step, next_col = None, 0
            for candidate in range(1, columns + 1):
                if used[candidate]:
                    continue
                reduced = cost(active - 1, candidate - 1) - row_potential[active] - column_potential[candidate]
                if distance[candidate] is None or reduced < distance[candidate]:
                    distance[candidate] = reduced
                    previous[candidate] = col
                if step is None or distance[candidate] < step:
                    step, next_col = distance[candidate], candidate
            for candidate in range(columns + 1):
                if used[candidate]:
                    row_potential[owners[candidate]] += step
                    column_potential[candidate] -= step
                elif distance[candidate] is not None:
                    distance[candidate] -= step
            col = next_col
            if owners[col] == 0:
                break
        while col:
            prior = previous[col]
            owners[col] = owners[prior]
            col = prior
    return {
        owners[col] - 1: col - 1 for col in range(1, drafts + 1)
        if owners[col] and scores[owners[col] - 1][col - 1] is not None
    }


def optimal_pairs(scores: list[list[int | None]]) -> tuple[dict[int, int], set[int]]:
    selected = _assignment(scores, ordered=True)
    total = sum(scores[row][col] for row, col in selected.items())
    tied = set()
    for row, col in selected.items():
        score = scores[row][col]
        scores[row][col] = None
        alternate = _assignment(scores)
        if sum(scores[r][c] for r, c in alternate.items()) == total:
            tied.add(row)
        scores[row][col] = score
    return selected, tied


def needs_confirmation(field) -> bool:
    if field is None:
        return True
    return getattr(field, "needs_confirmation", getattr(field, "uncertainty", None) is not None or field.value is None)


def key_value(name: str, value):
    if value is None:
        return None
    try:
        if name == "recharge_amount":
            if isinstance(value, bool):
                return None
            amount = Decimal(str(value))
            return amount if amount.is_finite() else None
        return normalize(name, value)
    except (ValueError, InvalidOperation):
        return None


def _draft_keys(draft, countries: dict[str, str]) -> dict:
    result = {}
    for name in PAIR_FIELDS:
        field = getattr(draft, name, None)
        if not needs_confirmation(field):
            result[name] = key_value(name, field.value)
    country = getattr(draft, "country", None)
    result["country"] = country.value if not needs_confirmation(country) else countries.get(result.get("company"))
    return result


def _gold_keys(pair: dict, sample: dict) -> dict:
    return {
        "country": sample["country"], "benefit_type": sample["benefit_type"],
        **{name: entry["value"] for name, entry in pair["configuration"].items() if "value" in entry},
        **pair.get("pairing", {}),
    }


def _score(gold: dict, draft: dict) -> list[str]:
    def equal(name: str) -> bool:
        value = draft.get(name)
        return value is not None and value == key_value(name, gold.get(name))

    hits = []
    if equal("company") or (draft["country"] is not None and draft["country"] == gold["country"]):
        hits.append("country_or_company")
    hits.extend(name for name in (
        "benefit_type", "packages", "recharge_days", "gift_days", "recharge_amount", "user_tags",
    ) if equal(name))
    if all(item.get(name) is not None for item in (gold, draft) for name in ("start_date", "end_date")):
        if max(draft["start_date"], gold["start_date"]) <= min(draft["end_date"], gold["end_date"]):
            hits.append("date_overlap")
    return hits


def _evidence_passes(name: str, field, mark: dict, body: str) -> bool:
    # 旧草稿接口在返回确定值前已校验作用域。这里只核对可见的结构证据。
    verified = getattr(field, "evidence_verified", None)
    if verified is False:
        return False
    status = getattr(field, "source_status", None)
    source = getattr(field, "value_source", None)
    company_default_source = G285_MAPPING["confirmation"]["company"]["default_source"]
    if name == "company" and status == "原文未写":
        if source != company_default_source or verified is not True or field.value != mark.get("default"):
            return False
    elif status == "原文未写" or source == "默认规则":
        return (
            status == "原文未写" and source == "默认规则"
            and verified is True and not field.evidence and field.value == mark.get("default")
        )
    elif status not in (None, "确定"):
        return False
    if field.evidence:
        return all(
            excerpt.text and (
                0 <= excerpt.start and body[excerpt.start:excerpt.start + len(excerpt.text)] == excerpt.text
                if excerpt.start is not None else excerpt.text in body
            )
            for excerpt in field.evidence
        )
    if verified is True:
        return True
    if status is not None or not mark.get("default_allowed", True):
        return False
    if name == "company":
        return field.value == mark.get("default")
    return name in LEGACY_DEFAULTS and field.value == LEGACY_DEFAULTS[name]


def grade(name: str, entry: dict, mark: dict, draft, body: str) -> str:
    if "value" not in entry or any(type(mark.get(key)) is not bool for key in ("inferable", "auto_allowed")):
        return "unknown"
    if draft is None:
        return "missing"
    field = getattr(draft, name, None)
    can_decide = mark["inferable"] and mark["auto_allowed"]
    if needs_confirmation(field):
        return "overasked" if can_decide else "justified_confirmation"
    if can_decide and compare_field(name, entry["value"], field) == "match" and _evidence_passes(name, field, mark, body):
        return "correct"
    return "false_determined"


def _g285_mark(
    name: str, pair: dict, sample: dict, package_style: str | None, company_table: CompanyTable | None,
) -> dict:
    """用冻结的取值与原文事实查 G285 规则表。"""
    configuration = pair["configuration"]
    entry = configuration.get(name, {})
    value = entry.get("value")
    source_status = pair.get("source_status", {})
    status = source_status.get(name)
    benefit = sample["benefit_type"]
    rules = G285_MAPPING["confirmation"]
    rule = rules.get(name)
    days_rule = rules["days_by_benefit"]
    uncertain_statuses = rules["source_statuses"]
    unresolved_inputs = (*uncertain_statuses, rules["unwritten_input_status"])
    needs_review = status in uncertain_statuses
    default = G285_MAPPING["defaults"].get(name)
    if isinstance(default, dict):
        default = default.get(benefit)
    if status == "原文未写":
        needs_review |= name in rules["required_without_default"] or (
            name == "user_tags" and sample["policy_type"] == rules["directed_missing_tags"]
        )
    if name in ("policy_name", "policy_type"):
        tag_field = rule["input"]
        tags = configuration.get(tag_field, {}).get("value", pair.get("pairing", {}).get(tag_field))
        needs_review |= source_status.get(tag_field) in unresolved_inputs or tags is None
        if name == "policy_name":
            needs_review |= (
                not tags or len(tags) != rule["required_count"]
                or (rule["must_match_tag"] and value not in tags)
            )
        elif tags is not None:
            expected = rule["nonempty"] if tags else rule["empty"]
            needs_review |= value != expected
    elif name == "company":
        rows = (company_table or {}).get(sample[rule["table_key"]], {})
        keywords = pair.get(rule["keyword_matches"])
        if keywords is None and status != rules["unwritten_input_status"]:
            # 旧冻结记录可没有独立的关键词审定。
            needs_review |= not any(row.display_name == value for row in rows.values())
        elif keywords and len(keywords) > 1:
            needs_review = True
        else:
            selected = rows.get(keywords[0].casefold() if keywords else rule["default_key"])
            needs_review |= selected is None or selected.display_name != value
            if not keywords and selected is not None:
                default = selected.display_name
    elif name == "trigger_once":
        needs_review |= value == rule
    elif name == "effective_multiplier" and value is not None:
        needs_review |= Decimal(value) != rule["only_automatic_value"]
    elif name == "cancel_on_downgrade" and status == rule["source_status"]:
        needs_review |= benefit == rule["benefit_type"] or source_status.get(rule["input"]) in unresolved_inputs
    elif name in days_rule["fields"]:
        needs_review |= benefit not in days_rule["types"] or source_status.get(days_rule["input"]) in unresolved_inputs
    elif name == "packages" and value is not None:
        needs_review |= (
            package_style == rule["package_style"] or source_status.get(rule["input"]) in unresolved_inputs
            or len(value) != rule["required_rows"] or (rule["required_gift"] and any(not row["gift_package"] for row in value))
        )
    return {"inferable": "value" in entry, "auto_allowed": not needs_review, "default": default}


def ratio(numerator: int, denominator: int | None) -> dict:
    return {
        "numerator": numerator, "denominator": denominator,
        "percent": 100 * numerator / denominator if denominator else None,
    }


class BusinessScore:
    def __init__(
        self, frozen: dict, threshold: int, countries: dict[str, str], *,
        company_table: CompanyTable | None = None,
    ):
        self.threshold = threshold
        self.fingerprint = pairing_fingerprint(threshold)
        self.measure_fingerprint = measure_fingerprint(threshold)
        self.countries = countries
        self.company_table = company_table
        self.samples = frozen["samples"]
        self.dataset = frozen.get("dataset", "exam")
        self.eligible = defaultdict(list)
        for name, denominator in frozen["denominators"]["fields"].items():
            for sample_id in denominator["ids"]:
                self.eligible[sample_id].append(name)
        self.counts = Counter()
        self.field_counts = defaultdict(Counter)
        self.rates = Counter()
        self.package_denominators = Counter()

    def add(self, document: dict, pairs: list[dict], drafts: list) -> dict:
        draft_keys = [_draft_keys(draft, self.countries) for draft in drafts]
        gold_keys = [_gold_keys(pair, self.samples[pair["id"]]) for pair in pairs]
        all_hits = [[_score(gold, draft) for draft in draft_keys] for gold in gold_keys]
        scores = [[sum(WEIGHTS[key] for key in hits) for hits in row] for row in all_hits]
        matches, tied = optimal_pairs([
            [score if score >= self.threshold else None for score in row] for row in scores
        ])
        self.counts["tied_pairs"] += len(tied)
        self.counts["matched_policies"] += len(matches)
        self.counts["missing_policies"] += len(pairs) - len(matches)
        extras = [index for index in range(len(drafts)) if index not in matches.values()]
        extra_kind = "extra_policies" if document.get("complete") is True else "unclassified_drafts"
        self.counts[extra_kind] += len(extras)
        audit = []
        for index, pair in enumerate(pairs):
            draft_index = matches.get(index)
            draft = drafts[draft_index] if draft_index is not None else None
            sample_id = pair["id"]
            configuration = pair["configuration"]
            package_style = pair.get("package_style")
            by_field = {}
            for name in self.eligible[sample_id]:
                mark = _g285_mark(
                    name, pair, self.samples[sample_id], package_style or document.get("package_style"), self.company_table,
                )
                status = grade(name, configuration.get(name, {}), mark, draft, document["body"])
                self.field_counts[name][status] += 1
                by_field[name] = status
            statuses = set(by_field.values())
            all_correct = draft is not None and statuses == {"correct"}
            has_comparable = bool(statuses - {"unknown"})
            self.counts["comparable_policies"] += draft is not None and has_comparable
            self.rates["all_correct"] += all_correct
            self.rates["safe_confirmation"] += draft is not None and "false_determined" not in statuses
            self.rates["zero_edit"] += all_correct
            self.rates["card_reviewed"] += pair.get("card_complete") is True and has_comparable and "unknown" not in statuses

            packages = getattr(draft, "packages", None)
            if package_style in ("name_only", "both"):
                self.package_denominators["package_rows_exact"] += 1
                self.rates["package_rows_exact"] += not needs_confirmation(packages) and (
                    compare_field("packages", configuration["packages"]["value"], packages) == "match"
                )
            elif package_style == "tier_only":
                self.package_denominators["package_tier_confirmation"] += 1
                self.rates["package_tier_confirmation"] += draft is not None and needs_confirmation(packages)
            audit.append({
                "id": sample_id, "draft": draft_index, "tie": index in tied, "fields": by_field,
                "positive_scores": [
                    {"draft": col, "score": score, "keys": all_hits[index][col]}
                    for col, score in enumerate(scores[index]) if score
                ],
                "gold": configuration, "parsed": draft.to_dict() if draft is not None else None,
            })
        return {
            "policies": audit,
            "unpaired_drafts": [{"draft": i, "kind": extra_kind, "parsed": drafts[i].to_dict()} for i in extras],
        }

    def report(self, reply_parse_failed: int) -> dict:
        field_totals = Counter()
        for counts in self.field_counts.values():
            field_totals.update(counts)
        comparable = sum(field_totals.values()) - field_totals["unknown"]
        total = len(self.samples)
        rates = {
            "all_correct": ratio(self.rates["all_correct"], total),
            "safe_confirmation": ratio(self.rates["safe_confirmation"], total),
            "false_determined": ratio(field_totals["false_determined"], comparable),
            "overasked": ratio(field_totals["overasked"], comparable),
            "zero_edit": ratio(self.rates["zero_edit"], total if self.rates["card_reviewed"] == total else None),
        }
        if rates["zero_edit"]["denominator"] is None:
            rates["zero_edit"]["numerator"] = None
        if self.dataset == "packages":
            rates.update({
                name: ratio(self.rates[name], self.package_denominators[name])
                for name in ("package_rows_exact", "package_tier_confirmation")
            })
        self.counts["unknown_fields"] = field_totals["unknown"]
        self.counts["reply_parse_failed"] = reply_parse_failed
        return {
            "pairing_sha256": self.fingerprint,
            "measure_sha256": self.measure_fingerprint,
            "rates": rates, "counts": {key: self.counts[key] for key in COUNT_LABELS},
            "field_categories": {category: field_totals[category] for category in CATEGORIES},
            "by_field": {name: {category: counts[category] for category in CATEGORIES}
                         for name, counts in self.field_counts.items()},
        }

def pairing_audit(documents: list[dict]) -> dict:
    """按冻结篇序及篇内草稿、金标顺序选出供独立抽核的编号。"""
    limits = {"matched": 10, "missing": 5, "extra_or_unknown": 5, "ties": 5}
    cases = {key: [] for key in limits}
    for document in documents:
        token = document["token"]
        matched = sorted(
            (row for row in document["policies"] if row["draft"] is not None), key=lambda row: row["draft"],
        )
        cases["matched"].extend({"token": token, "id": row["id"], "draft": row["draft"]} for row in matched)
        cases["ties"].extend({"token": token, "id": row["id"], "draft": row["draft"]} for row in matched if row["tie"])
        cases["missing"].extend({"token": token, "id": row["id"]} for row in document["policies"] if row["draft"] is None)
        cases["extra_or_unknown"].extend(
            {"token": token, "draft": row["draft"], "kind": row["kind"]} for row in document["unpaired_drafts"]
        )
    return {key: cases[key][:limit] for key, limit in limits.items()}
