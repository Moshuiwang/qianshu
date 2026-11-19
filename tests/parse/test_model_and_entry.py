import io
import json
import logging
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import anthropic
import httpx
import pytest

from qianshu.__main__ import main as service_main
from qianshu.config import ConfigError
from qianshu.parse import ModelError, parse_application
from qianshu.parse.__main__ import main
from qianshu.parse.model import configured_model


def test_service_help_describes_current_offline_input(capsys):
    with pytest.raises(SystemExit) as exc:
        service_main(["--help"])

    assert exc.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--inject" in help_text
    assert "model_reply 为合成模型回复字符串。" in help_text
    assert "--ui-config" not in help_text
    assert "--private-dir" not in help_text
    assert "演练" not in help_text


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
        assert payload["stream"] is True
        assert payload["max_tokens"] == 32000
        events = [
            {"type": "message_start", "message": {
                "id": "msg_placeholder",
                "role": "assistant",
                "type": "message",
                "model": expected,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 0},
            }},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "[]"}},
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
            {"type": "message_stop"},
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events),
        )

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


@pytest.fixture
def stub_stream(monkeypatch):
    stream = Mock()
    open_stream = Mock(return_value=nullcontext(stream))
    client = SimpleNamespace(messages=SimpleNamespace(stream=open_stream))
    monkeypatch.setattr(anthropic, "Anthropic", lambda **_: nullcontext(client))
    return open_stream, stream.get_final_message


def test_stream_returns_all_text_blocks(stub_stream):
    open_stream, get_final_message = stub_stream
    get_final_message.return_value = SimpleNamespace(
        stop_reason="end_turn",
        content=[
            SimpleNamespace(type="text", text="["),
            SimpleNamespace(type="thinking"),
            SimpleNamespace(type="text", text="]"),
        ],
    )
    complete = configured_model({
        "ANTHROPIC_API_KEY": "placeholder-key",
        "ANTHROPIC_MODEL": "placeholder-model",
    })
    assert complete(system="占位规则", user="占位正文") == "[]"
    open_stream.assert_called_once_with(
        model="placeholder-model",
        system="占位规则",
        messages=[{"role": "user", "content": "占位正文"}],
        max_tokens=32000,
    )
    get_final_message.assert_called_once_with()


@pytest.mark.parametrize(("stop_reason", "output_tokens"), [("refusal", 1), ("max_tokens", 32000)])
def test_unsuccessful_stop_reason_rejects_even_valid_json(stub_stream, stop_reason, output_tokens):
    _, get_final_message = stub_stream
    get_final_message.return_value = SimpleNamespace(
        stop_reason=stop_reason,
        usage=SimpleNamespace(output_tokens=output_tokens),
        content=[SimpleNamespace(type="text", text="[]")],
    )
    with pytest.raises(ModelError) as exc:
        configured_model({"ANTHROPIC_API_KEY": "placeholder-key"})(system="", user="占位正文")
    assert str(exc.value) == f"申请解析模型回复不完整（stop_reason={stop_reason}，输出 {output_tokens} token）。"


@pytest.mark.parametrize("stop_reason", ["placeholder_reason", "https://model.example.invalid", None])
def test_unknown_stop_reason_with_missing_usage(stub_stream, stop_reason):
    _, get_final_message = stub_stream
    get_final_message.return_value = SimpleNamespace(stop_reason=stop_reason)
    with pytest.raises(ModelError) as exc:
        configured_model({"ANTHROPIC_API_KEY": "placeholder-key"})(system="", user="占位正文")
    assert str(exc.value) == "申请解析模型回复不完整（stop_reason=unknown，输出 未知 token）。"


@pytest.mark.parametrize("error_type", [anthropic.AnthropicError, ValueError])
def test_stream_error_is_sanitized(stub_stream, error_type):
    _, get_final_message = stub_stream
    get_final_message.side_effect = error_type("占位错误详情")
    with pytest.raises(ModelError) as exc:
        configured_model({"ANTHROPIC_API_KEY": "placeholder-key"})(system="", user="占位正文")
    assert str(exc.value) == "申请解析模型调用失败。"


class _BrokenStream(httpx.SyncByteStream):
    """先送出部分事件，再按指定方式中断，模拟流读取中途失败。"""

    def __init__(self, error):
        self._error = error

    def __iter__(self):
        start = {"type": "message_start", "message": {
            "id": "msg_placeholder", "role": "assistant", "type": "message", "model": "placeholder-model",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 0},
        }}
        delta = {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "占位回复"}}
        if self._error is not None:
            for event in (start, {"type": "content_block_start", "index": 0,
                                  "content_block": {"type": "text", "text": ""}}, delta):
                yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
            raise self._error


@pytest.mark.parametrize("error", [
    httpx.ReadError("占位错误详情"),
    httpx.ReadTimeout("占位错误详情"),
    httpx.RemoteProtocolError("占位错误详情"),
    None,  # 流在 message_start 前正常结束
])
def test_stream_interrupted_midway_is_sanitized(monkeypatch, error):
    client_type = anthropic.Anthropic
    http_client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=_BrokenStream(error),
    )))
    monkeypatch.setattr(
        anthropic, "Anthropic",
        lambda **kwargs: client_type(http_client=http_client, max_retries=0, **kwargs),
    )
    config = {"ANTHROPIC_API_KEY": "placeholder-key", "ANTHROPIC_BASE_URL": "https://model.example.invalid"}
    with pytest.raises(ModelError) as exc:
        configured_model(config)(system="", user="占位正文")
    assert str(exc.value) == "申请解析模型调用失败。"


def test_parse_entry_writes_only_to_a_new_private_file(monkeypatch, tmp_path, capsys):
    import qianshu.parse

    def recorded_model(config):
        assert config == {}
        return lambda **_: '[{"company":{"value":"占位公司","evidence":["占位公司"]}}]'

    output = tmp_path / "drafts.json"
    monkeypatch.setattr(qianshu.parse, "configured_model", recorded_model)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rules").symlink_to(Path(__file__).resolve().parents[2] / "rules", target_is_directory=True)
    Path(".private").mkdir()
    Path(".private/companies.tsv").write_text("赞比亚\t占位公司甲\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["qianshu.parse", "--output", str(output)])
    monkeypatch.setattr(sys, "stdin", io.StringIO("赞比亚占位申请"))
    assert main() == 0
    first = capsys.readouterr()
    assert not first.out and not first.err
    contents = output.read_text(encoding="utf-8")
    company = json.loads(contents)[0]["company"]
    assert company["value"] == "占位公司甲"
    assert company["evidence"] == [{"text": "赞比亚占位申请", "start": 0}]
    assert output.stat().st_mode & 0o777 == 0o600
    assert main() == 1
    assert output.read_text(encoding="utf-8") == contents


@pytest.mark.parametrize(("env_file", "rules_dir", "flags"), [
    (".env", "rules", []),
    ("placeholder.env", "placeholder-rules", ["--env-file", "placeholder.env", "--rules-dir", "placeholder-rules"]),
])
def test_parse_entry_loads_config_and_rules_from_selected_paths(monkeypatch, tmp_path, env_file, rules_dir, flags):
    monkeypatch.chdir(tmp_path)
    Path(".env").write_text("ANTHROPIC_MODEL=placeholder-default\n", encoding="utf-8")
    if env_file != ".env":
        Path(env_file).write_text("ANTHROPIC_MODEL=placeholder-custom\n", encoding="utf-8")
    Path(rules_dir).symlink_to(Path(__file__).resolve().parents[2] / "rules", target_is_directory=True)
    configured = Mock(return_value=Mock(return_value="[]"))
    monkeypatch.setattr("qianshu.parse.configured_model", configured)
    monkeypatch.setattr(sys, "argv", ["qianshu.parse", "--output", "drafts.json", *flags])
    monkeypatch.setattr(sys, "stdin", io.StringIO("占位正文"))

    assert main() == 0
    expected_model = "placeholder-default" if env_file == ".env" else "placeholder-custom"
    configured.assert_called_once_with({"ANTHROPIC_MODEL": expected_model})
    configured.return_value.assert_called_once()
    assert json.loads(Path("drafts.json").read_text(encoding="utf-8")) == []


def test_replay_entry_uses_explicit_company_table_instead_of_default(monkeypatch, tmp_path):
    rules = Path("rules").resolve()
    monkeypatch.chdir(tmp_path)
    Path(".private").mkdir()
    Path(".private/companies.tsv").write_text("赞比亚\t占位默认公司\n", encoding="utf-8")
    Path("selected.tsv").write_text("赞比亚\t占位所选公司\n", encoding="utf-8")
    Path("model.json").write_text('[{"company":{"value":"占位模型公司","evidence":["赞比亚"]}}]', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "qianshu.parse", "--output", "drafts.json", "--replay", "model.json",
        "--rules-dir", str(rules), "--companies-file", "selected.tsv",
    ])
    monkeypatch.setattr(sys, "stdin", io.StringIO("赞比亚占位申请"))
    assert main() == 0
    draft, = json.loads(Path("drafts.json").read_text(encoding="utf-8"))
    assert draft["company"]["value"] == "占位所选公司"
    assert draft["company"]["uncertainty"] is None


@pytest.mark.parametrize("reply", [
    '  \r\n[{"company":{"value":"占位公司","evidence":["占位公司"]}}]\r\n',
    "占位无效回复\r\n",
])
def test_raw_output_preserves_original_reply_before_validation(monkeypatch, tmp_path, capsys, reply):
    body = "占位公司"
    raw = tmp_path / "raw.txt"
    output = tmp_path / "drafts.json"
    model = Mock(return_value=reply)
    monkeypatch.setattr("qianshu.parse.__main__.load_config", lambda *, env_file: {})
    monkeypatch.setattr("qianshu.parse.configured_model", lambda _: model)
    monkeypatch.setattr(sys, "argv", ["qianshu.parse", "--output", str(output), "--raw-output", str(raw)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(body))
    exit_code = 0 if reply.lstrip().startswith("[") else 1
    assert main() == exit_code
    assert raw.read_bytes() == reply.encode()
    assert raw.stat().st_mode & 0o777 == 0o600
    assert output.exists() == (exit_code == 0)
    captured = capsys.readouterr()
    assert not captured.out
    if exit_code:
        assert "不是有效的政策 JSON" in captured.err
        assert reply.strip() not in captured.err


@pytest.mark.parametrize("existing", ["file", "dangling_symlink", "same_output"])
def test_raw_output_rejects_existing_or_shared_path_before_model(monkeypatch, tmp_path, existing):
    raw = tmp_path / "raw.txt"
    output = tmp_path / "drafts.json"
    if existing == "file":
        raw.write_text("占位已有文本", encoding="utf-8")
    elif existing == "dangling_symlink":
        raw.symlink_to(tmp_path / "missing")
    else:
        output = raw
    config = Mock(side_effect=AssertionError("不应读取配置"))
    monkeypatch.setattr("qianshu.parse.__main__.load_config", config)
    monkeypatch.setattr(sys, "argv", ["qianshu.parse", "--output", str(output), "--raw-output", str(raw)])
    assert main() == 1
    config.assert_not_called()
    if existing == "file":
        assert raw.read_text(encoding="utf-8") == "占位已有文本"
    elif existing == "dangling_symlink":
        assert raw.is_symlink()
        assert not (tmp_path / "missing").exists()
    else:
        assert not raw.exists()


def test_raw_output_does_not_overwrite_path_created_during_model_call(monkeypatch, tmp_path):
    raw = tmp_path / "raw.txt"
    output = tmp_path / "drafts.json"

    def model(**_):
        raw.write_text("占位并发输出", encoding="utf-8")
        return "[]"

    monkeypatch.setattr("qianshu.parse.__main__.load_config", lambda *, env_file: {})
    monkeypatch.setattr("qianshu.parse.configured_model", lambda _: model)
    monkeypatch.setattr(sys, "argv", ["qianshu.parse", "--output", str(output), "--raw-output", str(raw)])
    monkeypatch.setattr(sys, "stdin", io.StringIO("占位正文"))
    assert main() == 1
    assert raw.read_text(encoding="utf-8") == "占位并发输出"
    assert not output.exists()


def test_raw_output_is_absent_after_model_failure(monkeypatch, tmp_path):
    raw = tmp_path / "raw.txt"
    output = tmp_path / "drafts.json"
    model = Mock(side_effect=ModelError("占位模型调用失败。"))
    monkeypatch.setattr("qianshu.parse.__main__.load_config", lambda *, env_file: {})
    monkeypatch.setattr("qianshu.parse.configured_model", lambda _: model)
    monkeypatch.setattr(sys, "argv", ["qianshu.parse", "--output", str(output), "--raw-output", str(raw)])
    monkeypatch.setattr(sys, "stdin", io.StringIO("占位正文"))
    assert main() == 1
    assert not raw.exists()
    assert not output.exists()


def test_replay_matches_direct_stub_without_reading_api_config(monkeypatch, tmp_path):
    config = Mock(side_effect=AssertionError("不应读取配置或创建模型"))
    monkeypatch.setattr("qianshu.parse.load_config", config)
    monkeypatch.setattr("qianshu.parse.__main__.load_config", config)
    monkeypatch.setattr("qianshu.parse.configured_model", config)
    fixtures = Path(__file__).parent / "fixtures"
    body = (fixtures / "application.txt").read_text(encoding="utf-8")
    replay = fixtures / "model-reply.json"
    output = tmp_path / "drafts.json"
    raw = tmp_path / "raw.txt"
    monkeypatch.setattr(sys, "argv", [
        "qianshu.parse", "--output", str(output), "--replay", str(replay), "--raw-output", str(raw),
        "--env-file", str(tmp_path / "unused.env"),
    ])
    monkeypatch.setattr(sys, "stdin", io.StringIO(body))
    assert main() == 0
    expected = parse_application(body, model=lambda **_: replay.read_text(encoding="utf-8"))
    assert json.loads(output.read_text(encoding="utf-8")) == [draft.to_dict() for draft in expected]
    assert raw.read_bytes() == replay.read_bytes()
    config.assert_not_called()
