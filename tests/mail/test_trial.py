import hashlib
import importlib.util
import json
import subprocess
from datetime import date
from email.message import EmailMessage
from pathlib import Path

import httpx
import pytest

from trial_segments import Inbox, Segments, fixture_item, read_fixture


def sample(charset="iso-8859-1"):
    return (f"Content-Type: text/plain; charset={charset}\r\n\r\n".encode() + b"\xa1")


def test_identity_charset_and_related_root():
    inspect = lambda raw: fixture_item(raw).digest
    assert inspect(sample()) != inspect(sample("iso-8859-2"))
    assert inspect(sample()[:-1] + b"A") != inspect(sample("iso-8859-2")[:-1] + b"A")
    raw = b'Content-Type: multipart/related; boundary=q; start="<a>"\n\n'
    raw += b'--q\nContent-ID: <a>\n\nfirst\n--q\nContent-ID: <b>\n\nsecond\n--q--\n'
    assert inspect(raw) != inspect(raw.replace(b'start="<a>"', b'start="<b>"'))


def test_triage_http_contains_only_authored_fields(monkeypatch):
    from qianshu.mail.triage import classify
    from qianshu.ai import anthropic
    msg = EmailMessage()
    msg.set_content('<p>current</p><div style="border-left:2px solid">HISTORY_SENTINEL</div>', subtype="html")
    container = EmailMessage()
    container.set_content("ATTACHMENT_SENTINEL")
    container.make_mixed()
    container.set_param("name", "private")
    msg.make_mixed()
    msg.attach(container)
    seen = []

    def receive(request):
        seen.append(json.loads(request.content))
        return httpx.Response(503)

    monkeypatch.setattr(anthropic, "_direct_transport", lambda _: httpx.MockTransport(receive))
    item = fixture_item(msg.as_bytes())
    assert item.attachments == 1
    result = classify({"ANTHROPIC_API_KEY": "synthetic"}, item, lambda req: None)
    assert result[0] == "拿不准"
    assert len(seen) == 1 and seen[0]["max_tokens"] == 8 and "tools" not in seen[0]
    assert "SENTINEL" not in json.dumps(seen)
    assert set(json.loads(seen[0]["messages"][0]["content"])) == {"display_name", "domain", "subject", "body"}


@pytest.mark.parametrize("amount", ["19.90", "19.70", None])
def test_fee_reservation_prevents_transport(tmp_path, amount):
    from qianshu.mail.trial_state import Store, Hold
    store = Store(tmp_path / "trial")
    if amount:
        store.import_spend(amount)
    with pytest.raises(Hold):
        store.reserve("sonnet", 128000, 42061, 1)
    assert store.db.execute("select count(*) from charge").fetchone()[0] == 0


def test_initialization_crash_freezes_and_observed_uids_survive(tmp_path):
    from qianshu.mail.trial_state import Store
    store = Store(tmp_path / "trial")
    assert store.begin("first") == "initializing"
    assert Store(store.path).begin("first") == "frozen"
    store.begin("second")
    store.activate("second", 1, 100, 99)
    store.observe("second", 1, 100, 102, [101])
    assert Store(store.path).pending("second", 1) == [101]


def capture_sonnet(monkeypatch, trial, text="合成材料", *, adapter=None):
    from qianshu.ai import anthropic
    from qianshu.parse.model import configured_model
    from qianshu.pipeline import Pipeline
    from qianshu.feishu.entrance import Application
    requests = []

    def receive(request):
        requests.append(request.content)
        if len(requests) == 2:
            raise httpx.ReadTimeout("synthetic")
        events = [{"type": "message_start", "message": {"id": "x", "type": "message", "role": "assistant",
                   "model": "synthetic", "content": [], "usage": {"input_tokens": 1, "output_tokens": 0}}}]
        for index in range(2):
            events.extend([{"type": "content_block_start", "index": index, "content_block": {"type": "tool_use",
                           "id": str(index), "name": "submit_application_reply", "input": {}}},
                           {"type": "content_block_stop", "index": index}])
        events.extend([{"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                        "usage": {"output_tokens": 1}}, {"type": "message_stop"}])
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events))

    monkeypatch.setattr(adapter or anthropic, "_direct_transport", lambda _: httpx.MockTransport(receive))
    config = {"ANTHROPIC_API_KEY": "synthetic"}
    if trial:
        from qianshu.mail.triage import trial_model
        model = trial_model(config, lambda req: None, lambda result: None)
    else:
        if adapter is not None:
            from qianshu.ai import ADAPTERS
            monkeypatch.setitem(ADAPTERS, "anthropic", adapter.streaming_model)
        model = configured_model(config)
    Pipeline(model=model, tag_dates={}, companies_file=Path("tests/parse/fixtures/companies.tsv"),
             today=lambda: date(2026, 1, 1))(Application(None, text, ()))
    return requests


def test_first_and_correction_match_ccd7420(monkeypatch):
    base = capture_sonnet(monkeypatch, False)
    assert [hashlib.sha256(raw).hexdigest() for raw in base] == [
        "88dbfcff74a247effd245d62771115fcf39c10026539f5c9cf2d76727fb80d0f",
        "8eb262f5c3aedc5ee6cddd38e6b5baa2cd97ec5a9a537faa6b496caed60b6563",
    ]
    assert capture_sonnet(monkeypatch, True) == base


@pytest.mark.parametrize("response", ["timeout", "redirect"])
def test_trial_no_sdk_replay(monkeypatch, response):
    from qianshu.ai import anthropic
    from qianshu.mail.triage import trial_model
    from qianshu.parse.model import ModelError
    sent = []

    def receive(req):
        sent.append(req.content)
        if response == "timeout":
            raise httpx.ReadTimeout("synthetic")
        return httpx.Response(307, headers={"Location": "/redirect"})

    monkeypatch.setattr(anthropic, "_direct_transport", lambda _: httpx.MockTransport(receive))
    with pytest.raises(ModelError):
        trial_model({"ANTHROPIC_API_KEY": "synthetic"}, lambda req: None, lambda result: None)(system="", user="x")
    assert len(sent) == 1


@pytest.mark.parametrize("stage", ["sonnet", "card"])
def test_committed_freeze_blocks_handoff(tmp_path, stage):
    from qianshu.mail.trial_state import Store, Hold
    one = Store(tmp_path / "trial")
    two = Store(one.path)
    one.begin("scope")
    one.activate("scope", 3, 1, 0)
    from qianshu.mail.triage import MailItem
    one.observe("scope", 1, 1, 2, [1])
    item = MailItem("key", "digest", "candidate", 0, dict(display_name="", subject=""))
    reg = one.register("scope", 1, 1, item)
    one.import_spend("0")
    with one.write() as db:
        db.execute("UPDATE registration SET status=? WHERE id=?", ("parse" if stage == "sonnet" else "sending", reg))
    with one.dispatch("scope", 1, reg, stage, lambda: True):
        pass
    two.freeze("scope", "generation")
    sent = []
    with pytest.raises(Hold):
        with one.dispatch("scope", 1, reg, stage, lambda: True):
            sent.append(1)
    assert not sent


@pytest.fixture(scope="module")
def baseline_modules(tmp_path_factory):
    directory = tmp_path_factory.mktemp("baseline")

    def load(package, name):
        path = directory / f"{name}.py"
        path.write_bytes(subprocess.check_output([
            "git", "show", f"ccd7420:src/qianshu/{package}/{name}.py",
        ]))
        spec = importlib.util.spec_from_file_location(f"qianshu.{package}.baseline_{name}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    return load("mail", "message"), load("ai", "anthropic")


@pytest.mark.parametrize("raw", [
    b"Content-Type: multipart/mixed; boundary=q\r\n\r\n"
    b"--q\r\nContent-Type: text/plain\r\n\r\nFIRST\r\n--q\r\n\r\nSECOND\r\n--q--\r\n",
    b"Content-Type: text/plain\r\n\r\nPLAIN\r\nTEXT",
    b"Content-Type: multipart/alternative; boundary=q\r\n\r\n"
    b"--q\r\nContent-Type: text/html\r\n\r\n<p>HTML</p>\r\n"
    b"--q\r\nContent-Type: text/plain\r\n\r\nPLAIN\r\n--q--\r\n",
    b"Content-Type: multipart/mixed; boundary=q\r\n\r\n"
    b"--q\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: quoted-printable\r\n"
    b"\r\nENCODED=20=E6=B5=8B=\r\n=E8=AF=95\r\n\r\n--q--\r\n",
    b"Content-Type: multipart/mixed; boundary=q\r\n\r\n"
    b"--q\r\nContent-Type: text/plain\r\n\r\nFIRST\r\n--q\r\nContent-Type: text/plain\r\n--q--\r\n",
    b"Content-Type: multipart/mixed; boundary=q\r\n\r\n"
    b"--q\r\nContent-Type: text/plain\r\n\r\nFIRST\r\n--q\r\nContent-Type: text/plain\r\nDate: invalid\r\n--q--\r\n",
    b"Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n\r\nQ1VSUkVOVA==",
], ids=["mixed-headerless", "plain", "alternative", "quoted-printable", "mixed-header-only", "invalid-date", "base64"])
def test_mime_to_sonnet_matches_baseline(monkeypatch, baseline_modules, raw):
    baseline, adapter = baseline_modules
    segments = Segments(raw)
    previous = baseline.request_text(baseline.body_parts(segments, 2, segments.sections["HEADER"]))
    with monkeypatch.context() as old:
        expected = capture_sonnet(old, False, previous, adapter=adapter)
    actual = capture_sonnet(monkeypatch, True, read_fixture(raw)[1])
    assert len(expected) == len(actual) == 2
    assert [body == reference for body, reference in zip(actual, expected)] == [True, True]


def test_related_root_trailing_space_poll(tmp_path, monkeypatch, baseline_modules):
    from test_trial_journey import Inbox, Store, revision_trial, synthetic
    from qianshu.ai import anthropic

    header = b"Message-ID: " + synthetic(2, "").get("Message-ID").encode() + b'\r\nContent-Type: multipart/related; boundary=q; start="<root>"\r\n\r\n'
    resource = b"Content-Type: text/plain\r\nContent-ID: <resource>\r\n\r\n"
    root = b"Content-Type: multipart/alternative; boundary=a\r\nContent-ID: <root> \t\r\n\r\n"
    plain = b"Content-Type: text/plain\r\n\r\n"
    html = b"Content-Type: text/html\r\n\r\n"
    sections = {"1.MIME": resource, "2.MIME": root, "2.1.MIME": plain, "2.2.MIME": html,
                "2.1": b"CURRENT", "2.2": b"<p>alternative</p>"}
    raw = (header + b"--q\r\n" + resource + b"RESOURCE\r\n--q\r\n" + root
           + b"--a\r\n" + plain + sections["2.1"] + b"\r\n--a\r\n" + html + sections["2.2"]
           + b"\r\n--a--\r\n--q--\r\n")

    segments = Segments(raw)
    baseline, adapter = baseline_modules
    text = baseline.request_text(baseline.body_parts(segments, 2, header))
    assert text == "CURRENT"
    with monkeypatch.context() as old:
        expected = capture_sonnet(old, False, text, adapter=adapter)
    assert len(expected) == 2

    inbox, sent = Inbox(), []
    store = Store(tmp_path / "trial")
    trial = revision_trial(store, inbox, sent)
    trial.poll()
    inbox.add(synthetic(2, "placeholder"))
    inbox.messages[2] = Segments(raw)
    store.import_spend("0")
    trial.input_limits.update({"claude-haiku-5-5": 100000, "claude-sonnet-5-5": 100000})
    requests = []

    def receive(req):
        data = json.loads(req.content)
        requests.append(req.content)
        if data["model"] == "claude-haiku-5-5":
            assert json.loads(data["messages"][0]["content"])["body"] == text
            return httpx.Response(200, json={"type": "message", "id": "msg", "role": "assistant",
                "model": data["model"], "content": [{"type": "text", "text": "是"}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 2}})
        if len(requests) == 3:
            raise httpx.ReadTimeout("synthetic")
        events = [{"type": "message_start", "message": {"id": "x", "type": "message", "role": "assistant",
                   "model": "synthetic", "content": [], "usage": {"input_tokens": 1, "output_tokens": 0}}}]
        for index in range(2):
            events.extend([{"type": "content_block_start", "index": index, "content_block": {"type": "tool_use",
                           "id": str(index), "name": "submit_application_reply", "input": {}}},
                           {"type": "content_block_stop", "index": index}])
        events.extend([{"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                        "usage": {"output_tokens": 1}}, {"type": "message_stop"}])
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events))

    monkeypatch.setattr(anthropic, "_direct_transport", lambda _: httpx.MockTransport(receive))
    trial.poll()
    assert tuple(store.row("SELECT status,verdict,attachments FROM registration")) == ("parse", "是", 1)
    trial.poll()
    trial.poll()
    assert trial.box()["state"] == "active"
    assert tuple(store.row("SELECT status,verdict,attachments FROM registration")) == ("转人工（正文未参与解析）", "是", 1)
    assert requests[1:] == expected
    assert inbox.commands.count(("FETCH", "2", "(BODY.PEEK[TEXT])")) == 2
    assert sent and inbox.seen == {1}


def test_raw_payload_fingerprint_decodes_leaf_once(monkeypatch):
    import base64
    from email.message import EmailMessage
    from email.parser import BytesParser

    parsed, decoded = [], []
    parse, decode = BytesParser.parsebytes, EmailMessage.get_content
    def record(self, raw, headersonly=False):
        if not headersonly:
            parsed.append(raw)
        return parse(self, raw, headersonly=headersonly)
    monkeypatch.setattr(BytesParser, "parsebytes", record)
    monkeypatch.setattr(EmailMessage, "get_content", lambda self, **kw: decoded.append(1) or decode(self, **kw))
    header = b"Content-Type: text/plain; charset=utf-8-sig\r\nContent-Transfer-Encoding: base64\r\n\r\n"
    results = [read_fixture(header + base64.b64encode(prefix + b"CURRENT")) for prefix in (b"", b"\xef\xbb\xbf")]
    assert [text for _, text in results] == ["CURRENT"] * 2
    assert results[0][0].digest != results[1][0].digest
    assert len(parsed) == len(decoded) == 2


@pytest.mark.parametrize("tail", [b"--q\r\nTAIL", b"\rTAIL", b""])
def test_segmented_body_ignores_epilogue_boundary(monkeypatch, baseline_modules, tail):
    raw = b"Content-Type: multipart/mixed; boundary=q\r\n\r\n--q\r\n\r\nCURRENT\r\n"
    if tail:
        raw += b"--q--\r\n" + tail
    compare_segments(monkeypatch, baseline_modules, Segments(raw), "CURRENT")


def compare_segments(monkeypatch, baseline_modules, segments, expected_text):
    from qianshu.mail.segmented import read_segmented
    base, adapter = baseline_modules
    text = base.request_text(base.body_parts(segments, 2, segments.sections["HEADER"]))
    assert text == expected_text
    with monkeypatch.context() as old:
        expected = capture_sonnet(old, False, text, adapter=adapter)
    box = Inbox()
    box.add(segments)
    item, verified = read_segmented(box, 2, False)
    actual = capture_sonnet(monkeypatch, True, verified)
    assert len(expected) == len(actual) == (2 if text else 0)
    assert actual == expected
    return item


@pytest.mark.parametrize("structure_id,start,text", [(b"<root>", True, "SECOND"), (b"NIL", True, "SECOND"),
                                                     (b"NIL", False, "FIRST")])
def test_related_binding_uses_server_structure(monkeypatch, baseline_modules, structure_id, start, text):
    raw = (b'Content-Type: multipart/related; boundary=q; start="<root>"\r\n\r\n--q\r\n\r\nFIRST\r\n'
           b'--q\r\nContent-ID: <root> \t\r\n\r\nSECOND\r\n--q--\r\n')
    parts = Segments(raw)
    parts.tree[1][3] = structure_id
    if not start:
        parts.tree[-2] = b"NIL"
    assert compare_segments(monkeypatch, baseline_modules, parts, text).attachments == 1


def test_signed_body_and_signature_batch(monkeypatch, baseline_modules):
    raw = (b"Content-Type: multipart/signed; boundary=q\r\n\r\n--q\r\n\r\nCURRENT\r\n"
           b"--q\r\nContent-Type: application/signature\r\n\r\nSIGNATURE\r\n--q--\r\n")
    assert compare_segments(monkeypatch, baseline_modules, Segments(raw), "CURRENT").attachments == 1


@pytest.mark.parametrize("opaque", [
    b"Content-Type: application/octet-stream\r\nContent-Transfer-Encoding: base64\r\n\r\nYQ",
    b"Content-Type: application/octet-stream\r\nContent-Type: text/plain\r\n\r\nX",
    b"Content-Type: message/rfc822\r\n\r\nContent-Type: multipart/mixed; boundary=z\r\n\r\n--z"])
def test_opaque_attachment_defects_preserve_body(monkeypatch, baseline_modules, opaque):
    raw = (b"Content-Type: multipart/mixed; boundary=q\r\n\r\n--q\r\n\r\nCURRENT\r\n--q\r\n"
           + opaque + b"\r\n--q--\r\n")
    item = compare_segments(monkeypatch, baseline_modules, Segments(raw), "CURRENT")
    assert item.attachments == 1 and fixture_item(raw.replace(opaque, opaque + b"X")).digest != item.digest


def test_outer_identity_and_cross_id_candidate():
    raw = b"Message-ID: <a" + chr(64).encode() + b"local>\r\nSubject: A\r\n\r\nCURRENT"
    item = fixture_item(raw)
    for field in [b"From", b"Date", b"Cc", b"To", b"Subject"]:
        assert fixture_item(field + b": B\r\n" + raw).digest != item.digest
    changed = fixture_item(raw.replace(b"<a", b"<b"))
    assert changed.key != item.key and changed.candidate == item.candidate


@pytest.mark.parametrize("role", ["attachment", "omitted", "inline-filename", "name"])
def test_container_sonnet_matches_old_sections(monkeypatch, baseline_modules, role):
    from trial_segments import synthetic
    inner = synthetic(2, "ATTACHED_SENTINEL")
    inner.make_mixed()
    if role == "name":
        inner.set_param("name", "file")
    else:
        inner["Content-Disposition"] = 'inline; filename="file"' if role == "inline-filename" else "attachment"
    msg = synthetic(2, "")
    msg.clear_content()
    msg.make_mixed()
    msg.attach(inner)
    parts = Segments(msg.as_bytes())
    if role == "omitted":
        parts.tree[0].pop()
    item = compare_segments(monkeypatch, baseline_modules, parts, "" if role == "attachment" else "ATTACHED_SENTINEL")
    assert item.attachments == 1 and item.fields["body"] == ""


def test_final_seen_check_survives_outer_exception_handler(monkeypatch):
    from qianshu.mail.imap import MailError
    from qianshu.mail.segmented import read_segmented
    from trial_segments import synthetic
    box = Inbox()
    box.add(Segments(synthetic(2, "CURRENT").as_bytes()))
    checks = []
    check = box.check_seen

    def observe(uid, seen):
        checks.append(uid)
        if len(checks) == 4:  # HEADER、结构、TEXT 后，最后一次复核
            box.seen.add(uid)
        check(uid, seen)

    monkeypatch.setattr(box, "check_seen", observe)
    try:
        raise RuntimeError("synthetic")
    except RuntimeError:
        with pytest.raises(MailError):
            read_segmented(box, 2, False)
