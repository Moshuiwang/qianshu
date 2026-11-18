import hashlib
import json
import sqlite3
import time
from dataclasses import replace
from threading import Event

import pytest

from qianshu.feishu import Entrance, Message, Report, State, fingerprint, load_settings


class FakeTransport:
    def __init__(self):
        self.events = []
        self.offline = []
        self.fail_receipt = False
        self.reply_attempts = []

    def react(self, message_id):
        if self.fail_receipt:
            raise RuntimeError("synthetic failure")
        self.events.append(("reaction", message_id))

    def reply(self, message_id, text, *, deadline):
        assert time.monotonic() < deadline
        self.reply_attempts.append(message_id)
        if self.fail_receipt:
            raise RuntimeError("synthetic failure")
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
    {"sender_id": "<非白名单占位>"},
    {"content": '{"text":"合成样例 ABC"}'},
    {"content": '{"text":"@_user_1 合成样例 ABC 附加文本"}'},
    {"kind": "image", "content": '{"image_key":"<合成图片>"}'},
], ids=["sender", "explicit-mention", "fingerprint", "image"])
def test_ineligible_messages_only_receive_notice(tmp_path, settings, window, source, change):
    fake = FakeTransport()
    entrance = Entrance(settings, window, State(tmp_path / "private.sqlite", settings), fake,
                        lambda app: pytest.fail("意外进入业务处理"), clock=lambda: 150)
    try:
        assert entrance.inject_message(replace(source, chat_id="<他群占位>")) is None
        assert entrance.inject_message(replace(source, created_ms=99_999)) is None
        assert entrance.inject_message(replace(source, bot_mention_keys=())) is None
        assert entrance.inject_message(replace(source, **change)).result(timeout=2) is None
    finally:
        entrance.close()
    assert fake.events == [("reply", source.message_id, "概念验证期不处理")]


@pytest.mark.parametrize("attachment", [
    {"tag": "img", "image_key": "<图片占位>"}, {"tag": "file", "file_key": "<文件占位>"},
])
def test_attachments_are_refused_before_download(tmp_path, settings, window, source, attachment):
    fake = FakeTransport()
    post = replace(source, kind="post", content=json.dumps({
        "title": "",
        "content": [[{"tag": "at", "user_id": "@_user_1"}, {"tag": "text", "text": "合成样例 ABC"}],
                    [attachment]],
    }))
    entrance = Entrance(settings, window, State(tmp_path / "private.sqlite", settings), fake,
                        lambda app: pytest.fail("带附件的消息进入业务处理"), clock=lambda: 150)
    try:
        entrance.inject_message(post).result(timeout=2)
    finally:
        entrance.close()
    assert fake.events == [("reply", source.message_id, "概念验证期不处理")]

@pytest.mark.parametrize("history", [False, True], ids=["redelivery", "history"])
def test_failed_notice_is_retried_once_after_restart(tmp_path, settings, window, source, history):
    fake = FakeTransport()
    message = replace(source, sender_id="<非白名单占位>")
    fake.offline = [message, message]
    path = tmp_path / "private.sqlite"

    def make_entrance():
        return Entrance(settings, window, State(path, settings), fake,
                        lambda app: pytest.fail("意外进入业务处理"), clock=lambda: 150)

    entrance = make_entrance()
    try:
        fake.fail_receipt = True
        entrance.inject_message(message).result(timeout=2)
        assert entrance.state.message_status(message.message_id) == "notice_pending"
        assert not fake.events
    finally:
        entrance.close()
    entrance = make_entrance()
    try:
        fake.fail_receipt = False
        if history:
            entrance.recover().result(timeout=2)
        else:
            entrance.inject_message(message).result(timeout=2)
        entrance.recover().result(timeout=2)
        entrance.inject_message(message).result(timeout=2)
    finally:
        entrance.close()
    assert fake.reply_attempts == [message.message_id, message.message_id]
    assert fake.events == [("reply", message.message_id, "概念验证期不处理")]


def test_pending_notice_merges_active_retry_with_live_and_history(tmp_path, settings, window, source):
    fake = FakeTransport()
    message = replace(source, sender_id="<非白名单占位>")
    fake.offline = [message]
    attempts = []
    retry_started, release_retry, completed = Event(), Event(), Event()
    state = State(tmp_path / "private.sqlite", settings)
    complete_notice = state.complete_notice

    def reply(message_id, text, *, deadline):
        attempts.append(message_id)
        if len(attempts) == 1:
            raise OSError("合成 REST 网络中断")
        retry_started.set()
        assert release_retry.wait(3)

    def complete(message_id):
        complete_notice(message_id)
        completed.set()

    fake.reply = reply
    state.complete_notice = complete
    entrance = Entrance(settings, window, state, fake,
                        lambda app: pytest.fail("意外进入业务处理"), clock=lambda: 150)
    try:
        entrance.inject_message(message).result(timeout=2)
        assert retry_started.wait(2)
        entrance.inject_message(message).result(timeout=2)
        entrance.recover().result(timeout=2)
        assert attempts == [message.message_id, message.message_id]
        release_retry.set()
        assert completed.wait(2)
        assert state.message_status(message.message_id) == "done"
    finally:
        release_retry.set()
        entrance.close()


def test_notice_is_sent_while_business_is_blocked(tmp_path, settings, window, source):
    fake = FakeTransport()
    started, release = Event(), Event()

    def process(app):
        started.set()
        assert release.wait(5)
        return Report("合成来源")

    entrance = Entrance(settings, window, State(tmp_path / "private.sqlite", settings), fake,
                        process, clock=lambda: 150)
    try:
        entrance.inject_message(source).result(timeout=2)
        assert started.wait(2)
        rejected = replace(source, message_id="<另一个消息占位>", sender_id="<非白名单占位>")
        entrance.inject_message(rejected).result(timeout=2)
        assert fake.events == [
            ("reaction", source.message_id), ("reply", rejected.message_id, "概念验证期不处理"),
        ]
    finally:
        release.set()
        entrance.close()


def test_expired_receipt_budget_leaves_notice_pending(tmp_path, settings, window, source):
    fake = FakeTransport()
    message = replace(source, sender_id="<非白名单占位>")
    fake.offline = [message]
    entrance = Entrance(settings, window, State(tmp_path / "private.sqlite", settings), fake,
                        lambda app: pytest.fail("意外进入业务处理"), clock=lambda: 150)
    try:
        entrance._receive(message, time.monotonic() - 1)
        assert not fake.reply_attempts
        assert entrance.state.message_status(message.message_id) == "notice_pending"
        entrance.recover().result(timeout=2)
        assert fake.reply_attempts == [message.message_id]
    finally:
        entrance.close()


def test_existing_message_claims_remain_deduplicated(tmp_path, settings, source):
    path = tmp_path / "private.sqlite"
    path.touch(mode=0o600)
    scope = hashlib.sha256(f"{settings.app_id}\0{settings.chat_id}".encode()).hexdigest()
    digest = hashlib.sha256(source.message_id.encode()).hexdigest()
    with sqlite3.connect(path) as db:
        db.execute("""
            CREATE TABLE feishu_messages (
                scope TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY (scope, digest)
            )
        """)
        db.execute("INSERT INTO feishu_messages VALUES (?, ?)", (scope, digest))

    state = State(path, settings)
    assert state.message_status(source.message_id) == "done"
    assert not state.claim_message(source.message_id)


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
