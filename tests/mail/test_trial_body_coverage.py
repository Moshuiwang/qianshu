import json
from contextlib import nullcontext
from email.message import EmailMessage
from unittest.mock import patch

import httpx
import pytest

from qianshu.ai import anthropic
from qianshu.mail import segmented
from qianshu.mail.trial_state import MANUAL_BODY_MISSED
from test_trial_journey import journey, mixed
from trial_segments import Segments, synthetic


def leaf(kind, text):
    part = EmailMessage()
    part.set_content(text, subtype=kind)
    return part


def message(shape, missed):
    msg = synthetic(2, "CURRENT")
    if shape in {"plain", "html"}:
        msg.set_content("CURRENT", subtype=shape)
    elif shape == "alternative":
        if missed:
            msg.set_content("\ufeff\u200b")
        msg.add_alternative("<p>CURRENT</p>", subtype="html")
    else:
        msg.make_mixed()
        group = msg
        if shape == "nested":
            group = EmailMessage()
            group.make_mixed()
            msg.attach(group)
        if shape == "embedded":
            msg.add_attachment(leaf("plain", "FORWARDED"), disposition="inline")
        group.attach(leaf("html" if missed else "plain", "OTHER"))
    return msg


def deliver(journey, monkeypatch, msg, answer, missed, *, empty_request=False):
    # 单段的旧入口总会选中自己；注入空请求，单独验证终态门禁。
    selection = patch.object(segmented, "request_text", return_value="") if empty_request else nullcontext()
    with selection:
        worker, _, requests, cards = journey(msg, answer)

        def receive(req):
            requests.append(req.content)
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
        worker.poll()
        row = worker.store.row("SELECT * FROM registration")
        assert row["verdict"] == answer
        assert row["status"] == (MANUAL_BODY_MISSED if missed else "done")
        assert len(requests) == 1 + int(answer != "否" and not empty_request)
        assert ("正文未参与解析" in json.dumps(cards, ensure_ascii=False)) == missed
        assert (worker.store.batch(row["id"], row["version"])[3] == "正文未参与解析") == missed


@pytest.mark.parametrize("shape", ["plain", "html", "alternative", "mixed", "nested", "embedded"])
@pytest.mark.parametrize("missed,answer", [(False, "是"), (True, "是"), (True, "拿不准"), (True, "否")])
def test_structure_coverage(journey, monkeypatch, shape, missed, answer):
    must_review = missed or shape in {"mixed", "nested", "embedded"}
    deliver(journey, monkeypatch, message(shape, missed), answer, must_review,
            empty_request=missed and shape in {"plain", "html"})


@pytest.mark.parametrize("named", [False, True])
def test_independent_html_after_container(journey, monkeypatch, named):
    msg = synthetic(2, "")
    msg.clear_content()
    msg.make_mixed()
    group = leaf("plain", "请查收")
    if named:
        group.make_mixed()
        group.set_param("name", "file")
    else:
        group.add_alternative("请查收", subtype="html")
    msg.attach(group)
    msg.attach(leaf("html", "请配置合成政策甲"))
    deliver(journey, monkeypatch, msg, "拿不准", True)


def test_negative_verdict_after_invisible_plain(journey, monkeypatch):
    deliver(journey, monkeypatch, mixed(("plain", "\ufeff\u200b"), ("html", "<p>请配置合成政策甲</p>")), "否", True)


@pytest.mark.parametrize("shape", ["nested_alternative", "inline_message", "alt_text"])
@pytest.mark.parametrize("answer", ["是", "否"])
def test_additional_visible_segment(journey, monkeypatch, shape, answer):

    msg = synthetic(2, "CURRENT")
    if shape == "nested_alternative":
        msg.make_mixed()
        msg.attach(leaf("html", "OTHER"))
        outer = EmailMessage()
        outer["Message-ID"] = msg["Message-ID"]
        outer.make_alternative()
        outer.attach(msg)
        outer.attach(leaf("html", "CURRENT OTHER"))
        msg = outer
    elif shape == "inline_message":
        inner = leaf("plain", "OTHER")
        msg.add_attachment(inner, disposition="inline")
        parts = Segments(msg.as_bytes())
        node = parts.tree[1]
        node[7:7] = [[b"NIL"] * 10, Segments(inner.as_bytes()).tree, b"1"]
        msg = parts
    else:
        msg.make_mixed()
        msg.attach(leaf("html", '<img alt="OTHER">'))
    deliver(journey, monkeypatch, msg, answer, True)
