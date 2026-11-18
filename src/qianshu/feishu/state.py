"""工作区外的私有 SQLite 状态。"""

import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .settings import Settings


class State:
    def __init__(self, path: Path, settings: Settings):
        self.path = path
        self.scope = hashlib.sha256(
            f"{settings.app_id}\0{settings.chat_id}".encode()
        ).hexdigest()
        # 新库权限从创建时起即为 0600。部署时目录也应为私有目录。
        path.touch(mode=0o600, exist_ok=True)
        if path.stat().st_mode & 0o077:
            raise ValueError("飞书状态文件须设为私有权限。")
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS feishu_messages (
                    scope TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    PRIMARY KEY (scope, digest)
                );
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
                );
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=0.15, autocommit=True)
        try:
            yield db
        finally:
            db.close()

    def claim_message(self, message_id: str) -> bool:
        # 保存指纹即可跨重推和重启去重。
        digest = hashlib.sha256(message_id.encode()).hexdigest()
        with self.connection() as db:
            return db.execute(
                "INSERT OR IGNORE INTO feishu_messages VALUES (?, ?)", (self.scope, digest),
            ).rowcount == 1

    def has_message(self, message_id: str) -> bool:
        digest = hashlib.sha256(message_id.encode()).hexdigest()
        with self.connection() as db:
            return db.execute(
                "SELECT 1 FROM feishu_messages WHERE scope = ? AND digest = ?", (self.scope, digest),
            ).fetchone() is not None

    def bind(self, approval_id: str, message_id: str, record_id: str, expires: float) -> None:
        with self.connection() as db:
            db.execute("""
                INSERT INTO feishu_approvals (scope, approval_id, message_id, record_id, expires)
                VALUES (?, ?, ?, ?, ?)
            """, (self.scope, approval_id, message_id, record_id, expires))

    def decide(
        self, approval_id: str, message_id: str, record_id: str, decision: str,
        event_id: str, operator_id: str, now: float,
    ) -> bool:
        with self.connection() as db:
            return db.execute("""
                UPDATE OR IGNORE feishu_approvals
                SET status = ?, event_id = ?, operator_id = ?, decided_at = ?
                WHERE scope = ? AND approval_id = ? AND message_id = ? AND record_id = ?
                  AND status = 'pending' AND expires > ?
            """, (decision, event_id, operator_id, now, self.scope,
                  approval_id, message_id, record_id, now)).rowcount == 1

    def close_approvals(self) -> None:
        with self.connection() as db:
            db.execute("""
                UPDATE feishu_approvals SET status = 'closed'
                WHERE scope = ? AND status IN ('pending', 'approve')
            """, (self.scope,))
