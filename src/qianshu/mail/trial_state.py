"""050 私有登记。费用用十亿分之一美元整数记账。"""

import fcntl
import hashlib
import json
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from qianshu.feishu.handoff import _file

from . import _fingerprint


MANUAL_BODY_MISSED = "转人工（正文未参与解析）"


class Hold(RuntimeError):
    """未获派发资格。"""


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
 scope TEXT, generation INTEGER, uid INTEGER, registration INTEGER, status TEXT DEFAULT 'pending',
 PRIMARY KEY(scope,generation,uid));
CREATE TABLE IF NOT EXISTS registration(
 id INTEGER PRIMARY KEY, scope TEXT, msgkey TEXT, digest TEXT, candidate TEXT, attachments INTEGER,
 version INTEGER DEFAULT 1, status TEXT, reason TEXT, created TEXT, verdict TEXT, failure TEXT,
 sender_length INTEGER, subject_length INTEGER, due TEXT, parse_day TEXT);
CREATE INDEX IF NOT EXISTS trial_identity ON registration(scope,msgkey);
CREATE INDEX IF NOT EXISTS trial_candidate ON registration(scope,candidate);
CREATE TABLE IF NOT EXISTS trial_target(id INTEGER PRIMARY KEY CHECK(id=1), fingerprint TEXT NOT NULL);
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


_LEDGER_SCHEMA = """
CREATE TABLE budget(id INTEGER PRIMARY KEY CHECK(id=1), imported INTEGER, at TEXT, stopped INTEGER DEFAULT 0);
CREATE TABLE charge(id INTEGER PRIMARY KEY, registration INTEGER, model TEXT, at TEXT,
 amount INTEGER, inputs INTEGER, outputs INTEGER, partial_inputs INTEGER, partial_outputs INTEGER);
CREATE TABLE ledger_check(id INTEGER PRIMARY KEY CHECK(id=1), digest TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: Path):
        self.path = path
        os.close(_file(path, create=True))
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self._locked = False
        with self.write() as db:
            fresh = not self.row("SELECT 1 FROM sqlite_master WHERE type='table'")
            for statement in (_SCHEMA + (_LEDGER_SCHEMA if fresh else "")).split(";"):
                if statement.strip():
                    db.execute(statement)
            if "initial_seen" not in {r[1] for r in db.execute("PRAGMA table_info(location)")}:
                db.execute("ALTER TABLE location ADD COLUMN initial_seen INTEGER")
            if "reason" not in {r[1] for r in db.execute("PRAGMA table_info(reconciliation)")}:
                db.execute("ALTER TABLE reconciliation ADD COLUMN reason TEXT")
                # 旧库已停在对账页时，UIDNEXT 不变也要用 v2 重读一次。
                db.execute("""UPDATE mailbox SET reason='originals' WHERE state='frozen' AND
                    (reason='reconciling' OR (reason='legacy' AND EXISTS
                    (SELECT 1 FROM registration WHERE scope=mailbox.scope AND digest LIKE '1:%')))""")
            if fresh:
                self._seal_ledger()
            else:
                self._ledger_valid()

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

    def begin(self, scope, folder=None):
        with self.write() as db:
            if folder is not None:
                binding = self.row("SELECT folder FROM trial_folder WHERE scope=?", scope)
                if binding and binding[0] != folder:
                    self._freeze(scope, "folder")
                    return "folder_conflict"
                db.execute("INSERT OR IGNORE INTO trial_folder VALUES(?,?)", (scope, folder))
            row = self.row("SELECT state FROM mailbox WHERE scope=?", scope)
            if row is None:
                db.execute("INSERT INTO mailbox(scope,state) VALUES(?,'initializing')", (scope,))
                return "initializing"
            if row[0] == "initializing":
                self._freeze(scope, "initialization")
                return "frozen"
            if row[0] == "active" and self.uncovered(scope):
                self._freeze(scope, "originals")
                return "frozen"
            if row[0] == "active" and self.row("SELECT 1 FROM scan_range WHERE scope=?", scope):
                self._freeze(scope, "interrupted_search")
                return "frozen"
            return row[0]

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
            first = start
            for uid in [*uids, next_uid]:
                if first < uid:
                    db.execute("INSERT INTO unknown_range VALUES(?,?,?,?,'unknown')",
                               (scope, generation, first, uid - 1))
                    self.notice(f"gap:{scope}:{generation}:{first}", scope, None, "gap", uid - first)
                first = uid + 1
            db.executemany("INSERT OR IGNORE INTO location(scope,generation,uid) VALUES(?,?,?)",
                           ((scope, generation, uid) for uid in uids))
            if not self.row("""SELECT 1 FROM mailbox WHERE scope=? AND generation=?
                    AND cursor=? AND state='active'""", scope, generation, start):
                raise Hold()
            db.execute("INSERT INTO observed_range VALUES(?,?,?)", (scope, generation, next_uid))
            self._advance(scope, generation)

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
        box = self.row("SELECT state FROM mailbox WHERE scope=?", scope)
        if box["state"] != "frozen":
            self.db.execute("UPDATE registration SET version=version+1 WHERE scope=?", (scope,))
        self.db.execute("UPDATE mailbox SET state='frozen',reason=? WHERE scope=?", (reason, scope))
        generation = self.row("SELECT generation FROM mailbox WHERE scope=?", scope)[0]
        count = self.row("SELECT count(*) FROM registration WHERE scope=?", scope)[0]
        self.notice(f"freeze:{scope}:{generation}", scope, None, "freeze", count)

    def freeze(self, scope, reason):
        with self.write():
            self._freeze(scope, reason)

    def uncovered(self, scope):
        # 旧归档补出的索引无法证明登记当时的身份，只认登记本身的 v2 证据。
        return self.row("""SELECT 1 FROM registration WHERE scope=? AND
            (digest IS NULL OR digest NOT LIKE '2:%' OR candidate IS NULL OR candidate NOT LIKE '2:%')
            LIMIT 1""", scope)

    def register(self, scope, generation, uid, item, *, seen=None):
        with self.write() as db:
            if not self.row("SELECT 1 FROM mailbox WHERE scope=? AND generation=? AND state='active'", scope, generation):
                raise Hold()
            loc = self.row("SELECT * FROM location WHERE scope=? AND generation=? AND uid=?", scope, generation, uid)
            if not loc or loc["status"] != "pending":
                raise Hold()
            key, digest, candidate = item.key, item.digest, item.candidate
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

    def _ledger_digest(self):
        digest = hashlib.sha256()
        for table in ("budget", "charge"):
            digest.update(table.encode())
            for row in self.db.execute(f"SELECT * FROM {table} ORDER BY id"):
                digest.update(json.dumps(tuple(row), separators=(",", ":")).encode())
        return digest.hexdigest()

    def _seal_ledger(self):
        self.db.execute("INSERT OR REPLACE INTO ledger_check VALUES(1,?)", (self._ledger_digest(),))

    def _ledger_valid(self):
        try:
            seal = self.row("SELECT digest FROM ledger_check WHERE id=1")
            if seal and seal[0] == self._ledger_digest():
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
        with self.write() as db:
            self._freeze(scope, "legacy")
            db.execute("""INSERT OR IGNORE INTO legacy_review SELECT ?,?,source FROM legacy
                WHERE status='pending'""", (scope, generation))

    def rebuild(self, scope, validity, cursor, history, entries):
        with self.write() as db:
            box = self.row("SELECT * FROM mailbox WHERE scope=?", scope)
            if box["state"] != "frozen":
                raise Hold()
            generation = box["generation"]
            observations = []
            for uid, item in entries:
                matches = list(db.execute("""SELECT id,status FROM registration WHERE scope=?
                    AND msgkey=? AND digest=?""", (scope, item.key, item.digest)))
                match = matches[0][0] if len(matches) == 1 and matches[0][1] != "frozen" else None
                observations.append((uid, item.key, item.digest, item.candidate, item.attachments, match))
            old = [tuple(r) for r in db.execute("""SELECT uid,msgkey,digest,candidate,attachments,matched
                FROM reconciliation WHERE scope=? AND generation=? ORDER BY uid""", (scope, generation))]
            if (box["reason"] != "reconciling" or box["validity"] != validity or box["cursor"] != cursor
                    or old != observations):
                generation += 1
                db.execute("INSERT INTO snapshot VALUES(?,?,?,?,?,?)", (scope, generation, validity, cursor, history, now()))
                db.executemany("""INSERT INTO reconciliation(scope,generation,uid,msgkey,digest,candidate,attachments,matched)
                    VALUES(?,?,?,?,?,?,?,?)""",
                               ((scope, generation, *data) for data in observations))
            db.execute("""UPDATE mailbox SET generation=?,validity=?,cursor=?,peak=?,reason='reconciling'
                WHERE scope=?""", (generation, validity, cursor, cursor, scope))

    def reconcile(self, scope, generation, validity, cursor):
        with self.write() as db:
            box = self.row("SELECT reason FROM mailbox WHERE scope=?", scope)
            if self.uncovered(scope) or self.row("""SELECT 1 FROM reconciliation WHERE scope=? AND generation=?
                    AND (digest IS NULL OR candidate IS NULL) LIMIT 1""", scope, generation):
                raise Hold()
            if db.execute("""UPDATE mailbox SET state='active',reason=NULL WHERE scope=? AND generation=?
                AND validity=? AND cursor=? AND state='frozen' AND reason IN ('reconciling','legacy')""",
                          (scope, generation, validity, cursor)).rowcount != 1:
                raise Hold()
            # 本次水位以前未能证明的新信、旧待办和可能已发的工作均交人工。
            db.execute("""UPDATE registration SET status='manual',reason='reconciliation',version=version+1
                WHERE scope=? AND status NOT IN ('done','no','frozen','manual',?)""", (scope, MANUAL_BODY_MISSED))
            for row in list(db.execute("""SELECT * FROM reconciliation WHERE scope=? AND generation=?
                    AND matched IS NULL""", (scope, generation))):
                reg = db.execute("""INSERT INTO registration(scope,msgkey,digest,candidate,attachments,status,
                    reason,created) VALUES(?,?,?,?,?,'manual','reconciliation',?)""",
                                 (scope, row["msgkey"], row["digest"], row["candidate"],
                                  row["attachments"], now())).lastrowid
                db.execute("UPDATE reconciliation SET matched=? WHERE scope=? AND generation=? AND uid=?",
                           (reg, scope, generation, row["uid"]))
            db.execute("DELETE FROM scan_range WHERE scope=?", (scope,))
            db.execute("DELETE FROM observed_range WHERE scope=?", (scope,))
            db.execute("""INSERT OR IGNORE INTO location(scope,generation,uid,registration,status)
                SELECT scope,generation,uid,matched,'reviewed' FROM reconciliation WHERE scope=? AND generation=?""",
                       (scope, generation))
            if box["reason"] == "legacy":
                db.execute("""UPDATE legacy SET status='reviewed' WHERE source IN
                    (SELECT source FROM legacy_review WHERE scope=? AND generation=?)""", (scope, generation))
