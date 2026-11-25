"""试运行卡片回调、通知及凭据出口检查。"""

import asyncio
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from copy import copy
from dataclasses import dataclass
from pathlib import Path
from html import escape
import json
import logging
import re
import time
from threading import Lock
from uuid import uuid4

from lark_oapi import RequestOption
from lark_oapi.api.im.v1 import (CreateMessageRequest, CreateMessageRequestBody, PatchMessageRequest,
                                     PatchMessageRequestBody, PatchMessageResponse)

from qianshu.config import ConfigError
from qianshu.mail import _fingerprint
from qianshu.mingdao.create_order import CredentialConflict, Outlet, OutletConflict, Stop, decode, private_bytes

from .cards import mention
from .sdk import P2CardActionTriggerResponse, _Transport
from .settings import Settings
from .state import CALLBACK_RESPONSE_MARGIN
from .trial_approval import CARD_FORMAT, MANUAL_NOTICE, journal_at
from .trial_order import record_handoff, unbuilt_notice


_LOG = logging.getLogger(__name__)
ARCHIVED_TEXT = "已建存档，等待建单"


class MessageNotSent(RuntimeError):
    """消息尚未交接给 HTTP 传输。"""


@dataclass(frozen=True, repr=False, kw_only=True)
class ApprovalSettings(Settings):
    trial_credentials: tuple[tuple[str, str], ...]
    trial_credentials_complete: bool
    trial_login_dir: Path | None = None


class OutletUnavailable(CredentialConflict):
    """发送前无法核对已配置的凭据来源或检查器。"""


def send_trial_message(transport, content, request_id, card, check):
    if (not getattr(transport, "order_http", None) or transport.im.message is not transport.order_http
            or not callable(getattr(transport, "before_send", None))
            or not callable(getattr(transport, "trial_action", None))):
        raise OutletUnavailable("缺少试运行凭据出口检查器")
    handed_off = False

    def claim():
        nonlocal handed_off
        check()
        handed_off = True

    async def deliver():
        payload = content if card else {"text": content}
        body = (CreateMessageRequestBody.builder().receive_id(transport.settings.chat_id)
                .msg_type("interactive" if card else "text").content(json.dumps(payload, ensure_ascii=False))
                .uuid(request_id).build())
        request = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
        transport.before_send("message", {"receive_id": body.receive_id, "content": body.content, "uuid": request_id})
        token = await transport._tenant_token()
        return await transport.im.message.acreate(request, RequestOption.builder().tenant_access_token(token).build())
    try:
        with transport.trial_action(claim):
            response = asyncio.run(deliver())
    except CredentialConflict:
        raise
    except Exception as exc:
        if not handed_off:
            raise MessageNotSent("试运行群消息尚未交接") from exc
        raise
    if (not response.success() or not response.data or not isinstance(response.data.message_id, str)
            or not response.data.message_id.strip()):
        raise RuntimeError("试运行群消息回执不明。")
    return response.data.message_id


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
            elif outlet == "http" and value[1] in ("message", "patch"):
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
        self.queue_updates = False
        self.archive_wired = False  # 存档执行器已装配：同意后进入建存档，而非「执行器未就绪」
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
            opened = self.clock()
            journal.rotate(session, reason, instant=opened)
        with self._rotation_lock:
            if self._pending is pending:
                self.session, self.connected_at, self.origin = session, opened, origin
                self._pending = None

    def maintain(self):
        self._apply_rotation()
        with journal_at(self.path) as j:
            if self.switches().get("MAIL_TRIAL_CARD_ENABLED") != "true":
                if j.session():
                    j.rotate("", "审批已关闭", instant=self.clock())
            else:
                j.scan(self.clock(), create_enabled=self.switches().get("MAIL_TRIAL_CREATE_ENABLED") == "true")
            for row in j.db.execute("""SELECT n.*, c.snapshot FROM trial_notification n JOIN trial_card c
                                       USING(card) WHERE n.status='pending' ORDER BY n.rowid""").fetchall():
                target = json.loads(row["snapshot"]).get("contact")

                def claim():
                    with j.db:
                        if not j.db.execute("""UPDATE trial_notification SET status='unknown'
                            WHERE uuid=? AND status='pending'""", (row["uuid"],)).rowcount:
                            raise ValueError
                needs_contact = row["kind"] == "remind" or row["kind"].startswith(("closed", "archive_", "order_"))
                # 旧「未建单」公告投递前按目标表 + 归一政策名核对：已被新卡接管或已建单的改为过时文案（2d）。
                text, handoff = unbuilt_notice(j.db, row)
                body = (mention(target, "配置负责人") + " " if target else "") + text
                if row["kind"] == "result_manual" or row["kind"].startswith("first_manual:"):
                    body = MANUAL_NOTICE
                    handoff = []
                try:
                    send_trial_message(self.transport, body, row["uuid"], False, claim)
                except CredentialConflict:
                    _LOG.error("E-TRIAL-MANUAL")
                    with j.db:
                        j.db.execute("UPDATE trial_notification SET status='blocked' WHERE uuid=?", (row["uuid"],))
                        if row["kind"] != "manual":
                            j.db.execute("""INSERT OR IGNORE INTO trial_notification VALUES(
                                ?, 'manual', ?, '本批群通知被拦截，请人工核对私有登记。', 'pending')""",
                                         (row["card"], str(uuid4())))
                    continue
                with j.db:
                    j.db.execute("UPDATE trial_notification SET status=? WHERE uuid=?",
                                 ("missing_contact" if needs_contact and not target else "sent", row["uuid"]))
                    # 「未建单」已告知人工：登记移交，守望者销清前机器人不再接管同一政策。
                    record_handoff(j.db, row["card"], handoff, self.clock())

            self._rewrite_archived_cards(j)

            for row in j.db.execute("SELECT * FROM trial_feedback WHERE status='pending'").fetchall():
                def claim_feedback():
                    with j.db:
                        if not j.db.execute("""UPDATE trial_feedback SET status='unknown'
                                WHERE uuid=? AND status='pending'""", (row["uuid"],)).rowcount:
                            raise ValueError
                try:
                    send_trial_message(self.transport, MANUAL_NOTICE, row["uuid"], False, claim_feedback)
                except CredentialConflict:
                    with j.db:
                        j.db.execute("UPDATE trial_feedback SET status='blocked' WHERE uuid=?", (row["uuid"],))
                    continue
                with j.db:
                    j.db.execute("UPDATE trial_feedback SET status='sent' WHERE uuid=?", (row["uuid"],))

    def _rewrite_archived_cards(self, j):
        """存档已建：改写原卡为「已建存档，等待建单」。改卡失败或未知按卡版本登记人工核对，不重试。"""
        rows = j.db.execute("""SELECT a.registration, a.version, a.card, c.snapshot, d.message
            FROM trial_archive a JOIN trial_card c USING(card)
            JOIN card_intent i ON i.registration=a.registration AND i.version=a.version
            JOIN trial_delivery d USING(uuid)
            WHERE a.status='success' AND a.card_update='pending' AND c.status='accepted'
            AND i.page=(SELECT max(page) FROM card_intent WHERE registration=a.registration
                        AND version=a.version AND status='sent')""").fetchall()
        for row in rows:
            with j.db:
                if not j.db.execute("""UPDATE trial_archive SET card_update='unknown' WHERE registration=?
                        AND version=? AND card_update='pending'""", (row["registration"], row["version"])).rowcount:
                    continue
            try:
                transport = self.transport
                if not transport.order_http or not callable(transport.before_send):
                    raise OutletUnavailable("缺少试运行凭据出口检查器")
                page = json.loads(row["snapshot"])["pages"][-1]
                page["elements"] = [e for e in page["elements"] if e.get("tag") != "form"]
                page["elements"].append({"tag": "div", "text": {"tag": "plain_text", "content": ARCHIVED_TEXT}})
                asyncio.run(self._patch_card(row["message"], page))
            except Exception:
                with j.db:
                    j.manual(row["card"])
                    j.db.execute("""UPDATE trial_archive SET card_update='manual' WHERE registration=?
                                 AND version=?""", (row["registration"], row["version"]))
            else:
                with j.db:
                    j.db.execute("""UPDATE trial_archive SET card_update='sent' WHERE registration=?
                                 AND version=?""", (row["registration"], row["version"]))

    async def _patch_card(self, message, page):
        transport = self.transport
        with transport.trial_action():
            transport.before_send("patch_content", page)
            body = PatchMessageRequestBody.builder().content(json.dumps(page, ensure_ascii=False)).build()
            request = PatchMessageRequest.builder().message_id(message).request_body(body).build()
            option = RequestOption.builder().tenant_access_token(await transport._tenant_token()).build()
            await transport.order_http.async_request(request, option, "patch", PatchMessageResponse)

    def refuse_click(self, data, *, notify=True):
        event = data.header.event_id
        if not isinstance(event, str) or not event:
            raise ValueError("E-TRIAL-DISABLED")
        with journal_at(self.path) as j, j.db:
            key = _fingerprint(self.settings.app_id, event)
            j.db.execute("INSERT OR IGNORE INTO trial_event VALUES(?,1)", (key,))
            if notify and not j.db.execute("SELECT 1 FROM trial_receipt WHERE event=?", (key,)).fetchone():
                j.db.execute("INSERT OR IGNORE INTO trial_feedback VALUES(?,?,'pending')",
                             (key, str(uuid4())))

    async def update_result(self, data):
        # 只改已提交回执所绑定的原卡，从私有送达登记取得消息号与卡面。
        key = _fingerprint(self.settings.app_id, data.header.event_id)
        with journal_at(self.path) as j:
            row = j.db.execute("""SELECT u.card, u.action, u.status, c.snapshot, d.message
                FROM trial_update u JOIN trial_card c USING(card)
                JOIN card_intent i ON i.registration=c.registration AND i.version=c.version
                JOIN trial_delivery d USING(uuid)
                WHERE u.event=? ORDER BY i.page DESC LIMIT 1""", (key,)).fetchone()
            if not row:
                return False
            if row["status"] != "pending":
                return True
            try:
                transport = self.transport
                if not transport.order_http or not callable(transport.before_send):
                    raise OutletUnavailable("缺少试运行凭据出口检查器")
                page = json.loads(row["snapshot"])["pages"][-1]
                if row["action"] != "verify":
                    page["elements"] = [e for e in page["elements"] if e.get("tag") != "form"]
                text = {"verify": "邮件审批已核验",
                        "approve": "有效同意已记录，将建存档" if self.archive_wired else "有效同意已记录，执行器未就绪",
                        "reject": "已驳回"}[row["action"]]
                page["elements"].append({"tag": "div", "text": {"tag": "plain_text", "content": text}})
                with transport.trial_action():
                    transport.before_send("patch_content", page)
                    body = PatchMessageRequestBody.builder().content(json.dumps(page, ensure_ascii=False)).build()
                    request = PatchMessageRequest.builder().message_id(row["message"]).request_body(body).build()
                    option = RequestOption.builder().tenant_access_token(await transport._tenant_token()).build()
                    await transport.order_http.async_request(request, option, "patch", PatchMessageResponse)
            except Exception:
                with j.db:
                    j.manual(row["card"])
                    j.db.execute("UPDATE trial_update SET status='manual' WHERE event=?", (key,))
            else:
                with j.db:
                    j.db.execute("UPDATE trial_update SET status='sent' WHERE event=?", (key,))
            return True

    async def record(self, data, origin, deadline):
        if not self.queue_updates:
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
        arrival = self.switches()
        max_age = (int(arrival.get("MAIL_TRIAL_MEMBER_CHECK_SECONDS", "120")) if self.queue_updates
                   else self.settings.callback_max_age)
        now = self.clock()
        raw = str(header.create_time)
        if not re.fullmatch(r"\d{1,18}", raw, re.ASCII):
            return response("回调时间不符")
        times = [int(raw)/unit for unit in (1, 1000, 1000000)]
        times = [t for t in times if max(self.connected_at, now-max_age) <= t <= now]
        if len(times) != 1:
            return response("回调时间不符")
        if self.queue_updates:
            key = _fingerprint(self.settings.app_id, key)

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
                    if not 0 <= self.clock() - times[0] <= max_age:
                        return response("回调时间不符")
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
            # 自由文本中的实名和内部编号没有完整字典。群内只回显登记的原因类别。
            if reason.strip() and reason not in {"字段缺失", "申请材料不完整", "审批依据不足", "原因已拦截，请人工核对"}:
                reason = "驳回说明请向点击人核对"
            reason = escape(reason.strip())
        def record():
            with journal_at(self.path) as j:
                return j.action(session, value["card"], value["registration"], value["version"], msg,
                                value["action"], key, actor, times[0], self.clock,
                                lambda: current() and 0 <= self.clock()-times[0] <= max_age,
                                member, switches, reason=reason, claimed=True, queue_update=self.queue_updates,
                                archive=self.archive_wired)
        async with asyncio.timeout(max(0, deadline-time.monotonic())):
            return response(await asyncio.to_thread(record))
