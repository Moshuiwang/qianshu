import io
import json
import logging
import sys
from pathlib import Path

import anthropic
import httpx
import pytest

from qianshu.config import ConfigError
from qianshu.parse import ModelError
from qianshu.parse.__main__ import main
from qianshu.parse.model import configured_model


@pytest.mark.parametrize(("model_config", "expected"), [
    ({}, "claude-opus-5-5"),
    ({"ANTHROPIC_MODEL": "placeholder-model"}, "placeholder-model"),
])
def test_anthropic_request_with_recorded_transport(monkeypatch, model_config, expected, caplog):
    client_type = anthropic.Anthropic

    def mocked_reply(request):
        assert request.url.host == "model.example.invalid"
        assert request.headers["x-api-key"] == "placeholder-key"
        payload = json.loads(request.content)
        assert payload["model"] == expected
        assert payload["system"] == "占位规则"
        assert payload["messages"] == [{"role": "user", "content": "占位正文"}]
        assert "tools" not in payload
        return httpx.Response(200, json={
            "id": "msg_placeholder",
            "role": "assistant",
            "type": "message",
            "model": expected,
            "content": [{"type": "text", "text": "[]"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    http_client = httpx.Client(transport=httpx.MockTransport(mocked_reply))
    monkeypatch.setattr(
        anthropic, "Anthropic", lambda **kwargs: client_type(http_client=http_client, **kwargs),
    )
    for name in ("anthropic", "httpx", "httpcore"):
        monkeypatch.setattr(logging.getLogger(name), "level", logging.DEBUG)
    config = {
        "AI_PLATFORM": "anthropic",
        "ANTHROPIC_API_KEY": "placeholder-key",
        "ANTHROPIC_BASE_URL": "https://model.example.invalid",
        **model_config,
    }
    with caplog.at_level(logging.DEBUG):
        assert configured_model(config)(system="占位规则", user="占位正文") == "[]"
    assert "占位正文" not in caplog.text
    assert "model.example.invalid" not in caplog.text


def test_unsupported_platform_is_rejected():
    with pytest.raises(ConfigError, match="只支持 Anthropic"):
        configured_model({"AI_PLATFORM": "placeholder-platform"})


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_unsuccessful_stop_reason_rejects_even_valid_json(monkeypatch, stop_reason):
    class StubClient:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        @property
        def messages(self):
            return self

        def create(self, **_):
            return anthropic.types.Message(
                id="msg_placeholder",
                type="message",
                role="assistant",
                model="placeholder-model",
                usage=anthropic.types.Usage(input_tokens=1, output_tokens=1),
                content=[anthropic.types.TextBlock(type="text", text="[]")],
                stop_reason=stop_reason,
            )

    monkeypatch.setattr(anthropic, "Anthropic", lambda **_: StubClient())
    with pytest.raises(ModelError, match="回复不完整"):
        configured_model({"ANTHROPIC_API_KEY": "placeholder-key"})(system="", user="")


def test_parse_entry_writes_only_to_a_new_private_file(monkeypatch, tmp_path, capsys):
    import qianshu.parse

    def recorded_model(config):
        assert config == {}
        return lambda **_: '[{"company":{"value":"占位公司","evidence":["占位公司"]}}]'

    output = tmp_path / "drafts.json"
    monkeypatch.setattr(qianshu.parse, "configured_model", recorded_model)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rules").symlink_to(Path(__file__).resolve().parents[2] / "rules", target_is_directory=True)
    monkeypatch.setattr(sys, "argv", ["qianshu.parse", "--output", str(output)])
    monkeypatch.setattr(sys, "stdin", io.StringIO("占位公司"))
    assert main() == 0
    first = capsys.readouterr()
    assert not first.out and not first.err
    contents = output.read_text(encoding="utf-8")
    assert json.loads(contents)[0]["company"]["value"] == "占位公司"
    assert output.stat().st_mode & 0o777 == 0o600
    assert main() == 1
    assert output.read_text(encoding="utf-8") == contents
