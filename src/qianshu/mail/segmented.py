"""试运行的一轮只读分段响应。正文选择和解码由旧收信入口完成。"""

import hashlib
import json
import re
from email.errors import MessageError

from . import _fingerprint
from .imap import MailError
from .message import body_parts, headers, message_id, request_text, sender
from .triage import MailItem, triage_fields


def _frame(digest, value):
    if isinstance(value, (tuple, list)):
        digest.update(b"L" + len(value).to_bytes(8, "big"))
        for element in value:
            _frame(digest, element)
    else:
        raw = value if isinstance(value, bytes) else value.encode("utf-8", "surrogatepass")
        digest.update(b"B" + len(raw).to_bytes(8, "big") + raw)


class _Read:
    def __init__(self, mailbox, uid, seen):
        self.mailbox, self.uid, self.seen = mailbox, uid, seen
        self.tree = None
        self.mime = {}
        self.payloads = {}
        self.selected = []

    def fetch(self, section):
        raw = self.mailbox.checked_section(self.uid, section)
        self.mailbox.check_seen(self.uid, self.seen)
        return raw

    def structure(self, uid):
        if self.tree is None:
            self.tree = self.mailbox.structure(uid)
            self.mailbox.check_seen(uid, self.seen)
        return self.tree

    def payload(self, path):
        raw = self.fetch(path)
        self.payloads[path] = (str(len(raw)), hashlib.sha256(raw).digest())
        return raw

    def section(self, uid, section):
        if section.endswith(".MIME"):
            if section not in self.mime:
                self.mime[section] = self.fetch(section)
            return self.mime[section]
        self.selected.append(section)
        return self.payload(section)


def _params(value):
    if value == b"NIL":
        return {}
    if not isinstance(value, list) or len(value) % 2:
        raise MailError("邮件结构参数无效。")
    result = {}
    for key, val in zip(value[::2], value[1::2]):
        key = key.upper()
        if key in result or not isinstance(val, bytes):
            raise MailError("邮件结构参数不唯一。")
        result[key] = val
    return result


def _named(params, key):
    # RFC 2231 的扩展名或分片同样表示文件名。
    return any(re.fullmatch(key + rb"(?:\*(?:[0-9]+\*?)?)?", name) for name in params)


def _attached(disposition):
    if disposition == b"NIL":
        return False
    if (not isinstance(disposition, list) or len(disposition) != 2
            or not isinstance(disposition[0], bytes) or not disposition[0]):
        return None
    if disposition[0].upper() != b"INLINE":
        return True
    try:
        return _named(_params(disposition[1]), b"FILENAME")
    except MailError:
        return None


def _related_root(children, params, read, prefix):
    start = params.get(b"START")
    if start is None:
        return 1
    matches = []
    for i, child in enumerate(children, 1):
        content_id = None if isinstance(child[0], list) else child[3]
        if content_id in (None, b"NIL"):
            values = headers(read.section(read.uid, f"{prefix}{i}.MIME")).get_all("Content-ID", [])
            if len(values) > 1:
                raise MailError("根段标识不唯一。")
            content_id = str(values[0]).strip().encode() if values else None
        if content_id is not None and content_id.strip() == start.strip():
            matches.append((i, content_id))
    if len(matches) != 1 or matches[0][1] != start:
        raise MailError("无法核实内嵌资源的根段。")
    return matches[0][0]


def _original_proof(outer, part):
    # 单文本叶可用旧函数已解出的文字和原 charset 还原 v1 的负载分帧。
    # 旧摘要不符或内容不能无损表示时，不会补入候选索引。
    kind, text = part
    try:
        payload = text.encode(outer.get_content_charset() or "ascii")
    except (LookupError, UnicodeError):
        return None
    fields = [(k.lower(), str(v)) for k, v in outer.items() if k.lower().startswith("content-")
              and k.lower() not in {"content-type", "content-transfer-encoding"}]
    params = [(k, v) for k, v in (outer.get_params() or [])[1:] if k.lower() != "boundary"]
    proof = hashlib.sha256(b"mime-v1")
    for raw in (json.dumps([kind, params, fields]).encode(), payload):
        proof.update(len(raw).to_bytes(8, "big"))
        proof.update(raw)
    return "1:" + proof.hexdigest()


def read_segmented(mailbox, uid, seen, *, original=False):
    read = _Read(mailbox, uid, seen)
    complete = False
    try:
        raw = read.fetch("HEADER")
        if not raw.endswith((b"\r\n\r\n", b"\n\n")):
            raise MailError("邮件头未完整结束。")
        outer = headers(raw)
        if outer.defects:
            raise MailError("邮件头不可核实。")
        parts = body_parts(read, uid, raw)
        chosen = dict(zip(read.selected, parts, strict=True))
        digest = hashlib.sha256(b"segmented-v2")
        _frame(digest, [(name.lower(), re.sub(r"\r?\n(?=[ \t])", "", val))
                        for name, val in outer.raw_items() if name.lower() not in
                        {"received", "return-path", "delivered-to", "message-id"}])
        _frame(digest, read.tree)
        unknown_count = False
        visible = set()
        selected_paths = {""} if chosen else set()
        for path in chosen:
            while path:
                selected_paths.add(path)
                path = path.rpartition(".")[0]

        def visit(node, path="", resource=False):
            nonlocal unknown_count
            section = path or "TEXT"
            mime = headers(read.section(uid, path + ".MIME")) if path else outer
            multi = isinstance(node[0], list)
            if multi:
                children = []
                for child in node:
                    if not isinstance(child, list):
                        break
                    children.append(child)
                n = len(children)
                params = _params(node[n + 1]) if len(node) > n + 1 else {}
                disp_index = n + 2
            else:
                if len(node) < 7 or not node[6].isdigit():
                    raise MailError("无法核实邮件段长度。")
                params = _params(node[2])
                disp_index = 8
                if node[0].upper() == b"TEXT":
                    disp_index = 9
                elif node[0].upper() == b"MESSAGE" and node[1].upper() == b"RFC822":
                    disp_index = 11
            disp = node[disp_index] if len(node) > disp_index else b"NIL"
            named = _named(params, b"NAME") or mime.get_filename() is not None
            disposition = _attached(disp)
            attached = mime.get_content_disposition() not in (None, "inline") or disposition is True
            uncertain = disposition is None and not (resource or named or attached) and (multi or section in chosen)
            unknown_count |= uncertain
            if (multi or section in chosen) and any(len(mime.get_all(name, [])) > 1 for name in
                                                    ("Content-Type", "Content-Disposition", "Content-Transfer-Encoding")):
                raise MailError("正文或容器的 MIME 解释字段不唯一。")
            opaque = resource or named or attached or uncertain
            if multi:
                prefix = path + "." if path else ""
                if not opaque or path in selected_paths:
                    root = _related_root(children, params, read, prefix) if node[n].upper() == b"RELATED" else None
                    count = 0
                    for i, child in enumerate(children, 1):
                        child_path = f"{prefix}{i}"
                        nonroot = root is not None and i != root
                        if nonroot and child_path in selected_paths:
                            raise MailError("已读段与根绑定不符。")
                        # 不透明容器只复核旧函数实际读出的后代，整棵子树计一。
                        if not opaque or child_path in selected_paths:
                            count += visit(child, child_path, opaque or nonroot)
                    if not opaque:
                        return count
            if section not in read.payloads:
                read.payload(section)
            if not multi and int(node[6]) != int(read.payloads[section][0]):
                raise MailError("邮件段长度与结构不符。")
            if section in chosen and not opaque:
                visible.add(section)
                return 0
            return 1

        attachments = visit(read.tree)
        if unknown_count:
            attachments = None
        if "TEXT" not in read.payloads:
            read.payload("TEXT")
        for path, header in sorted(read.mime.items()):
            _frame(digest, (path, header))
        for path, fact in sorted(read.payloads.items()):
            _frame(digest, (path, fact))
        for path, part in chosen.items():
            _frame(digest, (path, part))
        text = request_text(parts)
        preferred = "text/plain" if any(k == "text/plain" and s.strip() for k, s in parts) else "text/html"
        fields = triage_fields(outer, (part for path, part in chosen.items() if path in visible), preferred)
        candidate = "2:" + digest.hexdigest()
        _frame(digest, [(k.lower(), v) for k, v in outer.raw_items() if k.lower() == "message-id"])
        key = message_id(outer)
        item = MailItem(_fingerprint(key) if key else None, "2:" + digest.hexdigest(),
                        candidate, attachments, fields)
        if original and not isinstance(read.tree[0], list) and len(parts) == 1:
            prior = _original_proof(outer, parts[0])
            if prior:
                item.original = (prior, _fingerprint(sender(outer), fields["subject"], prior))
        complete = True
        return item, text
    except (AttributeError, IndexError, LookupError, MessageError, ValueError, TypeError, RecursionError, AssertionError):
        raise MailError("分段响应不可核实。") from None
    finally:
        try:
            mailbox.check_seen(uid, seen)
        except MailError:
            if complete:
                raise
