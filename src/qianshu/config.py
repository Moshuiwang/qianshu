"""从指定的 .env 文件读取外部依赖配置。"""

from pathlib import Path

from dotenv import dotenv_values

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
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
)


class ConfigError(ValueError):
    """配置文件不可读，或调用方所需的配置缺失。"""


def load_config(*required: str, env_file: Path = Path(".env")) -> dict[str, str]:
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
    return config
