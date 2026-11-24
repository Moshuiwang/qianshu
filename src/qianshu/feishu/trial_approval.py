"""试运行审批私有台账。格式 1 仅记录决定，执行器永不可消费。"""

import json
import sqlite3
from contextlib import closing, contextmanager
from hashlib import sha256
from uuid import uuid4

from .state import trial_batch

CARD_FORMAT = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS trial_session(id INTEGER PRIMARY KEY CHECK(id=1), session TEXT);
CREATE TABLE IF NOT EXISTS trial_card(
 card TEXT PRIMARY KEY, registration INTEGER, version INTEGER, format INTEGER, session TEXT,
 country TEXT, snapshot TEXT, fingerprint TEXT, readonly INTEGER, sent REAL, expires REAL,
 status TEXT, UNIQUE(registration,version));
CREATE TABLE IF NOT EXISTS trial_bound(card TEXT PRIMARY KEY, at REAL);
CREATE TABLE IF NOT EXISTS trial_delivery(uuid TEXT PRIMARY KEY, message TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS trial_event(event TEXT PRIMARY KEY, consumed INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS trial_click_candidate(event TEXT PRIMARY KEY, card TEXT, action TEXT, actor TEXT,
 created REAL, recorded REAL, fingerprint TEXT);
CREATE TABLE IF NOT EXISTS trial_receipt(card TEXT, action TEXT, event TEXT UNIQUE, actor TEXT,
 created REAL, recorded REAL, fingerprint TEXT, PRIMARY KEY(card,action));
CREATE TABLE IF NOT EXISTS trial_task(card TEXT PRIMARY KEY, status TEXT);
CREATE TABLE IF NOT EXISTS trial_notification(card TEXT, kind TEXT, uuid TEXT UNIQUE, body TEXT,
 status TEXT, PRIMARY KEY(card,kind));
"""


@contextmanager
def journal_at(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=0.25)) as db:
        db.row_factory = sqlite3.Row
        yield Journal(db)


def recovery(db):
    rows = []
    for table, condition in (("trial_card", "status IN ('preparing','open','accepted','partial')"),
                             ("trial_task", "status!='closed'"),
                             ("trial_notification", "status IN ('pending','unknown')")):
        rows.extend(dict(row) for row in db.execute(
            f"SELECT '{table}' AS kind, card, status FROM {table} WHERE {condition}"))
    rows.extend(dict(row) for row in db.execute("""
        SELECT 'card_intent' AS kind, registration, version, page, status FROM card_intent
        WHERE status IN ('pending','unknown')"""))
    rows.extend(dict(row) for row in db.execute("""
        SELECT 'mail_notice' AS kind, registration, kind AS notice, status FROM notice
        WHERE status IN ('pending','review','unknown')"""))
    return rows


class Journal:
    def __init__(self, db):
        self.db = db

    def session(self):
        row = self.db.execute("SELECT session FROM trial_session WHERE id=1").fetchone()
        return row[0] if row else ""

    def prepare(self, card, reg, version, session, country, snapshot, readonly, sent):
        packed = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        with self.db:
            self.db.execute("""INSERT INTO trial_card VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (card, reg, version, CARD_FORMAT, session, country, packed,
                             sha256(packed.encode()).hexdigest(), int(readonly), sent,
                             (int(sent) // 86400 + 2) * 86400, "preparing"))

    def bind(self, card, now):
        row = self.db.execute("SELECT * FROM trial_card WHERE card=?", (card,)).fetchone()
        if row["status"] == "partial":
            self.close(row, "卡片送达未确认")
        elif row["status"] == "preparing":
            if row["readonly"]:
                self.db.execute("UPDATE trial_card SET status='readonly' WHERE card=?", (card,))
                return
            reg, version = row["registration"], row["version"]
            if self.session() != row["session"] or trial_batch(self.db, reg, version)[3]:
                self.close(row, "批次不可用")
            else:
                self.db.execute("UPDATE trial_card SET status='open' WHERE card=?", (card,))
                self.db.execute("INSERT INTO trial_bound VALUES(?,?)", (card, now))

    def received(self, key, message):
        if not isinstance(message, str) or not message.strip():
            raise ValueError("群消息回执未知")
        self.db.execute("INSERT INTO trial_delivery VALUES(?,?)", (key, message))

    def notify(self, row, kind, reason):
        snapshot = json.loads(row["snapshot"])
        items = "、".join(str(i) for i in snapshot["items"])
        missing = "、".join(snapshot["missing"]) or "无"
        body = f"登记 {row['registration']} · 条目 {items} · {reason}。待补项：{missing}。"
        self.db.execute("INSERT OR IGNORE INTO trial_notification VALUES(?,?,?,?,'pending')",
                        (row["card"], kind, str(uuid4()), body))

    def close(self, row, reason):
        self.db.execute("UPDATE trial_card SET status='closed' WHERE card=?", (row["card"],))
        self.db.execute("UPDATE trial_task SET status='closed' WHERE card=?", (row["card"],))
        self.db.execute("""UPDATE trial_notification SET status='cancelled' WHERE card=?
                        AND kind='remind' AND status='pending'""", (row["card"],))
        self.notify(row, "closed", reason + "，未建单，转人工")

    def rotate(self, session, reason):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            for row in self.db.execute("SELECT * FROM trial_card WHERE status IN ('preparing','open','accepted','partial')"):
                self.close(row, reason)
            self.db.execute("INSERT OR REPLACE INTO trial_session VALUES(1,?)", (session,))

    def scan(self, now, *, create_enabled=True):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            for row in self.db.execute("SELECT * FROM trial_card WHERE status IN ('open','accepted')"):
                if not create_enabled and row["status"] == "accepted":
                    self.close(row, "建单开关关闭")
                elif trial_batch(self.db, row["registration"], row["version"])[3]:
                    self.close(row, "批次冻结或版本失效")
                elif now >= row["expires"]:
                    self.close(row, "审批到期")
                elif now >= row["expires"] - 86400 and not self.db.execute(
                    "SELECT 1 FROM trial_receipt WHERE card=?", (row["card"],)).fetchone():
                    self.notify(row, "remind", "尚无有效点击，请核对本批审批")

    def claim_event(self, event):
        with self.db:
            return self.db.execute("INSERT OR IGNORE INTO trial_event VALUES(?,0)", (event,)).rowcount == 1

    def action(self, session, card, reg, version, message, action, event, actor, created,
               clock, current, member, switches, *, reason="", claimed=False):
        refused = "卡片绑定、身份或有效期不符"
        reload_switches = switches if callable(switches) else None
        if reload_switches:
            switches = reload_switches()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            claim = (self.db.execute("UPDATE trial_event SET consumed=1 WHERE event=? AND consumed=0", (event,))
                     if claimed else self.db.execute("INSERT OR IGNORE INTO trial_event VALUES(?,1)", (event,)))
            if not claim.rowcount:
                return "该回调已处理"
            row = self.db.execute("SELECT * FROM trial_card WHERE card=?", (card,)).fetchone()
            if (not row or row["format"] != CARD_FORMAT or not session or session != self.session()
                    or session != row["session"] or row["readonly"] or row["status"] != "open"
                    or (reg, version) != (row["registration"], row["version"])
                    or action not in {"verify", "approve", "reject"} or member is not True
                    or not actor or actor not in switches.get("MAIL_TRIAL_APPROVER_OPEN_IDS", "").split(",")
                    or switches.get("MAIL_TRIAL_CARD_ENABLED") != "true" or not row["country"]
                    or switches.get("MAIL_TRIAL_CREATE_COUNTRY") != row["country"]
                    or trial_batch(self.db, reg, version)[3] or not current()):
                return refused
            bound = self.db.execute("SELECT at FROM trial_bound WHERE card=?", (card,)).fetchone()
            if not bound or created < bound[0]:
                return refused
            if sha256(row["snapshot"].encode()).hexdigest() != row["fingerprint"]:
                return refused
            snapshot = json.loads(row["snapshot"])
            pages = list(self.db.execute("""SELECT i.page,d.message FROM card_intent i JOIN trial_delivery d
                USING(uuid) WHERE registration=? AND version=? AND status='sent' ORDER BY page""", (reg, version)))
            if (not snapshot["eligible"] or not snapshot["preview"] or not pages
                    or [r[0] for r in pages] != list(range(1, len(snapshot["pages"]) + 1))
                    or pages[-1][1] != message or not row["sent"] <= created <= clock() < row["expires"]):
                return refused
            if action == "approve":
                if switches.get("MAIL_TRIAL_CREATE_ENABLED") != "true":
                    return "建单未开启"
                verified = self.db.execute(
                    "SELECT recorded FROM trial_receipt WHERE card=? AND action='verify'", (card,)).fetchone()
                if not verified or created < verified[0]:
                    return "请先核验邮件审批"
            self.db.execute("INSERT INTO trial_click_candidate VALUES(?,?,?,?,?,?,?)",
                            (event, card, action, actor, created, clock(), row["fingerprint"]))
        # 收据证明引用已经落盘的本次点击。迟到、崩溃或断线的候选永不补签。
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if reload_switches:
                latest = reload_switches()
                if (latest.get("MAIL_TRIAL_CARD_ENABLED") != "true"
                        or latest.get("MAIL_TRIAL_CREATE_COUNTRY") != row["country"]
                        or actor not in latest.get("MAIL_TRIAL_APPROVER_OPEN_IDS", "").split(",")):
                    return refused
                if action == "approve" and latest.get("MAIL_TRIAL_CREATE_ENABLED") != "true":
                    return "建单未开启"
            if not current() or clock() >= row["expires"] or session != self.session():
                return refused
            if not self.db.execute("SELECT 1 FROM trial_card WHERE card=? AND status='open'",
                                   (card,)).fetchone() or trial_batch(self.db, reg, version)[3]:
                return refused
            if not self.db.execute("""INSERT OR IGNORE INTO trial_receipt
                    SELECT card,action,event,actor,created,recorded,fingerprint FROM trial_click_candidate
                    WHERE event=?""", (event,)).rowcount:
                return "已记录"
            if action == "verify":
                return "邮件审批已核验"
            if action == "reject":
                self.close(row, "驳回：" + (reason or "点击人未认可本批草稿，请联系点击人核对原因"))
                return "已驳回"
            self.db.execute("UPDATE trial_card SET status='accepted' WHERE card=?", (card,))
            self.db.execute("INSERT INTO trial_task VALUES(?,'executor_unavailable')", (card,))
            return "有效同意已记录，执行器未就绪"
