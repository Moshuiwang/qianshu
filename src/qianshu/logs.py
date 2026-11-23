"""UTC JSON 日志。消息只传事件与统计，邮件正文放在业务数据中。"""

import json
import logging
import re
import sys
from collections.abc import Collection, Iterable, Mapping
from contextvars import ContextVar
from copy import copy
from datetime import UTC, datetime
from typing import TextIO

from qianshu.config import LOGGABLE_CONFIG_KEYS, REDACTED, Config


archive_request_fingerprint: ContextVar[str | None] = ContextVar("archive_request_fingerprint", default=None)


class _Redactor:
    _MIN_PREFIX_LENGTH = 8
    _MIN_SECRET_LENGTH = 12

    def __init__(self, values: Iterable[str], *, exact_values: Collection[str] = ()) -> None:
        literals = set()
        self._prefix_values: dict[str, list[str]] = {}
        for value in values:
            literals.update((
                value,
                repr(value)[1:-1],
                json.dumps(value)[1:-1],
                json.dumps(value, ensure_ascii=False)[1:-1],
            ))
            if len(value) >= self._MIN_SECRET_LENGTH and value not in exact_values:
                prefix = value[:self._MIN_PREFIX_LENGTH]
                self._prefix_values.setdefault(prefix, []).append(value)
                literals.add(prefix)
        self._pattern = re.compile("|".join(
            re.escape(value) for value in sorted(literals, key=len, reverse=True)
        ))

    def __call__(self, text: str) -> str:
        parts = []
        end = 0
        while match := self._pattern.search(text, end):
            start = match.start()
            parts.extend((text[end:start], REDACTED))
            end = match.end()
            prefix = text[start:start + self._MIN_PREFIX_LENGTH]
            # 找到最长的匹配前缀，无需为长正文枚举所有前缀。
            for value in self._prefix_values.get(prefix, ()):
                if text.startswith(value, start):
                    end = max(end, start + len(value))
                    continue
                limit = min(len(value), len(text) - start)
                length = self._MIN_PREFIX_LENGTH
                while length < limit and value[length] == text[start + length]:
                    length += 1
                end = max(end, start + length)
        parts.append(text[end:])
        return "".join(parts)


def _redact_argument(argument: object, redact: _Redactor) -> object:
    if isinstance(argument, Config):
        return Config({key: redact(value) for key, value in argument.items()})
    if isinstance(argument, Mapping):
        return {key: _redact_argument(value, redact) for key, value in argument.items()}
    if isinstance(argument, list):
        return [_redact_argument(item, redact) for item in argument]
    if isinstance(argument, tuple):
        return tuple(_redact_argument(item, redact) for item in argument)
    if isinstance(argument, (set, frozenset)):
        collection_type = frozenset if isinstance(argument, frozenset) else set
        return collection_type(_redact_argument(item, redact) for item in argument)
    if isinstance(argument, (bytes, bytearray)):
        return redact(argument.decode("utf-8", errors="replace"))
    text = str(argument)
    redacted = redact(text)
    # 保留未命中脱敏的内置数字，供 %d、%f 和动态宽度等格式使用。
    if type(argument) in (int, float, bool) and redacted == text:
        return argument
    return redacted


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
        self._redact = _Redactor(self._sensitive_values) if self._sensitive_values else None

    def format(self, record: logging.LogRecord) -> str:
        fingerprint = archive_request_fingerprint.get()
        if fingerprint is not None:
            record = copy(record)
            record.msg, record.args = "archive_request %s", (fingerprint,)
        redact = self._redact
        try:
            extra_values = getattr(record, "redact_values", ())
            if isinstance(extra_values, str):
                extra_values = (extra_values,)
            if extra_values:
                sensitive_values = self._sensitive_values.union(value for value in extra_values if value)
                if sensitive_values:
                    redact = _Redactor(sensitive_values)
            if redact and record.args:
                record = copy(record)
                record.args = _redact_argument(record.args, redact)
            message = record.getMessage()
        except Exception as exc:
            exception_type = type(exc).__name__
            if redact:
                exception_type = redact(exception_type)
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
        if redact:
            for key in ("logger", "message", "exception_type"):
                if key in fields:
                    fields[key] = redact(fields[key])
        return json.dumps(fields, ensure_ascii=False)


class JsonStreamHandler(logging.StreamHandler):
    """写出项目 JSON 日志，输出失败时只记录事件与异常类型。"""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        sensitive_values: Iterable[str] | Mapping[str, str] = (),
    ) -> None:
        super().__init__(stream)
        self.setFormatter(JsonFormatter(sensitive_values))

    def handleError(self, record: logging.LogRecord) -> None:
        diagnostic = {
            "message": "log_write_failed",
            "exception_type": type(sys.exception()).__name__,
        }
        try:
            sys.stderr.write(json.dumps(diagnostic) + "\n")
        except Exception:
            # stderr 本身也可能是发生故障的流。
            pass


def setup_logging(config: Mapping[str, str]) -> None:
    """为根日志器配置项目 JSON 日志。"""
    logging.basicConfig(
        handlers=[JsonStreamHandler(sensitive_values=config)],
        level=logging.INFO,
        force=True,
    )
