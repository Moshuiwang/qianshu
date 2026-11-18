"""UTC JSON 日志。消息只传事件与统计，邮件正文放在业务数据中。"""

import json
import logging
import re
from collections.abc import Iterable, Mapping
from copy import copy
from datetime import UTC, datetime
from typing import TextIO

from qianshu.config import LOGGABLE_CONFIG_KEYS, REDACTED, Config


def _redact_argument(
    argument: object, pattern: re.Pattern[str], sensitive_values: frozenset[str]
) -> object:
    if isinstance(argument, BaseException):
        argument = str(argument)
        # 异常消息可能只保留了凭据前缀。
        if argument and any(value.startswith(argument) for value in sensitive_values):
            return REDACTED
    if isinstance(argument, str):
        return pattern.sub(REDACTED, argument)
    if isinstance(argument, Config):
        return Config({key: pattern.sub(REDACTED, argument[key]) for key in argument})
    if isinstance(argument, dict):
        return {
            _redact_argument(key, pattern, sensitive_values): _redact_argument(
                value, pattern, sensitive_values
            )
            for key, value in argument.items()
        }
    if isinstance(argument, list):
        return [_redact_argument(item, pattern, sensitive_values) for item in argument]
    if isinstance(argument, tuple):
        return tuple(_redact_argument(item, pattern, sensitive_values) for item in argument)
    return argument


class JsonFormatter(logging.Formatter):
    """按行输出 JSON，替换配置值及调用方标记的敏感原文。

    初始化时可传入配置映射或敏感值序列。配置映射中默认遮蔽所有未明确放行的值。
    新增令牌等值可通过 extra={"redact_values": (token,)} 指定，
    单个字符串按一个值处理。输出只包含固定字段，
    异常仅记录类型，避免第三方异常携带请求参数或邮件内容。
    """

    def __init__(self, sensitive_values: Iterable[str] | Mapping[str, str] = ()) -> None:
        super().__init__()
        if isinstance(sensitive_values, Mapping):
            sensitive_values = tuple(
                value for key, value in sensitive_values.items() if key not in LOGGABLE_CONFIG_KEYS
            )
        elif isinstance(sensitive_values, str):
            sensitive_values = (sensitive_values,)
        self._sensitive_values = frozenset(value for value in sensitive_values if value)
        self._pattern = self._redaction_pattern(self._sensitive_values)

    @staticmethod
    def _redaction_pattern(values: Iterable[str]) -> re.Pattern[str] | None:
        variants = set()
        for value in values:
            variants.update((
                value,
                repr(value)[1:-1],
                json.dumps(value)[1:-1],
                json.dumps(value, ensure_ascii=False)[1:-1],
            ))
        literals = sorted(variants, key=len, reverse=True)
        if literals:
            return re.compile("|".join(re.escape(value) for value in literals))
        return None

    def format(self, record: logging.LogRecord) -> str:
        pattern = self._pattern
        sensitive_values = self._sensitive_values
        try:
            extra_values = getattr(record, "redact_values", ())
            if isinstance(extra_values, str):
                extra_values = (extra_values,)
            if extra_values:
                sensitive_values = sensitive_values.union(value for value in extra_values if value)
                pattern = self._redaction_pattern(sensitive_values)
            if pattern and record.args:
                record = copy(record)
                record.args = _redact_argument(record.args, pattern, sensitive_values)
            message = record.getMessage()
        except Exception as exc:
            exception_type = type(exc).__name__
            if pattern:
                exception_type = pattern.sub(REDACTED, exception_type)
            return json.dumps(
                {"message": "log_format_failed", "exception_type": exception_type},
                ensure_ascii=False,
            )

        fields = {
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }
        if record.exc_info and record.exc_info[0]:
            fields["exception_type"] = record.exc_info[0].__name__
        if pattern:
            for key in ("logger", "message", "exception_type"):
                if key in fields:
                    fields[key] = pattern.sub(REDACTED, fields[key])
        return json.dumps(fields, ensure_ascii=False)


class JsonStreamHandler(logging.StreamHandler):
    """写出项目 JSON 日志，输出失败时静默丢弃该条日志。"""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        sensitive_values: Iterable[str] | Mapping[str, str] = (),
    ) -> None:
        super().__init__(stream)
        self.setFormatter(JsonFormatter(sensitive_values))

    def handleError(self, record: logging.LogRecord) -> None:
        """丢弃故障记录，避免标准诊断直接输出原始消息和参数。"""
