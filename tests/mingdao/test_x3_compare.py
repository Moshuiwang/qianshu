import json
from collections import Counter
from secrets import token_hex
from stat import S_IMODE
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from qianshu.mingdao import x3_compare as x3


@pytest.fixture
def sample(tmp_path, monkeypatch):
    names = {
        "policy_name": (2, "政策名称"),
        "gift_days": (6, "赠送天数"),
        "start_date": (15, "开始日期"),
        "trigger_once": (11, "只触发一次"),
        "user_tags": (29, "用户标签"),
        "packages": (34, "节目包"),
        "approval_attachments": ("unsupported", "审批件附件"),
    }
    ids = {key: str(uuid4()) for key in (*names, "worksheet", "option", "tag_name", "current", "gift")}
    row_ids = [str(uuid4()) for _ in range(6)]
    token, app = f"PLACEHOLDER_TOKEN_{uuid4()}", f"PLACEHOLDER_APP_{uuid4()}"
    fields = {
        ids[key]: {"type": kind, "label": label, "draft": key}
        for key, (kind, label) in names.items()
    }
    fields[ids["trigger_once"]]["status_map"] = {ids["option"]: "否", "占位否": "否", "占位是": "是"}
    fields[ids["user_tags"]]["relations"] = {"name_path": [ids["tag_name"]]}
    fields[ids["packages"]]["relations"] = {
        "current": {"control": ids["current"], "name_path": [0, "name"]},
        "gift": {"control": ids["gift"], "name_path": []},
    }
    config = {
        "official": {"origin": "https://placeholder.invalid",
                     "token_key": "PLACEHOLDER_TOKEN", "app_id_key": "PLACEHOLDER_APP"},
        "worksheet_id": ids["worksheet"],
        "records": [{"rowId": row_id, "draft": index} for index, row_id in enumerate(row_ids, 1)][::-1],
        "fields": fields,
    }
    values = {
        "gift_days": 30, "start_date": "2099-02-04", "trigger_once": "否",
        "user_tags": ["  Placeholder Tag_A ", "Placeholder TAG B"],
        "packages": [
            {"current_package": "Placeholder_A", "gift_package": "PLACEHOLDER_GIFT"},
            {"current_package": "Placeholder B", "gift_package": None},
        ],
        "approval_attachments": ["占位附件"],
    }
    drafts = [{key: {"value": value, "evidence": ["占位证据不进报告"]}
               for key, value in {**values, "policy_name": f"占位政策 {index}"}.items()}
              for index in range(1, 7)]
    base = f"/v3/app/worksheets/{ids['worksheet']}/rows"
    requests = []
    failures = {}

    def respond(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.host == "placeholder.invalid"
        assert request.headers["Authorization"] == f"Bearer {token}"
        assert request.headers["HAP-Appid"] == app
        row_id, *relation = request.url.path.removeprefix(base + "/").split("/")
        index = row_ids.index(row_id) + 1
        if relation:
            assert dict(request.url.params) == {"pageIndex": "1", "pageSize": "50"}
            prefix, control = relation
            assert prefix == "relations"
            if control == ids["user_tags"]:
                rows = [{ids["tag_name"]: "placeholder_tag_b"}, {ids["tag_name"]: "placeholder_tag_a"}]
            else:
                assert control == ids["packages"]
                rows = [
                    {ids["current"]: [{"name": "placeholder_b"}], ids["gift"]: ""},
                    {ids["current"]: '[{"name":"placeholder a"}]', ids["gift"]: "Placeholder Gift"},
                ]
            data = {
                "rows": [{"_id": token_hex(12), "rowId": str(uuid4()), **row} for row in rows],
                "total": len(rows),
            }
        else:
            assert not request.url.params
            data = {
                "_id": token_hex(12), "rowId": row_id.upper(),
                ids["policy_name"]: f" 占位政策 {index} ",
                ids["gift_days"]: "30.00", ids["start_date"]: "2099-02-04T23:30:00-05:00",
                ids["trigger_once"]: [{"key": ids["option"], "value": "占位否"}],
                ids["approval_attachments"]: ["占位附件"],
                "unconfigured": "占位无关字段不进报告",
            }
        override = failures.get((index, relation[-1] if relation else None))
        if override:
            return override(data)
        return httpx.Response(200, json={"success": True, "data": data})

    make_client = httpx.Client
    monkeypatch.setattr(
        x3.httpx, "Client", lambda **kwargs: make_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    config_path, drafts_path, env_path = (tmp_path / name for name in ("config.json", "drafts.json", ".env"))
    output = tmp_path / "report.json"
    for path in (config_path, drafts_path):
        path.touch(mode=0o600)
    env_path.write_text(f"PLACEHOLDER_TOKEN={token}\nPLACEHOLDER_APP={app}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    def run(*, explicit_env=False):
        config_path.write_text(json.dumps(config), encoding="utf-8")
        drafts_path.write_text(json.dumps(drafts), encoding="utf-8")
        argv = ["--config", str(config_path), "--drafts", str(drafts_path), "--output", str(output)]
        if explicit_env:
            argv += ["--env-file", str(env_path)]
        return x3.main(argv)

    return SimpleNamespace(run=run, output=output, markdown=output.with_suffix(".md"),
                           requests=requests, failures=failures, ids=ids, row_ids=row_ids,
                           drafts=drafts, config=config, token=token, app=app)


def _read(sample):
    return json.loads(sample.output.read_text(encoding="utf-8"))


def test_entry_gets_once_and_writes_private_reports(sample, capsys, caplog):
    sample.drafts[0]["policy_name"]["value"] = f"占位 | <值>\n[占位链接](/占位路径) {sample.token}"

    def reflected(data):
        data[sample.ids["policy_name"]] = f"占位人工值 {sample.app}"
        return httpx.Response(200, json={"success": True, "data": data})

    sample.failures[1, None] = reflected
    assert sample.run() == 0
    assert len(sample.requests) == 18
    assert all(count == 1 for count in Counter(str(request.url) for request in sample.requests).values())
    report = _read(sample)
    assert [item["草稿序号"] for item in report["记录"]] == [6, 5, 4, 3, 2, 1]
    assert [field["结论"] for field in report["记录"][0]["字段"]] == ["一致"] * 6 + ["无法比较"]
    name_summary = report["字段汇总"][0]
    assert name_summary["一致率"] == 5 / 6
    assert name_summary["不一致"] == 1
    assert report["字段汇总"][-1]["无法比较"] == 6
    assert report["字段汇总"][-1]["一致率"] == 0
    markdown = sample.markdown.read_text(encoding="utf-8")
    assert markdown.count("| 字段名 | 草稿值 | 人工值 | 结论 |") == 6
    assert "| 字段名 | 一致 |" in markdown
    assert "83.3%" in markdown
    assert "&#124; &lt;值&gt;&#10;" in markdown
    assert "[占位链接](" not in markdown
    for path in (sample.output, sample.markdown):
        assert S_IMODE(path.stat().st_mode) == 0o600
        content = path.read_text(encoding="utf-8")
        assert all(secret not in content for secret in (sample.token, sample.app))
        assert "占位证据不进报告" not in content
        assert "占位无关字段不进报告" not in content
    terminal = capsys.readouterr()
    assert terminal.out == "读取成功 6，读取失败 0\n"
    assert terminal.err == ""
    assert caplog.text == ""


def test_reader_has_no_method_parameter(sample):
    with x3.httpx.Client() as client:
        reader = x3.Reader(client, "https://placeholder.invalid", sample.ids["worksheet"], sample.token, sample.app)
        with pytest.raises(TypeError):
            reader._get(sample.row_ids[0], method="POST")
        with pytest.raises(x3.Stop):
            reader.relations(sample.row_ids[0], "../../占位路径")
    assert sample.requests == []


@pytest.mark.parametrize("bad_data", [
    {"rows": [], "total": 1},
    {"rows": [], "total": False},
    {"rows": [], "total": "0"},
    {"rows": {}, "total": 0},
    {"rows": [None], "total": 1},
    {"rows": [{}], "total": 1},
    {"rows": []},
])
def test_relations_fail_only_the_affected_record(sample, bad_data):
    sample.failures[3, sample.ids["user_tags"]] = lambda _: httpx.Response(
        200, json={"success": True, "data": bad_data},
    )
    assert sample.run(explicit_env=True) == 1
    report = _read(sample)
    failed, = [row for row in report["记录"] if row["读取"] == "读取失败"]
    assert failed["草稿序号"] == 3
    assert failed["字段"] == []
    assert len(sample.requests) == 17
    assert report["读取成功"] == 5
    assert report["字段汇总"][0]["一致率"] == 5 / 6


def test_relations_stop_at_page_limit_with_valid_names(sample):
    def full_page(_):
        rows = [
            {"_id": token_hex(12), "rowId": str(uuid4()), sample.ids["tag_name"]: f"占位标签 {index}"}
            for index in range(50)
        ]
        return httpx.Response(200, json={"success": True, "data": {"rows": rows, "total": 50}})

    sample.failures[3, sample.ids["user_tags"]] = full_page
    assert sample.run() == 1
    failed, = [row for row in _read(sample)["记录"] if row["读取"] == "读取失败"]
    assert failed["草稿序号"] == 3
    assert len(sample.requests) == 17


@pytest.mark.parametrize("failure", ["identity", "http", "redirect", "shape", "control", "json", "overflow", "timeout"])
def test_detail_failure_isolated_and_redirects_not_followed(sample, failure, capsys):
    def fail(data):
        if failure == "timeout":
            raise httpx.ReadTimeout(sample.token)
        if failure == "http":
            return httpx.Response(404, text=sample.token)
        if failure == "redirect":
            return httpx.Response(307, headers={"Location": "https://redirect-placeholder.invalid"})
        if failure == "json":
            return httpx.Response(200, text=sample.token)
        if failure == "overflow":
            return httpx.Response(200, text='{"success":true,"data":1e999}')
        if failure == "identity":
            data["rowId"] = sample.row_ids[0]
        if failure == "shape":
            data = []
        if failure == "control":
            del data[sample.ids["policy_name"]]
        return httpx.Response(200, json={"success": True, "data": data})

    sample.failures[5, None] = fail
    assert sample.run() == 1
    report = _read(sample)
    assert [row["草稿序号"] for row in report["记录"] if row["读取"] == "读取失败"] == [5]
    assert report["读取成功"] == 5
    assert len(sample.requests) == 16
    assert capsys.readouterr().out == "读取成功 5，读取失败 1\n"


def test_empty_relations_are_read_successfully(sample):
    def empty(_):
        return httpx.Response(200, json={"success": True, "data": {"rows": [], "total": 0}})

    sample.failures[6, sample.ids["user_tags"]] = empty
    sample.failures[6, sample.ids["packages"]] = empty
    sample.drafts[5]["user_tags"]["value"] = []
    assert sample.run() == 0
    fields = {item["字段名"]: item for item in _read(sample)["记录"][0]["字段"]}
    assert fields["用户标签"]["结论"] == "一致"
    assert fields["节目包"]["结论"] == "人工为空"


@pytest.mark.parametrize(("kind", "draft", "manual", "verdict"), [
    ("text", "占位\n文本", " 占位\n文本 ", "一致"),
    ("text", "占位文本", "占位 文本", "不一致"),
    ("text", None, "占位文本", "草稿不确定"),
    ("text", "占位文本", "  ", "人工为空"),
    ("text", "占位文本", 2, "无法比较"),
    ("number", 30, "3e1", "一致"),
    ("number", 0, "0.0", "一致"),
    ("number", 0, False, "无法比较"),
    ("number", 2, "2.01", "不一致"),
    ("number", 2, "NaN", "无法比较"),
    ("date", "2099-02-04", "2099-02-04 00:00:00", "一致"),
    ("date", "2099-02-04", "2099-02-30", "无法比较"),
    ("date", "2099-02-04", "2099-02-05T00:00:00+08:00", "不一致"),
    ("single_select", "标准否", "占位否", "一致"),
    ("single_select", "标准否", [{"key": "占位键"}], "一致"),
    ("single_select", "标准否", [{"key": "占位键", "value": "占位是"}], "无法比较"),
    ("single_select", "标准否", '["占位键"]', "一致"),
    ("single_select", "标准否", "[]", "人工为空"),
    ("single_select", "标准否", "未知", "无法比较"),
    ("user_tags", [" X_Y ", "z"], ["Z", "x y"], "一致"),
    ("user_tags", ["X_Y", "X Y"], ["x y"], "不一致"),
    ("packages", [{"current_package": "占位甲", "gift_package": None}],
     [{"current_package": "占位甲", "gift_package": "占位乙"}], "不一致"),
    ("packages", [{"current_package": "占位甲", "gift_package": "占位乙"}],
     [{"current_package": "占位乙", "gift_package": "占位甲"}], "不一致"),
])
def test_comparison_rules(kind, draft, manual, verdict):
    spec = {"type": kind, "status_map": {"占位键": "标准否", "占位否": "标准否", "占位是": "标准是"}}
    assert x3._comparison({"value": draft}, manual, spec) == verdict
    assert x3._comparison({"value": draft, "uncertainty": {"reason": "占位原因"}}, manual, spec) == "草稿不确定"


@pytest.mark.parametrize("existing", ["json", "markdown", "dangling_symlink"])
def test_existing_output_preserved_before_any_request(sample, existing):
    if existing == "dangling_symlink":
        sample.markdown.symlink_to(sample.markdown.with_name("missing"))
    else:
        path = sample.output if existing == "json" else sample.markdown
        path.write_text("占位既有内容", encoding="utf-8")
    assert sample.run() == 1
    assert sample.requests == []
    if existing == "dangling_symlink":
        assert sample.markdown.is_symlink()
    else:
        assert path.read_text(encoding="utf-8") == "占位既有内容"


def test_second_output_created_by_other_writer_is_preserved(sample, monkeypatch):
    link = x3.os.link

    def collide(source, target):
        if target == sample.markdown:
            target.write_text("占位并发内容", encoding="utf-8")
        return link(source, target)

    monkeypatch.setattr(x3.os, "link", collide)
    assert sample.run() == 1
    assert not sample.output.exists()
    assert sample.markdown.read_text(encoding="utf-8") == "占位并发内容"


@pytest.mark.parametrize("invalid", ["duplicate_row", "duplicate_draft", "boolean_index", "wrong_count"])
def test_invalid_record_mapping_is_rejected_without_requests(sample, invalid):
    records = sample.config["records"]
    if invalid == "duplicate_row":
        records[0]["rowId"] = records[1]["rowId"].upper()
    elif invalid == "duplicate_draft":
        records[0]["draft"] = records[1]["draft"]
    elif invalid == "boolean_index":
        records[-1]["draft"] = True
    else:
        records.pop()
    assert sample.run() == 1
    assert sample.requests == []
    assert not sample.output.exists()
