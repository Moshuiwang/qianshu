import json
import logging
import sqlite3
import sys
import unicodedata
from contextlib import closing, nullcontext
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from threading import Event

import httpx
import pytest

from tests.feishu.startup_replay import observe
from trial_segments import Inbox, Segments, fixture_item, synthetic

from qianshu.ai import anthropic
from qianshu.config import ConfigError, trial_config
from qianshu.mail.imap import MailError, ReadOnlyMailbox
from qianshu.mail.trial import Trial
from qianshu.mail.trial_state import Hold, Store


def test_switch_journey_queue_budget_attachments_and_rebuild(tmp_path, monkeypatch):
    store = Store(tmp_path / "trial")
    config = {"MAIL_HOST": "synthetic", "MAIL_USER": "local", "ANTHROPIC_API_KEY": "test-key"}
    switches = {"MAIL_TRIAL_RECEIVE_ENABLED": "false", "MAIL_TRIAL_CARD_ENABLED": "false",
                "MAIL_TRIAL_DAILY_PARSE_LIMIT": "1"}
    box = Inbox()
    opened, sent, requests = [], [], []
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]

    def receive(req):
        if req.method == "GET":
            assert not req.content
            return httpx.Response(200, json={"type": "model", "id": req.url.path.split("/")[-1],
                                  "max_input_tokens": 1000000, "display_name": "synthetic",
                                  "created_at": "2026-01-01T00:00:00Z"})
        data = json.loads(req.content)
        assert b"ATTACHMENT_SENTINEL" not in req.content
        requests.append(data["model"])
        if data["model"] == "claude-sonnet-5-5":
            raise httpx.ReadTimeout("synthetic")
        fields = json.loads(data["messages"][0]["content"])
        text = {"first": "是", "discard": "否", "uncertain": "unrecognized"}.get(fields["body"], "是")
        return httpx.Response(200, json={"type": "message", "id": "msg", "role": "assistant",
            "model": data["model"], "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 2}})

    monkeypatch.setattr(anthropic, "_direct_transport", lambda _: httpx.MockTransport(receive))
    def restart():
        return Trial(store, config, lambda: switches,
                     lambda content, uid, card, check: check() or sent.append((content, uid, card)), Event(),
                     connector=lambda _: opened.append(1) or nullcontext(box), clock=lambda: clock[0],
                     companies=Path("tests/parse/fixtures/companies.tsv"))

    trial = restart()
    trial.poll()
    assert not opened
    switches["MAIL_TRIAL_RECEIVE_ENABLED"] = "true"
    trial = restart()
    trial.poll()
    assert not box.reads
    for i, text in enumerate(("first", "discard", "uncertain"), 2):
        box.add(synthetic(i, text, attachment=i == 2))
    box.seen.add(3)
    trial.poll()
    assert not requests
    assert not store.row("SELECT 1 FROM registration WHERE verdict IS NOT NULL")
    store.import_spend("0")
    trial.poll()
    assert requests == ["claude-haiku-5-5"] * 3
    assert [r[0] for r in store.db.execute("SELECT verdict FROM registration ORDER BY id")] == ["是", "否", "拿不准"]
    assert [x[0] for x in sent] == ["收到，待核对 1 封。"] * 2
    assert store.batch(1, 1) == (1, 1, 1, "附件未解析")
    switches["MAIL_TRIAL_CARD_ENABLED"] = "true"
    trial = restart()
    trial.poll()
    assert requests.count("claude-sonnet-5-5") == 1
    assert "含 1 个附件未解析，请对照原件核对" in json.dumps(sent, ensure_ascii=False)
    assert store.row("SELECT count(*) FROM registration WHERE parse_day IS NOT NULL")[0] == 1
    assert sum("顺延" in str(entry) for entry in sent) == 1
    trial.store = Store(store.path)
    trial.poll()
    assert requests.count("claude-sonnet-5-5") == 1
    clock[0] += timedelta(days=1)
    trial.poll()
    assert requests.count("claude-sonnet-5-5") == 2
    assert store.row("SELECT count(*) FROM registration WHERE status='manual'")[0] == 2
    assert box.seen == {1, 3} and 1 not in box.reads
    box.add(box.messages[2])
    trial.poll()
    assert len(requests) == 5 and store.row("SELECT count(*) FROM registration")[0] == 3
    box.generation += 1
    trial.poll()
    assert trial.box()["state"] == "frozen"
    assert store.row("SELECT count(*) FROM reconciliation")[0] == len(box.messages)
    assert len(requests) == 5


def test_interval_and_checked_literals_only_read_matching_uid():
    class Wire:
        def uid(self, command, uid, query):
            assert (command, uid, query) == ("FETCH", "3", "(BODY.PEEK[TEXT])")
            return "OK", [(b"2 (BODY[TEXT] {4}", b"text"), self.tail]

    wire = Wire()
    mailbox = ReadOnlyMailbox(wire)
    wire.tail = b" UID 3)"
    assert mailbox.checked_section(3, "TEXT") == b"text"
    wire.tail = b" UID 2)"
    with pytest.raises(MailError):
        mailbox.checked_section(3, "TEXT")


def test_atomic_last_allowance_and_unknown_after_restart(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    path = tmp_path / "db"
    Store(path).import_spend("11")
    ready = Barrier(2)
    def reserve():
        store = Store(path)
        ready.wait()
        try:
            return store.reserve("sonnet", 128000, 2000000, 1)
        except Hold:
            return None
    with ThreadPoolExecutor(2) as pool:
        results = [pool.submit(reserve) for _ in range(2)]
    assert sum(result.result() is not None for result in results) == 1
    store = Store(path)
    assert store.row("SELECT count(*) FROM charge WHERE inputs IS NULL")[0] == 1
    for model in ("haiku", "sonnet"):
        with pytest.raises(Hold):
            store.reserve(model, 8, 100, 2)
    assert {r[0] for r in store.db.execute("SELECT kind FROM notice")} == {"fee15", "stop20"}


def test_legacy_pending_reconciliation_and_report_thresholds(tmp_path):
    store = Store(tmp_path / "trial")
    path = tmp_path / "old"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE delivery(scope,fingerprint,delivered)")
        db.execute("INSERT INTO delivery VALUES('scope','fp',0)")
    original = path.read_bytes()
    store.import_legacy(path)
    store.import_legacy(path)
    assert path.read_bytes() == original and store.row("SELECT count(*) FROM legacy WHERE status='pending'")[0] == 1
    store.import_spend("6")
    store.reserve("sonnet", 128000, 2000000, 1)
    store.reserve("sonnet", 128000, 2000000, 2)
    assert {r[0] for r in store.db.execute("SELECT kind FROM notice")} == {"fee15", "parse8"}


def test_switch_defaults_and_invalid_order(tmp_path):
    path = tmp_path / "switches"
    path.write_text("")
    assert dict(trial_config(path)) == {"MAIL_TRIAL_RECEIVE_ENABLED": "false", "MAIL_TRIAL_CARD_ENABLED": "false",
                                       "MAIL_TRIAL_DAILY_PARSE_LIMIT": "15"}
    path.write_text("MAIL_TRIAL_CARD_ENABLED=true")
    with pytest.raises(ConfigError):
        trial_config(path)


@pytest.mark.parametrize("amount", ["19.70", "19.90"])
def test_paid_http_never_reaches_large_output_stub(tmp_path, monkeypatch, amount):
    store = Store(tmp_path / "trial")
    store.import_spend(amount)
    trial = Trial(store, dict(MAIL_HOST="synthetic", MAIL_USER="local", ANTHROPIC_API_KEY="test-key"),
                  lambda: dict(MAIL_TRIAL_RECEIVE_ENABLED="true", MAIL_TRIAL_CARD_ENABLED="true",
                               MAIL_TRIAL_DAILY_PARSE_LIMIT="15"), lambda *args: pytest.fail("card"), Event(),
                  companies=Path("tests/parse/fixtures/companies.tsv"))
    store.begin(trial.scope)
    store.activate(trial.scope, 1, 1, 0)
    store.observe(trial.scope, 1, 1, 2, [1])
    reg = store.register(trial.scope, 1, 1, fixture_item(synthetic(1, "text").as_bytes()))
    trial.status(reg, "parse")
    sent = []
    def receive(req):
        if req.method == "GET":
            return httpx.Response(200, json=dict(id="claude-sonnet-5-5", max_input_tokens=1000000,
                                  type="model", display_name="synthetic", created_at="2026-01-01T00:00:00Z"))
        sent.append(req.content)
        return httpx.Response(200, json={"usage": {"input_tokens": 10, "output_tokens": 50000}})
    monkeypatch.setattr(anthropic, "_direct_transport", lambda _: httpx.MockTransport(receive))
    trial.parse(store.row("SELECT * FROM registration WHERE id=?", reg), 1, "text")
    assert not sent and store.row("SELECT stopped FROM budget")[0] == 1
    assert tuple(store.row("SELECT status,parse_day FROM registration")) == ("parse", None)


@pytest.mark.parametrize("size", [1999, 2000, 2001])
def test_unicode_limit_after_quote_removal_and_sender_privacy(monkeypatch, size):
    from qianshu.mail.triage import classify
    msg = synthetic(1, "字" * size + "\nOn a date wrote:\n> HISTORY_SENTINEL")
    msg["From"] = "display <" + "LOCAL_SENTINEL" + chr(64) + "synthetic.invalid>"
    msg["Cc"] = "CC_SENTINEL" + chr(64) + "synthetic.invalid"
    requests = []
    def receive(req):
        requests.append(req.content)
        return httpx.Response(503)
    monkeypatch.setattr(anthropic, "_direct_transport", lambda _: httpx.MockTransport(receive))
    classify({"ANTHROPIC_API_KEY": "synthetic"}, fixture_item(msg.as_bytes()), lambda req: None)
    raw, = requests
    assert b"SENTINEL" not in raw
    fields = json.loads(json.loads(raw)["messages"][0]["content"])
    assert fields == dict(body="字" * min(size, 2000), display_name="display", domain="synthetic.invalid",
                          subject="subject 1")


def test_rebuild_requires_current_snapshot_and_preserves_historical_identity(tmp_path):
    store = Store(tmp_path / "trial")
    store.begin("scope")
    store.activate("scope", 1, 10, 0)
    store.searching("scope", 1, 10, 11)
    assert Store(store.path).begin("scope") == "frozen"
    item = fixture_item(synthetic(2, "synthetic").as_bytes())
    store.rebuild("scope", 2, 3, 1, [(2, item)])
    first = store.row("SELECT generation FROM mailbox")[0]
    second_item = fixture_item(synthetic(3, "changed").as_bytes())
    store.rebuild("scope", 2, 3, 1, [(2, second_item)])
    with pytest.raises(Hold):
        store.reconcile("scope", first, 2, 3)
    second = store.row("SELECT generation FROM mailbox")[0]
    store.reconcile("scope", second, 2, 3)
    store.observe("scope", second, 3, 4, [3])
    reg = store.register("scope", second, 3, second_item)
    assert store.row("SELECT status FROM registration WHERE id=?", reg)[0] == "manual"


def test_distinct_id_conflict_and_full_attachment_bytes(tmp_path):
    store = Store(tmp_path / "trial")
    store.begin("scope")
    store.activate("scope", 1, 1, 0)
    store.observe("scope", 1, 1, 5, [1, 2, 3, 4])
    raw = synthetic(1, "body", attachment=True)
    first = store.register("scope", 1, 1, fixture_item(raw.as_bytes()))
    raw.get_payload()[1].set_payload("QUJD")
    second = store.register("scope", 1, 2, fixture_item(raw.as_bytes()))
    assert first != second and store.batch(first, 1)[3]
    raw.replace_header("Message-ID", "<other" + chr(64) + "synthetic.invalid>")
    third = store.register("scope", 1, 3, fixture_item(raw.as_bytes()))
    del raw["Message-ID"]
    fourth = store.register("scope", 1, 4, fixture_item(raw.as_bytes()))
    assert len({first, second, third, fourth}) == 4
    assert {r[0] for r in store.db.execute("SELECT status FROM registration")} == {"frozen"}


@pytest.mark.parametrize("stage", ["sonnet", "card"])
def test_freeze_committed_while_sender_waits_at_last_gate(tmp_path, monkeypatch, stage):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    path = tmp_path / "trial"
    db = Store(path)
    db.import_spend("0")
    config = dict(MAIL_HOST="synthetic", MAIL_USER="local", ANTHROPIC_API_KEY="test-key")
    switches = lambda: dict(MAIL_TRIAL_RECEIVE_ENABLED="true", MAIL_TRIAL_CARD_ENABLED="true",
                            MAIL_TRIAL_DAILY_PARSE_LIMIT="1")
    scope = Trial(db, config, switches, None, Event()).scope
    db.begin(scope)
    db.activate(scope, 17, 1, 0)
    db.observe(scope, 1, 1, 2, [1])
    reg = db.register(scope, 1, 1, fixture_item(synthetic(2, "body").as_bytes()))
    with db.write() as sql:
        sql.execute("UPDATE registration SET status='parse' WHERE id=?", (reg,))
    waiting, resume = Event(), Event()
    sent = []
    monkeypatch.setattr(anthropic, "_direct_transport", lambda _: httpx.MockTransport(lambda req: sent.append(req)))
    def sender():
        store = Store(path)
        original = store.dispatch
        @contextmanager
        def dispatch(scope, gen, reg, kind, enabled):
            if kind == stage:
                waiting.set()
                assert resume.wait(15)
            with original(scope, gen, reg, kind, enabled):
                yield
        store.dispatch = dispatch
        trial = Trial(store, config, switches, lambda *args: sent.append(args), Event(),
                      companies=Path("tests/parse/fixtures/companies.tsv"))
        trial.parse(store.row("SELECT * FROM registration WHERE id=?", reg), 1, "body" if stage == "sonnet" else "")
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(sender)
        try:
            assert waiting.wait(15)
            db.freeze(scope, "generation")
        finally:
            resume.set()
        future.result()
    assert not sent
    assert db.row("SELECT count(*) FROM charge")[0] == db.row("SELECT count(*) FROM card_intent")[0] == 0


def test_existing_listener_lease_precedes_trial_worker(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from qianshu.feishu.handoff import HandoffService, MentionLog
    from qianshu.feishu.settings import Settings
    from qianshu.mail.trial import run_service
    settings = Settings("app", "secret", "group", "bot", frozenset({"owner"}))
    path = tmp_path / "mention"
    business = tmp_path / "business"
    business.write_text(f"MAIL_HOST=synthetic\nMAIL_USER=local\nMAIL_PASSWORD=secret\nFEISHU_CHAT_ID=group\n"
                        f"MAIL_PRIVATE_DIR={tmp_path}\n")
    monkeypatch.setattr(HandoffService, "__init__", lambda self, store: setattr(self, "store", store))
    def run(self):
        with self.store.listener():
            pytest.fail("第二实例取得了租约")
    monkeypatch.setattr(HandoffService, "run", run)
    log = MentionLog(path, settings, create=True)
    with log.listener(), pytest.raises(BlockingIOError):
        run_service(SimpleNamespace(business_env_file=business), settings, path, lambda: {})
    assert not (tmp_path / "trial-050.sqlite3").exists()


def test_cli_disabled_trial_preserves_listener_and_skips_mail(tmp_path, monkeypatch, caplog):
    from qianshu.__main__ import main
    from qianshu.feishu.handoff import HandoffService
    file = tmp_path / "handoff"
    file.write_text(f"FEISHU_MENTION_LOG={tmp_path / 'mention'}\nFEISHU_HANDOFF_OWNER_OPEN_ID=ou_owner\n"
                    "FEISHU_BOT_OPEN_ID=ou_bot\nFEISHU_APP_ID=app\nFEISHU_APP_SECRET=secret\nFEISHU_CHAT_ID=group\n")
    switches = tmp_path / "switches"
    switches.write_text("MAIL_TRIAL_RECEIVE_ENABLED=false\n")
    called = []
    monkeypatch.setattr(HandoffService, "__init__", lambda self, store: None)
    monkeypatch.setattr(HandoffService, "run", lambda self: called.append(1))
    argv = ["--env-file", str(file), "--business-env-file", str(file), "--handoff-only", "--trial-switches", str(switches)]
    assert main(argv) == 0 and called == [1]
    switches.write_text("MAIL_TRIAL_CARD_ENABLED=true\n")
    monkeypatch.undo()
    monkeypatch.setattr(logging.getLogger("qianshu"), "handlers", [caplog.handler])
    listen, dial, socket = observe(monkeypatch)
    assert main(argv) == 0
    assert listen.call_count == dial.await_count == 1
    assert any(r.levelno == logging.CRITICAL and r.getMessage() == "E-TRIAL-DISABLED" for r in caplog.records)
    assert socket.callback_responses[-1]["toast"]["content"] == "审批暂不可用"
    assert not (tmp_path / "trial-050.sqlite3").exists()


@pytest.mark.parametrize("text", ["", "synthetic"])
def test_attachment_only_or_zero_policies_discloses_batch_and_counts_handoffs(tmp_path, monkeypatch, text):
    store = Store(tmp_path / "trial")
    store.import_spend("0")
    sent, requests = [], []
    trial = Trial(store, dict(MAIL_HOST="synthetic", MAIL_USER="local", ANTHROPIC_API_KEY="test-key"),
                  lambda: dict(MAIL_TRIAL_RECEIVE_ENABLED="true", MAIL_TRIAL_CARD_ENABLED="true",
                               MAIL_TRIAL_DAILY_PARSE_LIMIT="1"),
                  lambda content, uid, card, check: check() or sent.append(content), Event(),
                  companies=Path("tests/parse/fixtures/companies.tsv"))
    msg = EmailMessage()
    msg["Message-ID"] = "<local" + chr(64) + "synthetic.invalid>"
    if text:
        msg.set_content(text)
    msg.add_attachment(b"synthetic", maintype="image", subtype="png")
    item = fixture_item(msg.as_bytes())
    store.begin(trial.scope)
    store.activate(trial.scope, 1, 1, 0)
    store.observe(trial.scope, 1, 1, 2, [1])
    reg = store.register(trial.scope, 1, 1, item)
    trial.status(reg, "parse")
    trial.input_limits["claude-sonnet-5-5"] = 1000000

    def receive(req):
        requests.append(1)
        events = [
            {"type": "message_start", "message": {"id": "x", "role": "assistant", "type": "message",
             "model": "synthetic", "content": [], "usage": {"input_tokens": 9, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "x",
             "name": "submit_application_reply", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta",
             "partial_json": '{"policies":[]}'}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
             "usage": {"output_tokens": 5}},
            {"type": "message_stop"}]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events))

    monkeypatch.setattr(anthropic, "_direct_transport", lambda _: httpx.MockTransport(receive))
    trial.parse(store.row("SELECT * FROM registration WHERE id=?", reg), 1, text)
    assert len(requests) == int(bool(text))
    assert store.row("SELECT count(parse_day) FROM registration")[0] == len(requests)
    assert "含 1 个附件未解析，请对照原件核对" in json.dumps(sent, ensure_ascii=False)
    assert store.batch(reg, 1)[3] and store.batch(reg, 2)[3] and store.batch(12345, 1)[3]
    assert store.row("SELECT count(*) FROM card_intent WHERE status='sent'")[0] == len(sent)


def test_unknown_notification_is_not_replayed(tmp_path):
    from qianshu.mail.trial import _NOTICES
    store = Store(tmp_path / "trial")
    store.import_spend("0")
    sent = []
    def lose(content, uid, card, check):
        sent.append((content, uid))
        raise TimeoutError()
    trial = Trial(store, dict(MAIL_HOST="synthetic", MAIL_USER="local"),
                  lambda: dict(MAIL_TRIAL_RECEIVE_ENABLED="true"), lose, Event())
    store.begin(trial.scope)
    store.activate(trial.scope, 1, 1, 0)
    store.observe(trial.scope, 1, 1, 2, [1])
    reg = store.register(trial.scope, 1, 1, fixture_item(synthetic(1, "text").as_bytes()))
    trial.status(reg, "parse")
    with store.write():
        store.notice("event", trial.scope, reg, "received")
    trial.flush()
    trial.store = Store(store.path)
    trial.flush()
    assert sent == [(_NOTICES["received"], store.row("SELECT uuid FROM notice")[0])]
    assert store.row("SELECT status FROM registration")[0] == "manual"


def test_crash_after_examine_and_vanished_observed_uid(tmp_path, monkeypatch):
    box = Inbox()
    config = dict(MAIL_HOST="synthetic", MAIL_USER="local")
    switches = lambda: {"MAIL_TRIAL_RECEIVE_ENABLED": "true", "MAIL_TRIAL_CARD_ENABLED": "false"}
    store = Store(tmp_path / "interrupted")
    trial = Trial(store, config, switches, lambda *args: None, Event(), connector=lambda _: nullcontext(box))
    with monkeypatch.context() as change:
        def interrupt(*args):
            raise InterruptedError()
        change.setattr(store, "activate", interrupt)
        with pytest.raises(InterruptedError):
            trial.poll()
    assert trial.box()["state"] == "initializing"
    box.add(synthetic(2, "during interruption"))
    trial.store = Store(store.path)
    trial.poll()
    assert trial.box()["state"] == "frozen"
    assert store.row("SELECT count(*) FROM reconciliation")[0] == 2

    trial.store = Store(tmp_path / "vanished")
    trial.poll()
    box.add(synthetic(3, "observed once"))
    with monkeypatch.context() as change:
        def disappear(uid, section):
            del box.messages[uid]
            raise InterruptedError()
        change.setattr(box, "checked_section", disappear)
        with pytest.raises(InterruptedError):
            trial.poll()
    trial.store = Store(trial.store.path)
    trial.poll()
    assert trial.box()["state"] == "frozen" and trial.store.pending(trial.scope, 1) == [3]


def test_bodystructure_uid_is_required_outside_the_mime_tree():
    from qianshu.mail.imap import TrialMailbox
    class Wire:
        def uid(self, *args):
            assert args == ("FETCH", "7", "(BODYSTRUCTURE)")
            return "OK", [self.data]
    wire = Wire()
    box = TrialMailbox(wire)
    wire.data = b'1 (BODYSTRUCTURE ("TEXT" "PLAIN") UID 7)'
    assert box.structure(7) == [b"TEXT", b"PLAIN"]
    wire.data = b'1 (UID 7 BODYSTRUCTURE ("TEXT" "PLAIN"))'
    assert box.structure(7) == [b"TEXT", b"PLAIN"]
    wire.data = b'1 (BODYSTRUCTURE ("TEXT" "UID 7") UID 8)'
    with pytest.raises(MailError):
        box.structure(7)


def test_legacy_review_preserves_initial_boundary_without_reading_history(tmp_path):
    store = Store(tmp_path / "trial")
    old = tmp_path / "old"
    with sqlite3.connect(old) as db:
        db.execute("CREATE TABLE delivery(scope,fingerprint,delivered)")
        db.execute("INSERT INTO delivery VALUES('first','opaque',1)")
    box = Inbox()
    trial = Trial(store, dict(MAIL_HOST="synthetic", MAIL_USER="local"),
                  lambda: dict(MAIL_TRIAL_RECEIVE_ENABLED="true", MAIL_TRIAL_CARD_ENABLED="false"),
                  lambda *args: None, Event(), connector=lambda _: nullcontext(box), legacy_path=old)
    trial.poll()
    box.add(synthetic(2, "after enabling"))
    trial.poll()
    assert not box.reads and trial.box()["reason"] == "legacy"
    store.reconcile(trial.scope, 1, 1, 2)
    trial.poll()
    assert box.reads and set(box.reads) == {2}
    assert store.row("SELECT status FROM legacy")[0] == "reviewed"


def test_cli_import_and_receive_only_service_use_same_private_ledger(tmp_path, monkeypatch):
    from qianshu import __main__ as entry
    state = tmp_path / "trial-050.sqlite3"
    assert entry.main(["--state", str(state), "--trial-import-spend", "1.25"]) == 0
    assert entry.main(["--state", str(state), "--trial-import-spend", "0"]) == 1
    settings = tmp_path / "settings"
    settings.write_text(f"FEISHU_MENTION_LOG={tmp_path / 'mention'}\nFEISHU_HANDOFF_OWNER_OPEN_ID=ou_owner\n"
                        "FEISHU_BOT_OPEN_ID=ou_bot\nFEISHU_APP_ID=app\nFEISHU_APP_SECRET=secret\nFEISHU_CHAT_ID=group\n")
    business = tmp_path / "business"
    business.write_text(f"MAIL_PRIVATE_DIR={tmp_path}\nMAIL_HOST=synthetic\nMAIL_USER=local\nMAIL_PASSWORD=secret\n"
                        "FEISHU_CHAT_ID=group\nANTHROPIC_API_KEY=synthetic\n")
    switches = tmp_path / "switches"
    switches.write_text("MAIL_TRIAL_RECEIVE_ENABLED=true\n")
    inbox, finished, notices, models = Inbox(), Event(), [], []

    def two_polls(trial):
        try:
            trial.connector = lambda _: nullcontext(inbox)
            trial.poll()
            inbox.add(synthetic(2, "synthetic"))
            trial.poll()
        finally:
            finished.set()

    def response(req):
        if req.method == "GET":
            return httpx.Response(200, json=dict(id="claude-haiku-5-5", max_input_tokens=1000000,
                                  type="model", display_name="synthetic", created_at="2026-01-01T00:00:00Z"))
        model = json.loads(req.content)["model"]
        models.append(model)
        return httpx.Response(200, json={"type": "message", "id": "msg", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": "是"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 20, "output_tokens": 1}})

    observe(monkeypatch, finished)
    monkeypatch.setattr(Trial, "run", two_polls)
    monkeypatch.setattr(anthropic, "_direct_transport", lambda _: httpx.MockTransport(response))
    monkeypatch.setattr(entry, "_send_trial_message", lambda transport, content, uid, card, check:
                        check() or notices.append((content, card)))
    assert entry.main(["--env-file", str(settings), "--business-env-file", str(business),
                       "--handoff-only", "--trial-switches", str(switches)]) == 0
    assert models == ["claude-haiku-5-5"] and notices == [("收到，待核对 1 封。", False)]
    with sqlite3.connect(state) as db:
        assert db.execute("SELECT imported FROM budget").fetchone()[0] == 1250000000
        assert db.execute("SELECT amount FROM charge").fetchone()[0] == 2500


def revision_trial(store, inbox, sent, switches=None):
    return Trial(store, dict(MAIL_HOST="synthetic", MAIL_USER="local", ANTHROPIC_API_KEY="synthetic"),
                 switches or (lambda: dict(MAIL_TRIAL_RECEIVE_ENABLED="true", MAIL_TRIAL_CARD_ENABLED="true",
                                            MAIL_TRIAL_DAILY_PARSE_LIMIT="15")),
                 lambda content, uid, card, check: check() or sent.append((card, content)), Event(),
                 connector=lambda _: nullcontext(inbox), companies=Path("tests/parse/fixtures/companies.tsv"))


@pytest.mark.parametrize("damage", ["DROP TABLE charge", "DELETE FROM charge"])
def test_incomplete_imported_ledger_holds_pending(tmp_path, damage):
    store = Store(tmp_path / "ledger")
    inbox, sent = Inbox(), []
    trial = revision_trial(store, inbox, sent)
    trial.poll()
    store.observe(trial.scope, 1, 2, 3, [2])
    reg = store.register(trial.scope, 1, 2, fixture_item(synthetic(2, "pending").as_bytes()))
    trial.status(reg, "parse")
    store.import_spend("19.00")
    store.reserve("sonnet", 50000, 1, reg)
    assert store.row("SELECT amount FROM charge")[0] == 500002000
    with store.db:
        store.db.execute(damage)
    store.db.close()
    store = Store(store.path)
    with pytest.raises(Hold):
        store.reserve("sonnet", 60000, 1, reg, stage="sonnet")
    trial = revision_trial(store, inbox, sent)
    trial.flush()
    assert [text for card, text in sent if not card and "待人工对账" in text] == ["费用账本不完整，待人工对账。"]
    assert store.row("SELECT status FROM registration")[0] == "parse"


@pytest.mark.parametrize("prefix", ["\ufeff", "\u200b", " \ufeff", "CURRENT\n\u200b",
                                     "CURRENT\n\u200c \t\u200d\u2060"])
def test_prefixed_quote_absent_from_triage_http(monkeypatch, prefix):
    from qianshu.mail.triage import classify
    requests = []
    def receive(req):
        requests.append(req.content)
        return httpx.Response(503)
    monkeypatch.setattr(anthropic, "_direct_transport", lambda _: httpx.MockTransport(receive))
    item = fixture_item(synthetic(1, prefix + "> HISTORY_SENTINEL").as_bytes())
    classify({"ANTHROPIC_API_KEY": "synthetic"}, item, lambda req: None)
    request, = requests
    assert request.count(b"HISTORY_SENTINEL") == 0


def test_nested_received_changes_attachment_identity(tmp_path):
    msg = synthetic(1, "current")
    nested = synthetic(2, "attached")
    nested["Received"] = "inner-a"
    msg.add_attachment(nested)
    raw = msg.as_bytes()
    first = fixture_item(raw)
    assert first.digest and first.attachments == 1
    assert fixture_item(b"Received: outer\r\n" + raw).digest == first.digest
    second = fixture_item(raw.replace(b"inner-a", b"inner-b"))
    store = Store(tmp_path / "trial")
    store.begin("scope")
    store.activate("scope", 1, 1, 0)
    store.observe("scope", 1, 1, 3, [1, 2])
    assert store.register("scope", 1, 1, first) != store.register("scope", 1, 2, second)
    assert {r[0] for r in store.db.execute("SELECT reason FROM registration")} == {"conflict"}


def test_vanished_offline_uid_persists_unknown_and_notifies_once(tmp_path):
    path = tmp_path / "trial"
    inbox, sent = Inbox(), []
    inbox.next_uid = 100
    trial = revision_trial(Store(path), inbox, sent)
    trial.poll()
    trial.store.db.close()
    inbox.next_uid = 101
    trial = revision_trial(Store(path), inbox, sent)
    trial.poll()
    trial.poll()
    assert len(sent) == 1 and not sent[0][0] and "待人工对账" in sent[0][1]
    assert tuple(trial.store.row("SELECT first,last,status FROM unknown_range")) == (100, 100, "unknown")


def test_card_switch_file_changes_only_after_restart(tmp_path):
    path = tmp_path / "switches"
    def configure(value):
        path.write_text("MAIL_TRIAL_RECEIVE_ENABLED=true\nMAIL_TRIAL_CARD_ENABLED=" + value)
    configure("true")
    reads = []
    def load():
        reads.append(1)
        return trial_config(path)
    store = Store(tmp_path / "trial")
    inbox, sent = Inbox(), []
    trial = revision_trial(store, inbox, sent, load)
    trial.poll()
    store.import_spend("0")
    configure("false")
    for uid in (2, 3):
        msg = synthetic(uid, "")
        inbox.add(msg)
        store.observe(trial.scope, 1, uid, uid + 1, [uid])
        reg = store.register(trial.scope, 1, uid, fixture_item(msg.as_bytes()))
        trial.status(reg, "parse")
        trial.poll()
        if uid == 2:
            assert sent and all(card for card, _ in sent)
            assert len(reads) == 1
            sent.clear()
            trial = revision_trial(store, inbox, sent, load)
    assert not sent and len(reads) == 2


@pytest.mark.parametrize("line", [
    *(pytest.param(chr(code) + suffix, id=f"U+{code:04X}-{label}")
      for code in range(sys.maxunicode + 1)
      if unicodedata.category(chr(code)) in {"Cf", "Zs"}
      for label, suffix in (("ascii", "> HISTORY_SENTINEL"), ("wide", "＞HISTORY_SENTINEL"))),
    ">上次说的HISTORY_SENTINEL", "＞上次HISTORY_SENTINEL",
    "\u200e> HISTORY_SENTINEL", "\u200f＞ HISTORY_SENTINEL",
    "\u202a\u202b\u202c\u202d\u202e>\tHISTORY_SENTINEL",
    "\u2066\u2067\u2068\u2069＞>HISTORY_SENTINEL",
    "\u200b>10", ">10", ">=", "＞10",
])
def test_triage_http_quote_marker_boundary(monkeypatch, line):
    from qianshu.mail.triage import classify
    text = "CURRENT\n" + line
    requests = []

    def receive(request):
        requests.append(request.content)
        return httpx.Response(503)

    monkeypatch.setattr(anthropic, "_direct_transport", lambda _: httpx.MockTransport(receive))
    classify({"ANTHROPIC_API_KEY": "synthetic"}, fixture_item(synthetic(1, text).as_bytes()), lambda req: None)
    request, = requests
    if "HISTORY_SENTINEL" in line:
        assert request.count(b"HISTORY_SENTINEL") == 0
    else:
        fields = json.loads(json.loads(request)["messages"][0]["content"])
        assert fields["body"] == text


@pytest.mark.parametrize("extra_header", [b"", b"Date: invalid\r\n"], ids=["plain", "invalid-date"])
def test_header_only_child_keeps_mailbox_active(tmp_path, monkeypatch, extra_header):
    store = Store(tmp_path / "trial")
    inbox = Inbox()
    trial = revision_trial(store, inbox, [])
    trial.poll()
    msg = synthetic(2, "FIRST")
    inbox.add(msg)
    raw = (b"Message-ID: " + msg["Message-ID"].encode() + b"\r\nContent-Type: multipart/mixed; boundary=q\r\n\r\n"
           b"--q\r\nContent-Type: text/plain\r\n\r\nFIRST\r\n--q\r\nContent-Type: text/plain\r\n"
           + extra_header + b"--q--\r\n")
    inbox.messages[2] = Segments(raw)
    store.observe(trial.scope, 1, 2, 3, [2])
    reg = store.register(trial.scope, 1, 2, fixture_item(raw))
    trial.status(reg, "parse")
    store.import_spend("0")
    trial.input_limits["claude-sonnet-5-5"] = 100000
    requests = []

    def receive(request):
        requests.append(request.content)
        raise httpx.ReadTimeout("synthetic")

    monkeypatch.setattr(anthropic, "_direct_transport", lambda _: httpx.MockTransport(receive))
    trial.poll()
    assert trial.box()["state"] == "active"
    request, = requests
    assert b"FIRST" in request


@pytest.mark.parametrize("category", ["Cc", "Cf", "Mn", "Me", "Zs"])
@pytest.mark.parametrize("marker", [">", "＞"])
def test_all_invisible_quote_prefixes(monkeypatch, category, marker):
    from qianshu.mail.triage import classify

    requests = []

    def receive(req):
        requests.append(req.content)
        return httpx.Response(503)

    monkeypatch.setattr(anthropic, "_direct_transport", lambda _: httpx.MockTransport(receive))
    for code in range(sys.maxunicode + 1):
        prefix = chr(code)
        if unicodedata.category(prefix) != category or prefix.splitlines() != [prefix]:
            continue
        text = f"CURRENT\n{prefix}{marker} HISTORY_SENTINEL"
        msg = synthetic(1, text)
        if prefix == "\x00":
            # X7 在分拣前拒绝 NUL。另把同一文本单独交给投影，保留 HTTP 字符全集断言。
            from qianshu.mail.triage import MailItem, triage_fields
            with pytest.raises(MailError):
                fixture_item(msg.as_bytes())
            fields = triage_fields(msg, [("text/plain", text)], "text/plain")
            item = MailItem(None, None, None, None, fields)
        else:
            item = fixture_item(msg.as_bytes())
        classify({"ANTHROPIC_API_KEY": "synthetic"}, item, lambda req: None)
        request, = requests
        assert request.count(b"HISTORY_SENTINEL") == 0, f"U+{code:04X}"
        requests.clear()


def mixed(*leaves):
    msg = synthetic(2, "")
    msg.clear_content()
    msg.make_mixed()
    for kind, text in leaves:
        leaf = EmailMessage()
        leaf.set_content(text, subtype=kind)
        msg.attach(leaf)
    return Segments(msg.as_bytes())


@pytest.fixture
def journey(tmp_path, monkeypatch):
    store, inbox, cards, requests = Store(tmp_path / "trial"), Inbox(), [], []
    trial = revision_trial(store, inbox, cards)
    trial.poll()
    store.import_spend("0")
    trial.input_limits.update({"claude-haiku-5-5": 100000, "claude-sonnet-5-5": 100000})

    def start(message, verdict):
        def receive(req):
            data = json.loads(req.content)
            requests.append(req.content)
            if data["model"] == "claude-sonnet-5-5" or verdict == "timeout":
                raise httpx.ReadTimeout("synthetic")
            fields = json.loads(data["messages"][0]["content"])
            answer = verdict(fields) if callable(verdict) else verdict
            return httpx.Response(200, json={"type": "message", "id": "msg", "role": "assistant",
                "model": data["model"], "content": [{"type": "text", "text": answer}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 2}})

        monkeypatch.setattr(anthropic, "_direct_transport", lambda _: httpx.MockTransport(receive))
        inbox.add(message)
        trial.poll()
        return trial, inbox, requests, cards
    return start


@pytest.mark.parametrize("leaves", [
    [("plain", ""), ("plain", "CURRENT")], [("plain", "CURRENT"), ("plain", ""), ("plain", "")],
    [("plain", ""), ("html", "<p>CURRENT</p>")]])
def test_empty_sibling_never_clears_triage_body(journey, leaves):
    trial, _, requests, _ = journey(mixed(*leaves), lambda f: "是" if f["body"] == "CURRENT" else "否")
    trial.poll()
    assert trial.store.row("SELECT verdict FROM registration")[0] == "是"
    assert len(requests) == 2 and b"CURRENT" in requests[1]


@pytest.mark.parametrize("leaves,body,sonnet", [
    ([("plain", "> HISTORY")], "", 1), ([("html", '<div style="color:red">unknown</div>')], "", 1),
    ([("plain", "")], "", 0), ([("plain", "> HISTORY"), ("plain", "CURRENT")], "CURRENT", 0)])
def test_empty_or_uncertain_triage_cannot_record_no(journey, leaves, body, sonnet):
    trial, _, requests, cards = journey(mixed(*leaves), "否")
    assert json.loads(json.loads(requests[0])["messages"][0]["content"])["body"] == body
    trial.poll()
    verdict, day = trial.store.row("SELECT verdict,parse_day FROM registration")
    assert verdict == ("否" if body else "拿不准")
    assert len(requests) == 1 + sonnet and bool(day) == bool(sonnet)
    if not body:
        assert any(card for card, _ in cards)


def test_open_quote_boundary_spans_plain_siblings(journey):
    trial, _, requests, _ = journey(mixed(("plain", "CURRENT\n-----Original Message-----"),
                                          ("plain", "HISTORY_SENTINEL")), "否")
    assert b"HISTORY_SENTINEL" not in requests[0]
    assert trial.store.row("SELECT verdict FROM registration")[0] == "拿不准"
    trial.poll()
    assert len(requests) == 2


@pytest.mark.parametrize("extension", [True, False])
def test_multipart_container_roles_follow_fetched_sections(journey, extension):
    msg = synthetic(2, "ATTACHED_SENTINEL")
    msg.make_mixed()
    msg["Content-Disposition"] = "attachment"
    outer = synthetic(2, "")
    outer.clear_content()
    outer.make_mixed()
    outer.attach(msg)
    parts = Segments(outer.as_bytes())
    if not extension:
        parts.tree[0].pop()
    trial, _, requests, cards = journey(parts, "否")
    assert b"ATTACHED_SENTINEL" not in requests[0]
    trial.poll()
    assert tuple(trial.store.row("SELECT verdict,attachments FROM registration")) == ("拿不准", 1)
    assert len(requests) == (1 if extension else 2)
    assert bool(trial.store.row("SELECT parse_day FROM registration")[0]) == (not extension)
    assert trial.store.batch(1, 1)[3] and "含 1 个附件" in str(cards)


@pytest.mark.parametrize("changed", ["TEXT", "1", "2", "HEADER", "structure", "generation", "seen"])
def test_parse_reread_mismatch_freezes_before_http(journey, changed):
    parts = mixed(("plain", "BODY_A"), ("html", "<p>other</p>"))
    parts.tree[-3] = b"RELATED"
    parts.tree[0][3], parts.tree[1][3] = b"<first>", b"<other>"
    parts.tree[-2] = [b"START", b"<first>"]
    trial, inbox, requests, cards = journey(parts, "是")
    if changed == "generation":
        inbox.generation += 1
    elif changed == "seen":
        inbox.seen.add(2)
    elif changed == "structure":
        parts.tree[-2] = [b"START", b"<other>"]
    elif changed == "HEADER":
        parts.sections[changed] = b"Date: changed\r\n" + parts.sections[changed]
    else:
        parts.sections[changed] = parts.sections[changed].replace(b"A", b"B").replace(b"other", b"alter")
    trial.poll()
    assert trial.box()["state"] == "frozen" and len(requests) == 1
    assert not any(card for card, _ in cards)


@pytest.mark.parametrize("second", [False, True])
def test_incomplete_outer_header_freezes_each_phase(journey, second):
    parts = mixed(("plain", "CURRENT"))
    if not second:
        parts.sections["HEADER"] = parts.sections["HEADER"].rstrip() + b"\r\n"
    trial, _, requests, cards = journey(parts, "是")
    if second:
        parts.sections["HEADER"] = parts.sections["HEADER"].rstrip() + b"\r\n"
        trial.poll()
    assert trial.box()["state"] == "frozen" and len(requests) == int(second)
    assert not any(card for card, _ in cards)


@pytest.mark.parametrize("seen", [False, True])
def test_segment_reads_once_per_phase(journey, monkeypatch, seen):
    from collections import Counter
    parts = mixed(("plain", "CURRENT"), ("html", "<p>alternate</p>"))
    add = Inbox.add
    def deliver(box, message):
        if seen:
            box.seen.add(box.next_uid)
        add(box, message)
    monkeypatch.setattr(Inbox, "add", deliver)
    trial, inbox, requests, _ = journey(parts, "是")
    expected = Counter(["(BODYSTRUCTURE)", *(f"(BODY.PEEK[{s}])" for s in parts.sections)])
    assert Counter(c[2] for c in inbox.commands if c[0] == "FETCH") == expected
    assert inbox.seen == ({1, 2} if seen else {1})
    assert {c[0] for c in inbox.commands} == {"SEARCH", "FETCH", "EXAMINE"}
    inbox.commands.clear()
    trial.poll()
    assert Counter(c[2] for c in inbox.commands if c[0] == "FETCH") == expected
    assert {c[0] for c in inbox.commands} == {"SEARCH", "FETCH", "EXAMINE"}
    assert inbox.seen == ({1, 2} if seen else {1}) and len(requests) == 2


@pytest.mark.parametrize("answer,expected,sonnet", [("否", "否", 0), ("是", "是", 1), ("拿不准", "拿不准", 1),
                                                   ("invalid", "拿不准", 1), ("timeout", "拿不准", 1)])
def test_e7_triage_routes_at_transport(journey, answer, expected, sonnet):
    trial, _, requests, _ = journey(synthetic(2, "CURRENT"), answer)
    trial.poll()
    assert trial.store.row("SELECT verdict FROM registration")[0] == expected
    assert len(requests) == 1 + sonnet


def test_changed_epilogue_conflicts_after_no(journey):
    parts = mixed(("plain", "CURRENT"))
    parts.sections["TEXT"] += b"TAIL-AAAA"
    trial, inbox, requests, _ = journey(parts, "否")
    parts.sections["TEXT"] = parts.sections["TEXT"].replace(b"TAIL-AAAA", b"TAIL-BBBB")
    inbox.add(parts)
    trial.poll()
    assert len(requests) == 1 and b"TAIL-" not in requests[0]
    assert {r[0] for r in trial.store.db.execute("SELECT reason FROM registration")} == {"conflict"}


@pytest.mark.parametrize("second", [False, True])
@pytest.mark.parametrize("initial", [False, True])
def test_seen_changes_during_segment_read(journey, monkeypatch, second, initial):
    _interrupt_read(journey, monkeypatch, second, initial, vanish=False)


@pytest.mark.parametrize("second", [False, True])
def test_unread_uid_vanishes_before_final_seen_check(journey, monkeypatch, second):
    _interrupt_read(journey, monkeypatch, second, False, vanish=True)


def _interrupt_read(journey, monkeypatch, second, initial, vanish):
    original = Inbox.uid
    phase = 0

    def interrupt(box, command, *args):
        nonlocal phase
        if command == "SEARCH" and args[:2] == ("UID", "2") and not phase and initial:
            box.seen.add(2)
        result = original(box, command, *args)
        if command == "FETCH":
            if args[1] == "(BODY.PEEK[HEADER])":
                phase += 1
            if phase == 1 + second and args[1] == "(BODY.PEEK[TEXT])":
                if vanish:
                    del box.messages[2]
                else:
                    box.seen.symmetric_difference_update({2})
        return result
    monkeypatch.setattr(Inbox, "uid", interrupt)
    trial, _, requests, cards = journey(synthetic(2, "CURRENT"), "是")
    if second:
        trial.poll()
    assert trial.box()["state"] == "frozen" and len(requests) == int(second)
    assert not any(card for card, _ in cards)


@pytest.mark.parametrize("variant", ["original", "missing", "replacement", "previous-index"])
def test_v1_no_rebuild_requires_v2_candidate_coverage(tmp_path, variant):
    from dataclasses import replace
    inbox, cards = Inbox(), []
    store = Store(tmp_path / "trial")
    trial = revision_trial(store, inbox, cards)
    trial.poll()
    msg = synthetic(2, "ORIGINAL")
    inbox.add(msg)
    import hashlib
    from qianshu.mail import _fingerprint
    original = fixture_item(msg.as_bytes())
    # f730bfe 的 v1 固定分帧，独立于当前分段读取与候选索引。
    digest = hashlib.sha256(b"mime-v1")
    for value in (b'["text/plain", [["charset", "utf-8"]], []]', b"ORIGINAL\r\n"):
        digest.update(len(value).to_bytes(8, "big") + value)
    prior = "1:" + digest.hexdigest()
    store.observe(trial.scope, 1, 2, 3, [2])
    reg = store.register(trial.scope, 1, 2, replace(original, digest=prior,
                         candidate=_fingerprint("", msg["Subject"], prior)))
    trial.status(reg, "no")
    if variant == "missing":
        del inbox.messages[2]
    elif variant == "replacement":
        inbox.messages[2] = synthetic(2, "REPLACED")
    elif variant == "previous-index":
        # 已升级过的库可能留有归档补出的索引和匹配记录。
        with store.write() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS original_index(
                registration INTEGER PRIMARY KEY, digest TEXT, candidate TEXT, validity INTEGER, uid INTEGER)""")
            db.execute("INSERT INTO original_index VALUES(?,?,?,?,?)", (reg, original.digest, original.candidate, 1, 2))
            db.execute("""INSERT INTO reconciliation(scope,generation,uid,msgkey,digest,candidate,matched)
                VALUES(?,1,2,?,?,?,?)""", (trial.scope, original.key, original.digest, original.candidate, reg))
    trial.poll()
    assert trial.box()["state"] == "frozen"
    inbox.generation += 1
    trial.poll()
    generation = trial.box()["generation"]
    with pytest.raises(Hold):
        store.reconcile(trial.scope, generation, 2, 3)
    msg.replace_header("Message-ID", synthetic(3, "")["Message-ID"])
    inbox.add(msg)
    trial.poll()
    assert trial.box()["state"] == "frozen" and not inbox.reads
    assert not store.row("SELECT 1 FROM charge") and not any(card for card, _ in cards)


@pytest.mark.parametrize("damage", ["uid", "section", "length", "octets", "structure-literal"])
def test_invalid_section_evidence_freezes(journey, monkeypatch, damage):
    original = Inbox.uid

    def response(box, command, *args):
        status, data = original(box, command, *args)
        if command == "FETCH" and args[1] == "(BODY.PEEK[TEXT])":
            (prefix, raw), tail = data
            if damage == "uid":
                data = [(prefix.replace(b"UID 2", b"UID 3"), raw), tail]
            elif damage == "section":
                data = [(prefix.replace(b"[TEXT]", b"[1]"), raw), tail]
            elif damage == "length":
                data = [(prefix, raw[:-1]), tail]
        if command == "FETCH" and args[1] == "(BODYSTRUCTURE)" and damage == "structure-literal":
            data = [(b"1 (UID 2 BODYSTRUCTURE ({5}", b"TEXT"), b'"PLAIN"))']
        return status, data
    monkeypatch.setattr(Inbox, "uid", response)
    parts = Segments(synthetic(2, "CURRENT").as_bytes())
    if damage == "octets":
        parts.tree[6] = b"9999"
    trial, _, requests, cards = journey(parts, "是")
    assert trial.box()["state"] == "frozen" and not requests and not any(card for card, _ in cards)


@pytest.mark.parametrize("ids,structure", [
    ((b"<root>", b"<root> \t"), False), ((b"<first>", b"<second>"), False),
    ((b"<root>", b"<root> "), True), ((b"<root> ", b"<other>"), True)])
def test_related_requires_unique_normalized_root(journey, ids, structure):
    raw = b"Message-ID: " + synthetic(2, "")["Message-ID"].encode()
    raw += b'\r\nContent-Type: multipart/related; boundary=q; start="<root>"\r\n\r\n'
    for content_id in ids:
        raw += b"--q\r\nContent-ID: " + content_id + b"\r\n\r\nbody\r\n"
    parts = Segments(raw + b"--q--\r\n")
    if structure:
        parts.tree[0][3], parts.tree[1][3] = ids
    trial, _, requests, cards = journey(parts, "是")
    assert trial.box()["state"] == "frozen" and not requests and not any(card for card, _ in cards)
    assert not trial.store.row("SELECT 1 FROM registration WHERE status IN ('triage','parse')")


def test_binary_only_no_cannot_spend_parse_quota(journey):
    msg = synthetic(2, "")
    msg.set_content(b"OPAQUE", maintype="application", subtype="octet-stream")
    trial, _, requests, cards = journey(msg, "否")
    assert json.loads(json.loads(requests[0])["messages"][0]["content"])["body"] == ""
    trial.poll()
    assert tuple(trial.store.row("SELECT verdict,attachments,parse_day,status FROM registration")) == (
        "拿不准", 1, None, "转人工（正文未参与解析）")
    assert len(requests) == 1 and "含 1 个附件" in str(cards)


def test_unknown_attachment_count_disclosed_and_blocks_batch(tmp_path):
    from dataclasses import replace
    store, cards = Store(tmp_path / "trial"), []
    trial = revision_trial(store, Inbox(), cards)
    trial.poll()
    store.import_spend("0")
    item = replace(fixture_item(synthetic(2, "").as_bytes()), attachments=None)
    store.observe(trial.scope, 1, 2, 3, [2])
    reg = store.register(trial.scope, 1, 2, item)
    trial.status(reg, "parse")
    trial.parse(store.row("SELECT * FROM registration WHERE id=?", reg), 1, "")
    assert "附件数量未知，请对照原件核对" in str(cards)
    assert store.batch(reg, 1)[2] is None and store.batch(reg, 1)[3]


def test_old_format_freezes_reconciliation_snapshot(tmp_path):
    inbox = Inbox()
    trial = revision_trial(Store(tmp_path / "trial"), inbox, [])
    trial.poll()
    inbox.add(synthetic(2, "ORIGINAL"))
    with trial.store.write() as db:
        db.execute("PRAGMA user_version=0")
        db.execute("UPDATE mailbox SET state='frozen',reason='reconciling',cursor=3,peak=3")
        db.execute("""INSERT INTO reconciliation(scope,generation,uid,msgkey,digest,candidate)
            VALUES(?,1,2,'old-key','1:old','old-candidate')""", (trial.scope,))
    trial.store.db.close()
    with pytest.raises(Hold, match="登记库格式版本不符"):
        Store(tmp_path / "trial")
    with closing(sqlite3.connect(tmp_path / "trial")) as db:
        assert db.execute("SELECT digest FROM reconciliation").fetchone() == ("1:old",)


@pytest.mark.parametrize("replacement", [False, True])
def test_v1_multipart_stays_frozen_after_uidvalidity_change(tmp_path, replacement):
    from dataclasses import replace

    store, archive, live = Store(tmp_path / "trial"), Inbox(), Inbox()
    trial = revision_trial(store, live, [])
    trial.poll()
    msg = synthetic(2, "ORIGINAL", attachment=True)
    msg.set_boundary("synthetic-boundary")
    archive.add(Segments(msg.as_bytes()))
    old = fixture_item(msg.as_bytes())
    store.observe(trial.scope, 1, 2, 3, [2])
    # 同一固定边界夹具在 f730bfe 的两个实际摘要。
    prior = replace(old, digest="1:a83f0eb2ca4782658330cf37920118a7018a35152bb88dda1a6432dbe1ffd2f8",
                    candidate="2efdffcf8459e07874ca884605a4e72b0bbed700bec5ff0b26cb5b689d7b900b")
    reg = store.register(trial.scope, 1, 2, prior)
    with store.write() as db:
        db.execute("UPDATE registration SET status='no',verdict='否' WHERE id=?", (reg,))
    live.generation += 1
    live.add(Segments(msg.as_bytes().replace(b"ORIGINAL", b"REPLACED")) if replacement else Segments(msg.as_bytes()))
    trial.poll()
    gen = trial.box()["generation"]
    with pytest.raises(Hold):
        store.reconcile(trial.scope, gen, 2, 3)
    assert trial.box()["state"] == "frozen" and not live.reads and not archive.reads
    assert not store.row("SELECT 1 FROM charge")


@pytest.mark.parametrize("disposition,expected", [(b"ambiguous", None), ([b"INLINE", [b"FILENAME*0*", b"file"]], 1)])
def test_uncertain_or_extended_disposition_masks_selected_leaf(journey, disposition, expected):
    parts = mixed(("plain", "ATTACHED_SENTINEL"))
    parts.tree[0][-1] = disposition
    trial, _, requests, cards = journey(parts, "否")
    assert json.loads(json.loads(requests[0])["messages"][0]["content"])["body"] == ""
    trial.poll()
    assert tuple(trial.store.row("SELECT verdict,attachments FROM registration")) == ("拿不准", expected)
    assert len(requests) == 2 and b"ATTACHED_SENTINEL" in requests[1]
    assert ("附件数量未知" if expected is None else "含 1 个附件") in str(cards)
    assert trial.store.batch(1, 1)[3]


@pytest.mark.parametrize("prefix", ["\x01", "\t", "\u200e", "\u0301", "\u20dd", "\u00a0"],
                         ids=["Cc", "tab", "Cf", "Mn", "Me", "Zs"])
@pytest.mark.parametrize("layout", ["attribution", "divider", "siblings", "from"])
def test_prefixed_structure_withholds_history_at_http(journey, prefix, layout):
    marker = prefix + {"attribution": "On a date wrote:", "divider": "Original Message",
                       "siblings": "On a date wrote:", "from": "From: previous"}[layout]
    if layout == "siblings":
        leaves = ["CURRENT", "> previous\n" + marker, "HISTORY_SENTINEL"]
    else:
        leaves = ["CURRENT\n" + marker + "\nHISTORY_SENTINEL"]
    _, _, requests, _ = journey(mixed(*[("plain", leaf) for leaf in leaves]), "否")
    request, = requests
    assert request.count(b"HISTORY_SENTINEL") == 0


@pytest.mark.parametrize("change", ["seen", "missing", "validity", "absent-response"])
def test_final_examine_failure_requires_reconciliation(journey, monkeypatch, change):
    trial, inbox, requests, cards = journey(synthetic(2, "CURRENT"), "是")
    message = inbox.messages[2]
    response = inbox.response
    inbox.commands.clear()

    def changed_response(key):
        if key == "UIDVALIDITY" and any(cmd[0] == "FETCH" for cmd in inbox.commands):
            if change == "absent-response":
                return key, None
            if change == "validity":
                return key, [b"?"]
            if change == "seen":
                inbox.seen.add(2)
            else:
                inbox.messages.pop(2, None)
        return response(key)

    with monkeypatch.context() as patch:
        patch.setattr(inbox, "response", changed_response)
        trial.poll()
    assert trial.box()["state"] == "frozen"
    inbox.seen.discard(2)
    inbox.messages[2] = message
    trial.poll()
    assert (trial.box()["state"], trial.box()["reason"]) == ("frozen", "reconciling")
    assert len(requests) == 1 and not any(card for card, _ in cards)


@pytest.mark.parametrize("variant", ["original", "replacement", "unprovable", "invalid-encoding", "base64", "outer-header"])
def test_archive_binding_blocks_changed_id_redelivery(tmp_path, monkeypatch, variant):
    from dataclasses import replace

    store, archive, live = Store(tmp_path / "trial"), Inbox(), Inbox()
    trial = revision_trial(store, live, [])
    trial.poll()
    msg = synthetic(2, "ORIGINAL", attachment=True)
    msg.set_boundary("synthetic-boundary")
    raw = msg.as_bytes()
    prior = replace(fixture_item(raw),
                    digest="1:a83f0eb2ca4782658330cf37920118a7018a35152bb88dda1a6432dbe1ffd2f8",
                    candidate="2efdffcf8459e07874ca884605a4e72b0bbed700bec5ff0b26cb5b689d7b900b")
    store.observe(trial.scope, 1, 2, 3, [2])
    reg = store.register(trial.scope, 1, 2, prior)
    trial.status(reg, "no")
    live.generation += 1
    archived = raw
    if variant == "replacement":
        archived = raw.replace(b"ORIGINAL", b"REPLACED")
    elif variant == "unprovable":
        archived = raw.replace(b"--synthetic-boundary", b"PREAMBLE\r\n--synthetic-boundary", 1)
    elif variant == "invalid-encoding":
        archived = raw.replace(b"Encoding: 7bit", b"Encoding: unknown")
    elif variant == "base64":
        archived = raw.replace(b"Encoding: 7bit", b"Encoding: base64").replace(b"ORIGINAL", b"T1JJR0lOQUwNCg==")
    elif variant == "outer-header":
        archived = b"X-Synthetic-Test: extra\r\n" + raw
    for box in (archive, live):
        box.add(Segments(archived))
    decodes = []
    payload = EmailMessage.get_payload

    def counted(message, *args, **kwargs):
        if kwargs.get("decode"):
            decodes.append(message.get_content_type())
        return payload(message, *args, **kwargs)

    monkeypatch.setattr(EmailMessage, "get_payload", counted)
    requests = []

    def receive(req):
        requests.append(req.content)
        return httpx.Response(503)

    monkeypatch.setattr(anthropic, "_direct_transport", lambda _: httpx.MockTransport(receive))
    trial.input_limits["claude-haiku-5-5"] = 100000
    store.import_spend("0")
    trial.poll()
    box = trial.box()
    with pytest.raises(Hold):
        store.reconcile(trial.scope, box["generation"], box["validity"], box["cursor"])
    msg.replace_header("Message-ID", synthetic(3, "")["Message-ID"])
    live.add(msg)
    trial.poll()
    assert requests == []
    assert trial.box()["state"] == "frozen"
    assert decodes == [] and archive.reads == live.reads == []


@pytest.mark.parametrize("blank", ["\x01", "\t", "\u200b", "\u0301", "\u20dd", "\u00a0", "\ufeff\u200b"],
                         ids=["Cc", "tab", "Cf", "Mn", "Me", "Zs", "BOM"])
@pytest.mark.parametrize("layout", ["quote", "plain", "html-quote", "html-readable"])
def test_invisible_sibling_at_http(journey, blank, layout):
    sibling = {
        "quote": ("plain", "CURRENT\nOn a date wrote:\nHISTORY"),
        "plain": ("html", '<div style="color:red">CURRENT</div>'),
        "html-quote": ("html", "<p>CURRENT</p><p>From: previous</p>"),
        "html-readable": ("html", "<p>CURRENT</p>"),
    }[layout]
    trial, _, requests, _ = journey(mixed(("plain", blank), sibling), "否")
    body = json.loads(json.loads(requests[0])["messages"][0]["content"])["body"]
    assert body == ("CURRENT" if layout == "html-readable" else "")
    trial.poll()
    assert trial.store.row("SELECT verdict FROM registration")[0] == ("否" if body else "拿不准")
    assert [json.loads(req)["model"] for req in requests[1:]] == ([] if body else ["claude-sonnet-5-5"])


@pytest.mark.parametrize("damage", ["exists", "exists-extra", "search", "search-range", "uidnext", "tuple",
                                    "section-length", "structure-length", "sequence"])
def test_malformed_imap_reply_stays_frozen(journey, monkeypatch, damage):
    trial, inbox, requests, cards = journey(synthetic(2, "CURRENT"), "是")
    wire = inbox._connection
    select, uid, response = wire.select, wire.uid, wire.response
    inbox.commands.clear()
    final = False

    def examine(*args, **kwargs):
        nonlocal final
        final = any(cmd[0] == "FETCH" for cmd in inbox.commands)
        result = select(*args, **kwargs)
        if final and damage == "exists":
            return "OK", [b"9" * 5000]
        if final and damage == "exists-extra":
            return "OK", [b"2", b"1"]
        return result

    def read(command, *args):
        status, data = uid(command, *args)
        if final and command == "SEARCH" and damage in {"search", "search-range"}:
            return status, [b"9" * 5000 if damage == "search" else b"4294967296"]
        if command == "FETCH" and args[1] == "(BODY.PEEK[TEXT])":
            (prefix, raw), tail = data
            if damage == "tuple":
                return status, [(prefix,), tail]
            if damage == "section-length":
                return status, [(prefix.split(b"{")[0] + b"{" + b"9" * 5000 + b"}", raw), tail]
            if damage == "sequence":
                return status, [(b"4294967296" + prefix[1:], raw), tail]
        if command == "FETCH" and args[1] == "(BODYSTRUCTURE)" and damage == "structure-length":
            return status, [(b"1 (UID 2 BODYSTRUCTURE ({" + b"9" * 5000 + b"}", b"TEXT"), b"))"]
        return status, data

    with monkeypatch.context() as patch:
        patch.setattr(wire, "select", examine)
        patch.setattr(wire, "uid", read)
        if damage == "uidnext":
            patch.setattr(wire, "response", lambda key: (key, []) if key == "UIDNEXT" and final else response(key))
        trial.poll()
    assert trial.box()["state"] == "frozen" and len(requests) == 1
    trial.poll()
    assert trial.box()["state"] == "frozen" and len(requests) == 1 and not any(card for card, _ in cards)


@pytest.mark.parametrize("operation,row", [
    ("list", ()), ("list", (rb'(\Sent) "/" {2}', b"S")),
    ("list", (rb'(\Sent) "/" {1}', b"\xff")),
    ("section", (b"1 (BODY[TEXT] {1}",)),
    ("section", (b"1 (BODY[TEXT] {2}", b"x")),
    ("structure", (b"1 (BODYSTRUCTURE ({2}", b"x")),
])
def test_readonly_response_parser_reports_mail_error(operation, row):
    class Wire:
        def list(self):
            return "OK", [row]

        def uid(self, *args):
            return "OK", [row, b"))"]

    mailbox = ReadOnlyMailbox(Wire())
    with pytest.raises(MailError):
        if operation == "list":
            mailbox.sent_folder({})
        elif operation == "section":
            mailbox.section(1, "TEXT")
        else:
            mailbox.structure(1)


@pytest.mark.parametrize("kind,content,body", [
    ("html", "<p>CURRENT</p><!--\ufeff\u200b-->", "CURRENT"),
    ("html", "<p>CURRENT</p><blockquote>HISTORY</blockquote>\ufeff\u200b", "CURRENT"),
    ("plain", "\u200b" * 2000 + "CURRENT", ""),
    ("plain", "\u200b\nOn a date wrote:\n> HISTORY", ""),
], ids=["comment", "tail", "clipped", "before-quote"])
def test_normalized_retained_range_at_http(journey, kind, content, body):
    trial, _, requests, _ = journey(mixed((kind, content)), "否")
    assert json.loads(json.loads(requests[0])["messages"][0]["content"])["body"] == body
    trial.poll()
    assert trial.store.row("SELECT verdict FROM registration")[0] == ("否" if body else "拿不准")
    assert len(requests) == (1 if body else 2)
