"""按 Trace #12 的 X7 轮询验收邮件，交由调用方接续处理。"""

import imaplib
import logging
import os
import re
import sqlite3
import stat
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import Event
from time import monotonic

from .imap import SUMMARY, MailError, ReadOnlyMailbox, connect
from .message import body_parts, explicit_approval, headers, is_reply, message_id, request_text, sender

_LOG = logging.getLogger(__name__)
_PACKAGE_SOURCE_DIR = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class PollResult:
    total: int
    matched: int
    delivered: bool = False
    approved: bool = False


def _private_dir(config: Mapping[str, str]) -> Path:
    try:
        directory = Path(config.get("MAIL_PRIVATE_DIR") or ".private/").resolve()
    except (OSError, ValueError, RuntimeError):
        raise MailError("无法使用邮件私有目录。") from None
    repo_root = _PACKAGE_SOURCE_DIR.parent
    if (
        _PACKAGE_SOURCE_DIR.name == "src"
        and (repo_root / ".git").exists()
        and directory.is_relative_to(repo_root)
        and not directory.is_relative_to(repo_root / ".private")
    ):
        raise MailError("邮件私有目录须在仓库的 .private/ 内或仓库外。")
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.stat()
    except OSError:
        raise MailError("无法使用邮件私有目录。") from None
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise MailError("邮件私有目录须由当前用户拥有，并禁止其他用户写入。")
    return directory


def _fingerprint(*values: str | bytes) -> str:
    digest = sha256()
    for value in values:
        raw = value.encode("utf-8") if isinstance(value, str) else value
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def _sent_folder(config: Mapping[str, str]) -> str:
    folder = config.get("MAIL_SENT_FOLDER", "")
    if not folder.strip() or folder.casefold() == (config.get("MAIL_FOLDER") or "INBOX").casefold():
        raise MailError("请配置独立的已发送文件夹。")
    return folder


def _approval(mailbox: ReadOnlyMailbox, folder: str, request_id: str | None, approver: str) -> bool:
    if request_id is None:
        return False
    mailbox.select(folder)
    decisions = []
    for uid in mailbox.replies(request_id):
        summary = headers(mailbox.section(uid, SUMMARY))
        if sender(summary).casefold() == approver and is_reply(summary, request_id):
            raw_headers = mailbox.section(uid, "HEADER")
            decisions.append(explicit_approval(body_parts(mailbox, uid, raw_headers)))
    return bool(decisions) and all(decisions)


def _open_ledger(config: Mapping[str, str]) -> sqlite3.Connection:
    directory = _private_dir(config)
    path = directory / "mail-poll.sqlite3"
    fd = os.open(path, os.O_CREAT | os.O_NOFOLLOW | os.O_WRONLY | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise MailError("邮件去重记录须为普通文件。")
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    return sqlite3.connect(path, timeout=5)


def poll_once(
    config: Mapping[str, str],
    process: Callable[[str, bool, str], None],
    *,
    factory: Callable = imaplib.IMAP4_SSL,
) -> PollResult:
    """处理单封 X7 邮件。回调接收正文、当前审批结果和去重指纹。"""
    marker = config.get("MAIL_TEST_MARKER", "")
    approver = config.get("MAIL_APPROVER", "").strip().casefold()
    if not approver or not re.fullmatch(r"[A-Za-z0-9_-]+", marker):
        raise MailError("请配置产品负责人邮箱和一次性测试标记。")
    inbox = config.get("MAIL_FOLDER") or "INBOX"
    sent = _sent_folder(config)

    with connect(config, factory) as mailbox:
        total = mailbox.select(inbox)
        matches = []
        for uid in mailbox.marked(marker, approver):
            raw = mailbox.section(uid, SUMMARY)
            summary = headers(raw)
            subjects = summary.get_all("Subject", [])
            if sender(summary).casefold() == approver and len(subjects) == 1 and marker in str(subjects[0]):
                matches.append((uid, raw, message_id(summary)))
        if len(matches) != 1:
            _LOG.info("mail_poll total=%d matched=%d delivered=0", total, len(matches))
            return PollResult(total, len(matches))

        uid, raw, request_id = matches[0]
        scope = _fingerprint(config["MAIL_HOST"], config["MAIL_USER"], inbox, marker)
        fingerprint = _fingerprint(scope, str(uid), raw)
        try:
            with closing(_open_ledger(config)) as ledger:
                ledger.execute(
                    "CREATE TABLE IF NOT EXISTS delivery ("
                    "scope TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, delivered INTEGER NOT NULL)"
                )
                with ledger:
                    ledger.execute(
                        "INSERT OR IGNORE INTO delivery VALUES (?, ?, 0)", (scope, fingerprint)
                    )
                with ledger:
                    ledger.execute("BEGIN IMMEDIATE")
                    stored, delivered = ledger.execute(
                        "SELECT fingerprint, delivered FROM delivery WHERE scope = ?", (scope,)
                    ).fetchone()
                    if stored != fingerprint:
                        raise MailError("测试标记已绑定另一封邮件。")
                    body = ""
                    if not delivered:
                        raw_headers = mailbox.section(uid, "HEADER")
                        body = request_text(body_parts(mailbox, uid, raw_headers))
                        if not body:
                            raise MailError("测试邮件没有可处理的正文。")
                    approved = _approval(mailbox, sent, request_id, approver)
                    if not delivered:
                        try:
                            process(body, approved, fingerprint)
                        except Exception:
                            raise MailError("测试邮件处理回调失败。") from None
                        ledger.execute("UPDATE delivery SET delivered = 1 WHERE scope = ?", (scope,))
        except (OSError, sqlite3.Error):
            raise MailError("无法读写邮件去重记录。") from None

    _LOG.info("mail_poll total=%d matched=1 delivered=%d approved=%d", total, not delivered, approved)
    return PollResult(total, 1, not delivered, approved)


def run_polling(
    config: Mapping[str, str],
    process: Callable[[str, bool, str], None],
    stop: Event,
    *,
    factory: Callable = imaplib.IMAP4_SSL,
    clock: Callable[[], float] = monotonic,
    wait: Callable[[float], bool] | None = None,
) -> None:
    """立即轮询，每轮结束后等待 60 秒。wait 超时返回 False，收到停止信号返回 True。"""
    if wait is None:
        wait = stop.wait
    while not stop.is_set():
        try:
            poll_once(config, process, factory=factory)
        except MailError:
            _LOG.warning("mail_poll_failed count=1")
        deadline = clock() + 60
        while not stop.is_set():
            remaining = deadline - clock()
            if remaining <= 0:
                break
            if wait(remaining):
                return
