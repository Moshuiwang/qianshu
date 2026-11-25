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


def test_full_business_settings_select_only_official_values(case, invocation, monkeypatch, capsys):
    args, out, requests = invocation
    settings = case.private / "synthetic-settings"
    identifiers = case.config["create"]["identifiers"]
    business = {
        "MINGDAO_BASE_URL": case.config["official"]["origin"],
        "MINGDAO_WORKSHEET_ID": identifiers["worksheetId"],
        "MINGDAO_VIEW_ID": identifiers["viewId"],
        "MINGDAO_PROJECT_ID": identifiers["projectId"],
        "FEISHU_ALLOWED_SENDER_IDS": case.config["configurator"]["id"],
        "FEISHU_APP_ID": "synthetic-business",
        **{f"BUSINESS_{i}": f"synthetic-business-{i}" for i in range(20)},
    }
    monkeypatch.setenv("FEISHU_APP_ID", business["FEISHU_APP_ID"])
    with settings.open("a") as stream:
        for key, value in business.items():
            stream.write(f"{key}={value}\n")
        stream.write("IGNORED='first line\nPLACEHOLDER_OFFICIAL_APP=wrong\nFEISHU_HANDOFF_FAKE=x'\n")

    original = draft.x5.configure_x5_outlet
    def configure(guard, secrets, config, login):
        assert set(secrets) == {config["official"][k] for k in ("token_key", "app_id_key")}
        return original(guard, secrets, config, login)
    monkeypatch.setattr(draft.x5, "configure_x5_outlet", configure)
    assert draft.main(args) == 0 and requests
    assert set(path.name for path in out.iterdir()) == set(draft.FILES)
    stdout, stderr = capsys.readouterr()
    output = stdout + stderr + "".join((out / name).read_text() for name in draft.FILES)
    assert all(value not in output for value in (case.official_token, case.ids["official_app"]))


@pytest.mark.parametrize("field", ["token_key", "app_id_key"])
@pytest.mark.parametrize("value", [None, '"bad value"'])
def test_missing_or_invalid_auth_zero_requests(case, invocation, field, value):
    args, out, requests = invocation
    settings = case.private / "synthetic-settings"
    name = case.config["official"][field]
    lines = [line for line in settings.read_text().splitlines() if not line.startswith(name + "=")]
    if value is not None:
        lines.append(f"{name}={value}")
    settings.write_text("\n".join(lines))
    assert draft.main(args) != 0 and not requests and not list(out.iterdir())


@pytest.mark.parametrize("name", ["FEISHU_HANDOFF_OWNER_OPEN_ID", "FEISHU_HANDOFF_FUTURE", "FEISHU_MENTION_LOG"])
def test_handoff_key_in_file_zero_requests(case, invocation, name):
    args, out, requests = invocation
    with (case.private / "synthetic-settings").open("a") as stream:
        stream.write(f"export '{name}' =\n")
    assert draft.main(args) != 0 and not requests and not list(out.iterdir())


def test_selected_credential_conflict_still_stops(case, invocation):
    args, out, requests = invocation
    path = case.private / "base.json"
    base = draft.x5.decode(path.read_bytes())
    base["configurator"]["id"] = case.official_token
    path.write_text(draft.x5.compact(base))
    assert draft.main(args) != 0 and not requests and not list(out.iterdir())


def test_owned_web_app_id_only_in_config(case, invocation, capsys, caplog):
    args, out, requests = invocation
    app = case.ids["official_app"]
    case.config["create"]["identifiers"]["appId"] = app
    (case.private / "base.json").write_text(draft.x5.compact(case.config))

    assert draft.main(args) == 0 and requests
    assert {p.name for p in out.iterdir()} == set(draft.FILES)
    cfg, auth, sources = [(out / name).read_text() for name in draft.FILES]
    assert json.loads(cfg)["create"]["identifiers"]["appId"] == app and cfg.count(app) == 1
    stdout, stderr = capsys.readouterr()
    public = auth + sources + stdout + stderr + caplog.text
    assert app not in public and case.official_token not in public + cfg


@pytest.mark.parametrize("mode", ["token", "other_path", "contains_app", "app_fragment", "shared_key"])
def test_web_app_id_conflicts_before_read(case, invocation, mode):
    args, out, requests = invocation
    app = case.ids["official_app"]
    identifiers = case.config["create"]["identifiers"]
    identifiers["appId"] = app
    if mode == "token":
        identifiers["appId"] = case.official_token
    elif mode == "other_path":
        identifiers["extra"] = app
    elif mode == "contains_app":
        identifiers["appId"] = "prefix-" + app
    elif mode == "app_fragment":
        identifiers["appId"] = app[:8]
    else:
        case.config["official"]["token_key"] = case.config["official"]["app_id_key"]
    (case.private / "base.json").write_text(draft.x5.compact(case.config))
    assert draft.main(args) != 0 and not requests and not list(out.iterdir())


def test_x5_default_rejects_equal_web_app_id(case):
    official = case.config["official"]
    app = case.ids["official_app"]
    case.config["create"]["identifiers"]["appId"] = app
    secrets = {official["app_id_key"]: app, official["token_key"]: case.official_token}
    with pytest.raises(draft.x5.OutletConflict):
        draft.x5.configure_x5_outlet(draft.x5.Outlet(), secrets, case.config, {"cookies": []})
