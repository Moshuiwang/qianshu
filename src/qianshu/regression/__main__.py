"""本地生成、核验及运行私有回归考题集。"""

import argparse
import csv
from contextlib import ExitStack, nullcontext
import logging
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from io import StringIO
from pathlib import Path
from pydantic import TypeAdapter
from qianshu.parse.benefits import BenefitCatalog, DEFAULT_CATALOG_FILE

from qianshu.config import ConfigError
from qianshu.parse import system_message
from qianshu.parse.model import DEFAULT_MODEL, ParseModel, configured_effort, configured_model
from qianshu.parse.package_levels import DEFAULT_LEVELS_FILE, _COLUMNS, _SUMMARY, decode_package_levels
from qianshu.parse.protocol import LEGACY_REPLY_STRUCTURE

from . import baseline, tiers
from .ledger import Ledger, locked_directory, transaction
from .batches import capture_requests, common_body, fetch_batch, instant_transport
from .business import DEFAULT_THRESHOLD
from .corpus import bind_attachments, make_plan
from .freeze import freeze, load_exclusions, load_freeze
from .packages import freeze_packages
from .recording import MANIFEST, Recording, SavedReplies, configuration, doc_names, historical_cost, probe_replies, reference_cost
from .runner import RULES, RUN_FILES, evaluate, markdown_report
from qianshu.parse.materials import content_bytes, image_settings

from .storage import PrivateFiles, RegressionError, digest, json_bytes, read_json, staged_output
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

    account = commands.add_parser("account-recording", help="凭登记指纹接入台账启用前的原始真跑费用。")
    account.add_argument("--recording", required=True)
    account.add_argument("--expect-sha256", required=True, help="原录制清单的指纹。")
    account.add_argument("--estimate-usd", required=True)
    account.add_argument("--ledger", default=".private/regression-ledger.json")

    rebuild = commands.add_parser("rebuild-requests", help="从可核验的历史源码、依赖和输入重建逐篇请求。")
    for arg in ("frozen", "expect-sha256", "recording", "benefit-catalog", "evidence", "expect-evidence-sha256", "out"):
        rebuild.add_argument("--" + arg, required=True)

    quick = commands.add_parser("select-quick", help="从已验指纹的考题冻结 25 篇快检。")
    quick.add_argument("--frozen", required=True)
    quick.add_argument("--expect-sha256", required=True)
    quick.add_argument("--labels", required=True)
    quick.add_argument("--out", required=True)

    run = commands.add_parser("run", help="运行冻结资料，录制回复并生成两把量尺。")
    run.add_argument("--mode", choices=("instant", "batch"))
    run.add_argument("--run-id", help="Trace 内唯一的稳定运行号，试通也须登记。")
    run.add_argument("--estimate-usd", help="整次运行的费用估算，预占乘以 1.3。")
    run.add_argument("--ledger", default=".private/regression-ledger.json")
    run.add_argument("--reconcile-batch-id", help="续取受理未知的批次，全篇关联键核实后绑定。")
    run.add_argument("--trial", type=int, choices=(1, 2, 3), help="仅试通选定档位的前 1 至 3 篇。")
    run.add_argument("--resume", action="store_true", help="从已登记批次续取。")
    run.add_argument("--live", action="store_true", help="显式允许调用现有模型接口。")
    run.add_argument("--replaces-recording", help="本次放行运行替代的作废录制目录。")
    replay = commands.add_parser("replay", help="离线重放已录制回复。")
    replay.add_argument("--reconstruction", help="私有历史请求重建证据。")
    replay.add_argument("--expect-reconstruction-sha256")
    replay.add_argument("--trial", type=int, choices=(1, 2, 3))
    replay.add_argument("--recording", required=True)
    replay.add_argument("--legacy-recording", action="store_true",
                        help="将旧结构录制定点投影到薄协议，仅用于离线诊断。")
    imported = commands.add_parser("import", help="导入 probe 按用户消息指纹保存的回复。")
    imported.add_argument("--probe", required=True)
    imported.add_argument("--model-id", required=True, help="记录该历史回复所用的模型标识。")
    for command in (run, replay, imported):
        command.add_argument("--attachment-manifest", help="绑定冻结指纹的私有附件清单。")
        command.add_argument("--frozen", required=True, help="冻结考题、盲测集或节目包补充集。")
        command.add_argument("--expect-sha256", required=True, help="冻结或 verify 返回的预期冻结指纹。")
        command.add_argument("--out", required=True)
        command.add_argument("--selection", help="私有快检选择清单。")
        command.add_argument("--benefit-catalog", help="私有优惠目录快照。")
        command.add_argument("--package-levels", help="本次核对使用的私有销售品 CSV 快照。")
        command.add_argument("--threshold", type=int, help="业务配对最低分。重放默认沿用来源录制配置。")
        command.add_argument("--legacy-companies", action="store_true", default=None,
                             help="按无表头两列或三列 TSV 读取冻结的公司对照表。")
    for command in (run, imported):
        command.add_argument("--run-day", type=date.fromisoformat, help="销售品核对的运行日，基线沿用对照日。")
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


def _decode_snapshot(data: bytes):
    levels = decode_package_levels(data)
    if levels.offers:
        return levels
    # 线上读取失败也返回空表；回归只接受表头完整且仅含零行或汇总行的空表。
    try:
        reader = csv.DictReader(StringIO(data.decode("utf-8-sig"), newline=""), strict=True)
        header = reader.fieldnames or []
        if (_COLUMNS.issubset(header) and len(header) == len(set(header))
                and all(None not in row and None not in row.values() and row["record_type"] in _SUMMARY
                        for row in reader)):
            return levels
    except (UnicodeError, csv.Error):
        pass
    raise RegressionError("销售品快照格式无效。")


def _run(files: PrivateFiles, args, out: Path, model: ParseModel | None, stack: ExitStack) -> None:
    snapshot = load_freeze(files, files.argument(args.frozen))
    frozen_sha = parent_sha = snapshot["fingerprint"]
    attachment_selection = False
    if args.selection:
        selection = read_json(files.read(files.argument(args.selection)))
        attachment_selection = selection.get("purpose") == "attachment-check" and bool(args.attachment_manifest)
        if attachment_selection:
            snapshot = tiers.attachment_subset(snapshot, selection)
        else:
            snapshot = tiers.project(snapshot, selection)
    if snapshot["fingerprint"] != args.expect_sha256:
        raise RegressionError("预期冻结指纹与实际冻结指纹不一致。")
    if getattr(args, "trial", None):
        snapshot = tiers.trial(snapshot, args.trial)
        parent_sha = snapshot["fingerprint"]
    if args.attachment_manifest:
        snapshot = bind_attachments(files, snapshot, files.argument(args.attachment_manifest), frozen_sha)
        if attachment_selection and any(not any(b.image for b in doc["additional_blocks"])
                                        or any(b.missing for b in doc["additional_blocks"])
                                        for doc in snapshot["documents"].values()):
            raise RegressionError("附件快检的所选篇须有截图且原件全部可读。")
    source = origin_run_id = effort = None
    legacy_recording = getattr(args, "legacy_recording", False)
    source_system_sha = None
    settings = {}
    if args.command == "replay":
        saved = SavedReplies(files, files.argument(args.recording), legacy_recording=legacy_recording)
        manifest = saved.manifest
        prior = manifest["configuration"]
        if prior.get("attachment_manifest_sha256") != snapshot.get("attachment_manifest_sha256"):
            raise RegressionError("附件清单与来源录制不一致。")
        if prior["freeze_sha256"] not in {snapshot["fingerprint"], parent_sha}:
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
            if any(b.image for doc in snapshot["documents"].values() for b in doc.get("additional_blocks", ())):
                settings = image_settings(settings)
            model_id = settings.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
            effort = configured_effort(settings)
            origin = "live"
        else:
            model_id, origin = "injected", "injected"

    rules = files.repo / "rules"
    if baseline.binding:
        rules, versions = baseline.binding
    config = configuration(snapshot, rules, model_id, threshold, legacy, release, retries, effort=effort)
    if "attachment_manifest_sha256" in snapshot:
        config["attachment_manifest_sha256"] = snapshot["attachment_manifest_sha256"]
    if baseline.binding:
        config.update(versions)
    reconstruction = None
    catalog_path = files.argument(args.benefit_catalog or DEFAULT_CATALOG_FILE)
    if origin == "replay":
        if args.reconstruction or args.expect_reconstruction_sha256:
            if not baseline.binding or not args.reconstruction or not args.expect_reconstruction_sha256:
                raise RegressionError("历史证据须在标签解析树上按指纹核验。")
            reconstruction = baseline.pinned(files, args.reconstruction, args.expect_reconstruction_sha256)
            if reconstruction["recording_sha256"] != digest(files.read(saved.directory / MANIFEST)):
                raise RegressionError("重建证据与录制清单不同。")
        if "benefit_catalog_sha256" not in prior and not args.benefit_catalog:
            raise RegressionError("历史录制缺优惠目录指纹，须提供经独立重建核实的快照。")
        if not args.benefit_catalog:
            catalog_path = saved.directory / DEFAULT_CATALOG_FILE.name
    try:
        catalog_bytes = files.read(catalog_path)
    except FileNotFoundError:
        if args.benefit_catalog or origin == "replay":
            raise RegressionError("找不到指定的优惠目录快照。") from None
        catalog_bytes = json_bytes({})
    catalog = TypeAdapter(BenefitCatalog).validate_json(catalog_bytes, strict=True)
    config["benefit_catalog_sha256"] = digest(catalog_bytes)
    if origin == "replay" and prior.get("benefit_catalog_sha256", digest(catalog_bytes)) != digest(catalog_bytes):
        raise RegressionError("优惠目录与来源录制指纹不符。")
    if origin == "replay" and not prior.get("benefit_catalog_sha256"):
        if reconstruction:
            if reconstruction["benefit_catalog_sha256"] != digest(catalog_bytes):
                raise RegressionError("历史重建的优惠目录指纹不符。")
        elif baseline.binding:
            raise RegressionError("历史录制缺优惠目录证据，不能纳入双版本基线。")
        else:
            config["historical_catalog_unverified"] = True
    if origin == "replay" and prior.get("historical_catalog_unverified"):
        config["historical_catalog_unverified"] = True
    recorded_levels = origin == "replay" and "package_levels_sha256" in prior
    levels_path = files.argument(args.package_levels) if args.package_levels else None
    run_day = getattr(args, "run_day", None) or datetime.now(UTC).date()
    if recorded_levels:
        recorded_day = manifest.get("run_day", prior.get("run_day"))
        if recorded_day:
            run_day = date.fromisoformat(recorded_day)
        if prior["package_levels_sha256"] and not args.package_levels:
            levels_path = saved.directory / DEFAULT_LEVELS_FILE.name
    try:
        levels_data = files.read(levels_path) if levels_path is not None else None
    except FileNotFoundError:
        raise RegressionError("指定或录制的销售品快照不存在。") from None
    config["package_levels_sha256"] = digest(levels_data) if levels_data is not None else None
    if recorded_levels and config["package_levels_sha256"] != prior["package_levels_sha256"]:
        raise RegressionError("销售品快照与录制指纹不一致。")
    package_levels = _decode_snapshot(levels_data) if levels_data is not None else None
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
    wires = {}
    if origin == "replay" and baseline.binding:
        if prior.get("historical_catalog_unverified"):
            raise RegressionError("未经核实的历史目录仅可用于诊断。")
        wires = capture_requests(snapshot, rules, legacy, {"ANTHROPIC_MODEL": model_id, "ANTHROPIC_EFFORT": effort})
        for token, wire in wires.items():
            params = read_json(wire.encode())
            saved.for_attempts(token, digest(content_bytes(params["messages"][0]["content"])), digest(params["system"].encode()))
            expected = reconstruction["common_sha256"].get(token) if reconstruction else saved.request_sha256
            actual = digest(common_body(wire).encode())
            if expected != actual or saved.request_sha256 not in (None, actual):
                raise RegressionError("标签请求与原录制或独立重建的业务字节不同。")
    names = doc_names(snapshot)
    mode = (manifest.get("mode", "instant") if origin == "replay" else "instant" if origin != "live" else
            getattr(args, "mode", None) or ("instant" if args.selection else "batch"))
    if mode not in {"instant", "batch"}:
        raise RegressionError("录制的调用方式无效。")
    if origin == "live":
        if args.reconcile_batch_id and (not args.resume or mode != "batch"):
            raise RegressionError("批次对账号仅限原运行的只读续取。")
        if not args.run_id or not args.estimate_usd:
            raise RegressionError("真跑须提供稳定运行号和预计费用。")
        ledger = Ledger.for_trace(files, args.ledger, stack)
        shared = ledger.files
        if mode == "batch":
            run_lock = ledger.path.with_name("run-" + digest(args.run_id.encode()))
            stack.enter_context(locked_directory(shared, run_lock))
            stack.enter_context(locked_directory(files, Path("output-" + digest(str(out).encode()))))
            files._check_ignore(Path(".private") / out, directory=True)
            with files._directory(out.parent) as folder:
                try:
                    os.stat(out.name, dir_fd=folder, follow_symlinks=False)
                except FileNotFoundError:
                    existing = False
                else:
                    existing = True
                    if not args.resume:
                        raise RegressionError("批量输出目录已存在。")
            if args.resume:
                with transaction(shared, ledger.path) as runs:
                    row = runs.get(args.run_id)
                    if not row or row["mode"] != mode:
                        raise RegressionError("台账没有匹配的批量提交意图。")
                    if not args.today:
                        today = date.fromisoformat(row["today"])
                        config["today"] = today.isoformat()
                    if args.run_day and args.run_day.isoformat() != row["run_day"]:
                        raise RegressionError("续取运行日与登记不符。")
                    run_day = date.fromisoformat(row["run_day"])
                if existing:
                    done = SavedReplies(files, out).manifest
                    report_raw = files.read(out / "public-report.json")
                    if (done["run_id"] != args.run_id or done["origin"] != "live" or done["mode"] != mode
                        or done["configuration"] != config or digest(report_raw) != done["public_report_sha256"]
                        or row.get("config_sha256") != digest(json_bytes(config))
                        or any(digest(files.read(out / name)) != sha for name, sha in
                               (done["artifact_sha256"] | done["extra_sha256"]).items())):
                        raise RegressionError("已发布批量录制校验失败。")
                    ledger.record_cost(args.run_id, read_json(report_raw)["cost"])
                    print("已核验原运行的完整批量录制。")
                    return
            wires = capture_requests(snapshot, rules, legacy, settings)
            source = fetch_batch(ledger, args.run_id, args.estimate_usd, config, wires, settings,
                                 run_day=run_day.isoformat(), resume=args.resume, reconcile_id=args.reconcile_batch_id)
        elif args.resume:
            raise RegressionError("即时运行受理未知时禁止续提。")
    create_output = staged_output if origin == "live" and mode == "batch" else type(files).new_output
    with create_output(files, out, (*RUN_FILES, MANIFEST, DEFAULT_LEVELS_FILE.name,
                                DEFAULT_CATALOG_FILE.name, *names.values())) as output:
        if levels_data is not None:
            output.write(DEFAULT_LEVELS_FILE.name, levels_data)
        recording = Recording(output, names, config, model_id, model=model, source=source,
                              origin=origin, origin_run_id=origin_run_id, replaces_run_id=replaces_run_id,
                              source_system_sha=source_system_sha)
        recording.mode = mode
        recording.tier = "attachment-check" if attachment_selection else "quick" if args.selection else ("full" if snapshot["dataset"] == "exam" else "holdout")
        if origin == "live":
            recording.run_id = args.run_id
            recording.ledger = ledger
            if mode == "instant":
                recording.model = configured_model(settings, on_result=recording.on_result, max_retries=0)
                ledger.reserve(args.run_id, mode, args.estimate_usd, digest(json_bytes(config)))
        recording.requests = wires
        if reconstruction:
            recording.reconstruction_sha256 = args.expect_reconstruction_sha256
        if origin == "live" and mode == "batch":
            recording.batch_seconds = source.batch_seconds
        elif origin == "replay":
            recording.batch_seconds = manifest.get("batch_seconds")
        transport = instant_transport(recording) if origin == "live" and mode == "instant" else nullcontext()
        with transport:
            report, details = evaluate(snapshot, None, rules, legacy_companies=legacy, threshold=threshold,
                                       recording=recording, package_levels=package_levels, today=today,
                                       package_run_day=run_day, benefit_catalog=catalog)
        counts = report["business"]["counts"]
        void = release and recording.unfinished_calls
        if void:
            reasons = report["business"]["reply_parse_failure_reasons"]
            report = {key: report[key] for key in (
                "freeze_sha256", "rules_sha256", "samples", "documents", "evidence_location", "package_levels_supplied",
                "full_document_review", "full_document_review_reasons",
            )}
            report["business"] = {
                **{key: config[key] for key in ("pairing_version", "pairing_sha256", "measure_sha256")},
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
        report["cost"] = reference_cost(model_id, mode, recording.usage_totals, recording.missing_usage,
                                        live=origin == "live")
        report["tier"] = recording.tier
        report["attempt_failures"] = dict(recording.attempt_failures)
        output.write(DEFAULT_CATALOG_FILE.name, catalog_bytes)
        report["batch_seconds"] = recording.batch_seconds
        report["request_set_sha256"] = recording.request_set_sha256()
        report["package_run_day"] = run_day.isoformat()
        public = json_bytes(report)
        extra = {"mismatches.json": json_bytes(details), "public-report.md": markdown_report(report)}
        for name, raw in extra.items():
            output.write(name, raw)
        extra[DEFAULT_CATALOG_FILE.name] = catalog_bytes
        if levels_data is not None:
            extra[DEFAULT_LEVELS_FILE.name] = levels_data
        recording.extra_hashes = {name: digest(raw) for name, raw in extra.items()}
        output.write("public-report.json", public)
        recording.complete(digest(public), threshold, run_day=run_day.isoformat())
    print(f"已统计 {report['samples']} 条冻结样例，生成计数及私有明细。")


def main(argv: list[str] | None = None, *, model: ParseModel | None = None) -> int:
    args = _arguments(argv)
    try:
        files = PrivateFiles(Path.cwd())
        with files.open(), ExitStack() as stack:
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
            elif args.command == "account-recording":
                ledger = Ledger.for_trace(files, args.ledger, stack)
                manifest, usage = historical_cost(files, files.argument(args.recording), args.expect_sha256)
                ledger.carry_recording(manifest, args.expect_sha256, usage, args.estimate_usd)
                print("已核对并登记历史运行费用。")
            elif args.command == "rebuild-requests":
                baseline.rebuild(files, args, out)
            elif args.command == "select-quick":
                snapshot = load_freeze(files, files.argument(args.frozen))
                if snapshot["fingerprint"] != args.expect_sha256:
                    raise RegressionError("父冻结指纹不符。")
                selection = tiers.select(snapshot, read_json(files.read(files.argument(args.labels))))
                with files.new_output(out, ("selection.json",)) as output:
                    output.write("selection.json", json_bytes(selection))
                print(tiers.project(snapshot, selection)["fingerprint"])
            elif args.command == "verify":
                snapshot = load_freeze(files, files.argument(args.frozen), verify_plan=True, verify_sources=args.sources)
                print(snapshot["fingerprint"])
            elif args.command == "summarize":
                summarize(files, [files.argument(path) for path in args.recording], out,
                          diagnostic_replay=args.diagnostic_replay)
                print("已生成多次运行汇总。")
            else:
                previous = logging.root.manager.disable
                logging.disable(logging.CRITICAL)
                try:
                    _run(files, args, out, model, stack)
                finally:
                    logging.disable(previous)
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
