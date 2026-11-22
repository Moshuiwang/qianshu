"""工作区外的私有 SQLite 状态。"""

import fcntl
import hashlib
import math
import os
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing, contextmanager
from pathlib import Path
from threading import Lock
from typing import BinaryIO

from .settings import Settings


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
                INSERT OR IGNORE INTO feishu_callback_events (scope, event_id)
                SELECT scope, event_id FROM feishu_approvals WHERE event_id IS NOT NULL
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
        with self._write(deadline=deadline, before_commit=before_commit) as db:
            now = current_time()
            if now is None:
                return False
            return db.execute("""
                UPDATE OR IGNORE feishu_approvals
                SET status = ?, event_id = ?, operator_id = ?, decided_at = ?
                WHERE scope = ? AND approval_id = ? AND message_id = ? AND record_id = ?
                  AND status = 'pending' AND expires > ?
                  AND EXISTS (
                    SELECT 1 FROM feishu_callback_events WHERE scope = ? AND event_id = ?
                  )
            """, (decision, event_id, operator_id, now, self.scope,
                  approval_id, message_id, record_id, now, self.scope, event_id)).rowcount == 1

    def close_approvals(
        self, *, deadline: float | None = None, before_commit: Callable[[], None] | None = None,
    ) -> None:
        with self._write(deadline=deadline, before_commit=before_commit) as db:
            db.execute("""
                UPDATE feishu_approvals SET status = 'closed'
                WHERE scope = ? AND status IN ('pending', 'approve', 'claimed')
            """, (self.scope,))

    def approval_status(self, approval_id: str, record_id: str) -> str:
        with self.connection() as db:
            row = db.execute("""
                SELECT status FROM feishu_approvals
                WHERE scope = ? AND approval_id = ? AND record_id = ?
            """, (self.scope, approval_id, record_id)).fetchone()
            return row[0] if row else "closed"

    def close_create_approval(self, approval_id: str, record_id: str) -> None:
        with self._write() as db:
            db.execute("""
                UPDATE feishu_approvals SET status = 'closed'
                WHERE scope = ? AND approval_id = ? AND record_id = ?
                  AND status IN ('pending', 'approve', 'claimed')
            """, (self.scope, approval_id, record_id))

    def claim_create(self, approval_id, record_id):
        with self._write() as db:
            row = db.execute("""
                UPDATE feishu_approvals SET status = 'claimed'
                WHERE scope = ? AND approval_id = ? AND record_id = ? AND status = 'approve'
                RETURNING expires
            """, (self.scope, approval_id, record_id)).fetchone()
            return row[0] if row is not None else None

    def dispatch_create(self, approval_id, record_id, dispatch):
        # 关闭或认领同一卡片的其他进程与传输交接串行。
        # 即使这里回滚，入口也会依据已落盘的尝试记录冻结 Trace 资格。
        with self._write() as db:
            row = db.execute("""
                UPDATE feishu_approvals SET status = 'used'
                WHERE scope = ? AND approval_id = ? AND record_id = ? AND status = 'claimed'
                RETURNING expires
            """, (self.scope, approval_id, record_id)).fetchone()
            if row is None:
                return False
            dispatch(row[0])
            return True
