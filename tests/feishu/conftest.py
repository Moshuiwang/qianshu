from datetime import UTC, datetime

import pytest

from qianshu.feishu import Settings, Window, fingerprint


@pytest.fixture
def window():
    return Window(datetime.fromtimestamp(100, UTC), datetime.fromtimestamp(200, UTC))


@pytest.fixture
def settings():
    def placeholder(label):
        return f"<离线占位-{label}>"

    return Settings(
        placeholder("应用"), placeholder("密钥"), placeholder("群"), placeholder("机器人"),
        frozenset({placeholder("发送人")}), fingerprint("合成样例 ABC"),
    )
