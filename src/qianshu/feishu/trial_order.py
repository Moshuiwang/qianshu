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
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
CANCEL_REASONS = {
    "consent": "同意已失效或卡片已关闭",
    "switch": "建单开关已关闭",
    "halted": "建单已停写",
    "expired": "审批到期",
    "disconnected": "断线",
    "combination": "该类型尚未经真实回读验证",
    "archive": "取不到本批存档链接",
    "handoff": "该条已交人工处理，须守望者销清移交后才可再建",
    "plan": "建单字段未能核实",
    "format": "台账标识格式不符",
    "ledger": "建单台账未能写入",
    "not_sent": "建单请求未发出",
    "previous": "同批前序条目未建成",
    "slot": "本条发送登记已变化",
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


def notice_items(kind, snapshot):
    """「未建单」公告涉及的条目。"""
    if kind.startswith("closed_"):
        return [int(kind.split("_", 1)[1])]
    if kind.startswith("order_cancel:"):
        return [int(n) for n in kind.split(":", 1)[1].split(",")]
    return list(snapshot.get("eligible") or snapshot.get("items") or [])


def unbuilt_notice(db, row):
    """旧「未建单」公告投递前核对（cd-carry P1-1）。返回 (实际投递文案, 投递后登记移交的条目)。

    条目按目标表 + 归一政策名核对：若已被新卡接管或已建单，改为固定的过时文案，不登记移交。
    """
    body = row["body"]
    if row["kind"].startswith("order_unknown:") or row["kind"] == "order_mismatch":
        # 结果未知、写错：同样已交人工，登记移交；释放在途槽也经守望者销清移交。
        return body, [(r[0], r[1]) for r in db.execute("""SELECT item, name_key FROM trial_order WHERE card=?
                      AND status IN ('unknown','mismatch') AND name_key IS NOT NULL""", (row["card"],))]
    if "未建单" not in body:
        return body, []
    snapshot = json.loads(row["snapshot"])
    items = []
    for item in notice_items(row["kind"], snapshot):
        key = name_key(_reviewed_name(snapshot, item))
        if not key:
            continue
        taken = db.execute(f"""SELECT 1 FROM trial_order WHERE name_key=? AND card!=?
                           AND status IN ({','.join('?' * len(ACTIVE))})""", (key, row["card"], *ACTIVE)).fetchone()
        if taken:
            return f"登记 {_card_reg(db, row['card'])} · {SUPERSEDED}", []
        items.append((item, key))
    return body, items


def _card_reg(db, card):
    reg, version = db.execute("SELECT registration,version FROM trial_card WHERE card=?", (card,)).fetchone()
    return f"{reg} · 版本 {version}"


def record_handoff(db, card, items, now):
    """「未建单」公告已投递：条目交人工。须守望者销清移交后机器人才允许再接管同一政策。"""
    reg, version = db.execute("SELECT registration,version FROM trial_card WHERE card=?", (card,)).fetchone()
    for item, key in items:
        target = db.execute("SELECT target FROM trial_order WHERE registration=? AND version=? AND item=?",
                            (reg, version, item)).fetchone()
        db.execute("INSERT INTO trial_handoff VALUES(?,?,?,?,?,?,NULL,NULL)",
                   (key, target[0] if target else None, reg, version, item, now))


def release_handoff(db, reg, version, item, fingerprint, reason, now):
    """守望者销清移交（产品负责人当场同意后）。登记 ID 与政策名指纹都对上才写；返回 (改前, 改后) 状态。"""
    require(receipt(reason), "须写明原因")
    rows = db.execute("""SELECT name_key FROM trial_handoff WHERE registration=? AND version=? AND item=?
                      AND cleared IS NULL""", (reg, version, item)).fetchall()
    keys = {r[0] for r in rows}
    require(len(keys) == 1 and name_fingerprint(next(iter(keys))) == fingerprint, "登记 ID 或政策名指纹与登记库不符")
    key = next(iter(keys))
    before = handoff_state(db, key)
    db.execute("UPDATE trial_handoff SET cleared=?, reason=? WHERE name_key=? AND cleared IS NULL", (now, reason, key))
    held = db.execute("""SELECT 1 FROM trial_order_slot s JOIN trial_order o USING(registration,version,item)
                      WHERE o.name_key=?""", (key,)).fetchone()
    if held:
        db.execute("DELETE FROM trial_order_slot WHERE id=1")
    db.execute("INSERT INTO trial_guardian_log VALUES(?,?,?)",
               (now, f"release_handoff {reg}-{version}-{item} {name_fingerprint(key)}", reason))
    return before, handoff_state(db, key)


def handoff_state(db, key):
    open_count = db.execute("SELECT count(*) FROM trial_handoff WHERE name_key=? AND cleared IS NULL",
                            (key,)).fetchone()[0]
    slot = db.execute("""SELECT 1 FROM trial_order_slot s JOIN trial_order o USING(registration,version,item)
                      WHERE o.name_key=?""", (key,)).fetchone()
    orders = [r[0] for r in db.execute("SELECT status FROM trial_order WHERE name_key=? ORDER BY sent", (key,))]
    return {"name_fp": name_fingerprint(key), "open_handoffs": open_count, "slot_held": bool(slot), "orders": orders}


class OrderRunner:
    """每轮至多处理一张存档已建、待建单的卡。同批逐条串行，全局至多 1 个在途建单请求。"""

    def __init__(self, path, *, switches, backend, connected, ledger_dir, clock=time.time, pause=time.sleep,
                 readback_seconds=120):
        self.path, self.switches, self.backend, self.connected = path, switches, backend, connected
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
            current = j.db.execute("SELECT * FROM trial_card WHERE card=?", (card,)).fetchone()
            task = j.db.execute("SELECT status FROM trial_item_task WHERE card=? AND item=?", (card, item)).fetchone()
            if not self._up():
                return "disconnected"
            if (current is None or current["status"] != "accepted" or current["format"] != CARD_FORMAT
                    or current["fingerprint"] != row["fingerprint"]
                    or sha256(current["snapshot"].encode()).hexdigest() != current["fingerprint"]
                    or not current["session"] or current["session"] != j.session()
                    or trial_batch(j.db, reg, version)[3] or not task or task[0] != "archive_done"):
                return "consent"
            if self.clock() >= current["expires"]:
                return "expired"
            if (switches.get("MAIL_TRIAL_CREATE_ENABLED") != "true" or switches.get("MAIL_TRIAL_CARD_ENABLED") != "true"
                    or switches.get("MAIL_TRIAL_CREATE_COUNTRY") != current["country"]):
                return "switch"
            if j.write_halted():
                return "halted"
            if j.db.execute("SELECT 1 FROM trial_order_slot").fetchone():
                return "busy"
            now = self.clock()
            j.db.execute("INSERT INTO trial_order_slot VALUES(1,?,?,?,?)", (reg, version, item, now))
            j.db.execute("""INSERT INTO trial_order(registration,version,item,card,status,name_key,target,sent)
                         VALUES(?,?,?,?,'sending',?,?,?)""", (reg, version, item, card, key, plan.target, now))
            j.db.execute("UPDATE trial_item_task SET status='order_running' WHERE card=? AND item=?", (card, item))
            return ""

    def _handoff(self, job, item, state):
        """实际交接回调：预建连之后、真正写出请求之前再核截止、建单开关、停写位、本条持槽与发送记录。

        任一不符即抛出，传输层不写出（确证未发出）。此刻才观察到断线的，是登记先于断线的那 1 张：照发、单独计数（G464）。
        """
        row = job.row
        reg, version = row["registration"], row["version"]

        def check():
            try:
                switches = self.switches()
            except Exception:
                switches = {}
            with journal_at(self.path) as j:
                card = j.db.execute("SELECT country, expires FROM trial_card WHERE card=?", (row["card"],)).fetchone()
                slot = j.db.execute("SELECT registration, version, item FROM trial_order_slot WHERE id=1").fetchone()
                sending = j.db.execute("""SELECT status FROM trial_order WHERE registration=? AND version=?
                                       AND item=?""", (reg, version, item)).fetchone()
                halted = j.write_halted()
            if card is None or self.clock() >= card["expires"]:
                raise Refused("expired")
            if (not isinstance(switches, dict) or switches.get("MAIL_TRIAL_CREATE_ENABLED") != "true"
                    or switches.get("MAIL_TRIAL_CARD_ENABLED") != "true"
                    or switches.get("MAIL_TRIAL_CREATE_COUNTRY") != card["country"]):
                raise Refused("switch")
            if halted:
                raise Refused("halted")
            if slot is None or tuple(slot) != (reg, version, item) or sending is None or sending[0] != "sending":
                raise Refused("slot")
            if not self._up():
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

    def _cancel(self, job, items, code):
        """未发的条目撤销：置终态并公告「未建单」@ 配置负责人。"""
        if not items:
            return
        row = job.row
        try:
            with journal_at(self.path) as j, j.db:
                j.db.execute("BEGIN IMMEDIATE")
                marks = ",".join("?" * len(items))
                j.db.execute(f"""UPDATE trial_item_task SET status='closed' WHERE card=? AND item IN ({marks})
                             AND status IN ('archive_done','order_running')""", (row["card"], *items))
                self._notice(j, row["card"], "order_cancel:" + ",".join(map(str, items)),
                             self._head(row, items) + CANCEL_REASONS[code] + "，未建单，已转人工。")
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
    """复用 X5 的组装与核对：草稿 → 字段 → 模板组装（任务审批链接 = 本批存档链接）→ 请求核对 → 建单 → 官方回读比对。"""

    def __init__(self, config, *, official_token, official_app_id, login_token, options_data, sensitive):
        from qianshu.mingdao import create_order as O
        self.O, self.config = O, config
        self.official = (official_token, official_app_id)
        self.login, self.options, self.sensitive = login_token, options_data, set(sensitive)
        self.snapshot = config.get("order_snapshot", {})
        self.target = config["create"]["identifiers"]["worksheetId"]
        self.skip = set(config["readback_skip"])

    @contextmanager
    def _guard(self):
        O = self.O
        guard = O.Outlet()
        official = self.config["official"]
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

    def plan(self, snapshot, item, url, *, now, sent):
        O = self.O
        today = datetime.fromtimestamp(now, UTC).date()
        draft, sources, owners, values, inactive = self._values(snapshot, item, today)
        shown = self.card_face(snapshot, item, values)
        O.require_verified_combination(self.snapshot, values["policy_type"], values["benefit_type"])
        O.verify_company_inputs(draft, sources, owners, self.snapshot, self.config["template"]["country"]["value"])
        O.check_catalog(values, self.snapshot, draft["field_results"])
        projected, assembly = O.x5_projection(values, self.config, inactive)
        with self._api() as api:
            template_id = self.config["template"]["row_id"]
            require(O.uuid_record(template_id), "模板记录编号须为 UUID")
            cells = api.detail(api.read(template_id), template_id)
            relations = {}

            def rows(control):
                relations[control] = api.relations(template_id, control)
                return relations[control]
            arrays = {c for c, s in self.config["fields"].items() if s.get("draft") in values
                      and "draft" not in assembly["fields"][c] and s["type"] in (10, 26, 29, 34)}
            controls, _, _ = O.assemble_controls(projected, cells, assembly, rows, is_test=False,
                                                 array_controls=arrays, approval_url=url)
            create = O.CreateRequest(self.config["create"], {"create_endpoint": self.config["create"]["endpoint"]},
                                     self.login)
            payload = create.payload(controls)
            O.verify_order_request(payload, self.config, cells, relations, values, inactive, self.sensitive,
                                   draft["field_results"]["packages"]["statement"])
            require(O.decode(payload)["receiveControls"] == controls, "请求与已核控件不一致")
            require(not api.find(values["policy_name"]), "已存在同名记录，需人工")
        labels = {c["controlId"]: self.config["fields"][c["controlId"]]["label"] for c in controls}
        sent_values = {labels[c["controlId"]]: O.digest(c["value"].encode())[:16]
                       for c in O.decode(payload)["receiveControls"] if c["controlId"] not in self.skip}
        fingerprints = self.displayed(shown, controls, inactive)
        require(fingerprints == sent_values, "经卡片确认的取值与请求不一致，需重新审批")
        return Plan(values["policy_name"], self.target, list(sent_values), fingerprints, sent_values, payload,
                    {"controls": controls, "packages": values["packages"], "company": values["company"]})

    def send(self, plan, handoff):
        """预建连后由 handoff 在实际交接处复核；复核不符或交接前失败均确证未写出。"""
        O = self.O
        create = O.CreateRequest(self.config["create"], {"create_endpoint": self.config["create"]["endpoint"]},
                                 self.login)
        with self._guard():
            try:
                with create.prepare(plan.payload) as (transmit, read_id):
                    transmit(handoff)
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
        with self._api() as api:
            raw = api.read(record_id, budget=budget)
            if raw is O._MISSING:
                return None, []
            observed = api.detail(raw, record_id)
            report, _ = O.compare(plan.extra["controls"], observed, self.config,
                                  read_relations=lambda key: api.relations(record_id, key),
                                  packages=plan.extra["packages"], company=plan.extra["company"])
        rows = [r for c, r in zip(plan.extra["controls"], report["逐项核对"]) if c["controlId"] not in self.skip]
        results = [r["结果"] for r in rows]
        if all(r == "一致" for r in results):
            verdict = "一致"
        elif any(r in ("不一致", "缺失") for r in results):
            verdict = "写错"
        else:
            return None, []
        return verdict, [{"字段名": r["字段名"], "结果": "一致" if r["结果"] == "一致" else "不一致"} for r in rows]


def build_order_runner(args, settings, state, *, switches, connected, sensitive, archive_runner):
    """未确认配置下不装配：存档执行器已装配、建单开关开、明道云配置与凭据来源、登录态均已就绪。"""
    if archive_runner is None:
        return None
    try:
        from qianshu.mingdao.create_order import decode, extract_token, private_bytes
        current = switches()
        if current.get("MAIL_TRIAL_CREATE_ENABLED") != "true" or current.get("MAIL_TRIAL_CARD_ENABLED") != "true":
            return None
        if args.mingdao_config is None or not settings.trial_credentials_complete or settings.trial_login_dir is None:
            return None
        config = decode(private_bytes(args.mingdao_config))
        snapshot = config.get("order_snapshot")
        if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("verified_combinations"), list)
                or not all(k in config for k in ("create", "official", "template", "fields", "fixed",
                                                  "readback_skip", "applicant_control"))):
            return None
        credentials = dict(settings.trial_credentials)
        official = config["official"]
        token, app = credentials.get(official["token_key"]), credentials.get(official["app_id_key"])
        if not (receipt(token) and receipt(app)):
            return None
        login = extract_token(decode(private_bytes(settings.trial_login_dir / "mingdao-state.json")),
                              config["create"]["session_cookie"])
        options = (Path(__file__).resolve().parents[3] / "rules" / "parse-options.tsv").read_bytes()
        backend = MingdaoBackend(config, official_token=token, official_app_id=app, login_token=login,
                                 options_data=options, sensitive=sensitive)
        seconds = int(current.get(READBACK_KEY, "120"))
    except Exception:
        return None
    return OrderRunner(state, switches=switches, backend=backend, connected=connected, ledger_dir=state.parent,
                       readback_seconds=seconds)
