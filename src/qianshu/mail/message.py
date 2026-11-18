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
_DIVIDER = re.compile(
    r"(?:-{4,}|(?:-{2,}\s*)?(?:original message|(?:begin )?forwarded message|原始邮件|转发邮件)"
    r"\s*(?:-{2,}|[:：])?)",
    re.IGNORECASE,
)
_HEADER_FIELD = re.compile(r"[^\W\d_][\w-]*\s*[:：]")
_UNKNOWN_DIVIDER = re.compile(r"(?:(?:[^\w\s]|_)\s*){3}")


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
    _AUTHORED_BREAKS = _BREAKS | {"h1", "h2", "h3", "h4", "h5", "h6", "pre", "th"}
    _BODY_TAGS = _AUTHORED_BREAKS | {
        "a", "b", "body", "em", "font", "head", "html", "i", "img", "meta", "ol",
        "s", "script", "span", "strong", "style", "table", "tbody", "thead", "u", "ul",
    }
    _VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr",
    }
    _QUOTED_CONTAINERS = {
        "gmail_quote", "gmail_quote_container", "yahoo_quoted",
        "netease_mail_quote", "netease-mail-quote", "divneteasemailquote",
    }
    _OUTLOOK_SEPARATOR_IDS = {"divrplyfwdmsg", "x_divrplyfwdmsg"}

    def __init__(self, *, authored_only: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.authored_only = authored_only
        self.breaks = self._AUTHORED_BREAKS if authored_only else self._BREAKS
        self.excluded: list[str] = []
        self.uncertain = False
        self.structured_quote = False
        self.tail: list[str] = []

    def handle_starttag(self, tag, attrs):
        if self.excluded:
            if tag not in self._VOID_TAGS:
                self.excluded.append(tag)
            return
        if self.authored_only:
            attributes = dict(attrs)
            identifiers = {
                token.casefold()
                for name, value in attrs if name in {"id", "class", "name"} and value
                for token in value.split()
            }
            separator = (attributes.get("id") or "").casefold() in self._OUTLOOK_SEPARATOR_IDS
            quoted = tag == "blockquote" or bool(identifiers & self._QUOTED_CONTAINERS)
            known_identifiers = self._QUOTED_CONTAINERS
            if separator:
                known_identifiers = known_identifiers | self._OUTLOOK_SEPARATOR_IDS
            # 旧版 Outlook 的 OLK_SRC_BODY_SECTION 及其变体也无法确认文字归属。
            if (
                tag not in self._BODY_TAGS
                or identifiers - known_identifiers
                or any(name.startswith("data-") for name in attributes)
                or (tag != "blockquote" and (attributes.get("type") or "").casefold() == "cite")
                or "hidden" in attributes
            ):
                self.uncertain = True
            if separator or quoted or tag in {"script", "style"}:
                if tag in self._VOID_TAGS:
                    self.uncertain = True
                    return
                if separator:
                    self.handle_data("\n----\n")
                if quoted:
                    self.handle_data("\n")
                    self.structured_quote = True
                self.excluded.append(tag)
                return
            if tag == "hr":
                self.handle_data("\n----\n")
                return
        if tag in self.breaks:
            self.handle_data("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.authored_only and tag not in self._VOID_TAGS:
            # HTML 的自闭合语法无法证明非空元素已经结束。
            self.uncertain = True
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.excluded:
            if tag in self._VOID_TAGS:
                return
            if tag != self.excluded[-1]:
                self.uncertain = True
            else:
                self.excluded.pop()
            return
        if self.authored_only and tag not in self._BODY_TAGS:
            self.uncertain = True
        if tag in self.breaks:
            self.handle_data("\n")

    def handle_comment(self, text):
        if self.authored_only and not self.excluded and text.strip():
            self.uncertain = True

    def handle_data(self, text):
        if not self.excluded:
            (self.tail if self.structured_quote else self.text).append(text)

    def get_text(self) -> str:
        return "".join(self.text)


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
    # BODYSTRUCTURE 已展开 related 的根段，依次选择纯文本、HTML。
    for preferred in ("text/plain", "text/html"):
        body = "\n".join(_plain_text(kind, text) for kind, text in parts if kind == preferred)
        if body.strip():
            return body.strip()
    return ""


def authored_reply(kind: str, content: str) -> str | None:
    """提取本次回复文字。无法确认引文边界时返回 None。"""
    structured_quote = False
    if kind == "text/html":
        parser = _HTMLText(authored_only=True)
        try:
            parser.feed(content)
            parser.close()
        except (AssertionError, ValueError):
            return None
        if parser.excluded or parser.uncertain or "".join(parser.tail).strip():
            return None
        content = parser.get_text()
        structured_quote = parser.structured_quote

    lines = content.strip().splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(_QUOTE_PREFIXES):
            return None
        if _DIVIDER.fullmatch(stripped) or _ATTRIBUTION.fullmatch(stripped):
            # 连续分隔线可来自同一个 HTML 回复头（如 hr 紧接 Outlook 回复头）。
            for offset in range(index + 1, len(lines)):
                item = lines[offset].strip()
                if item and not _DIVIDER.fullmatch(item):
                    break
            else:
                offset = len(lines)
            quoted = lines[offset:]
            if all(item.lstrip().startswith(_QUOTE_PREFIXES) for item in quoted) and (
                quoted or structured_quote
            ):
                return "\n".join(lines[:index]).strip()
            return None
        # 不靠字段名的语言或拼写推断一个疑似回复头的归属。
        if _HEADER_FIELD.match(stripped) or _UNKNOWN_DIVIDER.match(stripped):
            return None
    return "\n".join(lines)
