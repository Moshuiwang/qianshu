"""邮件头关联与 MIME 正文提取。"""

import re
from collections.abc import Callable, Iterator
from email import policy
from email.errors import MessageError
from email.message import EmailMessage
from email.parser import BytesHeaderParser, BytesParser
from html.parser import HTMLParser

from .imap import MailError, ReadOnlyMailbox

_HEADERS = BytesHeaderParser(policy=policy.default)
_PARSER = BytesParser(policy=policy.default)
_MESSAGE_ID = re.compile(r"<[!-;=?-~]+>")
_QUOTE_PREFIXES = (">", "＞")
_ATTRIBUTION = re.compile(r"(?:On .+ wrote\s*:|.+写道\s*[:：])", re.IGNORECASE)
_ORIGINAL_MESSAGE = re.compile(
    r"-{2,}\s*(?:original message|forwarded message|原始邮件|转发邮件)\s*-{2,}",
    re.IGNORECASE,
)


def headers(raw: bytes) -> EmailMessage:
    return _HEADERS.parsebytes(raw)


def sender(message: EmailMessage) -> str:
    fields = message.get_all("From", [])
    if len(fields) != 1 or fields[0].defects or len(fields[0].addresses) != 1:
        return ""
    address = fields[0].addresses[0]
    return address.addr_spec if address.username and address.domain else ""


def message_id(message: EmailMessage) -> str | None:
    fields = message.get_all("Message-ID", [])
    if len(fields) == 1 and _MESSAGE_ID.fullmatch(str(fields[0])):
        return str(fields[0])
    return None


def is_reply(message: EmailMessage, request_id: str) -> bool:
    direct = message.get_all("In-Reply-To", [])
    if direct:
        return len(direct) == 1 and str(direct[0]).strip() == request_id
    references = message.get_all("References", [])
    if len(references) != 1:
        return False
    chain = _MESSAGE_ID.findall(str(references[0]))
    return bool(chain) and chain[-1] == request_id


def _text_sections(
    node: list, read_mime: Callable[[str], EmailMessage], section: str = "",
) -> Iterator[str]:
    if not node:
        raise MailError("IMAP 返回了空邮件结构。")
    if isinstance(node[0], list):
        parts = []
        for item in node:
            if not isinstance(item, list):
                break
            parts.append(item)
        # multipart 扩展字段顺序为参数、Content-Disposition、语言、位置。
        disposition_index = len(parts) + 2
        if disposition_index < len(node) and isinstance(node[disposition_index], list):
            if node[disposition_index][0].upper() == b"ATTACHMENT":
                return
        if len(node) <= len(parts) or not isinstance(node[len(parts)], bytes):
            raise MailError("IMAP 返回的多段邮件结构无效。")
        if node[len(parts)].upper() == b"RELATED":
            # related 的正文是 start 指定的根段，其余段是内嵌资源。
            params = node[len(parts) + 1] if len(node) > len(parts) + 1 else None
            start = None
            if isinstance(params, list):
                start = next((value for key, value in zip(params[::2], params[1::2])
                              if key.upper() == b"START"), None)
            for index, part in enumerate(parts, 1):
                child = f"{section}.{index}" if section else str(index)
                if start is None:
                    yield from _text_sections(part, read_mime, child)
                    return
                content_id = part[3] if len(part) > 3 and not isinstance(part[0], list) else None
                if content_id in {None, b"NIL"}:
                    values = read_mime(child).get_all("Content-ID", [])
                    content_id = str(values[0]).strip().encode("utf-8") if len(values) == 1 else None
                if content_id == start:
                    yield from _text_sections(part, read_mime, child)
                    return
            raise MailError("IMAP 未返回内嵌资源邮件的正文根段。")
        for index, part in enumerate(parts, 1):
            child = f"{section}.{index}" if section else str(index)
            yield from _text_sections(part, read_mime, child)
    elif len(node) >= 2 and node[0].upper() == b"TEXT":
        if node[1].upper() in {b"PLAIN", b"HTML"}:
            yield section


class _HTMLText(HTMLParser):
    _BREAKS = {"br", "div", "p", "li", "tr", "td", "hr", "blockquote"}
    _QUOTE_CLASSES = {"gmail_quote", "gmail_quote_container", "yahoo_quoted"}

    def __init__(self, *, authored_only: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.segments: list[str] = []
        self.text: list[str] = []
        self.authored_only = authored_only
        self.excluded_tag: str | None = None
        self.excluded_depth = 0

    def handle_starttag(self, tag, attrs):
        if self.excluded_tag is not None:
            if tag == self.excluded_tag:
                self.excluded_depth += 1
            return
        if self.authored_only:
            classes = (dict(attrs).get("class") or "").split()
            quoted = tag == "blockquote" or not self._QUOTE_CLASSES.isdisjoint(classes)
            if quoted:
                text = "".join(self.text).rstrip()
                previous, _, last_line = text.rpartition("\n")
                self.segments.append(previous if _ATTRIBUTION.fullmatch(last_line.strip()) else text)
                self.text.clear()
            if quoted or tag in {"script", "style"}:
                self.excluded_tag = tag
                self.excluded_depth = 1
                self.text.append("\n")
                return
        if tag in self._BREAKS:
            self.text.append("\n")

    def handle_endtag(self, tag):
        if self.excluded_tag is not None:
            if tag == self.excluded_tag:
                self.excluded_depth -= 1
                if self.excluded_depth == 0:
                    self.excluded_tag = None
            return
        if tag in self._BREAKS:
            self.text.append("\n")

    def handle_data(self, text):
        if self.excluded_tag is None:
            self.text.append(text)

    def get_text(self) -> str:
        return "\n".join([*self.segments, "".join(self.text)])


def _plain_text(kind: str, content: str) -> str:
    if kind == "text/plain":
        return content
    parser = _HTMLText()
    parser.feed(content)
    parser.close()
    return parser.get_text()


def body_parts(mailbox: ReadOnlyMailbox, uid: int, raw_headers: bytes) -> tuple[tuple[str, str], ...]:
    """只读取正文用的文本段，跳过文件附件、转发邮件及内嵌资源。"""
    mime_sections: dict[str, bytes] = {"": raw_headers}

    def read_mime(section: str) -> EmailMessage:
        if section not in mime_sections:
            mime_sections[section] = mailbox.section(uid, f"{section}.MIME")
        return headers(mime_sections[section])

    parts = []
    try:
        for section in _text_sections(mailbox.structure(uid), read_mime):
            mime = read_mime(section)
            kind = mime.get_content_type()
            if (
                kind not in {"text/plain", "text/html"}
                or mime.get_content_disposition() not in {None, "inline"}
                or mime.get_filename() is not None
            ):
                continue
            raw_body = mailbox.section(uid, section or "TEXT")
            raw_mime = mime_sections[section]
            part = _PARSER.parsebytes(raw_mime.rstrip(b"\r\n") + b"\r\n\r\n" + raw_body)
            decoded = part.get_content(errors="strict")
            if part.defects or "\x00" in decoded:
                raise MailError("邮件正文编码无效。")
            parts.append((kind, decoded))
    except (AttributeError, IndexError, LookupError, MessageError, TypeError, ValueError):
        raise MailError("无法解析邮件正文。") from None
    return tuple(parts)


def request_text(parts: tuple[tuple[str, str], ...]) -> str:
    plain = [text for kind, text in parts if kind == "text/plain" and text.strip()]
    return "\n".join(plain or [_plain_text(kind, text) for kind, text in parts]).strip()


def authored_reply(kind: str, content: str) -> str | None:
    """提取本次回复文字。无法确认引文边界时返回 None。"""
    if kind == "text/html":
        parser = _HTMLText(authored_only=True)
        try:
            parser.feed(content)
            parser.close()
        except (AssertionError, ValueError):
            return None
        if parser.excluded_tag is not None:
            return None
        content = parser.get_text()

    lines = content.strip().splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(_QUOTE_PREFIXES) or _ORIGINAL_MESSAGE.fullmatch(stripped):
            return None
        if _ATTRIBUTION.fullmatch(stripped):
            # 只有引述人行后全是连续的行首引用时，才确认引文边界。
            quoted = lines[index + 1:]
            if quoted and all(item.lstrip().startswith(_QUOTE_PREFIXES) for item in quoted):
                return "\n".join(lines[:index]).strip()
            return None
    return "\n".join(lines)
