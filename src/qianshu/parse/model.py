"""申请解析的模型调用边界。"""

import logging
from collections.abc import Mapping
from typing import Protocol

from qianshu.config import ConfigError

DEFAULT_MODEL = "claude-opus-5-5"


class ModelError(RuntimeError):
    """调用失败或模型回复不完整。"""


class ParseModel(Protocol):
    def __call__(self, *, system: str, user: str) -> str: ...


def configured_model(config: Mapping[str, str]) -> ParseModel:
    """用配置建立调用，调用方只接触系统消息、用户消息及文本回复。"""
    if config.get("AI_PLATFORM", "anthropic") != "anthropic":
        raise ConfigError("申请解析目前只支持 Anthropic 平台。")
    api_key = config.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ConfigError("缺少配置，请在 .env 中填写 ANTHROPIC_API_KEY。")
    base_url = config.get("ANTHROPIC_BASE_URL")
    model = config.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

    def complete(*, system: str, user: str) -> str:
        from anthropic import Anthropic, AnthropicError

        # SDK 的调试日志会展开申请正文，HTTP 日志会记录服务地址。
        for logger_name in ("anthropic", "httpx", "httpcore"):
            logging.getLogger(logger_name).setLevel(logging.WARNING)
        try:
            with Anthropic(api_key=api_key, base_url=base_url) as client:
                reply = client.messages.create(
                    model=model,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    max_tokens=8192,
                )
        except (AnthropicError, ValueError):
            raise ModelError("申请解析模型调用失败。") from None

        if reply.stop_reason != "end_turn":
            raise ModelError("申请解析模型回复不完整。")
        return "".join(block.text for block in reply.content if block.type == "text")

    return complete
