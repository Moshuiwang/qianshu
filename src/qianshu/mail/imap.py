"""IMAP 只读边界。筛选用 UID，取信只允许 BODY.PEEK。"""

import base64
import imaplib
import re
import ssl
from collections.abc import Callable, Mapping
from contextlib import contextmanager, suppress
from datetime import date, datetime, timedelta, timezone

SUMMARY = "HEADER.FIELDS (FROM SUBJECT MESSAGE-ID IN-REPLY-TO REFERENCES)"
_BODY_QUERY = re.compile(
    r"\(BODY\.PEEK\[(?:HEADER|TEXT|[1-9]\d*(?:\.[1-9]\d*)*(?:\.MIME)?)\]\)",
    re.ASCII,
)
_TOKEN = re.compile(rb'\s*(\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+)')
_LIST_ENTRY = re.compile(rb'\(([^()]*)\) (?:NIL|"(?:[^"\\]|\\["\\])") (.+)', re.IGNORECASE)
_LIST_QUOTED_NAME = re.compile(rb'"((?:[^"\\]|\\["\\])*)"')


class MailError(RuntimeError):
    """可记录的邮件错误，内容由本模块固定。"""


def _quoted(value: str) -> str:
    if not value.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise MailError("IMAP 参数包含不支持的字符。")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _encoded_mailbox_name(value: str) -> str:
    # IMAP4rev1 用 modified UTF-7 表示中文等非 ASCII 文件夹名。
    def encode(match: re.Match[str]) -> str:
        encoded = base64.b64encode(match[0].encode("utf-16-be")).decode("ascii")
        return "&" + encoded.rstrip("=").replace("/", ",") + "-"

    return re.sub(r"[^\x00-\x7f]+", encode, value.replace("&", "&-"))


def _imap_date(value: date) -> str:
    month = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return f"{value.day:02}-{month[value.month - 1]}-{value.year:04}"


class ReadOnlyMailbox:
    """封装已认证连接，只开放 LIST、EXAMINE 和受限的 UID 读取。"""

    def __init__(self, connection: imaplib.IMAP4) -> None:
        self._connection = connection

    @staticmethod
    def _checked(call: Callable, *args, **kwargs) -> list:
        try:
            status, data = call(*args, **kwargs)
        except (imaplib.IMAP4.error, OSError, ValueError):
            raise MailError("IMAP 读取失败。") from None
        if status != "OK":
            raise MailError("IMAP 读取被服务器拒绝。")
        return data

    def select(self, folder: str, *, readonly: bool = True) -> int:
        if readonly is not True:
            raise MailError("拒绝以可写方式打开邮箱文件夹。")
        return self._examine(_encoded_mailbox_name(folder))

    def _examine(self, encoded_folder: str) -> int:
        data = self._checked(self._connection.select, _quoted(encoded_folder), readonly=True)
        if not data or not isinstance(data[0], bytes) or not data[0].isdigit():
            raise MailError("IMAP 文件夹计数无效。")
        return int(data[0])

    def sent_folder(self, config: Mapping[str, str]) -> str | None:
        """返回已发送文件夹的 IMAP 编码名称，多候选且无法选定时返回 None。"""
        names = set()
        for row in self._checked(self._connection.list):
            if row in (None, b""):
                continue
            prefix = row[0].rstrip() if isinstance(row, tuple) else row
            entry = _LIST_ENTRY.fullmatch(prefix)
            if entry is None:
                raise MailError("IMAP 返回了无法解析的文件夹列表。")
            flags = entry[1].lower().split()
            if rb"\sent" not in flags:
                continue
            name = entry[2]
            if isinstance(row, tuple) and re.fullmatch(rb"\{[0-9]+\}", name):
                name = row[1]
            elif quoted := _LIST_QUOTED_NAME.fullmatch(name):
                name = re.sub(rb'\\(["\\])', rb'\1', quoted[1])
            elif not re.fullmatch(rb'[^\s(){%*"\\]+', name):
                raise MailError("IMAP 返回的已发送文件夹名称无效。")
            if not name or not name.isascii():
                raise MailError("IMAP 返回的已发送文件夹名称无效。")
            names.add(name.decode("ascii"))
        fallback = config.get("MAIL_SENT_FOLDER", "")
        if len(names) > 1:
            folder = _encoded_mailbox_name(fallback)
            if folder not in names:
                return None
        elif names:
            folder = next(iter(names))
        else:
            if not fallback.strip():
                raise MailError("IMAP 未返回带 \\Sent 属性的文件夹，请配置 MAIL_SENT_FOLDER。")
            folder = _encoded_mailbox_name(fallback)
        inbox = _encoded_mailbox_name(config.get("MAIL_FOLDER") or "INBOX")
        if folder.casefold() == inbox.casefold():
            raise MailError("已发送文件夹须与收件箱独立。")
        return folder

    def _read_uid(self, command: str, *args: str) -> list:
        if any("\r" in arg or "\n" in arg or "\x00" in arg for arg in args):
            raise MailError("拒绝含控制字符的 IMAP 指令。")
        if command == "FETCH":
            uid, query = args
            if (
                not re.fullmatch(r"[1-9][0-9]*", uid)
                or query not in {f"(BODY.PEEK[{SUMMARY}])", "(BODYSTRUCTURE)"}
                and not _BODY_QUERY.fullmatch(query)
            ):
                raise MailError("拒绝可能修改已读状态的 IMAP 取信指令。")
        elif command != "SEARCH":
            raise MailError("拒绝非只读 IMAP 指令。")
        return self._checked(self._connection.uid, command, *args)

    def _search(self, *criteria: str) -> tuple[int, ...]:
        values = self._read_uid("SEARCH", *criteria)
        if len(values) != 1 or not isinstance(values[0], bytes):
            raise MailError("IMAP 搜索结果无效。")
        uids = values[0].split()
        if any(not re.fullmatch(rb"[1-9][0-9]*", uid) for uid in uids):
            raise MailError("IMAP 搜索返回的 UID 无效。")
        return tuple(sorted({int(uid) for uid in uids}))

    def marked(self, marker: str, approver: str) -> tuple[int, ...]:
        # 部分服务器（X7 实测）的 SUBJECT / FROM / HEADER 搜索恒返回空，只有日期条件可用。
        # 这里只按日期取近两天的候选，标题、发件人和回复头由调用方逐封解码核对。
        _quoted(marker), _quoted(approver)  # 先拒绝控制字符。
        since = datetime.now(timezone.utc).date() - timedelta(days=1)
        return self._search("SINCE", _imap_date(since))

    def replies(self, message_id: str) -> tuple[int, ...]:
        quoted_id = _quoted(message_id)
        return self._search("OR", "HEADER", "In-Reply-To", quoted_id,
                            "HEADER", "References", quoted_id)

    def dated(self, since: date, before: date) -> tuple[int, ...]:
        return self._search("SINCE", _imap_date(since), "BEFORE", _imap_date(before))

    def seen(self) -> tuple[int, ...]:
        return self._search("SEEN")

    def section(self, uid: int, section: str) -> bytes:
        data = self._read_uid("FETCH", str(uid), f"(BODY.PEEK[{section}])")
        # 服务器的 FETCH 响应会省略 PEEK。
        response = re.compile(
            rb"(?:^|[ (])BODY\[" + re.escape(section.encode("ascii")) + rb"\] \{[0-9]+\}$",
            re.IGNORECASE,
        )
        literals = [part[1] for part in data if isinstance(part, tuple) and response.search(part[0])]
        if len(literals) != 1 or not isinstance(literals[0], bytes):
            raise MailError("IMAP 没有返回请求的邮件段。")
        return literals[0]

    def structure(self, uid: int) -> list:
        data = self._read_uid("FETCH", str(uid), "(BODYSTRUCTURE)")
        # imaplib 将 IMAP literal 拆成 (前缀, 值)，之后还有普通响应片段。
        parts = []
        for part in data:
            if isinstance(part, tuple):
                prefix, value = part
                escaped = value.replace(b"\\", b"\\\\").replace(b'"', b'\\"')
                parts.append(re.sub(rb"\{[0-9]+\}$", b"", prefix) + b'"' + escaped + b'"')
            elif isinstance(part, bytes):
                parts.append(part)
        match = re.search(rb"\bBODYSTRUCTURE\s+(\(.*)", b" ".join(parts), re.IGNORECASE | re.DOTALL)
        if match is None:
            raise MailError("IMAP 没有返回邮件结构。")

        stack: list[list] = []
        end = 0
        for token in _TOKEN.finditer(match[1]):
            if token.start() != end:
                break
            end = token.end()
            value = token[1]
            if value == b"(":
                stack.append([])
                if len(stack) > 64:
                    break
            elif value == b")" and stack:
                node = stack.pop()
                if not stack:
                    return node
                stack[-1].append(node)
            elif stack:
                stack[-1].append(value[1:-1] if value.startswith(b'"') else value)
        raise MailError("IMAP 返回的邮件结构无效。")


@contextmanager
def connect(config: Mapping[str, str], factory: Callable = imaplib.IMAP4_SSL):
    connection = None
    try:
        try:
            host = config["MAIL_HOST"]
            port = int(config["MAIL_PORT"])
            user = _quoted(config["MAIL_USER"])
            password = config["MAIL_PASSWORD"]
            _quoted(password)  # login 自行加引号，先拒绝控制字符。
            if not host or not 1 <= port <= 65535 or password == "":
                raise ValueError
        except (KeyError, ValueError):
            raise MailError("请配置有效的 IMAP 连接参数。") from None

        try:
            connection = factory(host, port, ssl_context=ssl.create_default_context(), timeout=20)
            connection.debug = 0
            status, _ = connection.login(user, password)
            if status != "OK":
                raise MailError("IMAP 认证被服务器拒绝。")
        except (imaplib.IMAP4.error, OSError, ValueError, OverflowError):
            raise MailError("无法建立 IMAP 会话。") from None
        yield ReadOnlyMailbox(connection)
    finally:
        if connection is not None:
            with suppress(imaplib.IMAP4.error, OSError):
                connection.shutdown()
