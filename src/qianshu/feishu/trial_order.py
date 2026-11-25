"""存档成功后建正式单（#154 段 2d-1）。

主链只有：核对 → 建单（单在途 + 五项同事务）→ 逐条官方回读 → 写错即停。其余异常一律停下 + 固定群提示 @ 配置负责人
+ 转人工（守望者核对），不做自动恢复：结果未知冻结该条与在途槽，守望者用 #142 只读核对后人工处理，
产品负责人当场同意后由守望者销清移交，才释放。建单与存档在审批子进程的同一专用线程里串行执行。
"""

import json
import logging
import re
import time
import unicodedata
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID

from qianshu.mingdao.create_order import (
    UnverifiedCombination, check_outlet, compact, ledger_open, receipt, require, uuid_record,
)

from .state import trial_batch
from .trial_approval import CARD_FORMAT, journal_at

_LOG = logging.getLogger(__name__)
LEDGER_FILE = "trial-orders.jsonl"
READBACK_KEY = "MAIL_TRIAL_READBACK_SECONDS"
ARCHIVE_URL = re.compile(r"https://feishu\.cn/docx/[A-Za-z0-9]+")
# 群里固定文案（产品文档 6.5、验收 E-9 同步）。
ORDER_NOTICES = {
    "mismatch": "建单回读与卡片同意时的展示值不一致，已停写：同批余项与后续建单一律不发，请守望者关闭建单开关并停查。",
    "unknown": "建单结果未知，请人工到明道云核对。",
    "disconnect": "断线期间已发出 1 单，已回读核对。",
    "disconnect_unknown": "断线期间已发出 1 单，回读未能核对。",
    "done": "均已建单并逐条回读一致，提交由人工在明道云处理。",
}
SUPERSEDED = "本条「未建单」公告已过时：相关条目已由新卡接管或已建单，请勿据此重复建单，以私有登记为准。"
# 转人工固定文案（守望者 G510，治根）：卡面与公告凡涉及转人工的（收口、晚投、「未建单」、出卡转人工的条目、同名组、
# 只读核对），一律只用这一句，不给任何可执行的建单指示；可不可以补建只由守望者判定、另行通知。
# 建成、写错即停告警、结果未知三类公告本来就不指示人去建单，不用这一句。
MANUAL = "已转人工，守望者核对后另行通知是否补建，请勿据此直接建单。"
# 合批公告部分条目被接管时，其余条目逐条写明（审核 P1-1）；转人工由整条公告的固定文案（MANUAL）说明。
UNBUILT_ITEMS = "未建单。"
# 结果未知类公告登记人工移交后附在群提示末尾（G488 取舍 A；G510 只保留给结果未知、写错两类，文案不变）。
HANDOFF_NOTE = "\n相关政策已交人工，机器人对同名政策的新卡一律不建，需守望者核对后解除。"
# G510 之前排队的旧公告（2b / 2c 收口）里的可执行字样：投递时改写，群里只出现固定文案。
LEGACY_WORDS = (("，未建单，转人工", "，未建单"), ("未建单，已转人工。", "未建单。"))
# 含这些字样的公告交人工处理：发出前先持久登记人工移交、占用该政策名（审核 P1-3）。
HANDOFF_WORDS = ("未建单", "结果未知", "未发出")
# 因断线而未发出的条目的固定公告（G464，守望者 12:0xZ）：按实际原因写，不写成「建单开关已关闭」；转人工用固定文案（G510）。
DISCONNECT_UNSENT = "断线期间未发出。" + MANUAL
CANCEL_REASONS = {
    "consent": "同意已失效或卡片已关闭",
    "switch": "建单开关已关闭",
    "halted": "建单已停写",
    "expired": "审批到期",
    "disconnected": "断线",
    "combination": "该类型尚未经真实回读验证",
    "archive": "取不到本批存档链接",
    "handoff": "该条已交人工处理",
    "plan": "建单字段未能核实",
    "format": "台账标识格式不符",
    "ledger": "建单台账未能写入",
    "not_sent": "建单请求未发出",
    "previous": "同批前序条目未建成",
    "slot": "本条发送登记已变化",
    "reapprove": "开始日期需顺延或来源证明已过期",
}
# 已占用建单资格的状态：在途、已建、写错、未知。
ACTIVE = ("sending", "created", "verified", "mismatch", "unknown")


def name_key(name):
    """归一政策名：全半角统一、空白折叠。"""
    return " ".join(unicodedata.normalize("NFKC", name).split()) if isinstance(name, str) else ""


def name_fingerprint(key):
    """守望者确认用的政策名指纹（按归一政策名算），不回显原文。"""
    return sha256(key.encode()).hexdigest()[:16]


def ledger_registration(reg, version, item):
    return f"trial-{reg}-v{version}-i{item}"


def canonical_card(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


@dataclass
class Plan:
    """一条拟建单：请求与卡片同意值由同一组装与核对得到。"""
    name: str
    target: str
    request_fields: list
    displayed: dict
    written: dict
    payload: object = None
    extra: dict = field(default_factory=dict)


class NotSent(Exception):
    """建单请求确证未交给传输层。code 为撤销原因（CANCEL_REASONS 的键）。"""

    def __init__(self, code="not_sent"):
        super().__init__(code)
        self.code = code


class Refused(Exception):
    """实际交接处复核不符：本条不写出。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass
class Job:
    row: object
    snapshot: dict
    url: str


def _reviewed_name(snapshot, item):
    try:
        return snapshot["reviewed"][item - 1]["policy_name"]["value"]
    except (KeyError, IndexError, TypeError):
        return None


# 缺可信政策名的条目按「登记 × 版本 × 条目」登记移交：政策名键留空，不占用任何政策名。
UNNAMED = ""


def card_handoffs(snapshot):
    """出卡即登记人工移交的条目（特批第 3 轮第 1 条）：快照里未列为拟建（`eligible`）的条目逐条按归一政策名占用；
    缺可信政策名的留空名键，只按「登记 × 版本 × 条目」登记。返回 [(条目, 名键)]，与 record_handoff 同格式。

    同卡同名组（G509）的成员照常列出：record_handoff 按卡片快照把同组成员合为一项组移交（登记在同组首条上）。"""
    eligible = set(snapshot.get("eligible") or [])
    return [(item, name_key(_reviewed_name(snapshot, item)) or UNNAMED)
            for item in snapshot.get("items") or [] if item not in eligible]


# 同卡同名合组（守望者 G509）：同一张卡内（含只读卡）归一政策名相同的条目合为一组，组有 2 条及以上时整组机器人 0 发、
# 合成一项人工移交。卡面与所有公告统一用这一文案（句末加「。」），{} 为组内条目号（「、」分隔）；转人工只用固定文案
# MANUAL（G510），组文案本身不给建单指示。
SAME_NAME_GROUP = "本卡第 {} 条同名，合为一项"
GROUP_WORD = "条同名，合为一项"
# 组移交的来源标记：未销清时记在 trial_handoff.reason，每组一行、登记在同组首条上；守望者按组销清时改写为销清原因。
SAME_NAME_GROUP_MARK = "same_name_group"
# 组销清时每个成员的去向。
DISPOSITIONS = {"built": "已建", "not_built": "不建", "merged": "合并"}


def same_name_groups(snapshot):
    """同卡同名分组（G509）：快照全部条目（`items`，含只读卡、含未列为拟建的条目）按唯一归一函数 name_key 分组，
    返回成员 2 条及以上的组 [[条目, …]]（组内、组间均按条目号升序）。缺可信政策名（空名键）的条目不分组。

    出卡、出卡登记、整卡收口、晚投登记、建单执行、恢复清单与组销清共用这一分组，且只按快照里的可信政策名重算，
    不依赖快照里另存的分组字段（旧快照同样适用，复核 rv-2dH P1-2）。"""
    keys = {}
    for item in sorted(set(snapshot.get("items") or []) | set(snapshot.get("eligible") or [])):
        key = name_key(_reviewed_name(snapshot, item))
        if key:
            keys.setdefault(key, []).append(item)
    return [group for group in sorted(keys.values()) if len(group) > 1]


def group_of(snapshot):
    """{条目: 所在同名组}，只含成员 2 条及以上的组。"""
    return {item: group for group in same_name_groups(snapshot) for item in group}


def group_text(group):
    return SAME_NAME_GROUP.format("、".join(map(str, group)))


def display_fingerprint(snapshot, item):
    """组销清双确认用的成员政策名指纹：按卡面展示的原名算（同组成员归一同名，原名可能仅全半角不同），不回显原文。"""
    name = _reviewed_name(snapshot, item)
    return name_fingerprint(name) if isinstance(name, str) else ""


def group_diff(snapshot, group):
    """组内差异摘要：逐字段只写「相同 / 不同」，不写任何值（防外泄）。字段按卡片快照 `reviewed` 的字段名。"""
    reviewed = snapshot.get("reviewed") or []
    rows = [reviewed[n - 1] if 0 < n <= len(reviewed) and isinstance(reviewed[n - 1], dict) else {} for n in group]
    fields = list(dict.fromkeys(f for row in rows for f in row))

    def value(row, name):
        cell = row.get(name)
        cell = cell.get("value") if isinstance(cell, dict) and "value" in cell else cell
        return json.dumps(cell, ensure_ascii=False, sort_keys=True, default=str)
    return {name: "相同" if len({value(row, name) for row in rows}) == 1 else "不同" for name in fields}


def _card_snapshot(db, reg, version):
    row = db.execute("SELECT card, snapshot FROM trial_card WHERE registration=? AND version=?",
                     (reg, version)).fetchone()
    return (row[0], json.loads(row[1])) if row else (None, None)


def group_member(db, reg, version, item):
    """该条所在的同卡同名组（无则 None）：单条销清只用于非组移交。"""
    _, snapshot = _card_snapshot(db, reg, version)
    return group_of(snapshot).get(item) if snapshot else None


# 出卡登记移交或建单前查重时同名政策已有机器人记录（复核 rv-2d4 P1、G506）：移交照登「只读核对」类、保持同名占位；
# 卡面与公告同样只写固定文案 MANUAL（G510），已有记录只在恢复清单的 robot_record 里如实标出，供守望者判定。
# 「只读核对」类移交的来源标记（G497）：未销清时记在 trial_handoff.reason；守望者销清时改写为销清原因。
READONLY_CHECK = "readonly_check"


def robot_held(db, key, card, members=()):
    """同一归一政策名已有其他卡的机器人在途、已建、写错或结果未知记录（`ACTIVE`）。

    公告投递裁决（unbuilt_notice）、出卡登记移交（Journal.prepare）与建单前置检查及领槽事务（OrderRunner._one /
    _admit，G506）共用这一判定；空名键一律不算。
    members 为本卡条目（恢复清单的 robot_record，G510）：本卡这些条目自己的 `ACTIVE` 记录同样算（如旧快照组内成员
    已建、重启后转结果未知），供守望者判定是否补建。
    """
    if not key:
        return False
    marks = ",".join("?" * len(ACTIVE))
    if db.execute(f"SELECT 1 FROM trial_order WHERE name_key=? AND card!=? AND status IN ({marks})",
                  (key, card, *ACTIVE)).fetchone():
        return True
    items = ",".join("?" * len(members))
    return bool(members) and db.execute(f"""SELECT 1 FROM trial_order WHERE card=? AND item IN ({items})
        AND status IN ({marks})""", (card, *members, *ACTIVE)).fetchone() is not None


def notice_items(kind, snapshot):
    """「未建单」公告实际涉及的条目：与公告文案同一来源（审核 P1-1）。

    单条关卡与建单撤销按键名里的条目；整批公告（关卡 `closed`、存档失败等）按 `Journal.notify` 列出的申请全部条目
    （快照 `items`，含原本就标人工、未列为拟建的条目），不得只取拟建（`eligible`）。
    """
    if kind.startswith("closed_"):
        return [int(kind.split("_", 1)[1])]
    if kind.startswith("order_cancel:"):
        return [int(n) for n in kind.split(":", 1)[1].split(",")]
    return list(dict.fromkeys([*(snapshot.get("items") or []), *(snapshot.get("eligible") or [])]))


def unbuilt_notice(db, row):
    """「未建单 / 结果未知 / 写错」公告投递前核对（cd-carry P1-1、审核 P1-3 / P1-4）。返回 (实际投递文案, 须登记移交的条目)。

    调用方须在投递前、与建单领槽互斥的同一写事务里调用并登记移交（record_handoff）。条目逐条按目标表 + 归一政策名核对：
    已被新卡接管或已建单的条目标「已过时」、不登记移交；其余条目保留原文案并各自登记移交。全部过时才整条改为过时文案。
    同卡同名组（G509，按快照可信政策名重算，旧快照同样适用）整组按一项组移交处理：不逐条裁决过时、不逐条登记，
    只登记一项组移交（record_handoff 落在同组首条上）；整批公告未写组文案的（含存档失败、旧版排队的公告）逐组补写
    「本卡第 M、N 条同名，合为一项。」。

    转人工只用固定文案 MANUAL（G510）：旧版排队公告里的可执行字样（LEGACY_WORDS）投递时改写；未带固定文案的
    （旧版排队、存档失败整批）在末尾补一次。只含结果未知、未补写任何条目的公告照旧附 HANDOFF_NOTE（结果未知类不变）。
    """
    body = row["body"]
    if row["kind"].startswith("order_unknown:") or row["kind"] == "order_mismatch":
        # 结果未知、写错：同样已交人工，登记移交；释放在途槽也经守望者销清移交。
        items = [(r[0], r[1]) for r in db.execute("""SELECT item, name_key FROM trial_order WHERE card=?
                 AND status IN ('unknown','mismatch') AND name_key IS NOT NULL""", (row["card"],))]
        return body + (HANDOFF_NOTE if items else ""), items
    if not any(word in body for word in (*HANDOFF_WORDS, GROUP_WORD)):
        return body, []
    snapshot = json.loads(row["snapshot"])
    keep, taken, unnamed, groups = [], [], [], []
    grouped = group_of(snapshot)
    for item in notice_items(row["kind"], snapshot):
        if item in grouped:
            if grouped[item] not in groups:
                groups.append(grouped[item])
            continue
        key = name_key(_reviewed_name(snapshot, item))
        if not key:
            unnamed.append(item)
            continue
        if robot_held(db, key, row["card"]):
            taken.append(item)
        else:
            keep.append((item, key))
    if taken and not keep and not unnamed and not groups:
        return f"登记 {_card_reg(db, row['card'])} · {SUPERSEDED}", []
    text = body
    for old, new in LEGACY_WORDS:
        text = text.replace(old, new)
    if taken:
        # 部分接管：逐条写明——已接管或已建单的标过时，其余条目明确「未建单」并各自登记移交。
        rest = sorted([item for item, _ in keep] + unnamed)
        text += f"\n条目 {'、'.join(map(str, taken))} · {SUPERSEDED}"
        if rest:
            text += f"\n条目 {'、'.join(map(str, rest))} · {UNBUILT_ITEMS}"
    text += "".join(f"\n{group_text(group)}。" for group in groups if group_text(group) not in text)
    handoff = keep + [(group[0], name_key(_reviewed_name(snapshot, group[0]))) for group in groups]
    if MANUAL in text:
        return text, handoff
    if text == body and "结果未知" in body:
        return text + (HANDOFF_NOTE if handoff else ""), handoff
    return text + "\n" + MANUAL, handoff


def _card_reg(db, card):
    reg, version = db.execute("SELECT registration,version FROM trial_card WHERE card=?", (card,)).fetchone()
    return f"{reg} · 版本 {version}"


def record_handoff(db, card, items, now, *, held=()):
    """公告投递前登记人工移交（占用该政策名）。须守望者销清移交后机器人才允许再接管同一政策。

    同一「登记 × 版本 × 条目 × 政策名」已有未销清移交时不重复登记（投递未成、下一轮重投）。
    held 内的条目（同名已有机器人建单记录，G497）标为「只读核对」类：未销清期间 reason 记 READONLY_CHECK，
    同样占用政策名、机器人 0 发，但恢复清单不作为可补建项列出（单列 order_readonly_check，可查）。
    同卡同名组（G509）的成员一律合为一项组移交：登记在同组首条上、按组名键占用、reason 记 SAME_NAME_GROUP_MARK，
    同组只登记一行；恢复清单按组列出全部成员，须守望者按组销清。
    已销清的不再重开（复核 rv-2dI P1-2）：组按「登记 × 版本 × 组名键」、单条按「登记 × 版本 × 条目」已有销清记录的，
    关卡、晚投与建单撤销都不再插入开放移交。
    """
    reg, version, packed = db.execute("SELECT registration,version,snapshot FROM trial_card WHERE card=?",
                                      (card,)).fetchone()
    snapshot = json.loads(packed)
    grouped = group_of(snapshot)
    for item, key in items:
        mark = READONLY_CHECK if item in held else None
        if item in grouped:
            group = grouped[item]
            item = group[0]
            key, mark = name_key(_reviewed_name(snapshot, item)), SAME_NAME_GROUP_MARK
            # 组销清（release_group）清的是组内全部成员行：按组名键核，组内成员行（含旧版逐条登记的）同样算。
            cleared = db.execute(f"""SELECT 1 FROM trial_handoff WHERE registration=? AND version=?
                                 AND cleared IS NOT NULL AND (name_key=? OR item IN ({','.join('?' * len(group))}))""",
                                 (reg, version, key, *group)).fetchone()
        else:
            cleared = db.execute("""SELECT 1 FROM trial_handoff WHERE registration=? AND version=? AND item=?
                                 AND cleared IS NOT NULL""", (reg, version, item)).fetchone()
        if cleared or db.execute("""SELECT 1 FROM trial_handoff WHERE name_key=? AND registration=? AND version=?
                                 AND item=? AND cleared IS NULL""", (key, reg, version, item)).fetchone():
            continue
        target = db.execute("SELECT target FROM trial_order WHERE registration=? AND version=? AND item=?",
                            (reg, version, item)).fetchone()
        db.execute("INSERT INTO trial_handoff VALUES(?,?,?,?,?,?,NULL,?)",
                   (key, target[0] if target else None, reg, version, item, now, mark))


def release_handoff(db, reg, version, item, fingerprint, reason, now):
    """守望者销清移交（产品负责人当场同意后）。登记 ID 与政策名指纹都对上才写；返回 (改前, 改后) 状态。

    只作用于双确认对应的那一条「登记 × 版本 × 条目」：不清同名其他登记的移交，只释放该条自己持有的在途槽（审核 P2）。
    只用于非组移交：该条属同卡同名组（G509）时拒绝，须按组销清（release_group）。
    """
    require(receipt(reason), "须写明原因")
    require(group_member(db, reg, version, item) is None, "该条属同卡同名组，须按组销清")
    rows = db.execute("""SELECT name_key FROM trial_handoff WHERE registration=? AND version=? AND item=?
                      AND cleared IS NULL""", (reg, version, item)).fetchall()
    keys = {r[0] for r in rows}
    require(len(keys) == 1 and name_fingerprint(next(iter(keys))) == fingerprint, "登记 ID 或政策名指纹与登记库不符")
    key = next(iter(keys))
    before = handoff_state(db, key)
    db.execute("""UPDATE trial_handoff SET cleared=?, reason=? WHERE name_key=? AND registration=? AND version=?
               AND item=? AND cleared IS NULL""", (now, reason, key, reg, version, item))
    db.execute("""DELETE FROM trial_order_slot WHERE registration=? AND version=? AND item=? AND EXISTS(
               SELECT 1 FROM trial_order o WHERE o.registration=? AND o.version=? AND o.item=? AND o.name_key=?)""",
               (reg, version, item, reg, version, item, key))
    db.execute("INSERT INTO trial_guardian_log VALUES(?,?,?)",
               (now, f"release_handoff {reg}-{version}-{item} {name_fingerprint(key)}", reason))
    return before, handoff_state(db, key)


def check_group(db, reg, version, members):
    """组销清前核对（G509 / G487）：members 为 {条目: (成员政策名指纹, 去向)}。登记 ID 对应的卡片存在；成员集合与
    该卡同一同名组完全一致；每个成员都给出去向（DISPOSITIONS）且政策名指纹与登记库一致；该组有未销清移交。
    任一不符即抛出（调用方零写入）。返回 (组, 组名键)。"""
    _, snapshot = _card_snapshot(db, reg, version)
    require(snapshot is not None, "登记 ID 与登记库不符")
    hit = [group for group in same_name_groups(snapshot) if set(group) & set(members)]
    require(len(hit) == 1 and set(hit[0]) == set(members), "组成员与登记库不符：须列出该组全部成员")
    group = hit[0]
    require(all(members[n][1] in DISPOSITIONS for n in group), "每个成员须给出去向（已建 / 不建 / 合并）")
    require(all(members[n][0] == display_fingerprint(snapshot, n) for n in group), "成员政策名指纹与登记库不符")
    marks = ",".join("?" * len(group))
    require(db.execute(f"""SELECT 1 FROM trial_handoff WHERE registration=? AND version=? AND item IN ({marks})
                       AND cleared IS NULL""", (reg, version, *group)).fetchone() is not None, "该组没有未销清的移交")
    return group, name_key(_reviewed_name(snapshot, group[0]))


def release_group(db, reg, version, members, reason, now):
    """守望者按组销清同卡同名组移交（G509；只在产品负责人当场明确同意时执行）。核对见 check_group；返回 (改前, 改后)。

    清掉该登记版本下组内全部成员的未销清移交（含旧版逐条登记的），只释放组内成员自己持有的在途槽；不清其他登记的
    同名移交。各成员去向写入守望者留痕（只含条目号、去向与指纹，不含政策名原文）。"""
    require(receipt(reason), "须写明原因")
    group, key = check_group(db, reg, version, members)
    before = group_state(db, reg, version)
    marks = ",".join("?" * len(group))
    db.execute(f"""UPDATE trial_handoff SET cleared=?, reason=? WHERE registration=? AND version=? AND item IN ({marks})
               AND cleared IS NULL""", (now, reason, reg, version, *group))
    db.execute(f"""DELETE FROM trial_order_slot WHERE registration=? AND version=? AND item IN ({marks}) AND EXISTS(
               SELECT 1 FROM trial_order o WHERE o.registration=trial_order_slot.registration
               AND o.version=trial_order_slot.version AND o.item=trial_order_slot.item AND o.name_key=?)""",
               (reg, version, *group, key))
    outcome = ",".join(f"{n}:{members[n][1]}" for n in group)
    db.execute("INSERT INTO trial_guardian_log VALUES(?,?,?)",
               (now, f"release_group {reg}-{version} {name_fingerprint(key)} {outcome}", reason))
    return before, group_state(db, reg, version)


def group_state(db, reg, version):
    """该登记版本的同卡同名组移交状态（守望者回显）：每组列成员条目号、成员政策名指纹与未销清移交数，不回显政策名原文。"""
    _, snapshot = _card_snapshot(db, reg, version)
    groups = []
    for group in same_name_groups(snapshot) if snapshot else []:
        marks = ",".join("?" * len(group))
        opened = {r[0] for r in db.execute(f"""SELECT item FROM trial_handoff WHERE registration=? AND version=?
                  AND item IN ({marks}) AND cleared IS NULL""", (reg, version, *group))}
        slot = db.execute(f"""SELECT item FROM trial_order_slot WHERE registration=? AND version=?
                          AND item IN ({marks})""", (reg, version, *group)).fetchone()
        key = name_key(_reviewed_name(snapshot, group[0]))
        groups.append({"name_fp": name_fingerprint(key), "open_handoffs": len(opened), "slot_held": bool(slot),
                       "members": [{"item": n, "display_fp": display_fingerprint(snapshot, n)} for n in group],
                       "orders": [r[0] for r in db.execute(
                           "SELECT status FROM trial_order WHERE name_key=? ORDER BY sent", (key,))]})
    return {"registration": reg, "version": version, "groups": groups}


def handoff_state(db, key):
    """同名政策的移交与在途槽状态；有多条同名未销清移交时逐条列出（只回显登记号与指纹，不回显政策名）。"""
    opened = [{"registration": r[0], "version": r[1], "item": r[2]} for r in db.execute(
        """SELECT registration, version, item FROM trial_handoff WHERE name_key=? AND cleared IS NULL
        ORDER BY rowid""", (key,))]
    slot = db.execute("""SELECT s.registration, s.version, s.item FROM trial_order_slot s
                      JOIN trial_order o USING(registration,version,item) WHERE o.name_key=?""", (key,)).fetchone()
    orders = [r[0] for r in db.execute("SELECT status FROM trial_order WHERE name_key=? ORDER BY sent", (key,))]
    return {"name_fp": name_fingerprint(key), "open_handoffs": len(opened), "open": opened, "slot_held": bool(slot),
            "slot": dict(zip(("registration", "version", "item"), slot)) if slot else None, "orders": orders}


class OrderRunner:
    """每轮至多处理一张存档已建、待建单的卡。同批逐条串行，全局至多 1 个在途建单请求。"""

    def __init__(self, path, *, switches, backend, connected, ledger_dir, clock=time.time, pause=time.sleep,
                 readback_seconds=120, switch_file=None):
        self.path, self.switches, self.backend, self.connected = path, switches, backend, connected
        # 只读开关文件、不含连接判断的读法（G464 交接回退）：交接处连接状态与开关文件分开核对。未提供时沿用 switches。
        self.switch_file = switch_file or switches
        self.ledger_dir, self.clock, self.pause = Path(ledger_dir), clock, pause
        self.readback_seconds = readback_seconds
        self.frozen = False  # 登记库提交失败或结果不明：本进程不再发任何建单
        self._settled = False

    # ------------------------------------------------------------ 轮询入口

    def run_once(self):
        if self.frozen:
            return False
        if not self._settled:
            self._settle_stale()
            self._settled = True
        if not self._up():
            return False
        job = self._next()
        if job is None:
            return False
        self._batch(job)
        return True

    def _up(self):
        try:
            return self.connected() is True
        except Exception:
            return False

    def _next(self):
        with journal_at(self.path) as j:
            if j.db.execute("SELECT 1 FROM trial_order_slot").fetchone():
                return None  # 在途或冻结：其他批次一律等待
            row = j.db.execute("""SELECT c.*, a.url AS archive_url FROM trial_card c JOIN trial_archive a
                ON a.registration=c.registration AND a.version=c.version
                WHERE c.status='accepted' AND a.status='success'
                AND EXISTS(SELECT 1 FROM trial_item_task t WHERE t.card=c.card AND t.status='archive_done')
                ORDER BY c.rowid LIMIT 1""").fetchone()
            if row is None:
                return None
            return Job(row, json.loads(row["snapshot"]), row["archive_url"])

    def _settle_stale(self):
        """上一进程在途（如被强杀）的建单：结果未知，冻结该条与在途槽，不重发。"""
        with journal_at(self.path) as j:
            rows = j.db.execute("""SELECT o.*, c.snapshot, c.country FROM trial_order o JOIN trial_card c USING(card)
                                WHERE o.status IN ('sending','created')""").fetchall()
        for row in rows:
            job = Job(row, json.loads(row["snapshot"]), "")
            plan = Plan(_reviewed_name(job.snapshot, row["item"]) or "", row["target"] or "", [], {}, {})
            self._unknown(job, row["item"], plan, row["record_id"], "restart",
                          disconnect=bool(row["disconnect"]))

    # ------------------------------------------------------------ 批与条

    def _batch(self, job):
        row = job.row
        with journal_at(self.path) as j:
            items = [r[0] for r in j.db.execute("""SELECT item FROM trial_item_task WHERE card=?
                     AND status='archive_done' ORDER BY item""", (row["card"],))]
        # 同卡同名组（G509）整组机器人 0 发：新卡出卡时组内条目都不进拟建；旧快照（无此分组时出卡）里仍列为拟建的
        # 组内条目在这里按组撤下，公告组文案，投递时登记一项组移交。
        grouped = group_of(job.snapshot)
        if any(item in grouped for item in items):
            self._cancel(job, [item for item in items if item in grouped], "group")
            items = [item for item in items if item not in grouped]
        if not isinstance(job.url, str) or not ARCHIVE_URL.fullmatch(job.url):
            self._cancel(job, items, "archive")
            return self._close_batch(job)
        for index, item in enumerate(items):
            if not self._up():
                self._cancel(job, items[index:], "disconnected")
                break
            outcome, code = self._one(job, item)
            if outcome == "wait":
                return
            if outcome == "stop":
                self._cancel(job, items[index + 1:], code or "previous")
                break
        self._close_batch(job)

    def _one(self, job, item):
        """返回 (verified|skip|stop|wait, 停批原因)。"""
        row = job.row
        # 本地同名查重（G506）：同一归一政策名已有其他卡的机器人记录，本条不建、不碰明道云，登记只读核对类移交。
        reviewed = name_key(_reviewed_name(job.snapshot, item))
        with journal_at(self.path) as j:
            held = robot_held(j.db, reviewed, row["card"])
        if held:
            self._cancel(job, [item], "held", held=[(item, reviewed)])
            return "skip", ""
        try:
            plan = self.backend.plan(job.snapshot, item, job.url, now=self.clock(), sent=row["sent"])
        except UnverifiedCombination:
            self._cancel(job, [item], "combination")
            return "skip", ""
        except Exception:
            self._cancel(job, [item], "plan")
            return "skip", ""
        key = name_key(plan.name)
        if not key or key != name_key(_reviewed_name(job.snapshot, item)):
            self._cancel(job, [item], "plan")
            return "skip", ""
        with journal_at(self.path) as j:
            handed = j.db.execute("SELECT 1 FROM trial_handoff WHERE name_key=? AND cleared IS NULL", (key,)).fetchone()
        if handed:
            self._cancel(job, [item], "handoff")
            return "skip", ""
        if not (canonical_card(row["card"]) and type(row["registration"]) is int and type(row["version"]) is int
                and receipt(plan.target)):
            self._cancel(job, [item], "format")
            return "skip", ""
        try:
            self._ledger(job, item, plan, "create_reserved", request_fields=plan.request_fields,
                         displayed=plan.displayed)
        except Exception:
            self._cancel(job, [item], "ledger")
            return "stop", "ledger"
        try:
            code = self._admit(job, item, plan, key)
        except Exception:
            # 发送记录提交失败或结果不明：不发，冻结（本进程不再发任何建单，交人工）。
            self.frozen = True
            _LOG.error("建单发送记录提交未确认，已冻结，转人工。")
            return "stop", "ledger"
        if code == "busy":
            self._abort_ledger(job, item, plan, code)
            return "wait", code
        if code in ("handoff", "held"):
            self._abort_ledger(job, item, plan, code)
            self._cancel(job, [item], code, held=[(item, key)] if code == "held" else ())
            return "skip", ""
        if code:
            self._abort_ledger(job, item, plan, code)
            self._cancel(job, [item], code)
            return "stop", code
        if not self._up():
            # 断线判序在登记提交之后做（G464）：此刻已观察到断线，登记不能证明先于断线，本条不发。
            return self._not_sent(job, item, plan, "disconnected")
        try:
            self._ledger(job, item, plan, "create_attempt", request_fields=plan.request_fields, approved=True)
        except Exception:
            self._unknown(job, item, plan, None, "ledger")
            return "stop", "previous"
        if self.clock() >= row["expires"]:
            # 回调校验与发出建单共用同一截止时间：截止后不发。
            return self._not_sent(job, item, plan, "expired")
        state = {"disconnect": False}
        try:
            record_id = self.backend.send(plan, self._handoff(job, item, state))
        except NotSent as error:
            return self._not_sent(job, item, plan, error.code if error.code in CANCEL_REASONS else "not_sent")
        except Exception:
            self._unknown(job, item, plan, None, "send", disconnect=state["disconnect"] or not self._up())
            return "stop", "previous"
        disconnect = state["disconnect"]
        if not disconnect and not self._up():
            # 请求在途时观察到断线：即断线时在途的那 1 张，照常回读核对、单独计数。
            disconnect = True
            self._mark_disconnect(row, item)
        if not uuid_record(record_id):
            # 台账只写合规记录 ID（#156 ①）：回执不合格按结果未知冻结。
            self._unknown(job, item, plan, None, "record_id", disconnect=disconnect)
            return "stop", "previous"
        try:
            self._ledger(job, item, plan, "created", record_id, displayed=plan.displayed, written=plan.written,
                         disconnect=disconnect)
            with journal_at(self.path) as j, j.db:
                j.db.execute("""UPDATE trial_order SET status='created', record_id=? WHERE registration=?
                             AND version=? AND item=? AND status='sending'""",
                             (record_id, row["registration"], row["version"], item))
        except Exception:
            self._unknown(job, item, plan, record_id, "ledger", disconnect=disconnect)
            return "stop", "previous"
        verdict, checks = self._readback(plan, record_id, row["expires"])
        if verdict is None:
            self._unknown(job, item, plan, record_id, "readback", disconnect=disconnect, checks=checks)
            return "stop", "previous"
        counts = {k: sum(c["结果"] == k for c in checks) for k in ("一致", "不一致") if any(c["结果"] == k for c in checks)}
        try:
            self._ledger(job, item, plan, "readback_compared", record_id, counts=counts, total=len(checks),
                         结论=verdict, 逐项核对=checks)
        except Exception:
            _LOG.error("回读结果台账未写入，转人工。")
        if verdict == "写错":
            self._mismatch(job, item, record_id, disconnect)
            return "stop", "halted"
        self._verified(job, item, disconnect)
        return "verified", ""

    # ------------------------------------------------------------ 五项同事务

    @staticmethod
    def _consent(j, row, item, task_status, *, bound=True):
        """同意有效性判定（领槽事务与实际交接共用，审核 P1-3）：卡片格式、指纹与快照摘要未变，所属登记的批次版本
        未失效、未冻结，条目任务处于 task_status；bound 时另核卡片仍为「已同意」且会话未换。不符返回 None。

        断线后才在交接处观察到的那 1 张（G464）免核「已同意」与会话两项：二者随断线后的会话轮换而变，只因断线不得拦下；
        批次版本、冻结状态与原同意绑定照核。
        """
        current = j.db.execute("SELECT * FROM trial_card WHERE card=?", (row["card"],)).fetchone()
        task = j.db.execute("SELECT status FROM trial_item_task WHERE card=? AND item=?", (row["card"], item)).fetchone()
        if (current is None or current["format"] != CARD_FORMAT or current["fingerprint"] != row["fingerprint"]
                or sha256(current["snapshot"].encode()).hexdigest() != current["fingerprint"]
                or trial_batch(j.db, row["registration"], row["version"])[3] or not task or task[0] != task_status
                or (bound and (current["status"] != "accepted" or not current["session"]
                               or current["session"] != j.session()))):
            return None
        return current

    def _admit(self, job, item, plan, key):
        """同一写事务：①截止未到 ②建单开关开 ③停写位未置 ④在途槽空闲并占用 ⑤写入发送记录。返回空串表示放行。"""
        row = job.row
        reg, version, card = row["registration"], row["version"], row["card"]
        with journal_at(self.path) as j, j.db:
            j.db.execute("BEGIN IMMEDIATE")
            try:
                switches = self.switches()
            except Exception:
                switches = {}
            if not self._up():
                return "disconnected"
            current = self._consent(j, row, item, "archive_done")
            if current is None:
                return "consent"
            if self.clock() >= current["expires"]:
                return "expired"
            if (switches.get("MAIL_TRIAL_CREATE_ENABLED") != "true" or switches.get("MAIL_TRIAL_CARD_ENABLED") != "true"
                    or switches.get("MAIL_TRIAL_CREATE_COUNTRY") != current["country"]):
                return "switch"
            if j.write_halted():
                return "halted"
            # 本地同名查重与领槽同一写事务（G506）：同一归一政策名已有其他卡的机器人在途、已建、写错或结果未知记录，
            # 本条不建，登记只读核对类移交（与 G497 同口径）。
            if robot_held(j.db, key, card):
                record_handoff(j.db, card, [(item, key)], self.clock(), held={item})
                return "held"
            # 人工移交与机器人发送资格在同一写事务里按「目标表 × 归一政策名」裁决（审核 P1-3）。
            if j.db.execute("""SELECT 1 FROM trial_handoff WHERE name_key=? AND cleared IS NULL
                            AND (target IS NULL OR target=?)""", (key, plan.target)).fetchone():
                return "handoff"
            if j.db.execute("SELECT 1 FROM trial_order_slot").fetchone():
                return "busy"
            now = self.clock()
            j.db.execute("INSERT INTO trial_order_slot VALUES(1,?,?,?,?)", (reg, version, item, now))
            j.db.execute("""INSERT INTO trial_order(registration,version,item,card,status,name_key,target,sent)
                         VALUES(?,?,?,?,'sending',?,?,?)""", (reg, version, item, card, key, plan.target, now))
            j.db.execute("UPDATE trial_item_task SET status='order_running' WHERE card=? AND item=?", (card, item))
            return ""

    def _handoff(self, job, item, state):
        """实际交接回调：预建连之后、真正写出请求之前再核同意与批次有效性（与领槽同口径）、截止、建单开关、停写位、
        本条持槽与发送记录。

        任一不符即抛出，传输层不写出（确证未发出）。连接状态与开关文件分开核对（G464）：此刻才观察到断线的，是登记先于
        断线的那 1 张——只因断线不拦下，照发、单独公告与计数；开关文件真关闭才按开关关闭处理。
        """
        row = job.row
        reg, version = row["registration"], row["version"]

        def check():
            up = self._up()
            try:
                switches = self.switch_file()
            except Exception:
                switches = {}
            with journal_at(self.path) as j:
                card = self._consent(j, row, item, "order_running", bound=up)
                slot = j.db.execute("SELECT registration, version, item FROM trial_order_slot WHERE id=1").fetchone()
                sending = j.db.execute("""SELECT status FROM trial_order WHERE registration=? AND version=?
                                       AND item=?""", (reg, version, item)).fetchone()
                halted = j.write_halted()
            if card is None:
                raise Refused("consent")
            if self.clock() >= card["expires"]:
                raise Refused("expired")
            # 生产开关是 Config（Mapping，不是 dict）：按映射核对，不得因类型拦下。
            if (not isinstance(switches, Mapping) or switches.get("MAIL_TRIAL_CREATE_ENABLED") != "true"
                    or switches.get("MAIL_TRIAL_CARD_ENABLED") != "true"
                    or switches.get("MAIL_TRIAL_CREATE_COUNTRY") != card["country"]):
                raise Refused("switch")
            if halted:
                raise Refused("halted")
            if slot is None or tuple(slot) != (reg, version, item) or sending is None or sending[0] != "sending":
                raise Refused("slot")
            if not up or not self._up():
                state["disconnect"] = True
                self._mark_disconnect(row, item)
        return check

    def _not_sent(self, job, item, plan, code):
        """确证未发出：释放在途槽，撤销本条并公告「未建单」，同批余项停发。"""
        self._abort_ledger(job, item, plan, code)
        self._aborted(job, item)
        self._cancel(job, [item], code)
        return "stop", code

    # ------------------------------------------------------------ 回读

    def _readback(self, plan, record_id, expires):
        """回读慢就等；等到卡片截止或私有配置的上限仍无结论，返回 None（按结果未知冻结）。"""
        limit = min(expires, self.clock() + self.readback_seconds)
        checks = []
        while True:
            budget = limit - self.clock()
            if budget <= 0:
                return None, checks
            try:
                verdict, checks = self.backend.readback(plan, record_id, budget)
            except Exception:
                verdict = None
            if verdict in ("一致", "写错"):
                if [c["字段名"] for c in checks] != list(plan.request_fields):
                    return None, checks
                return verdict, checks
            self.pause(min(1.0, max(0.0, limit - self.clock())))

    # ------------------------------------------------------------ 收口

    def _notice(self, j, card, kind, body):
        from uuid import uuid4
        j.db.execute("INSERT OR IGNORE INTO trial_notification VALUES(?,?,?,?,'pending')",
                     (card, kind, str(uuid4()), body))

    @staticmethod
    def _head(row, items):
        return f"登记 {row['registration']} · 版本 {row['version']} · 条目 {'、'.join(map(str, items))} · "

    def _cancel(self, job, items, code, *, held=()):
        """未发的条目撤销：置终态并公告「未建单」@ 配置负责人；转人工只写固定文案 MANUAL（G510）。

        held（[(条目, 名键)]）为同名政策已有机器人建单记录的条目（G506）：同一事务里登记只读核对类移交，公告只写固定
        文案，不写「未建单」（投递端不再按「未建单」改写或补登移交）。
        code 为 group（同卡同名组，G509）时同一事务里登记一项组移交，公告写组文案加固定文案。
        """
        if not items:
            return
        row = job.row
        try:
            with journal_at(self.path) as j, j.db:
                j.db.execute("BEGIN IMMEDIATE")
                marks = ",".join("?" * len(items))
                j.db.execute(f"""UPDATE trial_item_task SET status='closed' WHERE card=? AND item IN ({marks})
                             AND status IN ('archive_done','order_running')""", (row["card"], *items))
                if held:
                    record_handoff(j.db, row["card"], held, self.clock(), held={item for item, _ in held})
                if code == "group":
                    grouped = group_of(job.snapshot)
                    groups = list(dict.fromkeys(tuple(grouped[item]) for item in items))
                    record_handoff(j.db, row["card"], [(group[0], "") for group in groups], self.clock())
                    text = "".join(group_text(group) + "。" for group in groups) + MANUAL
                else:
                    text = (DISCONNECT_UNSENT if code == "disconnected" else MANUAL if code == "held"
                            else CANCEL_REASONS[code] + "，未建单。" + MANUAL)
                self._notice(j, row["card"], "order_cancel:" + ",".join(map(str, items)), self._head(row, items) + text)
        except Exception:
            self.frozen = True
            _LOG.error("撤销登记未成功，已冻结，转人工。")

    def _close_batch(self, job):
        row = job.row
        try:
            with journal_at(self.path) as j, j.db:
                j.db.execute("BEGIN IMMEDIATE")
                left = j.db.execute("""SELECT 1 FROM trial_item_task WHERE card=?
                    AND status IN ('archive_done','order_running')""", (row["card"],)).fetchone()
                if left:
                    return
                done = [r[0] for r in j.db.execute("""SELECT item FROM trial_item_task WHERE card=?
                         AND status='order_done' ORDER BY item""", (row["card"],))]
                j.db.execute("UPDATE trial_card SET status='done' WHERE card=? AND status='accepted'", (row["card"],))
                j.db.execute("UPDATE trial_task SET status=? WHERE card=?",
                             ("order_done" if done else "closed", row["card"]))
                j.db.execute("""UPDATE trial_notification SET status='cancelled' WHERE card=? AND kind='remind'
                             AND status='pending'""", (row["card"],))
                if done:
                    self._notice(j, row["card"], "order_done", self._head(row, done) + ORDER_NOTICES["done"])
        except Exception:
            _LOG.error("批次收口登记未成功，转人工。")

    def _mark_disconnect(self, row, item):
        try:
            with journal_at(self.path) as j, j.db:
                j.db.execute("UPDATE trial_order SET disconnect=1 WHERE registration=? AND version=? AND item=?",
                             (row["registration"], row["version"], item))
        except Exception:
            pass  # 收口时再写

    def _aborted(self, job, item):
        """确证未发出：释放在途槽。"""
        row = job.row
        with journal_at(self.path) as j, j.db:
            j.db.execute("BEGIN IMMEDIATE")
            j.db.execute("""UPDATE trial_order SET status='aborted', finished=? WHERE registration=? AND version=?
                         AND item=? AND status='sending'""", (self.clock(), row["registration"], row["version"], item))
            j.db.execute("DELETE FROM trial_order_slot WHERE registration=? AND version=? AND item=?",
                         (row["registration"], row["version"], item))

    def _verified(self, job, item, disconnect):
        row = job.row
        with journal_at(self.path) as j, j.db:
            j.db.execute("BEGIN IMMEDIATE")
            j.db.execute("""UPDATE trial_order SET status='verified', finished=?, disconnect=? WHERE registration=?
                         AND version=? AND item=?""", (self.clock(), int(disconnect), row["registration"],
                                                       row["version"], item))
            j.db.execute("UPDATE trial_item_task SET status='order_done' WHERE card=? AND item=?", (row["card"], item))
            j.db.execute("DELETE FROM trial_order_slot WHERE registration=? AND version=? AND item=?",
                         (row["registration"], row["version"], item))
            if disconnect:
                self._notice(j, row["card"], "order_disconnect", self._head(row, [item]) + ORDER_NOTICES["disconnect"])

    def _mismatch(self, job, item, record_id, disconnect):
        """写错即停：同一事务置停写位、记关建单开关请求、群里告警；同批余项与后续请求一律不发。"""
        row = job.row
        now = self.clock()
        try:
            with journal_at(self.path) as j, j.db:
                j.db.execute("BEGIN IMMEDIATE")
                j.db.execute("INSERT OR IGNORE INTO trial_halt VALUES(1,?,?)", ("readback_mismatch", now))
                j.db.execute("INSERT INTO trial_guardian_log VALUES(?,?,?)",
                             (now, "request_create_off", "readback_mismatch"))
                j.db.execute("""UPDATE trial_order SET status='mismatch', finished=?, reason='readback', disconnect=?
                             WHERE registration=? AND version=? AND item=?""",
                             (now, int(disconnect), row["registration"], row["version"], item))
                j.db.execute("UPDATE trial_item_task SET status='order_mismatch' WHERE card=? AND item=?",
                             (row["card"], item))
                j.db.execute("DELETE FROM trial_order_slot WHERE registration=? AND version=? AND item=?",
                             (row["registration"], row["version"], item))
                self._notice(j, row["card"], "order_mismatch", self._head(row, [item]) + ORDER_NOTICES["mismatch"])
                if disconnect:
                    self._notice(j, row["card"], "order_disconnect",
                                 self._head(row, [item]) + ORDER_NOTICES["disconnect"])
        except Exception:
            self.frozen = True
        _LOG.error("建单回读写错，已停写，请守望者关闭建单开关并停查。")

    def _unknown(self, job, item, plan, record_id, reason, *, disconnect=False, checks=None):
        """结果不明：冻结该条与在途槽（不释放、不重发、不清理），群里固定提示并 @ 配置负责人，恢复清单列出。"""
        row = job.row
        try:
            if record_id is None:
                self._ledger(job, item, plan, "create_unknown")
            else:
                self._ledger(job, item, plan, "readback_unconfirmed", record_id, 结论="未确认",
                             逐项核对=checks or [])
        except Exception:
            pass
        try:
            with journal_at(self.path) as j, j.db:
                j.db.execute("BEGIN IMMEDIATE")
                j.db.execute("""UPDATE trial_order SET status='unknown', reason=?, finished=?, disconnect=?
                             WHERE registration=? AND version=? AND item=?""",
                             (reason, self.clock(), int(disconnect), row["registration"], row["version"], item))
                j.db.execute("UPDATE trial_item_task SET status='order_unknown' WHERE card=? AND item=?",
                             (row["card"], item))
                self._notice(j, row["card"], f"order_unknown:{item}", self._head(row, [item]) + ORDER_NOTICES["unknown"])
                if disconnect:
                    self._notice(j, row["card"], "order_disconnect",
                                 self._head(row, [item]) + ORDER_NOTICES["disconnect_unknown"])
        except Exception:
            self.frozen = True
            _LOG.error("建单结果未知登记未成功，已冻结，转人工。")

    # ------------------------------------------------------------ 台账（#156 对接）

    def _abort_ledger(self, job, item, plan, code):
        try:
            self._ledger(job, item, plan, "aborted_before_write", allowed_writes=0)
            self._ledger(job, item, plan, "approval_closed", reason=code)
        except Exception:
            _LOG.error("未发出记录台账未写入。")

    def _ledger(self, job, item, plan, event, record_id=None, **extra):
        """沿用 X5 的 JSONL 台账格式与事件名；写前校验记录 ID 与卡片 ID 格式（#156 ①）。"""
        row = job.row
        require(canonical_card(row["card"]) and (record_id is None or uuid_record(record_id)), "台账标识格式不符")
        registration = ledger_registration(row["registration"], row["version"], item)
        entry = dict(registration=registration, event=event, name=plan.name, record_id=record_id, is_test=False,
                     utc=datetime.now(UTC).isoformat(), card_uuid=row["card"], trial_registration=row["registration"],
                     trial_version=row["version"], item=item, country=row["country"], target=plan.target, **extra)
        line = compact(entry) + "\n"
        check_outlet((entry, line))
        import os
        with ledger_open(self.ledger_dir, registration, plan.name) as ledger:
            ledger.stream.write(line)
            ledger.stream.flush()
            os.fsync(ledger.stream.fileno())


# ---------------------------------------------------------------- 生产适配（明道云）

class MingdaoBackend:
    """复用 X5 的组装与核对：草稿 → 字段 → 模板组装（任务审批链接 = 本批存档链接）→ 请求核对 → 建单 → 官方回读比对。

    配置含 050 网页公式字段时，照 X5 `_run_registered` 的同一路径取值与核对：`verified_order_rows`（平台行与资费）、
    `project_web_fields`、标签全名与版本复核、`approved_names` / `request_display`，回读把 `approved` 交给 `compare`。
    所需授权（`web_rules_sha256`、各表读范围、建单入口）来自按配置摘要绑定的正式单授权登记 `evidence`。
    """

    def __init__(self, config, *, official_token, official_app_id, login_token, options_data, sensitive,
                 evidence=None, bound=(), clock=time.time):
        from qianshu.mingdao import create_order as O
        self.O, self.config = O, config
        self.official = (official_token, official_app_id)
        self.login, self.options, self.sensitive = login_token, options_data, set(sensitive)
        self.snapshot = config.get("order_snapshot", {})
        self.target = config["create"]["identifiers"]["worksheetId"]
        self.skip = set(config["readback_skip"])
        self.evidence, self.bound, self.clock = evidence, tuple(bound), clock
        self.web, self.tags = O.order_profiles(config, evidence or {})

    @contextmanager
    def _guard(self):
        O = self.O
        guard = O.Outlet()
        official = self.config["official"]
        # 出口按 8 字符片段识别凭据：任一鉴权值短于 8 字符即无法拦截，按凭据冲突停；令牌与应用 ID 须分属两个位置，
        # 否则建单请求体 appId 的字段级豁免会连带放行令牌。
        if (official["token_key"] == official["app_id_key"]
                or not all(isinstance(value, str) and len(value) >= 8 and len(value.strip()) >= 8
                           for value in (*self.official, self.login))):
            raise O.OutletConflict()
        guard.configure({official["token_key"]: self.official[0], official["app_id_key"]: self.official[1],
                         ("cookie", 0): self.login})
        guard.session_owner = ("cookie", 0)
        guard.configuration_checked = True
        token = O._outlet.set(guard)
        try:
            yield guard
        finally:
            O._outlet.reset(token)

    @contextmanager
    def _api(self):
        import httpx
        from .sdk import CheckedHTTPTransport
        O = self.O
        with self._guard(), httpx.Client(timeout=httpx.Timeout(O._READ_TIMEOUT, connect=O._CONNECT_TIMEOUT),
                                          follow_redirects=False, trust_env=False) as client:
            api = O.Official(self.config["official"], *self.official, client)
            CheckedHTTPTransport(client, api.check_wire_request)
            yield api

    def _values(self, snapshot, item, day):
        O = self.O
        export = {k: snapshot[k] for k in ("sources", "drafts", "requires_full_review", "structure_issues")}
        draft, sources, owners = O.order_material(export, snapshot["drafts"][item - 1]["policy_id"])
        values, inactive, _, _, pending = O.order_fields(draft, sources, owners, day, self.options)
        require(not pending, "降包用户取消优惠须确认具体取值")
        return draft, sources, owners, values, inactive

    def card_face(self, snapshot, item, values):
        """出卡时冻结的卡面业务值（快照 reviewed）逐项核对建单取值；有任何差异即停单、需重新审批（审核 P1-2）。

        reviewed 是出卡时经校验的草稿（开始日期已按出卡当日顺延），与建单当日重算的取值逐字段比较，跨 UTC 日顺延即不符。
        """
        O = self.O
        reason = "经卡片确认的取值已变化，需重新审批"
        try:
            reviewed = snapshot["reviewed"][item - 1]
            shown = {key: field["value"] for key, field in reviewed.items()
                     if key in O._ORDER_FIELDS and isinstance(field, dict) and field.get("value") is not None}
        except (KeyError, IndexError, TypeError, AttributeError):
            raise O.Stop(reason) from None
        require(shown.keys() == values.keys() and all(compact(shown[k]) == compact(values[k]) for k in shown), reason)
        return shown

    def displayed(self, shown, controls, inactive):
        """台账 displayed 指纹：草稿可变字段取卡面快照值编码；模板、平台与固定字段没有单独的卡面值，取请求值。"""
        O = self.O
        result = {}
        for cell in controls:
            if cell["controlId"] in self.skip:
                continue
            spec = self.config["fields"][cell["controlId"]]
            key = spec.get("draft")
            encoded = cell["value"]
            if key in O._VARIABLE:
                encoded = O.encode_cell(None if key in inactive else shown.get(key), spec)
            result[spec["label"]] = O.digest(encoded.encode())[:16]
        return result

    def _proof_window(self):
        O = self.O
        if self.tags:
            require(self.clock() < O.utc(self.evidence["tag_scope"]["unique_until"]).timestamp(), "标签唯一性证明已到期")
        if self.web:
            require(self.clock() < O.utc(self.snapshot["concurrent_policies"]["valid_until"]).timestamp(),
                    "同期政策来源已过期")

    def plan(self, snapshot, item, url, *, now, sent):
        O = self.O
        require(not self.tags or self.evidence is not None, "正式单授权登记缺失，需人工")
        evidence = self.evidence or {}
        today = datetime.fromtimestamp(now, UTC).date()
        draft, sources, owners, values, inactive = self._values(snapshot, item, today)
        shown = self.card_face(snapshot, item, values)
        O.require_verified_combination(self.snapshot, values["policy_type"], values["benefit_type"])
        O.verify_company_inputs(draft, sources, owners, self.snapshot, self.config["template"]["country"]["value"])
        catalog_rate = O.check_catalog(values, self.snapshot, draft["field_results"])
        projected, assembly = O.x5_projection(values, self.config, inactive)
        O.check_search_binding(self.config, self.config["official"])
        self._proof_window()
        config, base_values = self.config, values
        create = O.CreateRequest(config["create"], evidence if self.evidence is not None
                                 else {"create_endpoint": config["create"]["endpoint"]}, self.login,
                                 app_owner=config["official"]["app_id_key"])
        with self._api() as api:
            template_id = config["template"]["row_id"]
            require(O.uuid_record(template_id), "模板记录编号须为 UUID")
            cells = api.detail(api.read(template_id), template_id)
            relations = {}

            def rows(control):
                relations[control] = api.relations(template_id, control)
                return relations[control]
            request_cells, request_relations, plan_names, sources_read = cells, relations, {}, {}
            if self.web:
                # 照 X5 050 路径：平台行、资费与同期政策按授权登记重读，公式与显示值用详情投影。
                for control, spec in config["fields"].items():
                    if spec.get("draft") in ("company", "user_tags", "packages"):
                        O.template_relation_rows(cells, control, rows, spec["label"])
                verified = O.verified_order_rows(api, config, evidence, values, cells, relations, catalog_rate,
                                                 self.clock)
                config, values = verified.config, verified.values
                request_cells, request_relations = verified.cells, verified.relations
                plan_names, sources_read = verified.plan_names, verified.sources
                projected, assembly = O.x5_projection(values, config, inactive)

            def web_fields(company_row, _packages):
                return O.project_web_fields(config, evidence, values, company_row, request_cells, self.clock)
            arrays = {c for c, s in config["fields"].items() if s.get("draft") in values
                      and "draft" not in assembly["fields"][c] and s["type"] in (10, 26, 29, 34)}
            controls, summary, web = O.assemble_controls(
                projected, request_cells, assembly, request_relations.__getitem__ if self.web else rows,
                is_test=False, array_controls=arrays, web_fields=web_fields if self.web else None, approval_url=url)
            selected = tag_version = None
            if self.tags:
                selected = O.selected_template_tags(config, controls, relations, values, self.snapshot)
                tag_version = (sources_read["tags"][0] if self.web else
                               api.verified_tag_version(evidence.get("tag_scope"), values["user_tags"], selected,
                                                        self.clock))
            payload = create.payload(controls)
            O.verify_order_request(payload, config, request_cells, request_relations, values, inactive, self.sensitive,
                                   draft["field_results"]["packages"]["statement"])
            # 明道云建单请求体：只豁免顶层 appId（与官方应用 ID 逐字相等），其余位置照常按凭据拦截。
            create.check_body(payload)
            require(O.decode(payload)["receiveControls"] == controls, "请求与已核控件不一致")
            approved = O.approved_names(controls, config, values, web, plan_names, request_cells) if self.web else None
            if self.web:
                O.request_display(payload, config, values, inactive, summary, web, plan_names, approved)
            # 官方查重：查询参数按原串发出；候选行名称与本条按同一归一（name_key）比对，归一同名即命中（G506）。
            require(not api.find(values["policy_name"], fold=name_key), "已存在同名记录，需人工")
        labels = {c["controlId"]: config["fields"][c["controlId"]]["label"] for c in controls}
        sent_values = {labels[c["controlId"]]: O.digest(c["value"].encode())[:16]
                       for c in O.decode(payload)["receiveControls"] if c["controlId"] not in self.skip}
        fingerprints = self.displayed(shown, controls, inactive)
        require(fingerprints == sent_values, "经卡片确认的取值与请求不一致，需重新审批")
        # 交接前来源复核的原值（照 X5 bound_sources：模板及关联、网页路径的平台来源）。
        source = {"values": base_values, "catalog_rate": catalog_rate, "cells": cells, "relations": dict(relations),
                  "platform": self._platform_digest(sources_read) if self.web else None,
                  "draft_start": draft["start_date"]["value"], "start_date": base_values["start_date"]}
        # 应回读的字段 = 全部已写控件（含 readback_skip：写入值均须回读，CREATE_ORDER.md）。
        written = list(dict.fromkeys(labels[c["controlId"]] for c in O.decode(payload)["receiveControls"]))
        return Plan(values["policy_name"], self.target, written, fingerprints, sent_values, payload,
                    {"controls": controls, "packages": values["packages"], "company": values["company"],
                     "config": config, "approved": approved, "tags": values["user_tags"], "selected": selected,
                     "tag_version": tag_version, "source": source})

    def _platform_digest(self, sources):
        """平台来源原值指纹（verified_order_rows 绑定的平台行、资费、标签、同期政策、升包任务）。"""
        O = self.O
        return O.digest(compact([[key, initial] for key, (initial, _) in sources.items()]).encode())

    def _local(self):
        for initial, read in self.bound:
            require(read() == initial, "建单配置或授权登记已变化，需重新审批")

    def _sources(self, api, source):
        """照 X5 `_run_registered` 交接前核对：按原标识重读模板及关联；网页路径再以同一 `verified_order_rows`
        重读并核对平台行、标签详情、资费、同期政策与适用的升包任务。来源变化或读不到即停单、需重新审批（修复轮 2 第 5 条）。"""
        O = self.O
        reason = "建单来源已变化，需重新审批"
        template_id = self.config["template"]["row_id"]
        cells = api.detail(api.read(template_id), template_id)
        relations = {key: api.relations(template_id, key) for key in source["relations"]}
        require(compact(cells) == compact(source["cells"]) and compact(relations) == compact(source["relations"]),
                reason)
        if self.web:
            verified = O.verified_order_rows(api, self.config, self.evidence, source["values"], cells, relations,
                                             source["catalog_rate"], self.clock)
            require(self._platform_digest(verified.sources) == source["platform"], reason)

    def recheck(self, plan):
        """交接前复核（照 X5 交接前核对）：证明有效期；先核本地配置与授权登记（撤权后不按旧登记查平台）；重读模板及关联与
        平台来源；标签全名唯一性版本；平台读取之后再核一次本地配置与授权登记；最后按 X5 判据再核开始日期与证明有效期。"""
        O = self.O
        extra = plan.extra
        try:
            self._proof_window()
            self._local()
            with self._api() as api:
                self._sources(api, extra["source"])
                if self.tags:
                    current = api.verified_tag_version(self.evidence.get("tag_scope"), extra["tags"],
                                                       extra["selected"], self.clock)
                    require(current == extra["tag_version"], "所选标签全名或版本已变化")
            self._local()
        except O.OutletConflict:
            raise
        except Exception:
            raise Refused("plan") from None
        try:
            self._start_window(extra["source"])
        except O.OutletConflict:
            raise
        except Exception:
            raise Refused("reapprove") from None

    def _start_window(self, source):
        """照 X5 `check_start_date`：耗时的来源重读完成后、写出前，用 X5 同一判据（`effective_start_date` 与证明有效期）
        再核一次。重读跨过 UTC 零点致开始日期需顺延，或证明在重读期间过期 → 不写出、需重新审批（特批第 3 轮第 3 条）。"""
        O = self.O
        today = datetime.fromtimestamp(self.clock(), UTC).date()
        approved = O.effective_start_date(date.fromisoformat(source["draft_start"]), today)
        require(source["start_date"] == approved.isoformat(), "经卡片确认的开始日期已过，需重新审批")
        self._proof_window()

    def send(self, plan, handoff):
        """预建连后先复核来源，再由 handoff 在实际交接处复核；不符或交接前失败均确证未写出。"""
        O = self.O
        create = O.CreateRequest(self.config["create"], self.evidence if self.evidence is not None
                                 else {"create_endpoint": self.config["create"]["endpoint"]}, self.login,
                                 app_owner=self.config["official"]["app_id_key"])

        def check():
            self.recheck(plan)
            handoff()
        with self._guard():
            try:
                with create.prepare(plan.payload) as (transmit, read_id):
                    transmit(check)
                    return read_id()
            except Refused as error:
                if not create.started:
                    raise NotSent(error.code) from None
                raise
            except Exception:
                if not create.started:
                    raise NotSent from None
                raise

    def readback(self, plan, record_id, budget):
        O = self.O
        extra = plan.extra
        with self._api() as api:
            raw = api.read(record_id, budget=budget)
            if raw is O._MISSING:
                return None, []
            observed = api.detail(raw, record_id)
            tag_names = None
            if self.tags:
                def tag_names():
                    return O.readback_tag_names_check(api, self.evidence["tag_scope"], extra["tags"],
                                                      extra["selected"])
            report, _ = O.compare(extra["controls"], observed, extra["config"],
                                  read_relations=lambda key: api.relations(record_id, key),
                                  packages=extra["packages"], company=extra["company"], tag_names=tag_names,
                                  approved=extra["approved"])
        return readback_verdict(report)


def readback_verdict(report):
    """正式单回读结论直接采用 X5 `compare` 的逐项结果与结论，覆盖全部已写控件（含 readback_skip），不自行放宽。

    任一已写字段回读缺失或不一致 → 「写错」（守望者 12:0xZ：走写错即停——停写位、后续 0 发、告警）；
    `compare` 判「一致」且逐项全一致才为「一致」；其余（含「无法核对」）→ 无结论，按结果未知冻结、停住下一条。
    """
    rows = report["逐项核对"]
    results = [r["结果"] for r in rows]
    if report["结论"] == "写错" or any(r in ("不一致", "缺失") for r in results):
        verdict = "写错"
    elif report["结论"] == "一致" and all(r == "一致" for r in results):
        verdict = "一致"
    else:
        return None, []
    return verdict, [{"字段名": r["字段名"], "结果": "一致" if r["结果"] == "一致" else "不一致"} for r in rows]


AUTHORIZATIONS_FILE = "create-authorizations.json"
# 非网页正式路径收窄（特批第 3 轮第 2 条）：不含网页公式字段规则的正式单配置不装配建单执行器，日志写明原因。
NO_WEB_RULES = "正式单建单配置不含网页公式字段规则（order_snapshot.web_rules），建单执行器不装配，建单 0。"
# 050 网页字段路径须在正式单授权登记里登记的项（见 CREATE_ORDER.md「050 的字段、授权与回读」）。
WEB_EVIDENCE = ("web_rules_sha256", "tag_scope", "concurrent_scope", "tariffs_scope", "products_scope", "plans_scope",
                "company_rows_scope", "create_endpoint")


def formal_authorization(path, config_data):
    """正式单授权登记：与明道云配置同目录的私有授权登记中，`is_test` 为假且 `config_sha256` 等于本配置
    完整字节摘要的唯一一条。缺失、多条或不可读返回 None（不装配）。"""
    from qianshu.mingdao.create_order import decode, digest, private_bytes
    try:
        registry = decode(private_bytes(path))
    except Exception:
        return None
    if not isinstance(registry, dict):
        return None
    wanted = digest(config_data)
    matched = [entry for entry in registry.values() if isinstance(entry, dict) and entry.get("is_test") is False
               and entry.get("config_sha256") == wanted]
    return matched[0] if len(matched) == 1 else None


def build_order_runner(args, settings, state, *, switches, connected, sensitive, archive_runner, switch_file=None):
    """未确认配置下不装配：存档执行器已装配、建单开关开、明道云配置与凭据来源、登录态均已就绪；
    正式单建单配置须含 050 网页公式字段规则（`order_snapshot.web_rules`，特批第 3 轮第 2 条：非网页正式路径不装配），
    另须有按配置摘要绑定的正式单授权登记，且公式授权指纹相符。X5 命令行不经此处，行为不变。"""
    if archive_runner is None:
        return None
    try:
        from qianshu.mingdao.create_order import decode, digest, extract_token, order_profiles, private_bytes
        current = switches()
        if current.get("MAIL_TRIAL_CREATE_ENABLED") != "true" or current.get("MAIL_TRIAL_CARD_ENABLED") != "true":
            return None
        if args.mingdao_config is None or not settings.trial_credentials_complete or settings.trial_login_dir is None:
            return None
        config_data = private_bytes(args.mingdao_config)
        config = decode(config_data)
        snapshot = config.get("order_snapshot")
        if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("verified_combinations"), list)
                or not all(k in config for k in ("create", "official", "template", "fields", "fixed",
                                                  "readback_skip", "applicant_control"))):
            return None
        if "web_rules" not in snapshot:
            _LOG.warning(NO_WEB_RULES)
            return None
        credentials = dict(settings.trial_credentials)
        official = config["official"]
        token, app = credentials.get(official["token_key"]), credentials.get(official["app_id_key"])
        if not (receipt(token) and receipt(app)):
            return None
        login = extract_token(decode(private_bytes(settings.trial_login_dir / "mingdao-state.json")),
                              config["create"]["session_cookie"])
        options = (Path(__file__).resolve().parents[3] / "rules" / "parse-options.tsv").read_bytes()
        evidence, bound = None, []
        web, tags = order_profiles(config, {})
        if web or tags:
            registry = args.mingdao_config.parent / AUTHORIZATIONS_FILE
            evidence = formal_authorization(registry, config_data)
            if evidence is None:
                return None
            web, tags = order_profiles(config, evidence)
            if web and (not all(k in evidence for k in WEB_EVIDENCE) or evidence["web_rules_sha256"] != digest(
                    compact(snapshot.get("web_rules")).encode())):
                return None
            if tags and "tag_scope" not in evidence:
                return None
            config_path = args.mingdao_config
            bound = [(digest(config_data), lambda: digest(private_bytes(config_path))),
                     (compact(evidence), lambda: compact(formal_authorization(registry, private_bytes(config_path))))]
        backend = MingdaoBackend(config, official_token=token, official_app_id=app, login_token=login,
                                 options_data=options, sensitive=sensitive, evidence=evidence, bound=bound)
        seconds = int(current.get(READBACK_KEY, "120"))
    except Exception:
        return None
    return OrderRunner(state, switches=switches, backend=backend, connected=connected, ledger_dir=state.parent,
                       readback_seconds=seconds, switch_file=switch_file)
