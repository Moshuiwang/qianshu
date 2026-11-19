"""飞书入口按本卡配置读取并限定运行窗口。"""

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values

from qianshu.config import ConfigError, load_config

_REQUIRED_EXTRA_KEYS = ("FEISHU_BOT_OPEN_ID", "FEISHU_ALLOWED_SENDER_IDS")


@dataclass(frozen=True, repr=False)
class Settings:
    app_id: str
    app_secret: str
    chat_id: str
    bot_open_id: str
    allowed_senders: frozenset[str]
    approved_fingerprint: str = ""
    tenant_key: str | None = None
    callback_max_age: float = 30

    def __post_init__(self) -> None:
        if (
            not all(value.strip() for value in (
                self.app_id, self.app_secret, self.chat_id, self.bot_open_id,
            ))
            or (self.tenant_key is not None and not self.tenant_key.strip())
            or not math.isfinite(self.callback_max_age)
            or self.callback_max_age <= 0
            or not self.allowed_senders
            or any(not value.strip() for value in self.allowed_senders)
            or (
                self.approved_fingerprint != ""
                and not re.fullmatch(r"[a-f0-9]{64}", self.approved_fingerprint)
            )
        ):
            raise ConfigError("飞书入口配置缺失或格式无效。")


def load_settings(env_file: Path = Path(".env")) -> Settings:
    base = load_config("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_CHAT_ID", env_file=env_file)
    # 公共配置的白名单由基础卡维护，本模块读取入口专用的配置键。
    try:
        with env_file.open(encoding="utf-8-sig") as stream:
            extra = dotenv_values(stream=stream, interpolate=False)
    except (OSError, UnicodeError):
        raise ConfigError("无法读取飞书入口配置。") from None
    missing = [key for key in _REQUIRED_EXTRA_KEYS if not (extra.get(key) or "").strip()]
    if missing:
        raise ConfigError(f"缺少配置，请在 .env 中填写：{'、'.join(missing)}")
    bot, senders = (extra[key].strip() for key in _REQUIRED_EXTRA_KEYS)
    fingerprint = (extra.get("FEISHU_APPROVED_FINGERPRINT") or "").strip().lower()
    try:
        max_age = float(extra.get("FEISHU_CALLBACK_MAX_AGE_SECONDS", "30"))
    except (TypeError, ValueError):
        raise ConfigError("飞书回调最长有效时间须为正数秒数。") from None
    tenant_key = extra.get("FEISHU_TENANT_KEY", "")
    if tenant_key is None:
        raise ConfigError("飞书租户配置格式无效。")
    return Settings(
        base["FEISHU_APP_ID"], base["FEISHU_APP_SECRET"], base["FEISHU_CHAT_ID"],
        bot, frozenset(value.strip() for value in senders.split(",") if value.strip()),
        fingerprint, tenant_key=tenant_key.strip() or None,
        callback_max_age=max_age,
    )


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None or self.start >= self.end:
            raise ValueError("验收窗口须提供有效的起止时刻与时区。")
        object.__setattr__(self, "start", self.start.astimezone(UTC))
        object.__setattr__(self, "end", self.end.astimezone(UTC))

    def contains(self, timestamp: float) -> bool:
        return self.start.timestamp() <= timestamp < self.end.timestamp()
