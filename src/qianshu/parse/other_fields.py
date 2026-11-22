"""互斥组、触发次数、多买多送、生效倍数、用户范围与审批件。"""

from dataclasses import replace

from qianshu.validate import _is_web_url

from .evidence import can_use_absence, default, issue, verified_value
from .protocol import FieldResult, ValueSource

ATTACHMENTS_HINT = "审批件按默认留空"
MUTEX_NO_HINT = "申请要求不维护互斥组，需人工确认"
RULE_PENDING_REASON = "取值规则待确认。"
_DEFAULTS = {
    "maintain_mutual_exclusion_group": ("是", "互斥组按默认填是"),
    "trigger_once": ("否", "是否只触发一次按默认填否"),
    "repeat_bonus": ("是", "多买多送按默认填是"),
    "effective_multiplier": (1, "生效倍数按默认填 1"),
    "approval_attachments": ([], ATTACHMENTS_HINT),
}


def web_url(value: str) -> str | None:
    return value if _is_web_url(value) else None


def apply_defaults(results: dict[str, FieldResult]) -> list[str]:
    hints = []
    for name, (value, hint) in _DEFAULTS.items():
        rule = "R7" if name == "maintain_mutual_exclusion_group" else "6.2"
        results[name] = default(results[name], value, rule)
        if results[name].source is ValueSource.DEFAULT:
            hints.append(hint)

    for name, manual in (("maintain_mutual_exclusion_group", "否"), ("trigger_once", "是")):
        result = results[name]
        if verified_value(result) and result.value == manual:
            results[name] = issue(result, "mutex_opt_out" if name == "maintain_mutual_exclusion_group" else "manual_rule")
            if name == "maintain_mutual_exclusion_group":
                hints.append(MUTEX_NO_HINT)
    multiplier = results["effective_multiplier"]
    if verified_value(multiplier) and multiplier.value != 1:
        results["effective_multiplier"] = issue(multiplier, "manual_rule")
    archive = results["approval_url"]
    if can_use_absence(archive):
        results["approval_url"] = replace(archive, needs_confirmation=False, rule="R9.deferred")
    attachments = results["approval_attachments"]
    if verified_value(attachments) and attachments.value != []:
        results["approval_attachments"] = issue(attachments, "attachments_require_review")
    return hints
