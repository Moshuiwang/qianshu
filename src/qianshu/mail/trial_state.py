"""050 私有登记。费用用十亿分之一美元整数记账。"""

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from qianshu.feishu.handoff import _file

from . import _fingerprint
from .triage import BodyShape
from .trial_diagnostics import CLOSE_FAILED, fault_writer


FORMAT_VERSION = 1
FORMAT_HOLD = "登记库格式版本不符，已整体冻结，请人工处理"
BODY_NOTICE = "正文结构超出自动处理范围，请人工核对原邮件"
MANUAL_BODY_MISSED = "转人工（正文未参与解析）"


# 供日报的只读连接直接执行。顺序为待发、需人工核对。
BODY_NOTICE_COUNTS_SQL = """
SELECT count(*) FILTER (WHERE status='pending'), count(*) FILTER (WHERE status IN ('unknown','review'))
FROM notice WHERE kind IN ('body_missed','interrupted')
"""


class Hold(RuntimeError):
    """未获派发资格。"""


class FolderHold(Hold):
    """已绑定文件夹与本轮配置不一致。"""


class FormatHold(Hold):
    """已有登记库需要人工处理。"""


def now():
    return datetime.now(UTC).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS mailbox(
 scope TEXT PRIMARY KEY, state TEXT NOT NULL, generation INTEGER DEFAULT 1,
 validity INTEGER, cursor INTEGER, peak INTEGER, initial INTEGER, history INTEGER, enabled TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS snapshot(
 scope TEXT, generation INTEGER, validity INTEGER, initial INTEGER, history INTEGER, enabled TEXT,
 PRIMARY KEY(scope,generation));
CREATE TABLE IF NOT EXISTS scan_range(scope TEXT PRIMARY KEY, generation INTEGER, first INTEGER, last INTEGER);
CREATE TABLE IF NOT EXISTS observed_range(scope TEXT PRIMARY KEY, generation INTEGER, next_uid INTEGER);
CREATE TABLE IF NOT EXISTS location(
 scope TEXT, generation INTEGER, uid INTEGER, registration INTEGER, status TEXT DEFAULT 'pending', initial_seen INTEGER,
 PRIMARY KEY(scope,generation,uid));
CREATE TABLE IF NOT EXISTS registration(
 id INTEGER PRIMARY KEY, scope TEXT, msgkey TEXT, digest TEXT, candidate TEXT, attachments INTEGER,
 version INTEGER DEFAULT 1, status TEXT, reason TEXT, created TEXT, verdict TEXT, failure TEXT,
 sender_length INTEGER, subject_length INTEGER, due TEXT, parse_day TEXT);
CREATE INDEX IF NOT EXISTS trial_identity ON registration(scope,msgkey);
CREATE INDEX IF NOT EXISTS trial_candidate ON registration(scope,candidate);
CREATE TABLE IF NOT EXISTS trial_target(id INTEGER PRIMARY KEY CHECK(id=1), fingerprint TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS body_shape(scope TEXT, digest TEXT, groups_count INTEGER, sources INTEGER,
 supported INTEGER, PRIMARY KEY(scope,digest));
CREATE TABLE IF NOT EXISTS trial_folder(scope TEXT PRIMARY KEY, folder TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS card_intent(
 uuid TEXT PRIMARY KEY, registration INTEGER NOT NULL, version INTEGER NOT NULL, page INTEGER NOT NULL,
 status TEXT NOT NULL, at TEXT NOT NULL, UNIQUE(registration,version,page));
CREATE TABLE IF NOT EXISTS unknown_range(scope TEXT, generation INTEGER, first INTEGER, last INTEGER,
 status TEXT NOT NULL, PRIMARY KEY(scope,generation,first));
CREATE TABLE IF NOT EXISTS notice(event TEXT PRIMARY KEY, scope TEXT, registration INTEGER, kind TEXT,
 status TEXT DEFAULT 'pending', uuid TEXT, at TEXT, quantity INTEGER, generation INTEGER);
CREATE TABLE IF NOT EXISTS legacy(source TEXT PRIMARY KEY, status TEXT);
CREATE TABLE IF NOT EXISTS legacy_review(scope TEXT, generation INTEGER, source TEXT,
 PRIMARY KEY(scope,generation,source));
CREATE TABLE IF NOT EXISTS reconciliation(scope TEXT, generation INTEGER, uid INTEGER, msgkey TEXT, digest TEXT,
 candidate TEXT, attachments INTEGER, matched INTEGER, PRIMARY KEY(scope,generation,uid));
"""


# 同一次冻结的 UID 边界独立于人工快照代次；旧格式库打开时也创建此表。
_RECOVERY_SCHEMA = """CREATE TABLE IF NOT EXISTS recovery_window(
 scope TEXT PRIMARY KEY, validity INTEGER, first INTEGER, high INTEGER)"""


_REQUEST_SCHEMA = """CREATE TABLE IF NOT EXISTS recovery_request(
 id INTEGER PRIMARY KEY, scope TEXT NOT NULL, generation INTEGER NOT NULL, validity INTEGER NOT NULL,
 cursor INTEGER NOT NULL, freeze_id INTEGER NOT NULL, retry_of INTEGER NOT NULL DEFAULT 0,
 actor INTEGER NOT NULL, requested TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 reason TEXT, completed TEXT, gaps INTEGER, UNIQUE(scope,generation,validity,cursor,freeze_id,retry_of))"""

RECOVERY_REASONS = {"mail_read": "邮箱读取失败", "snapshot": "快照身份不符", "folder": "文件夹不符"}


# 事件、允许的源状态（冻结时使用 reason）、有序前置条件、执行动作。
# 重建原因只在这里枚举。条件按行优先匹配，未匹配的转移保持冻结。
_REBUILD = frozenset({"mapping", "missing", "seen", "unclean_close", "folder",
                      "generation", "initialization", "originals", "interrupted_search",
                      "reconciling"})
_FROZEN = _REBUILD | {"legacy"}
_ALL = _FROZEN | {"initializing", "active"}
_TRANSITIONS = (
    ("open", _ALL, "folder_conflict", "freeze_folder"),
    ("open", {"initializing"}, "always", "freeze_initialization"),
    ("open", {"active"}, "uncovered", "freeze_originals"),
    ("open", {"active"}, "interrupted", "freeze_interrupted_search"),
    ("open", _ALL, "always", "resume"),
    ("freeze", {"legacy"}, "same_legacy_epoch", "keep_legacy"),
    ("freeze", _ALL, "legacy_requested", "bind_legacy"),
    ("freeze", _ALL, "freeze_reason", "mark_frozen"),
    ("verify", {"active"}, "same_epoch", "raise_peak"),
    ("verify", _ALL, "always", "freeze_generation"),
    ("scan", {"initializing"}, "always", "activate"),
    ("scan", _ALL, "changed_epoch", "freeze_generation"),
    ("scan", _FROZEN, "uncovered", "hold"),
    ("scan", {"active"}, "pending_legacy", "freeze_legacy"),
    ("scan", {"legacy"}, "always", "check_legacy"),
    ("scan", _REBUILD, "always", "rebuild"),
    ("scan", {"active"}, "always", "observe"),
    ("snapshot", _REBUILD, "snapshot_changed", "replace_snapshot"),
    ("snapshot", {"reconciling"}, "always", "retain_snapshot"),
    ("confirm", {"legacy"}, "approved_snapshot", "settle_legacy"),
    ("confirm", {"reconciling"}, "approved_snapshot", "settle_snapshot"),
)


def transition(box, event, **facts):
    phase = box["reason"] if box["state"] == "frozen" else box["state"]
    for trigger, sources, guard, action in _TRANSITIONS:
        if trigger == event and phase in sources and (guard == "always" or facts.get(guard)):
            return action
    raise Hold()


_LEDGER_SCHEMA = """
CREATE TABLE budget(id INTEGER PRIMARY KEY CHECK(id=1), imported INTEGER, at TEXT, stopped INTEGER DEFAULT 0);
CREATE TABLE charge(id INTEGER PRIMARY KEY, registration INTEGER, model TEXT, at TEXT,
 amount INTEGER, inputs INTEGER, outputs INTEGER, partial_inputs INTEGER, partial_outputs INTEGER);
CREATE TABLE ledger_check(id INTEGER PRIMARY KEY CHECK(id=1), digest TEXT NOT NULL);
"""


def ledger_digest(db):
    digest = hashlib.sha256()
    for table in ("budget", "charge"):
        digest.update(table.encode())
        for row in db.execute(f"SELECT * FROM {table} ORDER BY id"):
            digest.update(json.dumps(tuple(row), separators=(",", ":")).encode())
    return digest.hexdigest()


def ledger_matches(db):
    seal = db.execute("SELECT digest FROM ledger_check WHERE id=1").fetchone()
    return bool(seal and seal[0] == ledger_digest(db))


class Store:
    def __init__(self, path: Path, *, before_failed_close=None, close_failed=None):
        self.path = path
        self._locked = False
        self.process_confirmation = None
        with self.gate():
            fresh = not path.exists()
            os.close(_file(path, create=fresh))
            self.db = sqlite3.connect(path)
            self.db.row_factory = sqlite3.Row
            try:
                with self.write() as db:
                    if fresh:
                        for statement in (_SCHEMA + _LEDGER_SCHEMA).split(";"):
                            if statement.strip():
                                db.execute(statement)
                        db.execute(f"PRAGMA user_version={FORMAT_VERSION}")
                        self._seal_ledger()
                    elif self.row("PRAGMA user_version")[0] != FORMAT_VERSION:
                        raise FormatHold(FORMAT_HOLD)
                    else:
                        self._ledger_valid()
                    db.execute(_RECOVERY_SCHEMA)
                    db.execute(_REQUEST_SCHEMA)
            except BaseException:
                if before_failed_close is not None:
                    before_failed_close()
                try:
                    self.db.close()
                except Exception:
                    if close_failed is None:
                        writer = fault_writer(path)
                        writer.post(CLOSE_FAILED)
                        writer.finish()
                    else:
                        close_failed()
                raise

    @contextmanager
    def gate(self):
        if self._locked:
            yield
            return
        fd = _file(self.path.with_suffix(".gate"), create=True)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._locked = True
            yield
        finally:
            self._locked = False
            os.close(fd)

    @contextmanager
    def write(self):
        with self.gate(), self.db:
            self.db.execute("BEGIN IMMEDIATE")
            yield self.db

    def row(self, sql, *args):
        return self.db.execute(sql, args).fetchone()

    def notice(self, event, scope, reg, kind, quantity=1):
        self.db.execute("""INSERT OR IGNORE INTO notice VALUES(?,?,?,?,'pending',?,?,?,
            (SELECT generation FROM mailbox WHERE scope=?))""",
                        (event, scope, reg, kind, str(uuid4()), now(), quantity, scope))

    def review_stale_notices(self, scope):
        self.db.execute("""UPDATE notice SET status='review' WHERE scope=? AND kind='body_missed'
            AND status='pending' AND EXISTS (SELECT 1 FROM mailbox m WHERE m.scope=notice.scope
                AND (m.reason='generation' OR m.generation IS NOT notice.generation))""",
                        (scope,))

    def body_notice_counts(self):
        return tuple(self.db.execute(BODY_NOTICE_COUNTS_SQL).fetchone())

    def begin(self, scope, folder=None):
        with self.write() as db:
            binding = self.row("SELECT folder FROM trial_folder WHERE scope=?", scope)
            if folder is not None:
                db.execute("INSERT OR IGNORE INTO trial_folder VALUES(?,?)", (scope, folder))
            box = self.row("SELECT * FROM mailbox WHERE scope=?", scope)
            if box is None:
                db.execute("INSERT INTO mailbox(scope,state) VALUES(?,'initializing')", (scope,))
                return "initializing"
            action = transition(box, "open", folder_conflict=folder and binding and binding[0] != folder,
                                uncovered=self.uncovered(scope),
                                interrupted=self.row("SELECT 1 FROM scan_range WHERE scope=?", scope))
            if action == "resume":
                return box["state"]
            self._freeze(scope, action.removeprefix("freeze_"))
            return "folder_conflict" if action == "freeze_folder" else "frozen"

    def peak(self, scope, next_uid):
        with self.write() as db:
            db.execute("UPDATE mailbox SET peak=max(peak,?) WHERE scope=?", (next_uid, scope))

    def searching(self, scope, generation, first, last):
        with self.write() as db:
            db.execute("INSERT INTO scan_range VALUES(?,?,?,?)", (scope, generation, first, last))

    def activate(self, scope, validity, cursor, history):
        stamp = now()
        with self.write() as db:
            updated = db.execute("""UPDATE mailbox SET state='active',validity=?,cursor=?,peak=?,initial=?,history=?,enabled=?
                WHERE scope=? AND state='initializing'""", (validity, cursor, cursor, cursor, history, stamp, scope))
            if updated.rowcount != 1:
                raise Hold()
            db.execute("INSERT INTO snapshot VALUES(?,1,?,?,?,?)", (scope, validity, cursor, history, stamp))

    def observe(self, scope, generation, start, next_uid, uids):
        with self.write() as db:
            db.execute("DELETE FROM scan_range WHERE scope=?", (scope,))
            uids = sorted(set(uids))
            self._record_gaps(scope, generation, start, next_uid, uids)
            db.executemany("INSERT OR IGNORE INTO location(scope,generation,uid) VALUES(?,?,?)",
                           ((scope, generation, uid) for uid in uids))
            if not self.row("""SELECT 1 FROM mailbox WHERE scope=? AND generation=?
                    AND cursor=? AND state='active'""", scope, generation, start):
                raise Hold()
            db.execute("INSERT INTO observed_range VALUES(?,?,?)", (scope, generation, next_uid))
            self._advance(scope, generation)

    def _record_gaps(self, scope, generation, start, next_uid, uids):
        # 同一 UIDVALIDITY 的快照可能有多个本地代次。按区间跳过已有缺口，
        # 即使区间很大也不逐个枚举 UID。
        covered = [(uid, uid) for uid in uids if start <= uid < next_uid]
        existing = self.db.execute("""SELECT u.first,u.last,u.generation FROM unknown_range u
            JOIN snapshot s USING(scope,generation) JOIN mailbox m USING(scope)
            WHERE u.scope=? AND s.validity=m.validity AND u.last>=? AND u.first<?""",
                                       (scope, start, next_uid))
        for low, high, prior_generation in existing:
            event = f"gap:{scope}:{prior_generation}:{low}"
            if not self.row("SELECT 1 FROM notice WHERE event=?", event):
                self.notice(event, scope, None, "gap", high - low + 1)
            covered.append((low, high))
        first = start
        for low, high in sorted(covered) + [(next_uid, next_uid)]:
            if first < low:
                self.db.execute("INSERT INTO unknown_range VALUES(?,?,?,?,'unknown')",
                                (scope, generation, first, low - 1))
                self.notice(f"gap:{scope}:{generation}:{first}", scope, None, "gap", low - first)
            first = max(first, high + 1)

    def _advance(self, scope, generation):
        if not self.row("SELECT 1 FROM location WHERE scope=? AND generation=? AND status='pending' LIMIT 1",
                        scope, generation):
            observed = self.row("SELECT next_uid FROM observed_range WHERE scope=? AND generation=?", scope, generation)
            if observed:
                self.db.execute("UPDATE mailbox SET cursor=? WHERE scope=?", (observed[0], scope))
                self.db.execute("DELETE FROM observed_range WHERE scope=?", (scope,))

    def pending(self, scope, generation):
        return [r[0] for r in self.db.execute("""SELECT uid FROM location WHERE scope=? AND generation=?
            AND status='pending' ORDER BY uid""", (scope, generation))]

    def _freeze(self, scope, reason):
        box = self.row("SELECT * FROM mailbox WHERE scope=?", scope)
        allowed = reason in _FROZEN - {"reconciling"}
        action = transition(box, "freeze", freeze_reason=allowed,
                            legacy_requested=reason == "legacy",
                            same_legacy_epoch=allowed and reason != "generation")
        fresh = box["state"] != "frozen"
        if fresh and box["cursor"] is not None:
            self.db.execute("INSERT OR REPLACE INTO recovery_window VALUES(?,?,?,?)",
                            (scope, box["validity"], box["cursor"], box["peak"]))
        if action == "keep_legacy":
            reason = "legacy"
        if fresh:
            self.db.execute("UPDATE registration SET version=version+1 WHERE scope=?", (scope,))
        self.db.execute("UPDATE mailbox SET state='frozen',reason=? WHERE scope=?", (reason, scope))
        if reason == "generation":
            self.review_stale_notices(scope)
        generation = box["generation"]
        if action == "bind_legacy":
            self.db.execute("""INSERT OR IGNORE INTO legacy_review SELECT ?,?,source FROM legacy
                WHERE status='pending'""", (scope, generation))
        count = self.row("SELECT count(*) FROM registration WHERE scope=?", scope)[0]
        event = f"freeze:{scope}:{generation}"
        # 对账恢复后代次可能不变；新一次冻结须有新事件号，不得被同名去重吞掉（G448）。
        if fresh and self.row("SELECT 1 FROM notice WHERE event=?", event):
            event += f":{self.row('SELECT count(*) FROM notice WHERE scope=? AND kind=?', scope, 'freeze')[0]}"
        self.notice(event, scope, None, "freeze", count)

    def freeze(self, scope, reason):
        with self.write():
            self._freeze(scope, reason)

    def uncovered(self, scope):
        # 旧归档补出的索引无法证明登记当时的身份，只认登记本身的 v2 证据。
        return self.row("""SELECT 1 FROM registration WHERE scope=? AND
            (digest IS NULL OR digest NOT LIKE '2:%' OR candidate IS NULL OR candidate NOT LIKE '2:%')
            LIMIT 1""", scope)

    def record_coverage(self, scope, item):
        self.db.execute("INSERT OR REPLACE INTO body_shape VALUES(?,?,?,?,?)", (scope, item.digest, *item.shape))

    def review_reused(self, reg, status, item):
        if item.body_missed and status in {"done", "no", "manual", "correction_pending"}:
            self.db.execute("UPDATE registration SET version=version+1 WHERE id=?", (reg,))
            self.finish(reg, "manual")

    def finish(self, reg, status, *, reason=None):
        """在调用方事务内完成登记，同步登记尚待发送的人工提示。冻结优先。"""
        row = self.row("""SELECT r.scope, s.groups_count,s.sources,s.supported FROM registration r
            LEFT JOIN body_shape s USING(scope,digest) WHERE r.id=? AND r.status!='frozen'""", reg)
        if row is None:
            return
        if not BodyShape(*row[1:]).allowed():
            status = MANUAL_BODY_MISSED
            self.notice(f"body:{reg}", row["scope"], reg, "body_missed")
        self.db.execute("UPDATE registration SET status=?,reason=coalesce(?,reason) WHERE id=?",
                        (status, reason, reg))

    def register(self, scope, generation, uid, item, *, seen=None):
        with self.write() as db:
            if not self.row("SELECT 1 FROM mailbox WHERE scope=? AND generation=? AND state='active'", scope, generation):
                raise Hold()
            loc = self.row("SELECT * FROM location WHERE scope=? AND generation=? AND uid=?", scope, generation, uid)
            if not loc or loc["status"] != "pending":
                raise Hold()
            key, digest, candidate = item.key, item.digest, item.candidate
            self.record_coverage(scope, item)
            same = list(db.execute("SELECT * FROM registration WHERE scope=? AND msgkey=?", (scope, key)))
            related = list(db.execute("""SELECT * FROM registration WHERE scope=? AND candidate=?
                AND msgkey IS NOT ?""", (scope, candidate, key)))
            reason = None
            if not key or related:
                reason = "duplicate"
            if not digest or any(r["digest"] != digest or r["status"] == "frozen" for r in same):
                reason = "conflict"
            if same and not reason:
                reg = same[0]["id"]
                self.review_reused(reg, same[0]["status"], item)
            else:
                for row in same + related:
                    db.execute("UPDATE registration SET status='frozen',version=version+1,reason=? WHERE id=?",
                               (reason, row["id"]))
                reg = db.execute("""INSERT INTO registration(scope,msgkey,digest,candidate,attachments,status,reason,
                    created,sender_length,subject_length) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                                 (scope, key, digest, candidate, item.attachments, "frozen" if reason else "triage",
                                  reason, now(), len(item.fields["display_name"]), len(item.fields["subject"]))).lastrowid
                if reason:
                    self.notice(f"{reason}:{reg}", scope, reg, reason, 1 + len(same) + len(related))
            db.execute("""UPDATE location SET registration=?,initial_seen=?,status='registered'
                WHERE scope=? AND generation=? AND uid=?""", (reg, seen, scope, generation, uid))
            self._advance(scope, generation)
            return reg

    def batch(self, reg, version):
        from qianshu.feishu.state import trial_batch
        return trial_batch(self.db, reg, version)

    def _seal_ledger(self):
        self.db.execute("INSERT OR REPLACE INTO ledger_check VALUES(1,?)", (ledger_digest(self.db),))

    def _ledger_valid(self):
        try:
            if ledger_matches(self.db):
                return True
        except (sqlite3.DatabaseError, TypeError):
            pass
        self.notice("ledger", None, None, "ledger")
        return False

    @contextmanager
    def ledger_write(self):
        with self.write() as db:
            if self._ledger_valid():
                yield db
                self._seal_ledger()
                return
        raise Hold()

    def budget_open(self):
        with self.write():
            if self._ledger_valid():
                budget = self.row("SELECT stopped FROM budget")
                return bool(budget and not budget[0])
            return False

    def import_spend(self, amount):
        amount = Decimal(amount)
        if not amount.is_finite() or not 0 <= amount <= Decimal("9000000000") or amount.as_tuple().exponent < -9:
            raise ValueError("开发期金额无效。")
        with self.ledger_write() as db:
            db.execute("INSERT INTO budget VALUES(1,?,?,?)", (int(amount * 10**9), now(), int(amount >= 20)))
            if amount >= 15:
                self.notice("fee15", None, None, "fee15")
            if amount >= 20:
                self.notice("stop20", None, None, "stop20")

    def reserve(self, model, maximum, input_bound, reg, *, stage=None, day=None):
        if not all(type(value) is int and value > 0 for value in (maximum, input_bound)):
            raise Hold()
        input_rate, output_rate, estimate = {"sonnet": (2000, 10000, 221000000),
                                             "haiku": (100, 500, 195000)}[model]
        upper = max(estimate, input_bound * input_rate + maximum * output_rate)
        with self.ledger_write() as db:
            budget = self.row("SELECT * FROM budget")
            if not budget or budget["stopped"]:
                raise Hold()
            total = budget["imported"] + self.row("SELECT coalesce(sum(amount),0) FROM charge")[0] + upper
            if total > 20 * 10**9:
                db.execute("UPDATE budget SET stopped=1")
                self.notice("stop20", None, None, "stop20")
            else:
                if stage is not None:
                    expected, claimed = {"haiku": ("triage", "classifying"), "sonnet": ("parse", "parsing"),
                                         "correction": ("parsing", "parsing")}[stage]
                    if db.execute("""UPDATE registration SET status=?,parse_day=coalesce(parse_day,?)
                        WHERE id=? AND status=?""", (claimed, day, reg, expected)).rowcount != 1:
                        raise Hold()
                attempt = db.execute("INSERT INTO charge(registration,model,at,amount) VALUES(?,?,?,?)",
                                     (reg, model, now(), upper)).lastrowid
                if total >= 15 * 10**9:
                    self.notice("fee15", None, None, "fee15")
                if total == 20 * 10**9:
                    db.execute("UPDATE budget SET stopped=1")
                    self.notice("stop20", None, None, "stop20")
                if self.row("SELECT coalesce(sum(amount),0) FROM charge WHERE model='sonnet'")[0] > 8 * 10**9:
                    self.notice("parse8", None, None, "parse8")
                return attempt
        raise Hold()

    def settle(self, attempt, inputs, outputs, *, partial_inputs=None, partial_outputs=None):
        def known(value):
            return value if type(value) is int and value >= 0 else None

        with self.ledger_write() as db:
            if known(inputs) is None or known(outputs) is None:
                db.execute("""UPDATE charge SET partial_inputs=?,partial_outputs=?
                    WHERE id=? AND inputs IS NULL""",
                           (known(partial_inputs if inputs is None else inputs),
                            known(partial_outputs if outputs is None else outputs), attempt))
                return False
            row = self.row("SELECT model,amount FROM charge WHERE id=?", attempt)
            a, b = (2000, 10000) if row[0] == "sonnet" else (100, 500)
            db.execute("UPDATE charge SET inputs=?,outputs=?,amount=? WHERE id=? AND inputs IS NULL",
                       (inputs, outputs, inputs * a + outputs * b, attempt))
            return True

    @contextmanager
    def dispatch(self, scope, generation, reg, stage, enabled):
        with self.gate():
            row = self.row("""SELECT r.status,m.generation,m.state FROM registration r
                JOIN mailbox m USING(scope) WHERE r.scope=? AND r.id=?""", scope, reg)
            if (not enabled() or not row or row["state"] != "active" or row["generation"] != generation
                    or row["status"] not in {"haiku": ("triage",), "sonnet": ("parse",), "correction": ("parsing",),
                                             "body_missed": (MANUAL_BODY_MISSED,),
                                             "card": ("sending",), "received": ("parse",), "deferred": ("parse",)}[stage]
                    or not self.budget_open()):
                raise Hold()
            yield

    def import_legacy(self, path):
        if not path.exists():
            return
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as old:
            rows = old.execute("SELECT scope,fingerprint,delivered FROM delivery").fetchall()
        with self.write() as db:
            for scope, fingerprint, delivered in rows:
                db.execute("INSERT OR IGNORE INTO legacy VALUES(?,'pending')",
                           (_fingerprint(scope, fingerprint, str(delivered)),))

    def freeze_legacy(self, scope, generation):
        with self.write():
            if not self.row("SELECT 1 FROM mailbox WHERE scope=? AND generation=?", scope, generation):
                raise Hold()
            self._freeze(scope, "legacy")

    @staticmethod
    def same_epoch(box, validity, next_uid):
        return box["validity"] == validity and next_uid >= (box["peak"] or 0)

    def verify_epoch(self, scope, validity, next_uid):
        with self.write() as db:
            box = self.row("SELECT * FROM mailbox WHERE scope=?", scope)
            action = transition(box, "verify", same_epoch=self.same_epoch(box, validity, next_uid))
            if action == "raise_peak":
                db.execute("UPDATE mailbox SET peak=? WHERE scope=?", (next_uid, scope))
                return True
            self._freeze(scope, "generation")
            return False

    def scan_action(self, scope, validity, next_uid):
        with self.write():
            box = self.row("SELECT * FROM mailbox WHERE scope=?", scope)
            changed = not self.same_epoch(box, validity, next_uid)
            action = transition(box, "scan", changed_epoch=changed,
                                uncovered=self.uncovered(scope),
                                pending_legacy=self.row("SELECT 1 FROM legacy WHERE status='pending' LIMIT 1"))
            if action == "freeze_generation":
                self._freeze(scope, "generation")
            elif action == "freeze_legacy":
                self._freeze(scope, "legacy")
            else:
                return action
            box = self.row("SELECT * FROM mailbox WHERE scope=?", scope)
            return transition(box, "scan", uncovered=self.uncovered(scope))

    def recovery_gaps(self, box, validity, next_uid, uids):
        scope, generation = box["scope"], box["generation"]
        window = self.row("SELECT * FROM recovery_window WHERE scope=?", scope)
        if validity != box["validity"] or box["cursor"] is None:
            # 新 UIDVALIDITY 的第一份快照仍为基线，不把旧位置映射到新代。
            self.db.execute("INSERT OR REPLACE INTO recovery_window VALUES(?,?,?,?)",
                            (scope, validity, next_uid, next_uid))
            return
        if window is None:
            # 升级前已在等待确认的快照：从最近一次启用/已结清的快照恢复边界。
            baseline = self.row("""SELECT initial FROM snapshot s WHERE scope=? AND validity=?
                AND generation<=? AND (generation=1 OR EXISTS (SELECT 1 FROM location l
                    WHERE l.scope=s.scope AND l.generation=s.generation)
                    OR NOT EXISTS (SELECT 1 FROM snapshot prev WHERE prev.scope=s.scope
                        AND prev.generation=s.generation-1 AND prev.validity=s.validity))
                ORDER BY generation DESC LIMIT 1""", scope, validity, generation)
            first = baseline[0] if box["reason"] == "reconciling" and baseline else box["cursor"]
            high = box["peak"]
        else:
            first, high = window["first"], window["high"]
        observed = self.row("SELECT next_uid FROM observed_range WHERE scope=? AND generation=?",
                            scope, generation)
        high = max(next_uid, high or 0, observed[0] if observed else 0)
        self.db.execute("INSERT OR REPLACE INTO recovery_window VALUES(?,?,?,?)",
                        (scope, validity, first, high))
        self._record_gaps(scope, generation, first, high, uids)

    def check_legacy(self, scope, validity, next_uid, uids):
        with self.write() as db:
            box = self.row("SELECT * FROM mailbox WHERE scope=?", scope)
            if transition(box, "scan") != "check_legacy" or box["validity"] != validity:
                raise Hold()
            self.recovery_gaps(box, validity, next_uid, uids)
            present = set(uids)
            db.executemany("""UPDATE location SET status='unknown' WHERE scope=? AND generation=?
                AND uid=? AND status='pending'""", ((scope, box["generation"], uid)
                for uid in self.pending(scope, box["generation"]) if uid not in present))

    def rebuild(self, scope, validity, cursor, history, entries):
        with self.write() as db:
            box = self.row("SELECT * FROM mailbox WHERE scope=?", scope)
            if transition(box, "scan") != "rebuild":
                raise Hold()
            generation = box["generation"]
            observations = []
            for uid, item in entries:
                self.record_coverage(scope, item)
                matches = list(db.execute("""SELECT id,status FROM registration WHERE scope=?
                    AND msgkey=? AND digest=?""", (scope, item.key, item.digest)))
                match = matches[0][0] if len(matches) == 1 and matches[0][1] != "frozen" else None
                if match is not None:
                    self.review_reused(match, matches[0]["status"], item)
                observations.append((uid, item.key, item.digest, item.candidate, item.attachments, match))
            self.recovery_gaps(box, validity, cursor, [row[0] for row in observations])
            old = [tuple(r) for r in db.execute("""SELECT uid,msgkey,digest,candidate,attachments,matched
                FROM reconciliation WHERE scope=? AND generation=? ORDER BY uid""", (scope, generation))]
            changed = (box["reason"] != "reconciling" or box["validity"] != validity
                       or box["cursor"] != cursor or old != observations)
            if transition(box, "snapshot", snapshot_changed=changed) == "replace_snapshot":
                generation += 1
                db.execute("INSERT INTO snapshot VALUES(?,?,?,?,?,?)", (scope, generation, validity, cursor, history, now()))
                db.executemany("""INSERT INTO reconciliation(scope,generation,uid,msgkey,digest,candidate,attachments,matched)
                    VALUES(?,?,?,?,?,?,?,?)""",
                               ((scope, generation, *data) for data in observations))
            db.execute("""UPDATE mailbox SET generation=?,validity=?,cursor=?,peak=?,reason='reconciling'
                WHERE scope=?""", (generation, validity, cursor, cursor, scope))
            self.review_stale_notices(scope)

    @classmethod
    @contextmanager
    def confirmation_store(cls, path):
        """守望者只打开已存在的私有库，避免服务启动时的账本修复。"""
        store = cls.__new__(cls)
        store.path, store._locked = path, False
        os.close(_file(path))
        with store.gate(), closing(sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True)) as db:
            store.db = db
            db.row_factory = sqlite3.Row
            if store.row("PRAGMA user_version")[0] != FORMAT_VERSION:
                raise FormatHold(FORMAT_HOLD)
            yield store

    @staticmethod
    def confirmation_list(path):
        meta = path.lstat()
        if (not stat.S_ISREG(meta.st_mode) or stat.S_IMODE(meta.st_mode) != 0o600
                or meta.st_uid != os.getuid() or meta.st_nlink != 1
                or stat.S_IMODE(path.parent.stat().st_mode) != 0o700):
            raise ValueError("须指定私有登记库。")
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("PRAGMA user_version").fetchone()[0] != FORMAT_VERSION:
                raise ValueError("登记库格式版本不符。")
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='recovery_request'").fetchone():
                return []
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM recovery_request ORDER BY id")]

    def request_confirmation(self, scope, generation, validity, cursor, retry_of=0):
        with self.write() as db:
            db.execute(_REQUEST_SCHEMA)
            freeze = self.row("SELECT max(rowid) FROM notice WHERE scope=? AND kind='freeze'", scope)[0]
            key = (scope, generation, validity, cursor, freeze or 0, retry_of)
            previous = self.row("""SELECT id FROM recovery_request WHERE scope=? AND generation=?
                AND validity=? AND cursor=? AND freeze_id=? AND retry_of=?""", *key)
            if previous:
                return previous[0]
            self.confirm_action(scope, generation, validity, cursor)
            if retry_of and not self.row("""SELECT 1 FROM recovery_request WHERE id=? AND status='hold'
                AND scope=? AND generation=? AND validity=? AND cursor=? AND freeze_id=?""",
                                         retry_of, *key[:-1]):
                raise Hold()
            return db.execute("""INSERT INTO recovery_request(scope,generation,validity,cursor,
                freeze_id,retry_of,actor,requested) VALUES(?,?,?,?,?,?,?,?)""",
                              (*key, os.getuid(), now())).lastrowid

    def reconcile(self, scope, generation, validity, cursor):
        """兼容常驻进程内的同步调用，同样经过请求登记和服务重读。"""
        if self.process_confirmation is None:
            raise Hold()
        request = self.request_confirmation(scope, generation, validity, cursor)
        self.process_confirmation()
        if self.row("SELECT status FROM recovery_request WHERE id=?", request)[0] != "restored":
            raise Hold()

    def finish_confirmation(self, request, reason=None):
        scope = request["scope"]
        with self.write() as db:
            count = None
            kind = "recovery_" + (reason or "restored")
            if reason is None:
                count = self.row("""SELECT coalesce(sum(min(u.last,w.high-1)-max(u.first,w.first)+1),0)
                    FROM unknown_range u JOIN snapshot s USING(scope,generation)
                    JOIN recovery_window w ON w.scope=u.scope AND w.validity=s.validity
                    WHERE u.scope=? AND u.last>=w.first AND u.first<w.high""", scope)[0]
                self._settle(db, scope, request["generation"], request["validity"], request["cursor"])
            db.execute("""UPDATE recovery_request SET status=?,reason=?,completed=?,gaps=? WHERE id=?""",
                       ("hold" if reason else "restored", reason, now(), count, request["id"]))
            self.notice(f"recovery:{request['id']}", scope, None, kind, count)

    def confirm_action(self, scope, generation, validity, cursor):
        box = self.row("SELECT * FROM mailbox WHERE scope=?", scope)
        if box is None:
            raise Hold()
        approved = (generation, validity, cursor) == (box["generation"], box["validity"], box["cursor"])
        return transition(box, "confirm", approved_snapshot=approved and not self.uncovered(scope))

    def _settle(self, db, scope, generation, validity, cursor):
        action = self.confirm_action(scope, generation, validity, cursor)
        if self.row("""SELECT 1 FROM reconciliation WHERE scope=? AND generation=?
                AND (digest IS NULL OR candidate IS NULL) LIMIT 1""", scope, generation):
            raise Hold()
        if db.execute("""UPDATE mailbox SET state='active',reason=NULL WHERE scope=? AND generation=?
            AND validity=? AND cursor=? AND state='frozen'""",
                      (scope, generation, validity, cursor)).rowcount != 1:
            raise Hold()
        # 本次水位以前未能证明的新信、旧待办和可能已发的工作均交人工。
        pending = db.execute("""SELECT id FROM registration WHERE scope=?
            AND status NOT IN ('done','no','frozen','manual',?)""", (scope, MANUAL_BODY_MISSED)).fetchall()
        for reg, in pending:
            db.execute("UPDATE registration SET version=version+1 WHERE id=?", (reg,))
            self.finish(reg, "manual", reason="reconciliation")
        for row in list(db.execute("""SELECT * FROM reconciliation WHERE scope=? AND generation=?
                AND matched IS NULL""", (scope, generation))):
            reg = db.execute("""INSERT INTO registration(scope,msgkey,digest,candidate,attachments,status,
                reason,created) VALUES(?,?,?,?,?,'review','reconciliation',?)""",
                             (scope, row["msgkey"], row["digest"], row["candidate"],
                              row["attachments"], now())).lastrowid
            self.finish(reg, "manual")
            db.execute("UPDATE reconciliation SET matched=? WHERE scope=? AND generation=? AND uid=?",
                       (reg, scope, generation, row["uid"]))
        db.execute("DELETE FROM scan_range WHERE scope=?", (scope,))
        if action == "settle_legacy":
            self._advance(scope, generation)
        else:
            db.execute("DELETE FROM observed_range WHERE scope=?", (scope,))
        db.execute("DELETE FROM recovery_window WHERE scope=?", (scope,))
        db.execute("""INSERT OR IGNORE INTO location(scope,generation,uid,registration,status)
            SELECT scope,generation,uid,matched,'reviewed' FROM reconciliation WHERE scope=? AND generation=?""",
                   (scope, generation))
        if action == "settle_legacy":
            db.execute("""UPDATE legacy SET status='reviewed' WHERE source IN
                (SELECT source FROM legacy_review WHERE scope=? AND generation=?)""", (scope, generation))
