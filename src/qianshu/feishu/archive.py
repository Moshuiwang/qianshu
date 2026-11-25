"""申请存档。授权登记由受信编排入口写入，邮件与模型只提供原文材料。"""

import asyncio
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
from collections import Counter
from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from qianshu.config import LOGGABLE_CONFIG_KEYS, Config
from qianshu.draft import SourceExcerpt
from qianshu.logs import archive_request_fingerprint
from qianshu.pipeline import _CardRedaction

from .handoff import _CREDENTIAL_PATTERN, _file

_FIELDS = ("活动周期", "每个标签的用户数", "政策内容", "使用场景", "标签名称")
_ROUTES = {
    "定向政策": "FEISHU_ARCHIVE_TARGETED_SUMMARY_URL",
    "公开政策": "FEISHU_ARCHIVE_PUBLIC_SUMMARY_URL",
}
_SHARE = "FEISHU_ARCHIVE_LINK_SHARE_ENTITY"
# 独立于可传入的台账路径。每批一次性初始化，运行入口永远只打开已有状态。
_JOURNALS = Path(__file__).resolve().parents[3] / ".private" / "archive-journals"
_WAIT = 0.5
_PAGE = re.compile(r"https://(?:[a-z0-9-]+\.)?feishu\.cn/(wiki|docx)/([a-zA-Z0-9]+)")
_HEADER = re.compile(
    r"(?im)^(?P<header_prefix>[ \t>]*)(?:from|to|cc|bcc|subject|date|sent|thread-topic|thread-index|"
    r"sender|reply-to|message-id|in-reply-to|references|"
    r"received|return-path|delivered-to|mime-version|content-type|content-disposition|"
    r"content-transfer-encoding|content-id|content-language|authentication-results|dkim-signature|"
    r"resent-(?:from|to|cc|bcc|date|sender|message-id)|x-(?:mailer|original-to|forwarded-to)|"
    r"发件人|收件人|抄送|主题|发送时间)[ \t]*[:：]"
    r"[^\r\n]*(?:\r?\n(?:(?P=header_prefix)|(?:[ \t]*>[ ]?+)*)[ \t]+[^\r\n]*)*"
)
_QUOTE_PREFIX = re.compile(r"(?:[ \t]*>[ ]?)+")
_FIELD = re.compile(r"[!-9;-~]+:")  # RFC 5322 ftext
_ADDRESS = re.compile(r"(?i)https?://[^\s<>]+|[\w.+%-]+@[\w.-]+")
_DOCS = "/open-apis/docx/v1/documents"


@dataclass(frozen=True, repr=False)
class ArchivePolicy:
    kind: str
    # 同条政策中同一标签的五类证据保持一组；无标签的公开政策用明确为空的原文。
    groups: tuple[dict[str, SourceExcerpt], ...]


@dataclass(frozen=True, repr=False)
class VerifiedArchiveBatch:
    registration: str
    version: str
    utc_date: date
    country: str
    purpose: str  # X8a / X8b
    body: str
    policies: tuple[ArchivePolicy, ...]
    attachments_resolved: bool

    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self), default=str, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True, repr=False)
class ArchiveSuccess:
    url: str


@dataclass(frozen=True)
class ArchiveFailed:
    stage: str
    reason_code: str


@dataclass(frozen=True)
class ArchiveUnknown:
    stage: str
    reason_code: str


class ArchiveRejected(Exception):
    """传输层已确证拒绝且本步未生效。"""


class ArchiveTransport(Protocol):
    def request(self, method: str, path: str, *, query: dict, body: dict | None) -> dict: ...


class _SDK:
    def __init__(self, config):
        import lark_oapi as lark

        self.client = (lark.Client.builder().app_id(config["FEISHU_APP_ID"])
                       .app_secret(config["FEISHU_APP_SECRET"]).timeout(10)
                       .log_level(lark.LogLevel.ERROR).build())

    def request(self, method, path, *, query, body):
        from lark_oapi.core import AccessTokenType, HttpMethod
        from lark_oapi.core.model import BaseRequest

        req = (BaseRequest.builder().http_method(HttpMethod[method]).uri(path)
               .token_types({AccessTokenType.TENANT}).queries(list(query.items())).body(body).build())
        # 异步 SDK 传输使用 httpx，默认禁止重定向、重试。
        fingerprint = hashlib.sha256(json.dumps([method, path, query], sort_keys=True).encode()).hexdigest()
        token = archive_request_fingerprint.set(fingerprint)
        try:
            response = asyncio.run(self.client.arequest(req))
        finally:
            archive_request_fingerprint.reset(token)
        raw = response.raw
        if raw.status_code in (401, 403):
            raise ArchiveRejected
        payload = json.loads(raw.content)
        if raw.status_code != 200 or type(payload.get("code")) is not int or payload["code"] != 0:
            raise ValueError
        return payload["data"]


def _journal(registration):
    return _JOURNALS / hashlib.sha256(registration.encode()).hexdigest()


def _private_directory(path):
    info = path.lstat()
    if (path.absolute() != path.resolve() or not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid()):
        raise ValueError("存档登记须使用本账号的私有目录。")


@contextmanager
def _database(path):
    _private_directory(path.parent)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid() or info.st_nlink != 1):
        raise ValueError
    with closing(sqlite3.connect(
        path.absolute().as_uri() + "?mode=rw", uri=True, isolation_level=None,
    )) as db:
        db.row_factory = sqlite3.Row
        yield db


def _exclusive_file(path):
    _private_directory(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def initialize_archive_ledger(path: Path):
    """仅部署时首次初始化，已有台账不能覆盖。"""
    _exclusive_file(path)
    with _database(path) as db:
        db.execute("""CREATE TABLE grants (
            registration TEXT PRIMARY KEY, digest TEXT NOT NULL, country TEXT NOT NULL,
            verified INTEGER NOT NULL, release TEXT NOT NULL, expires REAL NOT NULL, registry TEXT NOT NULL,
            preview TEXT, version INTEGER NOT NULL DEFAULT 0, halted INTEGER NOT NULL DEFAULT 0
        )""")


def register_archive_batch(batch, *, ledger_path, approved_country, email_verified,
                           owner_release, expires, sensitive_values: tuple[str, ...],
                           preview_fingerprint: str | None = None):
    """仅供私有登记入口使用。核验、建单国家及当次 X8 确认来自可信记录。"""
    _JOURNALS.mkdir(mode=0o700, exist_ok=True, parents=True)
    path = _journal(batch.registration)
    with _database(ledger_path) as grants, grants:
        grants.execute("BEGIN IMMEDIATE")
        if grants.execute("SELECT 1 FROM grants WHERE registration=?", (batch.registration,)).fetchone():
            raise ValueError("该批已有授权登记，须核对原执行记录。")
        if "preview" not in {col["name"] for col in grants.execute("PRAGMA table_info(grants)")}:
            grants.execute("ALTER TABLE grants ADD COLUMN preview TEXT")
            grants.execute("ALTER TABLE grants ADD COLUMN version INTEGER NOT NULL DEFAULT 0")
            grants.execute("ALTER TABLE grants ADD COLUMN halted INTEGER NOT NULL DEFAULT 0")
        _exclusive_file(path)
        _exclusive_file(path.with_suffix(".lock"))
        with _database(path) as journal:
            journal.execute("""CREATE TABLE run (
                id INTEGER PRIMARY KEY CHECK(id=1), dev INTEGER, ino INTEGER,
                digest TEXT, plan TEXT, state TEXT, step INTEGER, stage TEXT,
                doc TEXT, reason TEXT, version INTEGER NOT NULL, registration TEXT NOT NULL)""")
            journal.execute("CREATE TABLE receipts (step INTEGER PRIMARY KEY, reference TEXT)")
            identity = ledger_path.stat()
            journal.execute("INSERT INTO run VALUES (1,?,?,?,'','ready',0,'prepare','','',0,?)",
                            (identity.st_dev, identity.st_ino, batch.fingerprint(), batch.registration))
        grants.execute("INSERT INTO grants VALUES (?,?,?,?,?,?,?,?,0,0)", (
            batch.registration, batch.fingerprint(), approved_country, email_verified is True,
            owner_release, expires, _registry_digest(sensitive_values), preview_fingerprint,
        ))


def _token(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9]+", value):
        raise ValueError
    return value


def _header_blocks(body):
    """在完整原文上定位连续头部，保留引文所用的字符坐标。"""
    start = None
    known = False
    end = 0
    field_prefix = ""
    for line in re.finditer(r"[^\r\n]*(?:\r\n|[\r\n])|[^\r\n]+$", body):
        raw = line[0].rstrip("\r\n")
        prefix = _QUOTE_PREFIX.match(raw)
        quote_prefix = prefix[0] if prefix else ""
        content = raw[len(quote_prefix):]
        known_field = _HEADER.match(raw)
        if known_field or _FIELD.match(content):
            if start is None:
                start = line.start()
            known |= bool(known_field)
            field_prefix = quote_prefix
        elif not (content.startswith((" ", "\t")) or raw.startswith((field_prefix + " ", field_prefix + "\t"))):
            if known:
                yield start, end
            start = None
            known = False
        end = line.start() + len(raw)
    if known:
        yield start, end


def _material(batch, config, registered):
    if not batch.policies or type(batch.utc_date) is not date or not batch.version or not batch.registration:
        raise ValueError
    if not re.fullmatch(r"[\w -]{1,80}", batch.country) or batch.attachments_resolved is not True:
        raise ValueError
    secrets = {v for v in registered if v} | {
        v for k, v in config.items() if k not in LOGGABLE_CONFIG_KEYS and k != _SHARE and v
    }
    patterns = [_HEADER.pattern.removeprefix("(?im)"), _ADDRESS.pattern.removeprefix("(?i)"),
                _CREDENTIAL_PATTERN.pattern]
    literals = {s for v in secrets for s in (v, json.dumps(v, ensure_ascii=False)[1:-1])}
    patterns.extend(map(re.escape, sorted(literals, key=len, reverse=True)))
    sensitive = re.compile("(?im:" + "|".join(patterns) + ")")
    fragments = {s[i:i+4] for s in literals for i in range(len(s)-3)}
    redact = _CardRedaction(batch.body, sensitive, fragments, source_spans=_header_blocks(batch.body))
    quotes = []
    layout = []
    for number, policy in enumerate(batch.policies, 1):
        if policy.kind not in _ROUTES or not policy.groups:
            raise ValueError
        for group, evidence in enumerate(policy.groups, 1):
            if set(evidence) != set(_FIELDS):
                raise ValueError
            for field in _FIELDS:
                quote = evidence[field]
                if (type(quote.start) is not int or quote.start < 0 or not quote.text.strip()
                        or not batch.body.startswith(quote.text, quote.start)):
                    raise ValueError
                quotes.append(redact.display(quote.text, quote.start))
                layout.append(f"政策 {number} · 标签 {group} · {field}　")
    # 保留坐标保护，并在全部摘录拼合后再查登记片段。来源为已知凭据的短引文也遮蔽。
    quotes = ["[已脱敏]" if any(q in s for s in literals) else q for q in quotes]
    safe = list(redact.free_texts(quotes))
    if any(not value.replace("[已脱敏]", "").strip() for value in safe):
        raise ValueError
    if (sensitive.search(batch.country) or any(f in batch.country for f in fragments)
            or any(batch.country in s for s in secrets)):
        raise ValueError
    blocks = []
    for label, value in zip(layout, safe, strict=True):
        line = label + value
        blocks.extend(line[i:i+1000] for i in range(0, len(line), 1000))
    day = batch.utc_date.strftime("%Y%m%d")
    marker = "【测试】" if batch.purpose == "X8a" else ""
    title = marker + "申请存档 " + day
    counts = Counter(p.kind for p in batch.policies)
    summaries = {kind: f"{marker}{day}　{batch.country}　{count} 条　" for kind, count in counts.items()}
    # 最终业务文本也须检查，覆盖加标签、分块及汇总行格式化产生的拼接。
    for text in (title, "".join(blocks), *summaries.values()):
        if sensitive.search(text) or any(text[i:i+4] in fragments for i in range(len(text)-3)):
            raise ValueError
    return title, blocks, summaries


@dataclass(frozen=True, repr=False)
class ArchivePreview:
    title: str
    blocks: tuple[str, ...]
    summaries: tuple[tuple[str, str], ...]

    @property
    def body(self):
        return "\n".join(self.blocks)

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self), ensure_ascii=False).encode()).hexdigest()


def preview_archive_batch(batch, *, config, sensitive_values=()) -> ArchivePreview:
    """生成待核对的正文段落和汇总行前缀。材料不完整时抛出 ValueError。"""
    title, blocks, summaries = _material(batch, config, sensitive_values)
    return ArchivePreview(title, tuple(blocks), tuple(summaries.items()))


def _targets(config, request):
    if config[_SHARE] != "anyone_readable":
        raise ValueError
    pages = {}
    for kind, key in _ROUTES.items():
        page = _PAGE.fullmatch(config[key])
        if not page:
            raise ValueError
        route, token = page.groups()
        if route == "wiki":
            node = request("GET", "/open-apis/wiki/v2/spaces/get_node", query={"token": token}, body=None)["node"]
            if node["obj_type"] != "docx":
                raise ValueError
            token = _token(node["obj_token"])
        else:
            doc = request("GET", f"{_DOCS}/{token}", query={}, body=None)["document"]
            if doc["document_id"] != token:
                raise ValueError
        pages[kind] = token
    if len(set(pages.values())) != len(pages):
        raise ValueError
    return pages


def _result(row):
    if row["state"] == "success":
        return ArchiveSuccess(_url(row["doc"]))
    if row["state"] == "failed":
        return ArchiveFailed(row["stage"], row["reason"])
    return ArchiveUnknown(row["stage"], row["reason"] or "unconfirmed")


def _url(doc):
    return "https://feishu.cn/docx/" + _token(doc)


def _row(db):
    row = db.execute("""SELECT run.*, grants.version AS expected, grants.halted
                        FROM run JOIN witness.grants USING (registration) WHERE id=1""").fetchone()
    if row is None or row["halted"]:
        raise sqlite3.DatabaseError("授权记录无法核对")
    # 仅恢复了较旧授权库时，完整的成功终态可回读；执行历史落后则一律冻结。
    cached_success = row["state"] == "success" and row["version"] > row["expected"]
    if row["version"] != row["expected"] and not cached_success:
        raise sqlite3.DatabaseError("执行版本不一致")
    return row


def _freeze(db):
    db.execute("""UPDATE run SET state='unknown', reason='unconfirmed'
                  WHERE state IN ('ready','sending')""")
    return _result(_row(db))


def _deny(db, reason):
    # 同批有写在途时不得声称明确失败，也不能让原调用继续交付链接。
    db.execute("""UPDATE run SET state=CASE state WHEN 'sending' THEN 'unknown' ELSE 'failed' END,
                  reason=? WHERE state IN ('ready','sending')""", (reason,))
    row = _row(db)
    if row["state"] == "success":
        return ArchiveFailed("prepare", reason)
    return _result(row)


def _append(request, doc, texts, key, url=None):
    children = []
    for text in texts:
        runs = [{"text_run": {"content": text}}]
        if url:
            runs.append({"text_run": {"content": "申请存档", "text_element_style": {"link": {"url": url}}}})
        children.append({"block_type": 2, "text": {"elements": runs}})
    data = request("POST", f"{_DOCS}/{doc}/blocks/{doc}/children",
                   query={"client_token": key}, body={"index": -1, "children": children})
    refs = [_token(b["block_id"]) for b in data["children"]]
    if len(set(refs)) != len(children) or len(refs) != len(children):
        raise ValueError
    return ",".join(refs)


def _registry_digest(values):
    return hashlib.sha256(json.dumps(sorted(set(values))).encode()).hexdigest()


def _authorized(grants, batch, digest, registry, preview=None):
    grant = grants.execute("SELECT * FROM grants WHERE registration=?", (batch.registration,)).fetchone()
    return (grant and grant["digest"] == digest and grant["registry"] == registry and grant["verified"] == 1
            and not grant["halted"] and (preview is None or grant["preview"] == preview)
            and grant["country"] == batch.country and grant["release"] == batch.purpose
            and batch.purpose in ("X8a", "X8b") and math.isfinite(grant["expires"])
            and time.time() < grant["expires"])


def _execute(db, grants, batch, config, digest, material, transport, registry):
    row = _row(db)
    if row["state"] != "ready":
        return _freeze(db) if row["state"] == "sending" else _result(row)
    request = (transport if transport is not None else _SDK(config)).request
    # 可选的放行核对：写步骤登记为发送中之后、发出之前调用；返回假则本步不发（试运行断线规则）。
    admit = getattr(transport, "admit", None)
    pages = _targets(config, request)
    title, blocks, summaries = material.title, material.blocks, dict(material.summaries)
    chunks = [blocks[i:i+40] for i in range(0, len(blocks), 40)]
    steps = [("create", None), *(("details", chunk) for chunk in chunks),
             ("share", None), ("verify", None), *(("summary", k) for k in pages if k in summaries)]
    plan = hashlib.sha256(json.dumps([pages, blocks, config["FEISHU_APP_ID"]]).encode()).hexdigest()
    if row["plan"] and row["plan"] != plan:
        return _freeze(db)
    preview = material.fingerprint
    doc = row["doc"]
    for index in range(row["step"], len(steps)):
        stage, arg = steps[index]
        if not _authorized(grants, batch, digest, registry, preview):
            db.execute("UPDATE run SET state='failed', reason='gate' WHERE state='ready'")
            return _result(_row(db))
        claim = db.execute("""UPDATE run SET state='sending', stage=?, plan=?
                              WHERE state='ready' AND step=?""", (stage, plan, index))
        if not claim.rowcount:
            return _result(_row(db))
        if admit is not None and stage != "verify" and not admit():
            # 登记之后才核对：此刻仍未观察到断线，才能证明登记先于断线；否则本步确证未发出。
            db.execute("UPDATE run SET state='failed', reason='rejected' WHERE state='sending'")
            return _result(_row(db))
        reference = ""
        key = str(uuid5(NAMESPACE_URL, f"{digest}/{index}"))
        perm = f"/open-apis/drive/v2/permissions/{doc}/public"
        try:
            if stage == "create":
                reply = request("POST", _DOCS, query={}, body={"title": title})
                doc = _token(reply["document"]["document_id"])
                if doc in pages.values():
                    raise ValueError
                reference = doc
            elif stage == "details":
                reference = _append(request, doc, arg, key)
            elif stage == "share":
                request("PATCH", perm, query={"type": "docx"}, body={
                    "link_share_entity": "anyone_readable", "external_access_entity": "open",
                })
            elif stage == "verify":
                try:
                    p = request("GET", perm, query={"type": "docx"}, body=None)["permission_public"]
                except ArchiveRejected:
                    return _freeze(db)
                if p["link_share_entity"] != "anyone_readable" or p["external_access_entity"] != "open":
                    raise ArchiveRejected
            else:
                reference = _append(request, pages[arg], [summaries[arg]], key, _url(doc))
        except ArchiveRejected:
            db.execute("UPDATE run SET state='failed', reason='rejected' WHERE state='sending'")
            return _result(_row(db))
        except Exception:
            return _freeze(db)
        db.execute("BEGIN IMMEDIATE")
        with db:
            changed = db.execute("""UPDATE run SET doc=?, step=?, state=?
                                    WHERE state='sending' AND step=?""", (
                doc, index+1, "success" if index+1 == len(steps) else "ready", index,
            ))
            if changed.rowcount:
                db.execute("INSERT INTO receipts VALUES (?,?)", (index, reference))
        if not changed.rowcount:
            break
    return _result(_row(db))


def archive_batch(batch: VerifiedArchiveBatch, *, config: Config, ledger_path: Path,
                  transport: ArchiveTransport | None = None, sensitive_values: tuple[str, ...] = ()
                  ) -> ArchiveSuccess | ArchiveFailed | ArchiveUnknown:
    """同步入口。额外的登记敏感值取自受信私有配置；结果只在成功时携带链接。"""
    path = None
    try:
        batch = deepcopy(batch)
        path = _journal(batch.registration)
        registry = _registry_digest(sensitive_values)
        with _database(ledger_path) as grants:
            if not path.exists():
                registered = grants.execute("SELECT 1 FROM grants WHERE registration=?",
                                            (batch.registration,)).fetchone()
                if registered:
                    raise sqlite3.DatabaseError("缺少执行记录")
                return ArchiveFailed("prepare", "missing_registration")
            with _database(path) as db, os.fdopen(_file(path.with_suffix(".lock")), "rb") as lock:
                identity = ledger_path.stat()
                binding = db.execute("SELECT dev, ino FROM run WHERE id=1").fetchone()
                if not binding or tuple(binding) != (identity.st_dev, identity.st_ino):
                    db.execute("UPDATE run SET state='unknown' WHERE state IN ('ready','sending')")
                    return ArchiveUnknown("ledger", "identity")
                # 状态与独立版本号通过 SQLite 多库事务一同提交。
                db.execute("ATTACH DATABASE ? AS witness", (ledger_path.absolute().as_uri() + "?mode=rw",))
                db.execute("""CREATE TEMP TRIGGER advance AFTER UPDATE OF state ON main.run BEGIN
                    SELECT CASE WHEN NOT EXISTS (
                        SELECT 1 FROM grants WHERE registration=OLD.registration AND version=OLD.version AND halted=0
                    ) THEN RAISE(ABORT, 'checkpoint') END;
                    UPDATE grants SET version=version+1 WHERE registration=OLD.registration;
                    UPDATE run SET version=version+1 WHERE id=NEW.id;
                END""")
                return _archive_registered(db, lock, grants, batch, config,
                                           transport, sensitive_values, registry)
    except (OSError, sqlite3.Error, ValueError, AttributeError, TypeError):
        if path is not None:
            # 两侧分别落冻结标记；任一文件暂时失联，另一侧仍能拦住迟到回执。
            for database, statement, args in (
                (path, "UPDATE run SET state='unknown', reason='unconfirmed' WHERE id=1", ()),
                (ledger_path, "UPDATE grants SET halted=1 WHERE registration=?", (batch.registration,)),
            ):
                try:
                    with _database(database) as available:
                        available.execute(statement, args)
                except (OSError, ValueError, sqlite3.Error):
                    continue
        return ArchiveUnknown("ledger", "unavailable")


def _archive_registered(db, lock, grants, batch, config, transport, sensitive_values, registry):
    row = _row(db)
    digest = batch.fingerprint()
    if digest != row["digest"] or not _authorized(grants, batch, digest, registry):
        return _deny(db, "gate")
    try:
        material = preview_archive_batch(batch, config=config, sensitive_values=sensitive_values)
        grant = grants.execute("SELECT preview FROM grants WHERE registration=?",
                               (batch.registration,)).fetchone()
        if not grant or grant["preview"] != material.fingerprint:
            return _deny(db, "preview")
    except (KeyError, TypeError, AttributeError, ValueError):
        return _deny(db, "material")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        deadline = time.monotonic() + _WAIT
        while time.monotonic() < deadline:
            row = _row(db)
            if row["state"] not in ("ready", "sending"):
                return _result(row)
            time.sleep(0.01)
        return _freeze(db)
    try:
        return _execute(db, grants, batch, config, digest, material, transport, registry)
    except (OSError, sqlite3.Error):
        return _freeze(db)
    except Exception:
        db.execute("UPDATE run SET state='failed', reason='prepare' WHERE state='ready'")
        return _freeze(db)
