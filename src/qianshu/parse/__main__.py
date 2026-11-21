"""从标准输入读取申请，将草稿写入仅当前用户可读的新文件。"""

import argparse
import json
import os
import sys
from pathlib import Path

from qianshu.config import ConfigError, load_config

from . import _DEFAULT_COMPANIES_FILE, ModelError, ParseError, parse_application
from ._output import check_output_path


def _write_private(path: Path, text: str) -> None:
    check_output_path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as output:
        output.write(text)


def main() -> int:
    parser = argparse.ArgumentParser(description="解析标准输入中的申请正文。")
    parser.add_argument("--output", type=Path, required=True,
                        help="私有草稿 JSON 新文件。Git 工作区内须使用忽略路径。")
    parser.add_argument("--raw-output", type=Path,
                        help="私有原始模型回复新文件。Git 工作区内须使用忽略路径。")
    parser.add_argument("--replay", type=Path, help="读取保存的原始模型回复，离线重放。")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="配置文件路径，默认 .env。")
    parser.add_argument("--rules-dir", type=Path, default=Path("rules"), help="规则目录路径，默认 rules。")
    parser.add_argument("--companies-file", type=Path, default=_DEFAULT_COMPANIES_FILE,
                        help="公司对照表路径，默认 .private/companies.tsv。")
    args = parser.parse_args()

    try:
        for path, message in (
            (args.output, "草稿输出文件已存在。"),
            (args.raw_output, "原始回复输出文件已存在。"),
        ):
            if path is not None and (path.exists() or path.is_symlink()):
                print(message, file=sys.stderr)
                return 1
        if args.raw_output is not None and os.path.realpath(args.output) == os.path.realpath(args.raw_output):
            print("草稿与原始回复须使用不同的输出路径。", file=sys.stderr)
            return 1
        check_output_path(args.output)
        if args.raw_output is not None:
            check_output_path(args.raw_output)

        def replay_model(*, system: str, user: str) -> str:
            return args.replay.read_bytes().decode("utf-8")

        def save_reply(reply: str) -> None:
            _write_private(args.raw_output, reply)

        drafts = parse_application(
            sys.stdin.read(),
            config=load_config(env_file=args.env_file) if args.replay is None else None,
            model=replay_model if args.replay is not None else None,
            rules_dir=args.rules_dir,
            companies_file=args.companies_file,
            on_reply=save_reply if args.raw_output is not None else None,
        )
        result = json.dumps([draft.to_dict() for draft in drafts], ensure_ascii=False, indent=2)
        _write_private(args.output, result + "\n")
    except (ConfigError, ModelError, ParseError) as error:
        print(error, file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print("无法读取申请正文或原始回复，或无法写入输出文件。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
