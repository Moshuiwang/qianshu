"""把待处理群 @ 输出给守望者会话，并把会话的答复交回监听器。"""

import argparse
import json
import logging
import sys
import time
from contextlib import closing
from pathlib import Path

from qianshu.config import ConfigError

from .handoff import REPLY_LIMIT, MentionLog, watcher_input
from .sdk import _Transport
from .settings import load_handoff_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="守望者读取群 @ 与近邻消息，或经监听器回复原消息。")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="仅含飞书转交配置的私有文件")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("read", help="一次读取当前待守望者回复的消息")
    commands.add_parser("watch", help="持续输出待回复的消息及之后的新增消息")
    reply = commands.add_parser("reply", help="从标准输入读取回复正文，交给监听器发送")
    reply.add_argument("ref", help="read 或 watch 输出的 ref")
    reply.add_argument("--mention-owner", action="store_true", help="仅在确需产品负责人处理时使用")
    status = commands.add_parser("status", help="查看守望者回复的发送状态")
    status.add_argument("ref")
    args = parser.parse_args(argv)

    try:
        settings, path = load_handoff_settings(args.env_file)
        with closing(MentionLog(path, settings)) as store:
            if args.command == "reply":
                response = sys.stdin.read(REPLY_LIMIT + 1)
                if len(response) > REPLY_LIMIT:
                    raise ValueError
                print(store.queue_reply(args.ref, response, mention_owner=args.mention_owner))
            elif args.command == "status":
                print(store.reply_status(args.ref))
            else:
                logging.getLogger("Lark").disabled = True
                logging.getLogger("httpx").disabled = True
                transport = _Transport(settings, None, time.time)
                shown = set()
                while True:
                    pending = store.pending()
                    shown.intersection_update(row["ref"] for row in pending)
                    for row in pending:
                        if row["ref"] not in shown:
                            entry = watcher_input(row, transport, store)
                            print(json.dumps(entry, ensure_ascii=False, separators=(",", ":")), flush=True)
                            shown.add(row["ref"])
                    if args.command == "read":
                        break
                    time.sleep(1)
    except KeyboardInterrupt:
        return 0
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 1
    except Exception:
        print("守望者命令失败，请核对私有配置、转交编号和回复输入。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
