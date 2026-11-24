"""飞书入口按本卡配置读取并限定运行窗口。"""

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values

from qianshu.config import CONFIG_KEYS, LOGGABLE_CONFIG_KEYS, ConfigError, load_config

_REQUIRED_EXTRA_KEYS = ("FEISHU_BOT_OPEN_ID", "FEISHU_ALLOWED_SENDER_IDS")
_HANDOFF_SENSITIVE_KEYS = (
    frozenset(CONFIG_KEYS) | {"MAIL_APPROVER", "MAIL_PRIVATE_DIR", "MAIL_TEST_MARKER"}
) - LOGGABLE_CONFIG_KEYS


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
    handoff_sensitive_values: tuple[str, ...] = ()
    handoff_sensitive_urls: frozenset[str] = frozenset()
    handoff_member_open_ids: frozenset[str] = frozenset()

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


def _read_handoff_config(path: Path, *, allow_missing: bool = False) -> dict[str, str | None]:
    try:
        with path.open(encoding="utf-8-sig") as stream:
            return dotenv_values(stream=stream, interpolate=False)
    except FileNotFoundError:
        if allow_missing:
            return {}
    except (OSError, UnicodeError):
        pass
    raise ConfigError("无法读取飞书转交所需的私有配置。")


def _read_official_token(path: Path, business: dict[str, str | None]) -> str:
    try:
        with path.open(encoding="utf-8-sig") as stream:
            token_key = json.load(stream)["official"]["token_key"]
    except (OSError, ValueError, KeyError, TypeError):
        raise ConfigError("无法读取明道云官方接口令牌键名。") from None
    if not isinstance(token_key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_key):
        raise ConfigError("明道云官方接口令牌键名无效。")
    token = (business.get(token_key) or "").strip()
    if not token:
        raise ConfigError("业务私有配置缺少明道云官方接口令牌。")
    return token


def load_handoff_settings(
    env_file: Path, *, for_listener: bool = False, business_env_file: Path | None = None,
    mingdao_config: Path | None = None,
) -> tuple[Settings, Path]:
    """读取飞书转交配置，监听器另读取业务配置的敏感值。"""
    values = _read_handoff_config(env_file)
    owner = (values.get("FEISHU_HANDOFF_OWNER_OPEN_ID") or "").strip()
    bot = (values.get("FEISHU_BOT_OPEN_ID") or "").strip()
    if not re.fullmatch(r"ou_[A-Za-z0-9]+", owner) or owner == bot:
        raise ConfigError("FEISHU_HANDOFF_OWNER_OPEN_ID 须填写一个产品负责人的飞书 open_id。")
    raw_members = (values.get("FEISHU_HANDOFF_MEMBER_OPEN_IDS") or "").strip()
    members = frozenset(item.strip() for item in raw_members.split(",")) if raw_members else frozenset()
    if bot in members or any(not re.fullmatch(r"ou_[A-Za-z0-9]+", item) for item in members):
        raise ConfigError("FEISHU_HANDOFF_MEMBER_OPEN_IDS 格式无效。")
    path = (values.get("FEISHU_MENTION_LOG", ".private/mentions.sqlite3") or "").strip()
    if not path:
        raise ConfigError("FEISHU_MENTION_LOG 须指向私有目录中的日志文件。")
    sensitive_values = ()
    sensitive_urls = frozenset()
    if for_listener:
        business_path = business_env_file or Path(".env")
        business = values if business_path == env_file else _read_handoff_config(
            business_path, allow_missing=business_env_file is None,
        )
        sensitive = [
            (key, value.strip()) for source in (values, business) for key, value in source.items()
            if key in _HANDOFF_SENSITIVE_KEYS and value and value.strip()
        ]
        sensitive_urls = frozenset(value for key, value in sensitive if key.endswith("_URL"))
        matches = [value for key, value in sensitive if not key.endswith("_URL")]
        if mingdao_config is not None:
            matches.append(_read_official_token(mingdao_config, business))
        sensitive_values = tuple(dict.fromkeys(matches))
    settings = Settings(
        app_id=(values.get("FEISHU_APP_ID") or "").strip(),
        app_secret=(values.get("FEISHU_APP_SECRET") or "").strip(),
        chat_id=(values.get("FEISHU_CHAT_ID") or "").strip(),
        bot_open_id=bot,
        allowed_senders=frozenset({owner}),
        tenant_key=(values.get("FEISHU_TENANT_KEY") or "").strip() or None,
        handoff_member_open_ids=members,
        handoff_sensitive_values=sensitive_values,
        handoff_sensitive_urls=sensitive_urls,
    )
    return settings, Path(path)


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
