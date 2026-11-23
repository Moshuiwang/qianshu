"""单租约的试运行轮询与交接。邮件正文仅在本轮内存中保留。"""

import asyncio
import json
import logging
import signal
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from qianshu.config import ConfigError
from qianshu.feishu.cards import CardLine, detail_pages, result_card
from qianshu.feishu.entrance import Application
from qianshu.pipeline import Pipeline

from . import _fingerprint
from .imap import MailError, trial_connect
from .segmented import read_segmented
from .triage import MailItem, classify, input_limit, trial_model
from .trial_state import Hold, Store

_LOG = logging.getLogger(__name__)
_BODY_NOTICE = "HTML 正文未参与解析，请人工核对原邮件"
_NOTICES = {
    "received": "收到，待核对 1 封。",
    "deferred": "今日解析已达上限，1 封顺延至下一 UTC 日。",
    "conflict": "身份冲突，已冻结 {n} 封，待人工核对。",
    "duplicate": "疑似重复，{n} 封待人工核对。",
    "freeze": "邮箱水位已冻结，本账户已登记 {n} 封，待人工对账。",
    "ledger": "费用账本不完整，待人工对账。",
    "gap": "收信水位区间有 {n} 个未知位置，待人工对账。",
    "fee15": "开发与试运行已花及已预占费用合计达到 15 美元报告线。",
    "parse8": "试运行解析已花及已预占费用合计超过 8 美元报告线。",
    "stop20": "费用停线，请守望者核对预算及开关。",
}


class Trial:
    def __init__(self, store, config, switches, send, stop, *, connector=trial_connect,
                 clock=lambda: datetime.now(UTC),
                 companies=None, sensitive=(), legacy_path=None):
        self.store, self.config, self.switches, self.send = store, config, dict(switches()), send
        self.stop, self.connector, self.clock = stop, connector, clock
        self.companies, self.sensitive = companies, sensitive
        self.scope = _fingerprint(config["MAIL_HOST"], config["MAIL_USER"])
        self.legacy_path = legacy_path
        self.input_limits = {}
        self.folder = config.get("MAIL_FOLDER", "INBOX")

    def enabled(self, card=False):
        settings = self.switches
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

    def poll(self):
        if not self.enabled():
            return
        if self.legacy_path is not None:
            self.store.import_legacy(self.legacy_path)
        state = self.store.begin(self.scope, _fingerprint(self.folder))
        if state == "folder_conflict":
            self.flush()
            return
        with self.connector(self.config) as mailbox:
            self.seen = {}
            try:
                self.scan(mailbox, state)
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

    def scan(self, mailbox, state):
        validity, next_uid, history = mailbox.snapshot(self.folder)
        if state == "initializing":
            self.store.activate(self.scope, validity, next_uid, history)
            return
        box = self.box()
        if (box["state"] == "frozen" and box["reason"] == "reconciling"
                and (validity, next_uid) == (box["validity"], box["cursor"])):
            self.flush()
            return
        if validity == box["validity"] and next_uid >= box["peak"]:
            if box["reason"] == "legacy":
                self.flush()
                return
            if box["state"] == "active" and self.store.row("SELECT 1 FROM legacy WHERE status='pending' LIMIT 1"):
                self.store.freeze_legacy(self.scope, box["generation"])
                self.flush()
                return
        if box["state"] == "frozen" or validity != box["validity"] or next_uid < box["peak"]:
            if box["state"] != "frozen":
                self.store.freeze(self.scope, "generation")
            if self.store.uncovered(self.scope):
                self.flush()
                return
            entries = []
            for uid in mailbox.interval(1, next_uid - 1):
                if not self.enabled():
                    return
                try:
                    item, _ = self.read(mailbox, uid)
                except MailError:
                    item = MailItem(None, None, None, None, {})
                entries.append((uid, replace(item, fields={})))
            if mailbox.snapshot(self.folder)[:2] == (validity, next_uid):
                self.store.rebuild(self.scope, validity, next_uid, history, entries)
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
            if item.body_missed:
                # 兼容升级前已经登记、这轮首次核实覆盖的邮件。
                with self.store.write():
                    self.store.record_coverage(self.scope, item)
            if card:
                self.parse(row, gen, text, body_missed=item.body_missed)
            else:
                self.triage(row, gen, item)
        self.flush()

    def verify_generation(self, mailbox):
        validity, next_uid, _ = mailbox.snapshot(self.folder)
        row = self.box()
        if validity == row["validity"] and next_uid >= row["peak"] and row["state"] == "active":
            self.store.peak(self.scope, next_uid)
            return True
        self.store.freeze(self.scope, "generation")
        return False

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
                unknown = bool(call.failure_stage) or not complete

        model = trial_model(self.config, reserve, result)
        pages = Pipeline(model=model, tag_dates={}, sensitive_values=self.sensitive,
                         companies_file=self.companies)(Application(None, text, ()))
        if refused or (text and attempt is None):
            if attempt is not None:
                self.status(reg, "correction_pending")
            return
        with self.store.write() as db:
            expected = "parsing" if attempt else "parse"
            if not db.execute("UPDATE registration SET status='sending' WHERE id=? AND status=?",
                              (reg, expected)).rowcount:
                return
        notices = []
        if body_missed:
            notices.append(CardLine("正文", _BODY_NOTICE))
        if row["attachments"] != 0:
            hint = f"含 {row['attachments']} 个附件未解析，请对照原件核对"
            if row["attachments"] is None:
                hint = "附件数量未知，请对照原件核对"
            notices.append(CardLine("附件", hint))
        if notices:
            pages = (*detail_pages("收信", notices), *pages)
        heading = detail_pages("试运行邮件", [])[0].elements
        try:
            for index, page in enumerate(pages, 1):
                page = replace(page, elements=(*heading, *page.elements[1:]), number=index, total=len(pages))
                with self.store.dispatch(self.scope, gen, reg, "card", lambda: self.enabled(True)):
                    request_id = str(uuid4())
                    with self.store.write() as db:
                        db.execute("INSERT INTO card_intent VALUES(?,?,?,?,'unknown',?)",
                                   (request_id, reg, row["version"], index, self.clock().isoformat()))
                    self.send(result_card(page), request_id, True, lambda: self.require_enabled(True))
                    with self.store.write() as db:
                        db.execute("UPDATE card_intent SET status='sent' WHERE uuid=?", (request_id,))
        except Exception:
            unknown = True
        status = "manual" if unknown or attempt is None else "done"
        with self.store.write():
            self.store.finish(reg, status, warned=body_missed)

    def flush(self):
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
                    with self.store.write() as db:
                        if not db.execute("""UPDATE notice SET status='unknown' WHERE event=?
                            AND status='pending'""", (row["event"],)).rowcount:
                            continue
                    warning = kind == "body_missed"
                    content = (result_card(detail_pages("试运行邮件", [CardLine("正文", _BODY_NOTICE)])[0])
                               if warning else _NOTICES[kind].format(n=row["quantity"]))
                    self.send(content, row["uuid"], warning,
                              lambda: self.require_enabled(card))
                    with self.store.write() as db:
                        db.execute("UPDATE notice SET status='sent' WHERE event=?", (row["event"],))
            except Hold:
                continue
            except Exception:
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


def run_service(args, settings, path, switches):
    from threading import Event, Thread, current_thread

    from dotenv import dotenv_values
    from qianshu.config import load_config
    from qianshu.feishu.handoff import HandoffService, MentionLog
    from qianshu.parse._output import check_output_path
    from qianshu.parse.model import load_model_config
    from qianshu.__main__ import _send_trial_message
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
    closed = Event()

    def poll():
        store = None
        try:
            store = Store(state)
            with store.write() as db:
                target = _fingerprint(settings.app_id, settings.chat_id, settings.tenant_key or "")
                db.execute("INSERT OR IGNORE INTO trial_target VALUES(1,?)", (target,))
                if store.row("SELECT fingerprint FROM trial_target")[0] != target:
                    raise ConfigError("试运行登记与汇报群不匹配。")
            trial = Trial(store, config, switches, send, stop, legacy_path=legacy, sensitive=(
                *settings.handoff_sensitive_values, *settings.handoff_sensitive_urls,
            ))
            trial.run()
        except Exception:
            _LOG.error("试运行收信已停止，请核对私有登记。")
        finally:
            if store is not None:
                try:
                    store.db.close()
                except Exception:
                    _LOG.critical("收信登记库关闭失败，监听锁保留，须人工停机核对。")
                    return
            closed.set()

    class TrialLog(MentionLog):
        _listener_thread = None

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

    def send(content, request_id, card, check):
        _send_trial_message(service.transport, content, request_id, card, check)

    interrupted = False

    def record_interrupt(signum, frame):
        nonlocal interrupted
        interrupted = True

    class TrialHandoff(HandoffService):
        async def _serve(self, client):
            # 让长连接从事件循环正常退出；信号处理器自身只写标志。
            serving = asyncio.create_task(super()._serve(client))
            try:
                while not interrupted:
                    done, _ = await asyncio.wait((serving,), timeout=0.25)
                    if done:
                        return serving.result()
            finally:
                serving.cancel()
                await asyncio.gather(serving, return_exceptions=True)

    previous = signal.signal(signal.SIGINT, record_interrupt)
    try:
        with TrialLog.listening(path, settings) as log:
            service = TrialHandoff(log)
            thread = Thread(target=poll, name="trial-mail")
            thread.start()
            try:
                if not interrupted:
                    service.run()
            finally:
                stop.set()
                closed.wait()
                thread.join()
    finally:
        signal.signal(signal.SIGINT, previous)
        if interrupted:
            signal.raise_signal(signal.SIGINT)
