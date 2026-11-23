"""私有投递身份与分拣输入。只从确认的正文段提取分拣材料。"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from email import policy
from email.errors import MessageError
from email.parser import BytesParser
from html.parser import HTMLParser

from qianshu.ai import anthropic
from qianshu.config import ConfigError
from qianshu.parse.model import DEFAULT_MODEL, configured_effort

from . import _fingerprint
from .message import authored_reply, message_id, sender


@dataclass(repr=False)
class MailItem:
    key: str | None
    digest: str | None
    candidate: str | None
    attachments: int | None
    fields: dict = field(repr=False)


class _StyledHTML(HTMLParser):
    uncertain = False

    def handle_starttag(self, tag, attrs):
        if tag in {"link", "style"} or any(key == "style" for key, _ in attrs):
            self.uncertain = True


def inspect_mail(raw: bytes) -> MailItem:
    try:
        return _inspect_mail(raw)
    except (AttributeError, IndexError, LookupError, MessageError, TypeError, ValueError, RecursionError):
        fields = dict(display_name="", domain="", subject="", body="")
        return MailItem(None, None, None, None, fields)


def _wire_body(wire):
    separator = re.search(rb"(?m)^\r?\n", wire)
    if separator is None:
        raise ValueError
    return wire[separator.end():]


def _wire_children(part, wire, count):
    # 从原始字节定位子段，避免重新序列化时折行、编码或内嵌邮件头发生变化。
    boundary = part.get_boundary()
    if not boundary:
        raise ValueError
    body = _wire_body(wire)
    markers = list(re.finditer(rb"(?m)^--" + re.escape(boundary.encode("ascii"))
                               + rb"(--)?[ \t]*(?:\r?\n|\Z)", body))
    if (len(markers) != count + 1 or not markers[-1][1]
            or any(marker[1] for marker in markers[:-1])):
        raise ValueError
    return [body[first.end():last.start()] for first, last in zip(markers, markers[1:])]


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
        attached = (part.get_filename() is not None or part.get_content_disposition() not in (None, "inline")
                    or (not kind.startswith("multipart/") and kind not in {"text/plain", "text/html"}))
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
            root = 0
            if kind == "multipart/related" and (start := part.get_param("start")) is not None:
                matches = [i for i, child in enumerate(children) if child.get("Content-ID") == start]
                if len(matches) != 1:
                    raise ValueError
                root = matches[0]
            if kind.startswith("message/"):
                if kind != "message/rfc822" or len(children) != 1:
                    raise ValueError
                child_wires = [_wire_body(wire)]
            else:
                child_wires = _wire_children(part, wire, len(children))
            for i, (child, child_wire) in enumerate(zip(children, child_wires, strict=True)):
                resource = kind == "multipart/related" and i != root
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
                content = part.get_content(errors="strict").lstrip("\ufeff\u200b\u200c\u200d\u2060")
                if "\0" in content:
                    raise ValueError
                eligible = True
                if kind == "text/html":
                    parser = _StyledHTML()
                    parser.feed(content)
                    parser.close()
                    eligible = not parser.uncertain
                bodies.append((kind, authored_reply(kind, content) if eligible else None))
        if part.defects:
            raise ValueError

    try:
        visit(msg, True, raw)
    except (ValueError, TypeError, LookupError, RecursionError, AssertionError):
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
