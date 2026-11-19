"""X8 历史申请样例的私有导出入口。"""

import argparse
import imaplib
import json
import os
from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path
from tempfile import mkdtemp

from qianshu.config import ConfigError, load_config

from . import _private_dir
from .imap import SUMMARY, MailError, ReadOnlyMailbox, connect
from .message import body_parts, headers, is_reply, message_id


def _sample(mailbox: ReadOnlyMailbox, uid: int, kind: str) -> dict[str, str | list[str]]:
    raw = mailbox.section(uid, "HEADER")
    return {
        "kind": kind,
        "headers": raw.decode("utf-8", errors="surrogateescape"),
        "body": [text for _, text in body_parts(mailbox, uid, raw)],
    }


def export_history(
    config: Mapping[str, str],
    since: date,
    before: date,
    *,
    limit: int = 10,
    factory: Callable = imaplib.IMAP4_SSL,
) -> tuple[int, int]:
    """按 IMAP 内部日期和主题筛申请，保存信头、文字正文和关联回复，返回封数。"""
    subject_text = config.get("MAIL_HISTORY_SUBJECT", "").strip().casefold()
    if since >= before or not 1 <= limit <= 10 or not subject_text:
        raise MailError("请提供有效日期范围、主题关键词和样例封数。")
    inbox = config.get("MAIL_FOLDER") or "INBOX"
    directory = _private_dir(config)

    applications = []
    replies = []
    request_ids = set()
    with connect(config, factory) as mailbox:
        sent = mailbox.sent_folder(config)
        if sent is None:
            raise MailError("IMAP 返回了多个带 \\Sent 属性的文件夹，请配置 MAIL_SENT_FOLDER。")
        mailbox._examine(sent)
        sent_seen = mailbox.seen()
        mailbox.select(inbox)
        inbox_seen = mailbox.seen()
        for uid in reversed(mailbox.dated(since, before)):
            summary = headers(mailbox.section(uid, SUMMARY))
            subjects = summary.get_all("Subject", [])
            if len(subjects) != 1 or subject_text not in str(subjects[0]).casefold():
                continue
            applications.append(_sample(mailbox, uid, "application"))
            if request_id := message_id(summary):
                request_ids.add(request_id)
            if len(applications) == limit:
                break
        mailbox._examine(sent)
        candidates: dict[int, set[str]] = {}
        for request_id in sorted(request_ids):
            for uid in mailbox.replies(request_id):
                candidates.setdefault(uid, set()).add(request_id)
        for uid, related_ids in sorted(candidates.items()):
            summary = headers(mailbox.section(uid, SUMMARY))
            if any(is_reply(summary, request_id) for request_id in related_ids):
                replies.append(_sample(mailbox, uid, "reply"))
        if sent_seen != mailbox.seen():
            raise MailError("已发送文件夹已读状态前后不一致。")
        mailbox.select(inbox)
        if inbox_seen != mailbox.seen():
            raise MailError("收件箱已读状态前后不一致。")

    try:
        output_dir = Path(mkdtemp(prefix="mail-history-", dir=directory))
        # 先写临时文件，再发布完整导出，避免中断时出现部分结果。
        temporary = output_dir / "samples.json.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", errors="backslashreplace") as stream:
            json.dump(applications + replies, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(output_dir / "samples.json")
    except OSError:
        raise MailError("历史样例无法写入私有目录。") from None
    return len(applications), len(replies)


def main() -> int:
    parser = argparse.ArgumentParser(description="在私有目录导出 X8 历史邮件样例。")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--since", type=date.fromisoformat, required=True)
    parser.add_argument("--before", type=date.fromisoformat, required=True)
    parser.add_argument("--limit", type=int, default=10)
    options = parser.parse_args()
    try:
        base = load_config("MAIL_HOST", "MAIL_PORT", "MAIL_USER", "MAIL_PASSWORD",
                           env_file=options.env_file)
        config = dict(base)
        for key in ("MAIL_PRIVATE_DIR", "MAIL_SENT_FOLDER", "MAIL_HISTORY_SUBJECT"):
            if value := os.environ.get(key):
                config[key] = value
        applications, replies = export_history(
            config, options.since, options.before, limit=options.limit,
        )
    except (ConfigError, MailError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"申请 {applications} 封，关联回复 {replies} 封。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
