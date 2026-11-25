"""审批租约持有者的收信、回调与恢复。此模块只构造 HTTP transport。"""

import asyncio
import time
from contextlib import contextmanager
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from dotenv import dotenv_values

from qianshu.config import registered_credentials, trial_config
from qianshu.mail import _fingerprint
from qianshu.mail.trial import mail_worker
from qianshu.mingdao.create_order import decode, private_bytes

from .sdk import lark
from .settings import load_handoff_settings
from .trial_approval import CARD_FORMAT, Journal, journal_at
from .trial_archive import archive_worker, build_runner
from .trial_listener import ApprovalSettings, Listener
from .trial_order import build_order_runner


def load_approval_settings(args):
    base, path = load_handoff_settings(args.env_file, for_listener=True,
                                       business_env_file=args.business_env_file, mingdao_config=args.mingdao_config)
    credentials = []
    for source in dict.fromkeys((args.env_file, args.business_env_file)):
        if source:
            with source.open(encoding="utf-8-sig") as stream:
                credentials.extend(registered_credentials(dotenv_values(stream=stream, interpolate=False)).items())
    complete = args.business_env_file is not None
    if args.mingdao_config is not None:
        official = decode(private_bytes(args.mingdao_config))["official"]
        keys = dict(credentials)
        complete = complete and all(isinstance(official.get(field), str) and keys.get(official[field])
                                    for field in ("app_id_key", "token_key"))
    settings = ApprovalSettings(**{field.name: getattr(base, field.name) for field in fields(base)},
                                trial_credentials=tuple(credentials),
                                trial_credentials_complete=bool(complete),
                                trial_login_dir=args.mingdao_config.parent if args.mingdao_config else None)
    return settings, path


def switch_source(path):
    before = path.lstat()
    content = private_bytes(path)
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_ctime_ns):
        raise ValueError("试运行开关在读取期间变更")
    return _fingerprint(str(path.absolute()), str(after.st_dev), str(after.st_ino), str(after.st_ctime_ns), content)


def publish_switches(path, state):
    """守望者在确认后发布现有文件的精确修订。外部编辑不会自动发布授权。"""
    from qianshu.mail.trial_state import Store
    from qianshu.parse._output import check_output_path

    check_output_path(state)
    source = switch_source(path)
    config = trial_config(path, approval=True)
    enabled = config.get("MAIL_TRIAL_CARD_ENABLED") == "true"
    if not state.exists():
        if state.with_suffix(".dirty").exists():
            raise ValueError("收信登记缺失，须人工对账")
        if not enabled:
            return
    store = Store(state)
    try:
        with store.write():
            j = Journal(store.db)
            if not enabled and not store.db.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name='trial_switch_epoch'").fetchone():
                return
            j.initialize()
            if switch_source(path) != source:
                raise ValueError("试运行开关在发布期间变更")
            now = time.time()
            store.db.execute("UPDATE trial_switch_epoch SET closed=? WHERE closed IS NULL", (now,))
            store.db.execute("INSERT INTO trial_switch_epoch VALUES(?,?,?,NULL,?)",
                             (str(uuid4()), source, now, int(enabled)))
    finally:
        store.db.close()


class Consumer:
    def __init__(self, args, path, settings, connected):
        self.args = args
        self.path = path
        self.connected = connected
        self.initial = switch_source(args.trial_switches)
        self.epoch_loaded = False
        self.epoch = None
        self.create_enabled = self.switches().get("MAIL_TRIAL_CREATE_ENABLED") == "true"
        self.origin = SimpleNamespace(close_code=None)
        self.listener = Listener(path.with_name("trial-050.sqlite3"), settings, self.switches, defer_rotation=True)
        self.listener.queue_updates = True
        self.listener.current_connection = lambda origin: (origin is self.origin and self.connected()
                and switch_source(args.trial_switches) == self.initial)
        self.listener.request_rotation(self.origin, "连接代次变更")
        self.dispatcher = lark.EventDispatcherHandler.builder("", "").register_p2_card_action_trigger(
            self.record).build()
        self.deadline = self.next_scan = 0.0
        self.ended = None
        self.manual_checks = self.pending_notices = 0

    def switches(self):
        values = trial_config(self.args.trial_switches, approval=True)
        if (not self.connected() or switch_source(self.args.trial_switches) != self.initial
                or values.get("MAIL_TRIAL_CARD_ENABLED") != "true"):
            raise ValueError("E-TRIAL-DISABLED")
        return values

    def tick(self):
        if self.ended is not None and self.ended.is_set():
            raise RuntimeError("E-TRIAL-DISABLED")
        if time.monotonic() >= self.next_scan:
            self.switches()
            self.listener.maintain()
            with journal_at(self.listener.path) as j, j.db:
                self.manual_checks, self.pending_notices = j.notice_counts()
                epoch = j.active_epoch(self.initial)
                if self.epoch_loaded and epoch != self.epoch:
                    raise ValueError("E-TRIAL-DISABLED")
                if not self.epoch_loaded and epoch is not None:
                    j.db.execute("INSERT INTO trial_session_grant VALUES(?,?)", (self.listener.session, epoch))
                self.epoch, self.epoch_loaded = epoch, True
            self.next_scan = time.monotonic() + 0.5

    async def record(self, data):
        settings = self.listener.settings
        event = data.event
        context = getattr(event, "context", None)
        if (not context or data.header.app_id != settings.app_id
                or (settings.tenant_key and data.header.tenant_key != settings.tenant_key)
                or context.open_chat_id != settings.chat_id or not context.open_message_id
                or not event.operator or not event.operator.open_id or not data.header.event_id):
            return
        value = getattr(event.action, "value", None)
        if (not isinstance(value, dict) or type(value.get("format")) is not int
                or value["format"] != CARD_FORMAT or value.get("action") not in ("verify", "approve", "reject")):
            return
        if value["action"] == "approve" and not self.create_enabled:
            self.listener.refuse_click(data, notify=False)
            return
        if self.epoch is not None:
            await self.listener.record(data, self.origin, self.deadline)
            if await self.listener.update_result(data):
                return
        self.listener.refuse_click(data)

    def consume(self, payload, deadline):
        self.deadline = deadline
        asyncio.run(self.dispatcher._do_without_validation(payload))


@contextmanager
def running(arguments, connected):
    env, business, config, switches, expected = arguments
    args = SimpleNamespace(env_file=Path(env), business_env_file=Path(business) if business else None,
                           mingdao_config=Path(config) if config else None, trial_switches=Path(switches))
    settings, path = load_approval_settings(args)
    if _fingerprint(settings.app_id, settings.tenant_key or "", settings.chat_id, str(path.absolute())) != expected:
        raise ValueError("E-TRIAL-DISABLED")
    consumer = Consumer(args, path, settings, connected)
    try:
        state = consumer.listener.path
        if state.with_suffix(".dirty").exists() and not state.exists():
            raise ValueError("E-TRIAL-DISABLED")
        consumer.tick()
        with mail_worker(args, settings, path, consumer.switches,
                         available=lambda: consumer.origin.close_code is None,
                         sender=consumer.listener.transport) as ended:
            consumer.ended = ended
            # 出卡与建单开关均开、配置已确认才装配存档执行器；否则同意后只记录，存档写为 0。
            runner = build_runner(args, settings, consumer.listener.path, path, switches=consumer.switches,
                                  connected=connected, sensitive=consumer.listener.transport.sensitive_values)
            consumer.listener.archive_wired = runner is not None
            # 建单执行器（2d）只在存档执行器已装配、明道云配置与登录态就绪时装配；否则建单写为 0。
            orders = build_order_runner(args, settings, consumer.listener.path, switches=consumer.switches,
                                        connected=connected, sensitive=consumer.listener.transport.sensitive_values,
                                        archive_runner=runner)
            with archive_worker(runner, orders=orders):
                yield consumer
    finally:
        consumer.origin.close_code = 1
        consumer.listener.rotate(None, "连接断开")
