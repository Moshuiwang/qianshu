"""本地生成、核验及运行私有回归考题集。"""

import argparse
import logging
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from qianshu.config import ConfigError
from qianshu.parse import system_message
from qianshu.parse.model import DEFAULT_MODEL, ParseModel, configured_effort, configured_model
from qianshu.parse.protocol import LEGACY_REPLY_STRUCTURE

from .business import DEFAULT_THRESHOLD
from .corpus import make_plan
from .freeze import freeze, load_exclusions, load_freeze
from .packages import freeze_packages
from .recording import MANIFEST, Recording, SavedReplies, configuration, doc_names, probe_replies
from .runner import RULES, RUN_FILES, evaluate, markdown_report
from .storage import PrivateFiles, RegressionError, digest, json_bytes, read_json
from .summary import summarize

_MODEL_SETTINGS = (
    "AI_PLATFORM", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "ANTHROPIC_BASE_URL", "ANTHROPIC_EFFORT",
)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse 默认把部分参数原值写到 stderr，可能包含私有路径。
        self.exit(2, "回归参数无效，请查看 --help。\n")


def _arguments(argv: list[str] | None):
    parser = _Parser(description="生成、冻结和运行千树Bot本地解析回归考题。")
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="生成私有考题或盲测抽样计划，或节目包候选池。")
    plan.add_argument("--corpus", required=True, help="语料根目录，须为 .private 或其子目录。")
    plan.add_argument("--out", required=True, help="新的私有输出目录。")
    plan.add_argument("--dataset", choices=("blind", "packages"), default="exam")
    size = plan.add_mutually_exclusive_group()
    size.add_argument("--size", type=int, default=150, help="考题和盲测的分层抽样规模，默认 150。")
    size.add_argument("--all", action="store_true", help="审定并运行全部考题或盲测候选。")

    build = commands.add_parser("freeze", help="冻结已独立审定的考题。")
    build.add_argument("--corpus", required=True)
    build.add_argument("--plan", required=True)
    build.add_argument("--reviews", required=True)
    build.add_argument("--companies", required=True)
    build.add_argument("--out", required=True)
    build.add_argument("--dataset", choices=("exam", "blind"), default="exam")

    packages = commands.add_parser("freeze-packages", help="冻结私有节目包补充审定资料。")
    packages.add_argument("--corpus", required=True)
    packages.add_argument("--plan", required=True)
    packages.add_argument("--reviews", required=True)
    packages.add_argument("--companies", required=True)
    packages.add_argument("--out", required=True)
    for command in (plan, build, packages):
        command.add_argument("--exclude-frozen", action="append", default=[])
        command.add_argument("--exclude-holdout-list", help="对方留集的整篇原稿指纹清单。")

    verify = commands.add_parser("verify", help="核验冻结产物的指纹、引用和抽样分层。")
    verify.add_argument("--frozen", required=True)
    verify.add_argument("--sources", action="store_true", help="一并检查冻结时的其余私有输入文件。")

    run = commands.add_parser("run", help="运行冻结资料，录制回复并生成两把量尺。")
    run.add_argument("--live", action="store_true", help="显式允许调用现有模型接口。")
    run.add_argument("--replaces-recording", help="本次放行运行替代的作废录制目录。")
    replay = commands.add_parser("replay", help="离线重放已录制回复。")
    replay.add_argument("--recording", required=True)
    replay.add_argument("--legacy-recording", action="store_true",
                        help="将旧结构录制定点投影到薄协议，仅用于离线诊断。")
    imported = commands.add_parser("import", help="导入 probe 按用户消息指纹保存的回复。")
    imported.add_argument("--probe", required=True)
    imported.add_argument("--model-id", required=True, help="记录该历史回复所用的模型标识。")
    for command in (run, replay, imported):
        command.add_argument("--frozen", required=True, help="冻结考题、盲测集或节目包补充集。")
        command.add_argument("--expect-sha256", required=True, help="冻结或 verify 返回的预期冻结指纹。")
        command.add_argument("--out", required=True)
        command.add_argument("--threshold", type=int, help="业务配对最低分。重放默认沿用来源录制配置。")
        command.add_argument("--legacy-companies", action="store_true", default=None,
                             help="按无表头两列或三列 TSV 读取冻结的公司对照表。")
    for command in (run, imported):
        command.add_argument("--today", type=date.fromisoformat, help="R1 使用的 UTC 日期（YYYY-MM-DD）。")
        command.add_argument("--release", action="store_true", help="有未完成调用时作废本次放行运行。")
        command.add_argument("--retries", type=int, help="调用补试上限，放行默认 1，诊断默认 0。")

    summary = commands.add_parser("summarize", help="汇总同一配置的多次运行。")
    summary.add_argument("--recording", required=True, action="append")
    summary.add_argument("--out", required=True)
    summary.add_argument("--diagnostic-replay", action="store_true", help="仅汇总旧结构离线诊断重放。")
    return parser.parse_args(argv)


_LEGACY_REVISION = "4fb3cb5d52a394f8cd7990572ca72f8009278fea"


def _legacy_fingerprints(repo: Path) -> tuple[str, str]:
    try:
        blobs = {name: subprocess.check_output(
            ["git", "--no-replace-objects", "cat-file", "blob", f"{_LEGACY_REVISION}:rules/{name}"],
            cwd=repo, stderr=subprocess.DEVNULL,
        ) for name in RULES}
    except (OSError, subprocess.CalledProcessError):
        raise RegressionError("无法读取受信旧版本的规则文件。") from None
    hashes = {name: digest(blob) for name, blob in blobs.items()}
    system = system_message(*(blobs[name].decode("utf-8") for name in (
        "parse-prompt.md", "parse-rules.md", "parse-options.tsv",
    )))
    return digest(json_bytes(hashes)), digest(system.encode())


def _run(files: PrivateFiles, args, out: Path, model: ParseModel | None) -> None:
    snapshot = load_freeze(files, files.argument(args.frozen))
    if snapshot["fingerprint"] != args.expect_sha256:
        raise RegressionError("预期冻结指纹与实际冻结指纹不一致。")
    source = origin_run_id = effort = None
    legacy_recording = getattr(args, "legacy_recording", False)
    source_system_sha = None
    settings = {}
    if args.command == "replay":
        saved = SavedReplies(files, files.argument(args.recording), legacy_recording=legacy_recording)
        manifest = saved.manifest
        prior = manifest["configuration"]
        if prior["freeze_sha256"] != snapshot["fingerprint"]:
            raise RegressionError("录制与本次重放的冻结指纹不一致。")
        model_id = manifest["model"]
        origin_run_id = manifest["origin_run_id"] or manifest["run_id"]
        if not legacy_recording and saved.reply_structure == LEGACY_REPLY_STRUCTURE:
            raise RegressionError("旧回复结构须显式指定 --legacy-recording。")
        source = saved
        legacy = prior["legacy_companies"] if args.legacy_companies is None else args.legacy_companies
        threshold = manifest["threshold"] if args.threshold is None else args.threshold
        release, retries = prior["release"], prior["retries"]
        effort = prior.get("effort")
        today = date.fromisoformat(prior["today"]) if prior.get("today") else None
        origin = "replay"
    else:
        today = args.today
        if today is None and args.command == "run":
            today = datetime.now(UTC).date()
        legacy = bool(args.legacy_companies)
        threshold = DEFAULT_THRESHOLD if args.threshold is None else args.threshold
        release = args.release
        retries = int(release) if args.retries is None else args.retries
        if retries < 0:
            raise RegressionError("调用补试上限须为非负整数。")
        if args.command == "import":
            model_id = args.model_id
            source = probe_replies(files, files.argument(args.probe), model_id)
            origin = "probe"
        elif model is None:
            if not args.live:
                raise RegressionError("实际调用模型须显式指定 --live。")
            settings = {key: os.environ[key] for key in _MODEL_SETTINGS if key in os.environ}
            model_id = settings.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
            effort = configured_effort(settings)
            origin = "live"
        else:
            model_id, origin = "injected", "injected"

    rules = files.repo / "rules"
    config = configuration(snapshot, rules, model_id, threshold, legacy, release, retries, effort=effort)
    config["today"] = today.isoformat() if today else None
    if legacy_recording:
        source_rules_sha, source_system_sha = _legacy_fingerprints(files.repo)
        if prior.get("source_rules_sha256", prior["rules_sha256"]) != source_rules_sha:
            raise RegressionError("旧录制的系统消息须使用录制时的规则文件重建。")
        config["source_rules_sha256"] = source_rules_sha
        config["source_reply_structure"] = LEGACY_REPLY_STRUCTURE
    replaces_run_id = None
    if getattr(args, "replaces_recording", None):
        prior_dir = files.argument(args.replaces_recording)
        prior_manifest = SavedReplies(files, prior_dir).manifest
        prior_report = files.read(prior_dir / "public-report.json")
        if args.today is None and prior_manifest["configuration"].get("today"):
            today = date.fromisoformat(prior_manifest["configuration"]["today"])
            config["today"] = today.isoformat()
        if (
            not release or prior_manifest["origin"] == "replay"
            or prior_manifest["configuration"] != config
            or digest(prior_report) != prior_manifest["public_report_sha256"]
            or read_json(prior_report).get("void") is not True
        ):
            raise RegressionError("替代放行须引用相同配置下作废的原始录制。")
        replaces_run_id = prior_manifest["run_id"]
    names = doc_names(snapshot)
    with files.new_output(out, (*RUN_FILES, MANIFEST, *names.values())) as output:
        recording = Recording(output, names, config, model_id, model=model, source=source,
                              origin=origin, origin_run_id=origin_run_id, replaces_run_id=replaces_run_id,
                              source_system_sha=source_system_sha)
        if origin == "live":
            model_options = {"max_retries": 0} if release else {}
            recording.model = configured_model(settings, on_result=recording.on_result, **model_options)
        previous = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        try:
            report, details = evaluate(snapshot, None, rules, legacy_companies=legacy, threshold=threshold,
                                       recording=recording, today=today)
        finally:
            logging.disable(previous)
        counts = report["business"]["counts"]
        void = release and recording.unfinished_calls
        if void:
            reasons = report["business"]["reply_parse_failure_reasons"]
            report = {key: report[key] for key in (
                "freeze_sha256", "rules_sha256", "samples", "documents", "evidence_location",
                "full_document_review", "full_document_review_reasons",
            )}
            report["business"] = {
                "counts": {"reply_parse_failed": counts["reply_parse_failed"]},
                "reply_parse_failure_reasons": reasons,
            }
        report.update(configuration_sha256=digest(json_bytes(config)), void=void, usage=recording.public_usage())
        if legacy_recording:
            report["diagnostic_replay"] = {
                "source_reply_structure": config["source_reply_structure"],
                "reply_structure": config["reply_structure"],
                "source_configuration_sha256": digest(json_bytes(prior)),
            }
        public = json_bytes(report)
        output.write("mismatches.json", json_bytes(details))
        output.write("public-report.md", markdown_report(report))
        output.write("public-report.json", public)
        recording.complete(digest(public), threshold)
    print(f"已统计 {report['samples']} 条冻结样例，生成计数及私有明细。")


def main(argv: list[str] | None = None, *, model: ParseModel | None = None) -> int:
    args = _arguments(argv)
    try:
        files = PrivateFiles(Path.cwd())
        with files.open():
            out = files.argument(args.out) if hasattr(args, "out") else None
            if args.command in ("plan", "freeze", "freeze-packages"):
                dataset = "packages" if args.command == "freeze-packages" else args.dataset
                if args.command == "plan" and dataset == "exam" and (args.exclude_frozen or args.exclude_holdout_list):
                    raise RegressionError("生成排除式抽样计划须指定留集类别。")
                peer_list = files.argument(args.exclude_holdout_list) if args.exclude_holdout_list else None
                exclusions = load_exclusions(files, dataset, [files.argument(path) for path in args.exclude_frozen], peer_list)
                corpus = files.argument(args.corpus, allow_root=True)
                if args.command == "plan":
                    plan = make_plan(
                        files, corpus, None if args.all else args.size, dataset=dataset, exclusions=exclusions,
                    )
                    with files.new_output(out, ("plan.json",)) as output:
                        output.write("plan.json", json_bytes(plan))
                    if dataset == "packages":
                        print(f"节目包候选池 {plan['candidate_count']} 条。")
                    else:
                        print(f"候选 {plan['candidate_count']} 条，目标 {plan['size']} 条。")
                    return 0
                plan_path = files.argument(args.plan)
                reviews = files.argument(args.reviews)
                companies = files.argument(args.companies)
                if args.command == "freeze":
                    checksum = freeze(files, corpus, plan_path, reviews, companies, out,
                                      dataset=dataset, exclusions=exclusions)
                else:
                    checksum = freeze_packages(files, corpus, plan_path, reviews, companies, out, exclusions=exclusions)
                print(checksum)
            elif args.command == "verify":
                snapshot = load_freeze(files, files.argument(args.frozen), verify_plan=True, verify_sources=args.sources)
                print(snapshot["fingerprint"])
            elif args.command == "summarize":
                summarize(files, [files.argument(path) for path in args.recording], out,
                          diagnostic_replay=args.diagnostic_replay)
                print("已生成多次运行汇总。")
            else:
                _run(files, args, out, model)
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
