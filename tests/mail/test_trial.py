import hashlib
import importlib.util
import json
import subprocess
from datetime import date
from email.message import EmailMessage
from pathlib import Path

import httpx
import pytest


def sample(charset="iso-8859-1"):
    return (f"Content-Type: text/plain; charset={charset}\r\n\r\n".encode() + b"\xa1")


def test_identity_charset_and_related_root():
    from qianshu.mail.triage import inspect_mail
    inspect = lambda raw: inspect_mail(raw).digest
    assert inspect(sample()) != inspect(sample("iso-8859-2"))
    raw = b'Content-Type: multipart/related; boundary=q; start="<a>"\n\n'
    raw += b'--q\nContent-ID: <a>\n\nfirst\n--q\nContent-ID: <b>\n\nsecond\n--q--\n'
    assert inspect(raw) != inspect(raw.replace(b'start="<a>"', b'start="<b>"'))


def test_triage_http_contains_only_authored_fields(monkeypatch):
    from qianshu.mail.triage import inspect_mail, classify
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
    item = inspect_mail(msg.as_bytes())
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
], ids=["mixed-headerless", "plain", "alternative", "quoted-printable", "mixed-header-only", "invalid-date"])
def test_mime_to_sonnet_matches_baseline(monkeypatch, baseline_modules, raw):
    from qianshu.mail.message import request_text, snapshot_parts
    baseline, adapter = baseline_modules

    def split_section(wire):
        if wire.startswith(b"\r\n"):
            return wire[:2], wire[2:]
        fields, separator, body = wire.partition(b"\r\n\r\n")
        if not separator and wire.startswith(b"Content-Type:"):
            return wire, b""
        assert separator
        return fields + separator, body

    header, body = split_section(raw)
    root = baseline.headers(header)
    sections = {"TEXT": body}
    structure = [b"TEXT", b"PLAIN"]
    if root.get_content_maintype() == "multipart":
        # 此存根只模拟以上单层样本的 IMAP 分段读取，保留 MIME 字段和正文的原始字节。
        structure = []
        assert body.startswith(b"--q\r\n") and body.endswith(b"\r\n--q--\r\n")
        parts = body.removeprefix(b"--q\r\n").removesuffix(b"\r\n--q--\r\n")
        for index, wire in enumerate(parts.split(b"\r\n--q\r\n"), 1):
            mime, content = split_section(wire)
            sections[f"{index}.MIME"] = mime
            sections[str(index)] = content
            structure.append([b"TEXT", baseline.headers(mime).get_content_subtype().upper().encode()])
        structure.append(root.get_content_subtype().upper().encode())

    class Mailbox:
        def structure(self, uid):
            return structure

        def section(self, uid, section):
            return sections[section]

    previous = baseline.request_text(baseline.body_parts(Mailbox(), 1, header))
    with monkeypatch.context() as old:
        expected = capture_sonnet(old, False, previous, adapter=adapter)
    actual = capture_sonnet(monkeypatch, True, request_text(snapshot_parts(raw)))
    assert len(expected) == len(actual) == 2
    assert [body == reference for body, reference in zip(actual, expected)] == [True, True]
