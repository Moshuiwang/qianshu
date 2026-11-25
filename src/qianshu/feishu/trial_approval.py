"""试运行审批私有台账。格式 1 仅记录决定，执行器永不可消费。"""

import json
import os
import stat
import sqlite3
from contextlib import closing, contextmanager
from hashlib import sha256
from uuid import uuid4

from .state import trial_batch

CARD_FORMAT = 1
MANUAL_NOTICE = "卡片结果未能确认，请配置负责人核对。"
SCHEMA = """
CREATE TABLE IF NOT EXISTS trial_session(id INTEGER PRIMARY KEY CHECK(id=1), session TEXT);
CREATE TABLE IF NOT EXISTS trial_interval(session TEXT PRIMARY KEY, opened REAL, closed REAL);
CREATE TABLE IF NOT EXISTS trial_switch_epoch(
 epoch TEXT PRIMARY KEY, source TEXT NOT NULL, opened REAL NOT NULL, closed REAL, enabled INTEGER NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS trial_one_switch_epoch ON trial_switch_epoch((1)) WHERE closed IS NULL;
CREATE TABLE IF NOT EXISTS trial_session_grant(session TEXT PRIMARY KEY, epoch TEXT NOT NULL);
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
CREATE TABLE IF NOT EXISTS trial_update(
 event TEXT PRIMARY KEY, card TEXT, action TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS trial_feedback(
 event TEXT PRIMARY KEY, uuid TEXT UNIQUE, status TEXT);
CREATE TABLE IF NOT EXISTS trial_item_task(
 registration INTEGER, version INTEGER, item INTEGER, kind TEXT, card TEXT, status TEXT,
 PRIMARY KEY(registration,version,item,kind));
CREATE TABLE IF NOT EXISTS trial_known_record(
 card TEXT, item INTEGER, record_id TEXT NOT NULL, PRIMARY KEY(card,item));
CREATE TABLE IF NOT EXISTS trial_notification(card TEXT, kind TEXT, uuid TEXT UNIQUE, body TEXT,
 status TEXT, PRIMARY KEY(card,kind));
CREATE TABLE IF NOT EXISTS trial_halt(id INTEGER PRIMARY KEY CHECK(id=1), reason TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS trial_archive(
 registration INTEGER, version INTEGER, card TEXT NOT NULL, status TEXT NOT NULL, purpose TEXT, url TEXT,
 stage TEXT, reason TEXT, started REAL, finished REAL, disconnect_writes INTEGER NOT NULL DEFAULT 0,
 card_update TEXT NOT NULL DEFAULT 'none', PRIMARY KEY(registration,version));
CREATE TABLE IF NOT EXISTS trial_archive_page(
 registration INTEGER, version INTEGER, kind TEXT, page TEXT, summary TEXT, PRIMARY KEY(registration,version,kind));
CREATE TABLE IF NOT EXISTS trial_order(
 registration INTEGER, version INTEGER, item INTEGER, card TEXT NOT NULL, status TEXT NOT NULL, name_key TEXT,
 target TEXT, record_id TEXT, reason TEXT, sent REAL, finished REAL, disconnect INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(registration,version,item));
CREATE TABLE IF NOT EXISTS trial_order_slot(id INTEGER PRIMARY KEY CHECK(id=1), registration INTEGER NOT NULL,
 version INTEGER NOT NULL, item INTEGER NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS trial_handoff(name_key TEXT NOT NULL, target TEXT, registration INTEGER, version INTEGER,
 item INTEGER, opened REAL NOT NULL, cleared REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS trial_guardian_log(at REAL NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL);
"""

# 存档阶段的条目任务状态：archive_pending 尚无外写（关卡时可安全关闭）；archive_running 已发起存档；
# archive_done 存档已建、待建单（2d 消费）；archive_unknown 结果未知，冻结等人工。
ARCHIVE_SAFE = ("executor_unavailable", "archive_pending", "archive_done")
ARCHIVE_NOTICES = {
    "gate": "本批存档失败，未建单，已转人工。",
    "failed": "本批存档失败，未建单，已转人工。",
    "unconfirmed": "本批存档发布确认未登记，未建单，已转人工。",
    "unknown": "本批存档结果未知，未建单，请配置负责人核对存档文档与汇总页。",
    # 最后一个存档写步骤在断线期间返回成功：存档已建成，但断线后不进入待建单。
    "disconnected": "本批存档已建成，因断线未建单，已转人工。",
}
DISCONNECT_NOTICES = (
    "断线期间已发出 1 个存档写步骤，已核对该步骤结果。",
    "断线期间已发出 1 个存档写步骤，该步骤结果未能核对。",
)


@contextmanager
def journal_at(path, *, readonly=False):
    # 先验证私有目录与登记文件，不用 resolve 把符号链接转换为合法目标。
    path = path.absolute()
    directory = path.parent.lstat()
    meta = path.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
            or stat.S_IMODE(directory.st_mode) != 0o700 or path.parent.resolve() != path.parent
            or not stat.S_ISREG(meta.st_mode) or stat.S_IMODE(meta.st_mode) != 0o600
            or meta.st_uid != os.getuid() or meta.st_nlink != 1):
        raise ValueError("审批登记库须为私有普通文件")
    mode = "ro" if readonly else "rw"
    with closing(sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True, timeout=0.25)) as db:
        # SQLite 返回实际打开的主库路径。复核路径及原 inode 后再交出写能力。
        opened = db.execute("PRAGMA database_list").fetchone()[2]
        if opened != str(path) or not os.path.samestat(meta, path.lstat()):
            raise ValueError("审批登记库在打开期间变更")
        db.row_factory = sqlite3.Row
        yield Journal(db)


def has_approval_history(path):
    if not path.exists():
        return False
    with journal_at(path, readonly=True) as journal:
        return journal.db.execute("SELECT 1 FROM sqlite_schema WHERE name='trial_card'").fetchone() is not None


def recovery(db):
    # 登记号与版本供人工定位，不将原卡、回调或通知标识送到 CLI。
    rows = [dict(row) for row in db.execute("""
        SELECT 'trial_card' AS kind, registration, version, status FROM trial_card
        WHERE status IN ('preparing','open','accepted','partial')
        UNION ALL SELECT 'trial_task', c.registration, c.version, t.status
        FROM trial_task t JOIN trial_card c USING(card) WHERE t.status NOT IN ('closed','order_done')
        UNION ALL SELECT 'trial_notification', c.registration, c.version, n.status
        FROM trial_notification n JOIN trial_card c USING(card)
        WHERE n.status IN ('pending','unknown','blocked','missing_contact')
        UNION ALL SELECT 'click_candidate', c.registration, c.version, 'manual'
        FROM trial_click_candidate k JOIN trial_card c USING(card)
        LEFT JOIN trial_receipt r USING(event) WHERE r.event IS NULL
        UNION ALL SELECT 'card_intent', registration, version, status FROM card_intent
        WHERE status IN ('pending','unknown','blocked')
    """)]
    rows.extend(dict(row) for row in db.execute("""
        SELECT 'mail_notice' AS kind, registration, status FROM notice
        WHERE status IN ('pending','review','unknown','blocked')"""))
    if db.execute("SELECT 1 FROM sqlite_schema WHERE name='trial_item_task'").fetchone():
        rows.extend(dict(row) for row in db.execute("""
            SELECT 'item_task' AS kind, registration, version, item, status FROM trial_item_task
            WHERE status NOT IN ('closed','order_done')"""))
    if db.execute("SELECT 1 FROM sqlite_schema WHERE name='trial_feedback'").fetchone():
        rows.extend(dict(row) for row in db.execute("""
            SELECT 'update' AS kind, registration, version, u.status FROM trial_update u JOIN trial_card c
            USING(card) WHERE u.status!='sent'"""))
        rows.extend(dict(row) for row in db.execute("""
            SELECT 'click_feedback' AS kind, status, count(*) AS quantity FROM trial_feedback
            WHERE status!='sent' GROUP BY status"""))
        rows.extend(dict(row) for row in db.execute("""
            SELECT 'manual_check' AS kind, registration, version, n.status
            FROM trial_notification n JOIN trial_card c USING(card) WHERE n.kind='result_manual' OR n.kind LIKE 'first_manual:%'"""))
    if db.execute("SELECT 1 FROM sqlite_schema WHERE name='trial_order'").fetchone():
        from .trial_order import READONLY_CHECK, name_fingerprint
        rows.extend(dict(row) for row in db.execute("""
            SELECT 'order' AS kind, registration, version, item, status FROM trial_order
            WHERE status IN ('sending','created','unknown','mismatch')"""))
        # 「只读核对」类移交（同名已有机器人建单记录，G497）不作为可补建项列出，单列可查。
        rows.extend({"kind": "order_readonly_check" if r[4] == READONLY_CHECK else "order_handoff",
                     "registration": r[0], "version": r[1], "item": r[2], "name_fp": name_fingerprint(r[3]),
                     "status": "readonly_check" if r[4] == READONLY_CHECK else "open"} for r in db.execute("""
            SELECT registration, version, item, name_key, reason FROM trial_handoff WHERE cleared IS NULL"""))
        rows.extend(dict(row) for row in db.execute("""
            SELECT 'order_slot' AS kind, registration, version, item, 'held' AS status FROM trial_order_slot"""))
        rows.extend(dict(row) for row in db.execute("""
            SELECT 'write_halt' AS kind, reason AS status FROM trial_halt"""))
    if db.execute("SELECT 1 FROM sqlite_schema WHERE name='trial_known_record'").fetchone():
        rows.extend(dict(row) for row in db.execute("""
            SELECT 'known_record' AS kind, registration, version, r.item, 'readback_pending' AS status
            FROM trial_known_record r JOIN trial_card c USING(card)"""))
    return rows


class Journal:
    def __init__(self, db):
        self.db = db

    def initialize(self):
        """在调用方事务内创建审批表，沿用现有收信登记。"""
        for statement in SCHEMA.split(";"):
            if statement.strip():
                self.db.execute(statement)

    def write_halted(self):
        """持久停写位。置位后不再发起任何存档写（2d 建单共用）。"""
        return self.db.execute("SELECT 1 FROM trial_halt WHERE id=1").fetchone() is not None

    def halt_writes(self, reason, now):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO trial_halt VALUES(1,?,?)", (reason, now))

    def clear_halt(self):
        with self.db:
            self.db.execute("DELETE FROM trial_halt WHERE id=1")

    def disconnect_orders(self):
        """断线期间照发的建单数（G464），供日报单列。"""
        return self.db.execute("SELECT count(*) FROM trial_order WHERE disconnect=1").fetchone()[0]

    def halt_state(self):
        """停写位当前值与最后一次变更（守望者读回）。"""
        halt = self.db.execute("SELECT reason, at FROM trial_halt WHERE id=1").fetchone()
        last = self.db.execute("""SELECT at, action, reason FROM trial_guardian_log
                               WHERE action IN ('halt_set','halt_clear','request_create_off')
                               ORDER BY rowid DESC LIMIT 1""").fetchone()
        return {"halted": halt is not None, "halt_reason": halt[0] if halt else None,
                "halt_at": halt[1] if halt else None,
                "last_change": dict(zip(("at", "action", "reason"), last)) if last else None}

    def disconnect_archive_writes(self):
        """断线期间发出的存档写步骤总数，供日报单列。"""
        return self.db.execute("SELECT coalesce(sum(disconnect_writes),0) FROM trial_archive").fetchone()[0]

    def notice_counts(self):
        return self.db.execute("""SELECT
            (SELECT count(*) FROM trial_notification WHERE kind='result_manual' OR kind GLOB 'first_manual:*'
             OR kind GLOB 'archive_*' OR kind GLOB 'order_unknown:*' OR kind='order_mismatch'),
            (SELECT count(*) FROM trial_notification WHERE status NOT IN ('sent','cancelled')) +
            (SELECT count(*) FROM trial_feedback WHERE status!='sent')""").fetchone()

    def session(self):
        row = self.db.execute("SELECT session FROM trial_session WHERE id=1").fetchone()
        return row[0] if row else ""

    def active_epoch(self, source):
        row = self.db.execute("""SELECT epoch FROM trial_switch_epoch
                              WHERE closed IS NULL AND enabled=1 AND source=?""", (source,)).fetchone()
        return row[0] if row else None

    def granted(self, session, created):
        return self.db.execute("""SELECT 1 FROM trial_session_grant g JOIN trial_switch_epoch e USING(epoch)
               JOIN trial_interval i USING(session) WHERE g.session=? AND e.enabled=1 AND e.closed IS NULL
               AND i.closed IS NULL AND max(i.opened,e.opened)<=?""", (session, created)).fetchone() is not None

    def prepare(self, card, reg, version, session, country, snapshot, readonly, sent, *, render=None):
        """卡片快照落盘；同一事务里给未列为拟建的条目逐条登记人工移交（特批第 3 轮第 1 条）。

        收口不再靠公告组合给这些条目补移交；投递端逐条裁决照旧用于拟建条目，同一条目已有未销清移交时按现有去重
        不重复登记。

        render(held) 给出卡面（快照 `pages`）：held 为要移交、但同名政策已有机器人建单记录的条目（与公告投递同一裁决
        robot_held），在登记移交的同一写事务里裁决后再渲染，卡面写「只读核对，请勿重复建单」（复核 rv-2d4 P1）。
        """
        from .trial_order import card_handoffs, record_handoff, robot_held, same_card_items
        handoffs = card_handoffs(snapshot)
        # 同卡同名条目（G507）一律登记为只读核对类移交。
        held = same_card_items(snapshot)
        with self.db:
            if render is not None:
                if not self.db.in_transaction:
                    self.db.execute("BEGIN IMMEDIATE")
                held |= {item for item, key in handoffs if robot_held(self.db, key, card)}
                snapshot = dict(snapshot, pages=render(held))
            packed = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
            self.db.execute("""INSERT INTO trial_card VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (card, reg, version, CARD_FORMAT, session, country, packed,
                             sha256(packed.encode()).hexdigest(), int(readonly), sent,
                             (int(sent) // 86400 + 2) * 86400, "preparing"))
            if handoffs:
                record_handoff(self.db, card, handoffs, sent, held=held)

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

    def notify(self, row, kind, reason, *, items=None):
        snapshot = json.loads(row["snapshot"])
        items = "、".join(str(i) for i in (snapshot["items"] if items is None else items))
        missing = "、".join(snapshot["missing"]) or "无"
        body = f"登记 {row['registration']} · 条目 {items} · {reason}。待补项：{missing}。"
        self.db.execute("INSERT OR IGNORE INTO trial_notification VALUES(?,?,?,?,'pending')",
                        (row["card"], kind, str(uuid4()), body))

    def manual(self, card, request_id=None):
        # 首发用已登记的请求键；原卡改写用只属于该登记版本的卡键。
        kind = "result_manual" if request_id is None else "first_manual:" + request_id
        self.db.execute("""INSERT OR IGNORE INTO trial_notification VALUES(
            ?, ?, ?, ?, 'pending')""", (card, kind, str(uuid4()), MANUAL_NOTICE))

    def reconcile_updates(self):
        for row in self.db.execute("SELECT DISTINCT card FROM trial_update WHERE status='pending'"):
            self.manual(row[0])
        self.db.execute("UPDATE trial_update SET status='manual' WHERE status='pending'")

    def close(self, row, reason):
        card = row["card"]
        safe = {*ARCHIVE_SAFE, "closed"}
        legacy = self.db.execute("SELECT status FROM trial_task WHERE card=?", (card,)).fetchone()
        tasks = dict(self.db.execute("SELECT item,status FROM trial_item_task WHERE card=?", (card,)))
        known = {r[0] for r in self.db.execute("""SELECT item FROM trial_known_record
                         WHERE card=? AND length(trim(record_id))>0""", (card,))}
        done = {item for item, status in tasks.items() if status == "order_done"}
        uncertain = {item for item, status in tasks.items() if status not in safe and item not in done}
        self.db.execute("UPDATE trial_card SET status='closed' WHERE card=?", (card,))
        marks = ",".join("?" * len(ARCHIVE_SAFE))
        self.db.execute(f"""UPDATE trial_item_task SET status='closed' WHERE card=? AND status IN ({marks})
                        AND item NOT IN (SELECT item FROM trial_known_record WHERE card=?)""",
                        (card, *ARCHIVE_SAFE, card))
        self.db.execute(f"UPDATE trial_task SET status='closed' WHERE card=? AND status IN ({marks})",
                        (card, *ARCHIVE_SAFE))
        # 存档已发起而卡片在途关闭：结果不明，冻结等人工，不重发。
        self.db.execute("UPDATE trial_archive SET status='unknown', reason='card_closed' WHERE card=? AND status='running'",
                        (card,))
        self.db.execute("UPDATE trial_item_task SET status='archive_unknown' WHERE card=? AND status='archive_running'",
                        (card,))
        self.db.execute("UPDATE trial_task SET status='archive_unknown' WHERE card=? AND status='archive_running'",
                        (card,))
        self.db.execute("""UPDATE trial_notification SET status='cancelled' WHERE card=?
                        AND kind='remind' AND status='pending'""", (card,))
        # 同卡同名后条（快照 same_card，G508）：沿用同组首条的归属，公告写「与本卡第 N 条同名，只读核对」，
        # 不作为「未建单，转人工」列出；整批公告里由投递端逐条补写（unbuilt_notice），同样不登记可执行移交。
        from .trial_order import SAME_CARD, same_card_owner
        snapshot = json.loads(row["snapshot"])
        owner = same_card_owner(snapshot)
        if not known and not uncertain and not done and (not legacy or legacy[0] in safe):
            self.notify(row, "closed", reason + "，未建单，转人工",
                        items=[n for n in snapshot["items"] if n not in owner] if owner else None)
            return
        for item in sorted(set(snapshot["items"]) | known | uncertain):
            if item in owner and item not in done and item not in known and item not in uncertain:
                detail = SAME_CARD.format(owner[item])
            elif item in done:
                detail = "已建单并回读一致"
            elif item in known:
                detail = "已有记录待回读，请按私有登记只读核对"
            elif tasks.get(item, "").startswith("archive_"):
                detail = "存档结果未知，请核对存档文档与汇总页"
            elif item in uncertain or (legacy and legacy[0] not in safe):
                detail = "结果未知，请人工到明道云核对"
            else:
                detail = "未建单，转人工"
            self.notify(row, f"closed_{item}", reason + "，" + detail, items=[item])

    def rotate(self, session, reason, *, instant=None):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.initialize()
            self.reconcile_updates()
            if instant is not None:
                self.db.execute("UPDATE trial_interval SET closed=? WHERE closed IS NULL", (instant,))
                if session:
                    self.db.execute("INSERT INTO trial_interval VALUES(?,?,NULL)", (session, instant))
            for row in self.db.execute("SELECT * FROM trial_card WHERE status IN ('preparing','open','accepted','partial')"):
                if row["status"] in ("preparing", "partial"):
                    for key, in self.db.execute("""SELECT uuid FROM card_intent WHERE registration=? AND version=?
                                                AND status!='sent'""", (row["registration"], row["version"])):
                        self.manual(row["card"], key)
                self.close(row, reason)
            self.db.execute("INSERT OR REPLACE INTO trial_session VALUES(1,?)", (session,))

    def scan(self, now, *, create_enabled=True):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            # 存档进行中的批次由存档执行器收口，扫描不抢先关闭（开始存档前的授权只核一次）。
            # 建单在途（已登记发送、待回读）的批次同样由建单执行器收口。
            for row in self.db.execute("""SELECT * FROM trial_card WHERE status IN ('open','accepted')
                    AND card NOT IN (SELECT card FROM trial_archive WHERE status='running')
                    AND card NOT IN (SELECT card FROM trial_order WHERE status IN ('sending','created'))""").fetchall():
                if not create_enabled and row["status"] == "accepted":
                    self.close(row, "建单开关关闭")
                elif trial_batch(self.db, row["registration"], row["version"])[3]:
                    self.close(row, "批次冻结或版本失效")
                elif now >= row["expires"]:
                    self.close(row, "审批到期")
                elif now >= row["expires"] - 86400 and not self.db.execute(
                    "SELECT 1 FROM trial_receipt WHERE card=? AND recorded<=?",
                    (row["card"], row["expires"] - 86400)).fetchone():
                    self.notify(row, "remind", "尚无有效点击，请核对本批审批")

    def claim_event(self, event):
        with self.db:
            return self.db.execute("INSERT OR IGNORE INTO trial_event VALUES(?,0)", (event,)).rowcount == 1

    def action(self, session, card, reg, version, message, action, event, actor, created,
               clock, current, member, switches, *, reason="", claimed=False, queue_update=False,
               archive=False):
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
            if (queue_update and not self.granted(session, created)):
                return refused
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
            if (not current() or clock() >= row["expires"] or session != self.session()
                    or (queue_update and not self.granted(session, created))):
                return refused
            if not self.db.execute("SELECT 1 FROM trial_card WHERE card=? AND status='open'",
                                   (card,)).fetchone() or trial_batch(self.db, reg, version)[3]:
                return refused
            if not self.db.execute("""INSERT OR IGNORE INTO trial_receipt
                    SELECT card,action,event,actor,created,recorded,fingerprint FROM trial_click_candidate
                    WHERE event=?""", (event,)).rowcount:
                return "已记录"
            if queue_update:
                self.db.execute("INSERT INTO trial_update VALUES(?,?,?,'pending')", (event, card, action))
            if action == "verify":
                return "邮件审批已核验"
            if action == "reject":
                self.close(row, "驳回：" + (reason or "点击人未认可本批草稿，请联系点击人核对原因"))
                return "已驳回"
            self.db.execute("UPDATE trial_card SET status='accepted' WHERE card=?", (card,))
            state = "archive_pending" if archive else "executor_unavailable"
            self.db.execute("INSERT INTO trial_task VALUES(?,?)", (card, state))
            self.db.executemany("""INSERT INTO trial_item_task VALUES(?,?,?,'formal',?,?)""",
                                ((reg, version, item, card, state) for item in snapshot["eligible"]))
            return "有效同意已记录，将建存档" if archive else "有效同意已记录，执行器未就绪"
