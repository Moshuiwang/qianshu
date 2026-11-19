"""真实命令入口的离线验收。"""

import json
import os
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing
from dataclasses import fields
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from lark_oapi.core.http import Transport as SDKTransport

import qianshu.__main__ as service_entry
from qianshu import parse, validate
from qianshu.__main__ import _EntranceTransport, _processor, _send_report_card
from qianshu.draft import DraftField, PackageRow, PolicyDraft, SourceExcerpt
from qianshu.feishu import Message, Window
from qianshu.feishu.cards import CardLine, detail_pages, result_card
from qianshu.feishu.entrance import Application, fingerprint
from qianshu.feishu.sdk import _Transport
from qianshu.pipeline import Pipeline, _policy_boundaries, _suspected_policy_count

ROOT = Path(__file__).resolve().parents[1]
PARSE_FIXTURES = ROOT / "tests" / "parse" / "fixtures"
COMPANIES_FIXTURE = PARSE_FIXTURES / "companies.tsv"
SECRET = "合成占位凭据-仅用于测试"

_CHILD_SETUP = """
import os
import sys

def prohibit_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
        os.write(2, (event + "\\n").encode())
        os._exit(91)

sys.addaudithook(prohibit_network)
"""


def _write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def single_policy():
    body = (
        "赞比亚占位申请。\n"
        "以下各条均适用：申请开始日期 2099-01-10，申请结束日期 2099-02-15。\n"
        "① 占位甲包用户\n"
        "政策类型：定向政策。充30天送30天，充一送一。用户标签：占位标签甲。\n"
        "用户范围：占位休眠用户。不限制升降包。\n"
        "本批存档文档：https://archive.example.invalid/batch"
    )
    reply, *_ = json.loads((PARSE_FIXTURES / "model-reply.json").read_text(encoding="utf-8"))
    for name in ("policy_name", "trigger_once", "repeat_bonus", "effective_multiplier", "approval_attachments"):
        del reply[name]
    reply.update(
        company={"value": None, "evidence": ["赞比亚占位申请。"]},
        policy_type={"value": "定向政策", "evidence": ["政策类型：定向政策"]},
        packages={
            "value": [{"current_package": "占位甲", "gift_package": "占位甲_copy"}],
            "evidence": ["占位甲包用户"],
        },
    )
    return body, [reply]


def _run(tmp_path, body, model_reply, *, tag_dates=None, approved_body=None):
    now = datetime.now(UTC)
    placeholder = {name: f"<合成占位-{name}>" for name in ("app", "bot", "chat", "sender", "message")}
    mention = "@_user_1"
    message = {
        "message_id": placeholder["message"],
        "chat_id": placeholder["chat"],
        "sender_id": placeholder["sender"],
        "created_ms": int(now.timestamp() * 1000),
        "kind": "text",
        "content": json.dumps({"text": f"{mention} {body}"}),
        "bot_mention_keys": [mention],
    }
    injection = _write_json(tmp_path / "inject.json", {
        "message": message,
        "model_reply": model_reply if isinstance(model_reply, str) else json.dumps(model_reply, ensure_ascii=False),
    })
    config = {
        "FEISHU_APP_ID": placeholder["app"], "FEISHU_APP_SECRET": SECRET,
        "FEISHU_CHAT_ID": placeholder["chat"], "FEISHU_BOT_OPEN_ID": placeholder["bot"],
        "FEISHU_ALLOWED_SENDER_IDS": placeholder["sender"],
        "FEISHU_APPROVED_FINGERPRINT": fingerprint(body if approved_body is None else approved_body),
    }
    dotenv = tmp_path / ".env"
    dotenv.write_text("".join(f"{key}={value}\n" for key, value in config.items()), encoding="utf-8")
    (tmp_path / "sitecustomize.py").write_text(_CHILD_SETUP, encoding="utf-8")
    cmd = [
        sys.executable, "-m", "qianshu", "--inject", str(injection), "--env-file", str(dotenv),
        "--window-start", (now - timedelta(minutes=1)).isoformat(),
        "--window-end", (now + timedelta(minutes=1)).isoformat(),
        "--companies-file", str(COMPANIES_FIXTURE),
    ]
    if tag_dates is not None:
        dates_file = _write_json(tmp_path / "tag-dates.json", tag_dates)
        cmd.extend(("--tag-dates", str(dates_file)))
    run = subprocess.run(
        cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=45,
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(tmp_path), str(ROOT / "src")))},
    )
    assert run.returncode == 0, run.stderr
    assert SECRET not in run.stdout + run.stderr
    return [json.loads(line) for line in run.stdout.splitlines()]


def _contents(cards):
    return [
        item["text"]["content"] if item["tag"] == "div" else item["content"]
        for card in cards
        for item in card["elements"]
    ]


def _resident_args(tmp_path):
    now = datetime.now(UTC)
    return [
        "--state", str(tmp_path / "state.sqlite"),
        "--window-start", (now - timedelta(minutes=1)).isoformat(),
        "--window-end", (now + timedelta(minutes=1)).isoformat(),
    ]


@pytest.mark.parametrize("companies_type", ["default-missing", "explicit-missing", "directory", "unreadable"])
def test_resident_rejects_missing_or_unreadable_company_file(tmp_path, monkeypatch, capsys, companies_type):
    monkeypatch.chdir(tmp_path)
    args = _resident_args(tmp_path)
    if companies_type != "default-missing":
        path = tmp_path / "占位对照表.tsv"
        args.extend(("--companies-file", str(path)))
        if companies_type == "directory":
            path.mkdir()
        elif companies_type == "unreadable":
            path.write_text("占位内容", encoding="utf-8")

            def unreadable(*args, **kwargs):
                raise PermissionError("占位底层错误")

            monkeypatch.setattr(Path, "read_text", unreadable)
    monkeypatch.setattr(service_entry, "load_config", lambda **_: pytest.fail("对照表检查应先于服务配置读取"))

    assert service_entry.main(args) == 1
    assert capsys.readouterr() == ("", "公司对照表文件缺失或不可读，请检查 --companies-file。\n")


@pytest.mark.parametrize("directory_type", ["missing", "file", "public"])
def test_resident_rejects_invalid_raw_reply_directory(tmp_path, monkeypatch, capsys, directory_type):
    path = tmp_path / "占位回复目录"
    if directory_type == "file":
        path.touch()
    elif directory_type == "public":
        path.mkdir(mode=0o755)
        path.chmod(0o755)
    monkeypatch.setattr(service_entry, "load_config", lambda **_: pytest.fail("目录检查应先于服务配置读取"))
    args = [
        *_resident_args(tmp_path), "--companies-file", str(COMPANIES_FIXTURE),
        "--raw-output-dir", str(path), "--env-file", str(tmp_path / ".env"),
    ]

    assert service_entry.main(args) == 1
    assert capsys.readouterr() == ("", "原始回复目录须为可访问的 0700 目录，请检查 --raw-output-dir。\n")


@pytest.mark.parametrize("save_raw", [False, True], ids=["disabled", "enabled"])
def test_resident_uses_company_path_and_only_saves_raw_when_enabled(
    tmp_path, single_policy, monkeypatch, capsys, caplog, save_raw,
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rules").symlink_to(ROOT / "rules", target_is_directory=True)
    body, model = single_policy
    marker = "占位原始回复标记"
    replies = [
        f'  {json.dumps(model, ensure_ascii=False)}\r\n',
        f"\r\n{marker}\r\n",
    ]
    args = [*_resident_args(tmp_path), "--env-file", str(tmp_path / ".env")]
    if save_raw:
        companies_file = tmp_path / "占位对照表.tsv"
        args.extend(("--companies-file", str(companies_file)))
    else:
        companies_file = tmp_path / ".private" / "companies.tsv"
        companies_file.parent.mkdir()
    companies_file.write_text("赞比亚\t占位常驻公司\n", encoding="utf-8")
    output = tmp_path / "占位回复目录"
    output.mkdir(mode=0o700)
    if save_raw:
        args.extend(("--raw-output-dir", str(output)))

    settings = SimpleNamespace(
        app_id="<占位应用>", tenant_key="<占位租户>", chat_id="<占位群>",
        bot_open_id="<占位机器人>", allowed_senders=("<占位申请人>",), approved_fingerprint=fingerprint(body),
    )
    cards = []
    pending = iter(replies)

    def fake_service(settings, window, state_path, processor):
        def run():
            for index in range(len(replies)):
                message = Message(f"<占位消息-{index}>", settings.chat_id, "<占位申请人>", 0, "text", "")
                processor(Application(message, body, ()))

        return SimpleNamespace(run=run, entrance=SimpleNamespace(), transport=SimpleNamespace(
            react=None, reply=None, download=None,
        ))

    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setattr(service_entry, "load_settings", lambda _: settings)
    monkeypatch.setattr(service_entry, "setup_logging", lambda _: None)
    monkeypatch.setattr(service_entry, "configured_model", lambda _: lambda **kwargs: next(pending))
    monkeypatch.setattr(service_entry, "FeishuService", fake_service)
    monkeypatch.setattr(service_entry, "_send_report_card", lambda _, card, *args: cards.append(card))
    monkeypatch.setattr(service_entry.time, "time_ns", lambda: 1_900_000_000_000_000_000)

    assert service_entry.main(args) == 0
    contents = "\n".join(_contents(cards))
    assert "公司\n值　占位常驻公司" in contents
    assert "解析不完整，需人工确认" in contents
    captured = capsys.readouterr()
    assert marker not in contents + captured.out + captured.err + caplog.text
    files = sorted(output.iterdir())
    if save_raw:
        assert len(files) == len(replies)
        for index, (file, reply) in enumerate(zip(files, replies, strict=True)):
            assert file.name == f"{1_900_000_000_000_000_000 + index}-{fingerprint(body)}.txt"
            assert stat.S_IMODE(file.stat().st_mode) == 0o600
            assert file.read_bytes() == reply.encode("utf-8")
    else:
        assert not files


def test_command_reports_all_field_evidence_and_waits_for_card_approval(tmp_path, single_policy):
    body, model = single_policy
    scope = f"占位休眠用户 {SECRET}"
    body = body.replace("占位休眠用户", scope)
    model[0]["user_scope"] = {"value": scope, "evidence": [f"用户范围：{scope}"]}
    cards = _run(
        tmp_path, body, model,
        tag_dates={"占位标签甲": "2099-02-15"},
    )
    content = _contents(cards)
    fields = [row for row in content if "\n值　" in row]
    assert len(fields) == 18
    for name, raw in model[0].items():
        if name == "company":
            continue
        for excerpt in raw["evidence"]:
            assert any(f"「{excerpt.replace(SECRET, '[已脱敏]')}」" in row for row in fields)
    assert any("是否需要维护互斥组\n值　是\n原文出处　无原文摘录" in row for row in fields)
    assert "政策 1 · 公司\n值　占位公司甲\n原文出处　「 赞比亚占位申请。」" in content
    assert '政策 1 · 节目包\n值　[{"当前节目包": "占位甲", "赠送节目包": "占位甲_copy"}]\n原文出处　「占位甲包用户」' in content
    for hint in (
        "互斥组按默认填是", "是否只触发一次按默认填否", "多买多送按默认填是", "生效倍数按默认填 1",
        "审批件按默认留空", "政策类型按用户标签判定",
    ):
        assert f"政策 1 · 自动调整\n{hint}" in content
    assert "政策 1 · 处理状态\n解析完成，待群卡片同意" in content


def test_command_marks_mutex_no_red_and_shows_candidate(tmp_path, single_policy):
    body, model = single_policy
    body += "\n是否需要维护互斥组：否"
    cards = _run(
        tmp_path, body, model,
        tag_dates={"占位标签甲": "2099-02-15"},
    )
    content = "\n".join(_contents(cards))
    assert "<font color='red'>政策 1 · 提醒项\n申请要求不维护互斥组，需人工确认</font>" in content
    assert "是否需要维护互斥组 · 候选 1\n值　否\n原文出处　「是否需要维护互斥组：否」" in content
    assert "需追问 · 是否需要维护互斥组" in content
    assert "停在校验，需人工确认或请申请人补充。" in content


def test_command_blocks_uncovered_heading_when_drafts_outnumber_segments(tmp_path, single_policy):
    body, model = single_policy
    cards = _run(tmp_path, body, model * 2)
    content = "\n".join(_contents(cards))
    assert "<font color='red'>处理状态\n解析不完整，需人工确认</font>" in content
    assert "未确认覆盖的段落序号\n1" in content
    assert "未校验" in content


@pytest.mark.parametrize("missing_scope", [False, True])
def test_command_defers_missing_approval_url_but_still_checks_other_fields(tmp_path, single_policy, missing_scope):
    body, model = single_policy
    body = body.rsplit("\n", 1)[0]
    del model[0]["approval_url"]
    if missing_scope:
        del model[0]["user_scope"]
    cards = _run(tmp_path, body, model, tag_dates={"占位标签甲": "2099-02-15"})
    content = _contents(cards)
    assert "政策 1 · 任务审批链接\n建单时填写" in content
    assert "政策 1 · 审批件附件\n值　[]\n原文出处　无原文摘录" in content
    assert not any("需追问 · 任务审批链接" in line for line in content)
    if missing_scope:
        assert "政策 1 · 处理状态\n停在校验，需人工确认或请申请人补充。" in content
        assert any("需追问 · 用户范围" in line for line in content)
    else:
        assert "政策 1 · 处理状态\n解析完成，待群卡片同意" in content


@pytest.mark.parametrize(("missing", "numbered"), [
    pytest.param(None, True, id="None"),
    pytest.param(3, True, id="3"),
    pytest.param(4, True, id="4"),
    pytest.param(4, False, id="company-heading"),
])
def test_command_checks_coverage_before_validation(tmp_path, single_policy, missing, numbered):
    body, model = single_policy
    intro, local = body.split("①", 1)
    policies = []
    segments = []
    tag_dates = {}
    for index, (number, suffix) in enumerate(zip("①②③④⑤⑥", "甲乙丙丁戊己", strict=True), 1):
        translation = str.maketrans({"①": number, "甲": suffix})
        segment = ("①" + local) if numbered else "公司：占位公司甲\n" + local.partition("\n")[2]
        segment = segment.translate(translation)
        if index > 1:
            segment = segment.split("\n本批存档文档", 1)[0]
        segments.append(segment.strip())
        if index != missing:
            reply = json.dumps(model[0], ensure_ascii=False)
            if not numbered:
                reply = reply.replace('"赞比亚占位申请。"', '"公司：占位公司甲"')
            policies.append(json.loads(reply.translate(translation)))
        tag_dates[f"占位标签{suffix}"] = "2099-02-15"
    body = intro + "\n".join(segments)
    assert _suspected_policy_count(body) == (6 if numbered else 0)
    cards = _run(tmp_path, body, policies, tag_dates=tag_dates)
    content = "\n".join(_contents(cards))
    assert "段落边界未确认" not in content
    if missing is None:
        assert "解析完成，共 6 条政策。" in content
        assert content.count("解析完成，待群卡片同意") == 6
    else:
        status = "解析不完整，需人工确认"
        if numbered:
            status += "\n原文疑似 6 条，解析出 5 条"
        assert f"<font color='red'>处理状态\n{status}</font>" in content
        assert f"未确认覆盖的段落序号\n{missing}" in content
        assert "未校验" in content
        assert "待群卡片同意" not in content


@pytest.mark.parametrize(("paragraph", "explicit"), [
    ("另申请一条独立政策，面向占位用户。", True),
    ("另外一条独立政策，面向占位用户。", True),
    ("第二条政策，面向占位用户。", True),
    ("再申请一条独立政策，面向占位用户。", True),
    ("此外还有面向占位用户的政策待核对。", False),
    ("新增一条政策：适用占位活跃用户，充60天送10天。", True),
    ("我们另申请一条独立政策，面向占位用户。", True),
    ("\n面向占位用户开放充一天送一天。", False),
])
def test_command_checks_untitled_paragraph_signals(tmp_path, single_policy, paragraph, explicit):
    body, model = single_policy
    cards = _run(tmp_path, body.rstrip() + "\n" + paragraph, model,
                 tag_dates={"占位标签甲": "2099-02-15"})
    content = "\n".join(_contents(cards))
    if explicit:
        assert "<font color='red'>处理状态\n解析不完整，需人工确认</font>" in content
        assert "未确认覆盖的段落序号\n2" in content
        assert "未校验" in content
    else:
        assert "处理状态\n解析完成，共 1 条政策。" in content
        assert "政策 1 · 处理状态\n解析完成，待群卡片同意" in content
        assert f"提醒项\n段落 2：{paragraph.strip()[:12]}…，段落边界未确认，请审批人核对" in content
        assert "<font color='red'>" not in content


@pytest.mark.parametrize(("paragraph", "numbered"), [
    pytest.param("  第二条 占位公司乙，买一赠一，用户标签：占位标签乙。", False, id="ordinal"),
    pytest.param("  2. 另申请一条政策：占位公司乙，买一赠一，用户标签：占位标签乙。", False, id="numbered-explicit"),
    pytest.param("  另申请一条占位公司乙政策：买一赠一，用户标签：占位标签乙。", False, id="untitled-explicit"),
    pytest.param("　　② 占位公司乙，买一赠一，用户标签：占位标签乙。", True, id="fullwidth-spaces"),
])
def test_command_stops_when_an_indented_line_starts_another_policy(tmp_path, single_policy, paragraph, numbered):
    body, model = single_policy
    body = body.rstrip() + "\n" + paragraph
    assert "标签名称" not in body
    content = "\n".join(_contents(_run(
        tmp_path, body, model,
        tag_dates={"占位标签甲": "2099-02-15"},
    )))
    status = "解析不完整，需人工确认"
    if numbered:
        status += "\n原文疑似 2 条，解析出 1 条"
    assert f"<font color='red'>处理状态\n{status}</font>" in content
    assert "未确认覆盖的段落序号\n2" in content
    assert "未校验" in content
    assert "待群卡片同意" not in content


def test_command_stops_when_a_heading_continues_with_another_policy(tmp_path, single_policy):
    body, model = single_policy
    body = body.replace(
        "① 占位甲包用户", "① 占位甲包用户，充30天送30天，我们另申请一条独立政策。", 1,
    ).replace("充30天送30天，充一送一", "充一送一")
    model = [{name: model[0][name] for name in ("company", "recharge_days", "gift_days")}]
    content = "\n".join(_contents(_run(
        tmp_path, body, model,
        tag_dates={"占位标签甲": "2099-02-15"},
    )))
    assert "<font color='red'>处理状态\n解析不完整，需人工确认</font>" in content
    assert "未确认覆盖的段落序号\n2" in content
    assert "未校验" in content


def test_command_rejects_missing_draft_when_numbered_boundary_is_uncertain(tmp_path, single_policy):
    body, model = single_policy
    body += "\n② 面向占位用户"
    drafts = parse.parse_application(
        body, model=lambda **_: json.dumps(model, ensure_ascii=False), companies_file=COMPANIES_FIXTURE,
    )
    (draft,) = drafts
    assert all(
        (field := getattr(draft, item.name)).value is None and field.uncertainty is not None
        for item in fields(draft)
    )

    rows = _contents(_run(tmp_path, body, model))
    assert (
        "<font color='red'>处理状态\n解析不完整，需人工确认\n原文疑似 2 条，解析出 1 条</font>"
    ) in rows
    assert "未确认覆盖的段落序号\n2" in rows
    assert "后续处理\n请核对段落与草稿对应关系。未校验。" in rows
    assert all(not row.startswith("政策 ") for row in rows)


@pytest.mark.parametrize("parsed_count", [6, 5])
def test_command_checks_suspected_count_with_indented_subitems(tmp_path, parsed_count):
    body = (PARSE_FIXTURES / "application.txt").read_text(encoding="utf-8")
    model_reply = (PARSE_FIXTURES / "model-reply.json").read_text(encoding="utf-8")
    _, shared_fields, *policy_lines = body.splitlines()
    table_headers = ("合成表头一：标签名称", "合成表头二：优惠类型", "合成表头三：政策类型")
    numbered = {number: f"{index}." for index, number in enumerate("①②③④⑤⑥", 1)}
    lines = [*table_headers, "", shared_fields]
    lines.extend(f"{numbered[line[:1]]}{line[1:]}" if line[:1] in numbered else line for line in policy_lines)
    for suffix in "庚辛壬":
        lines.append(f"      1. 占位子项公司{suffix}：占位")
    body = "\n".join(lines)
    starts, required, uncertain, _ = _policy_boundaries(body, companies_file=COMPANIES_FIXTURE)
    assert len(starts) == 10
    assert list(required) == list(range(2, 8))
    assert uncertain == {starts[index] for index in (0, 7, 8, 9)}
    assert _suspected_policy_count(body) == 6
    for number, replacement in numbered.items():
        model_reply = model_reply.replace(number, replacement)
    replies = json.loads(model_reply)
    rows = _contents(_run(
        tmp_path, body, replies[:parsed_count],
        tag_dates={"占位标签甲": "2099-02-15"},
    ))
    reminders = next(row for row in rows if row.startswith("提醒项\n"))
    header_preview = " ".join("\n".join(table_headers)[:12].splitlines()).strip()
    previews = [header_preview, *(f"1. 占位子项公司{suffix}：占" for suffix in "庚辛壬")]
    assert reminders.splitlines()[1:] == [
        f"段落 {index}：{preview}…，段落边界未确认，请审批人核对"
        for index, preview in zip((1, 8, 9, 10), previews, strict=True)
    ]
    assert all("合成表头" not in row and "占位子项公司" not in row for row in rows if row != reminders)
    content = "\n".join(rows)
    if parsed_count == 6:
        assert "处理状态\n解析完成，共 6 条政策。" in rows
        for index in range(1, 7):
            assert f"政策 {index} · 公司\n值　未确定\n" in content
            field_rows = [
                row for row in rows if row.startswith(f"政策 {index} · ")
                and "\n原文出处　" in row and " · 候选 " not in row.partition("\n")[0]
            ]
            assert len(field_rows) == len(fields(PolicyDraft))
        assert "政策 1 · 处理状态\n停在校验，需人工确认或请申请人补充。" in content
    else:
        assert "<font color='red'>处理状态\n解析不完整，需人工确认" in content
        assert "原文疑似 6 条，解析出 5 条</font>" in content
        assert "未校验" in content


def test_suspected_count_uses_only_unindented_numbered_lines():
    body = "1.\n2、\n3)\n一、\n政策1\n政策一\n① 占位\n⑳ 占位\n      1.\n\t①\n　⑳\n　 ②\n　\t③\n"
    assert _suspected_policy_count(body) == 9


def test_command_accepts_existing_parse_fixture(tmp_path):
    body = (PARSE_FIXTURES / "application.txt").read_text(encoding="utf-8")
    replies = json.loads((PARSE_FIXTURES / "model-reply.json").read_text(encoding="utf-8"))
    content = _contents(_run(tmp_path, body, replies))
    assert "处理状态\n解析完成，共 6 条政策。" in content


@pytest.mark.parametrize(("count_evidence", "suspected"), [
    ("标签名称：占位；标签名称：占位", 2),
    ("标签：占位\n标签: 占位", 2),
    ("1. 开始日期：占位\n2) 结束日期：占位", 3),
])
def test_command_checks_count_independently_of_policy_boundaries(tmp_path, single_policy, count_evidence, suspected):
    body, model = single_policy
    content = "\n".join(_contents(_run(tmp_path, body.rstrip() + "\n" + count_evidence, model)))
    assert (
        "<font color='red'>处理状态\n解析不完整，需人工确认\n"
        f"原文疑似 {suspected} 条，解析出 1 条</font>"
    ) in content
    assert "未校验" in content


def test_pipeline_turns_confirmed_mutex_no_into_red_uncertainty(monkeypatch):
    body = "公司：占位公司甲。是否需要维护互斥组：否。"
    evidence = SourceExcerpt(text="是否需要维护互斥组：否", start=body.index("是否"))
    draft = PolicyDraft(
        policy_name=DraftField(value="占位政策"),
        maintain_mutual_exclusion_group=DraftField(value="否", evidence=[evidence]),
        company=DraftField(value="占位公司甲", evidence=[SourceExcerpt(text="占位公司甲", start=3)]),
        policy_type=DraftField(value="通用政策"),
        start_date=DraftField(value="2099-01-10"),
        end_date=DraftField(value="2099-02-15"),
        benefit_type=DraftField(value="赠送时长"),
        trigger_once=DraftField(value="否"),
        cancel_on_downgrade=DraftField(value="否"),
        recharge_days=DraftField(value=30),
        gift_days=DraftField(value=30),
        repeat_bonus=DraftField(value="否"),
        effective_multiplier=DraftField(value=1),
        user_tags=DraftField(value=[]),
        packages=DraftField(value=[PackageRow(current_package="占位节目包")]),
        user_scope=DraftField(value="合成用户范围"),
        approval_url=DraftField(value="https://archive.example.invalid/batch"),
        approval_attachments=DraftField(value=[]),
    )
    monkeypatch.setattr(parse, "parse_application", lambda *args, **kwargs: [draft])
    original_validate = validate.validate_batch

    def check_mutex(policies, *, today):
        [(checked, _)] = policies
        mutex = checked.maintain_mutual_exclusion_group
        assert mutex.value is None
        assert [candidate.value for candidate in mutex.uncertainty.candidates] == ["否"]
        result = original_validate(policies, today=today)
        assert [error.field for error in result[0][1].errors] == ["maintain_mutual_exclusion_group"]
        return result

    monkeypatch.setattr(validate, "validate_batch", check_mutex)
    pipeline = Pipeline(
        model=lambda **kwargs: pytest.fail("直接注入草稿，不调用模型"),
        today=lambda: date(2099, 1, 1), tag_dates={}, companies_file=COMPANIES_FIXTURE,
    )
    pages = pipeline(_application(body))
    content = "\n".join(_contents(list(map(result_card, pages))))
    assert "校验执行异常" not in content
    assert "<font color='red'>政策 1 · 提醒项\n申请要求不维护互斥组，需人工确认</font>" in content
    assert "需追问 · 是否需要维护互斥组" in content
    assert "停在校验，需人工确认或请申请人补充。" in content


def _application(body):
    message = Message("<合成消息>", "<合成群>", "<合成发送者>", 0, "text", "")
    return Application(message, body, ())


@pytest.mark.parametrize("scenario", ["retry", "fail", "expired", "notice_failed"])
def test_processor_retries_page_in_order_with_same_sdk_uuid(tmp_path, scenario):
    now = datetime.now(UTC)
    window = Window(now - timedelta(seconds=1), now + timedelta(seconds=1))
    current = now.timestamp()
    second_attempts = []
    requests = []
    received = {}
    response = SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="<合成发送回执>"))

    def check_window():
        assert window.contains(current)

    async def tenant_token():
        return "<合成令牌>"

    def create(request, option):
        nonlocal current
        assert option.tenant_access_token == "<合成令牌>"
        body = request.request_body
        assert body.receive_id == "<合成群>"
        assert body.msg_type == "interactive"
        requests.append(body.uuid)
        card = json.loads(body.content)
        title = card["header"]["title"]["content"]
        if title.endswith("第 2 / 3 张"):
            second_attempts.append((body.uuid, body.content))
            if scenario == "expired":
                current = window.end.timestamp()
                raise TimeoutError
            if scenario in {"fail", "notice_failed"}:
                raise TimeoutError
            if len(second_attempts) == 1:
                # 模拟服务端已收、但客户端丢失回执。服务端按 UUID 去重。
                received.setdefault(body.uuid, card)
                raise TimeoutError
        if scenario == "notice_failed" and "汇报发送曾在" in " ".join(_contents([card])):
            raise TimeoutError
        received.setdefault(body.uuid, card)
        return response

    transport = SimpleNamespace(
        settings=SimpleNamespace(chat_id="<合成群>"), _require_window=check_window,
        _tenant_token=tenant_token,
        im=SimpleNamespace(message=SimpleNamespace(create=create)),
    )
    pages = detail_pages("合成申请", [CardLine("字段", "合成明细")] * 90)
    state = tmp_path / "reports.sqlite"
    application = _application("合成申请")

    def send(card, request_id, notice, check_attempt):
        _send_report_card(transport, card, request_id, check_attempt)

    process = _processor(lambda application: pages, send, window, state, clock=lambda: current)
    process(application)
    group_cards = list(received.values())
    if scenario != "expired":
        assert len(second_attempts) == 2
        assert second_attempts[0] == second_attempts[1]
    else:
        assert len(second_attempts) == 1
    if scenario == "retry":
        assert [card["header"]["title"]["content"] for card in group_cards] == [
            f"申请处理结果 · 第 {number} / 3 张" for number in range(1, 4)
        ]
    else:
        assert group_cards[0]["header"]["title"]["content"] == "申请处理结果 · 第 1 / 3 张"
        expected = ["汇报发送曾在第 2 / 3 张暂停。若未收齐，请人工核对。"] if scenario == "fail" else []
        assert _contents(group_cards[1:]) == expected
    with closing(sqlite3.connect(state)) as db:
        assert db.execute("""
            SELECT digest, total_pages, sent_pages, stop_notice_failures, notice_confirmed, manual_page,
                   pages IS NOT NULL, notice IS NOT NULL FROM reports
        """).fetchall() == [(
            sha256(application.message.message_id.encode()).hexdigest(),
            3, 3 if scenario == "retry" else 1, int(scenario == "notice_failed"),
            int(scenario == "fail"), 2 if scenario == "expired" else None,
            int(scenario in {"fail", "notice_failed"}), int(scenario in {"fail", "notice_failed"}),
        )]
    assert state.stat().st_mode & 0o777 == 0o600
    assert len(set(requests)) == (2 if scenario == "expired" else 3)
    assert len(requests) == (2 if scenario == "expired" else 4)

    if scenario == "expired":
        process.resume()
        assert len(requests) == 2
        with pytest.raises(AssertionError):
            _send_report_card(transport, {}, "<合成请求>", lambda: None)
        assert len(requests) == 2


@pytest.mark.parametrize("accepted_before_exit", [False, True])
@pytest.mark.parametrize("elapsed", [timedelta(seconds=1), timedelta(minutes=56)])
def test_history_recovers_report_after_process_exit(tmp_path, accepted_before_exit, elapsed):
    now = datetime(2099, 1, 1, tzinfo=UTC)
    window = Window(now - timedelta(seconds=1), now + timedelta(hours=2))
    state = tmp_path / "reports.sqlite"
    group = tmp_path / "group.jsonl"
    pages = detail_pages("原版合成申请", [CardLine("字段", "合成明细")] * 90)
    child = subprocess.run([
        sys.executable, "-c", """
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from qianshu.__main__ import _processor
from qianshu.feishu import Message, Window
from qianshu.feishu.cards import CardLine, detail_pages
from qianshu.feishu.entrance import Application

state, group, start, end, now, accepted_before_exit = sys.argv[1:]
window = Window(datetime.fromisoformat(start), datetime.fromisoformat(end))
pages = detail_pages("原版合成申请", [CardLine("字段", "合成明细")] * 90)

def send(card, request_id, notice, check_attempt):
    assert not notice
    second = card["header"]["title"]["content"].endswith("第 2 / 3 张")
    if not second or accepted_before_exit == "True":
        with Path(group).open("a", encoding="utf-8") as stream:
            print(json.dumps([request_id, card], ensure_ascii=False), file=stream)
    if second:
        os._exit(23)

message = Message("<合成消息>", "<合成群>", "<合成发送者>", 0, "text", "")
processor = _processor(lambda application: pages, send, window, Path(state), clock=lambda: float(now))
processor(Application(message, "合成申请", ()))
""", str(state), str(group), window.start.isoformat(), window.end.isoformat(),
        str(now.timestamp()), str(accepted_before_exit),
    ], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert child.returncode == 23, child.stderr
    received = dict(json.loads(line) for line in group.read_text(encoding="utf-8").splitlines())
    assert len(received) == (2 if accepted_before_exit else 1)
    with closing(sqlite3.connect(state)) as db:
        sent_pages, saved, first_attempt = db.execute("""
            SELECT sent_pages, pages, page_first_attempt FROM reports
        """).fetchone()
    assert sent_pages == 1
    assert first_attempt == now.timestamp()
    snapshot = json.loads(saved)
    attempted = []
    stale = elapsed > timedelta(minutes=55)

    def send(card, request_id, notice, check_attempt):
        assert not notice
        attempted.append([request_id, card])
        received.setdefault(request_id, card)

    def history(start, end):
        assert list(received.values()) == list(map(result_card, pages[:len(received)]))
        return ()

    restarted = _processor(
        lambda application: pytest.fail("续发不重新解析"), send, window, state,
        clock=lambda: (now + elapsed).timestamp(),
    )
    transport = SimpleNamespace(react=None, reply=None, download=None, history=history)
    entrance_transport = _EntranceTransport(transport, restarted.resume)
    for _ in range(2):
        assert list(entrance_transport.history(window.start.timestamp(), now.timestamp())) == []
    restarted(_application("合成重扫申请"))
    assert attempted == ([] if stale else snapshot[1:])
    with closing(sqlite3.connect(state)) as db:
        assert db.execute("SELECT sent_pages, pages, manual_page FROM reports").fetchone() == (
            (1, None, 2) if stale else (3, None, None)
        )


def test_recovery_reuses_unconfirmed_notice_and_records_its_ack(tmp_path):
    now = datetime(2099, 1, 1, tzinfo=UTC)
    window = Window(now - timedelta(seconds=1), now + timedelta(seconds=10))
    state = tmp_path / "reports.sqlite"
    pages = detail_pages("合成申请", [CardLine("字段", "合成明细")] * 50)
    clock = now.timestamp
    notices = []

    def send(card, request_id, notice, check_attempt):
        if notice:
            with closing(sqlite3.connect(state)) as db:
                assert db.execute("SELECT notice_first_attempt FROM reports").fetchone() == (clock(),)
            notices.append((request_id, card))
            if len(notices) > 1:
                return
        if card != result_card(pages[0]):
            raise TimeoutError

    processor = _processor(lambda application: pages, send, window, state, clock=clock)
    processor(_application("合成申请"))
    restarted = _processor(lambda application: (), send, window, state, clock=clock)
    restarted.resume()
    restarted.resume()
    assert len(notices) == 2
    assert notices[0] == notices[1]
    with closing(sqlite3.connect(state)) as db:
        assert db.execute("SELECT sent_pages, notice_confirmed, stop_notice_failures FROM reports").fetchone() == (1, 1, 0)


def test_recovery_stops_when_only_the_interruption_notice_is_stale(tmp_path):
    now = datetime(2099, 1, 1, tzinfo=UTC)
    window = Window(now - timedelta(seconds=1), now + timedelta(hours=2))
    current = now.timestamp()
    state = tmp_path / "reports.sqlite"
    pages = detail_pages("合成申请", [CardLine("字段", "合成明细")] * 50)
    attempts = []

    def send(card, request_id, notice, check_attempt):
        attempts.append((request_id, notice))
        if notice or current == now.timestamp() or card != result_card(pages[0]):
            raise TimeoutError

    processor = _processor(lambda application: pages, send, window, state, clock=lambda: current)
    processor(_application("合成申请"))
    current += 54 * 60
    processor.resume()
    with closing(sqlite3.connect(state)) as db:
        assert db.execute("""
            SELECT sent_pages, page_first_attempt, notice_first_attempt, notice_confirmed FROM reports
        """).fetchone() == (1, current, now.timestamp(), 0)

    attempts.clear()
    current += 2 * 60
    restarted = _processor(lambda application: (), send, window, state, clock=lambda: current)
    restarted.resume()
    assert attempts == []
    with closing(sqlite3.connect(state)) as db:
        assert db.execute("SELECT sent_pages, manual_page, pages, notice FROM reports").fetchone() == (1, 2, None, None)


def test_processor_stops_if_tenant_token_arrives_after_window_closes(tmp_path, monkeypatch):
    now = datetime(2099, 1, 1, tzinfo=UTC)
    window = Window(now - timedelta(seconds=1), now + timedelta(seconds=10))
    current = now.timestamp()
    token_requests = []
    message_posts = []

    async def fetch_token(config, request):
        nonlocal current
        token_requests.append(request)
        current = window.end.timestamp()
        return SimpleNamespace(status_code=200, content=json.dumps({
            "code": 0, "tenant_access_token": "<合成令牌>", "expire": 7200,
        }).encode())

    def post_message(config, request, option):
        message_posts.append(request)
        pytest.fail("关窗后发出了消息 POST")

    monkeypatch.setattr(SDKTransport, "aexecute", fetch_token)
    monkeypatch.setattr(SDKTransport, "execute", post_message)
    settings = SimpleNamespace(
        app_id="<合成跨窗应用>", app_secret=SECRET, chat_id="<合成群>",
    )
    transport = _Transport(settings, window, lambda: current)

    def send(card, request_id, notice, check_attempt):
        _send_report_card(transport, card, request_id, check_attempt)

    state = tmp_path / "reports.sqlite"
    pages = detail_pages("合成申请", [CardLine("字段", "合成明细")])
    processor = _processor(lambda application: pages, send, window, state, clock=lambda: current)
    processor(_application("合成申请"))
    assert len(token_requests) == 1
    assert message_posts == []
    with closing(sqlite3.connect(state)) as db:
        assert db.execute("""
            SELECT page_first_attempt, sent_pages, manual_page, pages FROM reports
        """).fetchone() == (now.timestamp(), 0, 1, None)


def test_recovery_stops_legacy_pending_report_without_attempt_time(tmp_path):
    now = datetime(2099, 1, 1, tzinfo=UTC)
    window = Window(now - timedelta(seconds=1), now + timedelta(hours=2))
    state = tmp_path / "reports.sqlite"
    pages = detail_pages("合成申请", [CardLine("字段", "合成明细")])

    def no_ack(*args):
        raise TimeoutError

    processor = _processor(lambda application: pages, no_ack, window, state, clock=now.timestamp)
    processor(_application("合成申请"))
    with closing(sqlite3.connect(state)) as db:
        db.execute("ALTER TABLE reports DROP COLUMN page_first_attempt")
        db.execute("ALTER TABLE reports DROP COLUMN notice_first_attempt")

    def unexpected_send(*args):
        pytest.fail("旧版待续发记录的首次尝试时间未知")

    restarted = _processor(lambda application: (), unexpected_send, window, state, clock=now.timestamp)
    restarted.resume()
    with closing(sqlite3.connect(state)) as db:
        assert db.execute("SELECT sent_pages, manual_page, pages, notice FROM reports").fetchone() == (0, 1, None, None)


def test_history_keeps_scanning_if_resume_fails(caplog):
    message = _application("合成申请").message

    def resume():
        raise RuntimeError(SECRET)

    def history(start, end):
        assert (start, end) == (1, 2)
        yield message

    transport = SimpleNamespace(react=None, reply=None, download=None, history=history)
    assert list(_EntranceTransport(transport, resume).history(1, 2)) == [message]
    assert caplog.messages == ["汇报续发失败，继续回扫。"]
    assert SECRET not in caplog.text


def test_command_shows_validation_questions(tmp_path, single_policy):
    body, model = single_policy
    cards = _run(tmp_path, body, model)
    content = "\n".join(_contents(cards))
    assert "需追问 · 结束日期\n标签最晚结束日期尚未确认，请申请人补充。" in content
    assert "停在校验，需人工确认或请申请人补充。" in content


def test_command_reports_parse_failure(tmp_path, single_policy):
    body, _ = single_policy
    cards = _run(tmp_path, body, f"{SECRET} 合成无效回复")
    content = "\n".join(_contents(cards))
    assert "<font color='red'>处理状态\n解析不完整，需人工确认\n停在解析" in content
    assert "未校验" in content
    assert "待群卡片同意" not in content


def test_command_preserves_long_evidence(tmp_path, single_policy):
    body, model = single_policy
    scope = "合成范围" * 3000 + "合成范围结束"
    body = body.replace("占位休眠用户", scope)
    model[0]["user_scope"] = {"value": scope, "evidence": [f"用户范围：{scope}"]}
    cards = _run(
        tmp_path, body, model,
        tag_dates={"占位标签甲": "2099-02-15"},
    )
    assert len(cards) > 2
    assert all(len(json.dumps(card, ensure_ascii=False).encode()) <= 24 * 1024 for card in cards)
    content = _contents(cards)
    assert "政策 1 · 处理状态\n解析完成，待群卡片同意" in content
    scope_chunks = "".join(
        row.split("\n", 1)[1] for row in content if row.startswith("政策 1 · 用户范围")
    )
    assert scope_chunks == f"值　{scope}\n原文出处　「用户范围：{scope}」"


def test_command_injection_still_enforces_approved_fingerprint(tmp_path, single_policy):
    body, model = single_policy
    assert _run(tmp_path, body, model, approved_body="合成的另一条正文") == [{"notice": "概念验证期不处理"}]
