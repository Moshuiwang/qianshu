"""试运行的一轮只读分段响应。正文选择和解码由旧收信入口完成。"""

import hashlib
import re
from email import policy
from email.errors import MessageError
from email.parser import BytesParser
from html.parser import HTMLParser

from . import _fingerprint
from .imap import MailError, _number
from .message import _plain_text, body_parts, headers, message_id, request_text
from .triage import MailItem, has_body, triage_fields


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


class _AltText(HTMLParser):
    visible = False

    def handle_starttag(self, tag, attrs):
        if any(name == "alt" and value and has_body(value) for name, value in attrs):
            self.visible = True


def _visible_text(kind, value):
    if has_body(_plain_text(kind, value)):
        return True
    if kind == "text/html":
        parser = _AltText()
        parser.feed(value)
        parser.close()
        return parser.visible
    return False


def _inline_segments(part, node):
    """按内封原件与结构逐段核对；边界不明与未读文本同样阻断整批。"""
    if part.defects or not isinstance(node, list) or not node:
        raise ValueError
    if any(len(part.get_all(key, [])) > 1 for key in
           ("Content-Type", "Content-Disposition", "Content-Transfer-Encoding")):
        raise ValueError
    if part.get_content_disposition() not in (None, "inline") or part.get_filename() is not None:
        return 0
    kind = part.get_content_type()
    if isinstance(node[0], list):
        children = list(part.iter_parts())
        n = len(children)
        if not n or kind != "multipart/" + node[n].decode().lower():
            raise ValueError
        return sum(_inline_segments(child, sub) for child, sub in zip(children, node[:n], strict=True))
    if kind != b"/".join(node[:2]).decode().lower():
        raise ValueError
    if kind == "message/rfc822":
        if node[5].upper() not in {b"7BIT", b"8BIT", b"BINARY"}:
            raise ValueError
        inner, = part.get_payload()
        return _inline_segments(inner, node[8])
    if kind in {"text/plain", "text/html"}:
        value = part.get_content(errors="strict")
        if part.defects or "\x00" in value:
            raise ValueError
        return int(_visible_text(kind, value))
    return 0


def _inline_uncovered(raw, node):
    try:
        if node[5].upper() not in {b"7BIT", b"8BIT", b"BINARY"}:
            return True
        inner = BytesParser(policy=policy.default).parsebytes(raw)
        return bool(_inline_segments(inner, node[8]))
    except (AttributeError, IndexError, LookupError, MessageError, TypeError, ValueError, RecursionError):
        return True


def _request_body(chosen, groups):
    """旧请求独立生成；逐段判可见后才按纯文本叶子的替代组折叠。"""
    text = ""
    source_kind = None
    for kind in ("text/plain", "text/html"):
        parts = tuple(part for part in chosen.values() if part[0] == kind)
        text = request_text(parts)
        if text:
            source_kind = kind
            break
    required, covered = set(), set()
    for path, group in groups.items():
        kind, value = chosen[path]
        if _visible_text(kind, value):
            required.add(group)
            if text and kind == source_kind and has_body(_plain_text(kind, value)):
                covered.add(group)
    # 无文本也无请求时，沿用空正文的人工出口；纯附件保留附件提示。
    return text, len(required) > 1 or required != covered or (not required and bool(text))


def read_segmented(mailbox, uid, seen):
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
        unverified_body = False
        visible = set()
        groups = {}
        selected_paths = {""} if chosen else set()
        for path in chosen:
            while path:
                selected_paths.add(path)
                path = path.rpartition(".")[0]

        def visit(node, path="", resource=False, attachment=False):
            nonlocal unknown_count, unverified_body
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
                if len(node) < 7:
                    raise MailError("无法核实邮件段长度。")
                octets = _number(node[6], zero=True)
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
            attachment = attachment or attached
            uncertain = disposition is None and not (resource or named or attached) and (multi or section in chosen)
            unknown_count |= uncertain
            ambiguous = any(len(mime.get_all(name, [])) > 1 for name in
                            ("Content-Type", "Content-Disposition", "Content-Transfer-Encoding"))
            if (multi or section in chosen) and ambiguous:
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
                            count += visit(child, child_path, opaque or nonroot, attachment)
                    if not opaque and node[n].upper() == b"ALTERNATIVE":
                        paths = [f"{prefix}{i}" for i in range(1, n + 1)]
                        if any(isinstance(child[0], list) for child in children):
                            unverified_body = True
                        elif all(p in visible for p in paths):
                            groups.update(dict.fromkeys(paths, path))
                    if not opaque:
                        return count
            if section not in read.payloads:
                raw_part = read.payload(section)
                if (not multi and not (resource or named or attachment)
                        and node[0].upper() == b"MESSAGE" and node[1].upper() == b"RFC822"):
                    unverified_body |= (ambiguous or disposition is None or mime.get_content_type() != "message/rfc822"
                                        or _inline_uncovered(raw_part, node))
            if not multi and str(octets) != read.payloads[section][0]:
                raise MailError("邮件段长度与结构不符。")
            if section in chosen and not attachment:
                groups[section] = section
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
        text, body_missed = _request_body(chosen, groups)
        preferred = "text/plain" if any(k == "text/plain" and has_body(s) for k, s in parts) else "text/html"
        visible_parts = [part for path, part in chosen.items() if path in visible]
        fields = triage_fields(outer, visible_parts, preferred)
        candidate = "2:" + digest.hexdigest()
        _frame(digest, [(k.lower(), v) for k, v in outer.raw_items() if k.lower() == "message-id"])
        key = message_id(outer)
        item = MailItem(_fingerprint(key) if key else None, "2:" + digest.hexdigest(),
                        candidate, attachments, fields, body_missed or unverified_body)
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
