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


def ordinary_purpose(switches):
    """普通申请入口可用的存档发布用途：只认 X8b（首批真实明细首次发布），其余（含 X8a、未登记）返回 None。

    X8a（【测试】单批次合成试写）不得用于普通申请：它只是一次合成试写确认，不授权发布真实申请明细。
    段 3 接入点：X8a 只能经【测试】单的一次性测试登记放行——由该入口核对 X8a 绑定的测试登记及其版本，
    并在认领本批的同一事务内一次性消费该绑定（见 ArchiveRunner._claim）。本片未实现，普通入口一律拒绝 X8a。
    """
    return "X8b" if switches.get(PURPOSE_KEY) == "X8b" else None


class DisconnectGate:
    """包住存档传输。断线（G464 / G466）后不再放行新的写步骤。

    断线判序只在「写步骤登记为发送中」的边界做一次：存档执行在登记之后、发出之前调用 admit()。
    此刻仍未观察到断线，才证明登记先于断线，发给本步 1 张放行票；已观察到断线（含读请求期间观察到的），
    或判断本身出错（先后无法确认），一律不放行，本步不发。写请求只凭放行票发出，票用一次即失效。
    持票的那 1 步若在发出前或返回时观察到断线，即断线时在途的那 1 步：照发、单独计数，此后的写一律拒绝，读照常。
    """

    def __init__(self, inner, connected, on_disconnect_write):
        self.inner, self.connected, self.on_write = inner, connected, on_disconnect_write
        self.closed = False
        self.ticket = False
        self.disconnect_write = False
        self.last_write_ok = False
        self.refused = 0
        self.created = None
        self.page_writes = []

    def _up(self):
        try:
            return self.connected() is True
        except Exception:
            return False

    def _mark(self):
        self.closed = True
        if not self.disconnect_write:
            self.disconnect_write = True
            try:
                self.on_write()
            except Exception:
                pass  # 计数在收口时再写一次

    def admit(self):
        """写步骤已登记为发送中之后调用。返回假表示断线在先或先后无法确认，本步不得发出。"""
        if self.closed or not self._up():
            self.closed = True
            self.ticket = False
            self.refused += 1
            return False
        self.ticket = True
        return True

    def request(self, method, path, *, query, body):
        write = method != "GET"
        if write:
            if self.closed or not self.ticket:
                self.closed = True
                self.ticket = False
                self.refused += 1
                raise A.ArchiveRejected
            self.ticket = False
            if not self._up():
                self._mark()  # 登记先于断线的那 1 步：照发
            self.last_write_ok = False
        reply = self.inner.request(method, path, query=query, body=body)
        if write:
            self.last_write_ok = True
            if method == "POST" and path == A._DOCS:
                self.created = reply["document"]["document_id"]
            elif (hit := _PAGE_WRITE.search(path)):
                self.page_writes.append(hit[1])
            if not self._up():
                self._mark()
        elif not self._up():
            self.closed = True  # 读请求期间观察到断线：此后不再放行任何写步骤
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
        self._purpose = ordinary_purpose(switches)
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
        if ordinary_purpose(switches) is None:
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
            # 段 3 接入点：【测试】单的 X8a 一次性登记在此核对（绑定的测试登记与版本）并在本事务内消费。
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
        notice = {"unknown": "archive_unknown", "disconnected": "archive_stopped"}.get(kind, "archive_failed")
        j.db.execute("INSERT OR IGNORE INTO trial_notification VALUES(?,?,?,?,'pending')", (
            card, notice, _uuid(),
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
                if isinstance(result, A.ArchiveSuccess) and disconnected:
                    # 最后一个写步骤在断线期间返回成功：存档已建成，但断线后整批停下，不进入待建单（2d 不再消费）。
                    self._stop(j, row, "disconnected", "done", "disconnect")
                    j.db.execute("""UPDATE trial_archive SET status='success', url=?, card_update='none'
                                 WHERE registration=? AND version=?""", (result.url, reg, version))
                elif isinstance(result, A.ArchiveSuccess):
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
def archive_worker(runner, *, interval=0.5, join=1.2, orders=None, dedupe=None):
    """同一线程按扫描节奏先存档、再建单，最后处理一条独立的只读查重。"""
    if runner is None and dedupe is None:
        yield None
        return
    stop = threading.Event()

    def loop():
        while not stop.wait(interval):
            if runner is not None:
                try:
                    runner.run_once()
                except Exception:
                    _LOG.error("存档执行器本轮未完成，下一轮重试。")
            if runner is not None and orders is not None:
                try:
                    orders.run_once()
                except Exception:
                    _LOG.error("建单执行器本轮未完成，下一轮重试。")

            if dedupe is not None:
                try:
                    dedupe()
                except Exception:
                    _LOG.error("查重登记读写未确认，请守望者核对。")

    thread = threading.Thread(target=loop, name="trial-archive", daemon=True)
    thread.start()
    try:
        yield runner
    finally:
        stop.set()
        # 断线时在途的那 1 步在此期限内收口；超时则由关闭流程按「结果未知」冻结。
        thread.join(join)
