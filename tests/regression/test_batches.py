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
from qianshu.regression.storage import PrivateFiles, RegressionError, json_bytes
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
    from qianshu.regression.storage import digest
    historical = ["account-recording", "--ledger", ".private/carry.json", "--recording", ".private/run",
                  "--expect-sha256", digest((repo / "run/recording.json").read_bytes()), "--estimate-usd", "2"]
    assert main(historical) == main(historical) == 0
    entry = get_json(repo / "carry.json")["r1"]
    assert entry["spent_usd"] == "0.0002" and entry["committed_usd"] == "2.6"
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


@pytest.mark.parametrize("fault", ["lost_create", "missing", "conflict", "unknown", "deadline", "disconnect"])
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
            if broken:
                if fault == "missing":
                    rows.pop()
                elif fault == "conflict":
                    rows.append(rows[0].replace("expired", "canceled"))
                elif fault == "unknown":
                    rows.append('{"custom_id":"unknown","result":{"type":"expired"}}\n')
                elif fault == "disconnect":
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
    assert main(args) == 1
    assert not (repo / "run/recording.json").exists()
    assert get_json(repo / "regression-ledger.json")["r"]["committed_usd"] == "1.3"
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
