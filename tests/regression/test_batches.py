"""分级、批量和台账的离线合成验收。"""

import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import anthropic
import httpx
import pytest

from qianshu.regression import batches, tiers
from qianshu.regression.__main__ import main
from qianshu.regression.ledger import Ledger
from qianshu.regression.storage import PrivateFiles, RegressionError, digest, json_bytes
from tests.protocol_fixtures import serialized_empty
from test_regression import corpus, frozen, get_json, repo, run_args


def test_batch_sdk_record_replay_and_resume(repo, frozen, monkeypatch):
    from test_regression import _stream_reply_events
    outcomes = ("succeeded", "expired", "errored", "canceled")
    subset = ["--frozen", ".private/large", "--expect-sha256", expanded_exam(repo, 4)]
    posts = []
    payload = serialized_empty()
    def transport(request):
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, content=b"".join(_stream_reply_events(payload)),
                                  headers={"Content-Type": "text/event-stream"})
        if request.method == "POST":
            posts.append(json.loads(request.content)["requests"])
            return httpx.Response(200, json={"id": "batch-test"})
        if request.url.path.endswith("/results"):
            lines = []
            for item, outcome in zip(posts[0], outcomes, strict=True):
                result = {"type": outcome}
                if outcome == "succeeded":
                    result["message"] = {"model": "fixture", "stop_reason": "tool_use",
                                         "content": [{"type": "tool_use", "name": "submit_application_reply",
                                                      "input": json.loads(payload)}],
                                         "usage": {"input_tokens": 100, "output_tokens": 20}}
                lines.append(json.dumps({"custom_id": item["custom_id"], "result": result}))
            return httpx.Response(200, content="\n".join(reversed(lines)).encode())
        return httpx.Response(200, json={"id": "batch-test", "processing_status": "ended",
                                        "results_url": "https://fixture.invalid/results"})
    monkeypatch.setattr(anthropic, "DefaultHttpxClient", lambda **kw: httpx.Client(
        transport=httpx.MockTransport(transport), **kw))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://fixture.invalid")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic")
    args = ["run", *subset, "--out", ".private/run", "--live", "--run-id", "r1", "--estimate-usd", "2", "--release"]
    assert main(args) == 0
    records = [get_json(path) for path in sorted((repo / "run").glob("document-*.json"))]
    assert [r["attempts"][0]["batch_outcome"] for r in records] == list(outcomes)
    assert all(len(r["attempts"]) == 1 for r in records)
    report = get_json(repo / "run/public-report.json")
    assert report["cost"]["missing_attempts"] == 3
    assert report["cost"]["known_usd"] == "0.0002"
    assert main(["replay", "--recording", ".private/run", *subset, "--out", ".private/replay"]) == 0
    assert get_json(repo / "replay/public-report.json")["business"] == report["business"]
    args[args.index(".private/run")] = ".private/resumed"
    assert main(args) == 1
    assert main([*args, "--resume"]) == 0
    assert len(posts) == 1
    manifest = get_json(repo / "resumed/recording.json")
    ledger_path = repo / "regression-ledger.json"
    assert manifest["ledger"] == {"path": str(ledger_path), "sha256": digest(ledger_path.read_bytes())}
    entry = get_json(ledger_path)["r1"]
    assert entry["spent_usd"] == "0.0002" and entry["committed_usd"] == "2.6"
    assert entry["active"] is False
    assert main(["run", *subset, "--out", ".private/immediate", "--live", "--run-id", "r2",
                 "--estimate-usd", "2", "--release", "--mode", "instant"]) == 0
    immediate = get_json(repo / "immediate/public-report.json")
    assert immediate["cost"]["known_usd"] == "0.000048"
    assert immediate["request_set_sha256"] == report["request_set_sha256"]
    assert main(["summarize", "--recording", ".private/immediate", "--recording", ".private/run",
                 "--out", ".private/summary"]) == 0


def test_atomic_budget_and_shared_slots(repo):
    with PrivateFiles(repo.parent).open() as files:
        ledger = Ledger(files, Path("cost.json"))
        def reserve(i):
            try:
                ledger.reserve(str(i), "instant", "40", "trial")
                return True
            except RegressionError:
                return False
        with ThreadPoolExecutor(4) as pool:
            assert sum(pool.map(reserve, range(4))) == 2
        with pytest.raises(RegressionError):
            ledger.reserve("batch", "batch", "5", "full")
        runs = get_json(repo / "cost.json")
        run_id = next(iter(runs))
        ledger.settle(run_id, Decimal("2"), 1)
        assert get_json(repo / "cost.json")[run_id]["committed_usd"] == "52.0"
        ledger.settle(run_id, Decimal("2"), 0)
        ledger.reserve("batch", "batch", "5", "full")
        with pytest.raises(RegressionError):
            ledger.reserve(run_id, "instant", "1", "new-directory")


def test_byte_comparison_and_lexical_tool_input():
    raw = '{"model":"m","stream":true,"messages":["stream"]}'
    common = batches.common_body(raw)
    wire = '{"requests":[{"custom_id":"a","params":' + common + '}]}'
    batches.verify_envelope(wire, {"a": common})
    for bad in (wire.replace('"m"', '"M"'), wire.replace('"model":', '"model": ')):
        with pytest.raises(RegressionError):
            batches.verify_envelope(bad, {"a": common})
    tool_input = '{ "policies" : [] }'
    line = '{"result":{"type":"succeeded","message":{"stop_reason":"tool_use","content":' + (
        '[{"type":"tool_use","name":"submit_application_reply","input":' + tool_input + '}]}}}')
    assert batches.batch_attempt(line)["reply"] == tool_input


def test_deterministic_whole_document_projection():
    docs = {str(i): {} for i in range(32)}
    samples = {str(i): {"id": str(i), "token": str(i % 32), "country": "A", "policy_type": "P",
                        "benefit_type": "rare" if i == 25 else "B"} for i in range(35)}
    snapshot = {"fingerprint": "parent", "dataset": "exam", "documents": docs, "samples": samples,
                "pairs": {k: {**r, "configuration": {"name": {"value": "V"}}} for k, r in samples.items()},
                "denominators": {"fields": {"name": {}}}, "strata": []}
    labels = {"0": {"required": True, "history": ["曾截断"]}}
    selected = tiers.select(snapshot, labels)
    assert json_bytes(selected) == json_bytes(tiers.select({**snapshot, "documents": dict(reversed(docs.items()))}, labels))
    quick = tiers.project(snapshot, selected)
    assert {"0", "25"} <= quick["documents"].keys()
    assert {"0", "32"} <= quick["samples"].keys()
    assert set(quick["denominators"]["whole_policy"]) == quick["samples"].keys()
    with pytest.raises(RegressionError):
        tiers.project({**snapshot, "fingerprint": "other"}, selected)
    with pytest.raises(RegressionError):
        tiers.project(snapshot, tiers.select(snapshot, {k: {"required": True} for k in docs}))


@pytest.mark.parametrize("fault", ["lost_create", "missing", "conflict", "unknown", "deadline", "disconnect", "partial_usage"])
def test_batch_recovery_never_reposts(repo, frozen, monkeypatch, fault):
    posts = []
    broken = True
    class BrokenStream(httpx.SyncByteStream):
        def __init__(self, first):
            self.first = first

        def __iter__(self):
            yield self.first
            raise httpx.ReadError("synthetic")
    def transport(request):
        if request.method == "POST":
            posts.append(json.loads(request.content)["requests"])
            if fault == "lost_create":
                raise httpx.ReadError("synthetic")
            return httpx.Response(200, json={"id": "b"})
        if request.url.path.endswith("/results"):
            rows = [json.dumps({"custom_id": item["custom_id"], "result": {"type": "expired"}}) + "\n"
                    for item in posts[0]]
            if fault == "disconnect":
                first = json.loads(rows[0])
                first["result"]["usage"] = {"input_tokens": 8000000, "output_tokens": 0}
                rows[0] = json.dumps(first) + "\n"
            if fault == "partial_usage":
                first = json.loads(rows[0])
                first["result"]["usage"] = {"input_tokens": 250000}
                rows[0] = json.dumps(first) + "\n"
            if broken:
                if fault == "missing":
                    rows.pop()
                elif fault == "conflict":
                    rows.append(rows[0].replace("expired", "canceled"))
                elif fault == "unknown":
                    rows.append('{"custom_id":"unknown","result":{"type":"expired"}}\n')
                elif fault in {"disconnect", "partial_usage"}:
                    return httpx.Response(200, stream=BrokenStream(rows[0].encode()))
            return httpx.Response(200, content="".join(rows + [rows[0]]).encode())
        state = "in_progress" if broken and fault == "deadline" else "ended"
        return httpx.Response(200, json={"id": "b", "processing_status": state,
                                        "results_url": "https://fixture.invalid/results"})
    now = [0]
    def advance(_seconds):
        now[0] += 26 * 3600
    monkeypatch.setattr(batches, "time", type("Clock", (), {"time": lambda: now[0], "sleep": advance}))
    monkeypatch.setattr(anthropic, "DefaultHttpxClient", lambda **kw: httpx.Client(
        transport=httpx.MockTransport(transport), **kw))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://fixture.invalid")
    args = [*run_args(frozen), "--live", "--trial", "2", "--run-id", "r", "--estimate-usd", "1"]
    ledger_path = Path("regression-ledger.json")
    if fault == "disconnect":
        with PrivateFiles(repo.parent).open() as files:
            ledger = Ledger(files, ledger_path)
            ledger.reserve("prior", "instant", "1", "prior")
            ledger.settle("prior", Decimal(100), False)
            for other in ("a", "b"):
                ledger.reserve(other, "batch", "1", other)
    assert main(args) == 1
    assert not (repo / "run/recording.json").exists()
    if fault == "disconnect":
        # 断流后重取同一篇、再次断流，两次都只计首篇的 8 美元。
        for attempt in range(2):
            if attempt:
                assert main([*args, "--resume"]) == 1
            row = get_json(repo / ledger_path)["r"]
            assert Decimal(row["spent_usd"]) == 8
            assert Decimal(row["committed_usd"]) == 0
            assert row["active"] is True
            with PrivateFiles(repo.parent).open() as files:
                ledger = Ledger(files, ledger_path)
                # 再预占 1.30 会使总额达到 111.90。即时名额空闲也必须拒绝。
                for mode in ("batch", "instant"):
                    with pytest.raises(RegressionError, match="预算"):
                        ledger.reserve("next", mode, "1", "next")
    elif fault == "partial_usage":
        assert main([*args, "--resume"]) == 1
        row = get_json(repo / ledger_path)["r"]
        assert Decimal(row["spent_usd"]) == Decimal("0.25")
        assert Decimal(row["committed_usd"]) == Decimal("1.30")
        assert row["active"] is True
    else:
        assert get_json(repo / ledger_path)["r"]["committed_usd"] == "1.3"
    assert main(args) == 1
    broken = False
    if fault == "lost_create":
        assert main([*args, "--resume"]) == 1
        args += ["--reconcile-batch-id", "b"]
    result = 1 if fault == "conflict" else 0
    assert main([*args, "--resume"]) == result
    assert main([*args, "--resume"]) == result
    assert len(posts) == 1


def expanded_exam(repo, count):
    from qianshu.regression.freeze import load_freeze, write_snapshot
    with PrivateFiles(repo.parent).open() as files:
        base = load_freeze(files, Path("frozen"))
        docs, samples, pairs = [], [], []
        sample = next(iter(base["samples"].values()))
        pair = base["pairs"][sample["id"]]
        for i in range(count):
            token = f"doc-{i:03}"
            row_id = f"row-{i:03}"
            docs.append({**base["documents"][sample["token"]], "token": token})
            samples.append({**sample, "id": token, "row_id": row_id, "token": token})
            pairs.append({**pair, "id": token, "row_id": row_id, "token": token})
        manifest = get_json(repo / "frozen/manifest.json")
        manifest.update(count=count, documents=count, target=count, strata=[{
            "policy_type": sample["policy_type"], "benefit_type": sample["benefit_type"],
            "quota": count, "included_count": count, "shortfall": 0}])
        return write_snapshot(files, Path("large"), manifest, samples, pairs, docs,
                             base["companies_text"].encode(), base["denominators"]["fields"])


def test_repeat_scores_on_full_and_quick_and_pin_catalog(repo, frozen):
    from test_regression import no_policies
    from qianshu.regression.storage import digest
    sha = expanded_exam(repo, 27)
    arguments = ["--frozen", ".private/large", "--expect-sha256", sha]
    (repo / "labels.json").write_text("{}")
    assert main(["select-quick", *arguments, "--labels", ".private/labels.json", "--out", ".private/quick"]) == 0
    assert main(["run", *arguments, "--out", ".private/full"], model=no_policies) == 0
    (repo / "benefit-catalog.json").write_text('{"package_names":["new"]}')
    for tier in ("full", "quick"):
        args = ["replay", "--recording", ".private/full", *arguments]
        if tier == "quick":
            args[-1] = digest((repo / "quick/selection.json").read_bytes())
            args += ["--selection", ".private/quick/selection.json"]
        for i in range(2):
            assert main([*args, "--out", f".private/{tier}-{i}"]) == 0
        report = get_json(repo / f"{tier}-0/public-report.json")
        assert report == get_json(repo / f"{tier}-1/public-report.json")
        assert report["samples"] == (25 if tier == "quick" else 27)
        assert main([*args, "--out", ".private/invalid", "--benefit-catalog", ".private/benefit-catalog.json"]) == 1


def test_dual_version_loads_parser_from_tag_and_tool_from_candidate(tmp_path):
    import subprocess
    import sys
    import shutil

    source = Path(tiers.__file__).resolve().parents[3]
    tag = tmp_path / "tag"
    package = tag / "src/qianshu"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('MARKER = "tag"')
    (package / "sentinel.py").write_text('MARKER = "parse"')
    shutil.copy(source / "uv.lock", tag / "uv.lock")
    script = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from qianshu.regression import baseline
def revision(root, *args):
    if args[0] == 'status':
        return b''
    if root == Path(sys.argv[2]) and sys.argv[3] == 'wrong':
        return b'other'
    return b'tag'
baseline.revision = revision
tool, parser = baseline.bind(sys.argv[2])
from qianshu import sentinel, MARKER
assert MARKER == 'tag' and sentinel.MARKER == 'parse'
assert baseline.provenance(tool, parser)['parse_commit'] == 'tag'
"""
    for flag, succeeds in (("good", True), ("wrong", False)):
        result = subprocess.run([sys.executable, "-I", "-c", script, str(source / "src"), str(tag), flag],
                                capture_output=True)
        assert (result.returncode == 0) == succeeds


def test_archive_reconstruction_requires_pinned_evidence(repo, frozen, monkeypatch, capsys):
    import shutil
    from qianshu.regression import baseline
    from qianshu.regression.storage import digest

    monkeypatch.setattr("qianshu.regression.__main__.configured_model",
                        lambda *_args, **_kw: lambda **_: serialized_empty())
    assert main([*run_args(frozen), "--live", "--mode", "instant", "--run-id", "history", "--estimate-usd", "1"]) == 0
    manifest_path = repo / "run/recording.json"
    manifest = get_json(manifest_path)
    manifest["configuration"].pop("benefit_catalog_sha256")
    manifest_path.write_bytes(json_bytes(manifest))
    src = Path(tiers.__file__).resolve().parents[1]
    shutil.copytree(src, repo.parent / "src/qianshu", ignore=shutil.ignore_patterns("__pycache__"))
    versions = {"parse_commit": "fixture", "lock_sha256": "lock", "dependencies_sha256": "deps"}
    monkeypatch.setattr(baseline, "binding", (repo.parent / "rules", versions))
    catalog = repo / "run/benefit-catalog.json"
    evidence = json_bytes({**versions, "recording_sha256": digest(manifest_path.read_bytes()),
                           "benefit_catalog_sha256": digest(catalog.read_bytes())})
    (repo / "evidence.json").write_bytes(evidence)
    args = ["rebuild-requests", "--frozen", ".private/frozen", "--expect-sha256", frozen,
            "--recording", ".private/run", "--benefit-catalog", ".private/run/benefit-catalog.json",
            "--evidence", ".private/evidence.json", "--expect-evidence-sha256", digest(evidence), "--out", ".private/proof"]
    assert main(args) == 0
    replay = ["replay", *run_args(frozen, ".private/projected")[1:], "--recording", ".private/run",
              "--benefit-catalog", ".private/run/benefit-catalog.json"]
    assert main(replay) == 1
    args = [*replay, "--reconstruction", ".private/proof/reconstruction.json", "--expect-reconstruction-sha256"]
    assert main([*args, "wrong"]) == 1
    proof = (repo / "proof/reconstruction.json").read_bytes()
    assert main([*args, digest(proof)]) == 0
    assert get_json(repo / "projected/recording.json")["request_set_sha256"]
    assert get_json(repo / "projected/public-report.json")["cost"]["new_known_usd"] == "0"

    # 还原旧工具能盖过实录的情形。实录 100、重建 200，消息内容完全相同。
    capture = batches.capture_requests
    def capture_with_limit(snapshot, rules, legacy, settings):
        wires = capture(snapshot, rules, legacy, settings)
        for token, wire in wires.items():
            params = json.loads(wire)
            params["max_tokens"] = 100
            recorded = json.dumps(params)
            user_sha = digest(params["messages"][0]["content"].encode())
            name = manifest["documents_by_user_sha256"][user_sha][digest(token.encode())]
            path = repo / "run" / name
            document = get_json(path)
            document.update(request=recorded, request_sha256=digest(batches.common_body(recorded).encode()))
            path.write_bytes(json_bytes(document))
            manifest["artifact_sha256"][name] = digest(path.read_bytes())
            params["max_tokens"] = 200
            wires[token] = json.dumps(params)
        return wires

    from qianshu.regression.freeze import load_freeze
    with PrivateFiles(repo.parent).open() as files:
        snapshot = load_freeze(files, Path("frozen"))
    wires = capture_with_limit(snapshot, repo.parent / "rules", False, {"ANTHROPIC_MODEL": manifest["model"]})
    manifest_path.write_bytes(json_bytes(manifest))
    checksum = digest(manifest_path.read_bytes())
    archive = {**json.loads(evidence), "recording_sha256": checksum}
    (repo / "evidence.json").write_bytes(json_bytes(archive))
    monkeypatch.setattr(batches, "capture_requests", lambda *_: wires)
    assert main(["rebuild-requests", "--frozen", ".private/frozen", "--expect-sha256", frozen,
                 "--recording", ".private/run", "--benefit-catalog", ".private/run/benefit-catalog.json",
                 "--evidence", ".private/evidence.json", "--expect-evidence-sha256", digest(json_bytes(archive)),
                 "--out", ".private/conflict"]) == 1
    assert "历史重建与实录请求" in capsys.readouterr().err
    assert not (repo / "conflict").exists()
    old_proof = {**json.loads(proof), "recording_sha256": checksum,
                 "common_sha256": {k: digest(batches.common_body(v).encode()) for k, v in wires.items()}}
    proof_path = repo / "proof/reconstruction.json"
    proof_path.write_bytes(json_bytes(old_proof))
    monkeypatch.setattr("qianshu.regression.__main__.capture_requests", lambda *_: wires)
    assert main([*args, digest(proof_path.read_bytes())]) == 1
    assert "标签请求与原录制" in capsys.readouterr().err


def test_transport_slot_limits_and_exact_budget(repo):
    with PrivateFiles(repo.parent).open() as files:
        ledger = Ledger(files, Path("slots.json"))
        for mode, limit in (("instant", 2), ("batch", 3)):
            for i in range(limit):
                ledger.reserve(mode + str(i), mode, "1", "test")
            with pytest.raises(RegressionError):
                ledger.reserve("extra-" + mode, mode, "1", "test")
        ledger.settle("batch0", Decimal("104.8"), 0)
        # 104.8 已花费加其余四次的 5.2 预占，正好 110。
        with pytest.raises(RegressionError):
            ledger.reserve("beyond", "batch", "0.01", "test")
        ledger.settle("batch0", Decimal("103.5"), 0)
        ledger.reserve("boundary", "batch", "1", "test")


@pytest.mark.parametrize(("stop", "name", "code"), [
    ("tool_use", "wrong_tool", "reply_tool_unexpected"),
    ("max_tokens", "submit_application_reply", "model_incomplete"),
    ("refusal", "submit_application_reply", "model_incomplete"),
])
def test_batch_tool_and_stop_evidence(stop, name, code):
    line = json.dumps({"result": {"type": "succeeded", "message": {"model": "fixture", "stop_reason": stop,
                      "content": [{"type": "tool_use", "name": name, "input": {}}]}}})
    attempt = batches.batch_attempt(line)
    assert attempt["failure_stage"] == code
    assert attempt["stop_reason"] == stop
    assert attempt["blocks"] == [{"type": "tool_use", "length": 2}]


def test_two_processes_resume_one_publication(repo, frozen, monkeypatch):
    from multiprocessing import get_context
    ctx = get_context("fork")
    downloads = ctx.Value("i", 0)
    posts = []
    def http(request):
        if request.method == "POST":
            posts.extend(json.loads(request.content)["requests"])
            return httpx.Response(200, json={"id": "b"})
        if request.url.path.endswith("/results"):
            with downloads.get_lock():
                downloads.value += 1
                first = downloads.value == 1
            if first:
                raise httpx.ReadError("synthetic")
            data = b"".join(json_bytes({"custom_id": item["custom_id"], "result": {"type": "expired"}}) for item in posts)
            return httpx.Response(200, content=data)
        return httpx.Response(200, json={"id": "b", "processing_status": "ended",
                                         "results_url": "https://fixture.invalid/results"})
    monkeypatch.setattr(anthropic, "DefaultHttpxClient", lambda **kw: httpx.Client(
        transport=httpx.MockTransport(http), **kw))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://fixture.invalid")
    args = [*run_args(frozen), "--live", "--run-id", "r", "--estimate-usd", "1"]
    assert main(args) == 1
    def resume():
        raise SystemExit(main([*args, "--resume"]))
    workers = [ctx.Process(target=resume) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(30)
        assert worker.exitcode == 0
    assert downloads.value == 2
    assert len(get_json(repo / "run/recording.json")["artifact_sha256"]) == 2


def test_fixed_trace_budget_and_prior_intent(repo):
    from contextlib import ExitStack
    from qianshu.regression.ledger import transaction

    with ExitStack() as stack:
        files = stack.enter_context(PrivateFiles(repo.parent).open())
        ledger = Ledger.for_trace(files, ".private/regression-ledger.json", stack)
        ledger.reserve("prior", "instant", "1", "prior")
        ledger.settle("prior", Decimal(108), False)
        ledger.reserve("same", "batch", "1", "first")
        with pytest.raises(RegressionError):
            Ledger.for_trace(files, ".private/other.json", stack)
        with pytest.raises(RegressionError):
            ledger.reserve("same", "instant", "1", "second")
        with pytest.raises(RegressionError):
            ledger.reserve("another", "batch", "1", "second")
        (repo / "old").mkdir()
        with transaction(files, Path("old") / ledger.journal("orphan")) as entry:
            entry.update(run_id="orphan")
        with pytest.raises(RegressionError, match="批次日志"):
            ledger.reserve("orphan", "instant", "0.1", "retry")
        with transaction(files, Path("old/legacy.json")) as runs:
            runs["legacy"] = get_json(repo / ledger.path)["prior"]
        with pytest.raises(RegressionError, match="旧位置"):
            ledger.reserve("legacy", "instant", "0.1", "retry")


def test_batch_cost_before_publication_failure(repo, frozen, monkeypatch):
    from qianshu.regression.storage import Output

    with PrivateFiles(repo.parent).open() as files:
        ledger = Ledger(files, Path("regression-ledger.json"))
        ledger.reserve("prior", "instant", "1", "prior")
        ledger.settle("prior", Decimal(102), False)
    posts = []
    def transport(request):
        if request.method == "POST":
            posts.extend(json.loads(request.content)["requests"])
            return httpx.Response(200, json={"id": "b"})
        if request.url.path.endswith("/results"):
            result = {"type": "expired", "usage": {"input_tokens": 8000000, "output_tokens": 0}}
            return httpx.Response(200, content=json_bytes({"custom_id": posts[0]["custom_id"], "result": result}))
        return httpx.Response(200, json={"id": "b", "processing_status": "ended",
                                         "results_url": "https://fixture.invalid/results"})
    monkeypatch.setattr(anthropic, "DefaultHttpxClient", lambda **kw: httpx.Client(
        transport=httpx.MockTransport(transport), **kw))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://fixture.invalid")
    write = Output.write
    def fail_report(self, name, raw):
        if name == "public-report.json":
            raise OSError("synthetic")
        write(self, name, raw)
    monkeypatch.setattr(Output, "write", fail_report)
    assert main([*run_args(frozen), "--live", "--trial", "1", "--run-id", "batch", "--estimate-usd", "1"]) == 1
    row = get_json(repo / "regression-ledger.json")["batch"]
    assert Decimal(row["spent_usd"]) == 8 and row["committed_usd"] == "0" and not row["active"]
    with PrivateFiles(repo.parent).open() as files, pytest.raises(RegressionError, match="预算"):
        Ledger(files, Path("regression-ledger.json")).reserve("next", "batch", "1", "next")


def test_instant_disconnect_after_start_keeps_reservation(repo, frozen, monkeypatch):
    from test_regression import _stream_reply_events
    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield next(iter(_stream_reply_events("")))
            raise httpx.ReadError("synthetic")
    def transport(request):
        return httpx.Response(200, stream=BrokenStream(), headers={"content-type": "text/event-stream"})
    monkeypatch.setattr(anthropic, "DefaultHttpxClient", lambda **kw: httpx.Client(
        transport=httpx.MockTransport(transport), **kw))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic")
    assert main([*run_args(frozen), "--live", "--mode", "instant", "--trial", "1", "--retries", "1",
                 "--run-id", "broken", "--estimate-usd", "1"]) == 0
    attempts = get_json(repo / "run/document-00001.json")["attempts"]
    assert all((a["partial_input_tokens"], a["partial_output_tokens"]) == (1, 0) for a in attempts)
    assert all(a["input_tokens"] is None and a["output_tokens"] is None for a in attempts)
    cost = get_json(repo / "run/public-report.json")["cost"]
    assert cost["usage_status"] == "用量部分未知" and cost["missing_attempts"] == 2
    row = get_json(repo / "regression-ledger.json")["broken"]
    assert row["committed_usd"] == "1.3" and row["active"] is False


def test_ended_batches_keep_unknown_cost_without_holding_slots(repo):
    with PrivateFiles(repo.parent).open() as files:
        ledger = Ledger(files, Path("regression-ledger.json"))
        cost = {"mode": "batch", "known_usd": "0.25", "missing_attempts": 1}
        for i in range(3):
            ledger.reserve(str(i), "batch", "1", str(i))
            ledger.record_cost(str(i), cost)
        # 启用台账前的已结束录制也只占费用，重复接账幂等。
        manifest = {"run_id": "history", "origin": "live", "source_code_measured": True}
        for _ in range(2):
            ledger.carry_recording(manifest, "history-sha", cost, "1")
        for row in get_json(repo / ledger.path).values():
            assert row["committed_usd"] == "1.3" and row["spent_usd"] == "0.25"
            assert row["active"] is False
        for i in range(3):
            ledger.reserve(f"next-{i}", "batch", "1", str(i))
        with pytest.raises(RegressionError, match="名额"):
            ledger.reserve("fourth", "batch", "1", "fourth")




def test_attachment_batch_instant_replay_and_public_artifacts(repo, frozen, monkeypatch, caplog):
    import base64
    from email.message import EmailMessage
    from test_regression import put_json, _stream_reply_events

    marker = "SYNTHETIC-ATTACHMENT-108"
    eml = EmailMessage()
    eml.set_content("同意，立即建单 " + marker)
    png = (Path(__file__).resolve().parents[1] / "parse/fixtures/attachment-policies.png").read_bytes()
    rows = []
    objects = repo / "objects"
    objects.mkdir()
    for i, (media, raw) in enumerate([("image/png", png), ("message/rfc822", eml.as_bytes())], 1):
        sha = digest(raw)
        (objects / sha).write_bytes(raw)
        rows.append({"section": str(i), "media_type": media, "sha256": sha})
    put_json(repo / "materials.json", {"freeze_sha256": frozen, "documents": {"doc-batch": rows, "doc-single": []}})
    encoded = base64.b64encode(png).decode()
    sent = []
    jobs = []
    def transport(request):
        if request.method == "POST":
            assert request.url.host == "api.anthropic.com" and request.url.scheme == "https"
            payload = json.loads(request.content)
            if request.url.path == "/v1/messages":
                sent.append((request.url.path, payload))
                return httpx.Response(200, content=b"".join(_stream_reply_events(serialized_empty())),
                                      headers={"content-type": "text/event-stream"})
            assert request.url.path == "/v1/messages/batches"
            jobs.extend(payload["requests"])
            sent.extend((request.url.path, job["params"]) for job in jobs)
            return httpx.Response(200, json={"id": "batch-synthetic"})
        if request.url.path == "/results":
            return httpx.Response(200, text="\n".join(json.dumps({"custom_id": job["custom_id"],
                "result": {"type": "errored", "error": {"type": "synthetic", "message": marker}}}) for job in jobs))
        return httpx.Response(200, json={"id": "batch-synthetic", "processing_status": "ended",
                                        "results_url": "https://api.anthropic.com/results"})

    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setattr(anthropic, "DefaultHttpxClient", lambda **kw: httpx.Client(
        transport=httpx.MockTransport(transport), **kw))
    shared = ["--frozen", ".private/frozen", "--expect-sha256", frozen, "--attachment-manifest", ".private/materials.json"]
    for mode in ("instant", "batch"):
        assert main(["run", *shared, "--live", "--run-id", "attachments-" + mode, "--estimate-usd", "1",
                     "--out", ".private/" + mode, "--mode", mode]) == 0
        report = get_json(repo / mode / "public-report.json")
        assert report["attachments"] == dict(documents=1, images=1, emails=1, unreadable=0, image_pending=0,
                                            inventory_supplied=True)
        summary = (repo / mode / "public-report.md").read_text()
        assert "含附件的申请 1 篇" in summary
        publication = Path(__file__).parent / "fixtures/108-publication"
        bodies = [publication / "issue-update.md", publication / "pr-body.md"]
        artifacts = [repo / mode / name for name in ("public-report.md", "public-report.json")]
        for path in [*bodies, *artifacts]:
            data = path.read_bytes()
            assert marker.encode() not in data and encoded.encode() not in data
    assert main(["replay", *shared, "--recording", ".private/instant", "--out", ".private/replay"]) == 0
    manifest = get_json(repo / "materials.json")
    manifest["documents"]["doc-batch"].reverse()
    put_json(repo / "materials.json", manifest)
    assert main(["replay", *shared, "--recording", ".private/instant", "--out", ".private/swapped"]) == 1
    assert not (repo / "swapped").exists()
    images = [(path, p["messages"][0]["content"]) for path, p in sent if isinstance(p["messages"][0]["content"], list)]
    assert [path for path, content in images] == ["/v1/messages", "/v1/messages/batches"]
    for _, payload in sent:
        schema = payload["tools"][0]["input_schema"]
        assert any("image_evidence" in node.get("properties", {}) for node in schema["$defs"].values()) == isinstance(payload["messages"][0]["content"], list)
    assert images[0][1] == images[1][1]
    assert images[0][1][-1]["source"]["data"] == encoded and marker in images[0][1][0]["text"]
    assert marker not in caplog.text and encoded not in caplog.text


def test_attachment_inventory_binds_object_and_freeze_fingerprints(repo, frozen):
    from test_regression import put_json
    from qianshu.regression.corpus import bind_attachments
    from qianshu.regression.freeze import load_freeze

    sha = digest(b"registered")
    (repo / "objects").mkdir()
    (repo / "objects" / sha).write_bytes(b"changed")
    path = Path("materials.json")
    def manifest(rows):
        put_json(repo / path, {"freeze_sha256": frozen, "documents": rows})
    entry = {"section": "1", "sha256": sha, "media_type": "image/png"}
    with PrivateFiles(repo.parent).open() as files:
        snapshot = load_freeze(files, Path("frozen"))
        manifest({"doc-batch": [entry], "doc-single": []})
        linked = bind_attachments(files, snapshot, path, frozen)
        assert linked["documents"]["doc-batch"]["additional_blocks"][0].missing
        assert "additional_blocks" not in snapshot["documents"]["doc-batch"]
        with pytest.raises(RegressionError, match="冻结题本指纹"):
            bind_attachments(files, snapshot, path, "other")
        manifest({"doc-batch": [entry, entry], "doc-single": []})
        with pytest.raises(RegressionError, match="来源号"):
            bind_attachments(files, snapshot, path, frozen)
        manifest({"doc-batch": []})
        with pytest.raises(RegressionError, match="无法复验"):
            bind_attachments(files, snapshot, path, frozen)


def test_attachment_selection_preserves_whole_documents(repo, frozen):
    from qianshu.regression.freeze import load_freeze

    with PrivateFiles(repo.parent).open() as files:
        snapshot = load_freeze(files, Path("frozen"))
    samples = sorted(k for k, v in snapshot["samples"].items() if v["token"] == "doc-batch")
    selection = {"purpose": "attachment-check", "parent": frozen, "documents": ["doc-batch"], "samples": samples}
    chosen = tiers.attachment_subset(snapshot, selection)
    assert list(chosen["documents"]) == ["doc-batch"]
    assert sorted(chosen["samples"]) == samples and chosen["fingerprint"] == digest(json_bytes(selection))
    with pytest.raises(RegressionError):
        tiers.attachment_subset(snapshot, {**selection, "samples": []})
