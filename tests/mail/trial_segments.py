"""合成 IMAP 响应。基线与试运行使用同一组结构、段头与 literal。"""

import imaplib
import re
from email import policy
from email.parser import BytesHeaderParser
from email.message import EmailMessage

from qianshu.mail.imap import TrialMailbox


def encoded(value):
    return str(value).encode()


def params(pairs):
    return [encoded(v) for pair in pairs for v in pair] or b"NIL"


def synthetic(number, text, attachment=False):
    msg = EmailMessage(policy=policy.SMTP)
    msg["Message-ID"] = "<" + str(number) + chr(64) + "synthetic.invalid>"
    msg["Subject"] = "subject " + str(number)
    msg.set_content(text)
    if attachment:
        msg.add_attachment(b"ATTACHMENT_SENTINEL", maintype="application", subtype="octet-stream", filename="file")
    return msg


class Segments:
    def __init__(self, raw):
        self.sections = {}
        self.tree = self.index(raw, "")

    def index(self, raw, path):
        split = re.match(rb"^(?:\r?\n)", raw) or re.search(rb"\r?\n\r?\n", raw)
        header, payload = (raw[:split.end()], raw[split.end():]) if split else (raw, b"")
        self.sections[path + ".MIME" if path else "HEADER"] = header
        self.sections[path or "TEXT"] = payload
        msg = BytesHeaderParser(policy=policy.default).parsebytes(header)
        kind, subtype = msg.get_content_type().upper().encode().split(b"/")
        attributes = params(msg.get_params()[1:] if msg["Content-Type"] else [])
        disposition = msg.get_content_disposition()
        disp = ([encoded(disposition.upper()), params(msg.get_params(header="content-disposition")[1:])]
                if disposition else b"NIL")
        if kind == b"MULTIPART":
            boundary = re.escape(encoded(msg.get_boundary()))
            markers = list(re.finditer(rb"(?m)^--" + boundary + rb"(--)?[ \t]*(?:\r?\n|$)", payload))
            children = []
            for idx, mark in enumerate(markers):
                if mark[1]:
                    break
                end = markers[idx + 1].start() if idx + 1 < len(markers) else len(payload)
                wire = re.sub(rb"\r?\n\Z", b"", payload[mark.end():end])
                children.append(self.index(wire, (path + "." if path else "") + str(idx + 1)))
            return [*children, subtype, attributes, disp]
        return [kind, subtype, attributes, b"NIL", b"NIL", encoded(msg.get("Content-Transfer-Encoding", "7BIT")),
                encoded(len(payload)), *([b"1"] if kind == b"TEXT" else []), b"NIL", disp]

    def section(self, uid, section):
        return self.sections[section]

    def structure(self, uid):
        return self.tree


def wire_tree(node):
    if isinstance(node, list):
        return b"(" + b" ".join(map(wire_tree, node)) + b")"
    if node == b"NIL" or node.isdigit():
        return node
    return b'"' + node.replace(b"\\", b"\\\\").replace(b'"', b'\\"') + b'"'


class Wire:
    select = imaplib.IMAP4.select

    def __init__(self, box):
        self.box = box


    def uid(self, *args):
        return self.box.uid(*args)

    def response(self, key):
        return self.box.response(key)

    def _simple_command(self, command, folder):
        assert command == "EXAMINE"
        self.box.commands.append((command, folder))
        self.untagged_responses["EXISTS"] = [encoded(len(self.box.messages))]
        return "OK", []


class Inbox(TrialMailbox):
    def __init__(self):
        super().__init__(Wire(self))
        self.messages = {1: synthetic(1, "historical")}
        self.seen = {1}
        self.generation = 1
        self.next_uid = 2
        self.reads = []
        self.commands = []

    def response(self, key):
        return key, [encoded({"UIDNEXT": self.next_uid, "UIDVALIDITY": self.generation}[key])]

    def add(self, msg):
        self.messages[self.next_uid] = msg
        self.next_uid += 1

    def uid(self, command, *args):
        self.commands.append((command, *args))
        if command == "SEARCH":
            assert args[0] == "UID"
            first, _, last = args[1].partition(":")
            result = [i for i in sorted(self.messages) if int(first) <= i <= int(last or first)]
            if len(args) > 2:
                assert args[2] in {"SEEN", "UNSEEN"}
                result = [i for i in result if (i in self.seen) == (args[2] == "SEEN")]
            return "OK", [b" ".join(map(encoded, result))]
        assert command == "FETCH"
        uid, query = args
        self.reads.append(int(uid))
        message = self.messages[int(uid)]
        if not isinstance(message, Segments):
            message = Segments(message if isinstance(message, bytes) else message.as_bytes())
        prefix = b"1 (UID " + encoded(uid) + b" "
        if query == "(BODYSTRUCTURE)":
            return "OK", [prefix + b"BODYSTRUCTURE " + wire_tree(message.tree) + b")"]
        section = re.fullmatch(r"\(BODY\.PEEK\[(.+)\]\)", query)[1]
        raw = message.sections[section]
        return "OK", [(prefix + b"BODY[" + encoded(section) + b"] {" + encoded(len(raw)) + b"}", raw), b")"]


def read_fixture(raw):
    from qianshu.mail.segmented import read_segmented
    box = Inbox()
    box.add(Segments(raw))
    return read_segmented(box, 2, False)


def fixture_item(raw):
    return read_fixture(raw)[0]
