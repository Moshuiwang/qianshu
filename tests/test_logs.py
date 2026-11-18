import io
import json
import logging
from uuid import uuid4

import pytest

from qianshu.logs import JsonFormatter


def assert_redacted(output, *sensitive_values):
    message = json.loads(output)["message"]
    assert all(value not in output and value not in message for value in sensitive_values), (
        "日志泄露了合成测试数据"
    )


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


def test_json_log_uses_utc_for_record_timestamp():
    record = logging.LogRecord("qianshu.tests", logging.INFO, "", 0, "测试事件", (), None)
    record.created = 946684800.125

    assert json.loads(JsonFormatter().format(record)) == {
        "timestamp": "2000-01-01T00:00:00.125Z",
        "level": "INFO",
        "logger": "qianshu.tests",
        "message": "测试事件",
    }
