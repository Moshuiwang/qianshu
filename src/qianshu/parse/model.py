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

from .materials import ANTHROPIC_ORIGIN
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


class _BlockOrderError(ValueError):
    """流式块的序号未指向当前块。"""


class ModelReplyError(ModelError):
    """已收到回复，但未取得有效的结构化结果。"""

    def __init__(self, code: str, reply: str | None, message: str, *, path: tuple = ()):
        super().__init__(message)
        self.code = code
        self.reply = reply
        self.path = path


class ParseModel(Protocol):
    def __call__(self, *, system: str, user: str | list[dict]) -> str: ...


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

    images_allowed: bool = False

    def __call__(self, *, system: str, user: str | list[dict]) -> str:
        if isinstance(user, list) and not self.images_allowed:
            raise ModelError("图片请求需要获准的解析接口与禁用重定向的客户端。")
        return self.send(system, [{"role": "user", "content": user}], self.on_result)


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

    def attempt(system: str, messages: list, on_result, first_shape_error: str | None = None) -> str:
        import httpx
        from anthropic import Anthropic, AnthropicError, DefaultHttpxClient
        from anthropic.types import StopReason

        tool_inputs: dict[int, list[str]] = {}

        def record(reply, raw: str | None, failure: str | None = None, *, partial=False, shape=None) -> None:
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
                None if partial else getattr(usage, "input_tokens", None),
                None if partial else getattr(usage, "output_tokens", None),
                (monotonic_ns() - started) // 1_000_000, getattr(reply, "model", model),
                raw, failure, effort=effort, first_shape_error=first_shape_error or shape,
                retry_result=("failed" if failure else "succeeded") if first_shape_error else None,
                partial_input_tokens=getattr(usage, "input_tokens", None) if partial else None,
                partial_output_tokens=getattr(usage, "output_tokens", None) if partial else None,
            ))

        def format_error(code: str, *, raw: str | None = None, path: tuple = ()) -> ModelReplyError:
            if raw is None:
                raw = _raw_content(reply.content, tool_inputs)
            record(reply, raw, code)
            return ModelReplyError(code, raw, "申请解析模型回复解析失败。", path=path)

        def retry(code: str, path: tuple, failure: str, raw: str, count: int) -> str:
            record(reply, raw, failure, shape=code)
            feedback = json.dumps({"path": path, "reason": code})
            # 重建工具信封，不复制模型的自由文本、引文或工具输入。
            calls = [{"type": "tool_use", "id": f"toolu_retry_{i}", "name": _REPLY_TOOL, "input": {}}
                     for i in range(count)]
            results = [{"type": "tool_result", "tool_use_id": call["id"], "content": feedback, "is_error": True}
                       for call in calls]
            return attempt(system, [*messages, {"role": "assistant", "content": calls},
                                    {"role": "user", "content": results}], on_result, code)

        # SDK 的调试日志会展开申请正文，HTTP 日志会记录服务地址。
        for logger_name in ("anthropic", "httpx", "httpcore"):
            logging.getLogger(logger_name).setLevel(logging.WARNING)
        started = monotonic_ns() if on_result is not None else 0
        options = {}
        if max_retries is not None:
            options.update(max_retries=max_retries, http_client=DefaultHttpxClient(follow_redirects=False))
        if first_shape_error:
            options["max_retries"] = 0
        reply = None
        try:
            with Anthropic(api_key=api_key, base_url=base_url, **options) as client:
                # 使用原始事件，避免 SDK 的部分 JSON 解码先补全或拒绝工具参数。
                with client.messages.create(
                    model=model,
                    system=system,
                    messages=messages,
                    max_tokens=128000,
                    output_config={"effort": effort},
                    tools=[{
                        "name": _REPLY_TOOL,
                        "description": "提交一份申请解析声明。",
                        "input_schema": ApplicationReply.model_json_schema(images=isinstance(messages[0]["content"], list)),
                    }],
                    stream=True,
                ) as stream:
                    stopped = False
                    active_index = None
                    for event in stream:
                        if stopped or (event.type == "message_start" and reply is not None):
                            raise _BlockOrderError("回复事件顺序异常。")
                        if event.type == "content_block_start":
                            if reply is None or event.index != len(reply.content) or active_index is not None:
                                raise _BlockOrderError("回复块序号异常。")
                            active_index = event.index
                        elif event.type in {"content_block_delta", "content_block_stop"}:
                            if active_index is None or event.index != active_index:
                                raise _BlockOrderError("回复块序号异常。")
                            if event.type == "content_block_stop":
                                active_index = None
                        elif event.type == "message_stop":
                            stopped = True
                        reply = _accumulate_event(reply, event, tool_inputs)
                    if reply is None:
                        raise ValueError("缺少回复起始事件。")
        # 流式读取中途断开或超时时，SDK 不包装传输层异常。
        except (AnthropicError, ValueError, httpx.HTTPError) as error:
            raw = _interrupted_content(reply, tool_inputs)
            if raw is not None or isinstance(error, _BlockOrderError):
                record(reply, raw, "reply_truncated", partial=True)
                raise ModelReplyError("reply_truncated", raw, "申请解析模型调用失败。") from None
            record(reply, None, "model_call", partial=True)
            raise ModelError("申请解析模型调用失败。") from None

        if not stopped or active_index is not None:
            raw = _interrupted_content(reply, tool_inputs)
            record(reply, raw, "reply_truncated", partial=True)
            raise ModelReplyError("reply_truncated", raw, "申请解析模型回复未完整结束。")

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
        if len(tool_uses) == 2 and not first_shape_error and all(tool.name == _REPLY_TOOL for _, tool in tool_uses):
            return retry("reply_tool_multiple", ("content",), "reply_tool_multiple",
                         _raw_content(reply.content, tool_inputs), 2)
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
            if not first_shape_error and (missing := _missing_shape(error)):
                code, path = missing
                return retry(code, path, "reply_schema", result, 1)
            path = error.errors(include_input=False, include_context=False, include_url=False)[0]["loc"]
            raise format_error("reply_schema", raw=result, path=path) from None
        record(reply, result)
        return result

    # 仅显式指定端点且已启用现有 follow_redirects=False 客户端的模型可组装原图请求。
    return StreamingModel(attempt, on_result, base_url == ANTHROPIC_ORIGIN and max_retries is not None)
