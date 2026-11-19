"""运行飞书群处理服务，或通过同一门禁离线回放一条合成消息。"""

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import closing, contextmanager
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_OID, uuid5

from lark_oapi import RequestOption
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from .config import Config, ConfigError, load_config
from .feishu import Entrance, FeishuService, Message, Report, Settings, State, Window, load_settings
from .feishu.cards import DetailPage, interrupted_report_card, result_card
from .feishu.entrance import Application, Attachment, Transport
from .feishu.sdk import _Transport
from .logs import setup_logging
from .parse.model import configured_model
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
    ) -> None:
        self.pipeline, self.send, self.window, self.clock = pipeline, send, window, clock
        self.state_path = state_path
        binding = [scope, window.start.isoformat(), window.end.isoformat()]
        self.binding = sha256(json.dumps(binding).encode()).hexdigest()
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

    def _try_send(self, payload: tuple[str, dict], first_attempt: float, *, notice: bool = False) -> bool:
        request_id, card = payload

        def check_attempt() -> None:
            now = self.clock()
            if not self.window.contains(now) or now - first_attempt > _REPORT_RETRY_SECONDS:
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
                confirmed = self._try_send(json.loads(report["notice"]), first_attempt, notice=True)
                db.execute("""
                    UPDATE reports SET notice_confirmed = ?, stop_notice_failures = ? WHERE digest = ?
                """, (int(confirmed), int(not confirmed), digest))
                if not confirmed:
                    self._stop_stale(db, digest, report)
                return

    def _deliver(self, digest: str) -> None:
        while self.window.contains(self.clock()):
            with self._write() as db:
                report = db.execute("""
                    SELECT sent_pages, total_pages, pages, notice, notice_confirmed,
                           page_first_attempt, notice_first_attempt FROM reports
                    WHERE digest = ? AND binding = ? AND pages IS NOT NULL
                """, (digest, self.binding)).fetchone()
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
                if self._try_send(payload, first_attempt) or self._try_send(payload, first_attempt):
                    db.execute("""
                        UPDATE reports SET sent_pages = ?, page_first_attempt = NULL,
                            pages = CASE WHEN total_pages = ? THEN NULL ELSE pages END,
                            notice = CASE WHEN total_pages = ? THEN NULL ELSE notice END
                        WHERE digest = ?
                    """, (number, number, number, digest))
                    continue
                if self._stop_stale(db, digest, report):
                    return
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

    def resume(self) -> None:
        self.expire()
        if not self.window.contains(self.clock()):
            return
        with closing(self._connect()) as db:
            pending = db.execute("""
                SELECT digest FROM reports WHERE binding = ? AND pages IS NOT NULL ORDER BY rowid
            """, (self.binding,)).fetchall()
        for report in pending:
            self._send_notice(report["digest"])
            self._deliver(report["digest"])

    def __call__(self, application: Application) -> Report:
        digest = sha256(application.message.message_id.encode()).hexdigest()
        with closing(self._connect()) as db:
            if db.execute("SELECT 1 FROM reports WHERE digest = ?", (digest,)).fetchone():
                return _REPORT_HANDLED
        pages = [self._payload(digest, number, result_card(page))
                 for number, page in enumerate(self.pipeline(application), 1)]
        if pages:
            with self._write() as db:
                inserted = db.execute("""
                    INSERT OR IGNORE INTO reports (digest, total_pages, binding, expires_at, pages)
                    VALUES (?, ?, ?, ?, ?)
                """, (digest, len(pages), self.binding, self.window.end.timestamp(),
                      json.dumps(pages, ensure_ascii=False))).rowcount
            if inserted:
                self._deliver(digest)
        return _REPORT_HANDLED


def _processor(
    pipeline: Callable[[Application], tuple[DetailPage, ...]],
    send: _SendReport,
    window: Window,
    state_path: Path,
    *,
    scope: str = "",
    clock: Callable[[], float] = time.time,
) -> _ReportProcessor:
    return _ReportProcessor(pipeline, send, window, state_path, scope=scope, clock=clock)


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
    pipeline = Pipeline(model=model, tag_dates=tag_dates, sensitive_values=sensitive_values)
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


def _serve(
    args: argparse.Namespace, settings: Settings, window: Window, tag_dates: dict[str, date],
    sensitive_values: tuple[str, ...], config: Config,
) -> None:
    # Anthropic SDK 支持从进程环境覆盖请求头和默认地址。服务只采用 .env 配置。
    if "ANTHROPIC_CUSTOM_HEADERS" in os.environ or (
        "ANTHROPIC_BASE_URL" in os.environ and "ANTHROPIC_BASE_URL" not in config
    ):
        raise ConfigError("请通过 .env 配置 AI 接口，并移除 SDK 的进程环境覆盖。")
    model = configured_model(config)

    def complete(*, system: str, user: str) -> str:
        if not window.contains(time.time()):
            raise RuntimeError("验收窗口已关闭。")
        return model(system=system, user=user)

    pipeline = Pipeline(model=complete, tag_dates=tag_dates, sensitive_values=sensitive_values)

    def send(card: dict, request_id: str, _notice: bool, check_attempt: Callable[[], None]) -> None:
        _send_report_card(service.transport, card, request_id, check_attempt)

    if args.state.parent.stat().st_mode & 0o077:
        raise ValueError("汇报状态须保存在私有状态目录中。")
    report_state = args.state.with_name(f"{args.state.name}.report-pages.sqlite")
    scope = json.dumps([
        settings.app_id, settings.tenant_key, settings.chat_id, settings.bot_open_id,
        sorted(settings.allowed_senders), settings.approved_fingerprint,
    ])
    processor = _processor(pipeline, send, window, report_state, scope=scope)
    processor.expire()
    if not window.contains(time.time()):
        raise ValueError("当前不在验收窗口内。")
    service = FeishuService(settings, window, args.state, processor)
    service.entrance.transport = _EntranceTransport(service.transport, processor.resume)
    try:
        service.run()
    finally:
        processor.expire()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="在登记的验收窗口内处理飞书群申请。",
        epilog="离线文件为 JSON，message 按飞书入口 Message 字段填写，content 为 JSON 字符串。"
               "model_reply 为合成模型回复字符串。",
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="私有 .env 文件")
    parser.add_argument("--window-start", type=datetime.fromisoformat, required=True, help="验收窗口开始时刻（含时区）")
    parser.add_argument("--window-end", type=datetime.fromisoformat, required=True, help="验收窗口结束时刻（含时区）")
    parser.add_argument("--state", type=Path, help="私有目录内的常驻服务 SQLite 状态文件")
    parser.add_argument("--inject", type=Path, help="离线注入文件，卡片以 JSON 行输出")
    parser.add_argument("--tag-dates", type=Path, help="私有 JSON 对象，标签名称映射到已确认的结束日期")
    args = parser.parse_args(argv)
    if args.inject is None and args.state is None:
        parser.error("常驻服务须指定 --state。")

    try:
        config = load_config(env_file=args.env_file)
        setup_logging(config)
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
            _serve(args, settings, window, tag_dates, sensitive_values, config)
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 1
    except Exception:
        print("群申请入口运行失败，请核对配置、验收窗口与输入。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
