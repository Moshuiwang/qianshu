"""收件、回执及离线注入。外部 I/O 由调用方提供。"""

import hashlib
import json
import logging
import re
import time
import unicodedata
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Condition, Thread
from typing import BinaryIO, Protocol

from .cards import Report, result_card
from .settings import Settings, Window
from .state import State

_NOTICE = "概念验证期不处理"
_NOTICE_BUDGET = 3
_NOTICE_RETRY_DELAY = 0.5
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, repr=False)
class Message:
    message_id: str
    chat_id: str
    sender_id: str
    created_ms: int
    kind: str
    content: str
    bot_mention_keys: tuple[str, ...] = ()
    sender_type: str = "user"


@dataclass(frozen=True, repr=False)
class Attachment:
    kind: str
    key: str


@dataclass(frozen=True, repr=False)
class Application:
    message: Message
    text: str
    attachments: tuple[bytes, ...]


class Transport(Protocol):
    def react(self, message_id: str) -> None: ...
    def reply(self, message_id: str, text: str, *, deadline: float) -> None: ...
    def send_card(self, card: dict) -> str: ...
    def history(self, start: float, end: float) -> Iterable[Message]: ...
    def download(self, message_id: str, attachment: Attachment) -> bytes: ...


def fingerprint(text: str) -> str:
    normalized = "".join(unicodedata.normalize("NFKC", text).split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def _application_body(message: Message) -> tuple[str, tuple[Attachment, ...]]:
    body = json.loads(message.content)
    if message.kind == "text":
        text = body["text"]
        # 飞书的 @ 占位符是传输元数据。保留其余正文做整段指纹校验。
        removed = 0
        for key in message.bot_mention_keys:
            if re.fullmatch(r"@_user_[0-9]+", key):
                text, count = re.subn(re.escape(key) + r"(?![0-9])", "", text, count=1)
                removed += count
        return (text if removed else ""), ()

    if message.kind != "post":
        raise ValueError("消息类型不含获准正文。")

    paragraphs = [body.get("title", "")]
    attachments = []
    mentioned = False
    for paragraph in body["content"]:
        fragments = []
        for item in paragraph:
            tag = item["tag"]
            if tag == "text":
                fragments.append(item["text"])
            elif tag == "at" and item.get("user_id") in message.bot_mention_keys:
                mentioned = True
            elif tag == "img" and isinstance(item.get("image_key"), str) and item["image_key"]:
                attachments.append(Attachment("image", item["image_key"]))
            elif tag == "file" and isinstance(item.get("file_key"), str) and item["file_key"]:
                attachments.append(Attachment("file", item["file_key"]))
            else:
                raise ValueError("富文本包含未识别的元素。")
        paragraphs.append("".join(fragments))
    return ("\n".join(paragraphs) if mentioned else ""), tuple(attachments)


@dataclass(frozen=True)
class _ManualRetry:
    digest: str


class Entrance:
    """注入口与飞书收件共用过滤、去重及回执流程。"""

    def __init__(
        self, settings: Settings, window: Window, state: State, transport: Transport,
        process: Callable[[Application], Report], *, clock: Callable[[], float] = time.time,
        snapshot_status: Callable[[str], str | None] | None = None,
    ):
        self.settings = settings
        self.window = window
        self.state = state
        self.transport = transport
        self.binding = hashlib.sha256(json.dumps([
            settings.app_id, settings.tenant_key, settings.chat_id, settings.bot_open_id,
            sorted(settings.allowed_senders), settings.approved_fingerprint,
            window.start.timestamp(), window.end.timestamp(),
        ]).encode()).hexdigest()
        self.manual_send: Callable[[dict, str, float], None] | None = None
        self.snapshot_status = snapshot_status
        self.recover_snapshot = snapshot_status
        self.process = process
        self.clock = clock
        self._pending = set()
        self._condition = Condition()
        self._retry_at: dict[str | _ManualRetry, float] = {}
        self._retry_thread: Thread | None = None
        self._closed = False
        self._receipts = ThreadPoolExecutor(max_workers=8, thread_name_prefix="feishu-receipt")
        self._reactions = ThreadPoolExecutor(max_workers=8, thread_name_prefix="feishu-reaction")
        self._work = ThreadPoolExecutor(max_workers=1, thread_name_prefix="feishu-work")
        self._history = ThreadPoolExecutor(max_workers=1, thread_name_prefix="feishu-history")

    def _in_scope(self, message: Message) -> bool:
        return (
            message.chat_id == self.settings.chat_id and bool(message.message_id.strip())
            and self.window.contains(message.created_ms / 1000) and self.window.contains(self.clock())
        )

    def inject_message(self, message: Message) -> Future | None:
        """进程内注入，使用离线 Transport 时整个处理链无需 TCP。"""
        deadline = time.monotonic() + _NOTICE_BUDGET
        if self._in_scope(message) and message.bot_mention_keys:
            return self._receipts.submit(self._receive, message, deadline)
        return None

    def _receive(self, message: Message, deadline: float) -> Future | None:
        message_id = message.message_id
        if not self._claim_pending(message_id):
            return None
        work = None
        retry = False
        lease = None
        try:
            if not self.window.contains(self.clock()):
                return None
            status = self.state.message_status(message_id)
            if status in ("done", "manual_notified", "listed"):
                return None
            text = self._approved_text(message)
            if status == "notice_pending" or (status is None and text is None):
                if status is None and not self.state.claim_message(message_id, "notice_pending"):
                    return None
                retry = not self._send_notice(message_id, deadline)
                return None
            if text is None:
                return None
            digest = hashlib.sha256(message_id.encode()).hexdigest()
            lease = self.state.message_lease(digest)
            if lease is None:
                return None
            if status is None:
                self.state.receive(digest, self.binding, message.created_ms, self.window.end.timestamp())
            row = self.state.message_record(digest)
            if row["binding"] != self.binding or row["stage"] in (None, "done", "manual_notified", "listed"):
                return None
            work = self._dispatch_after_reaction(message, text, digest, row["stage"], lease)
        except Exception:
            _LOG.error("feishu_receipt_failed")
        finally:
            if work is None:
                if lease is not None:
                    lease.close()
                self._release(message_id, retry=retry)
        return work

    def _claim_pending(self, message_id: str) -> bool:
        with self._condition:
            if self._closed or message_id in self._pending:
                return False
            self._pending.add(message_id)
            return True

    def _dispatch_after_reaction(
        self, message: Message, text: str, digest: str, status: str, lease: BinaryIO,
    ) -> Future:
        finished = Future()
        finished.set_running_or_notify_cancel()
        message_id = message.message_id

        def finish(task: Future) -> None:
            lease.close()
            self._release(message_id)
            if not task.cancelled() and (error := task.exception()) is not None:
                _LOG.error("feishu_message_processing_failed")
                finished.set_exception(error)
            else:
                finished.set_result(None)

        def dispatch(receipt: Future) -> None:
            if receipt.cancelled() or receipt.exception() is not None or not receipt.result():
                finish(receipt)
                return
            work = self._work.submit(self._handle, message, text, digest)
            work.add_done_callback(finish)

        receipt = self._reactions.submit(self._prepare, message_id, digest, status)
        receipt.add_done_callback(dispatch)
        return finished

    def _manual(self, digest: str, status: str) -> None:
        row = self.state.message_record(digest)
        if row["binding"] != self.binding or not self.window.contains(self.clock()):
            return
        if status not in ("received", "reacting", "ready", "processing", "report_saved", "sending",
                          "manual_pending", "manual_notice_unknown"):
            self.state.advance_message(digest, status, "listed")
            _LOG.warning("feishu_recovery_listed count=1")
            return
        if status not in ("manual_pending", "manual_notice_unknown"):
            self.state.advance_message(digest, status, "manual_pending")
            status = "manual_pending"
        first = row["manual_since"]
        now = self.clock()
        if first is not None and (
            self.manual_send is None or row["manual_attempts"] >= 3 or not first <= now < first + 55 * 60
        ):
            self.state.advance_message(digest, status, "listed")
            _LOG.warning("feishu_recovery_listed count=1")
            return
        self.state.start_manual_notice(digest, status, now)
        when = datetime.fromtimestamp(row["created_ms"] / 1000, UTC).isoformat()
        card = result_card(Report("飞书群申请", failures=(
            f"请人工核对未完成申请。原消息 UTC 时间 {when}，消息指纹 {digest}。",
        )))
        try:
            if self.manual_send is None:
                self.transport.send_card(card)
            else:
                self.manual_send(card, digest, first if first is not None else now)
        except Exception:
            _LOG.warning("feishu_manual_notice_unconfirmed count=1")
            self._release(_ManualRetry(digest), retry=True)
            return
        self.state.advance_message(digest, "manual_notice_unknown", "manual_notified")

    def _snapshot(self, digest: str, *, recover: bool = False) -> str | None:
        lookup = self.recover_snapshot if recover else self.snapshot_status
        try:
            return lookup(digest) if lookup else None
        except Exception:
            return "manual_pending"

    def _reconcile(self, digest: str, status: str, snapshot: str | None) -> None:
        if snapshot in ("report_saved", "done", "listed"):
            if status == "processing" and snapshot == "done":
                self.state.advance_message(digest, status, "report_saved")
                status = "report_saved"
            if status != snapshot:
                self.state.advance_message(digest, status, snapshot)
            if snapshot == "listed":
                _LOG.warning("feishu_recovery_listed count=1")
        else:
            self._manual(digest, status)

    def _prepare(self, message_id: str, digest: str, status: str) -> bool:
        if not self.window.contains(self.clock()):
            return False
        if status == "received":
            self.state.advance_message(digest, status, "reacting")
            try:
                self.transport.react(message_id)
            except Exception:
                self._manual(digest, "reacting")
                return False
            self.state.advance_message(digest, "reacting", "ready")
            return True
        if status == "ready":
            return True
        if status in ("processing", "report_saved"):
            snapshot = self._snapshot(digest, recover=True)
            if status == "processing" and snapshot is None and self.snapshot_status is not None:
                self.state.advance_message(digest, status, "ready")
                return True
            self._reconcile(digest, status, snapshot)
        else:
            self._manual(digest, status)
        return False

    def _send_notice(self, message_id: str, deadline: float) -> bool:
        with self._condition:
            if self._closed or time.monotonic() >= deadline or not self.window.contains(self.clock()):
                return False
        try:
            self.transport.reply(message_id, _NOTICE, deadline=deadline)
            self.state.complete_notice(message_id)
        except Exception:
            _LOG.error("feishu_receipt_failed")
            return False
        return True

    def _release(self, message_id: str | _ManualRetry, *, retry: bool = False) -> None:
        with self._condition:
            self._pending.discard(message_id)
            if retry and not self._closed and self.window.contains(self.clock()):
                self._retry_at[message_id] = time.monotonic() + _NOTICE_RETRY_DELAY
                if self._retry_thread is None:
                    self._retry_thread = Thread(target=self._schedule_retries, name="feishu-retry")
                    self._retry_thread.start()
            self._condition.notify()

    def _schedule_retries(self) -> None:
        with self._condition:
            while not self._closed:
                remaining = self.window.end.timestamp() - self.clock()
                if remaining <= 0:
                    self._retry_at.clear()
                    return
                if not self._retry_at:
                    self._condition.wait(remaining)
                    continue
                message_id = min(self._retry_at, key=self._retry_at.__getitem__)
                delay = self._retry_at[message_id] - time.monotonic()
                if delay > 0:
                    self._condition.wait(min(delay, remaining))
                    continue
                if message_id in self._pending:
                    self._retry_at[message_id] = time.monotonic() + _NOTICE_RETRY_DELAY
                    continue
                # 与实时和补漏共用占用状态，入队前即合并同一条消息的发送尝试。
                del self._retry_at[message_id]
                self._pending.add(message_id)
                deadline = time.monotonic() + _NOTICE_BUDGET
                self._receipts.submit(self._retry_notice, message_id, deadline)

    def _retry_notice(self, message_id: str | _ManualRetry, deadline: float) -> None:
        retry = False
        try:
            if isinstance(message_id, _ManualRetry):
                digest = message_id.digest
                lease = self.state.message_lease(digest)
                if lease is None:
                    retry = True
                else:
                    with lease:
                        row = self.state.message_record(digest)
                        if row["stage"] == "manual_notice_unknown":
                            self._manual(digest, row["stage"])
                return
            if self.window.contains(self.clock()) and self.state.message_status(message_id) == "notice_pending":
                retry = not self._send_notice(message_id, deadline)
        except Exception:
            _LOG.error("feishu_receipt_failed")
            retry = True
        finally:
            self._release(message_id, retry=retry)

    def _approved_text(self, message: Message) -> str | None:
        if message.sender_id not in self.settings.allowed_senders:
            return None
        try:
            text, attachments = _application_body(message)
            approved_fingerprint = self.settings.approved_fingerprint
            if not attachments and text.strip() and (
                not approved_fingerprint or fingerprint(text) == approved_fingerprint
            ):
                return text
        except (AttributeError, ValueError, TypeError, KeyError):
            pass
        return None

    def _handle(self, message: Message, text: str, digest: str) -> None:
        if not self.window.contains(self.clock()):
            return
        self.state.advance_message(digest, "ready", "processing")
        try:
            report = self.process(Application(message, text, ()))
        except Exception:
            _LOG.error("feishu_message_processing_failed")
            self._reconcile(digest, "processing", self._snapshot(digest))
            return
        if self.snapshot_status is not None:
            self._reconcile(digest, "processing", self._snapshot(digest))
        elif self.window.contains(self.clock()):
            self.state.advance_message(digest, "processing", "sending")
            try:
                self.transport.send_card(result_card(report))
            except Exception:
                self._manual(digest, "sending")
                return
            self.state.advance_message(digest, "sending", "done")

    def recover(self) -> Future:
        """每次连上后回扫当前验收窗口，持久去重与实时事件合流。"""
        return self._history.submit(self._recover, self.clock())

    def _recover(self, end: float) -> None:
        # 以回扫开始时的未完成项为准，避免误接管扫描期间的新消息。
        pending = {row["digest"] for row in self.state.unfinished()}
        try:
            if self.window.contains(self.clock()):
                for message in self.transport.history(self.window.start.timestamp(), end):
                    if not self.window.contains(self.clock()):
                        break
                    if self._in_scope(message) and message.bot_mention_keys:
                        if self._approved_text(message) is not None:
                            pending.discard(hashlib.sha256(message.message_id.encode()).hexdigest())
                        self._receive(message, time.monotonic() + _NOTICE_BUDGET)
        except Exception:
            _LOG.error("feishu_history_failed")

        for digest in pending:
            lease = self.state.message_lease(digest)
            if lease is None:
                continue
            with lease:
                row = self.state.message_record(digest)
                status = row["stage"]
                if status in ("done", "manual_notified", "listed"):
                    continue
                if row["binding"] != self.binding or not self.window.contains(self.clock()):
                    continue
                snapshot = self._snapshot(digest) if status in ("report_saved", "processing") else None
                if snapshot == "report_saved":
                    # 缺少合规原消息，只回读已有确认，未发完的页交由人工核对。
                    snapshot = "manual_pending"
                self._reconcile(digest, status, snapshot)
        with self.state.connection() as db:
            legacy = db.execute("""
                SELECT COUNT(*) FROM feishu_messages WHERE scope = ? AND stage IS NULL AND status = 'done'
            """, (self.state.scope,)).fetchone()[0]
        _LOG.info("feishu_recovery_unfinished count=%d legacy_done=%d", len(self.state.unfinished()), legacy)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._retry_at.clear()
            self._condition.notify()
        if self._retry_thread is not None:
            self._retry_thread.join()
        self._history.shutdown(cancel_futures=True)
        self._receipts.shutdown(cancel_futures=True)
        self._reactions.shutdown(cancel_futures=True)
        self._work.shutdown(cancel_futures=True)
