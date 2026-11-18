import imaplib
import json
import logging
import stat
from collections import deque
from datetime import date
from email import policy
from email.message import EmailMessage
from functools import partial
from threading import Event, enumerate as running_threads
from uuid import uuid4

import pytest

from qianshu.mail import ApprovalDecision, _private_dir, poll_once, run_polling
from qianshu.mail.history import export_history, main
from qianshu.mail.imap import SUMMARY, MailError, ReadOnlyMailbox, connect
from qianshu.mail.message import authored_reply


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
        self.list_rows = [None]

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

    def list(self):
        self.calls.append(("LIST",))
        return "OK", self.list_rows

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
            elif command == b'LIST "" *':
                self.responses.append(b'* LIST (\\Sent) "/" "&XfJT0ZAB-"\r\n')
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
        assert mailbox._examine(mailbox.sent_folder(config)) == 1
        assert mailbox.select("已发送") == 1
        assert mailbox.section(1, "TEXT") == b"abc"
    assert [command.split(b" ", 1)[0] for command in commands] == [
        b"CAPABILITY", b"LOGIN", b"LIST", b"EXAMINE", b"EXAMINE", b"UID",
    ]
    assert commands[3] == commands[4] == b'EXAMINE "&XfJT0ZAB-"'


@pytest.mark.parametrize(
    ("list_row", "discovered"),
    [(rb'(\HasNoChildren \sEnT) "/" "&XfJT0ZAB-"', "&XfJT0ZAB-"),
     ((rb'(\Sent) "/" {10}', b"&XfJT0ZAB-"), "&XfJT0ZAB-"),
     (rb'(\HasNoChildren) "/" "fixture"', None)],
)
def test_poll_selects_sent_by_list_attribute_or_config(config, list_row, discovered):
    fake = FakeIMAP()
    fake.list_rows = [list_row]
    sent = discovered or config["MAIL_SENT_FOLDER"]
    request = mail(config["MAIL_APPROVER"], config["MAIL_TEST_MARKER"], "合成申请")
    fake.add(config["MAIL_FOLDER"], 1, request)
    fake.add(sent, 2, mail(config["MAIL_APPROVER"], config["MAIL_TEST_MARKER"], "同意",
                           reply_to=str(request["Message-ID"])))

    result = poll_once(config, lambda *_: None, factory=fake.factory,
                       approval_judge=lambda _: ApprovalDecision.APPROVE)

    assert result.delivered and result.approved
    assert ("LIST",) in fake.calls
    assert ("EXAMINE", f'"{sent}"') in fake.calls
    assert fake.flags[sent] == set()


def test_poll_requires_sent_attribute_or_config(config):
    fake = FakeIMAP()
    del config["MAIL_SENT_FOLDER"]
    with pytest.raises(MailError, match="MAIL_SENT_FOLDER"):
        poll_once(config, lambda *_: None, factory=fake.factory)
    assert fake.calls == [("LIST",), ("SHUTDOWN",)]


@pytest.mark.parametrize("configured", ["Sent", None, "missing-fixture"])
def test_poll_delivers_with_multiple_sent_folders(config, configured):
    fake = FakeIMAP()
    fake.list_rows = [rb'(\Sent) "/" "archive-fixture"', rb'(\Sent) "/" "Sent"']
    inbox, sent = config["MAIL_FOLDER"], config["MAIL_SENT_FOLDER"]
    approver, marker = config["MAIL_APPROVER"], config["MAIL_TEST_MARKER"]
    request = mail(approver, marker, "合成申请")
    fake.add(inbox, 1, request)
    fake.add(sent, 2, mail(approver, marker, "同意", reply_to=str(request["Message-ID"])))
    if configured is None:
        del config["MAIL_SENT_FOLDER"]
    else:
        config["MAIL_SENT_FOLDER"] = configured
    calls, judged = [], []

    def judge(text):
        judged.append(text)
        return ApprovalDecision.APPROVE

    result = poll_once(config, lambda *args: calls.append(args), factory=fake.factory,
                       approval_judge=judge)

    approved = configured == sent
    assert result.delivered and result.approved is approved
    [(body, delivered_approval, _)] = calls
    assert (body, delivered_approval) == ("合成申请", approved)
    assert judged == (["同意"] if approved else [])
    folders = [call[1] for call in fake.calls if call[0] == "EXAMINE"]
    assert folders == ([f'"{inbox}"', f'"{sent}"'] if approved else [f'"{inbox}"'])


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
    judged = []

    def process(*args):
        calls.append(args)

    def judge(text):
        judged.append(text)
        return ApprovalDecision.APPROVE

    with caplog.at_level(logging.INFO):
        first = poll_once(config, process, factory=fake.factory, approval_judge=judge)
        fake.add(sent, 8, mail(approver, "关联回复", "同意。\n\nOn a test date, fixture wrote:\n> 合成正文",
                               reply_to=str(request["Message-ID"])))
        # 再次从配置打开私有台账，覆盖进程重启后的行为。
        second = poll_once(dict(config), process, factory=fake.factory, approval_judge=judge)

    assert (first.total, first.matched, first.delivered, first.approved) == (3, 1, True, False)
    assert (second.total, second.matched, second.delivered, second.approved) == (3, 1, False, True)
    assert judged == ["同意。"]
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


@pytest.mark.parametrize("reply_header", ["reply_to", "references"])
def test_poll_delivers_original_when_self_reply_arrived_before_first_poll(config, reply_header):
    fake = FakeIMAP()
    inbox, sent = config["MAIL_FOLDER"], config["MAIL_SENT_FOLDER"]
    config["MAIL_APPROVER"] = config["MAIL_USER"]
    approver, marker = config["MAIL_APPROVER"], config["MAIL_TEST_MARKER"]
    request = mail(approver, marker, "合成申请")
    reply = mail(approver, f"Re: {marker}", "同意",
                 **{reply_header: str(request["Message-ID"])})
    fake.add(inbox, 1, request)
    fake.add(inbox, 2, reply)
    fake.add(sent, 3, reply)
    calls = []

    result = poll_once(config, lambda *args: calls.append(args), factory=fake.factory,
                       approval_judge=lambda _: ApprovalDecision.APPROVE)

    assert (result.total, result.matched, result.delivered, result.approved) == (2, 1, True, True)
    [(body, approved, _)] = calls
    assert (body, approved) == ("合成申请", True)
    assert fake.fetched(inbox, 2, SUMMARY)
    assert not fake.fetched(inbox, 2, "HEADER")


@pytest.mark.parametrize(
    ("body", "decision", "approved"),
    [
        ("OK", ApprovalDecision.APPROVE, True),
        ("可以", ApprovalDecision.APPROVE, True),
        ("没问题", ApprovalDecision.APPROVE, True),
        ("不同意", ApprovalDecision.REJECT, False),
        ("同意，但需要确认", ApprovalDecision.CONDITIONAL, False),
        ("待核对", ApprovalDecision.UNCERTAIN, False),
        ("同意", None, False),
        ("同意", "同意", False),
    ],
)
def test_poll_uses_only_the_injected_approval_decision(config, body, decision, approved):
    fake = FakeIMAP()
    inbox, sent = config["MAIL_FOLDER"], config["MAIL_SENT_FOLDER"]
    approver, marker = config["MAIL_APPROVER"], config["MAIL_TEST_MARKER"]
    request = mail(approver, marker, "合成申请")
    fake.add(inbox, 1, request)
    fake.add(sent, 2, mail(approver, marker, body, reply_to=str(request["Message-ID"])))
    calls = []
    judged = []

    def judge(text):
        judged.append(text)
        return decision

    result = poll_once(config, lambda *args: calls.append(args), factory=fake.factory,
                       approval_judge=judge if decision is not None else None)
    assert result.delivered and result.approved is approved
    assert calls[0][:2] == ("合成申请", approved)
    assert judged == ([] if decision is None else [body])


def test_poll_delivers_unapproved_when_judge_raises(config, caplog):
    fake = FakeIMAP()
    approver, marker = config["MAIL_APPROVER"], config["MAIL_TEST_MARKER"]
    request = mail(approver, marker, "合成申请")
    fake.add(config["MAIL_FOLDER"], 1, request)
    fake.add(config["MAIL_SENT_FOLDER"], 2,
             mail(approver, marker, "同意", reply_to=str(request["Message-ID"])))
    calls = []

    def judge(_):
        raise ValueError("合成异常内容")

    result = poll_once(config, lambda *args: calls.append(args), factory=fake.factory,
                       approval_judge=judge)

    assert result.delivered and not result.approved
    [(body, approved, _)] = calls
    assert (body, approved) == ("合成申请", False)
    assert "合成异常内容" not in caplog.text


@pytest.mark.parametrize(
    ("kind", "body", "expected"),
    [
        ("text/html", '<blockquote><div>同意</div></blockquote>', ""),
        ("text/html", '<div class="gmail_quote"><div>同意</div></div>', ""),
        ("text/html", "<div>OK</div><div>On a test date, fixture wrote:</div>"
         "<blockquote><blockquote>旧引文</blockquote>旧正文</blockquote><p>仍需核对</p>",
         None),
        ("text/plain", "同意。\n> 10 万元须另行审批", None),
        ("text/plain", "可以\n\n样例作者写道：\n> 旧正文\n>> 旧引文", "可以"),
        ("text/plain", "可以\n\n样例作者写道：\n> 旧正文\n\n> 仍需另行审批", None),
        ("text/plain", "> 同意", None),
        ("text/html", "<blockquote>同意", None),
        ("text/html", '<div>收到</div><hr><div id="DIVRPLYFWDMSG">合成引用头</div>'
         '<div>同意</div>', None),
        ("text/html", '<div id="x_divRplyFwdMsg">合成引用头</div><p>同意</p>', None),
        ("text/html", '<p>&gt; 边界不明</p><div id="divRplyFwdMsg"></div><p>同意</p>', None),
        ("text/html", '<div>同意</div><hr><div id="x_divRplyFwdMsg">合成引用头</div>'
         '<blockquote><p>旧正文</p></blockquote>', "同意"),
        ("text/html", '<p>同意</p><div id="netease_mail_quote"><p>旧合成正文</p></div>', "同意"),
        ("text/plain", "同意\n-----原始邮件-----\n> 旧正文", "同意"),
        ("text/html", "<p>收到</p><script>同意</script><style>同意</style>", "收到"),
    ],
)
def test_authored_reply(kind, body, expected):
    extracted = authored_reply(kind, body)
    if expected is None:
        assert extracted is None
    else:
        assert extracted is not None
        assert extracted.split() == expected.split()


@pytest.mark.parametrize(
    ("kind", "body"),
    [
        ("plain", "同意。\n> 10 万元须另行审批"),
        ("plain", "收到\n-----原始邮件-----\n同意"),
        ("html", "<blockquote>同意</blockquote>"),
        ("html", '<p>收到</p><div class="mail-editor-reference-message-container"><p>同意</p></div>'),
        ("html", '<p>收到</p><div class="custom-original-fragment"><p>同意</p></div>'),
        ("html", "<p>收到</p><reply-fragment><p>同意</p></reply-fragment>"),
        ("html", '<p>同意</p><div data-customer-quote="1">合成旧文</div>'),
        ("html", '<p>收到</p><div>Von: 合成发件人</div>'
         '<div id="OLK_SRC_BODY_SECTION"><p>同意</p></div>'),
        ("html", '<p>收到</p><div class="OLK_SRC_BODY_SECTION_FWD"><p>同意</p></div>'),
        ("html", '<p>收到</p><div class="fixture-fragment"><p>同意</p></div>'),
        ("html", '<p>收到</p><fixture-fragment>同意</fixture-fragment>'),
        ("html", '<p>收到</p><!-- 合成边界 --><p>同意</p>'),
        ("plain", "收到\nVon: 合成发件人\n同意"),
        ("plain", "收到\nDe : 合成发件人\n同意"),
        ("plain", "收到\nDa: 合成发件人\n同意"),
        ("plain", "收到\n差出人: 合成发件人\n同意"),
        ("plain", "收到\n________\n同意"),
        ("html", "收到<pre>Von: 合成发件人</pre><p>同意</p>"),
        ("html", "<p>同意</p><blockquote>旧正文</blockquote><p>&gt; 10 万元须另行审批</p>"),
        ("html", "<p>同意</p><blockquote>旧正文</blockquote><p>＞ 10 万元须另行审批</p>"),
        ("html", '<p>同意</p><hr><div id="divRplyFwdMsg">合成引用头</div>'
         '<blockquote>旧正文</blockquote><p>超过 10 万元须另行审批</p>'
         '<blockquote>旧正文续行</blockquote>'),
        ("html", "<p>收到</p><div>From: 合成发件人</div><div>同意</div>"),
        ("html", "<p>收到</p><div>发件人：合成发件人</div><div>同意</div>"),
        ("plain", "同意\n-----原始邮件-----\n> 旧正文\n超过 10 万元须另行审批\n> 旧正文续行"),
    ],
)
def test_poll_does_not_submit_ambiguous_or_quoted_approval_to_judge(config, kind, body):
    fake = FakeIMAP()
    approver, marker = config["MAIL_APPROVER"], config["MAIL_TEST_MARKER"]
    request = mail(approver, marker, "合成申请")
    fake.add(config["MAIL_FOLDER"], 1, request)
    reply = mail(approver, marker, body, reply_to=str(request["Message-ID"]))
    reply.set_content(body, subtype=kind)
    fake.add(config["MAIL_SENT_FOLDER"], 2, reply)
    if kind == "html":
        fake.folders[config["MAIL_SENT_FOLDER"]][2]["structure"] = PLAIN.replace(b'"PLAIN"', b'"HTML"')

    calls, judged = [], []

    def judge(text):
        judged.append(text)
        return ApprovalDecision.APPROVE

    result = poll_once(config, lambda *args: calls.append(args), factory=fake.factory,
                       approval_judge=judge)
    assert result.delivered and not result.approved
    [(body, approved, _)] = calls
    assert (body, approved) == ("合成申请", False)
    assert not judged


def test_poll_judges_only_authored_text_in_each_mime_version(config):
    fake = FakeIMAP()
    inbox, sent = config["MAIL_FOLDER"], config["MAIL_SENT_FOLDER"]
    approver, marker = config["MAIL_APPROVER"], config["MAIL_TEST_MARKER"]
    request = mail(approver, marker, "合成申请")
    fake.add(inbox, 1, request)
    fake.add(sent, 2, mail(approver, marker, "", reply_to=str(request["Message-ID"])))
    fake.folders[sent][2].update({
        "structure": b"(" + PLAIN + PLAIN.replace(b'"PLAIN"', b'"HTML"') + b' "ALTERNATIVE")',
        "1": b"OK",
        "2.MIME": b"Content-Type: text/html; charset=utf-8\r\n\r\n",
        "2": ('<p>还需核对</p><hr><div id="divRplyFwdMsg">合成引用头</div>'
              '<blockquote>旧引文中的同意</blockquote>').encode(),
    })
    judged = []

    def judge(text):
        judged.append(text)
        return ApprovalDecision.APPROVE if text == "OK" else ApprovalDecision.CONDITIONAL

    result = poll_once(config, lambda *_: None, factory=fake.factory, approval_judge=judge)
    assert result.delivered and not result.approved
    assert judged == ["OK", "还需核对"]


@pytest.mark.parametrize("second_decision", [ApprovalDecision.APPROVE, ApprovalDecision.REJECT])
def test_poll_verifies_reply_sender_and_requires_all_approver_replies(config, second_decision):
    fake = FakeIMAP()
    inbox, sent = config["MAIL_FOLDER"], config["MAIL_SENT_FOLDER"]
    approver, marker = config["MAIL_APPROVER"], config["MAIL_TEST_MARKER"]
    request = mail(approver, marker, "合成申请")
    request_id = str(request["Message-ID"])
    fake.add(inbox, 1, request)
    fake.add(sent, 2, mail(config["MAIL_USER"], marker, "非审批人回复", reply_to=request_id))
    fake.add(sent, 3, mail(approver, marker, "OK", reply_to=request_id))
    fake.add(sent, 4, mail(approver, marker, "后续回复", references=request_id))
    judged = []

    def judge(text):
        judged.append(text)
        return {"OK": ApprovalDecision.APPROVE, "后续回复": second_decision}[text]

    result = poll_once(config, lambda *_: None, factory=fake.factory, approval_judge=judge)
    assert result.approved is (second_decision is ApprovalDecision.APPROVE)
    assert judged == ["OK", "后续回复"]
    assert not fake.fetched(sent, 2, "HEADER")


def test_run_polling_delivers_after_timeout_while_judge_is_still_running(config):
    fake = FakeIMAP()
    approver, marker = config["MAIL_APPROVER"], config["MAIL_TEST_MARKER"]
    request = mail(approver, marker, "合成申请")
    fake.add(config["MAIL_FOLDER"], 1, request)
    fake.add(config["MAIL_SENT_FOLDER"], 2,
             mail(approver, marker, "同意", reply_to=str(request["Message-ID"])))
    stop, started, release, finished = Event(), Event(), Event(), Event()
    calls, results, judged = [], [], []
    now = 0.0

    def judge(text):
        judged.append(text)
        started.set()
        release.wait(5)
        finished.set()
        return ApprovalDecision.APPROVE

    def on_result(result):
        results.append(result)
        assert started.wait(1) and not finished.is_set()
        if len(results) == 2:
            stop.set()

    def wait(seconds):
        nonlocal now
        now += seconds
        return False

    try:
        run_polling(config, lambda *args: calls.append(args), stop, factory=fake.factory,
                    clock=lambda: now, wait=wait, on_result=on_result,
                    approval_judge=judge, approval_judge_timeout=0.01)
    finally:
        release.set()
        if started.is_set():
            assert finished.wait(1)

    assert [(result.delivered, result.approved) for result in results] == [(True, False), (False, False)]
    assert judged == ["同意"]
    [(body, approved, _)] = calls
    assert (body, approved) == ("合成申请", False)


def test_poll_once_limits_judge_threads_across_three_distinct_wrappers(config):
    approver = config["MAIL_APPROVER"]
    release, started = Event(), Event()
    judged, results, calls = [], [], []
    before = set(running_threads())

    def judge(text):
        judged.append(text)
        started.set()
        release.wait()
        return ApprovalDecision.APPROVE

    bodies = [f"合成申请 {index}" for index in range(3)]
    try:
        for body in bodies:
            marker = uuid4().hex
            round_config = dict(config, MAIL_TEST_MARKER=marker)
            fake = FakeIMAP()
            request = mail(approver, marker, body)
            fake.add(config["MAIL_FOLDER"], 1, request)
            fake.add(config["MAIL_SENT_FOLDER"], 2,
                     mail(approver, marker, "同意", reply_to=str(request["Message-ID"])))
            results.append(poll_once(round_config, lambda *args: calls.append(args),
                                     factory=fake.factory, approval_judge=partial(judge),
                                     approval_judge_timeout=0.01))
            assert started.wait(1)
            assert sum(thread.name == "mail-approval-judge" for thread in running_threads()) <= 1
        assert judged == ["同意"]
        assert [(result.delivered, result.approved) for result in results] == [(True, False)] * 3
        assert [call[:2] for call in calls] == [(body, False) for body in bodies]
    finally:
        release.set()
        for thread in set(running_threads()) - before:
            if thread.name == "mail-approval-judge":
                thread.join(timeout=1)


def test_poll_delivers_when_reply_has_no_body(config):
    fake = FakeIMAP()
    approver, marker = config["MAIL_APPROVER"], config["MAIL_TEST_MARKER"]
    request = mail(approver, marker, "合成申请")
    sent = config["MAIL_SENT_FOLDER"]
    fake.add(config["MAIL_FOLDER"], 1, request)
    fake.add(sent, 2, mail(approver, marker, "合成占位", reply_to=str(request["Message-ID"])))
    fake.folders[sent][2]["structure"] = b'("IMAGE" "PNG" NIL NIL NIL "BASE64" 8)'
    calls, judged = [], []

    result = poll_once(config, lambda *args: calls.append(args), factory=fake.factory,
                       approval_judge=judged.append)

    assert result.delivered and not result.approved
    assert calls[0][:2] == ("合成申请", False)
    assert not judged
    assert not fake.fetched(sent, 2, "TEXT")


@pytest.mark.parametrize(("plain", "expected"), [("合成纯文本", "合成纯文本"), (" ", "合成 HTML")])
def test_related_alternative_prefers_plain_and_skips_inline_image(config, plain, expected):
    fake = FakeIMAP()
    inbox = config["MAIL_FOLDER"]
    fake.add(inbox, 1, mail(config["MAIL_APPROVER"], config["MAIL_TEST_MARKER"], "合成占位"))
    fake.folders[inbox][1].update({
        "structure": (
            b"((" + PLAIN.replace(b'"PLAIN"', b'"HTML"') + PLAIN + b' "ALTERNATIVE")'
            b'("IMAGE" "PNG" NIL "<fixture-image>" NIL "BASE64" 8) "RELATED")'
        ),
        "1.1.MIME": b"Content-Type: text/html; charset=utf-8\r\n\r\n",
        "1.1": '<p>合成 HTML</p><img src="cid:fixture-image">'.encode(),
        "1.2.MIME": b"Content-Type: text/plain; charset=utf-8\r\n\r\n",
        "1.2": plain.encode(),
    })
    calls = []

    assert poll_once(config, lambda *args: calls.append(args), factory=fake.factory).delivered
    assert calls[0][0] == expected
    assert not fake.fetched(inbox, 1, "2.MIME")
    assert not fake.fetched(inbox, 1, "2")


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


@pytest.mark.parametrize("reply_in_inbox", [False, True], ids=["sent-only", "self-reply"])
def test_run_polling_reports_late_approval(config, reply_in_inbox):
    fake = FakeIMAP()
    inbox, sent = config["MAIL_FOLDER"], config["MAIL_SENT_FOLDER"]
    if reply_in_inbox:
        config["MAIL_APPROVER"] = config["MAIL_USER"]
    approver, marker = config["MAIL_APPROVER"], config["MAIL_TEST_MARKER"]
    request = mail(approver, marker, "合成申请")
    fake.add(inbox, 1, request)
    calls = []
    results = []
    stop = Event()
    now = 0.0

    def on_result(result):
        results.append((now, result))
        if len(results) == 2:
            stop.set()

    def wait(seconds):
        nonlocal now
        assert seconds == 60 and len(results) == 1
        now += seconds
        reply = mail(approver, f"Re: {marker}", "可以", reply_to=str(request["Message-ID"]))
        fake.add(sent, 2, reply)
        if reply_in_inbox:
            fake.add(inbox, 3, reply)
        return False

    run_polling(config, lambda *args: calls.append(args), stop, factory=fake.factory,
                clock=lambda: now, wait=wait, on_result=on_result,
                approval_judge=lambda _: ApprovalDecision.APPROVE)

    assert [(tick, result.total, result.matched, result.delivered, result.approved)
            for tick, result in results] == [
        (0, 1, 1, True, False), (60, 2 if reply_in_inbox else 1, 1, False, True),
    ]
    [(body, approved, _)] = calls
    assert (body, approved) == ("合成申请", False)
    if reply_in_inbox:
        assert fake.fetched(inbox, 3, SUMMARY)
        assert not fake.fetched(inbox, 3, "HEADER")


def test_history_finds_related_alternative_root_by_mime_content_id(config, tmp_path):
    fake = FakeIMAP()
    inbox = config["MAIL_FOLDER"]
    fake.add(inbox, 1, mail(config["MAIL_USER"], config["MAIL_HISTORY_SUBJECT"], "合成申请"))
    fake.folders[inbox][1].update({
        "structure": (
            b"(" + PLAIN + b"(" + PLAIN + PLAIN.replace(b'"PLAIN"', b'"HTML"')
            + b' "ALTERNATIVE") "RELATED" ("START" "<fixture-root>"))'
        ),
        "1.MIME": b"Content-ID: <fixture-resource>\r\n\r\n",
        "2.MIME": b"Content-ID: <fixture-root>\r\n\r\n",
        "2.1.MIME": b"Content-Type: text/plain; charset=utf-8\r\n\r\n",
        "2.1": "合成正文".encode(),
        "2.2.MIME": b"Content-Type: text/html; charset=utf-8\r\n\r\n",
        "2.2": "<p>合成正文</p>".encode(),
    })

    assert export_history(config, date(2000, 1, 1), date(2000, 2, 1),
                          factory=fake.factory) == (1, 0)
    [output] = tmp_path.glob("mail-history-*/samples.json")
    [application] = json.loads(output.read_text())
    assert application["body"] == ["合成正文", "<p>合成正文</p>"]
    assert fake.fetched(inbox, 1, "1.MIME")
    assert fake.fetched(inbox, 1, "2.MIME")
    assert not fake.fetched(inbox, 1, "1")
    assert not fake.fetched(inbox, 1, "2")


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
