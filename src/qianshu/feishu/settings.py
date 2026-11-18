"""飞书入口按本卡配置读取并限定运行窗口。"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values

from qianshu.config import ConfigError, load_config

_EXTRA_KEYS = (
    "FEISHU_BOT_OPEN_ID", "FEISHU_ALLOWED_SENDER_IDS", "FEISHU_APPROVED_FINGERPRINT",
)


@dataclass(frozen=True, repr=False)
class Settings:
    app_id: str
    app_secret: str
    chat_id: str
    bot_open_id: str
    allowed_senders: frozenset[str]
    approved_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not all(value.strip() for value in (
                self.app_id, self.app_secret, self.chat_id, self.bot_open_id,
            ))
            or not self.allowed_senders
            or any(not value.strip() for value in self.allowed_senders)
            or not re.fullmatch(r"[a-f0-9]{64}", self.approved_fingerprint)
        ):
            raise ConfigError("飞书入口配置缺失或格式无效。")


def load_settings(env_file: Path = Path(".env")) -> Settings:
    base = load_config("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_CHAT_ID", env_file=env_file)
    # 公共配置的白名单由基础卡维护，本模块读取入口专用的三个键。
    try:
        with env_file.open(encoding="utf-8-sig") as stream:
            extra = dotenv_values(stream=stream, interpolate=False)
    except (OSError, UnicodeError):
        raise ConfigError("无法读取飞书入口配置。") from None
    missing = [key for key in _EXTRA_KEYS if not (extra.get(key) or "").strip()]
    if missing:
        raise ConfigError(f"缺少配置，请在 .env 中填写：{'、'.join(missing)}")
    bot, senders, fingerprint = (extra[key].strip() for key in _EXTRA_KEYS)
    return Settings(
        base["FEISHU_APP_ID"], base["FEISHU_APP_SECRET"], base["FEISHU_CHAT_ID"],
        bot, frozenset(value.strip() for value in senders.split(",") if value.strip()),
        fingerprint.lower(),
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
