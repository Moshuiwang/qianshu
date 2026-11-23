import fcntl
import json
import multiprocessing
import os
import signal
from unittest.mock import Mock

import httpx
import pytest

from qianshu.ai import anthropic
from qianshu.feishu.handoff import HandoffService, MentionLog
from qianshu.mail import trial
from test_trial import baseline_modules, capture_sonnet
from test_trial_journey import journey, mixed
from test_trial_listener import service


@pytest.mark.parametrize("plain,html,answer,body,verdict,missed", [
    ("\ufeff\u200b", "<p>CURRENT</p>", "是", "CURRENT", "是", True),
    ("\ufeff\u200b", "<p>CURRENT</p><p>From: previous</p>", "否", "", "拿不准", True),
    # mixed 里 plain 与 HTML 是两个独立正文；HTML 未进 Sonnet 即按 G437 转人工。
    # 「全部可见正文都进 Sonnet 可记 done」的正例见 test_trial_body_coverage.py。
    ("PLAIN", "<p>CURRENT</p>", "是", "PLAIN", "是", True),
])
def test_body_selection_at_delivery(journey, baseline_modules, monkeypatch, plain, html, answer,
                                    body, verdict, missed):
    segments = mixed(("plain", plain), ("html", html))
    baseline, adapter = baseline_modules
    text = baseline.request_text(baseline.body_parts(segments, 2, segments.sections["HEADER"]))
    with monkeypatch.context() as old:
        # G405 要求同一组分段的 Sonnet 请求与 ccd7420 逐字节一致。
        expected = capture_sonnet(old, False, text, adapter=adapter)[0]
    worker, _, requests, cards = journey(segments, answer)
    assert json.loads(json.loads(requests[0])["messages"][0]["content"])["body"] == body

    def reply(req):
        requests.append(req.content)
        events = [
            dict(type="message_start", message=dict(id="x", role="assistant", type="message",
                 model="synthetic", content=[], usage=dict(input_tokens=9, output_tokens=0))),
            dict(type="content_block_start", index=0, content_block=dict(type="tool_use", id="x",
                 name="submit_application_reply", input={})),
            dict(type="content_block_delta", index=0, delta=dict(type="input_json_delta",
                 partial_json='{"policies":[]}')),
            dict(type="content_block_stop", index=0),
            dict(type="message_delta", delta=dict(stop_reason="tool_use", stop_sequence=None),
                 usage=dict(output_tokens=5)),
            dict(type="message_stop"),
        ]
        wire = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=wire)

    monkeypatch.setattr(anthropic, "_direct_transport", lambda _: httpx.MockTransport(reply))
    worker.poll()
    worker.poll()
    assert requests[1:] == [expected]
    row = worker.store.row("SELECT * FROM registration")
    assert row["verdict"] == verdict
    rendered = json.dumps([c for card, c in cards if card], ensure_ascii=False)
    assert ("部分正文未参与解析，请人工核对原邮件" in rendered) == missed
    assert "CURRENT" not in rendered
    reason = worker.store.batch(row["id"], row["version"])[3]
    assert reason == ("正文未参与解析" if missed else None)
    assert row["status"] == ("转人工（正文未参与解析）" if missed else "done")


@pytest.mark.parametrize("interrupt", [False, True])
def test_lock_held_through_mail_db_close(service, monkeypatch, interrupt):
    path, settings, start = service
    ctx = multiprocessing.get_context("fork")
    closing, resume, closed, returned = (ctx.Event() for _ in range(4))
    original_close = MentionLog.close

    def mail_run(self):
        db = self.store.db

        def close():
            closing.set()
            assert resume.wait(10)
            db.close()
            closed.set()

        self.store.db = Mock(wraps=db)
        self.store.db.close = close
        assert self.stop.wait(10)

    def handoff_close(self):
        original_close(self)
        assert closed.is_set()

    def serve():
        try:
            start(settings)
        except KeyboardInterrupt:
            assert interrupt
        else:
            assert not interrupt
        finally:
            returned.set()

    monkeypatch.setattr(trial.Trial, "run", mail_run)
    monkeypatch.setattr(HandoffService, "run", lambda self: None)
    monkeypatch.setattr(MentionLog, "close", handoff_close)
    process = ctx.Process(target=serve)
    process.start()
    try:
        assert closing.wait(10)
        with path.open("rb") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if interrupt:
                os.kill(process.pid, signal.SIGINT)
                returned.wait(0.5)
                with pytest.raises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            resume.set()
            process.join(10)
            assert process.exitcode == 0
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        resume.set()
        if process.is_alive():
            process.kill()
            process.join()
