"""申请解析的模型调用边界。"""

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values
from pydantic import ValidationError

from qianshu.config import Config, ConfigError, load_config

from .protocol import ApplicationReply, Findings

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_EFFORT = "medium"
_REPLY_TOOL = "submit_application_reply"
_EFFORT_KEY = "ANTHROPIC_EFFORT"
REPLY_FORMAT_ERRORS = frozenset({
    "reply_tool_missing", "reply_tool_multiple", "reply_tool_unexpected", "tool_input_json", "reply_schema",
})


class ModelError(RuntimeError):
    """调用失败或模型回复不完整。"""


class ModelReplyError(ModelError):
    """已收到回复，但未取得有效的结构化结果。"""

    def __init__(self, code: str, reply: str | None, message: str, *, path: tuple = ()):
        super().__init__(message)
        self.code = code
        self.reply = reply
        self.path = path


class ParseModel(Protocol):
    def __call__(self, *, system: str, user: str) -> str: ...


@dataclass(frozen=True)
class CallResult:
    stop_reason: str | None
    blocks: tuple[tuple[str, int | None], ...]
    input_tokens: int | None
    output_tokens: int | None
    elapsed_ms: int
    model: str
    reply: str | None = field(repr=False)
    failure_stage: str | None = None
    effort: str | None = None
    first_shape_error: str | None = None
    retry_result: str | None = None
    partial_input_tokens: int | None = None
    partial_output_tokens: int | None = None


@dataclass(frozen=True)
class StreamingModel:
    send: Callable
    on_result: Callable[[CallResult], None] | None

    def __call__(self, *, system: str, user: str) -> str:
        return self.send(system, [{"role": "user", "content": user}], self.on_result)


def _missing_shape(error: ValidationError) -> tuple[str, tuple] | None:
    # 只回传 schema 中的固定键和数组下标。
    for detail in error.errors(include_input=False, include_context=False, include_url=False):
        if detail["type"] != "missing":
            continue
        match detail["loc"]:
            case ("policies", int(), "fields", name, *tail) if name in Findings.model_fields:
                match tail:
                    case ["evidence"]:
                        return "missing_evidence", detail["loc"]
                    case ["evidence", int(), "text"] | ["candidates", int(), "evidence", int(), "text"]:
                        return "missing_quote_text", detail["loc"]
    return None


def recorded_shape_error(attempt: dict) -> str | None:
    """从旧录制或单次批量结果复核形状，保持新录制字段可选。"""
    if reason := attempt.get("first_shape_error"):
        return reason
    raw = attempt.get("reply")
    if not isinstance(raw, str):
        return None
    if attempt["failure_stage"] == "reply_schema":
        try:
            ApplicationReply.model_validate_json(raw)
        except ValidationError as error:
            if missing := _missing_shape(error):
                return missing[0]
    elif attempt["failure_stage"] == "reply_tool_multiple":
        try:
            blocks = validate_reply_json(raw)
        except (ValueError, RecursionError):
            return None
        if isinstance(blocks, list):
            tools = [b.get("name") for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
            if tools == [_REPLY_TOOL, _REPLY_TOOL]:
                return "reply_tool_multiple"
    return None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError
    return result


def _reject_constant(_: str) -> None:
    raise ValueError


def _finite_float(text: str) -> float:
    value = float(text)
    if not isfinite(value):
        raise ValueError
    return value


def validate_reply_json(reply: str) -> object:
    return json.loads(reply, object_pairs_hook=_unique_object, parse_constant=_reject_constant, parse_float=_finite_float)


def load_model_config(*, env_file: Path = Path(".env"), config: Config | None = None) -> Config:
    """为申请解析补充模型专用的配置项。"""
    if config is None:
        config = load_config(env_file=env_file)
    if "ANTHROPIC_CUSTOM_HEADERS" in os.environ or (
        "ANTHROPIC_BASE_URL" in os.environ and not config.get("ANTHROPIC_BASE_URL")
    ):
        raise ConfigError("请通过 .env 配置 AI 接口，并移除 SDK 的进程环境覆盖。")
    try:
        with env_file.open(encoding="utf-8-sig") as stream:
            values = dotenv_values(stream=stream, interpolate=False)
    except FileNotFoundError:
        return config
    except (OSError, UnicodeError):
        raise ConfigError("无法读取 .env 配置文件，请检查文件权限和 UTF-8 编码。") from None
    if _EFFORT_KEY not in values:
        return config
    return Config({**config, _EFFORT_KEY: values[_EFFORT_KEY] or ""})


def configured_effort(config: Mapping[str, str]) -> str:
    effort = config.get(_EFFORT_KEY, DEFAULT_EFFORT)
    if effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise ConfigError("ANTHROPIC_EFFORT 仅接受 low、medium、high、xhigh 或 max。")
    return effort


def _validate_endpoint(value: str, profile: str) -> None:
    message = f"{profile}端点必须是有效的 HTTPS 地址。"
    try:
        endpoint = httpx.URL(value)
        # HTTPX 会在规范化时丢弃空的用户名分隔符。
        has_userinfo = urlsplit(value).username is not None
    except (ValueError, httpx.InvalidURL):
        raise ConfigError(message) from None
    if endpoint.scheme != "https" or not endpoint.host or endpoint.userinfo or has_userinfo:
        raise ConfigError(message)


def configured_model(
    config: Mapping[str, str], *, on_result: Callable[[CallResult], None] | None = None,
    max_retries: int | None = None,
) -> ParseModel:
    """按配置选择一个平台。适配器交回原始 JSON，业务层统一核对。"""
    from qianshu.ai import ADAPTERS

    profile = config.get("AI_ACTIVE_PROFILE", "primary")
    if profile == "primary":
        platform = config.get("AI_PLATFORM", "anthropic")
        key_name = "ANTHROPIC_API_KEY"
        base_url = config.get("ANTHROPIC_BASE_URL")
        if base_url is not None:
            _validate_endpoint(base_url, "主用")
        model = config.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
        effort = configured_effort(config)
    elif profile == "backup":
        platform = config.get("AI_BACKUP_PLATFORM")
        key_name = "AI_BACKUP_API_KEY"
        model = config.get("AI_BACKUP_MODEL")
        base_url = config.get("AI_BACKUP_BASE_URL")
        if not (platform and model and base_url):
            raise ConfigError("备用平台须填写 AI_BACKUP_PLATFORM、AI_BACKUP_MODEL 和 AI_BACKUP_BASE_URL。")
        _validate_endpoint(base_url, "备用")
        effort = DEFAULT_EFFORT
    else:
        raise ConfigError("AI_ACTIVE_PROFILE 仅接受 primary 或 backup。")
    if platform not in ADAPTERS:
        raise ConfigError("AI_PLATFORM 或 AI_BACKUP_PLATFORM 配置不受支持。")
    api_key = config.get(key_name)
    if not api_key:
        raise ConfigError(f"缺少配置，请在 .env 中填写 {key_name}。")
    return ADAPTERS[platform](
        api_key=api_key, base_url=base_url, model=model, effort=effort, on_result=on_result,
        max_retries=max_retries, follow_redirects=profile == "primary" and max_retries is None,
    )
