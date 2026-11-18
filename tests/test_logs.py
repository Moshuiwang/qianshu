import io
import json
import logging
import sys
from hashlib import sha256
from uuid import uuid4

import pytest

from qianshu.config import CONFIG_KEYS, LOGGABLE_CONFIG_KEYS, Config
from qianshu.logs import JsonFormatter, JsonStreamHandler


# 独立列出应脱敏项，避免测试与实现同时跳过被误放行的配置。
EXPECTED_REDACTED_CONFIG_KEYS = (
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_CHAT_ID",
    "FEISHU_WIKI_CHECK_NODE",
    "MINGDAO_ACCOUNT",
    "MINGDAO_PASSWORD",
    "MINGDAO_BASE_URL",
    "MINGDAO_APP_ID",
    "MINGDAO_PROJECT_ID",
    "MINGDAO_WORKSHEET_ID",
    "MINGDAO_VIEW_ID",
    "MINGDAO_SUBMIT_BTN_ID",
    "MAIL_HOST",
    "MAIL_USER",
    "MAIL_PASSWORD",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
)
EXPECTED_LOGGABLE_CONFIG_VALUES = {
    "MAIL_PORT": "993",
    "MAIL_FOLDER": "合成文件夹",
    "ANTHROPIC_MODEL": "合成模型",
}


def assert_redacted(output, *sensitive_values):
    def assert_fields_redacted(field):
        if isinstance(field, str):
            assert all(value not in field for value in sensitive_values), (
                "日志泄露了合成测试数据"
            )
        elif isinstance(field, dict):
            for key, value in field.items():
                assert_fields_redacted(key)
                assert_fields_redacted(value)
        elif isinstance(field, list):
            for value in field:
                assert_fields_redacted(value)

    assert_fields_redacted(json.loads(output))


def emit_test_message(formatter, synthetic_secret, synthetic_mail):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    logger = logging.Logger("qianshu.tests")
    logger.addHandler(handler)
    try:
        raise ValueError(synthetic_mail)
    except ValueError:
        logger.exception(
            "处理返回错误，密钥=%s，正文=%s", synthetic_secret, synthetic_mail,
            extra={"redact_values": (synthetic_mail,), "mail_body": synthetic_mail},
        )
    return stream.getvalue()


def test_json_log_redacts_config_secret_and_mail_text():
    synthetic_secret = f"占位密钥-{uuid4().hex}"
    synthetic_mail = f'合成正文 "{uuid4().hex}"\n下一行'
    output = emit_test_message(JsonFormatter([synthetic_secret]), synthetic_secret, synthetic_mail)

    assert_redacted(output, synthetic_secret, synthetic_mail)
    event = json.loads(output)
    assert event["message"] == "处理返回错误，密钥=[已脱敏]，正文=[已脱敏]"
    assert event["exception_type"] == "ValueError"
    assert "mail_body" not in event
    assert output.count("\n") == 1


def test_redaction_assertion_fails_when_secret_reaches_output():
    synthetic_secret = f"占位密钥-{uuid4().hex}"
    output = emit_test_message(JsonFormatter(), synthetic_secret, "合成正文")

    with pytest.raises(AssertionError, match="日志泄露了合成测试数据"):
        assert_redacted(output, synthetic_secret)


def test_redaction_assertion_rejects_nested_mail_body():
    synthetic_mail = f'合成正文 "{uuid4().hex}"\n下一行'
    output = json.dumps({"message": "测试事件", "details": [{"renamed_body": synthetic_mail}]})

    with pytest.raises(AssertionError, match="日志泄露了合成测试数据"):
        assert_redacted(output, synthetic_mail)


def test_json_log_records_only_exception_type():
    synthetic_error = f'合成异常 "{uuid4().hex}"\n下一行'
    try:
        raise ValueError(synthetic_error)
    except ValueError as exc:
        record = logging.LogRecord(
            "qianshu.tests", logging.ERROR, "", 0, "处理失败", (),
            (type(exc), exc, exc.__traceback__),
        )

    output = JsonFormatter().format(record)

    assert_redacted(output, synthetic_error)
    event = json.loads(output)
    assert event["exception_type"] == "ValueError"
    assert event["message"] == "处理失败"
    assert set(event) == {"timestamp", "level", "logger", "message", "exception_type"}


@pytest.mark.parametrize(
    ("message", "argument", "expected"),
    [
        ("密钥=%r", lambda value: value, "密钥='[已脱敏]'"),
        ("配置=%s", lambda value: {"token": value}, "配置={'token': '[已脱敏]'}"),
        ("配置=%s", lambda value: json.dumps({"token": value}), '配置={"token": "[已脱敏]"}'),
        (
            "配置=%s",
            lambda value: json.dumps({"token": value}, ensure_ascii=False),
            '配置={"token": "[已脱敏]"}',
        ),
    ],
    ids=["repr", "dict", "json-ascii", "json-unicode"],
)
def test_json_log_redacts_escaped_values(message, argument, expected):
    fingerprint = uuid4().hex
    synthetic_secret = f"合成密钥 \"'{fingerprint}\"\\\n下一行"
    record = logging.LogRecord(
        "qianshu.tests", logging.INFO, "", 0, message, (argument(synthetic_secret),), None
    )

    output = JsonFormatter([synthetic_secret]).format(record)

    assert_redacted(output, fingerprint)
    assert json.loads(output)["message"] == expected


@pytest.mark.parametrize(
    ("message", "args", "expected"),
    [
        ("密码=%.21s", lambda secret: (secret,), "密码=[已脱敏]"),
        (
            "配置=%(details)a",
            lambda secret: ({"details": [(secret,)]},),
            f"配置=[({ascii('[已脱敏]')},)]",
        ),
        (
            "配置=%r",
            lambda secret: (json.dumps({"token": secret}),),
            """配置='{"token": "[已脱敏]"}'""",
        ),
        ("异常=%s", lambda secret: (ValueError(secret[:21]),), "异常=[已脱敏]"),
    ],
    ids=["truncated", "ascii-nested-mapping", "repr-of-json", "exception-prefix"],
)
def test_json_log_redacts_arguments_before_rendering(message, args, expected):
    fingerprint = sha256(uuid4().bytes).hexdigest()[:12]
    secret = f"fake-{fingerprint}-é\\\n-end"
    record = logging.LogRecord(
        "qianshu.tests", logging.INFO, "", 0, message, args(secret), None
    )

    output = JsonFormatter([secret]).format(record)

    assert_redacted(output, fingerprint)
    assert json.loads(output)["message"] == expected


def test_json_log_handles_rendering_error_with_standard_stream_handler(capsys, monkeypatch):
    fingerprint = sha256(uuid4().bytes).hexdigest()[:12]
    secret = f"占位-{fingerprint}-合成密码"
    logger = logging.Logger("qianshu.tests")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter([secret]))
    logger.addHandler(handler)
    monkeypatch.setattr(logging, "raiseExceptions", True)

    logger.info("配置=%d", secret)

    stdout, stderr = capsys.readouterr()
    assert fingerprint not in stdout and fingerprint not in stderr, "日志泄露了合成密码"
    assert stderr == ""
    assert json.loads(stdout) == {"message": "log_format_failed", "exception_type": "TypeError"}


def test_json_stream_handler_suppresses_write_error_diagnostics(capsys, monkeypatch):
    class FailingStream(io.StringIO):
        def write(self, text):
            raise OSError("合成写入错误")

    fingerprint = sha256(uuid4().bytes).hexdigest()[:12]
    secret = f"占位-{fingerprint}-合成密码"
    logger = logging.Logger("qianshu.tests")
    logger.addHandler(JsonStreamHandler(FailingStream(), sensitive_values=[secret]))
    monkeypatch.setattr(logging, "raiseExceptions", True)

    logger.info("配置=%s", secret)

    assert capsys.readouterr() == ("", "")


def test_json_log_redacts_rendered_message_without_modifying_record():
    secret = f"占位-{uuid4().hex}"
    args = {"details": [secret]}
    record = logging.LogRecord(
        "qianshu.tests", logging.INFO, "", 0, f"密钥={secret}，配置=%(details)s", (args,), None
    )

    output = JsonFormatter([secret]).format(record)

    assert_redacted(output, secret)
    assert json.loads(output)["message"] == "密钥=[已脱敏]，配置=['[已脱敏]']"
    assert record.args is args
    assert args["details"] == [secret]


def test_all_config_keys_are_classified_for_logging():
    redacted_keys = set(EXPECTED_REDACTED_CONFIG_KEYS)
    loggable_keys = EXPECTED_LOGGABLE_CONFIG_VALUES.keys()

    assert redacted_keys.isdisjoint(loggable_keys)
    assert redacted_keys | loggable_keys == set(CONFIG_KEYS)
    assert LOGGABLE_CONFIG_KEYS == loggable_keys


@pytest.mark.parametrize("key", (*EXPECTED_REDACTED_CONFIG_KEYS, "UNCLASSIFIED_TEST_KEY"))
def test_json_log_redacts_config_value(key):
    synthetic_value = f"占位-{uuid4().hex}"
    formatter = JsonFormatter(Config({key: synthetic_value}))
    record = logging.LogRecord(
        "qianshu.tests", logging.INFO, "", 0, "配置=%s", (synthetic_value,), None
    )

    output = formatter.format(record)

    assert_redacted(output, synthetic_value)
    assert json.loads(output)["message"] == "配置=[已脱敏]"


def test_json_log_preserves_nonsecret_config_values():
    formatter = JsonFormatter(EXPECTED_LOGGABLE_CONFIG_VALUES)
    record = logging.LogRecord(
        "qianshu.tests", logging.INFO, "", 0, "处理 %s 封，文件夹=%s，模型=%s",
        tuple(EXPECTED_LOGGABLE_CONFIG_VALUES.values()), None,
    )

    assert json.loads(formatter.format(record))["message"] == (
        "处理 993 封，文件夹=合成文件夹，模型=合成模型"
    )


def test_json_log_redacts_named_argument_from_config():
    fingerprint = sha256(uuid4().bytes).hexdigest()[:12]
    config = Config({"MAIL_PASSWORD": f"占位-{fingerprint}-合成密码"})
    record = logging.LogRecord(
        "qianshu.tests", logging.INFO, "", 0, "配置=%(MAIL_PASSWORD).21s", (config,), None
    )

    output = JsonFormatter(config).format(record)

    assert_redacted(output, fingerprint)
    assert json.loads(output)["message"] == "配置=[已脱敏]"


def test_json_log_treats_single_extra_string_as_one_value():
    record = logging.LogRecord(
        "qianshu.tests", logging.INFO, "", 0, "合成事件，正文=%s", ("合成正文",), None
    )
    record.redact_values = "合成正文"

    output = JsonFormatter().format(record)

    assert_redacted(output, "合成正文")
    assert json.loads(output)["message"] == "合成事件，正文=[已脱敏]"


def test_json_log_uses_utc_for_record_timestamp():
    record = logging.LogRecord("qianshu.tests", logging.INFO, "", 0, "测试事件", (), None)
    record.created = 946684800.125

    assert json.loads(JsonFormatter().format(record)) == {
        "timestamp": "2000-01-01T00:00:00.125Z",
        "level": "INFO",
        "logger": "qianshu.tests",
        "message": "测试事件",
    }
