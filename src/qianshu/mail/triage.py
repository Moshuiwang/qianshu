"""私有投递身份与分拣输入。只从确认的正文段提取分拣材料。"""

import hashlib
import json
import unicodedata
from dataclasses import dataclass, field
from email import policy
from email.errors import MessageError
from email.parser import BytesParser

from qianshu.ai import anthropic
from qianshu.config import ConfigError
from qianshu.parse.model import DEFAULT_MODEL, configured_effort

from . import _fingerprint
from .imap import MailError
from .message import (
    _ATTRIBUTION, _DIVIDER, _HEADER_FIELD, _HTMLText, _UNKNOWN_DIVIDER,
    _body_leaf, _snapshot_body_children, _snapshot_leaf, _wire_body, _wire_children, message_id, sender,
)


def _is_quote_line(line: str) -> bool:
    for index, char in enumerate(line):
        if char == "\t" or unicodedata.category(char) in {"Cc", "Cf", "Mn", "Me", "Zs"}:
            continue
        if char not in {">", "＞"}:
            return False
        following = line[index + 1:index + 2]
        return following != "=" and not following.isdigit()
    return False



@dataclass(repr=False)
class MailItem:
    key: str | None
    digest: str | None
    candidate: str | None
    attachments: int | None
    fields: dict = field(repr=False)


class _StyledHTML(_HTMLText):
    def handle_starttag(self, tag, attrs):
        if tag in {"link", "style"} or any(key == "style" for key, _ in attrs):
            self.uncertain = True
        super().handle_starttag(tag, attrs)


def _triage_reply(kind, content):
    structured_quote = False
    if kind == "text/html":
        parser = _StyledHTML(authored_only=True)
        parser.feed(content)
        parser.close()
        if parser.uncertain or parser.excluded or "".join(parser.tail).strip():
            return None
        content = parser.get_text()
        structured_quote = parser.structured_quote

    lines = content.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if _is_quote_line(line):
            return None
        if _DIVIDER.fullmatch(stripped) or _ATTRIBUTION.fullmatch(stripped):
            offset = index + 1
            while offset < len(lines) and (not lines[offset].strip() or _DIVIDER.fullmatch(lines[offset].strip())):
                offset += 1
            quoted = lines[offset:]
            if (quoted or structured_quote) and all(_is_quote_line(item) for item in quoted):
                return "\n".join(lines[:index]).strip()
            return None
        if _HEADER_FIELD.match(stripped) or _UNKNOWN_DIVIDER.match(stripped):
            return None
    return "\n".join(lines).strip()


def inspect_mail(raw: bytes) -> MailItem:
    try:
        return _inspect_mail(raw)
    except (AttributeError, IndexError, LookupError, MessageError, TypeError, ValueError, RecursionError):
        fields = dict(display_name="", domain="", subject="", body="")
        return MailItem(None, None, None, None, fields)


def _inspect_mail(raw: bytes) -> MailItem:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    key = message_id(msg)
    address = sender(msg)
    display = domain = ""
    if address:
        parsed = msg["From"].addresses[0]
        domain = parsed.domain
        display = parsed.display_name if "@" not in parsed.display_name else ""
    subjects = msg.get_all("Subject", [])
    subject = str(subjects[0]) if len(subjects) == 1 and not subjects[0].defects else ""
    fields = dict(display_name=display, domain=domain, subject=subject, body="")
    digest = hashlib.sha256(b"mime-v1")
    bodies = []
    count = 0

    def frame(value):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)

    def visit(part, visible, wire):
        nonlocal count
        kind = part.get_content_type()
        for name in ("Content-Type", "Content-Disposition", "Content-Transfer-Encoding", "Content-ID"):
            values = part.get_all(name, [])
            if len(values) > 1 or any(value.defects for value in values):
                raise ValueError
        # 包含根绑定、字符集及其他内容头；忽略传输编码与容器边界拼写。
        content_headers = [(k.lower(), str(v)) for k, v in part.items()
                           if k.lower().startswith("content-") and k.lower() not in
                           {"content-type", "content-transfer-encoding"}]
        params = [(k, v) for k, v in part.get_params()[1:] if k.lower() != "boundary"] if part["Content-Type"] else []
        frame(json.dumps([kind, params, content_headers]).encode())
        if kind.startswith("multipart/"):
            attached = part.get_filename() is not None or part.get_content_disposition() not in (None, "inline")
        else:
            attached = not _body_leaf(part)
        if visible and attached:
            count += 1
            visible = False
        if not visible:
            frame(_wire_body(wire) if part is msg else wire)
        if (visible and kind.startswith("multipart/")
                and kind not in {"multipart/mixed", "multipart/alternative", "multipart/related"}):
            raise ValueError
        if part.is_multipart():
            if part.get("Content-Transfer-Encoding", "7bit").lower() not in {"7bit", "8bit", "binary"}:
                raise ValueError
            frame((part.preamble or "").encode("utf-8", "surrogateescape"))
            frame((part.epilogue or "").encode("utf-8", "surrogateescape"))
            children = list(part.iter_parts())
            frame(str(len(children)).encode())
            if kind.startswith("message/"):
                if kind != "message/rfc822" or len(children) != 1:
                    raise ValueError
                child_wires = [_wire_body(wire)]
                selected = {0}
            else:
                selected = set(_snapshot_body_children(part, children))
                child_wires = _wire_children(part, wire, len(children))
            for i, (child, child_wire) in enumerate(zip(children, child_wires, strict=True)):
                resource = i not in selected
                if resource and visible:
                    count += 1
                visit(child, visible and not resource, child_wire)
        else:
            if part.get("Content-Transfer-Encoding", "7bit").lower() not in {
                "7bit", "8bit", "binary", "base64", "quoted-printable"
            } or kind.startswith(("multipart/", "message/")):
                raise ValueError
            frame(part.get_payload(decode=True))
            if visible:
                kind, content = _snapshot_leaf(part, wire, part is not msg)
                content = content.lstrip("\ufeff\u200b\u200c\u200d\u2060")
                bodies.append((kind, _triage_reply(kind, content)))
        if part.defects:
            raise ValueError

    try:
        visit(msg, True, raw)
    except (MailError, ValueError, TypeError, LookupError, RecursionError, AssertionError):
        return MailItem(_fingerprint(key) if key else None, None, None, None, fields)
    for preferred in ("text/plain", "text/html"):
        selected = [text for kind, text in bodies if kind == preferred]
        if selected:
            fields["body"] = "\n".join(selected)[:2000] if all(selected) else ""
            break
    version = "1:" + digest.hexdigest()
    return MailItem(_fingerprint(key) if key else None, version, _fingerprint(address, subject, version), count, fields)


def _options(config):
    if (not config.get("ANTHROPIC_API_KEY") or config.get("AI_PLATFORM", "anthropic") != "anthropic"
            or config.get("AI_ACTIVE_PROFILE", "primary") != "primary"
            or config.get("ANTHROPIC_MODEL", DEFAULT_MODEL) != DEFAULT_MODEL):
        raise ConfigError("试运行须使用已登记的主用模型。")
    return dict(api_key=config["ANTHROPIC_API_KEY"], base_url=config.get("ANTHROPIC_BASE_URL"))


def trial_model(config, before_send, on_result):
    return anthropic.streaming_model(**_options(config), model=DEFAULT_MODEL, effort=configured_effort(config),
                                     on_result=on_result, max_retries=0, follow_redirects=False,
                                     before_send=before_send)


def classify(config, item, before_send):
    return anthropic.triage_message(**_options(config), before_send=before_send,
                                    user=json.dumps(item.fields, ensure_ascii=False))



def input_limit(config, model):
    # 同一主通道只读型号说明；请求中没有邮件、提示词或工具材料。
    with anthropic.trial_client(**_options(config)) as client:
        info = client.models.retrieve(model)
    if info.id != model or type(info.max_input_tokens) is not int or info.max_input_tokens <= 0:
        raise ConfigError("无法核定试运行型号的输入费用上界。")
    return info.max_input_tokens
