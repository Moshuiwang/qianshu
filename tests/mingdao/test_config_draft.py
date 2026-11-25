"""配置草稿通过 050 的同一官方接口替身读取，不访问平台。"""
import hashlib
import json
from functools import partial

import httpx
import pytest

from qianshu.mingdao import config_draft as draft
from test_order_field_checks import business_cli, case, platform, registered_entry, synthetic_uuids, web_order


@pytest.fixture
def invocation(case, web_order, monkeypatch):
    for key in draft.HANDOFF:
        monkeypatch.delenv(key, raising=False)
    private = case.private
    out = private / "drafts"
    out.mkdir(mode=0o700)
    draft.x5.exclusive(private / "base.json", case.config)
    draft.x5.exclusive(private / "scopes.json", {k: v for k, v in case.evidence.items() if k.endswith("_scope")})
    (private / "trial-orders.jsonl").touch()
    settings = private / "synthetic-settings"
    settings.write_text(f"PLACEHOLDER_OFFICIAL_TOKEN={case.official_token}\n"
                        f"PLACEHOLDER_OFFICIAL_APP={case.ids['official_app']}\n")
    settings.chmod(0o600)
    args = ["--config", str(private / "base.json"), "--out-dir", str(out), "--env-file", str(settings),
            "--read-scopes", str(private / "scopes.json")]
    requests = []
    transport = httpx.Client.keywords["transport"]
    def exchange(request):
        requests.append(request)
        return transport.handle_request(request)
    monkeypatch.setattr(httpx, "Client", partial(httpx.Client.func, transport=httpx.MockTransport(exchange)))
    return args, out, requests


def test_draft_from_business_sources(case, platform, web_order, invocation, capsys, caplog):
    args, out, requests = invocation
    platform.template["_owner"] = {"name": "不可带出的姓名", "id": "internal-marker-591",
                                   "unused": case.session_token}
    assert draft.main(args) == 0
    assert set(p.name for p in out.iterdir()) == set(draft.FILES)
    data = [draft.x5.private_bytes(out / name) for name in draft.FILES]
    config, auth, sources = map(draft.x5.decode, data)
    assert auth["is_test"] is False and auth["owner_approval"] == draft.OWNER
    assert auth["config_sha256"] == hashlib.sha256(data[0]).hexdigest()
    snapshot = config["order_snapshot"]
    assert auth["web_rules_sha256"] == hashlib.sha256(draft.x5.compact(snapshot["web_rules"]).encode()).hexdigest()
    draft.x5.check_search_binding(config, config["official"])
    mapping = draft.x5.cell_schema(config["fields"], config["readback_skip"])
    draft.x5.fixed_cells(config, config["fields"], mapping)
    assert config["template"] == case.config["template"]
    assert snapshot["tariffs"] == case.rate_rows
    assert snapshot["concurrent_policies"]["total"] == 0
    assert snapshot["tags"] == case.config["order_snapshot"]["tags"]
    assert all(snapshot["web_rules"][name]["control"] == rule["control"] for name, rule in web_order[0].items())
    assert any(item["missing"] for item in sources["items"])
    stdout, stderr = capsys.readouterr()
    public = stdout + stderr + caplog.text + data[2].decode()
    assert all(marker not in public for marker in (*case.ids.values(), *case.control.values()))
    assert all(marker not in public + b"".join(data).decode() for marker in (
        "不可带出的姓名", "internal-marker-591", case.session_token, case.official_token, "登记申请人", "合成负责人"))
    assert all(marker not in data[1].decode() for marker in (*case.ids.values(), *case.control.values()))
    assert {r.method for r in requests} == {"GET", "POST"}
    assert all(r.url.path.endswith("/rows/list") and json.loads(r.content)["viewId"]
               for r in requests if r.method == "POST")


@pytest.mark.parametrize("mode", ["directory", *draft.FILES, *sorted(draft.HANDOFF)])
def test_preflight_zero_requests(mode, invocation, monkeypatch):
    args, out, requests = invocation
    if mode == "directory":
        out.chmod(0o755)
    elif mode in draft.FILES:
        (out / mode).symlink_to(out / "absent")
    else:
        monkeypatch.setenv(mode, "synthetic")
    before = set(out.iterdir())
    assert draft.main(args) != 0 and not requests and set(out.iterdir()) == before


@pytest.mark.parametrize(("method", "path"), [("POST", "/api/Worksheet/AddWorksheetRow"),
                                               ("POST", "/api/Worksheet/UpdateWorksheetRow"),
                                               ("DELETE", "/v3/app/worksheets/test/rows/test")])
def test_write_rejected_at_wire(method, path, invocation, monkeypatch):
    args, out, requests = invocation
    def attempt(api, row):
        return api.client.request(method, api.origin + path, headers=api._headers, json={})
    monkeypatch.setattr(draft.x5.Official, "read", attempt)
    assert draft.main(args) != 0 and not requests and not list(out.iterdir())


def test_unlocated_tables_leave_missing_evidence(invocation, case):
    args, out, _ = invocation
    assert draft.main(args[:-2]) == 0
    config, auth, sources = [draft.x5.decode(draft.x5.private_bytes(out / name)) for name in draft.FILES]
    snapshot = config["order_snapshot"]
    assert snapshot["tags"][0]["id"] == case.ids["tag"]
    assert snapshot["tariffs"].startswith(draft.MISSING) and auth["tariffs_scope"].startswith(draft.MISSING)
    path = draft.fingerprint(["config", "order_snapshot", "tariffs"])
    item, = [entry for entry in sources["items"] if entry["item_sha256_prefix"] == path]
    assert item["missing"] and item["count"] == 0


def test_task_projection(invocation, case):
    args, out, requests = invocation
    path = case.private / "scopes.json"
    scopes = draft.x5.decode(path.read_bytes())
    task = case.ids["created"]
    scopes["tasks_scope"].update(record_id=task, columns={"company": "主体", "package": "包", "status": "状态"})
    path.write_text(draft.x5.compact(scopes))
    case.task_rows[task] = {"rowId": task, "主体": "合成主体", "包": "合成包", "状态": "合成状态", "owner": "私有姓名"}
    assert draft.main(args) == 0
    data = draft.x5.private_bytes(out / draft.FILES[0])
    config = draft.x5.decode(data)
    assert config["order_snapshot"]["upgrade_tasks"] == [
        {"id": task, "company": "合成主体", "package": "合成包", "状态": "合成状态"}]
    assert "私有姓名" not in data.decode()
    assert any(r.method == "GET" and r.url.path.endswith(task) for r in requests)
