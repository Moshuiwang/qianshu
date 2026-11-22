"""申请解析的模型调用边界。"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import monotonic_ns
from typing import Protocol, get_args

from qianshu.config import ConfigError

from .protocol import ApplicationReply

DEFAULT_MODEL = "claude-sonnet-5-5"


class ModelError(RuntimeError):
    """调用失败或模型回复不完整。"""


class ModelReplyError(ModelError):
    """已收到回复，但提供方未返回完整的结构化结果。"""

    def __init__(self, code: str, reply: str | None, message: str):
        super().__init__(message)
        self.code = code
        self.reply = reply


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


def _block_length(block) -> int | None:
    for name in ("text", "thinking"):
        if isinstance(value := getattr(block, name, None), str):
            return len(value)
    serialize = getattr(block, "model_dump_json", None)
    return len(serialize()) if callable(serialize) else None


def configured_model(
    config: Mapping[str, str], *, on_result: Callable[[CallResult], None] | None = None,
    max_retries: int | None = None,
) -> ParseModel:
    """用配置建立调用，使用服务端 JSON Schema 约束模型回复。"""
    if config.get("AI_PLATFORM", "anthropic") != "anthropic":
        raise ConfigError("AI_PLATFORM 配置不受支持，申请解析目前只支持 Anthropic（anthropic）。")
    api_key = config.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ConfigError("缺少配置，请在 .env 中填写 ANTHROPIC_API_KEY。")
    base_url = config.get("ANTHROPIC_BASE_URL")
    model = config.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

    def complete(*, system: str, user: str) -> str:
        import httpx
        from anthropic import Anthropic, AnthropicError, DefaultHttpxClient, transform_schema
        from anthropic.types import StopReason

        def record(reply, failure: str | None) -> None:
            blocks = getattr(reply, "content", ())
            usage = getattr(reply, "usage", None)
            stop = getattr(reply, "stop_reason", None)
            on_result(CallResult(
                stop if stop is None or stop in get_args(StopReason) else "unknown",
                tuple((block.type, _block_length(block)) for block in blocks),
                getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None),
                (monotonic_ns() - started) // 1_000_000, getattr(reply, "model", model),
                "".join(block.text for block in blocks if block.type == "text") if reply is not None else None,
                failure,
            ))

        # SDK 的调试日志会展开申请正文，HTTP 日志会记录服务地址。
        for logger_name in ("anthropic", "httpx", "httpcore"):
            logging.getLogger(logger_name).setLevel(logging.WARNING)
        started = monotonic_ns() if on_result is not None else 0
        options = {}
        if max_retries is not None:
            options.update(max_retries=max_retries, http_client=DefaultHttpxClient(follow_redirects=False))
        stream = None
        try:
            with Anthropic(api_key=api_key, base_url=base_url, **options) as client:
                with client.messages.stream(
                    model=model,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    max_tokens=32000,
                    output_config={"format": {"type": "json_schema", "schema": transform_schema(ApplicationReply)}},
                ) as stream:
                    reply = stream.get_final_message()
        # 流式读取中途断开或超时时，SDK 不包装传输层异常；流在 message_start 前结束时 SDK 抛 AssertionError。
        except (AnthropicError, ValueError, httpx.HTTPError, AssertionError):
            try:
                partial = getattr(stream, "current_message_snapshot", None)
            except AssertionError:
                partial = None
            blocks = getattr(partial, "content", None)
            raw = "".join(block.text for block in blocks if block.type == "text") if isinstance(blocks, list) else ""
            if on_result is not None:
                if raw:
                    record(partial, "reply_truncated")
                else:
                    record(None, "model_call")
            if raw:
                raise ModelReplyError("reply_truncated", raw, "申请解析模型调用失败。") from None
            raise ModelError("申请解析模型调用失败。") from None

        if on_result is not None:
            record(reply, None if reply.stop_reason == "end_turn" else "model_incomplete")
        if reply.stop_reason != "end_turn":
            stop_reason = reply.stop_reason if reply.stop_reason in get_args(StopReason) else "unknown"
            output_tokens = getattr(getattr(reply, "usage", None), "output_tokens", None)
            token_count = output_tokens if type(output_tokens) is int and output_tokens >= 0 else "未知"
            code = {"max_tokens": "reply_truncated", "refusal": "reply_refused"}.get(stop_reason, "reply_incomplete")
            raw = "".join(block.text for block in getattr(reply, "content", ()) if block.type == "text")
            raise ModelReplyError(code, raw or None,
                                  f"申请解析模型回复不完整（stop_reason={stop_reason}，输出 {token_count} token）。")
        return "".join(block.text for block in reply.content if block.type == "text")

    return complete
