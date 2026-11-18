import imaplib
import json
import logging
import stat
from collections import deque
from datetime import date
from email import policy
from email.message import EmailMessage
from threading import Event
from uuid import uuid4

import pytest

from qianshu.mail import _private_dir, poll_once, run_polling
from qianshu.mail.history import export_history, main
from qianshu.mail.imap import SUMMARY, MailError, ReadOnlyMailbox, connect
from qianshu.mail.message import explicit_approval


PLAIN = b'("TEXT" "PLAIN" NIL NIL NIL "8BIT" 20 1)'
MULTIPART = (
    b'(' + PLAIN + b'("TEXT" "PLAIN" ("NAME" "dummy.txt") NIL NIL "8BIT" 8 1)'
    b'("APPLICATION" "OCTET-STREAM" NIL NIL NIL "BASE64" 8) "MIXED")'
)


class FakeIMAP:
    def __init__(self):
        self.folders = {}
        self.flags = {}
        self.calls = []
        self.debug = 0
        self.folder = None
        self.newly_seen = None

    def add(self, folder, uid, message, *, multipart=False):
        raw_headers, body = message.as_bytes(policy=policy.SMTP).split(b"\r\n\r\n", 1)
        self.folders.setdefault(folder, {})[uid] = {
            "message": message,
            "HEADER": raw_headers + b"\r\n\r\n",
            "TEXT": body,
            "structure": MULTIPART if multipart else PLAIN,
            "1.MIME": b"Content-Type: text/plain; charset=utf-8\r\n\r\n",
            "1": "合成正文".encode(),
            "2.MIME": b'Content-Type: text/plain; name="dummy.txt"\r\n\r\n',
        }
        self.flags.setdefault(folder, set())

    def factory(self, host, port, *, ssl_context, timeout):
        return self

    def login(self, user, password):
        return "OK", [b""]

    def shutdown(self):
        self.calls.append(("SHUTDOWN",))

    def select(self, folder, *, readonly):
        self.calls.append(("EXAMINE" if readonly else "SELECT", folder))
        assert readonly
        self.folder = folder.strip('"')
        self.flags.setdefault(self.folder, set())
        return "OK", [str(len(self.folders.get(self.folder, {}))).encode()]

    def uid(self, command, *args):
        self.calls.append((command, self.folder, *args))
        messages = self.folders.get(self.folder, {})
        if command == "SEARCH":
            criteria = args[0]
            if criteria == "SEEN":
                return "OK", [b" ".join(str(uid).encode() for uid in sorted(self.flags[self.folder]))]
            if criteria == "SUBJECT":
                marker = args[1].strip('"').casefold()
                assert args[2] == "FROM"
                address = args[3].strip('"').casefold()
                messages = {uid: msg for uid, msg in messages.items()
                            if marker in str(msg["message"]["Subject"]).casefold()
                            and address in str(msg["message"]["From"]).casefold()}
            elif criteria == "OR":
                # 模拟服务器宽松匹配，调用端须二次校验回复链。
                assert args[1:3] == ("HEADER", "In-Reply-To")
                assert args[4:6] == ("HEADER", "References")
            else:
                assert criteria == "SINCE" and args[2] == "BEFORE"
            return "OK", [b" ".join(str(uid).encode() for uid in messages)]

        assert command == "FETCH"
        uid, query = args
        msg = messages[int(uid)]
        if query == "(BODYSTRUCTURE)":
            return "OK", [b"1 (UID " + uid.encode() + b" BODYSTRUCTURE " + msg["structure"] + b")"]
        assert query.startswith("(BODY.PEEK[") and query.endswith("])")
        section = query[len("(BODY.PEEK["):-2]
        if section == SUMMARY:
            summary = EmailMessage()
            for field in ("From", "Subject", "Message-ID", "In-Reply-To", "References"):
                if field in msg["message"]:
                    summary[field] = str(msg["message"][field])
            body = summary.as_bytes(policy=policy.SMTP)
        else:
            body = msg[section]
        if self.newly_seen == (self.folder, int(uid), section):
            self.flags[self.folder].add(int(uid))
        prefix = f"1 (UID {uid} BODY[{section}] {{{len(body)}}}".encode()
        return "OK", [(prefix, body), b")"]

    def fetched(self, folder, uid, section):
        return ("FETCH", folder, str(uid), f"(BODY.PEEK[{section}])") in self.calls


@pytest.fixture
def config(tmp_path):
    # 账号、域和标记均在本次测试生成，通过配置交给被测入口。
    domain = f"{uuid4().hex}.invalid"
    return {
        "MAIL_HOST": domain,
        "MAIL_PORT": "993",
        "MAIL_USER": f"{uuid4().hex}@{domain}",
        "MAIL_PASSWORD": uuid4().hex,
        "MAIL_FOLDER": "INBOX",
        "MAIL_SENT_FOLDER": "Sent",
        "MAIL_APPROVER": f"{uuid4().hex}@{domain}",
        "MAIL_TEST_MARKER": uuid4().hex,
        "MAIL_PRIVATE_DIR": str(tmp_path),
        "MAIL_HISTORY_SUBJECT": "合成主题",
    }


def mail(address, subject, body, *, reply_to=None, references=None):
    message = EmailMessage()
    message["From"] = address
    message["Subject"] = subject
    message["Message-ID"] = f"<{uuid4().hex}@{address.split('@')[1]}>"
    if reply_to:
        message["In-Reply-To"] = reply_to
    if references:
        message["References"] = references
    message.set_content(body)
    return message


def test_private_dir_allows_unrelated_git_ancestor(tmp_path):
    (tmp_path / ".git").mkdir()
    private_dir = tmp_path / "nested" / "mail-data"

    assert _private_dir({"MAIL_PRIVATE_DIR": str(private_dir)}) == private_dir.resolve()
    assert private_dir.is_dir()


def test_mutating_commands_are_rejected_before_transport():
    fake = FakeIMAP()
    mailbox = ReadOnlyMailbox(fake)
    with pytest.raises(MailError):
        mailbox.select("INBOX", readonly=False)
    with pytest.raises(MailError):
        mailbox._read_uid("STORE", "1", "+FLAGS", r"(\Seen)")
    with pytest.raises(MailError):
        mailbox._read_uid("FETCH", "1", "(BODY[TEXT])")
    with pytest.raises(MailError):
        mailbox._read_uid("FETCH", "1", "(BODY.PEEK[TEXT] BODY[])")
    with pytest.raises(MailError):
        mailbox._read_uid("FETCH", "1\r\n", "(BODY.PEEK[TEXT])")
    with pytest.raises(MailError):
        mailbox._read_uid("SEARCH", "ALL\r\nA001 UID STORE 1 +FLAGS (\\Seen)")
    assert fake.calls == []


def test_imaplib_wire_uses_examine_and_peek(config):
    commands = []

    class WireIMAP(imaplib.IMAP4):
        def open(self, host, port, timeout=None):
            self.responses = deque([b"* OK ready\r\n"])

        def readline(self):
            return self.responses.popleft()

        def read(self, size):
            data = self.responses.popleft()
            assert len(data) == size
            return data

        def send(self, data):
            tag, command = data.removesuffix(b"\r\n").split(b" ", 1)
            commands.append(command)
            if command == b"CAPABILITY":
                self.responses.append(b"* CAPABILITY IMAP4rev1\r\n")
            elif command.startswith(b"EXAMINE "):
                self.responses.append(b"* 1 EXISTS\r\n")
            elif command == b"UID FETCH 1 (BODY.PEEK[TEXT])":
                self.responses.extend([b"* 1 FETCH (BODY[TEXT] {3}\r\n", b"abc", b" UID 1)\r\n"])
            self.responses.append(tag + b" OK done\r\n")

        def shutdown(self):
            pass

    def factory(host, port, *, ssl_context, timeout):
        return WireIMAP(host, port, timeout)

    with connect(config, factory) as mailbox:
        assert mailbox.select("已发送") == 1
        assert mailbox.section(1, "TEXT") == b"abc"
    assert [command.split(b" ", 1)[0] for command in commands] == [
        b"CAPABILITY", b"LOGIN", b"EXAMINE", b"UID",
    ]
    assert commands[2].isascii() and b"&" in commands[2]


def test_poll_only_delivers_marked_request_once_and_refreshes_approval(config, caplog):
    inbox, sent = config["MAIL_FOLDER"], config["MAIL_SENT_FOLDER"]
    approver, marker = config["MAIL_APPROVER"], config["MAIL_TEST_MARKER"]
    fake = FakeIMAP()
    request = mail(approver, f"合成主题 {marker}", "合成正文")
    fake.add(inbox, 1, request, multipart=True)
    # IMAP FROM 是子串搜索，使用相似地址覆盖客户端的精确校验。
    fake.add(inbox, 2, mail("x" + approver, marker, "无关正文"))
    fake.add(inbox, 3, mail(approver, "无标记", "无关正文"))
    fake.add(sent, 7, mail(approver, marker, "同意", references="<unrelated>"))
    fake.flags[inbox].add(2)
    fake.flags[sent].add(7)
    calls = []

    def process(*args):
        calls.append(args)

    with caplog.at_level(logging.INFO):
        first = poll_once(config, process, factory=fake.factory)
        fake.add(sent, 8, mail(approver, "关联回复", "同意。\n\n> 合成正文",
                               reply_to=str(request["Message-ID"])))
        # 再次从配置打开私有台账，覆盖进程重启后的行为。
        second = poll_once(dict(config), process, factory=fake.factory)

    assert (first.total, first.matched, first.delivered, first.approved) == (3, 1, True, False)
    assert (second.total, second.matched, second.delivered, second.approved) == (3, 1, False, True)
    [(body, approved, digest)] = calls
    assert (body, approved, len(digest)) == ("合成正文", False, 64)
    assert fake.flags == {inbox: {2}, sent: {7}}
    assert fake.fetched(inbox, 2, SUMMARY)
    assert not fake.fetched(inbox, 2, "HEADER")
    assert not fake.fetched(inbox, 3, SUMMARY)
    assert not fake.fetched(inbox, 1, "2")
    assert not fake.fetched(inbox, 1, "3")
    assert not fake.fetched(sent, 7, "HEADER")
    for private_value in (approver, marker, "合成正文", config["MAIL_HOST"], str(request["Message-ID"])):
        assert private_value not in caplog.text


def test_poll_rejects_ambiguous_marker_and_prevents_rebinding(config):
    inbox = config["MAIL_FOLDER"]
    approver, marker = config["MAIL_APPROVER"], config["MAIL_TEST_MARKER"]
    fake = FakeIMAP()
    fake.add(inbox, 1, mail(approver, marker, "第一封"))
    fake.add(inbox, 2, mail(approver, marker, "第二封"))

    def reject(*args):
        pytest.fail("不应交接邮件")

    assert poll_once(config, reject, factory=fake.factory).matched == 2
    assert not any(command[0] == "FETCH" and command[-1] == "(BODYSTRUCTURE)" for command in fake.calls)
    first = fake.folders[inbox].pop(1)

    def fail(*args):
        raise RuntimeError("私有异常内容")

    with pytest.raises(MailError, match="回调失败") as caught:
        poll_once(config, fail, factory=fake.factory)
    assert str(caught.value.__cause__) == "None"
    second = fake.folders[inbox].pop(2)
    fake.folders[inbox][1] = first
    with pytest.raises(MailError, match="已绑定"):
        poll_once(config, reject, factory=fake.factory)
    fake.folders[inbox][2] = second
    del fake.folders[inbox][1]
    assert poll_once(config, lambda *_: None, factory=fake.factory).delivered


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (" 同意 ", True),
        ("我同意该申请！", True),
        ("审批结果：同意，请按原申请执行。", True),
        ("不同意", False),
        ("同意，但需要确认", False),
        ("同意。\n\n请先把条件改掉。", False),
        ("待核对后同意", False),
        ("> 同意", False),
        ("是否同意？", False),
    ],
)
def test_conservative_approval(body, expected):
    assert explicit_approval((("text/plain", body),)) is expected


def test_conflicting_mime_alternatives_do_not_approve():
    assert not explicit_approval((("text/plain", "同意"), ("text/html", "不同意")))


def test_related_text_resources_are_not_fetched(config):
    fake = FakeIMAP()
    inbox = config["MAIL_FOLDER"]
    fake.add(inbox, 1, mail(config["MAIL_APPROVER"], config["MAIL_TEST_MARKER"], "合成正文"),
             multipart=True)
    fake.folders[inbox][1]["structure"] = (
        b'(' + PLAIN + b'("TEXT" "PLAIN" NIL "<inline>" NIL "8BIT" 8 1) "RELATED")'
    )
    calls = []

    assert poll_once(config, lambda *args: calls.append(args), factory=fake.factory).delivered
    assert calls[0][0] == "合成正文"
    assert not fake.fetched(inbox, 1, "2.MIME")
    assert not fake.fetched(inbox, 1, "2")


def test_run_polling_picks_up_mail_at_next_minute(config):
    inbox = config["MAIL_FOLDER"]
    fake = FakeIMAP()
    calls = []
    stop = Event()
    now = 0.0
    waits = []

    def wait(seconds):
        nonlocal now
        waits.append(seconds)
        assert len(waits) == 1
        assert calls == []
        assert seconds == 60
        now += seconds
        fake.add(inbox, 1, mail(config["MAIL_APPROVER"], config["MAIL_TEST_MARKER"], "合成正文"))
        return False

    def process(*args):
        calls.append((now, args[0]))
        stop.set()

    run_polling(config, process, stop, factory=fake.factory, clock=lambda: now, wait=wait)
    assert calls == [(60, "合成正文")]
    assert fake.calls.count(("EXAMINE", f'"{inbox}"')) == 2


def test_history_writes_only_matching_text_and_linked_replies(config, tmp_path):
    inbox, sent = config["MAIL_FOLDER"], config["MAIL_SENT_FOLDER"]
    fake = FakeIMAP()
    request = mail(config["MAIL_USER"], "合成主题", "合成申请")
    request_id = str(request["Message-ID"])
    fake.add(inbox, 1, request, multipart=True)
    fake.add(inbox, 2, mail(config["MAIL_USER"], "其他主题", "无关正文"))
    fake.add(sent, 3, mail(config["MAIL_APPROVER"], "回复", "同意", references=request_id))
    fake.add(sent, 4, mail(config["MAIL_APPROVER"], "另一个回复", "无关正文",
                           reply_to="<other>", references=request_id))
    fake.flags[inbox].add(1)

    assert export_history(config, date(2000, 1, 1), date(2000, 2, 1),
                          factory=fake.factory) == (1, 1)
    [output] = tmp_path.glob("mail-history-*/samples.json")
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    application, reply = json.loads(output.read_text())
    assert application["kind"] == "application" and application["body"] == ["合成正文"]
    assert reply["kind"] == "reply" and reply["body"] == ["同意\r\n"]
    assert all(request_id in item["headers"] for item in (application, reply))
    assert not fake.fetched(inbox, 1, "2")
    assert not fake.fetched(inbox, 1, "3")
    assert not fake.fetched(inbox, 2, "TEXT")
    assert not fake.fetched(sent, 4, "TEXT")
    assert ("SEARCH", inbox, "SINCE", "01-Jan-2000", "BEFORE", "01-Feb-2000") in fake.calls
    for folder in (inbox, sent):
        assert fake.calls.count(("SEARCH", folder, "SEEN")) == 2
    assert fake.flags == {inbox: {1}, sent: set()}


def test_history_does_not_publish_on_seen_change(config, tmp_path):
    fake = FakeIMAP()
    inbox = config["MAIL_FOLDER"]
    fake.add(inbox, 1, mail(config["MAIL_USER"], "合成主题", "合成正文"))
    fake.newly_seen = (inbox, 1, "TEXT")

    with pytest.raises(MailError, match="已读状态前后不一致"):
        export_history(config, date(2000, 1, 1), date(2000, 2, 1), factory=fake.factory)
    assert not list(tmp_path.iterdir())


def test_history_cli_missing_configuration_fails_before_connecting(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("sys.argv", ["history", "--env-file", str(tmp_path / "missing"),
                                   "--since", "2000-01-01", "--before", "2000-02-01"])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 1
    assert "MAIL_HOST" in capsys.readouterr().err


def test_history_cli_exports_with_config_and_prints_only_counts(config, monkeypatch, tmp_path, capsys):
    fake = FakeIMAP()
    inbox = config["MAIL_FOLDER"]
    fake.add(inbox, 1, mail(config["MAIL_USER"], config["MAIL_HISTORY_SUBJECT"], "合成正文"))
    env_file = tmp_path / "fixture.env"
    base_keys = ("MAIL_HOST", "MAIL_PORT", "MAIL_USER", "MAIL_PASSWORD", "MAIL_FOLDER")
    env_file.write_text("".join(f"{key}={config[key]}\n" for key in base_keys))
    for key in ("MAIL_SENT_FOLDER", "MAIL_HISTORY_SUBJECT", "MAIL_PRIVATE_DIR"):
        monkeypatch.setenv(key, config[key])
    monkeypatch.setattr("qianshu.mail.history.connect", lambda settings, _: connect(settings, fake.factory))
    monkeypatch.setattr("sys.argv", ["history", "--env-file", str(env_file),
                                   "--since", "2000-01-01", "--before", "2000-02-01"])

    assert main() == 0
    assert capsys.readouterr().out == "申请 1 封，关联回复 0 封。\n"
    assert len(list(tmp_path.glob("mail-history-*/samples.json"))) == 1
