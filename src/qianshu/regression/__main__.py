"""本地生成、核验及运行私有回归考题集。"""

import argparse
import logging
import os
import sys
from pathlib import Path

from qianshu.config import ConfigError
from qianshu.parse.model import ParseModel, configured_model

from .corpus import make_plan
from .freeze import freeze, load_freeze
from .runner import RUN_FILES, evaluate, markdown_report
from .storage import PrivateFiles, RegressionError, json_bytes

_MODEL_SETTINGS = ("AI_PLATFORM", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "ANTHROPIC_BASE_URL")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse 默认把部分参数原值写到 stderr，可能包含私有路径。
        self.exit(2, "回归参数无效，请查看 --help。\n")


def _arguments(argv: list[str] | None):
    parser = _Parser(description="生成、冻结和运行千树Bot本地解析回归考题。")
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="生成私有候选配额和同层审定顺序。")
    plan.add_argument("--corpus", required=True, help="语料根目录，须为 .private 或其子目录。")
    plan.add_argument("--out", required=True, help="新的私有输出目录。")
    size = plan.add_mutually_exclusive_group()
    size.add_argument("--size", type=int, default=150, help="分层抽样规模，默认 150。")
    size.add_argument("--all", action="store_true", help="审定并运行全部候选。")

    build = commands.add_parser("freeze", help="冻结已独立审定的考题。")
    build.add_argument("--corpus", required=True)
    build.add_argument("--plan", required=True)
    build.add_argument("--reviews", required=True)
    build.add_argument("--companies", required=True)
    build.add_argument("--out", required=True)

    verify = commands.add_parser("verify", help="核验冻结产物的指纹和引用。")
    verify.add_argument("--frozen", required=True)
    verify.add_argument("--sources", action="store_true", help="一并检查冻结时的私有输入文件。")

    run = commands.add_parser("run", help="用冻结输入运行解析，生成统计及私有明细。")
    run.add_argument("--frozen", required=True)
    run.add_argument("--expect-sha256", required=True, help="freeze 或 verify 返回的预期冻结指纹。")
    run.add_argument("--out", required=True)
    run.add_argument("--live", action="store_true", help="显式允许调用现有模型接口。")
    run.add_argument("--legacy-companies", action="store_true", help="按无表头两列或三列 TSV 读取冻结的公司对照表。")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, model: ParseModel | None = None) -> int:
    args = _arguments(argv)
    try:
        files = PrivateFiles(Path.cwd())
        with files.open():
            out = files.argument(args.out) if hasattr(args, "out") else None
            if args.command == "plan":
                plan = make_plan(files, files.argument(args.corpus, allow_root=True), None if args.all else args.size)
                raw = json_bytes(plan)
                with files.new_output(out, ("plan.json",)) as output:
                    output.write("plan.json", raw)
                print(f"候选 {plan['candidate_count']} 条，目标 {plan['size']} 条。")
            elif args.command == "freeze":
                checksum = freeze(
                    files, files.argument(args.corpus, allow_root=True), files.argument(args.plan), files.argument(args.reviews),
                    files.argument(args.companies), out,
                )
                print(checksum)
            elif args.command == "verify":
                snapshot = load_freeze(files, files.argument(args.frozen), verify_sources=args.sources)
                print(snapshot["fingerprint"])
            else:
                snapshot = load_freeze(files, files.argument(args.frozen))
                if snapshot["fingerprint"] != args.expect_sha256:
                    raise RegressionError("预期冻结指纹与实际冻结指纹不一致。")
                if model is None:
                    if not args.live:
                        raise RegressionError("实际调用模型须显式指定 --live。")
                    model = configured_model({key: os.environ[key] for key in _MODEL_SETTINGS if key in os.environ})
                with files.new_output(out, RUN_FILES) as output:
                    previous = logging.root.manager.disable
                    logging.disable(logging.CRITICAL)
                    try:
                        report, details = evaluate(
                            snapshot, model, files.repo / "rules", legacy_companies=args.legacy_companies,
                        )
                    finally:
                        logging.disable(previous)
                    output.write("mismatches.json", json_bytes(details))
                    output.write("public-report.md", markdown_report(report))
                    output.write("public-report.json", json_bytes(report))
                print(f"已统计 {report['samples']} 条冻结样例，生成计数及私有明细。")
    except RegressionError as error:
        print(error, file=sys.stderr)
        return 1
    except ConfigError:
        print("回归模型配置不可用，请检查调用方提供的配置。", file=sys.stderr)
        return 1
    except Exception:
        # 输入数据和外部异常可能带正文、私有路径或模型回复。
        print("回归输入、路径或产物不可用，请检查私有目录和资料格式。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中断本地回归。", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
