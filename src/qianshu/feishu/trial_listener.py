"""与群转交共享长连接，审批消息使用独立的凭据检查传输。"""

import asyncio
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from copy import copy
from html import escape
import json
import logging
import re
import time
from threading import Lock
from uuid import uuid4

from qianshu.config import ConfigError
from qianshu.mail import _fingerprint
from qianshu.mingdao.create_order import CredentialConflict, Outlet, OutletConflict, OutletUnavailable, Stop, decode, private_bytes

from .cards import mention
from .sdk import P2CardActionTriggerResponse, _Transport
from .state import CALLBACK_RESPONSE_MARGIN
from .trial_approval import CARD_FORMAT, journal_at


_LOG = logging.getLogger(__name__)


class MessageNotSent(RuntimeError):
    """消息尚未交接给 HTTP 传输。"""


def checked_transport(settings, private_dir=None):
    guard = Outlet()
    credentials = {"FEISHU_APP_ID": settings.app_id, "FEISHU_APP_SECRET": settings.app_secret,
                   "FEISHU_CHAT_ID": settings.chat_id}
    for i, value in enumerate((*settings.handoff_sensitive_values, *settings.handoff_sensitive_urls)):
        if value not in credentials.values():
            credentials[str(i)] = value
    for index, (key, value) in enumerate(settings.trial_credentials):
        if credentials.get(key) != value:
            credentials[("setting", index)] = value
    complete = settings.trial_credentials_complete
    for directory in dict.fromkeys((private_dir, settings.trial_login_dir)):
        if directory is None:
            continue
        try:
            cookies = decode(private_bytes(directory / "mingdao-state.json"))["cookies"]
            if not isinstance(cookies, list):
                raise ValueError
            for index, cookie in enumerate(cookies):
                value = cookie["value"]
                if not isinstance(value, str):
                    raise ValueError
                if value.strip():
                    credentials[("cookie", directory, index)] = value
        except FileNotFoundError:
            if directory == settings.trial_login_dir:
                complete = False
        except (OSError, Stop, ValueError, KeyError, TypeError, RecursionError):
            complete = False
    guard.configure(credentials)
    lock = Lock()
    current = ContextVar("trial_outlet")

    claim_message = ContextVar("trial_message_claim", default=None)

    @contextmanager
    def action(claim=None):
        claim_token = claim_message.set(claim)
        # 复制动作状态，共享只追加的凭据和片段登记表。
        token = current.set(copy(guard))
        try:
            yield
        finally:
            current.reset(token)
            claim_message.reset(claim_token)

    def check(outlet, value):
        if not complete:
            raise OutletUnavailable("试运行凭据来源无法核对")
        with lock:
            checker = current.get()
            checker.sdk(outlet, value)
            if outlet == "message":
                check_content(decode(value["content"]))
            elif outlet == "http" and value[1] == "message":
                check_content(decode(decode(value[0].content)["content"]))
        if outlet == "http" and value[1] == "message":
            claim = claim_message.get()
            if claim is not None:
                claim()

    def check_content(value):
        if isinstance(value, dict):
            for item in value.values():
                check_content(item)
        elif isinstance(value, list):
            for item in value:
                check_content(item)
        elif isinstance(value, str):
            if any(secret and secret in value for raw in guard.credentials.values()
                   for secret in (raw, raw.strip())):
                raise OutletConflict()

    def check_reason(reason):
        with lock:
            check_content(reason)
            copy(guard).check(reason)

    transport = _Transport(settings, None, time.time, http_check=check)
    transport.trial_action = action
    transport.before_send = check
    transport.check_reason = check_reason
    transport.sensitive_values = tuple(dict.fromkeys(
        form for value in credentials.values() for form in (value, value.strip())
    ))
    return transport


def approval_setup(path, switches):
    try:
        config = decode(private_bytes(path)) if path else {}
        if not isinstance(config, dict) or not isinstance(config.get("configurator", {}), dict):
            raise ValueError
    except (OSError, Stop, ValueError):
        config = {}
    contact = config.get("configurator", {}).get("id", "")
    if not isinstance(contact, str) or not re.fullmatch(r"ou_[a-zA-Z0-9]+", contact):
        contact = ""
    snapshot = config.get("order_snapshot", {})
    return switches(), snapshot if isinstance(snapshot, dict) else {}, contact


class Listener:
    def __init__(self, path, settings, switches, *, clock=time.time, defer_rotation=False, transport=None):
        self.path, self.settings, self.switches, self.clock = path, settings, switches, clock
        self.origin, self.session, self.connected_at = None, "", 0
        self._rotation_lock = Lock()
        self._pending = None
        self.current_connection = lambda origin: True
        self._check_store = defer_rotation
        self.transport = transport if transport is not None else checked_transport(settings, path.parent)
        self.request_rotation(None, "服务重启或关闭")
        if not defer_rotation:
            self._apply_rotation()

    def request_rotation(self, origin, reason):
        with self._rotation_lock:
            self.origin = None
            self._pending = (origin, reason, str(uuid4()) if origin is not None else "")

    def rotate(self, origin, reason):
        self.request_rotation(origin, reason)
        self._apply_rotation()

    def _apply_rotation(self):
        with self._rotation_lock:
            pending = self._pending
        if pending is None:
            return
        if self._check_store:
            from qianshu.mail.trial_state import Store
            Store(self.path).db.close()
            self._check_store = False
        origin, reason, session = pending
        with journal_at(self.path) as journal:
            expected = _fingerprint(self.settings.app_id, self.settings.chat_id, self.settings.tenant_key or "")
            with journal.db:
                journal.db.execute("INSERT OR IGNORE INTO trial_target VALUES(1,?)", (expected,))
                if journal.db.execute("SELECT fingerprint FROM trial_target").fetchone()[0] != expected:
                    raise ConfigError("试运行审批登记与汇报群不匹配")
            journal.rotate(session, reason)
        with self._rotation_lock:
            if self._pending is pending:
                self.session, self.connected_at, self.origin = session, self.clock(), origin
                self._pending = None

    def maintain(self):
        self._apply_rotation()
        with journal_at(self.path) as j:
            if self.switches().get("MAIL_TRIAL_CARD_ENABLED") != "true":
                if j.session():
                    j.rotate("", "审批已关闭")
            else:
                j.scan(self.clock(), create_enabled=self.switches().get("MAIL_TRIAL_CREATE_ENABLED") == "true")
            for row in j.db.execute("""SELECT n.*, c.snapshot FROM trial_notification n JOIN trial_card c
                                       USING(card) WHERE n.status='pending' ORDER BY n.rowid""").fetchall():
                target = json.loads(row["snapshot"]).get("contact")

                from qianshu.__main__ import _send_trial_message
                def claim():
                    with j.db:
                        if not j.db.execute("""UPDATE trial_notification SET status='unknown'
                            WHERE uuid=? AND status='pending'""", (row["uuid"],)).rowcount:
                            raise ValueError
                body = (mention(target, "配置负责人") + " " if target else "") + row["body"]
                try:
                    _send_trial_message(self.transport, body, row["uuid"], False, claim)
                except CredentialConflict:
                    _LOG.error("E-TRIAL-MANUAL")
                    with j.db:
                        j.db.execute("UPDATE trial_notification SET status='blocked' WHERE uuid=?", (row["uuid"],))
                        if row["kind"] != "manual":
                            j.db.execute("""INSERT OR IGNORE INTO trial_notification VALUES(
                                ?, 'manual', ?, '本批群通知被拦截，未建单，转人工核对私有登记。', 'pending')""",
                                         (row["card"], str(uuid4())))
                    continue
                with j.db:
                    j.db.execute("UPDATE trial_notification SET status='sent' WHERE uuid=?", (row["uuid"],))

    async def record(self, data, origin, deadline):
        deadline -= CALLBACK_RESPONSE_MARGIN
        event, header = data.event, data.header
        session = self.session
        def current():
            return (origin is not None and self.origin is origin and origin.close_code is None
                    and self.current_connection(origin) and self.session == session and time.monotonic() < deadline)
        def response(message):
            return P2CardActionTriggerResponse({"toast": {"type": "info", "content": message}})
        if (not current() or header.event_type != "card.action.trigger" or not event or not event.context
                or event.context.open_chat_id != self.settings.chat_id or not event.action or not event.operator):
            return response("审批来源不符")
        value = event.action.value
        actor, msg, key = event.operator.open_id, event.context.open_message_id, header.event_id
        if (not isinstance(value, dict) or set(value) != {"format", "registration", "version", "card", "action"}
                or type(value.get("format")) is not int or value["format"] != CARD_FORMAT
                or any(type(value.get(k)) is not int for k in ("registration", "version"))
                or not all(isinstance(v, str) and v.strip() for v in (actor, msg, key, value.get("card")))
                or value.get("action") not in ("verify", "approve", "reject")):
            return response("审批卡格式无效")
        now = self.clock()
        raw = str(header.create_time)
        if not re.fullmatch(r"\d{1,18}", raw, re.ASCII):
            return response("回调时间不符")
        times = [int(raw)/unit for unit in (1, 1000, 1000000)]
        times = [t for t in times if max(self.connected_at, now-self.settings.callback_max_age) <= t <= now]
        if len(times) != 1:
            return response("回调时间不符")
        arrival = self.switches()

        def claim_event():
            with journal_at(self.path) as journal:
                return current() and journal.claim_event(key)

        async with asyncio.timeout(max(0, deadline-time.monotonic())):
            if not await asyncio.to_thread(claim_event):
                return response("该回调已处理")
        if actor not in arrival.get("MAIL_TRIAL_APPROVER_OPEN_IDS", "").split(","):
            return response("点击人名单不符")

        def switches():
            latest = dict(self.switches())
            if arrival.get("MAIL_TRIAL_CREATE_ENABLED") != "true":
                latest["MAIL_TRIAL_CREATE_ENABLED"] = "false"
            if (arrival.get("MAIL_TRIAL_CARD_ENABLED") != "true"
                    or arrival.get("MAIL_TRIAL_CREATE_COUNTRY") != latest.get("MAIL_TRIAL_CREATE_COUNTRY")):
                latest["MAIL_TRIAL_CARD_ENABLED"] = "false"
            return latest
        try:
            async with asyncio.timeout(max(0, deadline-time.monotonic())):
                with getattr(self.transport, "trial_action", nullcontext)():
                    member = await self.transport.is_member(actor)
        except Exception:
            return response("未能确认点击人是汇报群成员")
        reason = ""
        if value["action"] == "reject":
            form = getattr(event.action, "form_value", None) or {}
            reason = form.get("reason", "")
            if not isinstance(reason, str) or len(reason) > 500:
                return response("请核对驳回原因")
            try:
                self.transport.check_reason(reason)
            except CredentialConflict:
                _LOG.warning("E-CRED-STOP")
                reason = "原因已拦截，请人工核对"
            reason = escape(reason.strip())
        def record():
            with journal_at(self.path) as j:
                return j.action(session, value["card"], value["registration"], value["version"], msg,
                                value["action"], key, actor, times[0], self.clock,
                                lambda: current() and self.clock()-times[0] <= self.settings.callback_max_age,
                                member, switches, reason=reason, claimed=True)
        async with asyncio.timeout(max(0, deadline-time.monotonic())):
            return response(await asyncio.to_thread(record))
