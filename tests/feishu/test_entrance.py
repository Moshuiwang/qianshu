import hashlib
import json
import sqlite3
import time
from dataclasses import replace
from threading import Event

import pytest

from qianshu.config import ConfigError
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


@pytest.mark.parametrize("check_fingerprint", [True, False], ids=["configured", "empty"])
def test_different_body_requires_matching_fingerprint_only_when_configured(
    tmp_path, settings, window, source, check_fingerprint,
):
    if not check_fingerprint:
        settings = replace(settings, approved_fingerprint="")
    body = " 合成的不同正文　ＡＢＣ\n"
    message = replace(source, content=json.dumps({"text": f"@_user_1{body}"}))
    fake = FakeTransport()
    processed = []

    def process(application):
        processed.append(application.text)
        return Report("合成来源")

    entrance = Entrance(settings, window, State(tmp_path / "private.sqlite", settings), fake,
                        process, clock=lambda: 150)
    try:
        work = entrance.inject_message(message).result(timeout=2)
        if check_fingerprint:
            assert work is None
        else:
            work.result(timeout=2)
    finally:
        entrance.close()
    if check_fingerprint:
        assert processed == []
        assert fake.events == [("reply", message.message_id, "概念验证期不处理")]
    else:
        assert processed == [body]
        assert [kind for kind, *_ in fake.events] == ["reaction", "card"]


@pytest.mark.parametrize("check_fingerprint", [True, False], ids=["configured", "empty"])
@pytest.mark.parametrize("change", [
    {"sender_id": "<非白名单占位>"},
    {"content": '{"text":"合成样例 ABC"}'},
    {"content": '{"text":"@_user_1  "}'},
    {"kind": "image", "content": '{"image_key":"<合成图片>"}'},
], ids=["sender", "explicit-mention", "blank-body", "image"])
def test_ineligible_messages_only_receive_notice(tmp_path, settings, window, source, change, check_fingerprint):
    if not check_fingerprint:
        settings = replace(settings, approved_fingerprint="")
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


@pytest.mark.parametrize("check_fingerprint", [True, False], ids=["configured", "empty"])
@pytest.mark.parametrize("attachment", [
    {"tag": "img", "image_key": "<图片占位>"}, {"tag": "file", "file_key": "<文件占位>"},
])
def test_attachments_are_refused_before_download(tmp_path, settings, window, source, attachment, check_fingerprint):
    if not check_fingerprint:
        settings = replace(settings, approved_fingerprint="")
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


@pytest.mark.parametrize("fingerprint_value", [
    "uppercase", "", None, "a" * 63, "g" * 64,
], ids=["uppercase", "empty", "omitted", "wrong-length", "non-hex"])
def test_config_reads_from_dotenv(tmp_path, settings, fingerprint_value):
    if fingerprint_value == "uppercase":
        fingerprint_value = settings.approved_fingerprint.upper()
    fingerprint_line = (
        "" if fingerprint_value is None else f"FEISHU_APPROVED_FINGERPRINT={fingerprint_value}\n"
    )
    path = tmp_path / ".env"
    path.write_text(
        f"FEISHU_APP_ID={settings.app_id}\nFEISHU_APP_SECRET={settings.app_secret}\n"
        f"FEISHU_CHAT_ID={settings.chat_id}\nFEISHU_BOT_OPEN_ID={settings.bot_open_id}\n"
        f"FEISHU_ALLOWED_SENDER_IDS={','.join(settings.allowed_senders)}\n"
        + fingerprint_line,
        encoding="utf-8",
    )
    if not fingerprint_value:
        settings = replace(settings, approved_fingerprint="")
    elif fingerprint_value.lower() != settings.approved_fingerprint:
        with pytest.raises(ConfigError, match="配置缺失或格式无效"):
            load_settings(path)
        return
    assert load_settings(path) == settings
    tenant = "<租户配置占位>"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"FEISHU_TENANT_KEY={tenant}\nFEISHU_CALLBACK_MAX_AGE_SECONDS=2.5\n")
    assert load_settings(path) == replace(settings, tenant_key=tenant, callback_max_age=2.5)


def _run_crash_case(directory, settings, window, source, stop, now=150, history=True):
    import os

    from qianshu.__main__ import _EntranceTransport, _processor
    from qianshu.feishu.cards import detail_pages

    def checkpoint(name):
        if stop == name:
            os._exit(41)

    def record(name):
        with (directory / "events").open("a") as stream:
            print(name, file=stream)

    class Transport(FakeTransport):
        def react(self, message_id):
            record("reaction")
            checkpoint("reaction accepted")

        def send_card(self, card):
            record("generic card")
            checkpoint("unkeyed send")

    def pipeline(application):
        record("parse")
        checkpoint("parsing")
        return detail_pages("合成申请", [])

    def send(card, request_id, notice, check):
        check()
        record("manual request" if notice else "page request")
        # 录制传输以平台 UUID 合并确认结果。
        path = directory / request_id
        if not path.exists():
            path.touch()
            record("manual" if notice else "page")
        if notice:
            assert "人工核对" in str(card) and hashlib.sha256(source.message_id.encode()).hexdigest() in str(card)
        checkpoint("manual accepted" if notice else "page accepted")

    processor = _processor(pipeline, send, window, directory / "pages.sqlite", clock=lambda: now)
    deliver = processor._deliver

    def deliver_with_checkpoints(digest):
        checkpoint("snapshot saved")
        deliver(digest)
        checkpoint("page confirmed")

    processor._deliver = deliver_with_checkpoints
    state = State(directory / "entry.sqlite", settings)
    advance = state.advance_message

    def advance_and_stop(mid, previous, current):
        advance(mid, previous, current)
        checkpoint(current)

    state.advance_message = advance_and_stop
    transport = Transport()
    if stop == "received":
        claim = state.receive

        def claim_then_stop(*args):
            claim(*args)
            checkpoint("received")

        state.receive = claim_then_stop
    if stop in ("manual prepared", "manual accepted"):
        def fail_reaction(mid):
            raise TimeoutError

        transport.react = fail_reaction
        start = state.start_manual_notice

        def start_then_stop(*args):
            start(*args)
            checkpoint("manual prepared")

        state.start_manual_notice = start_then_stop
    generic = stop == "unkeyed send"
    entrance = Entrance(
        settings, window, state, _EntranceTransport(transport, processor.resume), (lambda app: Report("合成来源")) if generic else processor,
        clock=lambda: now, snapshot_status=None if generic else processor.snapshot_status,
    )
    try:
        if generic:
            entrance.manual_send = processor.send_manual
        else:
            processor.attach(entrance, transport)
        if stop is None:
            transport.offline = [source] if history is True else []
            if history == "error":
                def fail_history(start, end):
                    raise OSError

                entrance.transport._transport.history = fail_history
            futures = []
            receive = entrance._receive

            def track(*args):
                future = receive(*args)
                if future is not None:
                    futures.append(future)
                return future

            entrance._receive = track
            entrance.recover().result(timeout=5)
            for future in futures:
                future.result(timeout=5)
        for _ in range(2):
            receipt = entrance.inject_message(source)
            work = receipt.result(timeout=5) if receipt is not None else None
            if work is not None:
                work.result(timeout=5)
        return state.message_status(source.message_id)
    finally:
        entrance.close()
        state.close()


@pytest.mark.parametrize(("stop", "manual"), [
    ("received", False), ("reacting", True), ("reaction accepted", True), ("ready", False),
    ("processing", False), ("parsing", False), ("snapshot saved", False), ("page accepted", False),
    ("page confirmed", False), ("report_saved", False),
    ("unkeyed send", True), ("manual prepared", True), ("manual accepted", True),
])
def test_process_exit_and_restart(tmp_path, settings, window, source, stop, manual):
    _crash(tmp_path, settings, window, source, stop)
    status = _run_crash_case(tmp_path, settings, window, source, None)
    events = (tmp_path / "events").read_text().splitlines()
    assert status == ("manual_notified" if manual else "done")
    assert events.count("reaction") <= 1
    assert events.count("manual") == int(manual)
    assert events.count("page") == int(not manual)
    assert events.count("page request") == int(not manual) + int(stop == "page accepted")
    assert events.count("generic card") == int(stop == "unkeyed send")
    assert events.count("parse") == (0 if manual else 1 + int(stop == "parsing"))


def test_concurrent_entrances_share_processing_lease(tmp_path, settings, window, source):
    entered, release = Event(), Event()
    fake = FakeTransport()
    path = tmp_path / "entry.sqlite"

    def process(app):
        entered.set()
        assert release.wait(4)
        return Report("合成来源")

    states = [State(path, settings) for _ in range(2)]
    first, second = [Entrance(settings, window, state, fake, process, clock=lambda: 150) for state in states]
    try:
        work = first.inject_message(source).result(timeout=2)
        assert entered.wait(2)
        fake.offline = [source]
        second.recover().result(timeout=2)
        assert second.inject_message(source).result(timeout=2) is None
        release.set()
        work.result(timeout=2)
        assert second.inject_message(source).result(timeout=2) is None
        assert [event[0] for event in fake.events] == ["reaction", "card"]
    finally:
        release.set()
        first.close()
        second.close()
        for state in states:
            state.close()



def _crash(tmp_path, settings, window, source, stop):
    from multiprocessing import get_context

    context = get_context("fork")
    process = context.Process(target=_run_crash_case, args=(tmp_path, settings, window, source, stop))
    process.start()
    process.join(60)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail("合成进程未到达崩溃注入点")
    assert process.exitcode == 41


@pytest.mark.parametrize("history", [False, "error"])
def test_history_unavailable_after_reaction(tmp_path, settings, window, source, history):
    _crash(tmp_path, settings, window, source, "ready")
    assert _run_crash_case(tmp_path, settings, window, source, None, history=history) == "manual_notified"
    assert (tmp_path / "events").read_text().splitlines() == ["reaction", "manual request", "manual"]


@pytest.mark.parametrize("stop", ["page accepted", "manual accepted"])
def test_first_attempt_outside_idempotency_period(tmp_path, settings, window, source, stop, capsys):
    from datetime import timedelta
    from qianshu.__main__ import main

    window = replace(window, end=window.end + timedelta(hours=2))
    _crash(tmp_path, settings, window, source, stop)
    events = (tmp_path / "events").read_bytes()
    assert _run_crash_case(tmp_path, settings, window, source, None, now=3750) == "listed"
    assert (tmp_path / "events").read_bytes() == events
    assert main(["--list-recovery", "--state", str(tmp_path / "entry.sqlite")]) == 0
    output = capsys.readouterr()
    assert "未完成记录数 1" in output.err
    row, = json.loads(output.out)
    assert row["digest"] == hashlib.sha256(source.message_id.encode()).hexdigest()
    assert row["created_ms"] == source.created_ms and row["stage"] == "listed"


def test_expired_window_and_rollback_keep_new_claims(tmp_path, settings, window, source, capsys):
    from qianshu.__main__ import main

    complete = replace(source, message_id="合成已确认消息")
    assert _run_crash_case(tmp_path, settings, window, complete, False) == "done"
    _crash(tmp_path, settings, window, source, "ready")
    events = (tmp_path / "events").read_bytes()
    assert _run_crash_case(tmp_path, settings, window, source, None, now=201) == "ready"
    path = tmp_path / "entry.sqlite"
    # 旧入口先按原键读 status，done 在表情、旧提示和解析之前直接返回。
    with sqlite3.connect(path) as old_db:
        for mid in (source.message_id, complete.message_id):
            digest = hashlib.sha256(mid.encode()).hexdigest()
            assert old_db.execute(
                "SELECT status FROM feishu_messages WHERE digest = ?", (digest,),
            ).fetchone() == ("done",)
        assert old_db.execute("SELECT COUNT(*) FROM feishu_approvals").fetchone() == (0,)
    for _ in range(2):
        assert main(["--list-recovery", "--state", str(path)]) == 0
        row, = json.loads(capsys.readouterr().out)
        assert row["digest"] == hashlib.sha256(source.message_id.encode()).hexdigest()
        assert row["stage"] == "ready" and row["expires"] == 200
    assert (tmp_path / "events").read_bytes() == events


def test_old_mixed_states_survive_interrupted_migration(tmp_path, settings, window, source):
    import os
    from contextlib import contextmanager
    from multiprocessing import get_context

    path = tmp_path / "legacy.sqlite"
    path.touch(mode=0o600)
    scope = hashlib.sha256(f"{settings.app_id}\0{settings.chat_id}".encode()).hexdigest()
    digest = hashlib.sha256(source.message_id.encode()).hexdigest()
    legacy_rows = [(scope, digest, "notice_pending"), ("other", digest, "done"), (scope, "unknown", "unknown")]
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE feishu_messages(scope, digest, status, PRIMARY KEY(scope, digest))")
        db.executemany("INSERT INTO feishu_messages VALUES(?, ?, ?)", legacy_rows)

    class Interrupted(State):
        @contextmanager
        def _write(self):
            with super()._write() as db:
                yield db
                os._exit(41)

    child = get_context("fork").Process(target=Interrupted, args=(path, settings))
    child.start()
    child.join(5)
    assert child.exitcode == 41
    with sqlite3.connect(path) as db:
        assert [col[1] for col in db.execute("PRAGMA table_info(feishu_messages)")] == ["scope", "digest", "status"]
    for _ in range(2):
        State(path, settings).close()
    transport = FakeTransport()
    state = State(path, settings)
    entrance = Entrance(settings, window, state, transport, lambda app: pytest.fail(), clock=lambda: 150)
    try:
        assert entrance.inject_message(source).result(timeout=2) is None
        assert transport.events == [("reply", source.message_id, "概念验证期不处理")]
        assert [row["digest"] for row in state.recovery_list(path)] == ["unknown"]
        with state.connection() as db:
            assert db.execute("SELECT count(*) FROM feishu_messages").fetchone() == (3,)
            assert db.execute("SELECT count(*) FROM feishu_approvals").fetchone() == (0,)
    finally:
        entrance.close()
        state.close()


@pytest.mark.parametrize("manual", [False, True])
def test_two_instances_replay_during_snapshot_or_manual_delivery(tmp_path, settings, window, source, manual):
    from qianshu.__main__ import _processor
    from qianshu.feishu.cards import detail_pages

    calls, requests = [], []
    paused, release = Event(), Event()
    states, entrances = [], []

    def pipeline(app):
        calls.append("parse")
        return detail_pages("合成结果", [])

    def send(card, request_id, notice, check):
        check()
        requests.append((request_id, notice))
        if manual:
            paused.set()
            assert release.wait(5)

    def react(mid):
        calls.append("reaction")
        if manual:
            raise TimeoutError

    try:
        for _ in range(2):
            transport = FakeTransport()
            transport.react = react
            transport.offline = [source]
            state = State(tmp_path / "entry.sqlite", settings)
            states.append(state)
            processor = _processor(pipeline, send, window, tmp_path / "pages.sqlite", clock=lambda: 150)
            entrance = Entrance(settings, window, state, transport, processor, clock=lambda: 150)
            processor.attach(entrance, transport)
            entrances.append(entrance)
            if len(entrances) == 1 and not manual:
                deliver = processor._deliver

                def paused_delivery(digest):
                    paused.set()
                    assert release.wait(5)
                    deliver(digest)

                processor._deliver = paused_delivery
        first, other = entrances
        work = first.inject_message(source).result(2)
        assert paused.wait(2)
        other.recover().result(2)
        assert other.inject_message(source).result(2) is None
        release.set()
        work.result(2)
        other.recover().result(2)
        assert other.inject_message(source).result(2) is None
        assert calls == (["reaction"] if manual else ["reaction", "parse"])
        assert [notice for _, notice in requests] == [manual]
        assert states[0].message_status(source.message_id) == ("manual_notified" if manual else "done")
    finally:
        release.set()
        for entrance in entrances:
            entrance.close()
        for state in states:
            state.close()


def test_unconfirmed_notification_stops_after_three_requests(tmp_path, settings, window, source, monkeypatch):
    import qianshu.feishu.entrance as module

    monkeypatch.setattr(module, "_NOTICE_RETRY_DELAY", 0.01)
    fake = FakeTransport()
    fake.fail_receipt = True
    calls = []
    state = State(tmp_path / "entry.sqlite", settings)
    entrance = Entrance(settings, window, state, fake, lambda app: pytest.fail(), clock=lambda: 150)

    def unavailable(card, digest, since):
        calls.append((digest, since))
        raise OSError

    entrance.manual_send = unavailable
    try:
        entrance.inject_message(source).result(2).result(2)
        until = time.monotonic() + 3
        while state.message_status(source.message_id) != "listed" and time.monotonic() < until:
            time.sleep(0.01)
        assert state.message_status(source.message_id) == "listed"
        assert len(calls) == 3 and len(set(calls)) == 1
        entrance.recover().result(2)
        assert len(calls) == 3
    finally:
        entrance.close()
        state.close()


@pytest.mark.parametrize("kind", ["missing", "corrupt", "other binding"])
def test_incomplete_snapshot_requires_original_binding_and_source(tmp_path, settings, window, source, kind):
    _crash(tmp_path, settings, window, source, "snapshot saved")
    if kind != "missing":
        with sqlite3.connect(tmp_path / "pages.sqlite") as db:
            if kind == "corrupt":
                db.execute("UPDATE reports SET pages = '[]'")
            else:
                db.execute("UPDATE reports SET binding = 'other'")
    status = _run_crash_case(tmp_path, settings, window, source, None, history=kind != "missing")
    events = (tmp_path / "events").read_text().splitlines()
    assert status == ("listed" if kind == "other binding" else "manual_notified")
    assert events.count("page request") == 0
    assert events.count("manual") == int(kind != "other binding")
    assert events.count("parse") == 1


def test_normal_window_close_logs_unfinished_after_reaction(
    tmp_path, settings, window, source, monkeypatch, caplog,
):
    from types import SimpleNamespace
    import qianshu.__main__ as entry

    now = 150
    path = tmp_path / "entry.sqlite"
    transport = FakeTransport()

    def react_and_close_window(message_id):
        nonlocal now
        transport.events.append(("reaction", message_id))
        now = window.end.timestamp()

    transport.react = react_and_close_window

    def service(settings, window, path, processor):
        state = State(path, settings)
        entrance = Entrance(settings, window, state, transport, processor, clock=lambda: now)

        def run():
            try:
                entrance.recover().result(timeout=5)
                entrance.inject_message(source).result(timeout=5).result(timeout=5)
            finally:
                entrance.close()
                state.close()

        return SimpleNamespace(run=run, entrance=entrance, transport=transport)

    monkeypatch.setattr(entry, "time", SimpleNamespace(time=lambda: now))
    monkeypatch.setattr(entry, "FeishuService", service)
    monkeypatch.setattr(entry, "load_model_config", lambda **kwargs: None)
    monkeypatch.setattr(entry, "configured_model", lambda config: None)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    args = SimpleNamespace(state=path, raw_output_dir=None, env_file=None,
                           companies_file=None, legacy_companies=False)
    entry._serve(args, settings, window, {}, (), {})
    row, = State.recovery_list(path)
    assert row["digest"] == hashlib.sha256(source.message_id.encode()).hexdigest()
    assert row["stage"] == "ready"
    assert transport.events == [("reaction", source.message_id)]
    assert caplog.messages.count("feishu_recovery_unfinished count=1") == 1


def test_report_saved_restart_resumes_only_pending_page(tmp_path, settings, window, source, monkeypatch):
    import qianshu.__main__ as entry
    from qianshu.feishu.cards import detail_pages

    factory = entry._processor
    digest = hashlib.sha256(source.message_id.encode()).hexdigest()
    second_id, _ = entry._ReportProcessor._payload(digest, 2, {})

    def two_page_processor(pipeline, send, *args, **kwargs):
        def build_pages(application):
            return (*pipeline(application), *detail_pages("合成续页", []))

        def interrupt_second_page(card, request_id, notice, check):
            check()
            if request_id == second_id:
                raise TimeoutError
            if notice:
                # 合成平台确认第二页发送失败后的中断卡。
                return
            send(card, request_id, notice, check)

        return factory(build_pages, interrupt_second_page, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(entry, "_processor", two_page_processor)
        _crash(tmp_path, settings, window, source, "report_saved")
    row, = State.recovery_list(tmp_path / "entry.sqlite")
    assert row["stage"] == "report_saved"
    with sqlite3.connect(tmp_path / "pages.sqlite") as db:
        assert db.execute("SELECT sent_pages, total_pages FROM reports").fetchone() == (1, 2)
    assert not (tmp_path / second_id).exists()
    events = tmp_path / "events"
    before = events.read_text().splitlines()
    assert _run_crash_case(tmp_path, settings, window, source, None) == "done"
    assert events.read_text().splitlines()[len(before):] == ["page request", "page"]
    assert (tmp_path / second_id).is_file()
    assert State.recovery_list(tmp_path / "entry.sqlite") == []
