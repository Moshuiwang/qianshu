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
from threading import Condition, Thread
from typing import Protocol

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


class Entrance:
    """注入口与飞书收件共用过滤、去重及回执流程。"""

    def __init__(
        self, settings: Settings, window: Window, state: State, transport: Transport,
        process: Callable[[Application], Report], *, clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self.window = window
        self.state = state
        self.transport = transport
        self.process = process
        self.clock = clock
        self._pending = set()
        self._condition = Condition()
        self._retry_at: dict[str, float] = {}
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
        try:
            if not self.window.contains(self.clock()):
                return None
            status = self.state.message_status(message_id)
            if status == "done":
                return None
            text = self._approved_text(message) if status is None else None
            if text is None:
                if status is None and not self.state.claim_message(message_id, "notice_pending"):
                    return None
                retry = not self._send_notice(message_id, deadline)
                return None

            work = self._dispatch_after_reaction(message, text)
        except Exception:
            _LOG.error("feishu_receipt_failed")
        finally:
            if work is None:
                self._release(message_id, retry=retry)
        return work

    def _claim_pending(self, message_id: str) -> bool:
        with self._condition:
            if self._closed or message_id in self._pending:
                return False
            self._pending.add(message_id)
            return True

    def _dispatch_after_reaction(self, message: Message, text: str) -> Future:
        finished = Future()

        def finish() -> None:
            self._release(message.message_id)
            finished.set_result(None)

        def dispatch(reaction: Future) -> None:
            if not reaction.cancelled() and reaction.exception() is None:
                work = self._work.submit(self._handle_once, message, text)
                work.add_done_callback(lambda _: finish())
                return
            if not reaction.cancelled():
                _LOG.error("feishu_receipt_failed")
            finish()

        finished.set_running_or_notify_cancel()
        reaction = self._reactions.submit(self.transport.react, message.message_id)
        reaction.add_done_callback(dispatch)
        return finished

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

    def _release(self, message_id: str, *, retry: bool = False) -> None:
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

    def _retry_notice(self, message_id: str, deadline: float) -> None:
        retry = False
        try:
            if self.window.contains(self.clock()) and self.state.message_status(message_id) == "notice_pending":
                retry = not self._send_notice(message_id, deadline)
        except Exception:
            _LOG.error("feishu_receipt_failed")
            retry = True
        finally:
            self._release(message_id, retry=retry)

    def _handle_once(self, message: Message, text: str) -> None:
        try:
            if self.window.contains(self.clock()) and self.state.claim_message(message.message_id):
                self._handle(message, text)
        except Exception:
            _LOG.error("feishu_message_processing_failed")

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

    def _handle(self, message: Message, text: str) -> None:
        try:
            if not self.window.contains(self.clock()):
                return
            report = self.process(Application(message, text, ()))
        except Exception:
            _LOG.error("feishu_message_processing_failed")
            report = Report("飞书群申请", failures=("处理未完成，请核对处理状态。",))
        if self.window.contains(self.clock()):
            self.transport.send_card(result_card(report))

    def recover(self) -> Future:
        """每次连上后回扫当前验收窗口，持久去重与实时事件合流。"""
        return self._history.submit(self._recover, self.clock())

    def _recover(self, end: float) -> None:
        try:
            if self.window.contains(self.clock()):
                for message in self.transport.history(self.window.start.timestamp(), end):
                    if not self.window.contains(self.clock()):
                        break
                    if self._in_scope(message) and message.bot_mention_keys:
                        self._receive(message, time.monotonic() + _NOTICE_BUDGET)
        except Exception:
            _LOG.error("feishu_history_failed")

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
