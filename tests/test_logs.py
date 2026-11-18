import io
import json
import logging
from uuid import uuid4

import pytest

from qianshu.logs import JsonFormatter


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


def test_json_log_preserves_nonsecret_config_values():
    synthetic_secret = f"占位密钥-{uuid4().hex}"
    formatter = JsonFormatter(
        {"MAIL_PORT": "993", "MAIL_FOLDER": "合成文件夹", "MAIL_PASSWORD": synthetic_secret}
    )
    record = logging.LogRecord(
        "qianshu.tests", logging.INFO, "", 0, "处理 993 封，合成文件夹，密码=%s",
        (synthetic_secret,), None,
    )

    output = formatter.format(record)

    assert_redacted(output, synthetic_secret)
    assert json.loads(output)["message"] == "处理 993 封，合成文件夹，密码=[已脱敏]"


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
