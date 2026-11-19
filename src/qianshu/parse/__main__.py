"""从标准输入读取申请，将草稿写入仅当前用户可读的新文件。"""

import argparse
import json
import os
import sys
from pathlib import Path

from qianshu.config import ConfigError, load_config

from . import ModelError, ParseError, parse_application


def main() -> int:
    parser = argparse.ArgumentParser(description="解析标准输入中的申请正文。")
    parser.add_argument("--output", type=Path, required=True, help="私有草稿 JSON 文件，必须是新路径。")
    args = parser.parse_args()

    if args.output.exists():
        print("草稿输出文件已存在。", file=sys.stderr)
        return 1
    try:
        drafts = parse_application(sys.stdin.read(), config=load_config())
        result = json.dumps([draft.to_dict() for draft in drafts], ensure_ascii=False, indent=2)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(result + "\n")
    except (ConfigError, ModelError, ParseError) as error:
        print(error, file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print("无法读取申请正文或写入草稿文件。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
