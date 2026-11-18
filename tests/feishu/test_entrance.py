import json
from dataclasses import replace
from threading import Event

import pytest

from qianshu.feishu import Entrance, Message, Report, State, fingerprint, load_settings


class FakeTransport:
    def __init__(self):
        self.events = []
        self.offline = []
        self.fail_receipt = False

    def react(self, message_id):
        if self.fail_receipt:
            raise RuntimeError("synthetic failure")
        self.events.append(("reaction", message_id))

    def reply(self, message_id, text):
        self.events.append(("reply", message_id, text))

    def send_card(self, card):
        self.events.append(("card", card))
        return "<合成结果卡>"

    def history(self, start, end):
        return iter(self.offline)

    def download(self, message_id, attachment):
        self.events.append(("download", attachment.kind))
        return b"synthetic"


@pytest.fixture
def source(settings):
    return Message(
        "<合成消息>", settings.chat_id, next(iter(settings.allowed_senders)), 120_000, "text",
        json.dumps({"text": "@_user_1 合成样例\u3000ＡＢＣ\n"}), ("@_user_1",),
    )


def test_reaction_precedes_dispatch_and_duplicate_is_not_processed(tmp_path, settings, window, source):
    fake = FakeTransport()
    path = tmp_path / "private.sqlite"
    processing = Event()
    release = Event()

    def process(application):
        assert fake.events == [("reaction", source.message_id)]
        assert fingerprint(application.text) == settings.approved_fingerprint
        processing.set()
        assert release.wait(2)
        return Report("合成来源", ("合成建单",), ("合成调整",), ("合成原因",), ("合成提醒",))

    entrance = Entrance(settings, window, State(path, settings), fake, process, clock=lambda: 150)
    try:
        receipt = entrance.inject_message(source)
        work = receipt.result(timeout=2)
        assert processing.wait(2)
        assert entrance.inject_message(source).result(timeout=2) is None
        assert not work.done()
        release.set()
        work.result(timeout=2)
    finally:
        release.set()
        entrance.close()
    assert [kind for kind, *_ in fake.events] == ["reaction", "card"]
    card_text = str(fake.events[-1][1])
    for section in ("已建单（1）", "自动调整", "未建单原因", "提醒项"):
        assert section in card_text
    restarted = Entrance(settings, window, State(path, settings), fake, process, clock=lambda: 150)
    try:
        fake.offline = [source]
        restarted.recover().result(timeout=2)
        assert restarted.inject_message(source).result(timeout=2) is None
    finally:
        restarted.close()
    assert [kind for kind, *_ in fake.events] == ["reaction", "card"]


@pytest.mark.parametrize("change", [
    {"sender_id": "<非白名单占位>", "content": "不需要反序列化"},
    {"bot_mention_keys": ()},
    {"content": '{"text":"@_user_1 合成样例 ABC 附加文本"}'},
    {"kind": "image", "content": '{"image_key":"<合成图片>"}'},
])
def test_ineligible_messages_only_receive_notice(tmp_path, settings, window, source, change):
    fake = FakeTransport()
    entrance = Entrance(settings, window, State(tmp_path / "private.sqlite", settings), fake,
                        lambda app: pytest.fail("意外进入业务处理"), clock=lambda: 150)
    try:
        assert entrance.inject_message(replace(source, chat_id="<他群占位>")) is None
        assert entrance.inject_message(replace(source, created_ms=99_999)) is None
        entrance.inject_message(replace(source, **change)).result(timeout=2).result(timeout=2)
    finally:
        entrance.close()
    assert fake.events == [
        ("reaction", source.message_id), ("reply", source.message_id, "概念验证期不处理"),
    ]


def test_history_and_attachments_follow_successful_receipt(tmp_path, settings, window, source):
    fake = FakeTransport()
    fake.offline = [replace(source, kind="post", content=json.dumps({
        "title": "",
        "content": [[{"tag": "at", "user_id": "@_user_1"}, {"tag": "text", "text": "合成样例 ABC"}],
                    [{"tag": "img", "image_key": "<图片占位>"}, {"tag": "file", "file_key": "<文件占位>"}]],
    }))]

    processed = Event()

    def process(app):
        assert app.attachments == (b"synthetic", b"synthetic")
        processed.set()
        return Report("合成来源")

    entrance = Entrance(settings, window, State(tmp_path / "private.sqlite", settings), fake, process,
                        clock=lambda: 150)
    try:
        fake.fail_receipt = True
        assert entrance.inject_message(fake.offline[0]).result(timeout=2) is None
        assert not fake.events
        fake.fail_receipt = False
        entrance.recover().result(timeout=2)
        assert processed.wait(2)
    finally:
        entrance.close()
    assert [kind for kind, *_ in fake.events] == ["reaction", "download", "download", "card"]


def test_config_reads_from_dotenv(tmp_path, settings):
    path = tmp_path / ".env"
    path.write_text(
        f"FEISHU_APP_ID={settings.app_id}\nFEISHU_APP_SECRET={settings.app_secret}\n"
        f"FEISHU_CHAT_ID={settings.chat_id}\nFEISHU_BOT_OPEN_ID={settings.bot_open_id}\n"
        f"FEISHU_ALLOWED_SENDER_IDS={','.join(settings.allowed_senders)}\n"
        f"FEISHU_APPROVED_FINGERPRINT={settings.approved_fingerprint}\n",
        encoding="utf-8",
    )
    assert load_settings(path) == settings
