"""汇总同一冻结和运行配置下的多次结果。"""

from fractions import Fraction
from pathlib import Path

from qianshu.parse.evidence import STRUCTURE_REASONS

from .business import COUNT_LABELS, METRIC_LABELS
from .recording import USAGE_KEYS, SavedReplies
from .runner import ALIGNMENTS
from .storage import PrivateFiles, RegressionError, digest, json_bytes, read_json

SUMMARY_FILES = ("public-summary.json", "public-summary.md")
ALIGNMENT_LABELS = dict(zip(ALIGNMENTS, ("唯一对齐", "未对齐", "歧义", "模型失败", "解析失败"), strict=True))


def _series(values: list) -> dict:
    comparable = bool(values) and all(value is not None for value in values)
    return {
        "values": [float(value) if isinstance(value, Fraction) else value for value in values],
        "mean": float(sum(values) / len(values)) if comparable else None,
        "min": float(min(values)) if comparable else None,
        "max": float(max(values)) if comparable else None,
    }


def _ratio_value(entry: dict) -> Fraction | None:
    numerator, denominator = entry["numerator"], entry["denominator"]
    if denominator is None or denominator == 0:
        return None
    if type(numerator) is not int or type(denominator) is not int or not 0 <= numerator <= denominator:
        raise RegressionError("汇总输入比例无效。")
    return Fraction(100 * numerator, denominator)


def summarize(files: PrivateFiles, directories: list[Path], out: Path, *, diagnostic_replay: bool = False) -> None:
    reports = []
    config = None
    seen = set()
    fingerprints = set()
    for directory in directories:
        manifest = SavedReplies(files, directory).manifest
        run_id = manifest["origin_run_id"] or manifest["run_id"]
        if run_id in seen:
            raise RegressionError("同一录制及其重放不能计为独立运行。")
        seen.add(run_id)
        raw = files.read(directory / "public-report.json")
        report = read_json(raw)
        legacy_replay = manifest["origin"] == "replay" and "source_reply_structure" in manifest["configuration"]
        if (legacy_replay or bool(manifest["configuration"].get("historical_catalog_unverified"))) != diagnostic_replay:
            raise RegressionError("历史诊断须使用 --diagnostic-replay 单独汇总。")
        config_sha = digest(json_bytes(manifest["configuration"]))
        if digest(raw) != manifest["public_report_sha256"] or config_sha != report["configuration_sha256"]:
            raise RegressionError("汇总输入的报告指纹不一致。")
        if config is not None and config_sha != config:
            raise RegressionError("只能汇总同一冻结、代码、模型和配对配置的运行。")
        if report.get("request_set_sha256") != manifest.get("request_set_sha256"):
            raise RegressionError("报告与录制的请求指纹不同。")
        fingerprints.add((manifest.get("request_set_sha256"), report.get("package_run_day")))
        if len(fingerprints) != 1:
            raise RegressionError("各次录制的业务请求指纹不一致。")
        config = config_sha
        reports.append(report)

    valid = [report for report in reports if not report["void"]]
    metrics = {
        key: _series([_ratio_value(report["business"]["rates"][key]) for report in valid])
        for key in METRIC_LABELS if valid and key in valid[0]["business"]["rates"]
    }
    for key, series in metrics.items():
        series["fractions"] = [
            {part: report["business"]["rates"][key][part] for part in ("numerator", "denominator")}
            for report in valid
        ]
    counts = {
        key: _series([report["business"]["counts"][key] for report in valid])
        for key in COUNT_LABELS if key != "reply_parse_failed" and valid
    }
    counts["reply_parse_failed"] = _series([report["business"]["counts"]["reply_parse_failed"] for report in valid])
    data = {
        "configuration_sha256": config, "runs": len(reports), "valid_runs": len(valid), "void_runs": len(reports) - len(valid),
        "diagnostic_replay": diagnostic_replay,
        "old_alignment": {key: _series([report["sample_alignment"][key] for report in valid]) for key in ALIGNMENTS},
        "business_rates_percent": metrics, "business_counts": counts,
        "usage": {
            "attempts": [report["usage"]["attempts"] for report in reports],
            **{key: [report["usage"][key] for report in reports] for key in USAGE_KEYS},
        },
    }
    if all("full_document_review_reasons" in report for report in reports):
        data["full_document_review"] = {
            key: _series([report["full_document_review"][key] for report in valid])
            for key in ("cards", "policies")
        }
        data["full_document_review_reasons"] = {
            key: _series([report["full_document_review_reasons"][key] for report in valid])
            for key in STRUCTURE_REASONS
        }
    lines = [
        "# 解析回归运行汇总", "", f"- 配置指纹 `{config}`",
        *(["- 旧结构离线诊断重放"] if diagnostic_replay else []),
        f"- 共 {data['runs']} 次，有效 {data['valid_runs']} 次，作废 {data['void_runs']} 次", "",
        "| 指标 | 逐次 | 均值 | 最小 | 最大 |", "| --- | --- | ---: | ---: | ---: |",
    ]

    def show(value) -> str:
        return f"{value:.4f}" if isinstance(value, float) else "不可判定" if value is None else str(value)

    for section, labels in (
        ("old_alignment", ALIGNMENT_LABELS), ("business_rates_percent", METRIC_LABELS),
        ("business_counts", COUNT_LABELS), ("full_document_review", {"cards": "整卡硬拦篇数", "policies": "涉及政策数"}),
        ("full_document_review_reasons", STRUCTURE_REASONS),
    ):
        for key, series in data.get(section, {}).items():
            sequential = " / ".join(map(show, series["values"])) or "—"
            lines.append(f"| {labels[key]} | {sequential} | " +
                         " | ".join(show(series[part]) for part in ("mean", "min", "max")) + " |")
    lines.extend(["", "| 用量 | 逐次 |", "| --- | --- |"])
    for key, label in zip(USAGE_KEYS, ("输入 token", "输出 token", "耗时毫秒"), strict=True):
        values = " / ".join(
            "未采集" if entry["total"] is None else f"{entry['total']}（{entry['captured_attempts']} 次采集）"
            for entry in data["usage"][key]
        )
        lines.append(f"| {label} | {values} |")
    with files.new_output(out, SUMMARY_FILES) as output:
        output.write("public-summary.json", json_bytes(data))
        output.write("public-summary.md", ("\n".join(lines) + "\n").encode())
