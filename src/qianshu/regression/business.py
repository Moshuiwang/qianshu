"""按冻结业务键配对，并按独立审定的字段分母评分。"""

from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from re import Pattern
from unicodedata import normalize as unicode_normalize

from qianshu.parse import _normalized_value
from qianshu.parse.evidence import Sources
from qianshu.parse.protocol import SourceBlock, ValueSource
from qianshu.parse.companies import CompanyTable
from qianshu.parse.names import MAX_NAME_LENGTH, ORIGINAL_NAME_LABEL, PROPOSED_NAME_LABEL, compose_public_name
from qianshu.parse.scope import country_matches

from .compare import POLICY_TYPES, compare_field, normalize
from .storage import RegressionError, digest, json_bytes

WEIGHTS = {
    "country_or_company": 4, "benefit_type": 2, "packages": 8, "recharge_days": 2,
    "gift_days": 2, "recharge_amount": 2, "user_tags": 7, "date_overlap": 2,
}
EMPTY_WEIGHTS = {"packages": 4, "user_tags": 3}
SHADOW_TAG_SUFFIXES = ("_image",)
PAIRING_VERSION = 4
DEFAULT_THRESHOLD = 2
CATEGORIES = ("correct", "false_determined", "overasked", "justified_confirmation", "missing", "unknown")
LEGACY_DEFAULTS = {
    "maintain_mutual_exclusion_group": "是", "trigger_once": "否",
    "repeat_bonus": "是", "effective_multiplier": 1,
}
G285_MAPPING = {
    "version": "G285-4",
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
        "packages": {"input": "benefit_type", "package_style": "tier_only", "minimum_rows": 1, "required_gift": True},
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
    "tied_pairs": "配对歧义（政策数）",
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
        "version": PAIRING_VERSION, "weights": WEIGHTS, "empty_weights": EMPTY_WEIGHTS, "threshold": threshold,
        "gold_keys": ["included_gold", "reviewed_pairing", "unexcluded_sample_metadata"],
        "draft_keys": "v3_then_located_pending_on_unmatched_unambiguous_remainder",
        "ties": "retain_only_pairs_common_to_all_optimal_assignments",
        "user_tags": {
            "normalization": ["NFKC", "casefold", "strip", "remove_one_shadow_suffix_with_nonempty_base", "sorted_set"],
            "shadow_suffixes": SHADOW_TAG_SUFFIXES,
        },
    }))


def measure_fingerprint(threshold: int) -> str:
    return digest(json_bytes({"pairing_sha256": pairing_fingerprint(threshold), "mapping": G285_MAPPING}))


def _assignment(
    scores: list[list[int | None]], *, prefer_policy: int | None = None, prefer_draft: int | None = None,
) -> dict[int, int]:
    """矩形 Hungarian。每条金标额外有一个零分的空位可供使用。"""
    rows = len(scores)
    if not rows or not scores[0]:
        return {}
    drafts = len(scores[0])
    columns = drafts + rows

    def cost(row: int, col: int) -> int:
        score = scores[row][col] if col < drafts else None
        if score is None:
            return 0
        # 指定的一条政策或草稿至多加 1 分，原始总分始终优先。
        return -(2 * score + (row == prefer_policy or col == prefer_draft))

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


def optimal_pairs(scores: list[list[int | None]]) -> tuple[dict[int, int], set[int], set[int]]:
    """返回所有最优解共有的配对及配对可能变化的政策、草稿。"""
    def total(assignment: dict[int, int]) -> int:
        return sum(scores[row][col] for row, col in assignment.items())

    selected = _assignment(scores)
    best = total(selected)
    matches = {}
    for row, col in selected.items():
        without_pair = scores.copy()
        without_pair[row] = scores[row].copy()
        without_pair[row][col] = None
        if total(_assignment(without_pair)) < best:
            matches[row] = col

    ambiguous_policies = selected.keys() - matches.keys()
    ambiguous_drafts = set(selected.values()) - set(matches.values())
    # 参考解未配上的条目也可能出现在另一份同分解中。
    for row in set(range(len(scores))) - selected.keys():
        if any(scores[row][col] is not None for col in ambiguous_drafts):
            alternate = _assignment(scores, prefer_policy=row)
            if row in alternate and total(alternate) == best:
                ambiguous_policies.add(row)
    if scores:
        for col in set(range(len(scores[0]))) - set(selected.values()):
            if any(scores[row][col] is not None for row in ambiguous_policies):
                alternate = _assignment(scores, prefer_draft=col)
                if col in alternate.values() and total(alternate) == best:
                    ambiguous_drafts.add(col)
    return matches, ambiguous_policies, ambiguous_drafts


def needs_confirmation(field) -> bool:
    if field is None:
        return True
    return getattr(field, "needs_confirmation", getattr(field, "uncertainty", None) is not None or field.value is None)


def _tag_key(tag: str) -> str:
    tag = unicode_normalize("NFKC", tag).casefold().strip()
    for suffix in SHADOW_TAG_SUFFIXES:
        if tag.endswith(suffix):
            return tag.removesuffix(suffix).rstrip() or tag
    return tag


def key_value(name: str, value):
    if value is None:
        return None
    try:
        if name == "recharge_amount":
            if isinstance(value, bool):
                return None
            amount = Decimal(str(value))
            return amount if amount.is_finite() else None
        normalized = normalize(name, value)
        if name == "user_tags":
            return sorted({_tag_key(tag) for tag in normalized})
        return normalized
    except (ValueError, InvalidOperation):
        return None


def _draft_keys(draft, countries: dict[str, str]) -> dict:
    # v3 先按卡片投影取键，包括已确认的 R10 公司和它唯一映射的国家。
    keys = {}
    for name in PAIR_FIELDS:
        field = getattr(draft, name, None)
        if not needs_confirmation(field):
            keys[name] = key_value(name, field.value)
    country = getattr(draft, "country", None)
    keys["country"] = country.value if not needs_confirmation(country) else countries.get(keys.get("company"))

    return keys


def _with_pending_keys(draft, confirmed: dict, sources: Sources, options: dict) -> dict:
    keys = confirmed.copy()
    results = getattr(draft, "field_results", {})
    for name in ("country", *PAIR_FIELDS):
        field = getattr(draft, name, None)
        result = results.get(name)
        if not needs_confirmation(field if field is not None else result):
            continue
        if result is not None:
            statement = result.statement
            if statement.value is None or not statement.evidence or not all(
                sources.locate_quote(quote.span, quote.text)[0] is not None for quote in statement.evidence
            ):
                continue
            if name == "packages":
                # 待确认行的卡片投影会省略包名，配对只取声明中的原始行。
                value = [row.model_dump() for row in statement.value]
            else:
                value = _normalized_value(name, statement.value, options)
        else:
            # 无声明的旧投影须明确标为原文，避免用查表值或默认值配对。
            if getattr(field, "value_source", None) != ValueSource.ORIGINAL or field.value is None:
                continue
            if not field.evidence or not all(
                excerpt.text and excerpt.start is not None and excerpt.start >= 0
                and sources.blocks["body"].text.startswith(excerpt.text, excerpt.start)
                for excerpt in field.evidence
            ):
                continue
            value = field.value
        if name == "country":
            keys["pending_country"] = value
        else:
            keys[name] = key_value(name, value)
    return keys


def _gold_keys(pair: dict, sample: dict) -> dict:
    configuration = pair["configuration"]
    metadata = {"benefit_type": sample["benefit_type"]} if "benefit_type" not in configuration else {}
    return {
        "country": sample["country"], **metadata,
        **pair.get("pairing", {}),
        **{name: configuration[name]["value"] for name in PAIR_FIELDS if "value" in configuration.get(name, {})},
    }


def _score(gold: dict, draft: dict) -> list[str]:
    def equal(name: str) -> bool:
        value = draft.get(name)
        return value is not None and value == key_value(name, gold.get(name))

    hits = []
    countries = (draft["country"], draft.get("pending_country"))
    if equal("company") or (gold["country"] is not None and gold["country"] in countries):
        hits.append("country_or_company")
    hits.extend(name for name in (
        "benefit_type", "packages", "recharge_days", "gift_days", "recharge_amount", "user_tags",
    ) if equal(name))
    if all(item.get(name) is not None for item in (gold, draft) for name in ("start_date", "end_date")):
        if max(draft["start_date"], gold["start_date"]) <= min(draft["end_date"], gold["end_date"]):
            hits.append("date_overlap")
    return hits


def _matches_excerpt(excerpt, body, blocks):
    if hasattr(excerpt, "source_id"):
        return any(block.source_id == excerpt.source_id and excerpt.source_start >= block.start
                   and block.text.startswith(excerpt.text, excerpt.source_start - block.start) for block in blocks)
    if excerpt.start is None:
        return excerpt.text in body
    return excerpt.start >= 0 and body.startswith(excerpt.text, excerpt.start)


def _evidence_passes(name: str, draft, mark: dict, body: str, blocks) -> bool:
    # 4fb3cb5 也有 field_results，但没有薄版的只读核对结果，沿用旧投影。
    field = getattr(draft, name)
    result = getattr(draft, "field_results", {}).get(name)
    thin_result = hasattr(result, "evidence_verified")
    if thin_result:
        verified = result.evidence_verified
        status = result.statement.status
        source = result.source
    else:
        verified = getattr(field, "evidence_verified", None)
        status = getattr(field, "source_status", None)
        source = getattr(field, "value_source", None)
    if verified is False:
        return False
    lookup_source = G285_MAPPING["confirmation"]["company"]["default_source"]
    if thin_result and status == "原文未写" and source == lookup_source:
        # 查表派生可使用输入字段的证据；冻结记录明确给出默认公司时仍须一致。
        if name == "company" and mark.get("default") is not None and field.value != mark["default"]:
            return False
    elif name == "company" and status == "原文未写":
        if source != lookup_source or verified is not True or field.value != mark.get("default"):
            return False
    elif status == "原文未写" or source == "默认规则":
        return (
            status == "原文未写" and source == "默认规则"
            and verified is True and not field.evidence and field.value == mark.get("default")
        )
    elif status not in (None, "确定"):
        return False
    if field.evidence:
        return all(excerpt.text and _matches_excerpt(excerpt, body, blocks) for excerpt in field.evidence)
    if verified is True:
        return True
    if status is not None or not mark.get("default_allowed", True):
        return False
    if name == "company":
        return field.value == mark.get("default")
    return name in LEGACY_DEFAULTS and field.value == LEGACY_DEFAULTS[name]


def grade(name: str, entry: dict, mark: dict, draft, body: str, blocks=()) -> str:
    if "value" not in entry or any(type(mark.get(key)) is not bool for key in ("inferable", "auto_allowed")):
        return "unknown"
    if draft is None:
        return "missing"
    field = getattr(draft, name, None)
    can_decide = mark["inferable"] and mark["auto_allowed"]
    if needs_confirmation(field):
        return "overasked" if can_decide else "justified_confirmation"
    if can_decide and compare_field(name, entry["value"], field) == "match" and _evidence_passes(name, draft, mark, body, blocks):
        return "correct"
    return "false_determined"


def _g285_mark(
    name: str, pair: dict, sample: dict, package_style: str | None, company_table: CompanyTable | None,
    *, country_names: dict[str, Pattern[str]] | None = None,
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
        tag_entry = configuration.get(tag_field, {})
        needs_review |= source_status.get(tag_field) in unresolved_inputs
        # 标签金标排除时，仍可用独立冻结的标签集合核对名称和类型。
        tags = tag_entry.get("value", pair.get("pairing", {}).get(tag_field))
        if name == "policy_name" and tags == [] and sample["policy_type"] == "公开政策":
            needs_review |= (
                source_status.get(tag_field) != "确定" or status != "确定"
                or source_status.get("policy_type") in uncertain_statuses
                or configuration.get("policy_type", {}).get("value") == "定向政策"
                or not isinstance(value, str) or not value.strip() or len(value) > MAX_NAME_LENGTH
            )
        elif tags is None:
            needs_review |= "exclude" not in tag_entry
        elif name == "policy_name":
            needs_review |= (
                len(tags) != rule["required_count"] or (rule["must_match_tag"] and value not in tags)
            )
        else:
            expected = rule["nonempty"] if tags else rule["empty"]
            needs_review |= value != expected
    elif name == "company":
        table = company_table or {}
        country = sample[rule["table_key"]]
        if country not in table and table and country_names:
            matches = country_matches([country], country_names, exact=True)
            if len(matches) == 1:
                country = matches.pop()
        rows = table.get(country, {})
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
            or len(value) < rule["minimum_rows"] or (rule["required_gift"] and any(not row["gift_package"] for row in value))
        )
    return {"inferable": "value" in entry, "auto_allowed": not needs_review, "default": default}


def ratio(numerator: int, denominator: int | None) -> dict:
    return {
        "numerator": numerator, "denominator": denominator,
        "percent": 100 * numerator / denominator if denominator else None,
    }


def _public_name_grade(pair: dict, country_code: str | None, today: date | None, draft) -> str:
    """按独立审定的原文事实统计 #58 名称结果。"""
    statuses = pair.get("source_status", {})
    configuration = pair["configuration"]
    gold = configuration.get("policy_name", {})
    tags = configuration.get("user_tags", {}).get("value", pair.get("pairing", {}).get("user_tags"))
    status = statuses.get("policy_name")
    pending = G285_MAPPING["confirmation"]["source_statuses"]
    if (any(statuses.get(key) in pending for key in ("policy_name", "policy_type", "user_tags"))
            or statuses.get("user_tags") == "原文未写" or tags
            or configuration.get("policy_type", {}).get("value") == "定向政策"):
        return _review_name_grade(draft)
    if tags != [] or statuses.get("user_tags") != "确定":
        return "unknown"
    if status == "确定" and "value" in gold:
        expected, source = gold["value"], ORIGINAL_NAME_LABEL
    elif status == "原文未写" and "policy_short_name" in pair:
        short_name = pair["policy_short_name"]
        if short_name is None or any(statuses.get(key) in (*pending, "原文未写")
                                     for key in ("country", "start_date")):
            return _review_name_grade(draft)
        start = configuration.get("start_date", {}).get("value", pair.get("pairing", {}).get("start_date"))
        if not (start and country_code and today):
            return "unknown"
        try:
            start_date = date.fromisoformat(start)
        except (TypeError, ValueError):
            return "unknown"
        expected = compose_public_name(country_code, short_name, start_date, today)
        source = PROPOSED_NAME_LABEL
    else:
        return "unknown"
    if not isinstance(expected, str) or not expected.strip():
        return "unknown"
    if len(expected) > MAX_NAME_LENGTH:
        return _review_name_grade(draft)
    if draft is None:
        return "missing"
    label = getattr(draft, "policy_name_source", None)
    field = draft.policy_name
    if label == PROPOSED_NAME_LABEL:
        candidates = field.uncertainty.candidates if field.uncertainty else []
        if (source == PROPOSED_NAME_LABEL and field.value is None and len(candidates) == 1
                and candidates[0].value == expected and candidates[0].evidence):
            return "correct_proposal"
        return "incorrect_proposal"
    if needs_confirmation(field):
        return "overasked"
    if source == label and field.value == expected and any(q.text == expected for q in field.evidence):
        return "correct"
    return "false_determined"


def _name_totals(counts: Counter) -> dict:
    return {"comparable": sum(counts.values()) - counts["unknown"],
            **{key: counts[key] for key in (*CATEGORIES, "correct_proposal", "incorrect_proposal")}}


def _review_name_grade(draft) -> str:
    if draft is None:
        return "missing"
    if getattr(draft, "policy_name_source", None) == PROPOSED_NAME_LABEL:
        return "incorrect_proposal"
    return "justified_confirmation" if needs_confirmation(getattr(draft, "policy_name", None)) else "false_determined"


def _pair_round(gold_keys: list, draft_keys: list, threshold: int):
    all_hits = [[_score(gold, draft) for draft in draft_keys] for gold in gold_keys]
    scores = [
        [sum(EMPTY_WEIGHTS[key] if key in EMPTY_WEIGHTS and not draft[key] else WEIGHTS[key] for key in hits)
         for draft, hits in zip(draft_keys, row, strict=True)]
        for row in all_hits
    ]
    matches, ambiguous_policies, ambiguous_drafts = optimal_pairs([
        [score if score >= threshold else None for score in row] for row in scores
    ])
    return matches, ambiguous_policies, ambiguous_drafts, all_hits, scores


class BusinessScore:
    def __init__(
        self, frozen: dict, threshold: int, countries: dict[str, str], *,
        company_table: CompanyTable | None = None, country_names: dict[str, Pattern[str]] | None = None,
        country_codes: dict[str, str] | None = None, today: date | None = None, options: dict | None = None,
    ):
        self.options = options if options is not None else {"benefit_type": {}}
        self.threshold = threshold
        self.fingerprint = pairing_fingerprint(threshold)
        self.measure_fingerprint = measure_fingerprint(threshold)
        self.countries = countries
        self.company_table = company_table
        self.country_names = country_names
        self.country_codes = country_codes or {}
        self.today = today
        self.samples = frozen["samples"]
        self.dataset = frozen.get("dataset", "exam")
        self.eligible = defaultdict(list)
        for name, denominator in frozen["denominators"]["fields"].items():
            for sample_id in denominator["ids"]:
                self.eligible[sample_id].append(name)
        self.counts = Counter()
        self.field_counts = defaultdict(Counter)
        self.benefit_field_counts = defaultdict(lambda: defaultdict(Counter))
        self.name_counts = {kind: Counter() for kind in POLICY_TYPES}
        self.name_sources = defaultdict(Counter)
        self.rates = Counter()
        self.package_denominators = Counter()

    def add(self, document: dict, pairs: list[dict], drafts: list) -> dict:
        blocks = document.get("additional_blocks", ())
        draft_keys = [_draft_keys(draft, self.countries) for draft in drafts]
        gold_keys = [_gold_keys(pair, self.samples[pair["id"]]) for pair in pairs]
        matches, ambiguous_policies, ambiguous_drafts, all_hits, scores = _pair_round(
            gold_keys, draft_keys, self.threshold,
        )
        remaining_rows = sorted(set(range(len(pairs))) - matches.keys() - ambiguous_policies)
        remaining_cols = sorted(set(range(len(drafts))) - set(matches.values()) - ambiguous_drafts)
        if remaining_rows and remaining_cols:
            sources = Sources([SourceBlock("body", document["body"], "body"), *blocks])
            added, tied_rows, tied_cols, hits, points = _pair_round(
                [gold_keys[i] for i in remaining_rows],
                [_with_pending_keys(drafts[j], draft_keys[j], sources, self.options) for j in remaining_cols],
                self.threshold,
            )
            matches.update((remaining_rows[i], remaining_cols[j]) for i, j in added.items())
            ambiguous_policies.update(remaining_rows[i] for i in tied_rows)
            ambiguous_drafts.update(remaining_cols[j] for j in tied_cols)
            for i, row in enumerate(remaining_rows):
                for j, col in enumerate(remaining_cols):
                    all_hits[row][col] = hits[i][j]
                    scores[row][col] = points[i][j]
        self.counts["tied_pairs"] += len(ambiguous_policies)
        self.counts["ambiguous_drafts"] += len(ambiguous_drafts)
        self.counts["matched_policies"] += len(matches)
        self.counts["missing_policies"] += len(pairs) - len(matches) - len(ambiguous_policies)
        extras = sorted(set(range(len(drafts))) - set(matches.values()) - ambiguous_drafts)
        extra_kind = "extra_policies" if document.get("complete") is True else "unclassified_drafts"
        self.counts[extra_kind] += len(extras)
        audit = []
        for index, pair in enumerate(pairs):
            draft_index = matches.get(index)
            draft = drafts[draft_index] if draft_index is not None else None
            sample_id = pair["id"]
            sample = self.samples[sample_id]
            configuration = pair["configuration"]
            package_style = pair.get("package_style")
            by_field = {}
            for name in self.eligible[sample_id]:
                mark = _g285_mark(
                    name, pair, self.samples[sample_id], package_style or document.get("package_style"), self.company_table,
                    country_names=self.country_names,
                )
                status = grade(name, configuration.get(name, {}), mark, draft, document["body"], blocks)
                self.field_counts[name][status] += 1
                self.benefit_field_counts[self.samples[sample_id]["benefit_type"]][name][status] += 1
                by_field[name] = status
            kind = sample.get("policy_type")
            if kind == "公开政策":
                code = self.country_codes.get(sample["country"])
                if code is None and self.country_names:
                    aliases = country_matches([sample["country"]], self.country_names, exact=True)
                    if len(aliases) == 1:
                        code = self.country_codes.get(aliases.pop())
                name_grade = _public_name_grade(pair, code, self.today, draft)
                self.name_counts[kind][name_grade] += 1
                source = {"原文未写": PROPOSED_NAME_LABEL, "确定": ORIGINAL_NAME_LABEL}.get(
                    pair.get("source_status", {}).get("policy_name"), "待核或未知",
                )
                self.name_sources[source][name_grade] += 1
            elif kind == "定向政策":
                self.name_counts[kind][by_field.get("policy_name", "unknown")] += 1
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
                "id": sample_id, "draft": draft_index, "tie": index in ambiguous_policies, "fields": by_field,
                "positive_scores": [
                    {"draft": col, "score": score, "keys": all_hits[index][col]}
                    for col, score in enumerate(scores[index]) if score
                ],
                "gold": configuration, "parsed": draft.to_dict() if draft is not None else None,
            })
        return {
            "policies": audit,
            "unpaired_drafts": [{"draft": i, "kind": extra_kind, "parsed": drafts[i].to_dict()} for i in extras],
            "ambiguous_drafts": [{"draft": i, "parsed": drafts[i].to_dict()} for i in sorted(ambiguous_drafts)],
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
            "pairing_version": PAIRING_VERSION, "pairing_sha256": self.fingerprint,
            "measure_sha256": self.measure_fingerprint,
            "rates": rates, "counts": {key: self.counts[key] for key in (*COUNT_LABELS, "ambiguous_drafts")},
            "field_categories": {category: field_totals[category] for category in CATEGORIES},
            "by_benefit_type": {
                kind: {name: {category: counts[category] for category in CATEGORIES}
                       for name, counts in fields.items()}
                for kind, fields in self.benefit_field_counts.items()
            },
            "by_field": {name: {category: counts[category] for category in CATEGORIES}
                         for name, counts in self.field_counts.items()},
            "policy_names": {kind: _name_totals(counts) for kind, counts in self.name_counts.items()},
            "public_name_sources": {source: _name_totals(counts) for source, counts in self.name_sources.items()},
        }

def pairing_audit(documents: list[dict]) -> dict:
    """按冻结篇序及篇内草稿、金标顺序选出供独立抽核的编号。"""
    limits = {"matched": 10, "missing": 5, "extra_or_unknown": 5, "ambiguous": 5}
    cases = {key: [] for key in limits}
    for document in documents:
        token = document["token"]
        matched = sorted(
            (row for row in document["policies"] if row["draft"] is not None), key=lambda row: row["draft"],
        )
        cases["matched"].extend({"token": token, "id": row["id"], "draft": row["draft"]} for row in matched)
        cases["missing"].extend(
            {"token": token, "id": row["id"]} for row in document["policies"] if row["draft"] is None and not row["tie"]
        )
        cases["extra_or_unknown"].extend(
            {"token": token, "draft": row["draft"], "kind": row["kind"]} for row in document["unpaired_drafts"]
        )
        if document["ambiguous_drafts"]:
            cases["ambiguous"].append({
                "token": token,
                "policies": [row["id"] for row in document["policies"] if row["tie"]],
                "drafts": [row["draft"] for row in document["ambiguous_drafts"]],
            })
    return {key: cases[key][:limit] for key, limit in limits.items()}
