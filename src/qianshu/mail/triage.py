"""私有投递身份与分拣输入。只从确认的正文段提取分拣材料。"""

import json
import unicodedata
from dataclasses import dataclass, field

from qianshu.ai import anthropic
from qianshu.config import ConfigError
from qianshu.parse.model import DEFAULT_MODEL, configured_effort

from .message import _ATTRIBUTION, _DIVIDER, _HEADER_FIELD, _HTMLText, _UNKNOWN_DIVIDER, sender


def _structure_line(line: str) -> str:
    for index, char in enumerate(line):
        if unicodedata.category(char) not in {"Cc", "Cf", "Mn", "Me", "Zs"}:
            return line[index:].rstrip()
    return ""


def _is_quote_line(line: str) -> bool:
    following = line[1:2]
    return line.startswith((">", "＞")) and following != "=" and not following.isdigit()


@dataclass(repr=False)
class MailItem:
    key: str | None
    digest: str | None
    candidate: str | None
    attachments: int | None
    fields: dict = field(repr=False)
    original: tuple[str, str] | None = None


class _StyledHTML(_HTMLText):
    def handle_starttag(self, tag, attrs):
        if tag in {"link", "style"} or any(key == "style" for key, _ in attrs):
            self.uncertain = True
        super().handle_starttag(tag, attrs)


def _opens_boundary(line):
    return (_DIVIDER.fullmatch(line) or _ATTRIBUTION.fullmatch(line)
            or _HEADER_FIELD.match(line) or _UNKNOWN_DIVIDER.match(line))


def _triage_reply(kind, content):
    structured_quote = False
    if kind == "text/html":
        parser = _StyledHTML(authored_only=True)
        parser.feed(content)
        parser.close()
        if parser.uncertain or parser.excluded or "".join(parser.tail).strip():
            open_boundary = any(_opens_boundary(_structure_line(line)) for line in parser.get_text().splitlines())
            return "", bool(open_boundary or parser.excluded or "".join(parser.tail).strip())
        content = parser.get_text()
        structured_quote = parser.structured_quote

    lines = content.splitlines()
    structure = [_structure_line(line) for line in lines]
    while structure and not structure[-1]:
        structure.pop()
    for index, stripped in enumerate(structure):
        if _is_quote_line(stripped):
            return "", any(_opens_boundary(line) for line in structure[index + 1:])
        if _DIVIDER.fullmatch(stripped) or _ATTRIBUTION.fullmatch(stripped):
            offset = index + 1
            while offset < len(structure) and (not structure[offset] or _DIVIDER.fullmatch(structure[offset])):
                offset += 1
            quoted = structure[offset:]
            if (quoted or structured_quote) and all(_is_quote_line(item) for item in quoted):
                return "\n".join(lines[:index]).strip(), True
            return "", True
        if _HEADER_FIELD.match(stripped) or _UNKNOWN_DIVIDER.match(stripped):
            return "", True
    return "\n".join(lines).strip(), False


def triage_fields(msg, selected, preferred):
    address = sender(msg)
    display = domain = ""
    if address:
        parsed = msg["From"].addresses[0]
        domain = parsed.domain
        display = parsed.display_name if "@" not in parsed.display_name else ""
    subjects = msg.get_all("Subject", [])
    subject = str(subjects[0]) if len(subjects) == 1 and not subjects[0].defects else ""
    confirmed = []
    for kind, content in selected:
        if kind != preferred:
            continue
        text, boundary = _triage_reply(kind, content)
        if text:
            confirmed.append(text)
        if boundary:
            break
    return dict(display_name=display, domain=domain, subject=subject, body="\n".join(confirmed)[:2000])


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
