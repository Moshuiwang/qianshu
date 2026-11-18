"""汇报群使用的卡片模板。"""

from dataclasses import dataclass
from html import escape


@dataclass(frozen=True, repr=False)
class Report:
    source: str
    created: tuple[str, ...] = ()
    adjustments: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    reminders: tuple[str, ...] = ()


def _card(title: str, elements: list[dict]) -> dict:
    return {
        "header": {"title": {"tag": "plain_text", "content": title}},
        "elements": elements,
    }


def result_card(report: Report) -> dict:
    """各行由业务层提供，已建单行可带明道云链接。"""
    sections = (
        ("来源", (report.source,)),
        (f"已建单（{len(report.created)}）", report.created),
        ("自动调整", report.adjustments),
        ("未建单原因", report.failures),
        ("提醒项", report.reminders),
    )
    return _card("申请处理结果", [
        {"tag": "markdown", "content": f"**{label}**\n" + ("\n".join(lines) or "无")}
        for label, lines in sections
    ])


def mention(open_id: str, name: str) -> str:
    return f'<at user_id="{escape(open_id, quote=True)}">{escape(name)}</at>'


def approval_card(summary: str, approval_id: str, record_id: str) -> dict:
    return _card("测试单审批", [
        {"tag": "div", "text": {"tag": "plain_text", "content": summary}},
        {"tag": "action", "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": label},
                "type": style,
                "value": {"decision": decision, "approval_id": approval_id, "record_id": record_id},
            }
            for decision, label, style in (
                ("approve", "同意", "primary"), ("reject", "驳回", "danger"),
            )
        ]},
    ])
