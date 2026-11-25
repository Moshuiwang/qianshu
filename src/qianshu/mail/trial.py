"""单租约的试运行轮询与交接。邮件正文仅在本轮内存中保留。"""

import asyncio
import fcntl
import json
import logging
import os
import signal
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from qianshu.config import ConfigError
from qianshu.feishu.cards import CardLine, detail_pages, result_card
from qianshu.feishu.entrance import Application
from qianshu.pipeline import Pipeline
from qianshu.mingdao.create_order import CredentialConflict
from qianshu.feishu.trial_approval import Journal
from qianshu.feishu.trial_listener import MessageNotSent, approval_setup

from . import _fingerprint
from .imap import MailError, trial_connect
from .segmented import read_segmented
from .triage import MailItem, classify, input_limit, trial_model
from .trial_diagnostics import CLOSE_FAILED, CLOSE_TIMEOUT, fault_writer, listener_logs
from .trial_state import BODY_NOTICE, RECOVERY_REASONS, FolderHold, FormatHold, Hold, Store

_LOG = logging.getLogger(__name__)
_MAIL_CLOSE_TIMEOUT = 2
_NOTICES = {
    "blocked": "登记 {registration} 的试运行消息在本地被拦截，未建单，转人工核对私有登记。",
    **{"recovery_" + code: f"收信确认未完成：{label}，请配置负责人核对"
       for code, label in RECOVERY_REASONS.items()},
    "recovery_restored": "收信已恢复，缺口 {n} 个已登记待人工核对",
    "received": "收到，待核对 1 封。",
    "deferred": "今日解析已达上限，1 封顺延至下一 UTC 日。",
    "conflict": "身份冲突，已冻结 {n} 封，待人工核对。",
    "duplicate": "疑似重复，{n} 封待人工核对。",
    "freeze": "邮箱水位已冻结，本账户已登记 {n} 封，待人工对账。",
    "ledger": "费用账本不完整，待人工对账。",
    "interrupted": "解析中途停止，1 封待人工核对。",
    "gap": "收信水位区间有 {n} 个未知位置，待人工对账。",
    "fee15": "开发与试运行已花及已预占费用合计达到 15 美元报告线。",
    "parse8": "试运行解析已花及已预占费用合计超过 8 美元报告线。",
    "stop20": "费用停线，请守望者核对预算及开关。",
}


class Recovery:
    """常驻服务的快照重建与确认取证。"""

    def __init__(self, config, connector, enabled):
        self.config = config
        self.connector, self.enabled = connector, enabled
        self.scope = _fingerprint(config["MAIL_HOST"], config["MAIL_USER"])
        self.folder = config.get("MAIL_FOLDER", "INBOX")

    @contextmanager
    def covered_snapshot(self, mailbox, snapshot):
        # EXISTS 是整箱计数。legacy 也搜索整箱，只在持久化时应用自己的水位。
        last = snapshot[1] - 1
        present = mailbox.interval(1, last)
        if len(present) != snapshot[2]:
            raise Hold()
        yield present
        if (not self.enabled() or mailbox.interval(1, last) != present
                or mailbox.snapshot(self.folder) != snapshot):
            raise Hold()

    def verify_request(self, store, request):
        scope = request["scope"]
        binding = store.row("SELECT folder FROM trial_folder WHERE scope=?", scope)
        if scope != self.scope or not binding or binding[0] != _fingerprint(self.folder):
            raise FolderHold()
        generation, validity, cursor = (request[key] for key in ("generation", "validity", "cursor"))
        freeze = store.row("SELECT max(rowid) FROM notice WHERE scope=? AND kind='freeze'", scope)[0]
        if request["freeze_id"] != (freeze or 0):
            raise Hold()
        action = store.confirm_action(scope, generation, validity, cursor)
        box = store.row("SELECT * FROM mailbox WHERE scope=?", scope)
        with self.connector(self.config) as mailbox:
            snapshot = mailbox.snapshot(self.folder)
            if not store.same_epoch(box, *snapshot[:2]):
                raise Hold()
            next_uid = snapshot[1]
            with self.covered_snapshot(mailbox, snapshot) as present:
                pass  # 确认只读元数据。
        if action == "settle_legacy":
            store.check_legacy(scope, validity, next_uid, present)
            return
        with store.write():
            store.recovery_gaps(box, validity, next_uid, present)
        expected = tuple(row[0] for row in store.db.execute("""SELECT uid FROM reconciliation
            WHERE scope=? AND generation=? ORDER BY uid""", (scope, generation)))
        if next_uid != cursor or present != expected:
            raise Hold()

    def capture(self, store, mailbox, snapshot, action):
        validity, next_uid, history = snapshot
        scope = self.scope
        if action not in {"rebuild", "check_legacy"}:
            raise Hold()
        with self.covered_snapshot(mailbox, snapshot) as uids:
            entries = []
            seen_flags = {}
            if action == "rebuild":
                for uid in uids:
                    if not self.enabled():
                        raise Hold()
                    try:
                        seen = mailbox.read_seen(uid)
                        seen_flags[uid] = seen
                        item, _ = read_segmented(mailbox, uid, seen)
                    except MailError:
                        item = MailItem(None, None, None, None, {})
                    entries.append((uid, replace(item, fields={})))
            for uid, seen in seen_flags.items():
                mailbox.check_seen(uid, seen)
        if action == "check_legacy":
            store.check_legacy(scope, validity, next_uid, uids)
        else:
            store.rebuild(scope, validity, next_uid, history, entries)


class Trial:
    def __init__(self, store, config, switches, send, stop, *, connector=trial_connect,
                 clock=lambda: datetime.now(UTC),
                 companies=None, sensitive=(), legacy_path=None, approval_setup=None):
        self.store, self.config, self.switches, self.send = store, config, dict(switches()), send
        self.read_switches = switches
        self.stop, self.connector, self.clock = stop, connector, clock
        self.companies, self.sensitive = companies, sensitive
        self.approval_setup = approval_setup
        self.legacy_path = legacy_path
        self.input_limits = {}
        self.recovery = Recovery(config, connector, self.enabled)
        self.scope = self.recovery.scope
        self.folder = self.recovery.folder
        store.process_confirmation = self.process_confirmation

    def enabled(self, card=False):
        settings = self.read_switches() if self.approval_setup else self.switches
        return (not self.stop.is_set() and settings["MAIL_TRIAL_RECEIVE_ENABLED"] == "true"
                and (not card or settings["MAIL_TRIAL_CARD_ENABLED"] == "true"))

    def require_enabled(self, card=False):
        if not self.enabled(card):
            raise Hold()

    def box(self):
        return self.store.row("SELECT * FROM mailbox WHERE scope=?", self.scope)

    def read(self, mailbox, uid, expected=None):
        seen = mailbox.read_seen(uid)
        if self.seen.setdefault(uid, seen) != seen or (expected is not None and seen != expected):
            raise MailError("已读位置或状态变化。")
        return read_segmented(mailbox, uid, seen)

    def process_confirmation(self):
        with self.store.gate():
            request = self.store.row("""SELECT * FROM recovery_request WHERE scope=? AND status='pending'
                ORDER BY id LIMIT 1""", self.scope)
            if request is None:
                return False
            try:
                self.recovery.verify_request(self.store, request)
                self.store.finish_confirmation(request)
            except FolderHold:
                self.store.finish_confirmation(request, "folder")
            except (MailError, OSError):
                self.store.finish_confirmation(request, "mail_read")
            except Hold:
                self.store.finish_confirmation(request, "snapshot")
        return True

    def poll(self):
        if not self.enabled():
            return
        if self.legacy_path is not None:
            self.store.import_legacy(self.legacy_path)
        if self.process_confirmation():
            self.flush()
            return
        state = self.store.begin(self.scope, _fingerprint(self.folder))
        if state == "folder_conflict":
            self.flush()
            return
        with self.connector(self.config) as mailbox:
            self.seen = {}
            try:
                self.scan(mailbox)
            except MailError:
                self.store.freeze(self.scope, "mapping")
                self.flush()
            finally:
                for uid, seen in self.seen.items():
                    try:
                        mailbox.check_seen(uid, seen)
                    except MailError:
                        self.store.freeze(self.scope, "seen")
                        break

    def scan(self, mailbox):
        with self.store.gate():
            snapshot = mailbox.snapshot(self.folder)
            validity, next_uid, history = snapshot
            action = self.store.scan_action(self.scope, validity, next_uid)
            if action == "activate":
                self.store.activate(self.scope, validity, next_uid, history)
                return
            if action in {"rebuild", "check_legacy"}:
                self.recovery.capture(self.store, mailbox, snapshot, action)
            box = self.box()
        if action != "observe":
            self.flush()
            return
        gen = box["generation"]
        self.store.peak(self.scope, next_uid)
        if box["cursor"] < next_uid and not self.store.row("SELECT 1 FROM observed_range WHERE scope=?", self.scope):
            self.store.searching(self.scope, gen, box["cursor"], next_uid - 1)
            uids = mailbox.interval(box["cursor"], next_uid - 1)
            self.store.observe(self.scope, gen, box["cursor"], next_uid, uids)
        fresh = {}
        for uid in self.store.pending(self.scope, gen):
            if not self.enabled():
                return
            try:
                item, _ = self.read(mailbox, uid)
                if not self.verify_generation(mailbox):
                    return
            except MailError:
                # 无法确定已经观察到的定位指向谁，整代待对账。
                self.store.freeze(self.scope, "missing")
                self.flush()
                return
            self.store.register(self.scope, gen, uid, item, seen=self.seen[uid])
            fresh[uid] = item
        if not self.store.budget_open():
            self.flush()
            return
        rows = list(self.store.db.execute("""SELECT r.*,min(l.uid) uid,l.initial_seen FROM registration r
            JOIN location l ON l.registration=r.id WHERE r.scope=? AND l.generation=?
            AND r.status IN ('triage','parse') GROUP BY r.id ORDER BY r.id""", (self.scope, gen)))
        for row in rows:
            card = row["status"] == "parse"
            if not self.enabled(card) or (card and row["due"] and row["due"] > self.clock().astimezone(UTC).date().isoformat()):
                continue
            try:
                uid = row["uid"]
                if not card and uid in fresh:
                    item, text = fresh.pop(uid), None
                else:
                    item, text = self.read(mailbox, uid, row["initial_seen"])
                if not self.verify_generation(mailbox):
                    break
                mailbox.check_seen(uid, self.seen[uid])
            except MailError:
                self.store.freeze(self.scope, "mapping")
                break
            if (item.key, item.digest, item.candidate, item.attachments) != (
                    row["msgkey"], row["digest"], row["candidate"], row["attachments"]):
                self.store.freeze(self.scope, "mapping")
                break
            if card:
                if self.approval_setup and not Journal(self.store.db).session():
                    continue
                self.parse(row, gen, text, body_missed=item.body_missed)
            else:
                self.triage(row, gen, item)
        self.flush()

    def verify_generation(self, mailbox):
        validity, next_uid, _ = mailbox.snapshot(self.folder)
        return self.store.verify_epoch(self.scope, validity, next_uid)

    def status(self, reg, status):
        with self.store.write() as db:
            if status in {"done", "no", "manual", "correction_pending"}:
                self.store.finish(reg, status)
            else:
                db.execute("UPDATE registration SET status=? WHERE id=? AND status!='frozen'", (status, reg))

    def charge_limits(self, req, family):
        data = json.loads(req.content)
        model = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5"}[family]
        if data["model"] != model:
            raise Hold()
        if model not in self.input_limits:
            self.input_limits[model] = input_limit(self.config, model)
        return data["max_tokens"], self.input_limits[model]

    def triage(self, row, gen, item):
        reg = row["id"]
        attempt = None

        @contextmanager
        def reserve(req):
            nonlocal attempt
            with self.store.dispatch(self.scope, gen, reg, "haiku", self.enabled):
                maximum, bound = self.charge_limits(req, "haiku")
                self.require_enabled()
                attempt = self.store.reserve("haiku", maximum, bound, reg, stage="haiku")
                yield

        verdict, reason, inputs, outputs = classify(self.config, item, reserve)
        if attempt is None:
            return
        if not item.fields["body"]:
            verdict, reason = "拿不准", "分拣正文为空"
        self.store.settle(attempt, inputs, outputs)
        with self.store.write() as db:
            changed = db.execute("UPDATE registration SET verdict=?,failure=?,status=? WHERE id=? AND status='classifying'",
                       (verdict, reason, "classifying" if verdict == "否" else "parse", reg))
            if not changed.rowcount:
                return
            if verdict == "否":
                self.store.finish(reg, "no")
            elif not self.enabled(True):
                self.store.notice(f"received:{reg}", self.scope, reg, "received")

    def parse(self, row, gen, text, *, body_missed=False):
        reg = row["id"]
        attempt = None
        refused = False
        unknown = False

        @contextmanager
        def reserve(req):
            nonlocal attempt, refused
            refused = True
            stage = "sonnet" if attempt is None else "correction"
            with self.store.dispatch(self.scope, gen, reg, stage, lambda: self.enabled(True)):
                maximum, bound = self.charge_limits(req, "sonnet")
                self.require_enabled(True)
                day = self.clock().astimezone(UTC).date()
                if stage == "sonnet":
                    if row["due"] and row["due"] > day.isoformat():
                        raise Hold()
                    limit = int(self.switches["MAIL_TRIAL_DAILY_PARSE_LIMIT"])
                    used = self.store.row("SELECT count(*) FROM registration WHERE parse_day=?", day.isoformat())[0]
                    if used >= limit:
                        with self.store.write() as db:
                            db.execute("UPDATE registration SET due=? WHERE id=?",
                                       ((day + timedelta(days=1)).isoformat(), reg))
                            self.store.notice(f"defer:{reg}:{day}", self.scope, reg, "deferred")
                        raise Hold()
                attempt = self.store.reserve("sonnet", maximum, bound, reg, stage=stage, day=day.isoformat())
                refused = False
                yield

        def result(call):
            nonlocal unknown
            if attempt is not None and not refused:
                complete = self.store.settle(attempt, call.input_tokens, call.output_tokens,
                                             partial_inputs=call.partial_input_tokens,
                                             partial_outputs=call.partial_output_tokens)
                # 多次调用（首轮 + 纠错）累加，任何一次失败或用量不完整都转人工（G448）。
                unknown = unknown or bool(call.failure_stage) or not complete

        model = trial_model(self.config, reserve, result)
        setup = self.approval_setup() if self.approval_setup else None
        tag_dates = {}
        if setup:
            from .trial_cards import confirmed_tag_dates
            tag_dates = confirmed_tag_dates(setup[1])
        review = []

        def capture(drafts, policies, full_review):
            review.extend((drafts, policies, full_review))

        pages = Pipeline(model=model, tag_dates=tag_dates, sensitive_values=self.sensitive,
                         companies_file=self.companies, **({"on_review": capture} if self.approval_setup else {}))(Application(None, text, ()))
        if refused or (text and attempt is None):
            if attempt is not None:
                # 纠错阶段未获派发（如停机）：经统一出口转人工并发固定群提示（G448）。
                with self.store.write():
                    self.store.finish(reg, "manual", reason="correction_interrupted")
                    self.store.notice(f"interrupted:{reg}", self.scope, reg, "interrupted")
            return
        with self.store.write() as db:
            expected = "parsing" if attempt else "parse"
            if not db.execute("UPDATE registration SET status='sending' WHERE id=? AND status=?",
                              (reg, expected)).rowcount:
                return
            if body_missed:
                self.store.notice(f"body:{reg}", self.scope, reg, "body_missed")
        notices = []
        if body_missed:
            notices.append(CardLine("正文", BODY_NOTICE))
        if row["attachments"] != 0:
            hint = f"含 {row['attachments']} 个附件未解析，请对照原件核对"
            if row["attachments"] is None:
                hint = "附件数量未知，请对照原件核对"
            notices.append(CardLine("附件", hint + "，整批转人工，未建单"))
        if notices:
            pages = (*detail_pages("收信", notices), *pages)
        heading = detail_pages("试运行邮件", [])[0].elements
        cards = [result_card(replace(p, elements=(*heading, *p.elements[1:]), number=i, total=len(pages)))
                 for i, p in enumerate(pages, 1)]
        def register_request(page, warning):
            with self.store.write() as db:
                key = (self.store.row("SELECT uuid FROM notice WHERE event=?", f"body:{reg}")[0]
                       if warning else str(uuid4()))
                db.execute("INSERT INTO card_intent VALUES(?,?,?,?,?,?)",
                           (key, reg, row["version"], page, "pending", self.clock().isoformat()))
            return key

        approval = request_id = None
        warning_page = delivery_failed = False
        try:
            if self.approval_setup:
                from .trial_cards import decision_pages
                approval = decision_pages(self, row, text, cards, *(review or ([], [], True)),
                                          setup, unknown or body_missed or attempt is None
                                          or row["attachments"] != 0)
            for index, card in enumerate(cards, 1):
                warning_page = body_missed and index == 1
                request_id = None
                if approval:
                    request_id = register_request(index, warning_page)
                with self.store.dispatch(self.scope, gen, reg, "card", lambda: self.enabled(True)):
                    if request_id is None:
                        request_id = register_request(index, warning_page)

                    def before_send():
                        nonlocal sent
                        self.require_enabled(True)
                        with self.store.write() as db:
                            if warning_page:
                                self.claim_notice(f"body:{reg}")
                            db.execute("UPDATE card_intent SET status='unknown' WHERE uuid=?", (request_id,))
                        sent = self.clock().timestamp()

                    sent = self.clock().timestamp()
                    receipt = self.send(card, request_id, True, before_send)
                    with self.store.write() as db:
                        if approval:
                            Journal(db).received(request_id, receipt)
                        if approval and index == len(cards):
                            db.execute("UPDATE trial_card SET sent=?,expires=? WHERE card=?",
                                       (sent, (int(sent) // 86400 + 2) * 86400, approval))
                        db.execute("UPDATE card_intent SET status='sent' WHERE uuid=?", (request_id,))
                        if warning_page:
                            db.execute("UPDATE notice SET status='sent' WHERE event=?", (f"body:{reg}",))
        except CredentialConflict:
            _LOG.error("E-TRIAL-MANUAL")
            with self.store.write() as db:
                db.execute("UPDATE card_intent SET status='blocked' WHERE uuid=?", (request_id,))
                if warning_page:
                    db.execute("UPDATE notice SET status='blocked' WHERE event=?", (f"body:{reg}",))
                self.blocked_handoff(reg)
                if approval:
                    journal = Journal(db)
                    if request_id is not None:
                        journal.manual(approval, request_id)
                    journal.close(db.execute("SELECT * FROM trial_card WHERE card=?", (approval,)).fetchone(),
                                  "卡片在本地被拦截")
            return
        except Exception:
            delivery_failed = unknown = True
        status = "manual" if unknown or attempt is None else "done"
        with self.store.write():
            self.store.finish(reg, status)
            if unknown and setup and not approval:
                self.store.notice(f"interrupted:{reg}", self.scope, reg, "interrupted")
            if approval:
                journal = Journal(self.store.db)
                if delivery_failed:
                    if request_id is not None:
                        journal.manual(approval, request_id)
                    self.store.db.execute("UPDATE trial_card SET status='partial' WHERE card=? AND status!='closed'", (approval,))
                journal.bind(approval, self.clock().timestamp())

    def blocked_handoff(self, reg):
        self.store.finish(reg, "manual", reason="outlet_blocked")
        self.store.notice(f"blocked:{reg}", self.scope, reg, "blocked")

    def claim_notice(self, event):
        if not self.store.db.execute("""UPDATE notice SET status='unknown'
                WHERE event=? AND status='pending'""", (event,)).rowcount:
            raise Hold()

    def flush(self):
        with self.store.write():
            self.store.review_stale_notices(self.scope)
        rows = list(self.store.db.execute("""SELECT * FROM notice WHERE status='pending'
            AND (scope=? OR scope IS NULL) ORDER BY rowid""", (self.scope,)))
        for row in rows:
            if not self.enabled():
                return
            kind = row["kind"]
            card = kind in {"deferred", "body_missed"}
            if kind == "deferred" and not row["event"].endswith(self.clock().astimezone(UTC).date().isoformat()):
                with self.store.write() as db:
                    db.execute("UPDATE notice SET status='expired' WHERE event=? AND status='pending'", (row["event"],))
                continue
            gate = self.store.gate()
            if kind in {"received", "deferred", "body_missed"}:
                gate = self.store.dispatch(self.scope, row["generation"], row["registration"], kind,
                                           lambda: self.enabled(card))
            try:
                with gate:
                    self.require_enabled(card)
                    warning = kind == "body_missed"
                    if self.store.row("SELECT status FROM notice WHERE event=?", row["event"])[0] != "pending":
                        continue
                    content = (result_card(detail_pages("试运行邮件", [CardLine("正文", BODY_NOTICE)])[0])
                               if warning else _NOTICES[kind].format(n=row["quantity"], registration=row["registration"]))
                    def before_send():
                        self.require_enabled(card)
                        with self.store.write():
                            self.claim_notice(row["event"])

                    self.send(content, row["uuid"], warning, before_send)
                    with self.store.write() as db:
                        db.execute("UPDATE notice SET status='sent' WHERE event=?", (row["event"],))
            except (Hold, MessageNotSent):
                continue
            except CredentialConflict:
                _LOG.error("E-TRIAL-MANUAL")
                with self.store.write() as db:
                    db.execute("UPDATE notice SET status='blocked' WHERE event=?", (row["event"],))
                    if row["registration"] is not None and kind != "blocked":
                        self.blocked_handoff(row["registration"])
            except Exception:
                # 旧发送适配器可能在未调用 claim 的情况下已经交接。
                if kind != "body_missed":
                    with self.store.write() as db:
                        db.execute("UPDATE notice SET status='unknown' WHERE event=? AND status='pending'",
                                   (row["event"],))
                self.uncertain_notices()

    def uncertain_notices(self):
        with self.store.write() as db:
            rows = db.execute("""SELECT id FROM registration WHERE scope=?
                AND status='parse' AND id IN (SELECT registration FROM notice WHERE status='unknown'
                AND kind IN ('received','deferred'))""", (self.scope,)).fetchall()
            for reg, in rows:
                self.store.finish(reg, "manual", reason="notice_unknown")

    def run(self):
        while not self.stop.is_set():
            try:
                self.uncertain_notices()
                with self.store.write() as db:
                    rows = db.execute("""SELECT id FROM registration WHERE scope=?
                        AND status IN ('parsing','sending','classifying')""", (self.scope,)).fetchall()
                    for reg, in rows:
                        self.store.finish(reg, "manual", reason="unknown")
                self.poll()
            except (Hold, ConfigError, MailError):
                pass
            except Exception:
                _LOG.warning("试运行收信轮询中断，请核对私有登记。")
            try:
                self.flush()
            except Exception:
                _LOG.warning("试运行群通知待核对。")
            self.stop.wait(30)


@contextmanager
def _polling_worker(args, settings, path, switches, send, sensitive, report, *, setup=None):
    from threading import Event, Thread

    from dotenv import dotenv_values
    from qianshu.config import load_config
    from qianshu.parse._output import check_output_path
    from qianshu.parse.model import load_model_config
    from . import _private_dir

    business = args.business_env_file
    if business is None:
        raise ConfigError("试运行收信须显式指定业务私有配置。")
    config = dict(load_model_config(env_file=business, config=load_config("MAIL_HOST", "MAIL_USER", "MAIL_PASSWORD",
                                                                          env_file=business)))
    config.setdefault("MAIL_PORT", "993")
    with business.open(encoding="utf-8-sig") as stream:
        private_dir = dotenv_values(stream=stream, interpolate=False).get("MAIL_PRIVATE_DIR")
    if private_dir:
        config["MAIL_PRIVATE_DIR"] = private_dir
    if config.get("FEISHU_CHAT_ID") != settings.chat_id:
        raise ConfigError("试运行汇报群与业务登记不符。")
    state = path.with_name("trial-050.sqlite3")
    check_output_path(state)
    legacy = _private_dir(config) / "mail-poll.sqlite3"
    stop = Event()
    closing = Event()
    clean_close = Event()
    initialized = Event()
    recovering = Event()
    dirty = state.with_suffix(".dirty")

    close_fault = Event()

    def report_fault(message):
        report(message)

    def report_close_failure():
        close_fault.set()
        report_fault(CLOSE_FAILED)

    def poll():
        store = None
        registered = False
        lease = None
        try:
            if setup is None:
                # 旧收信档也可能接管开卡档。只有获取审批租约后才能碰旧台账。
                from qianshu.feishu import handoff
                lease_path = handoff._MACHINE_LEASE.with_name("consumer.lock")
                history = False
                if state.exists():
                    recovering.set()
                    from qianshu.feishu.trial_approval import has_approval_history
                    history = has_approval_history(state)
                if lease_path.exists() or history:
                    recovering.set()
                    from qianshu.feishu.trial_supervisor import recover_disabled
                    lease = handoff._file(lease_path, create=True)
                    while True:
                        try:
                            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if stop.wait(.1):
                                return
                    recover_disabled(args, path)
                recovering.clear()
            try:
                dirty.touch(mode=0o600, exist_ok=False)
                needs_review = False
            except FileExistsError:
                needs_review = True
                if not state.exists():
                    raise FormatHold("收信登记库缺失，请人工对账。")
            store = Store(state, before_failed_close=closing.set, close_failed=report_close_failure)
            with store.write() as db:
                target = _fingerprint(settings.app_id, settings.chat_id, settings.tenant_key or "")
                db.execute("INSERT OR IGNORE INTO trial_target VALUES(1,?)", (target,))
                if store.row("SELECT fingerprint FROM trial_target")[0] != target:
                    raise ConfigError("试运行登记与汇报群不匹配。")
                if needs_review:
                    # 在同一事务内冻结仍可派发的邮箱；已冻结的保留原对账原因。
                    for row in db.execute("SELECT scope FROM mailbox WHERE state!='frozen'").fetchall():
                        store._freeze(row["scope"], "unclean_close")
            registered = True
            trial = Trial(store, config, switches, send, stop, legacy_path=legacy, sensitive=sensitive,
                          approval_setup=setup)
            initialized.set()
            trial.run()
        except FormatHold as exc:
            _LOG.error("%s", exc)
        except Exception:
            _LOG.error("试运行收信已停止，请核对私有登记。")
        finally:
            closing.set()
            initialized.set()
            if store is not None:
                try:
                    store.db.close()
                except Exception:
                    report_close_failure()
                else:
                    if registered:
                        clean_close.set()
            if lease is not None:
                os.close(lease)

    thread = Thread(target=poll, name="trial-mail", daemon=True)
    thread.start()
    try:
        if setup:
            initialized.wait()
            if closing.is_set():
                raise RuntimeError("E-TRIAL-DISABLED")
        yield closing, close_fault
    finally:
        stop.set()
        if setup is None:
            # 收信中等当前操作退出；历史恢复可能稍后才取得或开始等待旧租约。
            while not recovering.is_set() and not closing.wait(.1):
                pass
        thread.join(1 if setup else _MAIL_CLOSE_TIMEOUT)
        if thread.is_alive():
            close_fault.set()
            report_fault(CLOSE_TIMEOUT)
        elif clean_close.is_set():
            dirty.unlink()


@contextmanager
def mail_worker(args, settings, path, switches, sender, available):
    from qianshu.feishu.trial_listener import send_trial_message

    def send(content, request_id, card, check):
        def before_send():
            if not available() or switches().get("MAIL_TRIAL_CARD_ENABLED") != "true":
                raise Hold()
            check()
        return send_trial_message(sender, content, request_id, card, before_send)

    def setup():
        return approval_setup(args.mingdao_config, switches)

    def fault(_):
        logging.getLogger("approval").critical("CRITICAL E-TRIAL-DISABLED 1")

    with _polling_worker(args, settings, path, switches, send, sender.sensitive_values, fault, setup=setup) as state:
        ended, failed = state
        yield ended
    if failed.is_set():
        raise RuntimeError("E-TRIAL-DISABLED")


@contextmanager
def receive_worker(args, settings, path, switches, service, logs, diagnostics=None):
    from qianshu.__main__ import _send_trial_message

    def send(content, request_id, card, check):
        return _send_trial_message(service.transport, content, request_id, card, check)

    def report(message):
        diagnostics.post(message)
        logs.post(message)

    owns_diagnostics = diagnostics is None
    if owns_diagnostics:
        diagnostics = fault_writer(path.with_name("trial-050.sqlite3"))
    sensitive = (*settings.handoff_sensitive_values, *settings.handoff_sensitive_urls)
    try:
        with _polling_worker(args, settings, path, switches, send, sensitive, report) as (_, failed):
            yield failed
    finally:
        if owns_diagnostics:
            diagnostics.finish()


def run_service(args, settings, path, switches):
    """已有收信服务的兼容入口。"""
    from threading import Event, current_thread
    from qianshu.feishu.handoff import HandoffService, MentionLog

    class TrialLog(MentionLog):
        _listener_thread = None

        def close(self):
            try:
                super().close()
            except Exception:
                message = "转交库关闭失败，监听锁保留，须人工停机核对。"
                diagnostics.post(message)
                logs.post(message)
                Event().wait()

        @contextmanager
        def listener(self, *, create=False):
            # 同一服务线程嵌套进入时，外层 listening 仍持有锁。
            if self._listener_thread is current_thread():
                yield
                return
            with super().listener(create=create):
                self._listener_thread = current_thread()
                try:
                    yield
                finally:
                    self._listener_thread = None

    interrupted = False
    listener_error = None

    def interrupt(signum, frame):
        nonlocal interrupted
        interrupted = True

    class TrialHandoff(HandoffService):
        async def _serve(self, client):
            serving = asyncio.create_task(super()._serve(client))
            try:
                while not interrupted:
                    done, _ = await asyncio.wait((serving,), timeout=0.25)
                    if done:
                        return serving.result()
            finally:
                serving.cancel()
                await asyncio.gather(serving, return_exceptions=True)

    diagnostics = fault_writer(path.with_name("trial-050.sqlite3"))
    previous = signal.signal(signal.SIGINT, interrupt)
    try:
        with listener_logs(_LOG) as logs, TrialLog.listening(path, settings) as log:
            service = TrialHandoff(log)
            with receive_worker(args, settings, path, switches, service, logs, diagnostics) as close_fault:
                try:
                    if not interrupted:
                        service.run()
                except Exception as exc:
                    listener_error = exc
    finally:
        diagnostics.finish()
        signal.signal(signal.SIGINT, previous)
    if close_fault.is_set() or logs.thread.is_alive():
        return 130 if interrupted else 1
    if interrupted:
        signal.raise_signal(signal.SIGINT)
    if listener_error is not None:
        raise listener_error
