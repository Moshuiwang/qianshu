"""工作区外的私有 SQLite 状态。"""

import fcntl
import hashlib
import math
import os
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing, contextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import Lock
from typing import BinaryIO

from .settings import Settings


CALLBACK_BUDGET = 3
CALLBACK_RESPONSE_MARGIN = 0.5


class ProofReceipt:
    """只在证明事务返回后裁决。锁内不得等待数据库或连接。"""

    def __init__(self, proof_deadline: float):
        self.proof_deadline = proof_deadline
        self._lock = Lock()
        self.review_required = False
        self._timely = False
        self._confirmed = False
        self._expired = False

    def sample_second_commit(self, deadline, business, expires):
        with self._lock:
            sample = time.monotonic()
            self._timely = not self._expired and business is not None and business < expires and sample < deadline
            return sample if self._timely else None

    def allow_proof_wait(self):
        with self._lock:
            if not self._timely:
                self._expired = True
            return self._timely

    def confirm(self):
        with self._lock:
            self._confirmed = self._timely and not self._expired and time.monotonic() < self.proof_deadline
            return self._confirmed

    def end_wait(self):
        with self._lock:
            self._expired = True
            return self._confirmed


callback_receipt: ContextVar[ProofReceipt | None] = ContextVar("callback_receipt", default=None)

# 读取、认领和传输交接使用同一绑定及严格截止条件。历史单调时刻只与同次截止比较。
_PROVEN = """
    EXISTS (SELECT 1 FROM feishu_decision_proofs p
            WHERE p.scope = feishu_approvals.scope AND p.event_id = feishu_approvals.event_id
              AND p.approval_id = feishu_approvals.approval_id
              AND p.message_id = feishu_approvals.message_id
              AND p.record_id = feishu_approvals.record_id
              AND p.operator_id = feishu_approvals.operator_id
              AND p.expires = feishu_approvals.expires
              AND p.decision = CASE WHEN feishu_approvals.status = 'reject' THEN 'reject' ELSE 'approve' END
              AND p.first_mono < p.deadline AND p.second_mono < p.deadline
              AND p.first_business < p.expires AND p.second_business < p.expires)
"""


class State:
    def __init__(self, path: Path, settings: Settings, *, busy_timeout: float = 5):
        self.path = path
        self.scope = hashlib.sha256(
            f"{settings.app_id}\0{settings.chat_id}".encode()
        ).hexdigest()
        self._busy_ms = math.floor(busy_timeout * 1000)
        if not 0 <= self._busy_ms <= 2**31 - 1:
            raise ValueError("飞书状态库的等待时限无效。")
        self._write_lock = Lock()
        # 新库权限从创建时起即为 0600。部署时目录也应为私有目录。
        path.touch(mode=0o600, exist_ok=True)
        if path.stat().st_mode & 0o077:
            raise ValueError("飞书状态文件须设为私有权限。")
        self._writer = self._connect(check_same_thread=False)
        try:
            journal = self._writer.execute("PRAGMA journal_mode=WAL").fetchone()
            if journal != ("wal",):
                raise RuntimeError("飞书状态库无法开启 WAL。")
            self._writer.execute("PRAGMA synchronous=FULL")
            self._initialize()
        except BaseException:
            self._writer.close()
            raise

    def _initialize(self) -> None:
        with self._write() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS feishu_messages (
                    scope TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'done',
                    PRIMARY KEY (scope, digest)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS feishu_approvals (
                    scope TEXT NOT NULL,
                    approval_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    expires REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    event_id TEXT,
                    operator_id TEXT,
                    decided_at REAL,
                    PRIMARY KEY (scope, approval_id),
                    UNIQUE (scope, message_id),
                    UNIQUE (scope, event_id)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS feishu_callback_events (
                    scope TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    PRIMARY KEY (scope, event_id)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS feishu_decision_proofs (
                    scope TEXT NOT NULL, event_id TEXT NOT NULL,
                    approval_id TEXT NOT NULL, message_id TEXT NOT NULL, record_id TEXT NOT NULL,
                    operator_id TEXT NOT NULL, decision TEXT NOT NULL, expires REAL NOT NULL,
                    first_mono REAL NOT NULL, first_business REAL NOT NULL,
                    second_mono REAL NOT NULL, second_business REAL NOT NULL, deadline REAL NOT NULL,
                    PRIMARY KEY (scope, event_id)
                )
            """)
            if "attempted" not in {c[1] for c in db.execute("PRAGMA table_info(feishu_callback_events)")}:
                db.execute("ALTER TABLE feishu_callback_events ADD COLUMN attempted INTEGER NOT NULL DEFAULT 0")
                db.execute("UPDATE feishu_callback_events SET attempted = 1")
            db.execute("""
                INSERT OR IGNORE INTO feishu_callback_events (scope, event_id, attempted)
                SELECT scope, event_id, 1 FROM feishu_approvals WHERE event_id IS NOT NULL
            """)
            columns = {column[1] for column in db.execute("PRAGMA table_info(feishu_messages)")}
            if "status" not in columns:
                db.execute("ALTER TABLE feishu_messages ADD COLUMN status TEXT NOT NULL DEFAULT 'done'")
            # 旧入口只读 status；新版已认领的行对旧入口始终显示 done。
            for name, declaration in (
                ("stage", "TEXT"), ("binding", "TEXT"), ("created_ms", "INTEGER"),
                ("expires", "REAL"), ("manual_since", "REAL"),
                ("manual_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE feishu_messages ADD COLUMN {name} {declaration}")

    def message_lease(self, digest: str) -> BinaryIO | None:
        """每条消息只允许一个执行者，崩溃后由内核释放。"""
        directory = self.path.with_name(self.path.name + ".leases")
        directory.mkdir(mode=0o700, exist_ok=True)
        name = hashlib.sha256(f"{self.scope}\0{digest}".encode()).hexdigest()
        fd = os.open(directory / name, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        lease = os.fdopen(fd, "rb")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lease.close()
            return None
        return lease

    def receive(self, digest: str, binding: str, created_ms: int, expires: float) -> None:
        with self._write() as db:
            db.execute("""
                INSERT OR IGNORE INTO feishu_messages
                    (scope, digest, status, stage, binding, created_ms, expires)
                VALUES (?, ?, 'done', 'received', ?, ?, ?)
            """, (self.scope, digest, binding, created_ms, expires))

    def message_record(self, digest: str) -> sqlite3.Row | None:
        with self.connection() as db:
            db.row_factory = sqlite3.Row
            return db.execute("SELECT * FROM feishu_messages WHERE scope = ? AND digest = ?",
                              (self.scope, digest)).fetchone()

    def unfinished(self) -> list[sqlite3.Row]:
        with self.connection() as db:
            db.row_factory = sqlite3.Row
            return db.execute("""
                SELECT * FROM feishu_messages WHERE scope = ? AND stage IS NOT NULL
                    AND stage NOT IN ('done', 'manual_notified')
            """, (self.scope,)).fetchall()

    @staticmethod
    def recovery_list(path: Path) -> list[dict]:
        """只读打开，即使服务窗口已关闭，也能从私有库交接。"""
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise ValueError("须指定私有入口状态文件。")
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='trial_card'").fetchone():
                from .trial_approval import recovery
                return recovery(db)
            columns = {c[1] for c in db.execute("PRAGMA table_info(feishu_messages)")}
            if "digest" not in columns:
                raise ValueError("入口状态表不存在。")
            if "stage" not in columns:
                if "status" not in columns:
                    return []
                return [dict(row) for row in db.execute("""
                    SELECT scope, digest, NULL AS created_ms, NULL AS expires,
                           CASE WHEN status = 'notice_pending' THEN status ELSE 'legacy_unknown' END AS stage
                    FROM feishu_messages WHERE status != 'done' ORDER BY scope, digest
                """)]
            return [dict(row) for row in db.execute("""
                SELECT scope, digest, created_ms, expires, COALESCE(stage, CASE WHEN status = 'notice_pending' THEN status ELSE 'legacy_unknown' END) AS stage
                FROM feishu_messages
                WHERE stage NOT IN ('done', 'manual_notified')
                   OR (stage IS NULL AND status != 'done')
                ORDER BY scope, created_ms, digest
            """)]

    def advance_message(self, digest: str, previous: str, stage: str) -> None:
        with self._write() as db:
            changed = db.execute("""
                UPDATE feishu_messages SET stage = ? WHERE scope = ? AND digest = ? AND stage = ?
            """, (stage, self.scope, digest, previous)).rowcount
            if changed != 1:
                raise RuntimeError("入口状态已变化。")

    def start_manual_notice(self, digest: str, stage: str, now: float) -> None:
        with self._write() as db:
            changed = db.execute("""
                UPDATE feishu_messages SET manual_since = COALESCE(manual_since, ?),
                    manual_attempts = manual_attempts + 1, stage = 'manual_notice_unknown'
                WHERE scope = ? AND digest = ? AND stage = ?
            """, (now, self.scope, digest, stage)).rowcount
            if changed != 1:
                raise RuntimeError("入口状态已变化。")

    def _connect(self, *, check_same_thread: bool = True) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, autocommit=True, check_same_thread=check_same_thread)
        db.execute(f"PRAGMA busy_timeout={self._busy_ms}")
        return db

    @contextmanager
    def connection(self):
        db = self._connect()
        try:
            db.execute("PRAGMA query_only=ON")
            yield db
        finally:
            db.close()

    @contextmanager
    def _write(
        self, *, deadline: float | None = None, before_commit: Callable[[], None] | None = None,
    ):
        timeout = -1 if deadline is None else max(0, deadline - time.monotonic())
        if not self._write_lock.acquire(timeout=timeout):
            raise TimeoutError
        db = self._writer
        try:
            busy_ms = self._busy_ms
            if deadline is not None:
                remaining_ms = math.floor((deadline - time.monotonic()) * 1000)
                if remaining_ms <= 0:
                    raise TimeoutError
                busy_ms = min(busy_ms, remaining_ms)
            db.execute(f"PRAGMA busy_timeout={busy_ms}")
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
                if before_commit is not None:
                    before_commit()
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError
                db.execute("COMMIT")
            except BaseException as error:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                if (
                    deadline is not None and isinstance(error, sqlite3.OperationalError)
                    and error.sqlite_errorcode == sqlite3.SQLITE_BUSY
                ):
                    raise TimeoutError from None
                raise
        finally:
            self._write_lock.release()

    def close(self) -> None:
        with self._write_lock:
            self._writer.close()

    def claim_message(self, message_id: str, status: str = "done") -> bool:
        # 保存指纹即可跨重推和重启去重。
        digest = hashlib.sha256(message_id.encode()).hexdigest()
        with self._write() as db:
            return db.execute(
                "INSERT OR IGNORE INTO feishu_messages (scope, digest, status) VALUES (?, ?, ?)",
                (self.scope, digest, status),
            ).rowcount == 1

    def message_status(self, message_id: str) -> str | None:
        digest = hashlib.sha256(message_id.encode()).hexdigest()
        with self.connection() as db:
            row = db.execute(
                "SELECT COALESCE(stage, status) FROM feishu_messages WHERE scope = ? AND digest = ?", (self.scope, digest),
            ).fetchone()
            return row[0] if row else None

    def complete_notice(self, message_id: str) -> None:
        digest = hashlib.sha256(message_id.encode()).hexdigest()
        with self._write() as db:
            db.execute("""
                UPDATE feishu_messages SET status = 'done'
                WHERE scope = ? AND digest = ? AND status = 'notice_pending'
            """, (self.scope, digest))

    def bind(self, approval_id: str, message_id: str, record_id: str, expires: float) -> None:
        with self._write() as db:
            db.execute("""
                INSERT INTO feishu_approvals (scope, approval_id, message_id, record_id, expires)
                VALUES (?, ?, ?, ?, ?)
            """, (self.scope, approval_id, message_id, record_id, expires))

    def claim_callback(
        self, event_id: str, *, deadline: float | None = None,
        before_commit: Callable[[], None] | None = None,
    ) -> bool:
        # 在核实成员之前持久认领。查询失败、超时或进程中断时，重推也不能重开审批。
        with self._write(deadline=deadline, before_commit=before_commit) as db:
            return db.execute("""
                INSERT OR IGNORE INTO feishu_callback_events (scope, event_id) VALUES (?, ?)
            """, (self.scope, event_id)).rowcount == 1

    def decide(
        self, approval_id: str, message_id: str, record_id: str, decision: str,
        event_id: str, operator_id: str, current_time: Callable[[], float | None],
        *, deadline: float | None = None, before_commit: Callable[[], None] | None = None,
    ) -> bool:
        if decision not in ("approve", "reject"):
            return False
        if deadline is None:
            deadline = time.monotonic() + CALLBACK_BUDGET - CALLBACK_RESPONSE_MARGIN
        receipt = callback_receipt.get() or ProofReceipt(deadline)
        candidate = "candidate_" + decision
        binding = (self.scope, approval_id, message_id, record_id, event_id)
        with self._write(deadline=deadline, before_commit=before_commit) as db:
            now = current_time()
            if now is None or not db.execute("""
                UPDATE feishu_callback_events SET attempted = 1
                WHERE scope = ? AND event_id = ? AND attempted = 0
            """, (self.scope, event_id)).rowcount:
                return False
            row = db.execute("""
                UPDATE feishu_approvals SET status = ?, event_id = ?, operator_id = ?, decided_at = ?
                WHERE scope = ? AND approval_id = ? AND message_id = ? AND record_id = ?
                  AND status = 'pending' AND expires > ?
                RETURNING expires
            """, (candidate, event_id, operator_id, now, *binding[:4], now)).fetchone()
        if row is None:
            return False
        expires, = row
        first_business, first_mono = current_time(), time.monotonic()
        match = "scope = ? AND approval_id = ? AND message_id = ? AND record_id = ? AND event_id = ?"
        if first_business is None or first_business >= expires or first_mono >= deadline:
            with self._write() as db:
                retry = first_business is not None and first_business < expires
                db.execute(f"""
                    UPDATE feishu_approvals SET status = ?, event_id = NULL, operator_id = NULL, decided_at = NULL
                    WHERE {match} AND status = ?
                """, ("pending" if retry else "closed", *binding, candidate))
            return False

        receipt.review_required = True
        fixed = "confirming_" + decision
        with self._write(deadline=deadline) as db:
            if not db.execute(f"UPDATE feishu_approvals SET status = ? WHERE {match} AND status = ?",
                              (fixed, *binding, candidate)).rowcount:
                return False
        second_business = current_time()
        second_mono = receipt.sample_second_commit(deadline, second_business, expires)
        if second_mono is None:
            return False

        with self._write(deadline=receipt.proof_deadline) as db:
            if not db.execute(f"""
                INSERT INTO feishu_decision_proofs (
                    scope, event_id, approval_id, message_id, record_id, operator_id, decision, expires,
                    first_mono, first_business, second_mono, second_business, deadline
                )
                SELECT scope, event_id, approval_id, message_id, record_id, operator_id, ?, expires, ?, ?, ?, ?, ?
                FROM feishu_approvals WHERE {match} AND status = ?
            """, (decision, first_mono, first_business, second_mono, second_business, deadline,
                  *binding, fixed)).rowcount:
                return False
        # 此时证明才已同步落盘。等待方已判超时或提交迟到时，绝不发布，也不在恢复时补签。
        business = current_time()
        if business is None or business >= expires or not receipt.confirm():
            return False
        with self._write() as db:
            return db.execute(f"UPDATE feishu_approvals SET status = ? WHERE {match} AND status = ?",
                              (decision, *binding, fixed)).rowcount == 1

    def close_approvals(
        self, *, deadline: float | None = None, before_commit: Callable[[], None] | None = None,
    ) -> None:
        with self._write(deadline=deadline, before_commit=before_commit) as db:
            db.execute("""
                UPDATE feishu_approvals SET status = 'closed'
                WHERE scope = ? AND status IN ('pending', 'candidate_approve', 'candidate_reject', 'confirming_approve', 'confirming_reject', 'approve', 'claimed')
            """, (self.scope,))

    def approval_status(self, approval_id: str, record_id: str) -> str:
        with self.connection() as db:
            row = db.execute(f"""
                SELECT CASE
                    WHEN status IN ('approve', 'claimed') AND NOT {_PROVEN} THEN 'closed'
                    WHEN status IN ('candidate_approve', 'candidate_reject', 'confirming_approve', 'confirming_reject') THEN 'pending'
                    ELSE status END FROM feishu_approvals
                WHERE scope = ? AND approval_id = ? AND record_id = ?
            """, (self.scope, approval_id, record_id)).fetchone()
            return row[0] if row else "closed"

    def close_create_approval(self, approval_id: str, record_id: str) -> None:
        with self._write() as db:
            db.execute("""
                UPDATE feishu_approvals SET status = 'closed'
                WHERE scope = ? AND approval_id = ? AND record_id = ?
                  AND status IN ('pending', 'candidate_approve', 'candidate_reject', 'confirming_approve', 'confirming_reject', 'approve', 'claimed')
            """, (self.scope, approval_id, record_id))

    def claim_create(self, approval_id, record_id):
        with self._write() as db:
            row = db.execute(f"""
                UPDATE feishu_approvals SET status = 'claimed'
                WHERE scope = ? AND approval_id = ? AND record_id = ? AND status = 'approve' AND {_PROVEN}
                RETURNING expires
            """, (self.scope, approval_id, record_id)).fetchone()
            return row[0] if row is not None else None

    def dispatch_create(self, approval_id, record_id, dispatch):
        # 关闭或认领同一卡片的其他进程与传输交接串行。
        # 即使这里回滚，入口也会依据已落盘的尝试记录冻结 Trace 资格。
        with self._write() as db:
            row = db.execute(f"""
                UPDATE feishu_approvals SET status = 'used'
                WHERE scope = ? AND approval_id = ? AND record_id = ? AND status = 'claimed' AND {_PROVEN}
                RETURNING expires
            """, (self.scope, approval_id, record_id)).fetchone()
            if row is None:
                return False
            dispatch(row[0])
            return True


def trial_batch(db: sqlite3.Connection, registration: int, version: int):
    """按卡片绑定的登记版本读取整批人工原因。连接指向 050 私有登记库。"""
    from qianshu.mail.trial_state import BODY_NOTICE, FORMAT_HOLD, FORMAT_VERSION, MANUAL_BODY_MISSED

    if db.execute("PRAGMA user_version").fetchone()[0] != FORMAT_VERSION:
        return registration, version, None, FORMAT_HOLD

    row = db.execute("""SELECT r.attachments,r.status,m.state FROM registration r JOIN mailbox m USING(scope)
        WHERE r.id=? AND r.version=?""", (registration, version)).fetchone()
    if not row:
        return registration, version, None, "登记缺失或版本失效"
    count, status, state = row
    if status == MANUAL_BODY_MISSED:
        reason = BODY_NOTICE
    elif state != "active" or status in ("manual", "frozen"):
        reason = "登记冻结或待人工"
    elif count is None or count > 0:
        reason = "附件未解析"
    elif status != "done":
        reason = "草稿尚未确认送达"
    else:
        reason = None
    return registration, version, count, reason
