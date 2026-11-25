"""从指定的 .env 文件读取外部依赖配置。"""

import re
from collections.abc import Iterator, Mapping
from pathlib import Path

from dotenv import dotenv_values
from dotenv.parser import parse_stream

CONFIG_KEYS = (
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_CHAT_ID",
    "FEISHU_WIKI_CHECK_NODE",
    "MINGDAO_ACCOUNT",
    "MINGDAO_PASSWORD",
    "MINGDAO_BASE_URL",
    "MINGDAO_APP_ID",
    "MINGDAO_PROJECT_ID",
    "MINGDAO_WORKSHEET_ID",
    "MINGDAO_VIEW_ID",
    "MINGDAO_SUBMIT_BTN_ID",
    "MAIL_HOST",
    "MAIL_PORT",
    "MAIL_USER",
    "MAIL_PASSWORD",
    "MAIL_FOLDER",
    "AI_PLATFORM",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "AI_ACTIVE_PROFILE",
    "AI_BACKUP_PLATFORM",
    "AI_BACKUP_MODEL",
    "AI_BACKUP_BASE_URL",
    "AI_BACKUP_API_KEY",
    "FEISHU_ARCHIVE_TARGETED_SUMMARY_URL",
    "FEISHU_ARCHIVE_PUBLIC_SUMMARY_URL",
    "FEISHU_ARCHIVE_LINK_SHARE_ENTITY",
)

# 端口、邮箱文件夹名和模型名仅用于选择连接端口、收件范围和模型，允许写入日志。
# 其余配置默认脱敏，包括以后新增的配置项。
LOGGABLE_CONFIG_KEYS = frozenset({
    "MAIL_PORT",
    "MAIL_FOLDER",
    "ANTHROPIC_MODEL",
})
REDACTED = "[已脱敏]"


def registered_credentials(values, official_keys=()):
    """#57 展示与出口共同登记所有非空私有配置，官方鉴权键始终纳入。"""
    display = LOGGABLE_CONFIG_KEYS | {"FEISHU_CALLBACK_MAX_AGE_SECONDS"}
    return {key: value for key, value in values.items()
            if isinstance(value, str) and value.strip() and (key not in display or key in official_keys)}


class ConfigError(ValueError):
    """配置文件不可读，或调用方所需的配置缺失。"""


class Config(Mapping[str, str]):
    """按键读取配置，文本表示只展示已填的键名。"""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        keys = ", ".join(
            key if key in LOGGABLE_CONFIG_KEYS else f"{key}={REDACTED}" for key in self
        )
        return f"Config({keys})"


def load_config(*required: str, env_file: Path = Path(".env")) -> Config:
    """读取已填写的配置，按调用方传入的键校验必填项。"""
    try:
        with env_file.open(encoding="utf-8-sig") as stream:
            raw_values = dotenv_values(stream=stream, interpolate=False)
    except FileNotFoundError:
        raw_values = {}
    except (OSError, UnicodeError):
        raise ConfigError("无法读取 .env 配置文件，请检查文件权限和 UTF-8 编码。") from None

    config = {}
    for key in CONFIG_KEYS:
        value = raw_values.get(key)
        if value and value.strip():
            config[key] = value

    missing = [key for key in required if key not in config]
    if missing:
        raise ConfigError(f"缺少配置，请在 .env 中填写：{'、'.join(missing)}")
    return Config(config)


def trial_config(path: Path, *, approval=False) -> Config:
    """从显式私有文件读取试运行开关，业务配置只在收信开启后读取。"""
    try:
        with path.open(encoding="utf-8-sig") as stream:
            values = {}
            for binding in parse_stream(stream):
                if binding.error or (binding.key and binding.key.startswith("MAIL_TRIAL_") and binding.key in values):
                    raise ConfigError("试运行开关文件有重复键或格式不明。")
                if binding.key is not None:
                    values[binding.key] = binding.value
    except (OSError, UnicodeError):
        raise ConfigError("无法读取试运行私有开关。") from None
    result = {}
    for name in ("RECEIVE", "CARD"):
        key = f"MAIL_TRIAL_{name}_ENABLED"
        value = values.get(key, "false")
        if value not in {"true", "false"}:
            raise ConfigError("试运行开关须为 true 或 false。")
        result[key] = value
    if result["MAIL_TRIAL_CARD_ENABLED"] == "true" and result["MAIL_TRIAL_RECEIVE_ENABLED"] != "true":
        raise ConfigError("出卡须先开启收信。")
    if approval:
        _approval_switches(values, result)
    limit = values.get("MAIL_TRIAL_DAILY_PARSE_LIMIT", "15")
    if not limit or not limit.isascii() or not limit.isdecimal() or len(limit) > 9:
        raise ConfigError("试运行每日解析上限须为非负整数。")
    if values.get("MAIL_TRIAL_TRIAGE_MODEL", "claude-haiku-5-5") != "claude-haiku-5-5":
        raise ConfigError("试运行分拣模型与登记不符。")
    result["MAIL_TRIAL_DAILY_PARSE_LIMIT"] = limit
    return Config(result)


class TrialModeProbe:
    """监听普通配置加载期间异步读取初始档位。SDK 线程只读取已发布的结果。"""

    def __init__(self, path):
        import _thread
        from threading import Event

        self.done = Event()
        self.values = None
        try:
            _thread.start_new_thread(self._read, (path,))
        except RuntimeError:
            self.done.set()

    def _read(self, path):
        try:
            self.values = trial_config(path)
        except Exception:
            pass
        finally:
            self.done.set()


def _approval_switches(values, result):
    window_key = "MAIL_TRIAL_MEMBER_CHECK_SECONDS"
    window = values.get(window_key, "120")
    if not isinstance(window, str) or not re.fullmatch(r"[1-9][0-9]{0,2}", window) or int(window) > 120:
        raise ConfigError("点击成员核验时限须为 1 至 120 秒。")
    result[window_key] = window
    create = values.get("MAIL_TRIAL_CREATE_ENABLED", "false")
    country = values.get("MAIL_TRIAL_CREATE_COUNTRY", "")
    members = values.get("MAIL_TRIAL_APPROVER_OPEN_IDS", "")
    if (create not in {"true", "false"} or country is None or members is None
            or (country and (country != country.strip() or not re.fullmatch(r"\w[\w -]{0,79}", country)))
            or (members and len(set(members.split(","))) != len(members.split(",")))
            or (members and not re.fullmatch(r"ou_[A-Za-z0-9]+(?:,ou_[A-Za-z0-9]+)*", members))
            or (create == "true" and (result["MAIL_TRIAL_CARD_ENABLED"] != "true" or not country or not members))):
        raise ConfigError("试运行建单配置缺失或格式无效。")
    for key in ("MAIL_TRIAL_CREATE_ENABLED", "MAIL_TRIAL_CREATE_COUNTRY", "MAIL_TRIAL_APPROVER_OPEN_IDS"):
        if key in values:
            result[key] = values[key]
    # 守望者登记的存档发布确认（X8a 合成试写 / X8b 首次真实发布）。未登记即不发布。
    purpose = values.get("MAIL_TRIAL_ARCHIVE_PURPOSE", "")
    if purpose not in {"", "X8a", "X8b"}:
        raise ConfigError("试运行存档发布确认须为 X8a 或 X8b。")
    if purpose:
        result["MAIL_TRIAL_ARCHIVE_PURPOSE"] = purpose
