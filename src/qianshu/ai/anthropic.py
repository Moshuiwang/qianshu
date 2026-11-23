"""Anthropic 即时流式适配，保留工具参数的原始 JSON。"""

import json
import logging
import os
from time import monotonic_ns
from typing import get_args

import httpx
from pydantic import ValidationError

from qianshu.config import ConfigError
from qianshu.parse.model import (
    _REPLY_TOOL, CallResult, ModelError, ModelReplyError, StreamingModel, _missing_shape, validate_reply_json,
)
from qianshu.parse.protocol import ApplicationReply


class _BlockOrderError(ValueError):
    """流式块的序号未指向当前块。"""


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


def _guard_redirect(response: httpx.Response) -> None:
    if not response.is_redirect or "location" not in response.headers:
        return
    source = response.request.url
    try:
        target = httpx.URL(response.headers["location"])
        if not target.scheme:
            target = source.join(target)
        if (source.scheme, source.raw_host, source.port) == (target.scheme, target.raw_host, target.port):
            return
    except (ValueError, httpx.InvalidURL):
        pass
    # HTTPX 尚未构造下一跳。保留 3xx 供 SDK 报错，同时禁止按响应头重试。
    del response.headers["location"]
    response.headers["x-should-retry"] = "false"


def streaming_model(*, api_key, base_url, model, effort, on_result, max_retries, follow_redirects) -> StreamingModel:
    # 显式传入默认端点，防止 SDK 在解析 CLI 中另从进程环境取址。
    endpoint = base_url if base_url is not None else "https://api.anthropic.com"

    def attempt(system: str, messages: list, on_result, first_shape_error: str | None = None) -> str:
        from anthropic import Anthropic, AnthropicError, DefaultHttpxClient
        from anthropic.types import StopReason

        if "ANTHROPIC_CUSTOM_HEADERS" in os.environ:
            raise ConfigError("请通过私有配置指定 AI 凭据，并移除 SDK 的请求头覆盖。")
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
            options["max_retries"] = max_retries
        http_client = DefaultHttpxClient(follow_redirects=follow_redirects)
        http_client.event_hooks["response"].append(_guard_redirect)
        options["http_client"] = http_client
        if first_shape_error:
            options["max_retries"] = 0
        reply = None
        try:
            with Anthropic(api_key=api_key, base_url=endpoint, **options) as client:
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

    return StreamingModel(attempt, on_result)
