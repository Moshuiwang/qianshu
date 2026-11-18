import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from qianshu.config import CONFIG_KEYS, ConfigError, load_config

ROOT = Path(__file__).resolve().parents[1]


def test_load_config_reads_synthetic_values_for_all_services(tmp_path):
    env_file = tmp_path / "synthetic.env"
    values = {key: f"占位-{uuid4().hex}=#${{UNEXPANDED}}" for key in CONFIG_KEYS}
    env_file.write_text(
        "\n".join(f'{key}="{value}"' for key, value in values.items())
        + '\nUNRELATED="忽略该项"\n',
        encoding="utf-8",
    )

    assert load_config(*CONFIG_KEYS, env_file=env_file) == values


def test_missing_required_values_list_only_key_names(tmp_path):
    env_file = tmp_path / "synthetic.env"
    env_file.write_text('FEISHU_APP_ID=\nFEISHU_APP_SECRET="   "\n', encoding="utf-8")

    with pytest.raises(ConfigError, match=r"缺少配置.*FEISHU_APP_ID、FEISHU_APP_SECRET"):
        load_config("FEISHU_APP_ID", "FEISHU_APP_SECRET", env_file=env_file)


def test_env_example_contains_only_config_keys():
    template = ROOT / ".env.example"
    entries = [
        line for line in template.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]

    assert entries == [f"{key}=" for key in CONFIG_KEYS]
    assert load_config(env_file=template) == {}


@pytest.mark.parametrize(
    ("script_name", "required_key"),
    [
        ("check_feishu.py", "FEISHU_APP_ID"),
        ("check_mingdao_login.py", "MINGDAO_ACCOUNT"),
    ],
)
def test_check_script_stops_before_connecting_without_config(tmp_path, script_name, required_key):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script = scripts_dir / script_name
    script.write_bytes((ROOT / "scripts" / script_name).read_bytes())

    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=10,
    )

    assert result.returncode == 1
    assert "缺少配置" in result.stderr
    assert required_key in result.stderr
