"""UTC JSON 日志。消息只传事件与统计，邮件正文放在业务数据中。"""

import json
import logging
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

_SENSITIVE_CONFIG_KEYS = (
    "FEISHU_APP_SECRET",
    "MINGDAO_ACCOUNT",
    "MINGDAO_PASSWORD",
    "MAIL_USER",
    "MAIL_PASSWORD",
    "ANTHROPIC_API_KEY",
)


class JsonFormatter(logging.Formatter):
    """按行输出 JSON，替换配置值及调用方标记的敏感原文。

    初始化时可传入配置字典或敏感值序列。配置字典中只选取机密项。
    新增令牌等值可通过 extra={"redact_values": (token,)} 指定，
    单个字符串按一个值处理。输出只包含固定字段，
    异常仅记录类型，避免第三方异常携带请求参数或邮件内容。
    """

    def __init__(self, sensitive_values: Iterable[str] | Mapping[str, str] = ()) -> None:
        super().__init__()
        if isinstance(sensitive_values, Mapping):
            sensitive_values = tuple(sensitive_values.get(key, "") for key in _SENSITIVE_CONFIG_KEYS)
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
        extra_values = getattr(record, "redact_values", ())
        if isinstance(extra_values, str):
            extra_values = (extra_values,)
        if extra_values:
            pattern = self._redaction_pattern(
                self._sensitive_values.union(value for value in extra_values if value)
            )

        fields = {
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            fields["exception_type"] = record.exc_info[0].__name__
        if pattern:
            for key in ("logger", "message", "exception_type"):
                if key in fields:
                    fields[key] = pattern.sub("[已脱敏]", fields[key])
        return json.dumps(fields, ensure_ascii=False)
