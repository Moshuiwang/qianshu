"""汇报群使用的卡片模板。"""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from html import escape


@dataclass(frozen=True, repr=False)
class Report:
    source: str
    created: tuple[str, ...] = ()
    adjustments: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    reminders: tuple[str, ...] = ()


@dataclass(frozen=True, repr=False)
class CardLine:
    label: str
    text: str
    red: bool = False


@dataclass(frozen=True, repr=False)
class DetailPage(Report):
    elements: tuple[dict, ...] = ()
    number: int = 1
    total: int = 1


_PAGE_BYTES = 24 * 1024
_PAGE_ELEMENTS = 40
_LINE_CHARS = 1500
_RESULT_TITLE = "申请处理结果"


def _card(title: str, elements: list[dict]) -> dict:
    return {
        "header": {"title": {"tag": "plain_text", "content": title}},
        "elements": elements,
    }


def result_card(report: Report) -> dict:
    """各行由业务层提供，已建单行可带明道云链接。"""
    if isinstance(report, DetailPage):
        return _card(_page_title(report.number, report.total), list(report.elements))
    sections = (
        ("来源", (report.source,)),
        (f"已建单（{len(report.created)}）", report.created),
        ("自动调整", report.adjustments),
        ("未建单原因", report.failures),
        ("提醒项", report.reminders),
    )
    return _card(_RESULT_TITLE, [
        {"tag": "markdown", "content": f"**{label}**\n" + ("\n".join(lines) or "无")}
        for label, lines in sections
    ])


def _detail_element(line: CardLine, chunk: str, *, continued: bool) -> dict:
    label = f"{line.label}（续）" if continued else line.label
    content = f"{label}\n{chunk}"
    if line.red:
        return {"tag": "markdown", "content": f"<font color='red'>{escape(content)}</font>"}
    return {"tag": "div", "text": {"tag": "plain_text", "content": content}}


def _page_title(number: int, total: int) -> str:
    return f"{_RESULT_TITLE} · 第 {number} / {total} 张"


def interrupted_report_card(number: int, total: int) -> dict:
    return _card(_RESULT_TITLE, [{
        "tag": "div", "text": {"tag": "plain_text", "content": (
            f"汇报发送曾在第 {number} / {total} 张暂停。若未收齐，请人工核对。"
        )},
    }])


def detail_pages(source: str, lines: Iterable[CardLine]) -> tuple[DetailPage, ...]:
    """按序分卡，续段保留字段名和完整摘录。"""
    heading = _detail_element(CardLine("来源", source), source, continued=False)
    details = [
        _detail_element(line, line.text[start:start + _LINE_CHARS], continued=start > 0)
        for line in lines for start in range(0, max(1, len(line.text)), _LINE_CHARS)
    ]
    # 每页至少放一条明细，用最大页码测量字节数。
    title = _page_title(max(1, len(details)), max(1, len(details)))
    pages = []
    elements = [heading]
    for element in details:
        candidate = _card(title, [*elements, element])
        if (len(elements) >= _PAGE_ELEMENTS
                or len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) > _PAGE_BYTES):
            pages.append(tuple(elements))
            elements = [heading]
        elements.append(element)
    pages.append(tuple(elements))
    return tuple(
        DetailPage(source, elements=elements, number=number, total=len(pages))
        for number, elements in enumerate(pages, 1)
    )


def mention(open_id: str, name: str) -> str:
    return f'<at user_id="{escape(open_id, quote=True)}">{escape(name)}</at>'


def approval_card(summary: str, approval_id: str, record_id: str) -> dict:
    return _approval_card(summary, {"approval_id": approval_id, "record_id": record_id})


def decided_approval_card(decision: str) -> dict:
    status = {"approve": "已收到，处理中", "reject": "已驳回"}[decision]
    return _approval_card(status, None)


def _approval_card(summary: str, binding: dict[str, str] | None) -> dict:
    actions = []
    for decision, label, style in (("approve", "同意", "primary"), ("reject", "驳回", "danger")):
        button = {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": style,
        }
        if binding is None:
            button["disabled"] = True
            button["disabled_tips"] = {"tag": "plain_text", "content": "审批决定已记录"}
        else:
            button["value"] = {"decision": decision, **binding}
        actions.append(button)
    card = _card("测试单审批", [
        {"tag": "div", "text": {"tag": "plain_text", "content": summary}},
        {"tag": "action", "actions": actions},
    ])
    card["config"] = {"update_multi": True}
    return card
