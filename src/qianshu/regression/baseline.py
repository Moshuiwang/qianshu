"""以 v0.3.0 的解析实现和当前候选的回归工具启动一次基线命令。"""

import argparse
from importlib import util
from importlib.metadata import version
import subprocess
import sys
import tomllib
from pathlib import Path

from .storage import RegressionError, digest, json_bytes, read_json

binding = None


def revision(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL).strip()


def bind(parser_repo, historical_commit=None):
    import qianshu
    import qianshu.regression

    tool = Path(__file__).resolve().parents[3]
    parser = Path(parser_repo).resolve(strict=True)
    for root in (tool, parser):
        if revision(root, "status", "--porcelain", "--untracked-files=all"):
            raise RegressionError("基线源码、规则或依赖树不干净。")
    tag = historical_commit.encode() if historical_commit else revision(tool, "rev-parse", "v0.3.0^{commit}")
    if revision(parser, "rev-parse", "HEAD") != tag or any(
        name.startswith("qianshu.") and not name.startswith("qianshu.regression") for name in sys.modules
    ):
        raise RegressionError("解析模块已导入或解析树版本不符。")
    spec = util.spec_from_file_location("qianshu", parser / "src/qianshu/__init__.py")
    package = util.module_from_spec(spec)
    sys.modules["qianshu"] = package
    spec.loader.exec_module(package)
    package.regression = qianshu.regression
    qianshu.regression.__path__ = [str(tool / "src/qianshu/regression")]
    return tool, parser


def provenance(tool, parser):
    loaded = {}
    for name, module in sys.modules.items():
        if (name != "qianshu" and not name.startswith("qianshu.")) or not getattr(module, "__file__", None):
            continue
        path = Path(module.__file__).resolve()
        root = tool if name.startswith("qianshu.regression") else parser
        if not path.is_relative_to(root / "src/qianshu"):
            raise RegressionError("基线模块来源不符。")
        loaded[name] = digest(path.read_bytes())
    lock = (parser / "uv.lock").read_bytes()
    packages = tomllib.loads(lock.decode())["package"]
    deps = {key: version(key) for key in ("anthropic", "httpx", "pydantic", "pydantic-core", "jiter")}
    if any(not any(p["name"] == k and p["version"] == v for p in packages) for k, v in deps.items()):
        raise RegressionError("解释器依赖与解析工作树的锁文件不符。")
    return {"tool_commit": revision(tool, "rev-parse", "HEAD").decode(),
            "parse_commit": revision(parser, "rev-parse", "HEAD").decode(),
            "dependencies_sha256": digest(json_bytes(deps)),
            "loaded_sha256": digest(json_bytes(loaded)), "lock_sha256": digest(lock)}


def pinned(files, path, checksum):
    raw = files.read(files.argument(path))
    if digest(raw) != checksum:
        raise RegressionError("私有证据与独立登记的指纹不符。")
    return read_json(raw)


def rebuild(files, args, out):
    """在原录制的干净源码及依赖上重建，不调用模型。"""
    from .batches import capture_requests, common_body
    from .freeze import load_freeze
    from .recording import MANIFEST, SavedReplies

    if not binding:
        raise RegressionError("历史重建须从双版本入口启动。")
    rules, versions = binding
    archive = pinned(files, args.evidence, args.expect_evidence_sha256)
    for key in ("parse_commit", "lock_sha256", "dependencies_sha256"):
        if archive.get(key) != versions[key]:
            raise RegressionError("历史源码或依赖与独立档案不符。")
    snapshot = load_freeze(files, files.argument(args.frozen))
    saved = SavedReplies(files, files.argument(args.recording))
    prior = saved.manifest["configuration"]
    checksum = digest(files.read(saved.directory / MANIFEST))
    source = rules.parent / "src/qianshu"
    code = {p.relative_to(source).as_posix(): digest(p.read_bytes()) for p in source.rglob("*.py")}
    if (archive["recording_sha256"] != checksum or prior["code_sha256"] != digest(json_bytes(code))
        or prior["freeze_sha256"] != snapshot["fingerprint"] or snapshot["fingerprint"] != args.expect_sha256
        or saved.manifest["origin"] != "live" or not saved.manifest["source_code_measured"]
        or archive["benefit_catalog_sha256"] != digest(files.read(files.argument(args.benefit_catalog)))):
        raise RegressionError("历史录制、已运行源码、题集或优惠目录证据不一致。")
    settings = {"ANTHROPIC_MODEL": saved.manifest["model"], "ANTHROPIC_EFFORT": prior["effort"]}
    wires = capture_requests(snapshot, rules, prior["legacy_companies"], settings)
    for token, wire in wires.items():
        params = read_json(wire.encode())
        saved.for_attempts(token, digest(params["messages"][0]["content"].encode()), digest(params["system"].encode()))
        if saved.request_sha256 is not None and saved.request_sha256 != digest(common_body(wire).encode()):
            raise RegressionError("历史重建与实录请求的业务字节不同。")
    result = json_bytes({"recording_sha256": checksum, "benefit_catalog_sha256": archive["benefit_catalog_sha256"],
                         "archive_sha256": args.expect_evidence_sha256, "versions": versions,
                         "common_sha256": {k: digest(common_body(v).encode()) for k, v in wires.items()}})
    with files.new_output(out, ("reconstruction.json",)) as output:
        output.write("reconstruction.json", result)
    print(digest(result))


def main():
    args = argparse.ArgumentParser()
    args.add_argument("--parser-tree", required=True)
    args.add_argument("--historical-commit", help="独立档案中的完整提交号，仅用于离线重建。")
    options, rest = args.parse_known_args()
    if options.historical_commit and rest[:1] != ["rebuild-requests"]:
        args.error("历史源码入口仅可离线重建。")
    try:
        tool, parser = bind(options.parser_tree, options.historical_commit)
        from . import __main__ as cli
        from . import baseline
        baseline.binding = (parser / "rules", provenance(tool, parser))
        return cli.main(rest)
    except (OSError, subprocess.SubprocessError, RegressionError):
        print("双版本基线启动核验失败。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
