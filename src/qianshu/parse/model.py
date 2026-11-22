"""申请解析的模型调用边界。"""

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from time import monotonic_ns
from typing import Protocol, get_args

from dotenv import dotenv_values
from pydantic import ValidationError

from qianshu.config import Config, ConfigError, load_config

from .protocol import ApplicationReply

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


def _block_length(block) -> int | None:
    for name in ("text", "thinking"):
        if isinstance(value := getattr(block, name, None), str):
            return len(value)
    serialize = getattr(block, "model_dump_json", None)
    return len(serialize()) if callable(serialize) else None


def _raw_content(blocks, tool_inputs: dict[int, list[str]]) -> str:
    content = []
    for index, block in enumerate(blocks):
        entry = block.model_dump(exclude_unset=True)
        if block.type == "tool_use":
            entry["input"] = "".join(tool_inputs.get(index, ()))
        content.append(entry)
    return json.dumps(content, ensure_ascii=False)


def _interrupted_content(reply, tool_inputs: dict[int, list[str]]) -> str | None:
    blocks = getattr(reply, "content", ())
    if any(block.type == "tool_use" for block in blocks):
        return _raw_content(blocks, tool_inputs)
    return "".join(block.text for block in blocks if block.type == "text") or None


def _accumulate_event(reply, event, tool_inputs: dict[int, list[str]]):
    if event.type == "message_start":
        return event.message
    if reply is None:
        raise ValueError("缺少回复起始事件。")
    if event.type == "content_block_start":
        reply.content.append(event.content_block)
    elif event.type == "content_block_delta":
        block = reply.content[event.index]
        delta = event.delta
        if delta.type == "input_json_delta":
            tool_inputs.setdefault(event.index, []).append(delta.partial_json)
        elif delta.type == "text_delta" and block.type == "text":
            block.text += delta.text
        elif delta.type == "thinking_delta" and block.type == "thinking":
            block.thinking += delta.thinking
        elif delta.type == "signature_delta" and block.type == "thinking":
            block.signature = delta.signature
        elif delta.type == "citations_delta" and block.type == "text":
            block.citations = [*(block.citations or ()), delta.citation]
    elif event.type == "message_delta":
        reply.stop_reason = event.delta.stop_reason
        reply.usage.output_tokens = event.usage.output_tokens
        if event.usage.input_tokens is not None:
            reply.usage.input_tokens = event.usage.input_tokens
    return reply


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


def configured_model(
    config: Mapping[str, str], *, on_result: Callable[[CallResult], None] | None = None,
    max_retries: int | None = None,
) -> ParseModel:
    """要求模型通过提交工具返回解析声明，再在本地校验。"""
    if config.get("AI_PLATFORM", "anthropic") != "anthropic":
        raise ConfigError("AI_PLATFORM 配置不受支持，申请解析目前只支持 Anthropic（anthropic）。")
    api_key = config.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ConfigError("缺少配置，请在 .env 中填写 ANTHROPIC_API_KEY。")
    base_url = config.get("ANTHROPIC_BASE_URL")
    model = config.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
    effort = configured_effort(config)

    def complete(*, system: str, user: str) -> str:
        import httpx
        from anthropic import Anthropic, AnthropicError, DefaultHttpxClient
        from anthropic.types import StopReason

        tool_inputs: dict[int, list[str]] = {}

        def record(reply, raw: str | None, failure: str | None = None) -> None:
            if on_result is None:
                return
            blocks = getattr(reply, "content", ())
            usage = getattr(reply, "usage", None)
            stop = getattr(reply, "stop_reason", None)
            on_result(CallResult(
                stop if stop is None or stop in get_args(StopReason) else "unknown",
                tuple(
                    (block.type, sum(map(len, tool_inputs.get(index, ()))))
                    if block.type == "tool_use" else (block.type, _block_length(block))
                    for index, block in enumerate(blocks)
                ),
                getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None),
                (monotonic_ns() - started) // 1_000_000, getattr(reply, "model", model),
                raw, failure, effort=effort,
            ))

        def format_error(code: str, *, raw: str | None = None, path: tuple = ()) -> ModelReplyError:
            if raw is None:
                raw = _raw_content(reply.content, tool_inputs)
            record(reply, raw, code)
            return ModelReplyError(code, raw, "申请解析模型回复解析失败。", path=path)

        # SDK 的调试日志会展开申请正文，HTTP 日志会记录服务地址。
        for logger_name in ("anthropic", "httpx", "httpcore"):
            logging.getLogger(logger_name).setLevel(logging.WARNING)
        started = monotonic_ns() if on_result is not None else 0
        options = {}
        if max_retries is not None:
            options.update(max_retries=max_retries, http_client=DefaultHttpxClient(follow_redirects=False))
        reply = None
        try:
            with Anthropic(api_key=api_key, base_url=base_url, **options) as client:
                # 使用原始事件，避免 SDK 的部分 JSON 解码先补全或拒绝工具参数。
                with client.messages.create(
                    model=model,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    max_tokens=32000,
                    output_config={"effort": effort},
                    tools=[{
                        "name": _REPLY_TOOL,
                        "description": "提交一份申请解析声明。",
                        "input_schema": ApplicationReply.model_json_schema(),
                    }],
                    stream=True,
                ) as stream:
                    for event in stream:
                        reply = _accumulate_event(reply, event, tool_inputs)
                    if reply is None:
                        raise ValueError("缺少回复起始事件。")
        # 流式读取中途断开或超时时，SDK 不包装传输层异常。
        except (AnthropicError, ValueError, httpx.HTTPError):
            raw = _interrupted_content(reply, tool_inputs)
            if raw is not None:
                record(reply, raw, "reply_truncated")
                raise ModelReplyError("reply_truncated", raw, "申请解析模型调用失败。") from None
            record(None, None, "model_call")
            raise ModelError("申请解析模型调用失败。") from None

        if reply.stop_reason not in {"tool_use", "end_turn"}:
            stop_reason = reply.stop_reason if reply.stop_reason in get_args(StopReason) else "unknown"
            output_tokens = getattr(getattr(reply, "usage", None), "output_tokens", None)
            token_count = output_tokens if type(output_tokens) is int and output_tokens >= 0 else "未知"
            code = {"max_tokens": "reply_truncated", "refusal": "reply_refused"}.get(stop_reason, "reply_incomplete")
            raw = _interrupted_content(reply, tool_inputs)
            record(reply, raw, "model_incomplete")
            raise ModelReplyError(code, raw,
                                  f"申请解析模型回复不完整（stop_reason={stop_reason}，输出 {token_count} token）。")

        tool_uses = [(index, block) for index, block in enumerate(reply.content) if block.type == "tool_use"]
        if not tool_uses:
            raise format_error("reply_tool_missing")
        if len(tool_uses) > 1:
            raise format_error("reply_tool_multiple")
        (index, tool), = tool_uses
        if tool.name != _REPLY_TOOL:
            raise format_error("reply_tool_unexpected")
        result = "".join(tool_inputs.get(index, ()))
        try:
            validate_reply_json(result)
        except (ValueError, TypeError, RecursionError):
            raise format_error("tool_input_json", raw=result) from None
        try:
            # 严格模式按 JSON 校验，以同样的方式接受工具与重放中的枚举字符串。
            ApplicationReply.model_validate_json(result)
        except ValidationError as error:
            path = error.errors(include_input=False, include_context=False, include_url=False)[0]["loc"]
            raise format_error("reply_schema", raw=result, path=path) from None
        record(reply, result)
        return result

    return complete
