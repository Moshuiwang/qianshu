"""与群转交共享长连接，审批消息使用独立的凭据检查传输。"""

import asyncio
from html import escape
import json
import re
import time
from uuid import uuid4

from qianshu.config import ConfigError
from qianshu.mail import _fingerprint
from qianshu.mingdao.create_order import Outlet, Stop, decode, private_bytes

from .cards import mention
from .sdk import P2CardActionTriggerResponse, _Transport
from .state import CALLBACK_RESPONSE_MARGIN
from .trial_approval import CARD_FORMAT, journal_at


def checked_transport(settings, private_dir=None):
    guard = Outlet()
    credentials = {"FEISHU_APP_ID": settings.app_id, "FEISHU_APP_SECRET": settings.app_secret,
                   "FEISHU_CHAT_ID": settings.chat_id}
    for i, value in enumerate((*settings.handoff_sensitive_values, *settings.handoff_sensitive_urls)):
        if value not in credentials.values():
            credentials[str(i)] = value
    if private_dir is not None:
        login = private_dir / "mingdao-state.json"
        try:
            data = private_bytes(login)
        except FileNotFoundError:
            pass
        else:
            cookies = decode(data)["cookies"]
            if not isinstance(cookies, list):
                raise ConfigError("无法核对已登记登录会话的凭据出口")
            for index, cookie in enumerate(cookies):
                value = cookie["value"]
                if not isinstance(value, str):
                    raise ConfigError("无法核对已登记登录会话的凭据出口")
                if value:
                    credentials[("cookie", index)] = value
    guard.configure(credentials)
    transport = _Transport(settings, None, time.time, http_check=guard.sdk)
    transport.before_send = guard.sdk
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
    def __init__(self, path, settings, switches, *, clock=time.time):
        self.path, self.settings, self.switches, self.clock = path, settings, switches, clock
        self.origin, self.session, self.connected_at = None, "", 0
        with journal_at(path) as journal:
            expected = _fingerprint(settings.app_id, settings.chat_id, settings.tenant_key or "")
            with journal.db:
                journal.db.execute("INSERT OR IGNORE INTO trial_target VALUES(1,?)", (expected,))
                if journal.db.execute("SELECT fingerprint FROM trial_target").fetchone()[0] != expected:
                    raise ConfigError("试运行审批登记与汇报群不匹配")
        self.rotate(None, "服务重启或关闭")
        self.transport = checked_transport(settings, path.parent)

    def rotate(self, origin, reason):
        self.origin = None
        self.session = str(uuid4()) if origin is not None else ""
        with journal_at(self.path) as j:
            j.rotate(self.session, reason)
        self.connected_at = self.clock()
        self.origin = origin

    def maintain(self):
        with journal_at(self.path) as j:
            if self.switches().get("MAIL_TRIAL_CARD_ENABLED") != "true":
                if j.session():
                    j.rotate("", "审批已关闭")
            else:
                j.scan(self.clock(), create_enabled=self.switches().get("MAIL_TRIAL_CREATE_ENABLED") == "true")
            for row in j.db.execute("""SELECT n.*, c.snapshot FROM trial_notification n JOIN trial_card c
                                       USING(card) WHERE n.status='pending' ORDER BY n.rowid""").fetchall():
                target = json.loads(row["snapshot"]).get("contact")
                if not target:
                    continue
                from qianshu.__main__ import _send_trial_message
                def claim():
                    with j.db:
                        if not j.db.execute("""UPDATE trial_notification SET status='unknown'
                            WHERE uuid=? AND status='pending'""", (row["uuid"],)).rowcount:
                            raise ValueError
                _send_trial_message(self.transport, mention(target, "配置负责人") + " " + row["body"],
                                    row["uuid"], False, claim)
                with j.db:
                    j.db.execute("UPDATE trial_notification SET status='sent' WHERE uuid=?", (row["uuid"],))

    async def record(self, data, origin, deadline):
        deadline -= CALLBACK_RESPONSE_MARGIN
        event, header = data.event, data.header
        session = self.session
        def current():
            return (origin is not None and self.origin is origin and origin.close_code is None
                    and self.session == session and time.monotonic() < deadline)
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
                member = await self.transport.is_member(actor)
        except Exception:
            return response("未能确认点击人是汇报群成员")
        reason = ""
        if value["action"] == "reject":
            form = getattr(event.action, "form_value", None) or {}
            reason = form.get("reason", "")
            if not isinstance(reason, str) or len(reason) > 500:
                return response("请核对驳回原因")
            reason = escape(reason.strip())
        def record():
            with journal_at(self.path) as j:
                return j.action(session, value["card"], value["registration"], value["version"], msg,
                                value["action"], key, actor, times[0], self.clock,
                                lambda: current() and self.clock()-times[0] <= self.settings.callback_max_age,
                                member, switches, reason=reason, claimed=True)
        async with asyncio.timeout(max(0, deadline-time.monotonic())):
            return response(await asyncio.to_thread(record))
