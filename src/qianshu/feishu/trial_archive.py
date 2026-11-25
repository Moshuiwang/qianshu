"""同意后建存档（#154 段 2c）。

主链只有：核对一次授权 → 登记存档授权 → 建存档 → 记录结果。其余异常一律「整批停下 + 固定群提示 + 转人工」，
不做自动恢复。存档在审批子进程的专用线程里执行，不占用主循环的心跳。
"""

import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from stat import S_ISREG

from qianshu.draft import SourceExcerpt

from . import archive as A
from .state import trial_batch
from .trial_approval import ARCHIVE_NOTICES, CARD_FORMAT, DISCONNECT_NOTICES, journal_at

_LOG = logging.getLogger(__name__)
PURPOSE_KEY = "MAIL_TRIAL_ARCHIVE_PURPOSE"
LEDGER_NAME = "archive-ledger.sqlite3"
_PAGE_WRITE = re.compile(r"/documents/([A-Za-z0-9]+)/blocks/")
_REQUIRED = ("FEISHU_APP_ID", "FEISHU_APP_SECRET", A._SHARE, *A._ROUTES.values())


class DisconnectGate:
    """包住存档传输。断线（G464 / G466）后不再放行新的写步骤。

    写步骤进入时若已观察到断线，则拒绝；若此刻才首次观察到断线，则该步已登记为发送中，照发这 1 步；
    写步骤返回时若观察到断线，则该步是断线时在途的那 1 步。此后的写一律拒绝，读照常。
    """

    def __init__(self, inner, connected, on_disconnect_write):
        self.inner, self.connected, self.on_write = inner, connected, on_disconnect_write
        self.closed = False
        self.disconnect_write = False
        self.last_write_ok = False
        self.refused = 0
        self.created = None
        self.page_writes = []

    def _mark(self):
        self.closed = True
        if not self.disconnect_write:
            self.disconnect_write = True
            try:
                self.on_write()
            except Exception:
                pass  # 计数在收口时再写一次

    def request(self, method, path, *, query, body):
        write = method != "GET"
        if write:
            if self.closed:
                self.refused += 1
                raise A.ArchiveRejected
            if not self.connected():
                self._mark()
            self.last_write_ok = False
        reply = self.inner.request(method, path, query=query, body=body)
        if write:
            self.last_write_ok = True
            if method == "POST" and path == A._DOCS:
                self.created = reply["document"]["document_id"]
            elif (hit := _PAGE_WRITE.search(path)):
                self.page_writes.append(hit[1])
            if not self.connected():
                self._mark()
        return reply


@dataclass
class Job:
    row: object
    snapshot: dict
    purpose: str
    key: str


def _batch(material, purpose, key, version):
    policies = tuple(A.ArchivePolicy(p["kind"], tuple(
        {field: SourceExcerpt(text=e["text"], start=e["start"]) for field, e in group.items()}
        for group in p["groups"])) for p in material["policies"])
    return A.VerifiedArchiveBatch(key, str(version), date.fromisoformat(material["utc_date"]),
                                  material["country"], purpose, material["body"], policies,
                                  material["attachments_resolved"])


class ArchiveRunner:
    """每轮至多处理一张已同意的卡。同一「登记 × 版本」至多发起一次存档。"""

    def __init__(self, path, *, switches, config, sensitive, ledger, connected,
                 clock=time.time, factory=A._SDK):
        self.path, self.switches, self.config, self.sensitive = path, switches, config, tuple(sensitive)
        self.ledger, self.connected, self.clock, self.factory = ledger, connected, clock, factory
        self._purpose = None

    def run_once(self):
        if not self.connected():
            return False
        job = self._claim()
        if job is None:
            return False
        self._run(job)
        return True

    def _gate(self, j, row):
        """开始存档前只核一次授权。返回空串表示通过，否则为停批原因。"""
        try:
            switches = self.switches()
        except Exception:
            return "switch"
        self._purpose = switches.get(PURPOSE_KEY)
        if (switches.get("MAIL_TRIAL_CARD_ENABLED") != "true"
                or switches.get("MAIL_TRIAL_CREATE_ENABLED") != "true"):
            return "switch"
        if (row["format"] != CARD_FORMAT or not row["country"] or row["readonly"]
                or switches.get("MAIL_TRIAL_CREATE_COUNTRY") != row["country"]):
            return "country"
        if j.write_halted():
            return "halted"
        if not row["session"] or row["session"] != j.session() or self.clock() >= row["expires"]:
            return "consent"
        if trial_batch(j.db, row["registration"], row["version"])[3]:
            return "batch"
        if sha256(row["snapshot"].encode()).hexdigest() != row["fingerprint"]:
            return "material"
        receipts = {r[0] for r in j.db.execute("SELECT action FROM trial_receipt WHERE card=?", (row["card"],))}
        if not {"verify", "approve"} <= receipts:
            return "consent"
        snapshot = json.loads(row["snapshot"])
        material = snapshot.get("material")
        if (not snapshot.get("preview") or not isinstance(material, dict) or not snapshot.get("eligible")
                or material.get("country") != row["country"]):
            return "material"
        if switches.get(PURPOSE_KEY) not in ("X8a", "X8b"):
            return "unconfirmed"
        return ""

    def _claim(self):
        with journal_at(self.path) as j, j.db:
            j.db.execute("BEGIN IMMEDIATE")
            row = j.db.execute("""SELECT c.* FROM trial_card c WHERE c.status='accepted'
                AND EXISTS(SELECT 1 FROM trial_item_task t WHERE t.card=c.card AND t.status='archive_pending')
                AND NOT EXISTS(SELECT 1 FROM trial_archive a WHERE a.registration=c.registration
                               AND a.version=c.version)
                ORDER BY c.rowid LIMIT 1""").fetchone()
            if row is None:
                return None
            code = self._gate(j, row)
            if code:
                self._stop(j, row, "unconfirmed" if code == "unconfirmed" else "failed", "gate", code)
                return None
            purpose = self._purpose
            reg, version, card = row["registration"], row["version"], row["card"]
            j.db.execute("INSERT INTO trial_archive(registration,version,card,status,purpose,started) "
                         "VALUES(?,?,?,'running',?,?)", (reg, version, card, purpose, self.clock()))
            j.db.execute("UPDATE trial_item_task SET status='archive_running' WHERE card=? AND status='archive_pending'",
                         (card,))
            j.db.execute("UPDATE trial_task SET status='archive_running' WHERE card=?", (card,))
            return Job(row, json.loads(row["snapshot"]), purpose, f"{reg}-v{version}")

    @staticmethod
    def _stop(j, row, kind, stage, reason):
        """整批停下：条目任务置终态、卡片关闭、登记固定群提示。调用方持有事务。"""
        card, reg, version = row["card"], row["registration"], row["version"]
        unknown = kind == "unknown"
        state = "archive_unknown" if unknown else "closed"
        j.db.execute("UPDATE trial_item_task SET status=? WHERE card=? AND status!='closed'", (state, card))
        j.db.execute("UPDATE trial_task SET status=? WHERE card=? AND status!='closed'", (state, card))
        j.db.execute("UPDATE trial_card SET status='closed' WHERE card=?", (card,))
        j.db.execute("UPDATE trial_notification SET status='cancelled' WHERE card=? AND kind='remind' AND status='pending'",
                     (card,))
        if j.db.execute("SELECT 1 FROM trial_archive WHERE registration=? AND version=?", (reg, version)).fetchone():
            j.db.execute("UPDATE trial_archive SET status=?, stage=?, reason=?, finished=? "
                         "WHERE registration=? AND version=?",
                         ("unknown" if unknown else "failed", stage, reason, time.time(), reg, version))
        else:
            j.db.execute("INSERT INTO trial_archive(registration,version,card,status,stage,reason,finished) "
                         "VALUES(?,?,?,'failed',?,?,?)", (reg, version, card, stage, reason, time.time()))
        j.db.execute("INSERT OR IGNORE INTO trial_notification VALUES(?,?,?,?,'pending')", (
            card, "archive_unknown" if unknown else "archive_failed", _uuid(),
            f"登记 {reg} · 版本 {version} · {ARCHIVE_NOTICES[kind]}"))

    def _run(self, job):
        row, snapshot = job.row, job.snapshot
        reg, version = row["registration"], row["version"]
        gate = batch = preview = None
        try:
            batch = _batch(snapshot["material"], job.purpose, job.key, version)
            preview = A.preview_archive_batch(batch, config=self.config, sensitive_values=self.sensitive)
            if preview.fingerprint != snapshot["preview"]:
                raise ValueError("preview")
            A.register_archive_batch(batch, ledger_path=self.ledger, approved_country=row["country"],
                                     email_verified=True, owner_release=job.purpose, expires=row["expires"],
                                     sensitive_values=self.sensitive, preview_fingerprint=snapshot["preview"])
            gate = DisconnectGate(self.factory(self.config), self.connected, lambda: self._count(reg, version))
        except Exception:
            # 授权登记或传输尚未建好，没有任何外部写。
            result = A.ArchiveFailed("prepare", "register")
        else:
            try:
                result = A.archive_batch(batch, config=self.config, ledger_path=self.ledger, transport=gate,
                                         sensitive_values=self.sensitive)
            except Exception:
                result = A.ArchiveUnknown("run", "exception")
        for attempt in range(3):
            try:
                self._finish(job, result, gate, batch, preview)
                return
            except Exception:
                _LOG.error("存档结果登记未成功，重试。")
                time.sleep(0.2)
        _LOG.error("存档结果登记失败，保留进行中记录，转人工。")

    def _count(self, reg, version):
        """断线时在途那 1 个写步骤：先单独计数，再放行。"""
        with journal_at(self.path) as j, j.db:
            j.db.execute("UPDATE trial_archive SET disconnect_writes=1 WHERE registration=? AND version=?",
                         (reg, version))

    def _confirmed(self, batch, gate):
        """只读核对在途步骤：本步请求正常返回，且存档执行记录的收据与步数一致、未冻结。"""
        try:
            with A._database(A._journal(batch.registration)) as db:
                run = db.execute("SELECT state, step FROM run WHERE id=1").fetchone()
                receipts = db.execute("SELECT count(*) FROM receipts").fetchone()[0]
            return bool(gate.last_write_ok and run["state"] != "unknown" and receipts == run["step"])
        except Exception:
            return False

    def _finish(self, job, result, gate, batch, preview):
        row = job.row
        reg, version, card = row["registration"], row["version"], row["card"]
        disconnected = bool(gate and gate.disconnect_write)
        confirmed = self._confirmed(batch, gate) if disconnected else False
        with journal_at(self.path) as j, j.db:
            j.db.execute("BEGIN IMMEDIATE")
            running = j.db.execute("SELECT 1 FROM trial_archive WHERE registration=? AND version=? AND status='running'",
                                   (reg, version)).fetchone()
            if gate is not None and preview is not None:
                kinds = [k for k in A._ROUTES if k in dict(preview.summaries)]
                summaries = dict(preview.summaries)
                pages = [t for t in gate.page_writes if t != gate.created]
                for kind, page in zip(kinds, pages):
                    j.db.execute("INSERT OR REPLACE INTO trial_archive_page VALUES(?,?,?,?,?)",
                                 (reg, version, kind, page, summaries[kind]))
            if disconnected:
                j.db.execute("UPDATE trial_archive SET disconnect_writes=1 WHERE registration=? AND version=?",
                             (reg, version))
            if running:
                if isinstance(result, A.ArchiveSuccess):
                    open_card = j.db.execute("SELECT 1 FROM trial_card WHERE card=? AND status='accepted'",
                                             (card,)).fetchone()
                    j.db.execute("""UPDATE trial_archive SET status='success', url=?, stage='done', finished=?,
                                 card_update=? WHERE registration=? AND version=?""",
                                 (result.url, time.time(), "pending" if open_card else "none", reg, version))
                    j.db.execute("UPDATE trial_item_task SET status='archive_done' WHERE card=? AND status='archive_running'",
                                 (card,))
                    j.db.execute("UPDATE trial_task SET status='archive_done' WHERE card=? AND status='archive_running'",
                                 (card,))
                else:
                    kind = "unknown" if isinstance(result, A.ArchiveUnknown) else "failed"
                    self._stop(j, row, kind, result.stage, result.reason_code)
            elif isinstance(result, A.ArchiveSuccess):
                j.db.execute("UPDATE trial_archive SET url=? WHERE registration=? AND version=?",
                             (result.url, reg, version))
            if disconnected:
                j.db.execute("INSERT OR IGNORE INTO trial_notification VALUES(?,'archive_disconnect',?,?,'pending')",
                             (card, _uuid(), f"登记 {reg} · 版本 {version} · "
                              + DISCONNECT_NOTICES[0 if confirmed else 1]))


def _uuid():
    from uuid import uuid4
    return str(uuid4())


def build_runner(args, settings, state, handoff_path, *, switches, connected, sensitive):
    """未确认配置下不装配：出卡与建单开关须开，业务配置、凭据来源与存档授权库均已就绪。"""
    import os
    try:
        if (switches().get("MAIL_TRIAL_CARD_ENABLED") != "true"
                or switches().get("MAIL_TRIAL_CREATE_ENABLED") != "true"):
            return None
        if args.business_env_file is None or not settings.trial_credentials_complete:
            return None
        ledger = handoff_path.with_name(LEDGER_NAME)
        info = ledger.lstat()
        if not S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return None
        from qianshu.mail.trial import business_config
        config = business_config(args.business_env_file)
        if not all(config.get(key) for key in _REQUIRED):
            return None
    except Exception:
        return None
    return ArchiveRunner(state, switches=switches, config=config, sensitive=sensitive, ledger=ledger,
                         connected=connected)


@contextmanager
def archive_worker(runner, *, interval=0.5, join=1.2):
    if runner is None:
        yield None
        return
    stop = threading.Event()

    def loop():
        while not stop.wait(interval):
            try:
                runner.run_once()
            except Exception:
                _LOG.error("存档执行器本轮未完成，下一轮重试。")

    thread = threading.Thread(target=loop, name="trial-archive", daemon=True)
    thread.start()
    try:
        yield runner
    finally:
        stop.set()
        # 断线时在途的那 1 步在此期限内收口；超时则由关闭流程按「结果未知」冻结。
        thread.join(join)
