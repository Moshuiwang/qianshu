"""守望者查重通道，登记库与 HTTP 传输均为进程内合成替身。"""

import json
from functools import partial
from threading import Event
from types import SimpleNamespace

import httpx
import pytest

from qianshu import __main__ as cli
from qianshu.feishu import trial_dedupe as dedupe
from qianshu.feishu.trial_approval import journal_at
from qianshu.feishu.trial_archive import archive_worker
from qianshu.feishu.trial_order import name_fingerprint, name_key
from test_create_order import case  # noqa: F401


NAMES = ("合成Ａ 方案", "合成A　方案")
MISS = "精确名未命中（全半角 / 空白变体未覆盖，需人工比对）"
KEY = name_key(NAMES[0])
FP = name_fingerprint(KEY)


@pytest.fixture
def state(case):
    path = case.private / "trial-050.sqlite3"
    path.touch(mode=0o600)
    snap = {"items": [1, 2], "reviewed": [{"policy_name": {"value": n}} for n in NAMES]}
    with journal_at(path) as j, j.db:
        j.db.execute("BEGIN IMMEDIATE")
        j.initialize()
        j.db.execute("INSERT INTO trial_card(card,registration,version,snapshot) VALUES('card',7,2,?)",
                     (json.dumps(snap),))
        j.db.execute("INSERT INTO trial_handoff(name_key,registration,version,item,opened) VALUES(?,7,2,1,0)",
                     (KEY,))
        j.db.execute("""INSERT INTO trial_order(registration,version,item,name_key,card,status)
                        VALUES(7,2,2,?,'card','unknown')""", (KEY,))
    return path


@pytest.mark.parametrize("mode,expected,count", [
    ("hit", "平台有同名", 1), ("empty", MISS, 0), ("partial", "查重失败", None),
    ("variant", MISS, 0), ("end_flag", MISS, 0), ("no_signal", "查重失败", None),
    ("conflict", "查重失败", None),
])
def test_channel_uses_only_official_search(case, state, capsys, monkeypatch, mode, expected, count):
    calls = []
    official = case.config["official"]
    config = case.private / "config.json"
    config.touch(mode=0o600)
    config.write_text(json.dumps(case.config))
    settings = SimpleNamespace(trial_credentials_complete=True, trial_credentials={
        official["token_key"]: case.official_token, official["app_id_key"]: case.ids["official_app"]})

    def exchange(req):
        condition, = json.loads(req.content)["filter"]["children"]
        assert condition["operator"] == "eq"
        name = condition["value"]
        calls.append((req.method, req.url.path, name))
        rows = [] if mode == "empty" else [{"rowId": case.ids["created"], case.control["policy_name"]: KEY}]
        if mode == "variant":
            stored = "合成A  方案"
            assert name_key(stored) == name_key(name) and stored != name
            rows = [row for row in [{"rowId": case.ids["created"], case.control["policy_name"]: stored}]
                    if row[case.control["policy_name"]] == name]
        data = {"rows": rows, "total": len(rows)}
        if mode == "partial" and name == NAMES[1]:
            data = {}
        elif mode == "end_flag":
            data = {"rows": [], "hasMore": False}
        elif mode == "no_signal":
            assert "tag_search" not in official
            data = {"rows": []}
        return httpx.Response(200, json={"success": True, "data": data})

    monkeypatch.setattr(httpx, "Client", partial(httpx.Client, transport=httpx.MockTransport(exchange)))
    common = ["--state", str(state)]
    assert cli.main([*common, "--trial-dedupe-request", "7", "2", FP, "--reason", NAMES[0]]) == 0
    queued = capsys.readouterr().out
    assert all(n not in queued for n in NAMES)
    assert json.loads(queued)["status"] == "待查重"
    sensitive = []
    if mode == "conflict":
        # 把合成凭据片段放入请求的目标表路径，须在交付替身传输前拦截。
        sensitive = [case.ids["worksheetId"]]

    def lookup(names):
        return dedupe.official_matches(config, settings, sensitive, names)

    dedupe.scan(state, lookup)
    dedupe.recover(state)
    dedupe.scan(state, lookup)
    assert calls == ([] if mode == "conflict" else [
        ("POST", official["search"]["path"], name) for name in (NAMES[:1] if mode == "no_signal" else NAMES)])
    assert cli.main([*common, "--trial-dedupe-status", "7"]) == 0
    output = capsys.readouterr().out
    assert all(n not in output for n in NAMES)
    result, = json.loads(output)
    assert (result["status"], result["matches"], result["robot_record"]) == (expected, count, True)
    assert result["order_states"] == ["unknown"] and result["id"] == json.loads(queued)["id"]
    assert cli.main([*common, "--trial-dedupe-status", "8"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_interrupted_claim_is_failed_without_lookup(state):
    with journal_at(state) as j:
        dedupe.request(j, 7, 2, FP, "合成原因")

    def interrupted(names):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        dedupe.scan(state, interrupted)
    dedupe.recover(state)
    dedupe.scan(state, lambda _: pytest.fail("已领取的请求被重发"))
    with journal_at(state, readonly=True) as j:
        result, = dedupe.status(j.db)
    assert result["status"] == "查重失败" and result["matches"] is None


def test_wrong_binding_and_cleared_handoff(state):
    with journal_at(state) as j:
        for version, fp in [(1, FP), (2, "0" * 16)]:
            with pytest.raises(ValueError):
                dedupe.request(j, 7, version, fp, "合成原因")
        assert dedupe.status(j.db) == []
        dedupe.request(j, 7, 2, FP, "合成原因")
        with j.db:
            j.db.execute("UPDATE trial_handoff SET cleared=1")
    dedupe.scan(state, lambda _: pytest.fail("已销清移交不应发查询"))
    with journal_at(state, readonly=True) as j:
        assert dedupe.status(j.db)[0]["status"] == "查重失败"


def test_worker_scans_without_write_runners(state):
    with journal_at(state) as j:
        dedupe.request(j, 7, 2, FP, "合成原因")
    completed = Event()

    def scan():
        dedupe.scan(state, lambda names: 0)
        completed.set()

    with archive_worker(None, dedupe=scan, interval=0.01, join=3):
        assert completed.wait(3)
    with journal_at(state, readonly=True) as j:
        assert dedupe.status(j.db)[0]["status"] == MISS


def test_guardian_uses_only_registry(state, monkeypatch, capsys):
    with journal_at(state) as j, j.db:
        j.db.execute("DROP TABLE trial_dedupe")
    assert cli.main(["--trial-dedupe-status", "--state", str(state)]) == 0
    assert json.loads(capsys.readouterr().out) == []
    monkeypatch.setenv("MINGDAO_PASSWORD", "synthetic")
    for args in (["--trial-dedupe-status"], ["--trial-dedupe-request", "7", "2", FP, "--reason", "核对"]):
        with pytest.raises(SystemExit):
            cli.main([*args, "--state", str(state)])
    with journal_at(state, readonly=True) as j:
        assert not j.db.execute("SELECT 1 FROM sqlite_schema WHERE name='trial_dedupe'").fetchone()


def test_legacy_miss_without_pagination_evidence(state):
    with journal_at(state) as j, j.db:
        result = dedupe.request(j, 7, 2, FP, "合成原因")
        j.db.execute("UPDATE trial_dedupe SET status='无同名', matches=0 WHERE id=?", (result["id"],))
    with journal_at(state, readonly=True) as j:
        row, = dedupe.status(j.db)
    assert (row["status"], row["matches"]) == ("查重失败", None)
