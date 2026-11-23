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
from enum import Enum
from hashlib import sha256
from pathlib import Path
from threading import TIMEOUT_MAX, Event, Lock, Thread
from time import monotonic

from .imap import SUMMARY, MailError, ReadOnlyMailbox, connect
from .message import application_attachments, authored_reply, body_parts, headers, is_reply, message_id, request_text, sender

_LOG = logging.getLogger(__name__)
_PACKAGE_SOURCE_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_APPROVAL_TIMEOUT = 5.0
_APPROVAL_TASK_LOCK = Lock()
_APPROVAL_THREAD: Thread | None = None


@dataclass(frozen=True)
class PollResult:
    total: int
    matched: int
    delivered: bool = False
    approved: bool = False


class ApprovalDecision(Enum):
    APPROVE = "同意"
    REJECT = "不同意"
    CONDITIONAL = "带条件"
    UNCERTAIN = "不确定"


ApprovalJudge = Callable[[str], ApprovalDecision]


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


def _bounded_approval(
    judge: ApprovalJudge | None, timeout: float,
) -> Callable[[list[str]], bool] | None:
    if judge is None:
        return None
    if not 0 < timeout <= TIMEOUT_MAX:
        raise ValueError("审批判断超时须为有效的正秒数。")

    def check(texts: list[str]) -> bool:
        global _APPROVAL_THREAD
        approved = False
        expired = Event()

        def evaluate() -> None:
            nonlocal approved
            try:
                approved = all(
                    not expired.is_set() and judge(text) is ApprovalDecision.APPROVE
                    for text in texts
                )
            except BaseException:
                # 后台线程的异常也按不确定处理，避免异常文本进入日志。
                approved = False

        with _APPROVAL_TASK_LOCK:
            # 判断线程真正退出后才允许下一轮启动，与判断器对象无关。
            if _APPROVAL_THREAD is not None and _APPROVAL_THREAD.is_alive():
                return False
            thread = Thread(target=evaluate, name="mail-approval-judge", daemon=True)
            try:
                thread.start()
            except RuntimeError:
                return False
            _APPROVAL_THREAD = thread
        thread.join(timeout)
        if thread.is_alive():
            expired.set()
            return False
        return approved

    return check


def _approval(
    mailbox: ReadOnlyMailbox, folder: str | None, request_id: str | None, approver: str,
    check: Callable[[list[str]], bool] | None,
) -> bool:
    if folder is None or request_id is None or check is None:
        return False
    mailbox._examine(folder)
    texts = []
    for uid in mailbox.replies(request_id):
        summary = headers(mailbox.section(uid, SUMMARY))
        if sender(summary).casefold() == approver and is_reply(summary, request_id):
            try:
                raw_headers = mailbox.section(uid, "HEADER")
                parts = body_parts(mailbox, uid, raw_headers)
            except MailError:
                return False
            authored = dict.fromkeys(authored_reply(kind, content) for kind, content in parts)
            if not authored or None in authored or "" in authored:
                return False
            texts.extend(text for text in authored if text is not None)
    return bool(texts) and check(texts)


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
    process: Callable,
    *,
    include_attachments: bool = False,
    factory: Callable = imaplib.IMAP4_SSL,
    approval_judge: ApprovalJudge | None = None,
    approval_judge_timeout: float = _DEFAULT_APPROVAL_TIMEOUT,
) -> PollResult:
    """处理单封 X7 邮件。回调接收正文、当前审批结果和去重指纹；启用附件时第四项为来源块元组。"""
    return _poll_once(config, process, factory, _bounded_approval(approval_judge, approval_judge_timeout),
                      include_attachments)


def _poll_once(
    config: Mapping[str, str],
    process: Callable,
    factory: Callable,
    check_approval: Callable[[list[str]], bool] | None, include_attachments: bool,
) -> PollResult:
    marker = config.get("MAIL_TEST_MARKER", "")
    approver = config.get("MAIL_APPROVER", "").strip().casefold()
    if not approver or not re.fullmatch(r"[A-Za-z0-9_-]+", marker):
        raise MailError("请配置产品负责人邮箱和一次性测试标记。")
    inbox = config.get("MAIL_FOLDER") or "INBOX"

    with connect(config, factory) as mailbox:
        sent = mailbox.sent_folder(config)
        scope = _fingerprint(config["MAIL_HOST"], config["MAIL_USER"], inbox, marker)
        total = mailbox.select(inbox)
        matches = []
        for uid in mailbox.marked(marker, approver):
            raw = mailbox.section(uid, SUMMARY)
            summary = headers(raw)
            subjects = summary.get_all("Subject", [])
            if (
                sender(summary).casefold() == approver
                and len(subjects) == 1
                and marker in str(subjects[0])
                and "In-Reply-To" not in summary
                and "References" not in summary
            ):
                matches.append((uid, summary, _fingerprint(scope, str(uid), raw)))
        if not matches:
            _LOG.info("mail_poll total=%d matched=0 delivered=0", total)
            return PollResult(total, 0)
        try:
            with closing(_open_ledger(config)) as ledger:
                ledger.execute(
                    "CREATE TABLE IF NOT EXISTS delivery ("
                    "scope TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, delivered INTEGER NOT NULL)"
                )
                with ledger:
                    ledger.execute("BEGIN IMMEDIATE")
                    binding = ledger.execute(
                        "SELECT fingerprint, delivered FROM delivery WHERE scope = ?", (scope,)
                    ).fetchone()
                    if len(matches) != 1:
                        _LOG.info("mail_poll total=%d matched=%d delivered=0", total, len(matches))
                        return PollResult(total, len(matches))

                    uid, summary, fingerprint = matches[0]
                    if binding is None:
                        ledger.execute("INSERT INTO delivery VALUES (?, ?, 0)", (scope, fingerprint))
                        ledger.commit()  # 交接失败时保留绑定，下一轮只能重试同一封邮件。
                        ledger.execute("BEGIN IMMEDIATE")
                        binding = ledger.execute(
                            "SELECT fingerprint, delivered FROM delivery WHERE scope = ?", (scope,)
                        ).fetchone()
                    stored, delivered = binding
                    if stored != fingerprint:
                        raise MailError("测试标记已绑定另一封邮件。")
                    if not delivered:
                        raw_headers = mailbox.section(uid, "HEADER")
                        body = request_text(body_parts(mailbox, uid, raw_headers))
                    approved = _approval(mailbox, sent, message_id(summary), approver, check_approval)
                    if not delivered:
                        blocks = ()
                        if include_attachments and approved:
                            mailbox.select(inbox)
                            if _fingerprint(scope, str(uid), mailbox.section(uid, SUMMARY)) != fingerprint:
                                raise MailError("测试标记已绑定另一封邮件。")
                            blocks = application_attachments(mailbox, uid, raw_headers, fingerprint)
                        if not body and not blocks:
                            raise MailError("测试邮件没有可处理的正文。")
                        try:
                            if include_attachments:
                                process(body, approved, fingerprint, blocks)
                            else:
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
    process: Callable,
    stop: Event,
    *,
    include_attachments: bool = False,
    factory: Callable = imaplib.IMAP4_SSL,
    clock: Callable[[], float] = monotonic,
    wait: Callable[[float], bool] | None = None,
    approval_judge: ApprovalJudge | None = None,
    approval_judge_timeout: float = _DEFAULT_APPROVAL_TIMEOUT,
    on_result: Callable[[PollResult], None] | None = None,
) -> None:
    """立即轮询，每轮结束后等待 60 秒。wait 超时返回 False，收到停止信号返回 True。"""
    if wait is None:
        wait = stop.wait
    check_approval = _bounded_approval(approval_judge, approval_judge_timeout)
    while not stop.is_set():
        try:
            result = _poll_once(config, process, factory, check_approval, include_attachments)
        except MailError:
            _LOG.warning("mail_poll_failed count=1")
        else:
            if on_result is not None:
                on_result(result)
        deadline = clock() + 60
        while not stop.is_set():
            remaining = deadline - clock()
            if remaining <= 0:
                break
            if wait(remaining):
                return
