import io
import json
import logging
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import anthropic
import httpx
import pytest
from anthropic.types import TextBlock, ThinkingBlock, ToolUseBlock

from qianshu.__main__ import main as service_main
from qianshu.config import ConfigError
from qianshu.parse import ModelError, parse_application
from qianshu.parse.__main__ import main
from qianshu.parse.model import ModelReplyError, configured_model, load_model_config
from qianshu.parse.protocol import ApplicationReply
from tests.protocol_fixtures import fixed_reply, serialized_empty


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


@pytest.mark.parametrize(("model_config", "expected", "effort"), [
    ({}, "claude-sonnet-5-5", "medium"),
    ({"ANTHROPIC_MODEL": "placeholder-model", "ANTHROPIC_EFFORT": "low"}, "placeholder-model", "low"),
    *[({"ANTHROPIC_EFFORT": effort}, "claude-sonnet-5-5", effort)
      for effort in ("medium", "high", "xhigh", "max")],
])
def test_anthropic_request_with_recorded_transport(monkeypatch, model_config, expected, effort, caplog):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://ambient.invalid")
    raw = "  \r\n" + serialized_empty() + "\r\n"

    def mocked_reply(request):
        assert request.url.host == "model.example.invalid"
        assert request.headers["x-api-key"] == "placeholder-key"
        payload = json.loads(request.content)
        assert payload["model"] == expected
        assert payload["system"] == "占位规则"
        assert payload["messages"] == [{"role": "user", "content": "占位正文"}]
        assert payload["output_config"] == {"effort": effort}
        assert "thinking" not in payload
        assert payload["tools"] == [{
            "name": "submit_application_reply", "description": "提交一份申请解析声明。",
            "input_schema": ApplicationReply.model_json_schema(),
        }]
        definitions = payload["tools"][0]["input_schema"]["$defs"]
        schema = payload["tools"][0]["input_schema"]
        assert set(schema["properties"]) == {"policies", "unused_passages"}
        assert schema["required"] == ["policies"]
        for name, finding in definitions.items():
            if name.startswith("Finding_"):
                assert set(finding["required"]) == {"status", "reason_code", "value", "candidates", "evidence"}
                assert set(finding["properties"]) == {*finding["required"], "explanation"}
        anchor = definitions["PolicyAnchor"]
        assert {"source_id", "start", "end", "text"} == set(anchor["properties"])
        assert "text" not in anchor["required"]
        assert set(definitions["UnusedPassage"]["required"]) == {"text", "reason"}
        assert "tool_choice" not in payload
        assert payload["stream"] is True
        assert payload["max_tokens"] == 128000
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
            {"type": "content_block_start", "index": 0, "content_block": {
                "type": "thinking", "thinking": "占位思考", "signature": "placeholder",
            }},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {
                "type": "tool_use", "name": "submit_application_reply", "id": "toolu_placeholder", "input": {},
            }},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": raw}},
            {"type": "content_block_stop", "index": 1},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
            {"type": "message_stop"},
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events),
        )

    monkeypatch.setattr(anthropic, "DefaultHttpxClient", lambda *, transport, **kw: httpx.Client(
        transport=httpx.MockTransport(mocked_reply), **kw))
    for name in ("anthropic", "httpx", "httpcore"):
        monkeypatch.setattr(logging.getLogger(name), "level", logging.DEBUG)
    config = {
        "AI_PLATFORM": "anthropic",
        "ANTHROPIC_API_KEY": "placeholder-key",
        "ANTHROPIC_BASE_URL": "https://model.example.invalid",
        **model_config,
    }
    captured = []
    with caplog.at_level(logging.DEBUG):
        complete = configured_model(config, on_result=captured.append)
        assert complete(system="占位规则", user="占位正文") == raw
    result, = captured
    assert result.reply == raw
    assert result.effort == effort
    assert "占位正文" not in caplog.text
    assert "model.example.invalid" not in caplog.text


def test_unsupported_platform_is_rejected():
    with pytest.raises(ConfigError, match="AI_PLATFORM.*不受支持"):
        configured_model({"AI_PLATFORM": "placeholder-platform"})


@pytest.mark.parametrize("effort", ["", " ", "HIGH", "unsupported"])
def test_invalid_effort_from_config_file_is_rejected(tmp_path, effort):
    config_file = tmp_path / "placeholder.env"
    config_file.write_text(
        f"ANTHROPIC_API_KEY=placeholder-key\nANTHROPIC_EFFORT='{effort}'\n", encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="ANTHROPIC_EFFORT"):
        configured_model(load_model_config(env_file=config_file))


@pytest.fixture
def stub_stream(monkeypatch):
    get_reply = Mock()

    def events():
        reply = get_reply()
        # 和 SDK 一样从空内容开始，逐块提交。
        message = SimpleNamespace(**{**vars(reply), "content": []})
        yield SimpleNamespace(type="message_start", message=message)
        for index, block in enumerate(getattr(reply, "content", ())):
            yield SimpleNamespace(type="content_block_start", index=index, content_block=block)
            if block.type == "tool_use":
                yield SimpleNamespace(type="content_block_delta", index=index, delta=SimpleNamespace(
                    type="input_json_delta", partial_json=json.dumps(block.input),
                ))

            yield SimpleNamespace(type="content_block_stop", index=index)
        yield SimpleNamespace(type="message_stop")

    open_stream = Mock(side_effect=lambda **_: nullcontext(events()))
    client = SimpleNamespace(messages=SimpleNamespace(create=open_stream))
    monkeypatch.setattr(anthropic, "Anthropic", lambda **_: nullcontext(client))
    return open_stream, get_reply


def _reply_tool(input, *, name="submit_application_reply"):
    return ToolUseBlock(type="tool_use", id="toolu_placeholder", name=name, input=input)


def test_stream_returns_only_tool_input(stub_stream):
    _, get_reply = stub_stream
    payload = json.loads(serialized_empty())
    get_reply.return_value = SimpleNamespace(
        stop_reason="tool_use",
        content=[
            TextBlock(type="text", text="占位说明"),
            _reply_tool(payload),
            ThinkingBlock(type="thinking", thinking="占位思考", signature="placeholder"),
        ],
    )
    complete = configured_model({
        "ANTHROPIC_API_KEY": "placeholder-key",
        "ANTHROPIC_MODEL": "placeholder-model",
    })
    assert json.loads(complete(system="占位规则", user="占位正文")) == payload
    get_reply.assert_called_once_with()


@pytest.mark.parametrize(("case", "code", "path"), [
    ("missing", "reply_tool_missing", ()),
    ("multiple", "reply_tool_multiple", ()),
    ("unexpected", "reply_tool_unexpected", ()),
    ("invalid", "reply_schema", ("policies",)),
])
def test_stream_rejects_bad_tool_reply_and_records_content(stub_stream, case, code, path):
    _, get_reply = stub_stream
    payload = json.loads(serialized_empty())
    invalid = {**payload, "policies": "占位非法政策清单"}
    tools = {
        "missing": [], "multiple": [_reply_tool(payload), _reply_tool(invalid)],
        "unexpected": [_reply_tool(payload, name="unexpected_tool")], "invalid": [_reply_tool(invalid)],
    }[case]
    blocks = [TextBlock(type="text", text=json.dumps(payload)), *tools]
    get_reply.return_value = SimpleNamespace(
        stop_reason="end_turn" if case == "missing" else "tool_use", content=blocks,
    )
    captured = []
    complete = configured_model({"ANTHROPIC_API_KEY": "placeholder-key"}, on_result=captured.append)

    with pytest.raises(ModelReplyError, match="回复解析失败") as exc:
        complete(system="", user="")
    assert (exc.value.code, exc.value.path) == (code, path)
    assert len(captured) == (2 if case == "multiple" else 1)
    result = captured[-1]
    assert result.failure_stage == code
    assert result.reply == exc.value.reply
    if case == "invalid":
        assert result.reply == json.dumps(invalid)
    else:
        assert json.loads(result.reply) == [
            {**block.model_dump(exclude_unset=True), "input": json.dumps(block.input)}
            if block.type == "tool_use" else block.model_dump(exclude_unset=True)
            for block in blocks
        ]


@pytest.mark.parametrize("stop_reason", ["tool_use", "max_tokens"])
def test_result_callback_records_blocks_and_use_without_changing_return(stub_stream, stop_reason):
    _, get_reply = stub_stream
    payload = json.loads(serialized_empty())
    blocks = [
        TextBlock(type="text", text="占位说明"),
        ThinkingBlock(type="thinking", thinking="占位思考", signature="placeholder"),
        _reply_tool(payload),
    ]
    get_reply.return_value = SimpleNamespace(
        stop_reason=stop_reason,
        model="synthetic-resolved-model",
        usage=SimpleNamespace(input_tokens=9, output_tokens=4),
        content=blocks,
    )
    captured = []
    complete = configured_model({"ANTHROPIC_API_KEY": "placeholder-key"}, on_result=captured.append)
    if stop_reason == "tool_use":
        assert json.loads(complete(system="", user="")) == payload
    else:
        with pytest.raises(ModelReplyError, match="max_tokens") as exc:
            complete(system="", user="")
        assert exc.value.code == "reply_truncated"
    result, = captured
    raw = json.dumps(payload)
    expected = payload if stop_reason == "tool_use" else [
        blocks[0].model_dump(exclude_unset=True),
        blocks[1].model_dump(exclude_unset=True),
        {**blocks[2].model_dump(exclude_unset=True), "input": raw},
    ]
    assert json.loads(result.reply) == expected
    assert result.stop_reason == stop_reason
    assert result.blocks == (("text", 4), ("thinking", 4), ("tool_use", len(raw)))
    assert (result.input_tokens, result.output_tokens, result.model) == (9, 4, "synthetic-resolved-model")
    assert result.elapsed_ms >= 0
    assert result.failure_stage == (None if stop_reason == "tool_use" else "model_incomplete")


@pytest.mark.parametrize(("stop_reason", "output_tokens"), [("refusal", 1), ("max_tokens", 128000)])
def test_unsuccessful_stop_reason_rejects_even_valid_json(stub_stream, stop_reason, output_tokens):
    _, get_reply = stub_stream
    get_reply.return_value = SimpleNamespace(
        stop_reason=stop_reason,
        usage=SimpleNamespace(output_tokens=output_tokens),
        content=[SimpleNamespace(type="text", text="[]")],
    )
    with pytest.raises(ModelError) as exc:
        configured_model({"ANTHROPIC_API_KEY": "placeholder-key"})(system="", user="占位正文")
    assert str(exc.value) == f"申请解析模型回复不完整（stop_reason={stop_reason}，输出 {output_tokens} token）。"


@pytest.mark.parametrize("stop_reason", ["placeholder_reason", "https://model.example.invalid", None])
def test_unknown_stop_reason_with_missing_usage(stub_stream, stop_reason):
    _, get_reply = stub_stream
    get_reply.return_value = SimpleNamespace(stop_reason=stop_reason)
    with pytest.raises(ModelError) as exc:
        configured_model({"ANTHROPIC_API_KEY": "placeholder-key"})(system="", user="占位正文")
    assert str(exc.value) == "申请解析模型回复不完整（stop_reason=unknown，输出 未知 token）。"


@pytest.mark.parametrize("error_type", [anthropic.AnthropicError, ValueError])
def test_stream_error_is_sanitized(stub_stream, error_type):
    _, get_reply = stub_stream
    get_reply.side_effect = error_type("占位错误详情")
    with pytest.raises(ModelError) as exc:
        configured_model({"ANTHROPIC_API_KEY": "placeholder-key"})(system="", user="占位正文")
    assert str(exc.value) == "申请解析模型调用失败。"


class _BrokenStream(httpx.SyncByteStream):
    """先送出部分事件，再按指定方式中断，模拟流读取中途失败。"""

    def __init__(self, error, block_type):
        self._error = error
        self._block_type = block_type

    def __iter__(self):
        start = {"type": "message_start", "message": {
            "id": "msg_placeholder", "role": "assistant", "type": "message", "model": "placeholder-model",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 0},
        }}
        if self._error is not None:
            if self._block_type == "tool_use":
                block = {"type": "tool_use", "id": "toolu_placeholder", "name": "submit_application_reply", "input": {}}
                delta = {"type": "input_json_delta", "partial_json": '{"policies": ['}
            else:
                block = {"type": "text", "text": ""}
                delta = {"type": "text_delta", "text": "占位回复"}
            for event in (start, {"type": "content_block_start", "index": 0, "content_block": block},
                          {"type": "content_block_delta", "index": 0, "delta": delta}):
                yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
            raise self._error


@pytest.mark.parametrize(("error", "block_type"), [
    (httpx.ReadError("占位错误详情"), "text"),
    (httpx.ReadTimeout("占位错误详情"), "text"),
    (httpx.RemoteProtocolError("占位错误详情"), "text"),
    (httpx.ReadTimeout("占位错误详情"), "tool_use"),
    (None, "text"),  # 流在 message_start 前正常结束
])
def test_stream_interrupted_midway_is_sanitized(monkeypatch, error, block_type):
    def interrupted(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=_BrokenStream(error, block_type))

    monkeypatch.setattr(anthropic, "DefaultHttpxClient", lambda *, transport, **kw: httpx.Client(
        transport=httpx.MockTransport(interrupted), **kw))
    config = {"ANTHROPIC_API_KEY": "placeholder-key", "ANTHROPIC_BASE_URL": "https://model.example.invalid"}
    captured = []
    with pytest.raises(ModelError) as exc:
        configured_model(config, on_result=captured.append)(system="", user="占位正文")
    result, = captured
    assert result.failure_stage == ("model_call" if error is None else "reply_truncated")
    if block_type == "tool_use":
        block, = json.loads(result.reply)
        assert block["type"] == "tool_use" and block["input"] == '{"policies": ['
    else:
        assert result.reply == (None if error is None else "占位回复")
    assert str(exc.value) == "申请解析模型调用失败。"


def test_parse_entry_writes_only_to_a_new_private_file(monkeypatch, tmp_path, capsys):
    import qianshu.parse

    def recorded_model(config):
        assert config == {}
        return lambda **_: json.dumps(fixed_reply("赞比亚占位申请", [
            {"country": {"value": "赞比亚", "evidence": ["赞比亚占位申请"]}},
        ]))

    output = tmp_path / "drafts.json"
    monkeypatch.setattr(qianshu.parse, "configured_model", recorded_model)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rules").symlink_to(Path(__file__).resolve().parents[2] / "rules", target_is_directory=True)
    Path(".private").mkdir()
    Path(".private/companies.tsv").write_text(
        "国家\t关键词\t公司内部编号\t公司显示名\n赞比亚\t\t0001\t占位公司甲\n", encoding="utf-8",
    )
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
    Path(".env").write_text(
        "ANTHROPIC_MODEL=placeholder-default\nANTHROPIC_EFFORT=low\n", encoding="utf-8",
    )
    if env_file != ".env":
        Path(env_file).write_text(
            "ANTHROPIC_MODEL=placeholder-custom\nANTHROPIC_EFFORT=xhigh\n", encoding="utf-8",
        )
    Path(rules_dir).symlink_to(Path(__file__).resolve().parents[2] / "rules", target_is_directory=True)
    configured = Mock(return_value=Mock(return_value=serialized_empty()))
    monkeypatch.setattr("qianshu.parse.configured_model", configured)
    monkeypatch.setattr(sys, "argv", ["qianshu.parse", "--output", "drafts.json", *flags])
    monkeypatch.setattr(sys, "stdin", io.StringIO("占位正文"))

    assert main() == 0
    expected_model = "placeholder-default" if env_file == ".env" else "placeholder-custom"
    expected_effort = "low" if env_file == ".env" else "xhigh"
    configured.assert_called_once_with({"ANTHROPIC_MODEL": expected_model, "ANTHROPIC_EFFORT": expected_effort})
    configured.return_value.assert_called_once()
    assert json.loads(Path("drafts.json").read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("legacy", [False, True])
def test_replay_entry_uses_explicit_company_table_instead_of_default(monkeypatch, tmp_path, legacy):
    rules = Path("rules").resolve()
    monkeypatch.chdir(tmp_path)
    Path(".private").mkdir()
    header = "国家\t关键词\t公司内部编号\t公司显示名\n"
    Path(".private/companies.tsv").write_text(header + "赞比亚\t\t0001\t占位默认公司\n", encoding="utf-8")
    selected = "赞比亚\t占位所选公司\n" if legacy else header + "赞比亚\t\t0002\t占位所选公司\n"
    Path("selected.tsv").write_text(selected, encoding="utf-8")
    reply = fixed_reply("赞比亚占位申请", [{"country": {"value": "赞比亚", "evidence": ["赞比亚"]}}])
    Path("model.json").write_text(json.dumps(reply), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "qianshu.parse", "--output", "drafts.json", "--replay", "model.json",
        "--rules-dir", str(rules), "--companies-file", "selected.tsv",
        *(["--legacy-companies"] if legacy else []),
    ])
    monkeypatch.setattr(sys, "stdin", io.StringIO("赞比亚占位申请"))
    assert main() == 0
    draft, = json.loads(Path("drafts.json").read_text(encoding="utf-8"))
    assert draft["company"]["value"] == "占位所选公司"
    assert draft["company"]["uncertainty"] is None


@pytest.mark.parametrize("reply", [
    "  \r\n" + json.dumps(fixed_reply("占位公司", [{}])) + "\r\n",
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
    exit_code = 0 if reply.lstrip().startswith("{") else 1
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


@pytest.mark.parametrize("passages", [None, [{"text": "合成申请材料", "reason": "占位说明"}]])
def test_replay_writes_empty_drafts_and_optional_passages(monkeypatch, tmp_path, capsys, passages):
    body = "合成申请材料"
    replay = tmp_path / "reply.json"
    output = tmp_path / "drafts.json"
    raw = tmp_path / "raw.json"
    reply = "  \r\n" + json.dumps({"policies": [], "unused_passages": passages}, ensure_ascii=False) + "\r\n"
    replay.write_bytes(reply.encode())
    monkeypatch.setattr(sys, "argv", [
        "qianshu.parse", "--replay", str(replay), "--output", str(output), "--raw-output", str(raw),
        "--companies-file", str(Path(__file__).parent / "fixtures" / "companies.tsv"),
    ])
    monkeypatch.setattr(sys, "stdin", io.StringIO(body))

    assert main() == 0
    expected = {"drafts": [], "unused_passages": passages} if passages else []
    assert json.loads(output.read_text(encoding="utf-8")) == expected
    assert raw.read_bytes() == reply.encode()
    assert raw.stat().st_mode & 0o777 == 0o600
    assert capsys.readouterr() == ("", "")


def test_replay_writes_full_review_for_unlocated_policy_anchor(monkeypatch, tmp_path):
    body = "合成申请"
    reply = fixed_reply(body, [{}])
    reply["policies"][0]["main"]["text"] = "合成未找到的引文"
    replay = tmp_path / "reply.json"
    output = tmp_path / "drafts.json"
    replay.write_text(json.dumps(reply, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["qianshu.parse", "--replay", str(replay), "--output", str(output)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(body))

    assert main() == 0
    parsed = json.loads(output.read_text(encoding="utf-8"))
    assert parsed["requires_full_review"] is True
    assert parsed["structure_issues"] == [
        {"span": None, "code": "unlocated_policy_main", "policy_ids": ["p1"]},
    ]
    assert parsed["drafts"][0]["repeat_bonus"]["value"] == "是"


@pytest.fixture
def output_worktree(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run(["git", "init", "--quiet", "--template="], cwd=worktree, check=True)
    (worktree / ".gitignore").write_text(".private/\n", encoding="utf-8")
    (worktree / ".private").mkdir(mode=0o700)
    return worktree


@pytest.mark.parametrize("blocked_flag", ["--output", "--raw-output"])
def test_parse_rejects_unignored_output_before_reading_config(
    monkeypatch, tmp_path, capsys, output_worktree, blocked_flag,
):
    monkeypatch.chdir(tmp_path)
    draft = Path("worktree", ".private" if blocked_flag == "--raw-output" else "", "draft.json")
    raw = Path("worktree", ".private" if blocked_flag == "--output" else "", "raw.json")
    config = Mock(side_effect=AssertionError("不应读取配置"))
    monkeypatch.setattr("qianshu.parse.__main__.load_config", config)
    monkeypatch.setattr(sys, "argv", ["qianshu.parse", "--output", str(draft), "--raw-output", str(raw)])

    assert main() == 1
    assert capsys.readouterr() == ("", "输出路径位于 Git 工作区，须已被 Git 忽略且未被跟踪。\n")
    config.assert_not_called()
    assert not draft.exists()
    assert not raw.exists()


@pytest.mark.parametrize("path_type", ["tracked", "symlink"])
def test_parse_rejects_path_under_ignored_directory(monkeypatch, capsys, output_worktree, path_type):
    private = output_worktree / ".private"
    if path_type == "tracked":
        output = private / "draft.json"
        output.touch()
        subprocess.run(["git", "add", "-f", "--", output], cwd=output_worktree, check=True)
        output.unlink()
        target = output
    else:
        alias = private / "alias"
        alias.symlink_to(output_worktree, target_is_directory=True)
        output = alias / "draft.json"
        target = output_worktree / output.name
    monkeypatch.setattr(sys, "argv", ["qianshu.parse", "--output", str(output)])

    assert main() == 1
    assert capsys.readouterr() == ("", "输出路径位于 Git 工作区，须已被 Git 忽略且未被跟踪。\n")
    assert not target.exists()


@pytest.mark.parametrize("override", ["worktree", "index"])
def test_parse_checks_output_worktree_and_its_index(monkeypatch, capsys, output_worktree, override):
    other = output_worktree.with_name("other-worktree")
    other.mkdir()
    subprocess.run(["git", "init", "--quiet", "--template="], cwd=other, check=True)
    (other / ".gitignore").write_text("/draft.json\n/.private/\n", encoding="utf-8")
    if override == "worktree":
        output = output_worktree / "draft.json"
        monkeypatch.setenv("GIT_DIR", str(other / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(other))
    else:
        output = output_worktree / ".private" / "draft.json"
        output.touch()
        subprocess.run(["git", "add", "-f", "--", output], cwd=output_worktree, check=True)
        output.unlink()
        monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))
    config = Mock(side_effect=AssertionError("不应读取配置"))
    monkeypatch.setattr("qianshu.parse.__main__.load_config", config)
    monkeypatch.setattr(sys, "argv", ["qianshu.parse", "--output", str(output)])

    assert main() == 1
    assert capsys.readouterr() == ("", "输出路径位于 Git 工作区，须已被 Git 忽略且未被跟踪。\n")
    config.assert_not_called()
    assert not output.exists()


def test_parse_writes_git_ignored_outputs(monkeypatch, capsys, output_worktree):
    fixtures = Path(__file__).parent / "fixtures"
    replay = fixtures / "model-reply.json"
    monkeypatch.chdir(output_worktree)
    draft = Path(".private/draft.json")
    raw = Path(".private/raw.json")
    monkeypatch.setattr(sys, "argv", [
        "qianshu.parse", "--output", str(draft), "--raw-output", str(raw), "--replay", str(replay),
        "--rules-dir", str(fixtures.parents[2] / "rules"), "--companies-file", str(fixtures / "companies.tsv"),
    ])
    monkeypatch.setattr(sys, "stdin", io.StringIO((fixtures / "application.txt").read_text(encoding="utf-8")))

    assert main() == 0
    assert capsys.readouterr() == ("", "")
    assert draft.is_file()
    assert raw.read_bytes() == replay.read_bytes()
    result = subprocess.run(["git", "status", "--porcelain", "--", draft, raw], capture_output=True, check=True)
    assert result.stdout == b""


def test_service_rejects_unignored_private_raw_directory(monkeypatch, capsys, output_worktree):
    output = output_worktree / "raw"
    output.mkdir(mode=0o700)
    config = Mock(side_effect=AssertionError("不应读取配置"))
    monkeypatch.setattr("qianshu.__main__.load_config", config)

    assert service_main([
        "--state", str(output_worktree / "state.sqlite"),
        "--window-start", "2099-01-01T00:00:00+00:00", "--window-end", "2099-01-02T00:00:00+00:00",
        "--companies-file", str(Path(__file__).parent / "fixtures" / "companies.tsv"), "--raw-output-dir", str(output),
    ]) == 1
    assert capsys.readouterr() == ("", "输出路径位于 Git 工作区，须已被 Git 忽略且未被跟踪。\n")
    config.assert_not_called()
    assert not list(output.iterdir())


@pytest.mark.parametrize("shape", ["missing_evidence", "missing_quote_text", "reply_tool_multiple"])
@pytest.mark.parametrize("repaired", [False, True])
def test_shape_retry_through_group_card(stub_stream, monkeypatch, shape, repaired):
    from copy import deepcopy
    from qianshu import validate
    from qianshu.feishu import Message
    from qianshu.feishu.entrance import Application
    from qianshu.pipeline import Pipeline

    body = "合成保留标记甲"
    valid = fixed_reply(body, [{"user_scope": {"value": "占位用户", "evidence": ["合成不在原文的出处"]}}])
    invalid = deepcopy(valid)
    finding = invalid["policies"][0]["fields"]["user_scope"]
    path = ["policies", 0, "fields", "user_scope", "evidence"]
    if shape == "missing_evidence":
        del finding["evidence"]
    elif shape == "missing_quote_text":
        del finding["evidence"][0]["text"]
        path += [0, "text"]
    else:
        path = ["content"]
    bad_blocks = [_reply_tool(invalid)] * (2 if shape == "reply_tool_multiple" else 1)
    stream, get_reply = stub_stream
    get_reply.side_effect = [SimpleNamespace(stop_reason="tool_use", content=blocks)
                             for blocks in (bad_blocks, [_reply_tool(valid)] if repaired else bad_blocks)]
    results = []
    model = configured_model({"ANTHROPIC_API_KEY": "synthetic"}, on_result=results.append)
    validate_batch = Mock(wraps=validate.validate_batch)
    monkeypatch.setattr(validate, "validate_batch", validate_batch)
    pipeline = Pipeline(model=model, companies_file=Path(__file__).parent / "fixtures/companies.tsv", tag_dates={})
    message = Message("合成消息", "合成群", "合成发送者", 0, "text", "")
    pages = pipeline(Application(message, body, ()))
    elements = [el for page in pages for el in page.elements]
    card = json.dumps(elements, ensure_ascii=False)
    assert stream.call_count == 2
    first, second = (call.kwargs["messages"] for call in stream.call_args_list)
    assert second[:1] == first
    assert all(block["input"] == {} for block in second[1]["content"])
    assert all(json.loads(item["content"]) == {"path": path, "reason": shape} for item in second[2]["content"])
    assert body not in json.dumps(second[1:], ensure_ascii=False)
    assert [r.first_shape_error for r in results] == [shape, shape]
    assert results[-1].retry_result == ("succeeded" if repaired else "failed")
    if repaired:
        assert "解析完成，共 1 条政策" in card
        assert "用户范围 · 待确认" in card
        validate_batch.assert_called_once()
    else:
        assert "解析不完整，需人工确认" in card and "政策 1 ·" not in card
        validate_batch.assert_not_called()
        assert all(el["tag"] != "action" for el in elements)


@pytest.mark.parametrize("damage", ["bad_start", "negative_delta", "bad_stop", "missing_stop", None])
def test_stream_requires_matching_indices_and_message_stop(monkeypatch, damage):
    block = _reply_tool({"policies": []})
    message = SimpleNamespace(content=[], stop_reason="tool_use", usage=SimpleNamespace(input_tokens=7, output_tokens=1))
    events = [
        SimpleNamespace(type="message_start", message=message),
        SimpleNamespace(type="content_block_start", index=1 if damage == "bad_start" else 0, content_block=block),
        SimpleNamespace(type="content_block_delta", index=-1 if damage == "negative_delta" else 0,
                        delta=SimpleNamespace(type="input_json_delta", partial_json=serialized_empty())),
        SimpleNamespace(type="content_block_stop", index=1 if damage == "bad_stop" else 0),
    ]
    if damage != "missing_stop":
        events.append(SimpleNamespace(type="message_stop"))
    transport = Mock(return_value=nullcontext(events))
    client = SimpleNamespace(messages=SimpleNamespace(create=transport))
    monkeypatch.setattr(anthropic, "Anthropic", lambda **_: nullcontext(client))
    calls = []
    model = configured_model({"ANTHROPIC_API_KEY": "synthetic"}, on_result=calls.append)
    if damage:
        with pytest.raises(ModelReplyError) as exc:
            model(system="", user="")
        assert exc.value.code == "reply_truncated"
        assert calls[0].partial_input_tokens == 7 and calls[0].input_tokens is None
    else:
        assert model(system="", user="") == serialized_empty()
        assert calls[0].input_tokens == 7 and calls[0].partial_input_tokens is None
    assert transport.call_count == len(calls) == 1


def test_shape_correction_network_failure_has_no_sdk_retry(monkeypatch):
    requests = []

    def reply(request):
        requests.append(json.loads(request.content)["messages"])
        if len(requests) > 1:
            return httpx.Response(503, json={"type": "error", "error": {"type": "api_error", "message": "synthetic"}})
        value = fixed_reply("合成材料", [{}])
        del value["policies"][0]["fields"]["country"]["evidence"]
        events = [
            {"type": "message_start", "message": {"id": "msg_synthetic", "type": "message", "role": "assistant",
             "model": "synthetic", "content": [], "stop_reason": None, "stop_sequence": None,
             "usage": {"input_tokens": 1, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {
                "type": "tool_use", "id": "toolu_synthetic", "name": "submit_application_reply", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {
                "type": "input_json_delta", "partial_json": json.dumps(value)}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
             "usage": {"output_tokens": 1}},
            {"type": "message_stop"},
        ]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events))

    monkeypatch.setattr(anthropic, "DefaultHttpxClient", lambda *, transport, **kw: httpx.Client(
        transport=httpx.MockTransport(reply), **kw))
    results = []
    model = configured_model({"ANTHROPIC_API_KEY": "synthetic"}, on_result=results.append)
    with pytest.raises(ModelError):
        model(system="", user="合成材料")
    assert len(requests) == len(results) == 2
    assert results[-1].retry_result == "failed"


def test_manual_platform_switch_uses_same_reply_validation(stub_stream, monkeypatch):
    from qianshu import parse
    from qianshu.ai import ADAPTERS

    body = "合成申请"
    payload = fixed_reply(body, [{"country": {"value": "合成国家", "evidence": ["缺失引文"]}}])
    open_stream, tool_reply = stub_stream
    tool_reply.return_value = SimpleNamespace(stop_reason="tool_use", content=[_reply_tool(payload)])
    json_reply = Mock(return_value=json.dumps(payload))
    json_adapter = Mock(return_value=json_reply)
    monkeypatch.setitem(ADAPTERS, "json-fixture", json_adapter)
    config = {
        "ANTHROPIC_API_KEY": "primary-fixture",
        "AI_BACKUP_PLATFORM": "json-fixture",
        "AI_BACKUP_MODEL": "backup-model",
        "AI_BACKUP_BASE_URL": "https://backup.invalid",
        "AI_BACKUP_API_KEY": "backup-fixture",
    }
    decode = Mock(wraps=parse._decode)
    monkeypatch.setattr(parse, "_decode", decode)
    primary, = parse_application(body, config=config, companies_text="")
    json_adapter.assert_not_called()
    config["AI_ACTIVE_PROFILE"] = "backup"
    backup, = parse_application(body, config=config, companies_text="")
    assert decode.call_count == 2
    assert primary.to_dict() == backup.to_dict()
    assert backup.field_results["country"].needs_confirmation
    request = open_stream.call_args.kwargs
    json_reply.assert_called_once_with(system=request["system"], user=request["messages"][0]["content"])
    json_adapter.assert_called_once_with(api_key="backup-fixture", base_url="https://backup.invalid",
                                         model="backup-model", effort="medium", max_retries=None, on_result=None,
                                         follow_redirects=False)

    json_reply.return_value = "{}"
    with pytest.raises(parse.ParseError) as error:
        parse_application(body, config=config, companies_text="")
    assert error.value.code == "reply_schema"

    json_reply.side_effect = ModelError("合成平台不可用")
    with pytest.raises(ModelError):
        parse_application(body, config=config, companies_text="")
    open_stream.assert_called_once()
    config["AI_ACTIVE_PROFILE"] = "primary"
    tool_reply.side_effect = anthropic.AnthropicError("合成平台不可用")
    with pytest.raises(ModelError):
        parse_application(body, config=config, companies_text="")
    assert json_reply.call_count == 3


@pytest.mark.parametrize("missing", ["PLATFORM", "MODEL", "BASE_URL", "API_KEY"])
def test_backup_requires_its_own_configuration(missing):
    config = {
        "AI_ACTIVE_PROFILE": "backup",
        "ANTHROPIC_API_KEY": "primary-fixture",
        "AI_BACKUP_PLATFORM": "anthropic",
        "AI_BACKUP_MODEL": "backup-model",
        "AI_BACKUP_BASE_URL": "https://backup.invalid",
        "AI_BACKUP_API_KEY": "backup-fixture",
    }
    del config["AI_BACKUP_" + missing]
    with pytest.raises(ConfigError, match="AI_BACKUP_" + missing):
        configured_model(config)


def test_unknown_profile_cannot_select_a_platform():
    with pytest.raises(ConfigError, match="AI_ACTIVE_PROFILE"):
        configured_model({"AI_ACTIVE_PROFILE": "auto", "ANTHROPIC_API_KEY": "fixture"})


def test_business_modules_do_not_import_model_sdks():
    import ast

    root = Path(__file__).resolve().parents[2] / "src/qianshu"
    vendors = {"anthropic", "openai", "boto3", "vertexai", "google.genai", "google.generativeai"}
    for path in root.rglob("*.py"):
        if path.relative_to(root).parts[0] in {"ai", "regression"}:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [f"{node.module}.{alias.name}" for alias in node.names]
            else:
                continue
            assert not any(name == sdk or name.startswith(sdk + ".") for name in names for sdk in vendors), path


def test_backup_uses_only_configured_endpoint_and_key(monkeypatch, tmp_path):
    settings = tmp_path / "platforms.conf"
    settings.write_text(
        "AI_ACTIVE_PROFILE=backup\nANTHROPIC_API_KEY=primary-fixture\n"
        "ANTHROPIC_BASE_URL=https://primary.invalid\nAI_BACKUP_PLATFORM=anthropic\n"
        "AI_BACKUP_MODEL=backup-model\nAI_BACKUP_BASE_URL=https://backup.invalid\n"
        "AI_BACKUP_API_KEY=backup-fixture\n", encoding="utf-8",
    )
    requests = []

    def redirect(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://other.invalid"})

    monkeypatch.setattr(anthropic, "DefaultHttpxClient", lambda *, transport, **kw: httpx.Client(
        transport=httpx.MockTransport(redirect), **kw))
    model = configured_model(load_model_config(env_file=settings))
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "x-test: synthetic")
    with pytest.raises(ConfigError):
        model(system="合成规则", user="合成申请")
    assert not requests
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS")
    with pytest.raises(ModelError):
        model(system="合成规则", user="合成申请")
    request, = requests
    assert request.url.host == "backup.invalid"
    assert request.headers["x-api-key"] == "backup-fixture"
    assert json.loads(request.content)["model"] == "backup-model"


@pytest.mark.parametrize(("profile", "location"), [
    ("primary", "https://backup.invalid"),
    ("backup", "https://primary.invalid"),
    ("primary", "https://primary.invalid:8443"),
    ("backup", "http://backup.invalid"),
])
def test_cross_origin_redirect_stops_after_first_request(monkeypatch, profile, location):
    config = {
        "AI_ACTIVE_PROFILE": profile,
        "ANTHROPIC_BASE_URL": "https://primary.invalid",
        "ANTHROPIC_API_KEY": "primary-fixture",
        "AI_BACKUP_PLATFORM": "anthropic",
        "AI_BACKUP_MODEL": "fixture-model",
        "AI_BACKUP_BASE_URL": "https://backup.invalid",
        "AI_BACKUP_API_KEY": "backup-fixture",
    }
    requests = []

    def respond(request):
        requests.append(request.url.host)
        if len(requests) == 1:
            return httpx.Response(307, headers={
                "Location": location, "x-should-retry": "true",
            })
        return httpx.Response(400)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda self, req: respond(req))
    with pytest.raises(ModelError, match="模型调用失败"):
        configured_model(config)(system="", user="合成申请")
    assert requests == [f"{profile}.invalid"]


def test_primary_follows_same_origin_redirect(monkeypatch):
    urls = []

    def respond(request):
        urls.append(str(request.url))
        if len(urls) == 1:
            return httpx.Response(307, headers={"Location": "/redirected"})
        return httpx.Response(400)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda self, req: respond(req))
    with pytest.raises(ModelError, match="模型调用失败"):
        configured_model({
            "ANTHROPIC_API_KEY": "fixture", "ANTHROPIC_BASE_URL": "https://primary.invalid",
        })(system="", user="合成申请")
    assert urls == ["https://primary.invalid/v1/messages", "https://primary.invalid/redirected"]


def test_primary_rejects_ambient_endpoint_before_send(monkeypatch, tmp_path, capsys):
    settings = tmp_path / "primary.conf"
    settings.write_text("ANTHROPIC_API_KEY=primary-fixture\n", encoding="utf-8")
    output = tmp_path / "draft.json"
    companies = tmp_path / "companies.tsv"
    companies.write_text("", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://ambient.invalid")
    monkeypatch.setattr(sys, "argv", ["parse", "--env-file", str(settings), "--output", str(output),
                                     "--companies-file", str(companies)])
    monkeypatch.setattr(sys, "stdin", io.StringIO("合成申请"))
    send = Mock(return_value=httpx.Response(400))
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    assert main() == 1
    assert "配置" in capsys.readouterr().err
    send.assert_not_called()
    assert not output.exists()


def test_backup_rejects_plain_http_before_send(monkeypatch):
    send = Mock(return_value=httpx.Response(400))
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    with pytest.raises(ConfigError, match="HTTPS"):
        configured_model({
            "AI_ACTIVE_PROFILE": "backup", "AI_BACKUP_PLATFORM": "anthropic",
            "AI_BACKUP_API_KEY": "fixture", "AI_BACKUP_MODEL": "fixture-model",
            "AI_BACKUP_BASE_URL": "http://backup.invalid",
        })(system="", user="合成申请")
    send.assert_not_called()


def _endpoint_config(profile):
    return {
        "AI_ACTIVE_PROFILE": profile, "ANTHROPIC_API_KEY": "primary-fixture",
        "AI_BACKUP_PLATFORM": "anthropic", "AI_BACKUP_MODEL": "fixture-model",
        "AI_BACKUP_BASE_URL": "https://backup.invalid", "AI_BACKUP_API_KEY": "backup-fixture",
    }


@pytest.mark.parametrize("endpoint", ["http://model.invalid", "", "/relative", "https:///path", "https://[bad"])
def test_primary_requires_valid_https_endpoint(endpoint, monkeypatch):
    from qianshu.ai import ADAPTERS

    dispatch = Mock()
    monkeypatch.setitem(ADAPTERS, "anthropic", dispatch)
    with pytest.raises(ConfigError, match="HTTPS"):
        configured_model({**_endpoint_config("primary"), "ANTHROPIC_BASE_URL": endpoint})
    dispatch.assert_not_called()


@pytest.mark.parametrize("profile", ["primary", "backup"])
@pytest.mark.parametrize("userinfo", ["fixture-user:fixture-token", "fixture-user", ":fixture-token", ""])
def test_endpoint_userinfo_rejected_before_send(monkeypatch, profile, userinfo):
    send = Mock(return_value=httpx.Response(400))
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    name = "ANTHROPIC_BASE_URL" if profile == "primary" else "AI_BACKUP_BASE_URL"
    with pytest.raises(ConfigError, match="HTTPS") as error:
        configured_model({**_endpoint_config(profile), name: f"https://{userinfo}@model.invalid"})
    assert "fixture" not in str(error.value)
    send.assert_not_called()


@pytest.mark.parametrize(("source", "location"), [
    ("http://model.invalid", "/again"), ("https://model.invalid", "http://model.invalid/again"),
])
def test_redirect_requires_https_at_both_ends(source, location):
    from qianshu.ai.anthropic import _guard_redirect

    responses = [httpx.Response(307, headers={"location": location, "x-should-retry": "true"}), httpx.Response(200)]
    send = Mock(side_effect=responses)
    with httpx.Client(transport=httpx.MockTransport(send), follow_redirects=True,
                      event_hooks={"response": [_guard_redirect]}) as client:
        response = client.get(source)
    send.assert_called_once()
    assert response.status_code == 307
    assert "location" not in response.headers
    assert response.headers["x-should-retry"] == "false"


@pytest.fixture
def transport_requests(monkeypatch):
    requests = []

    def respond(transport, request):
        requests.append(request)
        events = [
            {"type": "message_start", "message": {"id": "msg_fixture", "type": "message", "role": "assistant",
             "model": "fixture", "content": [], "stop_reason": None, "stop_sequence": None,
             "usage": {"input_tokens": 1, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {
                "type": "tool_use", "id": "toolu_fixture", "name": "submit_application_reply", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {
                "type": "input_json_delta", "partial_json": serialized_empty()}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1}},
            {"type": "message_stop"},
        ]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events))

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
    return requests


@pytest.mark.parametrize("profile", ["primary", "backup"])
@pytest.mark.parametrize("simulate_fallback", [False, True], ids=["locked-sdk", "token-fallback"])
def test_model_uses_only_selected_credentials(profile, simulate_fallback, monkeypatch, transport_requests):
    for name in ("API_KEY", "AUTH_TOKEN", "WEBHOOK_SIGNING_KEY"):
        monkeypatch.setenv(f"ANTHROPIC_{name}", f"ambient-{name}")
    constructor = anthropic.Anthropic
    signing_keys = []

    def create(**kwargs):
        if simulate_fallback and kwargs.get("auth_token") is None:
            kwargs["auth_token"] = "ambient-AUTH_TOKEN"
        client = constructor(**kwargs)
        signing_keys.append(client.webhook_key)
        return client

    monkeypatch.setattr(anthropic, "Anthropic", create)
    assert configured_model(_endpoint_config(profile))(system="", user="") == serialized_empty()
    request, = transport_requests
    assert request.headers.get_list("x-api-key") == [f"{profile}-fixture"]
    assert "Authorization" not in request.headers
    assert all(b"ambient-" not in value for _, value in request.headers.raw)
    assert b"ambient-" not in request.content
    assert signing_keys[0] != "ambient-WEBHOOK_SIGNING_KEY"


@pytest.mark.parametrize("profile", ["primary", "backup"])
def test_environment_cannot_supply_missing_key(profile, monkeypatch, transport_requests):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key")
    config = _endpoint_config(profile)
    del config["ANTHROPIC_API_KEY" if profile == "primary" else "AI_BACKUP_API_KEY"]
    with pytest.raises(ConfigError, match="API_KEY"):
        configured_model(config)
    assert not transport_requests


@pytest.mark.parametrize("scheme", ["http", "ftp"])
def test_client_ignores_credentialed_environment_proxy(scheme, monkeypatch, transport_requests):
    # 检查 SDK 创建的每个传输；实际发送由内存存根接管。
    initialize = httpx.HTTPTransport.__init__
    proxies = []

    def check_proxy(transport, *args, **kwargs):
        if kwargs.get("proxy") is not None:
            proxies.append(True)
        initialize(transport, *args, **kwargs)

    monkeypatch.setattr(httpx.HTTPTransport, "__init__", check_proxy)
    for name in ("https_proxy", "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY", "REQUEST_METHOD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", f"{scheme}://fixture-user:fixture-token@proxy.invalid")
    assert configured_model(_endpoint_config("primary"))(system="", user="") == serialized_empty()
    assert not proxies
    assert len(transport_requests) == 1


def test_client_construction_error_is_sanitized(monkeypatch, transport_requests):
    monkeypatch.setattr(anthropic, "DefaultHttpxClient", Mock(side_effect=ValueError("fixture-user")))
    with pytest.raises(ModelError, match="^申请解析模型调用失败。$") as error:
        configured_model(_endpoint_config("primary"))(system="", user="")
    assert error.value.__suppress_context__
    assert not transport_requests


def _request_bytes(request):
    url = request.url
    parts = [request.method.encode(), url.raw_scheme, url.raw_host,
             str(url.port).encode() if url.port is not None else None, url.raw_path]
    for key, value in request.headers.raw:
        parts.extend((key, value))
    parts.append(request.content)
    return b"".join(b"-1:" if part is None else str(len(part)).encode() + b":" + part for part in parts)


def test_default_request_matches_baseline_bytes(monkeypatch, transport_requests, tmp_path):
    # 两份请求共用解释器和 SDK，保留平台相关的原始请求头。
    for name in ("BASE_URL", "CUSTOM_HEADERS", "AUTH_TOKEN"):
        monkeypatch.delenv(f"ANTHROPIC_{name}", raising=False)
    assert configured_model({"ANTHROPIC_API_KEY": "fixture"})(system="合成规则", user="合成正文") == serialized_empty()
    actual, = transport_requests
    (tmp_path / "default-request.bin").write_bytes(_request_bytes(actual))

    with anthropic.Anthropic(api_key="fixture", http_client=anthropic.DefaultHttpxClient()) as baseline:
        with baseline.messages.create(
            model="claude-sonnet-5-5", system="合成规则", messages=[{"role": "user", "content": "合成正文"}],
            max_tokens=128000, output_config={"effort": "medium"},
            tools=[{"name": "submit_application_reply", "description": "提交一份申请解析声明。",
                    "input_schema": ApplicationReply.model_json_schema()}],
            stream=True,
        ) as stream:
            assert [event.delta.partial_json for event in stream if event.type == "content_block_delta"] == [
                serialized_empty(),
            ]
    _, reference = transport_requests
    assert _request_bytes(actual) == _request_bytes(reference)
