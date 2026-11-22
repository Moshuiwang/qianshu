"""运行飞书群处理服务，或通过同一门禁离线回放一条合成消息。"""

import argparse
import asyncio
import json
import logging
import os
import re
import sqlite3
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import closing, contextmanager, nullcontext
from datetime import date, datetime
from functools import partial
from hashlib import sha256
from pathlib import Path
from threading import Event, Thread, Timer
from uuid import NAMESPACE_OID, uuid5

from dotenv import dotenv_values
from lark_oapi import RequestOption
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from . import mail
from .config import Config, ConfigError, load_config
from .feishu import Entrance, FeishuService, Message, Report, Settings, State, Window, load_settings
from .feishu.cards import CardLine, DetailPage, detail_pages, interrupted_report_card, result_card
from .feishu.entrance import Application, Attachment, Transport, fingerprint
from .feishu.sdk import _Transport
from .feishu.settings import load_handoff_settings
from .logs import setup_logging
from .parse import EMPTY_DRAFTS_STATUS
from .parse._output import check_output_path
from .parse.companies import DEFAULT_COMPANIES_FILE
from .parse.model import configured_model, load_model_config
from .pipeline import Pipeline


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _tag_dates(path: Path | None) -> dict[str, date]:
    if path is None:
        return {}
    return {name: date.fromisoformat(end) for name, end in _json(path).items()}


_REPORT_HANDLED = DetailPage("")
_LOG = logging.getLogger(__name__)
_REPORT_RETRY_SECONDS = 55 * 60
_MAIL_PREFIX = "mail:"
_MAIL_REQUIRED = ("MAIL_HOST", "MAIL_USER", "MAIL_PASSWORD", "MAIL_APPROVER", "MAIL_TEST_MARKER")
_MAIL_EXTRA = ("MAIL_APPROVER", "MAIL_TEST_MARKER", "MAIL_SENT_FOLDER", "MAIL_PRIVATE_DIR")
_MAIL_CONFIG_ERROR = "邮件轮询配置缺失或不可读，请检查 .env。"
_COMPANIES_FILE_ERROR = "公司对照表文件缺失或不可读，请检查 --companies-file。"
_RAW_OUTPUT_DIR_ERROR = "原始回复目录须为可访问的 0700 目录，请检查 --raw-output-dir。"
_SendReport = Callable[[dict, str, bool, Callable[[], None]], None]


class _EntranceTransport:
    """消费处理器已接管的汇报回执，原样转发入口自行生成的异常卡片。"""

    def __init__(self, transport: Transport, resume_reports: Callable[[], None] | None = None):
        self._transport = transport
        self._resume_reports = resume_reports
        self.react = transport.react
        self.reply = transport.reply
        self.download = transport.download

    def history(self, start: float, end: float) -> Iterable[Message]:
        if self._resume_reports is not None:
            try:
                self._resume_reports()
            except Exception:
                _LOG.warning("汇报续发失败，继续回扫。")
        return self._transport.history(start, end)

    def send_card(self, card: dict) -> str:
        if card == result_card(_REPORT_HANDLED):
            return ""
        return self._transport.send_card(card)


def _send_report_card(
    transport: _Transport, card: dict, request_id: str, check_attempt: Callable[[], None],
) -> None:
    body = (
        CreateMessageRequestBody.builder().receive_id(transport.settings.chat_id)
        .msg_type("interactive").content(json.dumps(card, ensure_ascii=False)).uuid(request_id).build()
    )
    request = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
    transport._require_window()
    token = asyncio.run(transport._tenant_token())
    option = RequestOption.builder().tenant_access_token(token).build()
    transport._require_window()
    check_attempt()
    response = transport.im.message.create(request, option)
    if not response.success() or not response.data or not response.data.message_id:
        raise RuntimeError("飞书未确认汇报卡片发送成功。")


class _ReportProcessor:
    def __init__(
        self,
        pipeline: Callable[[Application], tuple[DetailPage, ...]],
        send: _SendReport,
        window: Window,
        state_path: Path,
        *,
        scope: str = "",
        clock: Callable[[], float] = time.time,
        mail_stop: Event | None = None,
        mail_scope: str | None = None,
    ) -> None:
        self.pipeline, self.send, self.window, self.clock = pipeline, send, window, clock
        self.state_path = state_path
        self.mail_stop = mail_stop
        binding = [scope, window.start.isoformat(), window.end.isoformat()]
        self.binding = sha256(json.dumps(binding).encode()).hexdigest()
        self.mail_binding = (
            sha256(json.dumps([*binding, mail_scope]).encode()).hexdigest()
            if mail_scope is not None else None
        )
        if state_path.is_symlink():
            raise ValueError("汇报状态文件须为权限 0600 的私有文件。")
        state_path.touch(mode=0o600, exist_ok=True)
        if not state_path.is_file() or state_path.stat().st_mode & 0o777 != 0o600:
            raise ValueError("汇报状态文件须为权限 0600 的私有文件。")
        with self._write() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS reports (
                    digest TEXT PRIMARY KEY,
                    total_pages INTEGER NOT NULL,
                    sent_pages INTEGER NOT NULL DEFAULT 0,
                    stop_notice_failures INTEGER NOT NULL DEFAULT 0,
                    binding TEXT,
                    expires_at REAL,
                    pages TEXT,
                    notice TEXT,
                    notice_confirmed INTEGER NOT NULL DEFAULT 0,
                    manual_page INTEGER,
                    page_first_attempt REAL,
                    notice_first_attempt REAL
                )
            """)
            columns = {column["name"] for column in db.execute("PRAGMA table_info(reports)")}
            for name, declaration in (
                ("binding", "TEXT"), ("expires_at", "REAL"), ("pages", "TEXT"), ("notice", "TEXT"),
                ("notice_confirmed", "INTEGER NOT NULL DEFAULT 0"), ("manual_page", "INTEGER"),
                ("page_first_attempt", "REAL"), ("notice_first_attempt", "REAL"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE reports ADD COLUMN {name} {declaration}")
            # 更早的记录只有页数。已有快照的旧记录也无法确认 UUID 的首次尝试时刻。
            db.execute("""
                UPDATE reports SET manual_page = sent_pages + 1
                WHERE binding IS NULL AND sent_pages < total_pages AND manual_page IS NULL
            """)
            if "page_first_attempt" not in columns:
                db.execute("""
                    UPDATE reports SET manual_page = sent_pages + 1, pages = NULL, notice = NULL
                    WHERE pages IS NOT NULL
                """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.state_path, autocommit=True)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA secure_delete = ON")
        return db

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")

    @staticmethod
    def _payload(digest: str, suffix: int | str, card: dict) -> tuple[str, dict]:
        return str(uuid5(NAMESPACE_OID, f"qianshu-report:{digest}:{suffix}")), card

    def _active(self, digest: str, now: float) -> bool:
        return self.window.contains(now) and (
            not digest.startswith(_MAIL_PREFIX)
            or (self.mail_stop is not None and not self.mail_stop.is_set())
        )

    def _binding(self, digest: str) -> str | None:
        return self.mail_binding if digest.startswith(_MAIL_PREFIX) else self.binding

    def _try_send(
        self, digest: str, payload: tuple[str, dict], first_attempt: float, *, notice: bool = False,
    ) -> bool:
        request_id, card = payload

        def check_attempt() -> None:
            now = self.clock()
            if not self._active(digest, now) or now - first_attempt > _REPORT_RETRY_SECONDS:
                raise RuntimeError("汇报自动发送期限已结束。")

        try:
            check_attempt()
            self.send(card, request_id, notice, check_attempt)
        except Exception:
            return False
        return True

    def _stop_stale(self, db: sqlite3.Connection, digest: str, report: sqlite3.Row) -> bool:
        now = self.clock()
        first_attempts = [report["page_first_attempt"]]
        if not report["notice_confirmed"]:
            first_attempts.append(report["notice_first_attempt"])
        if not any(attempt is not None and now - attempt > _REPORT_RETRY_SECONDS
                   for attempt in first_attempts):
            return False
        db.execute("""
            UPDATE reports SET manual_page = sent_pages + 1, pages = NULL, notice = NULL
            WHERE digest = ?
        """, (digest,))
        return True

    def _send_notice(self, digest: str) -> None:
        while self.window.contains(self.clock()):
            with self._write() as db:
                report = db.execute("""
                    SELECT notice, notice_confirmed, page_first_attempt, notice_first_attempt FROM reports
                    WHERE digest = ? AND binding = ? AND pages IS NOT NULL AND notice_confirmed = 0
                """, (digest, self.binding)).fetchone()
                if report is None or report["notice"] is None or self._stop_stale(db, digest, report):
                    return
                first_attempt = report["notice_first_attempt"]
                if first_attempt is None:
                    db.execute("UPDATE reports SET notice_first_attempt = ? WHERE digest = ?",
                               (self.clock(), digest))
                    continue
                confirmed = self._try_send(digest, json.loads(report["notice"]), first_attempt, notice=True)
                db.execute("""
                    UPDATE reports SET notice_confirmed = ?, stop_notice_failures = ? WHERE digest = ?
                """, (int(confirmed), int(not confirmed), digest))
                if not confirmed:
                    self._stop_stale(db, digest, report)
                return

    def _deliver(self, digest: str) -> None:
        while self._active(digest, self.clock()):
            with self._write() as db:
                report = db.execute("""
                    SELECT sent_pages, total_pages, pages, notice, notice_confirmed,
                           page_first_attempt, notice_first_attempt FROM reports
                    WHERE digest = ? AND binding = ? AND pages IS NOT NULL
                """, (digest, self._binding(digest))).fetchone()
                if report is None or self._stop_stale(db, digest, report):
                    return
                first_attempt = report["page_first_attempt"]
                if first_attempt is None:
                    # 提交首次尝试时刻，再进入持锁的发送阶段。进程退出后仍可核对期限。
                    db.execute("UPDATE reports SET page_first_attempt = ? WHERE digest = ?",
                               (self.clock(), digest))
                    continue
                number, total = report["sent_pages"] + 1, report["total_pages"]
                payload = json.loads(report["pages"])[number - 1]
                if self._try_send(digest, payload, first_attempt) or self._try_send(digest, payload, first_attempt):
                    db.execute("""
                        UPDATE reports SET sent_pages = ?, page_first_attempt = NULL,
                            pages = CASE WHEN total_pages = ? THEN NULL ELSE pages END,
                            notice = CASE WHEN total_pages = ? THEN NULL ELSE notice END
                        WHERE digest = ?
                    """, (number, number, number, digest))
                    continue
                if self._stop_stale(db, digest, report):
                    return
                if digest.startswith(_MAIL_PREFIX):
                    break
                prepare_notice = report["notice"] is None and self.window.contains(self.clock())
                if prepare_notice:
                    notice = self._payload(digest, "stopped", interrupted_report_card(number, total))
                    db.execute("UPDATE reports SET notice = ? WHERE digest = ?",
                               (json.dumps(notice, ensure_ascii=False), digest))
            # 中断卡也须先提交持久快照，才能发请求。
            if prepare_notice:
                self._send_notice(digest)
            break
        self.expire()

    def expire(self) -> None:
        with self._write() as db:
            db.execute("""
                UPDATE reports SET manual_page = sent_pages + 1, pages = NULL, notice = NULL
                WHERE pages IS NOT NULL AND expires_at <= ?
            """, (self.clock(),))

    def resume(self, *, mail_only: bool = False) -> None:
        self.expire()
        if not self.window.contains(self.clock()):
            return
        group_binding = None if mail_only else self.binding
        with closing(self._connect()) as db:
            pending = db.execute("""
                SELECT digest FROM reports WHERE binding IN (?, ?) AND pages IS NOT NULL ORDER BY rowid
            """, (group_binding, self.mail_binding)).fetchall()
        for report in pending:
            digest = report["digest"]
            is_mail = digest.startswith(_MAIL_PREFIX)
            if is_mail:
                self._deliver(digest)
            elif not mail_only:
                self._send_notice(digest)
                self._deliver(digest)

    def __call__(self, application: Application) -> Report:
        digest = sha256(application.message.message_id.encode()).hexdigest()
        self.store(digest, lambda: self.pipeline(application))
        return _REPORT_HANDLED

    def store(self, digest: str, build_pages: Callable[[], tuple[DetailPage, ...]]) -> None:
        with closing(self._connect()) as db:
            if db.execute("SELECT 1 FROM reports WHERE digest = ?", (digest,)).fetchone():
                return
        pages = [self._payload(digest, number, result_card(page))
                 for number, page in enumerate(build_pages(), 1)]
        if pages:
            with self._write() as db:
                inserted = db.execute("""
                    INSERT OR IGNORE INTO reports (digest, total_pages, binding, expires_at, pages)
                    VALUES (?, ?, ?, ?, ?)
                """, (digest, len(pages), self._binding(digest), self.window.end.timestamp(),
                      json.dumps(pages, ensure_ascii=False))).rowcount
            if inserted:
                self._deliver(digest)


def _processor(
    pipeline: Callable[[Application], tuple[DetailPage, ...]],
    send: _SendReport,
    window: Window,
    state_path: Path,
    *,
    scope: str = "",
    clock: Callable[[], float] = time.time,
    mail_stop: Event | None = None,
    mail_scope: str | None = None,
) -> _ReportProcessor:
    return _ReportProcessor(
        pipeline, send, window, state_path, scope=scope, clock=clock,
        mail_stop=mail_stop, mail_scope=mail_scope,
    )


class _OfflineTransport:
    def react(self, message_id: str) -> None:
        pass

    def reply(self, message_id: str, text: str, *, deadline: float) -> None:
        print(json.dumps({"notice": text}, ensure_ascii=False))

    def send_card(self, card: dict) -> str:
        print(json.dumps(card, ensure_ascii=False))
        return "offline"

    def send_report_card(
        self, card: dict, _request_id: str, _notice: bool, check_attempt: Callable[[], None],
    ) -> None:
        check_attempt()
        self.send_card(card)

    def history(self, start: float, end: float) -> tuple[Message, ...]:
        return ()

    def download(self, message_id: str, attachment: Attachment) -> bytes:
        raise RuntimeError("离线注入不提供附件。")


def _inject(
    args: argparse.Namespace, settings: Settings, window: Window, tag_dates: dict[str, date],
    sensitive_values: tuple[str, ...],
) -> None:
    fixture = _json(args.inject)
    raw = fixture["message"]
    message = Message(**{**raw, "bot_mention_keys": tuple(raw.get("bot_mention_keys", ()))})
    if not isinstance(message.content, str) or any(
        not isinstance(value, str) for value in message.bot_mention_keys
    ):
        raise ValueError("注入消息格式无效。")

    def model(*, system: str, user: str) -> str:
        return fixture["model_reply"]

    transport = _OfflineTransport()
    pipeline = Pipeline(
        model=model, tag_dates=tag_dates, sensitive_values=sensitive_values, companies_file=args.companies_file,
        legacy_companies=args.legacy_companies,
    )
    with tempfile.TemporaryDirectory(prefix="qianshu-inject-") as directory:
        state = State(Path(directory) / "state.sqlite", settings)
        try:
            entrance = Entrance(
                settings, window, state, _EntranceTransport(transport),
                _processor(pipeline, transport.send_report_card, window, Path(directory) / "reports.sqlite"),
            )
            try:
                receipt = entrance.inject_message(message)
                if receipt is not None and (processing := receipt.result()) is not None:
                    processing.result()
            finally:
                entrance.close()
        finally:
            state.close()


def _load_mail_config(config: Config, env_file: Path) -> Config:
    try:
        with env_file.open(encoding="utf-8-sig") as stream:
            extra = dotenv_values(stream=stream, interpolate=False)
    except (OSError, UnicodeError):
        raise ConfigError(_MAIL_CONFIG_ERROR) from None
    values = {
        key: config[key]
        for key in ("MAIL_HOST", "MAIL_PORT", "MAIL_USER", "MAIL_PASSWORD", "MAIL_FOLDER")
        if key in config
    }
    for key in _MAIL_EXTRA:
        if (value := extra.get(key)) and value.strip():
            values[key] = value
    if any(key not in values for key in _MAIL_REQUIRED):
        raise ConfigError(_MAIL_CONFIG_ERROR)
    values.setdefault("MAIL_PORT", "993")
    return Config(values)


def _mail_summary(pages: tuple[DetailPage, ...], approved: bool) -> tuple[DetailPage, ...]:
    # 汇报只允许固定状态和计数。群卡片的字段、摘录和模型输出都留在内存中。
    status = "解析不完整，需人工确认。"
    if pages and len(pages[0].elements) > 1:
        heading = pages[0].elements[1].get("text", {}).get("content", "")
        if heading == "处理状态\n解析完成，校验执行异常。":
            status = "解析完成，校验执行异常。"
        elif heading == f"处理状态\n{EMPTY_DRAFTS_STATUS}":
            status = EMPTY_DRAFTS_STATUS
        elif count := re.fullmatch(r"处理状态\n解析完成，共 ([0-9]+) 条政策。", heading):
            total = int(count[1])
            validated = sum(
                bool(re.fullmatch(r"政策 [0-9]+ · 处理状态\n解析完成，待群卡片同意", text))
                for page in pages for element in page.elements
                if (text := element.get("text", {}).get("content", ""))
            )
            status = f"解析完成，共 {total} 条政策。通过校验 {validated} 条，需人工核对 {total - validated} 条。"
    return detail_pages("邮件", [
        CardLine("邮件审批", "已获审批" if approved else "不确定，交人工"),
        CardLine("处理状态", status),
    ])


@contextmanager
def _mail_worker(
    config: Config, window: Window, stop: Event, process: Callable[[str, bool, str], None],
    on_result: Callable[[mail.PollResult], None],
) -> Iterator[None]:
    end = window.end.timestamp()

    def wait(delay: float) -> bool:
        if stop.wait(min(delay, max(0, end - time.time()))):
            return True
        if not window.contains(time.time()):
            stop.set()
        return stop.is_set()

    def poll() -> None:
        while not wait(0):
            try:
                mail.run_polling(config, process, stop=stop, wait=wait, approval_judge=None, on_result=on_result)
            except Exception as error:
                _LOG.warning("mail_poll_failed count=1 category=%s", type(error).__name__)
                if wait(60):
                    return
            else:
                return

    worker = Thread(target=poll, name="mail-polling", daemon=True)
    deadline = Timer(max(0, end - time.time()), stop.set)
    deadline.daemon = True
    deadline.start()
    try:
        worker.start()
        try:
            yield
        finally:
            stop.set()
            worker.join(timeout=1)
    finally:
        deadline.cancel()


def _check_companies_file(path: Path) -> None:
    try:
        if not path.is_file():
            raise OSError
        path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ConfigError(_COMPANIES_FILE_ERROR) from None


@contextmanager
def _raw_output_directory(path: Path) -> Iterator[int]:
    check_output_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        raise ConfigError(_RAW_OUTPUT_DIR_ERROR) from None
    try:
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
            raise ConfigError(_RAW_OUTPUT_DIR_ERROR)
        yield fd
    finally:
        os.close(fd)


def _save_raw_reply(path: Path, application: Application, reply: str) -> None:
    try:
        with _raw_output_directory(path) as directory:
            digest = fingerprint(application.text)
            timestamp = time.time_ns()
            while True:
                name = f"{timestamp}-{digest}.txt"
                check_output_path(path / name)
                try:
                    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
                    break
                except FileExistsError:
                    timestamp += 1
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as output:
                    os.fchmod(output.fileno(), 0o600)
                    output.write(reply)
            except (OSError, UnicodeError):
                os.unlink(name, dir_fd=directory)
                raise
    except (ConfigError, OSError, UnicodeError):
        _LOG.warning("原始模型回复保存失败。")
        raise


def _serve(
    args: argparse.Namespace, settings: Settings, window: Window, tag_dates: dict[str, date],
    sensitive_values: tuple[str, ...], config: Config, *, mail_config: Config | None = None,
) -> None:
    # Anthropic SDK 支持从进程环境覆盖请求头和默认地址。服务只采用 .env 配置。
    if "ANTHROPIC_CUSTOM_HEADERS" in os.environ or (
        "ANTHROPIC_BASE_URL" in os.environ and "ANTHROPIC_BASE_URL" not in config
    ):
        raise ConfigError("请通过 .env 配置 AI 接口，并移除 SDK 的进程环境覆盖。")
    model = configured_model(load_model_config(env_file=args.env_file, config=config))

    def complete(*, system: str, user: str) -> str:
        if not window.contains(time.time()):
            raise RuntimeError("验收窗口已关闭。")
        return model(system=system, user=user)

    pipeline = Pipeline(
        model=complete, tag_dates=tag_dates, sensitive_values=sensitive_values,
        companies_file=args.companies_file,
        legacy_companies=args.legacy_companies,
        on_reply=partial(_save_raw_reply, args.raw_output_dir) if args.raw_output_dir is not None else None,
    )

    def send(card: dict, request_id: str, _notice: bool, check_attempt: Callable[[], None]) -> None:
        _send_report_card(service.transport, card, request_id, check_attempt)

    if args.state.parent.stat().st_mode & 0o077:
        raise ValueError("汇报状态须保存在私有状态目录中。")
    report_state = args.state.with_name(f"{args.state.name}.report-pages.sqlite")
    scope = json.dumps([
        settings.app_id, settings.tenant_key, settings.chat_id, settings.bot_open_id,
        sorted(settings.allowed_senders), settings.approved_fingerprint,
    ])
    stop = Event() if mail_config is not None else None
    mail_scope = None
    if mail_config is not None:
        mail_scope = json.dumps([
            mail_config["MAIL_HOST"], mail_config["MAIL_USER"], mail_config.get("MAIL_FOLDER") or "INBOX",
            mail_config["MAIL_APPROVER"].strip().casefold(), mail_config["MAIL_TEST_MARKER"],
        ])
    processor = _processor(pipeline, send, window, report_state, scope=scope, mail_stop=stop, mail_scope=mail_scope)
    processor.expire()
    if not window.contains(time.time()):
        raise ValueError("当前不在验收窗口内。")
    service = FeishuService(settings, window, args.state, processor)
    service.entrance.transport = _EntranceTransport(service.transport, processor.resume)

    def process_mail(body: str, approved: bool, fingerprint: str) -> None:
        digest = _MAIL_PREFIX + fingerprint

        def build_pages() -> tuple[DetailPage, ...]:
            now = time.time()
            if not processor._active(digest, now):
                raise RuntimeError("邮件验收窗口已关闭。")
            message = Message(fingerprint, settings.chat_id, "", int(now * 1000), "text", "")
            pages = _mail_summary(pipeline(Application(message, body, ())), approved)
            if not processor._active(digest, time.time()):
                raise RuntimeError("邮件验收窗口已关闭。")
            return pages

        processor.store(digest, build_pages)

    def resume_mail(_result: mail.PollResult) -> None:
        processor.resume(mail_only=True)

    polling = (
        _mail_worker(mail_config, window, stop, process_mail, resume_mail)
        if mail_config is not None and stop is not None else nullcontext()
    )
    try:
        with polling:
            service.run()
    finally:
        processor.expire()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="常驻转交飞书群 @，或在登记的验收窗口内处理飞书群申请。",
        epilog="离线文件为 JSON，message 按飞书入口 Message 字段填写，content 为 JSON 字符串。"
               "model_reply 为合成模型回复字符串。",
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="私有 .env 文件")
    parser.add_argument("--window-start", type=datetime.fromisoformat, help="验收窗口开始时刻（含时区）")
    parser.add_argument("--window-end", type=datetime.fromisoformat, help="验收窗口结束时刻（含时区）")
    parser.add_argument("--state", type=Path, help="私有目录内的常驻服务 SQLite 状态文件")
    parser.add_argument("--inject", type=Path, help="离线注入文件，卡片以 JSON 行输出")
    parser.add_argument("--mail", action="store_true", help="在验收窗口内启用只读邮件轮询")
    parser.add_argument("--tag-dates", type=Path, help="私有 JSON 对象，标签名称映射到已确认的结束日期")
    parser.add_argument("--companies-file", type=Path,
                        help="私有公司对照表 CSV 或 TSV 路径，默认 .private/companies.tsv。")
    parser.add_argument("--legacy-companies", action="store_true",
                        help="按无表头两列或三列 TSV 读取公司对照表，默认关闭。")
    parser.add_argument("--raw-output-dir", type=Path,
                        help="常驻服务保存原始模型回复的现有 0700 目录。Git 工作区内须使用忽略目录。")
    parser.add_argument("--handoff-only", action="store_true", help="只把飞书群 @ 转交守望者。")
    parser.add_argument("--business-env-file", type=Path, help="仅转交监听器用于出站比对的业务私有配置。")
    parser.add_argument("--mingdao-config", type=Path, help="仅转交监听器用于定位官方接口令牌键名的明道云 JSON 配置。")
    args = parser.parse_args(argv)
    if args.handoff_only:
        if any((args.window_start, args.window_end, args.state, args.inject, args.mail,
                args.tag_dates, args.companies_file, args.legacy_companies, args.raw_output_dir)):
            parser.error("--handoff-only 只与 --env-file、--business-env-file、--mingdao-config 一起使用。")
        try:
            from .feishu.handoff import HandoffService, MentionLog

            settings, path = load_handoff_settings(
                args.env_file, for_listener=True, business_env_file=args.business_env_file,
                mingdao_config=args.mingdao_config,
            )
            setup_logging({"FEISHU_APP_SECRET": settings.app_secret})
            with closing(MentionLog(path, settings, create=True)) as store:
                HandoffService(store).run()
        except ConfigError as error:
            print(error, file=sys.stderr)
            return 1
        except Exception:
            print("飞书 @ 转交服务启动或运行失败，请核对私有转交日志和长连接配置。", file=sys.stderr)
            return 1
        return 0
    if args.business_env_file is not None:
        parser.error("--business-env-file 仅用于 --handoff-only。")
    if args.mingdao_config is not None:
        parser.error("--mingdao-config 仅用于 --handoff-only。")
    if args.window_start is None or args.window_end is None:
        parser.error("群申请入口须指定 --window-start 和 --window-end。")
    if args.inject is None and args.state is None:
        parser.error("常驻服务须指定 --state。")
    if args.mail and args.inject is not None:
        parser.error("--mail 仅用于常驻服务。")
    if args.raw_output_dir is not None and args.inject is not None:
        parser.error("--raw-output-dir 仅用于常驻服务。")

    try:
        if args.inject is None:
            if args.companies_file is None:
                args.companies_file = DEFAULT_COMPANIES_FILE
            _check_companies_file(args.companies_file)
            if args.raw_output_dir is not None:
                with _raw_output_directory(args.raw_output_dir):
                    pass
        config = load_config(env_file=args.env_file)
        mail_config = _load_mail_config(config, args.env_file) if args.mail else None
        setup_logging(config if mail_config is None else {**config, **mail_config})
        settings = load_settings(args.env_file)
        window = Window(args.window_start, args.window_end)
        tag_dates = _tag_dates(args.tag_dates)
        sensitive_values = tuple(
            config[key] for key in ("FEISHU_APP_SECRET", "ANTHROPIC_API_KEY", "MINGDAO_PASSWORD",
                                    "MINGDAO_ACCOUNT", "MAIL_PASSWORD", "MAIL_USER") if key in config
        )
        if args.inject is not None:
            _inject(args, settings, window, tag_dates, sensitive_values)
        else:
            _serve(args, settings, window, tag_dates, sensitive_values, config, mail_config=mail_config)
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 1
    except Exception:
        print("群申请入口运行失败，请核对配置、验收窗口与输入。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
