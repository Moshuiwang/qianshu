import pytest

from qianshu.mingdao.readonly import ReadOnlyPolicy


ORIGIN = "https://app.example.invalid"
APP = "/app/example-app"
SHEET = f"{APP}/example-sheet"
VIEW = f"{SHEET}/example_view1"
NEW_RECORD = f"{APP}/newrecord/example-sheet/example_view1"
ROW = f"{VIEW}/row/example-record"


@pytest.mark.parametrize("path", (SHEET, VIEW, NEW_RECORD, f"{NEW_RECORD}/", ROW))
def test_document_path_accepts_supported_shapes(path):
    assert ReadOnlyPolicy(ORIGIN, (path,)).document_paths == (path,)


@pytest.mark.parametrize("path", (
    f"{NEW_RECORD}/extra",
    f"{APP}/newrecord/example-sheet/",  # 缺视图且保留末尾斜杠。
    f"{APP}/add/example-sheet/example_view1",
    f"{APP}/submit/example-sheet/example_view1",
    f"{APP}/newrecord/example.sheet/example_view1",
    f"{APP}/newrecord/example%2Dsheet/example_view1",
    f"{APP}/newrecord//example_view1",
    f"{NEW_RECORD}//",
    f"{ROW}/extra",
    f"{SHEET}/row/example-record",
    f"{VIEW}/row",
    f"{VIEW}/edit/example-record",
    f"{VIEW}/row/example.record",
    f"{VIEW}/row/example%2Drecord",
    f"{SHEET}//row/example-record",
    f"{ROW}/",
))
def test_document_path_rejects_unsupported_shapes(path):
    with pytest.raises(ValueError, match="文档路径须为"):
        ReadOnlyPolicy(ORIGIN, (path,))


@pytest.mark.parametrize("suffix, other_suffix", (("", "/"), ("/", "")))
def test_new_record_requests_require_configured_get_document(suffix, other_suffix):
    path = NEW_RECORD + suffix
    policy = ReadOnlyPolicy(ORIGIN, (path,))

    assert policy.classify("GET", ORIGIN + path, "document")[0]
    assert not policy.classify("POST", ORIGIN + path, "document")[0]
    assert not policy.classify("GET", ORIGIN + path, "xhr")[0]
    assert not policy.classify("GET", ORIGIN + NEW_RECORD + other_suffix, "document")[0]
    assert not policy.classify("GET", ORIGIN + NEW_RECORD + "-other" + suffix, "document")[0]
