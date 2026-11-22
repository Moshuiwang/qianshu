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
from qianshu.parse.evidence import PolicyStructureIssue
from qianshu.parse.protocol import Span
from qianshu.pipeline import Pipeline
from tests.protocol_fixtures import fixed_reply, policy, quote, span, statement

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
def single_policy(request):
    body = (
        "赞比亚占位申请。\n"
        "以下各条均适用：申请开始日期 2099-01-10，申请结束日期 2099-02-15。\n"
        "① 占位甲包用户\n"
        "政策类型：定向政策。充30天送30天，充一送一。用户标签：占位标签甲。\n"
        "用户范围：占位休眠用户。不限制升降包。\n"
        "本批存档文档：https://archive.example.invalid/batch"
    )
    reply = {
        "company": {"value": "赞比亚", "evidence": [" 赞比亚占位申请。"]},
        "policy_type": {"value": "定向政策", "evidence": ["政策类型：定向政策"]},
        "benefit_type": {"value": "赠送时长", "evidence": ["充一送一"]},
        "start_date": {"value": "2099-01-10", "evidence": ["申请开始日期 2099-01-10"]},
        "end_date": {"value": "2099-02-15", "evidence": ["申请结束日期 2099-02-15"]},
        "user_tags": {"value": ["占位标签甲"], "evidence": ["占位标签甲"]},
        "approval_url": {"value": "https://archive.example.invalid/batch",
                         "evidence": ["本批存档文档：https://archive.example.invalid/batch"]},
        "cancel_on_downgrade": {"value": "否", "evidence": ["不限制升降包"]},
        "recharge_days": {"value": 30, "evidence": ["充30天"]},
        "gift_days": {"value": 30, "evidence": ["送30天"]},
        "packages": {"value": [{"current_package": "占位甲", "gift_package": "占位甲_copy"}],
                     "evidence": ["占位甲包用户"]},
        "user_scope": {"value": "占位休眠用户", "evidence": ["用户范围：占位休眠用户"]},
    }
    if request.node.originalname in (
        "test_resident_uses_company_path_and_only_saves_raw_when_enabled",
        "test_pipeline_reads_one_company_table_per_application",
    ):
        reply["country"] = {"value": "赞比亚", "evidence": ["赞比亚占位申请。"]}
        del reply["company"]
        return body, fixed_reply(body, [reply])
    return body, [reply]


def _run(tmp_path, body, model_reply, *, tag_dates=None, approved_body=None, separator=" "):
    now = datetime.now(UTC)
    placeholder = {name: f"<合成占位-{name}>" for name in ("app", "bot", "chat", "sender", "message")}
    mention = "@_user_1"
    message = {
        "message_id": placeholder["message"],
        "chat_id": placeholder["chat"],
        "sender_id": placeholder["sender"],
        "created_ms": int(now.timestamp() * 1000),
        "kind": "text",
        "content": json.dumps({"text": f"{mention}{separator}{body}"}),
        "bot_mention_keys": [mention],
    }
    if isinstance(model_reply, list):
        rows = []
        for row in model_reply:
            row = dict(row)
            if country := row.pop("company", None):
                row["country"] = country
            rows.append(row)
        model_reply = fixed_reply(separator + body, rows)
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
        args.extend(("--companies-file", str(companies_file), "--legacy-companies"))
        companies_data = "赞比亚\t占位常驻公司\n"
    else:
        companies_file = tmp_path / ".private" / "companies.tsv"
        companies_file.parent.mkdir()
        companies_data = "国家\t关键词\t公司内部编号\t公司显示名\n赞比亚\t\t0001\t占位常驻公司\n"
    companies_file.write_text(companies_data, encoding="utf-8")
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


def test_command_accepts_existing_parse_fixture(tmp_path):
    body = (PARSE_FIXTURES / "application.txt").read_text(encoding="utf-8")
    replies = json.loads((PARSE_FIXTURES / "model-reply.json").read_text(encoding="utf-8"))
    # The transport removes the mention and retains the following space. Supply the
    # exact input seen by the fixed reply instead of shifting its coordinates.
    content = _contents(_run(tmp_path, body, replies, separator=""))
    assert "处理状态\n解析完成，共 6 条政策。" in content


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


def test_pipeline_reads_one_company_table_per_application(tmp_path, monkeypatch, single_policy):
    body, reply = single_policy
    table = tmp_path / "companies.csv"
    header = "国家,关键词,公司内部编号,公司显示名\n"
    table.write_text(header + "赞比亚,,0009,占位换表前公司\n", encoding="utf-8")
    original_read = Path.read_bytes
    reads = []

    def read(path):
        raw = original_read(path)
        if path == table:
            reads.append(path)
            table.write_text(header + "赞比亚,,0010,占位换表后公司\n", encoding="utf-8")
        return raw

    monkeypatch.setattr(Path, "read_bytes", read)
    pipeline = Pipeline(model=lambda **_: json.dumps(reply), tag_dates={}, companies_file=table)
    first = "\n".join(_contents(list(map(result_card, pipeline(_application(body))))))
    assert reads == [table]
    assert "占位换表前公司" in first and "占位换表后公司" not in first and "0009" not in first
    second = "\n".join(_contents(list(map(result_card, pipeline(_application(body))))))
    assert reads == [table, table]
    assert "占位换表后公司" in second and "占位换表前公司" not in second and "0010" not in second


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



@pytest.mark.parametrize("case", ["unlocated_policy_main", "unlocated_policy_continuation", "complete"])
def test_command_reports_full_review_without_downgrading_fields(tmp_path, single_policy, case):
    body, rows = single_policy
    rows[0]["country"] = rows[0].pop("company")
    reply = fixed_reply(" " + body, rows)
    if case == "unlocated_policy_main":
        reply["policies"][0]["main"]["text"] = "合成正文中不存在的主段"
    elif case == "unlocated_policy_continuation":
        reply["policies"][0]["continuations"] = [span(len(body) + 1, len(body) + 2)]
    cards = _run(tmp_path, body, reply, tag_dates={"占位标签甲": "2099-02-15"})
    lines = _contents(cards)
    content = "\n".join(lines)
    assert "政策 1 · 用户范围\n值　占位休眠用户\n原文出处　「用户范围：占位休眠用户」" in lines
    assert "政策 1 · 用户范围 · 待确认" not in content
    assert "政策 1 · 多买多送\n值　是\n原文出处　无原文摘录" in lines
    if case == "complete":
        assert "需人工核对全文" not in content
        assert "解析完成，待群卡片同意" in content
    else:
        assert lines[1].startswith("<font color='red'>处理状态\n需人工核对全文\n停在校验。")
        assert "政策结构问题 1" in content
        assert "未校验" in content
        assert "待群卡片同意" not in content
        assert case in content


def test_command_validates_other_policies_when_a_field_is_uncertain(tmp_path, single_policy):
    body, rows = single_policy
    rows[0]["country"] = rows[0].pop("company")
    reply = fixed_reply(" " + body, rows)
    tail = "\n合成第二条"
    reply["policies"].append(policy("p2", span(len(body) + 1, len(body + tail) + 1), {
        "repeat_bonus": statement("缺证据"),
    }))

    lines = _contents(_run(tmp_path, body + tail, reply, tag_dates={"占位标签甲": "2099-02-15"}))
    content = "\n".join(lines)
    assert "需人工核对全文" not in content
    assert "未校验" not in content
    assert "政策 1 · 处理状态\n解析完成，待群卡片同意" in lines
    assert "政策 2 · 处理状态\n停在校验" in content
    assert "政策 2 · 多买多送 · 待确认" in content
    assert "政策 2 · 是否只触发一次\n值　否" in content


def test_pending_range_preview_redacts_secrets_at_range_edges():
    pipeline = Pipeline(model=lambda **_: pytest.fail("不应调用模型"), tag_dates={}, sensitive_values=[SECRET])
    body = f"甲{SECRET}乙"
    issues = [
        PolicyStructureIssue(Span(source_id="body", start=start, end=end), "overlapping_policy_mains")
        for start, end in [(1, len(body) - 1), (0, 5), (5, len(body))]
    ]

    whole, clipped_end, clipped_start = pipeline._structure_reminders(body, issues, [(1, len(body) - 1)])

    assert "[已脱敏]" in whole.text
    assert SECRET not in whole.text
    assert clipped_end.text == "body [0, 5)\n政策主段相交（overlapping_policy_mains）"
    assert clipped_start.text == f"body [5, {len(body)})\n政策主段相交（overlapping_policy_mains）"


@pytest.mark.parametrize("passages", [None, [], {}, [None, {"text": "坏项"}]])
def test_unused_passages_are_display_only(tmp_path, single_policy, passages):
    body, rows = single_policy
    rows[0]["country"] = rows[0].pop("company")
    reply = fixed_reply(" " + body, rows)
    tag_dates = {"占位标签甲": "2099-02-15"}
    baseline = _contents(_run(tmp_path, body, reply, tag_dates=tag_dates))
    reply["unused_passages"] = passages
    assert _contents(_run(tmp_path, body, reply, tag_dates=tag_dates)) == baseline

    long_text = "合成占位摘录\n" * 1800
    long_reason = "分页理由\n" * 1800
    reply["unused_passages"] = [
        {"text": long_text, "reason": long_reason + "合成理由甲"},
        {"text": {"bad": True}, "reason": "坏项"},
        {"text": "额外键坏项", "reason": "无", "extra": True},
        {"text": "合成段落乙", "reason": "合成理由乙"},
    ]
    lines = _contents(_run(tmp_path, body, reply, tag_dates=tag_dates))
    advisory = "没用上的原文段落（对照下方申请原文）"
    assert lines[2].startswith(advisory + "\n")
    assert any(line.startswith(advisory + "（续）") for line in lines)
    assert [line for line in lines if line.startswith("政策 ")] == [
        line for line in baseline if line.startswith("政策 ")
    ]
    chunks = "".join(line.split("\n", 1)[1] for line in lines if line.startswith(advisory))
    assert chunks == "理由　" + long_reason + "合成理由甲理由　合成理由乙"
    assert "政策 1 · 处理状态\n解析完成，待群卡片同意" in lines


def test_application_source_and_advisories_are_redacted():
    from qianshu.__main__ import _mail_summary

    body = "甲" * 1499 + SECRET + "\n合成原文尾"
    p = policy("p1", span(0, len(body)), {"user_scope": {
        **statement("缺证据"), "explanation": "合成说明 " + SECRET,
    }})
    reply = json.dumps({"policies": [p], "unused_passages": [
        {"text": body, "reason": SECRET},
        {"text": SECRET[:5], "reason": SECRET[-5:]},
    ]})
    pipeline = Pipeline(model=lambda **_: reply, tag_dates={}, sensitive_values=[SECRET])

    pages = pipeline(_application(body))
    lines = _contents(list(map(result_card, pages)))
    content = "\n".join(lines)
    assert SECRET not in content
    assert SECRET[:5] not in content and SECRET[-5:] not in content
    assert "模型说明　合成说明 [已脱敏]" in content
    assert "没用上的原文段落（对照下方申请原文）\n理由　[已脱敏]" in lines
    assert lines[-1].startswith("申请原文（续）\n")
    assert "".join(line.split("\n", 1)[1] for line in lines if line.startswith("申请原文")) == body.replace(SECRET, "[已脱敏]")
    summary = "\n".join(_contents(list(map(result_card, _mail_summary(pages, True)))))
    assert "通过校验 0 条，需人工核对 1 条" in summary
    assert all(text not in summary for text in ("申请原文", "没用上的原文段落", "合成说明", SECRET))


@pytest.mark.parametrize("size", [1, 4])
def test_partial_sensitive_value_is_redacted_before_paginating(size):
    secret = "H2NF5U9J7X"
    suffix = secret[-size:]
    body = "甲" * 1499 + secret + "\n" + "乙" * 8100
    explanation = "乙" * 1477 + f"；只引用末尾{suffix}，需核对。"
    reason = f"摘录含末尾{suffix}，略去。"
    p = policy("p1", span(0, len(body)), {"user_scope": {
        **statement("缺证据", evidence=[quote(body, suffix)]), "explanation": explanation,
    }})
    reply = json.dumps({"policies": [p], "unused_passages": [{"text": suffix, "reason": reason}]})
    pipeline = Pipeline(model=lambda **_: reply, tag_dates={}, sensitive_values=[secret])

    pages = pipeline(_application(body))
    lines = _contents(list(map(result_card, pages)))
    assert len(pages) > 1
    assert "原文出处　「[已脱敏]」" in "\n".join(lines)
    assert f"没用上的原文段落（对照下方申请原文）\n理由　{reason.replace(suffix, '[已脱敏]')}" in lines
    for label, expected in (
        ("申请原文", body.replace(secret, "[已脱敏]")),
        ("政策 1 · 用户范围 · 待确认", "模型说明　" + explanation.replace(suffix, "[已脱敏]")),
    ):
        chunks = [line.split("\n", 1)[1] for line in lines if line.split("\n", 1)[0] in (label, label + "（续）")]
        assert len(chunks) > 1
        assert "".join(chunks).endswith(expected)
    assert suffix not in "\n".join(lines)


@pytest.mark.parametrize("secret_in_body", [False, True])
def test_split_sensitive_value_is_redacted_across_advisories_and_pages(secret_in_body, monkeypatch):
    monkeypatch.setattr("qianshu.feishu.cards._PAGE_ELEMENTS", 3)
    pieces = ("充", "值赠", "送用户", "范围")
    fragment = "".join(pieces)
    secret = f"甲{fragment}乙" if secret_in_body else fragment
    first = "、".join(pieces) + "。\n"
    body = first + (secret if secret_in_body else "合成申请。")
    reply = json.dumps({"policies": [
        policy("p1", span(0, len(first)), {"user_scope": {
            **statement("缺证据"), "explanation": pieces[2],
        }}),
        policy("p2", span(len(first), len(body)), {"country": {
            **statement("缺证据"), "explanation": pieces[3],
        }}),
    ], "unused_passages": [
        {"text": "合成段落甲", "reason": pieces[0]},
        {"text": "丁" * 10000, "reason": pieces[1]},
    ]})
    pipeline = Pipeline(model=lambda **_: reply, tag_dates={}, sensitive_values=[secret])

    displayed = []
    for page in pipeline(_application(body)):
        for line in _contents([result_card(page)]):
            if line.startswith("没用上的原文段落") and "\n理由　" in line:
                displayed.append((page.number, line.split("\n理由　", 1)[1]))
            elif line.startswith("政策 1 · 用户范围 · 待确认\n"):
                displayed.append((page.number, line.split("\n模型说明　", 1)[1]))
            elif line.startswith("政策 2 · 国家 · 模型说明\n"):
                displayed.append((page.number, line.split("\n", 1)[1]))

    assert len(displayed) == len(pieces)
    assert displayed[0][0] < displayed[1][0]
    assert [text for _, text in displayed] == ["[已脱敏]"] * len(pieces)


def test_unused_passage_excerpt_cannot_recombine_with_reason():
    secret = "A12B34"
    body = "样例A12、B34。"
    excerpt = "样例A12"
    reply = json.dumps({"policies": [], "unused_passages": [{"text": excerpt, "reason": "B34"}]})
    pipeline = Pipeline(model=lambda **_: reply, tag_dates={}, sensitive_values=[secret])

    lines = _contents(list(map(result_card, pipeline(_application(body)))))
    free_text = "".join(
        part.removeprefix("理由　")
        for line in lines if line.startswith("没用上的原文段落")
        for part in line.split("\n")[1:]
    )

    assert secret not in free_text
    assert all(secret[start:start + 4] not in free_text for start in range(len(secret) - 3))
    assert all(excerpt not in line for line in lines if not line.startswith("申请原文\n"))
    assert "没用上的原文段落（对照下方申请原文）\n理由　B34" in lines
    assert f"申请原文\n{body}" in lines


def test_advisories_redact_registered_fragments_absent_from_the_source():
    body = "充、值。合成申请。"
    long_secret = "甲合成隔离占位值乙"
    reply = json.dumps({"policies": [policy("p1", span(0, len(body)), {
        "user_scope": {**statement("缺证据"), "explanation": "值"},
        "country": {**statement("缺证据"), "explanation": "需核对隔离占位值。"},
    })], "unused_passages": [
        {"text": "合成段落甲", "reason": "核对隔离占位。"},
        {"text": "合成段落乙", "reason": "充"},
    ]})
    pipeline = Pipeline(model=lambda **_: reply, tag_dates={}, sensitive_values=[long_secret, "充值"])

    lines = _contents(list(map(result_card, pipeline(_application(body)))))

    assert "没用上的原文段落（对照下方申请原文）\n理由　核对[已脱敏]。" in lines
    assert "没用上的原文段落（对照下方申请原文）\n理由　[已脱敏]" in lines
    assert "模型说明　[已脱敏]" in "\n".join(lines)
    assert "政策 1 · 国家 · 模型说明\n需核对[已脱敏]。" in lines


def test_advisories_keep_public_business_words_shared_with_a_sensitive_value():
    secret = "已充值甲赠送乙用户丙"
    body = f"已充值、赠送、用户。\n{secret}"
    reply = json.dumps({"policies": [policy("p1", span(0, len(body)), {
        "user_scope": {**statement("缺证据"), "explanation": "赠送"},
        "country": {**statement("缺证据"), "explanation": "用户"},
    })], "unused_passages": [{"text": "合成段落", "reason": "已充值"}]})
    pipeline = Pipeline(model=lambda **_: reply, tag_dates={}, sensitive_values=[secret])

    lines = _contents(list(map(result_card, pipeline(_application(body)))))

    assert "没用上的原文段落（对照下方申请原文）\n理由　已充值" in lines
    assert "模型说明　赠送" in "\n".join(lines)
    assert "政策 1 · 国家 · 模型说明\n用户" in lines
    assert "申请原文\n已充值、赠送、用户。\n[已脱敏]" in lines


def test_command_shows_country_and_company_keyword_explanations(tmp_path, single_policy):
    body, rows = single_policy
    rows[0]["country"] = rows[0].pop("company")
    reply = fixed_reply(" " + body, rows)
    fields = reply["policies"][0]["fields"]
    for name, explanation in {
        "country": "合成国家证据不足", "company_keyword": "合成关键词证据不足", "user_scope": "合成范围证据不足",
    }.items():
        fields[name] = {**statement("缺证据"), "explanation": explanation}

    lines = _contents(_run(tmp_path, body, reply))
    assert "政策 1 · 国家 · 模型说明\n合成国家证据不足" in lines
    assert "政策 1 · 公司关键词 · 模型说明\n合成关键词证据不足" in lines
    assert any(line.startswith("政策 1 · 用户范围 · 待确认\n") and "模型说明　合成范围证据不足" in line for line in lines)
    assert any(line.startswith("政策 1 · 公司 · 待确认\n") for line in lines)


def test_zero_drafts_keep_advisories_and_original_without_validating(monkeypatch):
    body = "合成原文"
    reply = json.dumps({"policies": [], "unused_passages": [{"text": body, "reason": "合成说明"}]})
    pipeline = Pipeline(model=lambda **_: reply, tag_dates={})
    monkeypatch.setattr("qianshu.validate.validate_batch", lambda *a, **kw: pytest.fail("零草稿不得校验"))
    lines = _contents(list(map(result_card, pipeline(_application(body)))))
    assert lines[1:] == [
        "处理状态\n未提取到可办理的政策，请核对原文",
        "没用上的原文段落（对照下方申请原文）\n理由　合成说明",
        "申请原文\n合成原文",
    ]


@pytest.mark.parametrize("has_title", [True, False])
def test_command_requires_package_confirmation_when_model_reports_no_title(tmp_path, single_policy, has_title):
    body, rows = single_policy
    rows[0]["country"] = rows[0].pop("company")
    reply = fixed_reply(" " + body, rows)
    reply["policies"][0]["has_title"] = has_title
    cards = _run(tmp_path, body, reply, tag_dates={"占位标签甲": "2099-02-15"})
    content = "\n".join(_contents(cards))
    assert ("政策 1 · 节目包 · 待确认" in content) is not has_title
    assert ("缺少本条政策的标题" in content) is not has_title
    assert ("待群卡片同意" in content) is has_title


def test_command_marks_mutex_no_red_and_shows_candidate(tmp_path, single_policy):
    body, rows = single_policy
    evidence = "是否需要维护互斥组：否"
    body += "\n" + evidence
    rows[0]["maintain_mutual_exclusion_group"] = {"value": "否", "evidence": [evidence]}
    content = "\n".join(_contents(_run(tmp_path, body, rows, tag_dates={"占位标签甲": "2099-02-15"})))
    assert "申请要求不维护互斥组，需人工确认" in content
    assert "<font color='red'>政策 1 · 提醒项\n申请要求不维护互斥组，需人工确认</font>" in content
    assert "是否需要维护互斥组 · 候选 1\n值　否\n原文出处　「是否需要维护互斥组：否」" in content
    assert "需追问 · 是否需要维护互斥组" in content
    assert "停在校验，需人工确认或请申请人补充。" in content
    assert "待群卡片同意" not in content


def test_command_blocks_overlapping_policy_anchors(tmp_path, single_policy):
    body, rows = single_policy
    reply = fixed_reply(" " + body, rows)
    first = reply["policies"][0]
    reply["policies"].append({**first, "id": "p2", "main": span(1, 5)})
    content = "\n".join(_contents(_run(tmp_path, body, reply)))
    assert "需人工核对全文" in content
    assert "body [1, 5)\n政策主段相交（overlapping_policy_mains）" in content
    assert "未校验" in content
    assert "待群卡片同意" not in content
    assert "政策 1 · 用户范围\n值　占位休眠用户" in content
