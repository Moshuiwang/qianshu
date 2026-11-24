import json
import re
import sqlite3
import sys
from datetime import timedelta
from types import SimpleNamespace

import pytest

from qianshu.mail.trial_state import Store
from scripts import trial_report as tr


START = tr.utc("2026-01-01T00:00:00Z")
END = START + timedelta(days=1)
AT = (START + timedelta(hours=1)).isoformat()
FIELDS = {"x": {"status": "确定", "candidate": 0}, "y": {"status": "需确认", "candidate": 1}}
DRAFT = {"x": 0, "y": 1}


class Reads:
    page_size = 5
    log_page_limit = 50
    config = {"search": {"view_ids": ["v"]}, "status_control": "state",
              "status_map": {s: s for s in ("新建", "待审批", "待配置")}}

    def __init__(self):
        self.rows = {}
        self.logs = {}
        self.requests = []

    def add(self, rid, link, policy, country, creator, *, returned=False, late=False):
        stamp = END.isoformat() if late else AT
        self.rows[rid] = dict(rowId=rid, link=link, policy=policy, area=country, _createdBy=creator,
                              _createdAt=AT, _updatedAt=stamp, state="新建" if returned else "待审批", x=1, y=1)
        change = dict(id="state", oldText="新建", newText="待审批")
        log = dict(_createdAt=AT, logData=[change])
        self.logs[rid] = [dict(content=log)]
        if returned:
            self.logs[rid].append(dict(content=dict(_createdAt=AT, node="returned", logData=[
                dict(id="state", oldText="待审批", newText="新建")])))

    def _request(self, action, rid=None, *, body=None, params=None):
        self.requests.append(action)
        if action == "logs":
            return {"logs": self.logs[rid]}
        assert action == "search"
        link = body["filter"]["children"][0]["value"]
        rows = [r for r in self.rows.values() if r["link"] == link]
        return {"rows": rows, "total": len(rows)}

    def page_rows(self, fetch, size):
        return fetch(1)["rows"]

    def read(self, rid):
        self.requests.append("read")
        return self.rows[rid]

    def detail(self, raw, rid):
        assert raw["rowId"] == rid
        return raw

    def record(self, row):
        return SimpleNamespace(name="private-item", status=row["state"], updated_at=tr.utc(row["_updatedAt"]))


@pytest.fixture
def dataset(tmp_path, monkeypatch):
    config = dict(trial_start=START.isoformat(), build_country="A", bot_account="robot",
                  controls=dict(link="link", policy="policy", country="area", node_key="node",
                                return_nodes=["returned"], reason_key="reason"),
                  registrations={}, cards={}, entry_scope="s", approval_ids=["a", "b", "c"], entry_db="entry.sqlite")
    ledger = tmp_path / "trial-orders.jsonl"
    ledger.touch(mode=0o600)
    api = Reads()
    store = Store(tmp_path / "trial-050.sqlite3")
    with store.db as db:
        for n, area in enumerate(("A", "B", "B"), 1):
            config["registrations"][str(n)] = {"country": area, "link": str(n)}
            db.execute("INSERT INTO registration(id,created,verdict,status,reason,attachments) VALUES(?,?,?,?,?,0)",
                       (n, AT, "是", "correction_pending" if n == 3 else "done",
                        "correction_interrupted" if n != 2 else None))
            db.execute("INSERT INTO card_intent VALUES(?,?,1,1,'sent',?)", (str(n), n, AT))
            config["cards"][str(n)] = dict(policy="p", fields=FIELDS, draft=DRAFT)
            api.add(str(n), str(n), "p", area, "robot" if n == 1 else "human", late=n == 3)
        for i, (kind, status) in enumerate([("freeze", "sent"), ("freeze", "pending"), ("freeze", "unknown"),
                                            ("body_missed", "review"), ("interrupted", "pending")]):
            db.execute("INSERT INTO notice(event,kind,status,at,quantity) VALUES(?,?,?,?,42)", (i, kind, status, AT))
        db.execute("INSERT INTO notice(event,kind,status,at) VALUES('old','freeze','sent',?)", (END.isoformat(),))
    monkeypatch.setattr("qianshu.mail.trial_state.now", lambda: AT)
    store.import_spend("0")
    for model in ("sonnet", "haiku"):
        attempt = store.reserve(model, maximum=20, input_bound=100, reg=1)
        assert store.settle(attempt, inputs=100, outputs=20)
    store.db.close()
    with sqlite3.connect(tmp_path / "entry.sqlite") as db:
        db.execute("CREATE TABLE feishu_approvals(scope,approval_id,status,decided_at,message_id)")
        db.executemany("INSERT INTO feishu_approvals VALUES('s',?,'reject',?,?)",
                       [(k, START.timestamp(), k) for k in ("a", "unrelated")])
        db.executemany("INSERT INTO feishu_approvals VALUES('s',?,'pending',NULL,?)", [(k, k) for k in ("b", "c")])
    event = dict(utc=AT, registration="r", is_test=False, event="created", record_id="1",
                 approved=True, written=DRAFT, displayed=DRAFT, country="A")
    base = dict(utc=AT, registration="r", is_test=False)
    events = [base | dict(event="card_published", registration="card" + card, card_uuid=card,
                          approval_id=approval, message_id=approval)
              for card, approval in zip(("1", "2", "3"), ("a", "b", "c"))]
    events.extend((base | dict(event="create_attempt", request_fields=["x", "y"]), event,
                   base | dict(event="readback_compared", record_id="1", 结论="一致")))
    ledger.write_text("".join(json.dumps(r) + "\n" for r in events))
    return tmp_path, config, api


def test_all_countries_fields_and_unknown_not_clean(scoped):
    public, private = tr.report(*scoped, START, END)
    assert [public[k] for k in ("收到申请", "出卡", "建单国家申请", "建单国家卡片", "建单")] == [3, 3, 1, 1, 1]
    assert [public[k + "已核"] for k in ("人工改动项", "错判确定", "多问", "无需修改")] == [2, 2, 2, 0]
    assert public["卡片核对未知"] == 1 and public["人工改动项"] == tr.UNKNOWN
    assert public["卡片驳回"] == 1 and public["明道云写错"] == public["漏条"] == 0
    assert private["建议"] == "维持"


def test_field_positive_negative_and_missing():
    assert tr.field_counts(FIELDS, DRAFT, DRAFT) == (0, 0, 1)
    assert tr.field_counts(FIELDS, DRAFT, {"x": 0, "y": 2}) == (1, 0, 0)
    assert tr.field_counts(FIELDS, {"x": 0, "y": 3}, DRAFT) == (1, 0, 1)
    assert tr.field_counts(FIELDS, DRAFT, {"x": 0}) is None
    assert tr.field_counts({}, {}, {}) is None


def test_returns_items_and_unregistered_writes_and_misses(scoped):
    path, config, api = scoped
    api.add("1", "1", "p", "A", "robot", returned=True)
    api.add("4", "1", "q", "A", "robot")
    api.add("5", "2", "q", "B", "robot")
    api.add("6", "2", "r", "B", "human")
    api.add("7", "outside", "s", "A", "robot")
    public, private = tr.report(*scoped, START, END)
    assert public["漏条"] == public["退回公告条目"] == 1
    assert public["明道云写错"] == public["明道云写错已核"] == 2
    assert public["建单"] == 3 and private["写错"] == ["4", "7"] and private["漏条"] == ["6"]
    assert private["建议"] == "回退"
    notice, = private["退回公告"]
    assert notice["record_id"] == "1" and notice["notify"] == "配置负责人"
    assert notice["reason"] == "原因未读到，请到明道云查看"
    api.logs["1"][-1]["content"]["reason"] = "private-reason"
    assert tr.report(*scoped, START, END)[1]["退回公告"][0]["reason"] == "private-reason"
    api.logs["1"][-1]["content"]["node"] = "withdrawn"
    assert tr.report(*scoped, START, END)[0]["退回公告条目"] == 0


@pytest.mark.parametrize("mutation", [{"displayed": {"x": 9, "y": 1}}, {"approved": False}, {"comparison": "写错"}])
def test_unapproved_or_mismatched_write(scoped, mutation):
    ledger = scoped[0] / "trial-orders.jsonl"
    row = created_row(ledger)
    if "comparison" in mutation:
        row = dict(row, event="readback_unconfirmed", 结论="写错")
        with ledger.open("a") as f:
            f.write(json.dumps(row))
    else:
        replace_created(ledger, row | mutation)
    public, private = tr.report(*scoped, START, END)
    assert public["明道云写错已核"] == 1 and private["建议"] == "回退"
    assert public["明道云写错"] == (tr.UNKNOWN if "comparison" in mutation else 1)


@pytest.mark.parametrize("contents", [None, "{}", "{", '{"event":"future"}'])
def test_absent_or_unknown_ledger(dataset, contents):
    ledger = dataset[0] / "trial-orders.jsonl"
    if contents is None:
        ledger.unlink()
    else:
        ledger.write_text(contents)
    public, private = tr.report(*dataset, START, END)
    assert public["建单"] == public["明道云写错"] == tr.UNKNOWN
    assert public["明道云写错已核"] == 0 and private["建议"] == "维持"


def test_costs_g448_and_body_backlog(dataset):
    public, _ = tr.report(*dataset, START, END)
    assert public["总费用美元"] == "0.000420000"
    assert public["解析中途停止已转人工"] == 2
    assert all(public["冻结通知" + k] == 1 for k in ("待发", "已发", "结果未知"))
    assert public["待发提示"] == public["需人工核对"] == 1
    for n in (0, None):
        result = tr.charge_counts([dict(model="sonnet", inputs=n, outputs=0)])
        assert result["总费用美元"] == ("0.000000000" if n == 0 else tr.UNKNOWN)


@pytest.mark.parametrize("counts,expected", [
    ((1, 1, 0, 0, 0, 0), "回退"), ((0, 0, 3, 10, 6, 0), "扩大"),
    ((0, 0, 2, 10, 10, 0), "维持"), ((0, 0, 3, 9, 9, 0), "维持"),
    ((0, 0, 3, 10, 5, 0), "维持"), ((0, 1, 3, 10, 10, 0), "维持")])
def test_recommendation(counts, expected):
    assert tr.recommendation(*counts) == expected


def test_cli_private_public_and_read_only(dataset, monkeypatch, capsys):
    path, config, api = dataset
    config["official"] = dict(token_key="SYNTHETIC_INPUT", app_id_key="SYNTHETIC_INPUT")
    monkeypatch.setenv("SYNTHETIC_INPUT", "fixture")
    cfg = path / "trial-report.json"
    cfg.touch(mode=0o600)
    cfg.write_text(json.dumps(config))
    monkeypatch.setattr(tr, "Official", lambda *_: api)
    monkeypatch.setattr(sys, "argv", ["report", "--private-dir", str(path), "--end", END.isoformat()])
    inputs = [p for p in path.iterdir() if p.is_file()]
    before = [p.read_bytes() for p in inputs]
    tr.main()
    output = json.loads(capsys.readouterr().out)
    fees = {model + "费用美元" + column for model in ("总", "sonnet", "haiku") for column in ("", "已核")}
    assert fees <= output.keys()
    unknown = {k for k, v in output.items() if v == tr.UNKNOWN}
    assert {k for k in output if k.endswith("原因")} == {k + "原因" for k in unknown}
    for k, v in output.items():
        if k.endswith("原因"):
            assert isinstance(v, list) and v and set(v) <= set(tr.Reason)
        else:
            assert type(v) is int or v == tr.UNKNOWN or k in fees and re.fullmatch(r"\d+\.\d{9}", v)
    detail = path / "reports/2026-01-01/trial-report-2026-01-01.json"
    assert detail.stat().st_mode & 0o777 == 0o600 and len(json.loads(detail.read_text())["卡片"]) == 3
    metrics = json.loads(detail.read_text())["指标"]
    assert metrics == {k: output[k] for k in (*unknown, *(k + "原因" for k in unknown), *COVERAGE)}
    assert before == [p.read_bytes() for p in inputs]


def test_failed_detail_and_missing_link_stay_unknown(scoped, monkeypatch):
    _, config, api = scoped
    api.add("extra", "1", "q", "A", "robot")
    read = api.read

    def fail_one(rid):
        if rid == "extra":
            raise tr.Stop()
        return read(rid)

    monkeypatch.setattr(api, "read", fail_one)
    public, _ = tr.report(*scoped, START, END)
    assert public["漏条"] == public["明道云写错"] == tr.UNKNOWN
    assert public["写入核对未知"] == 1 and public["卡片核对未知"] == 2
    del api.rows["extra"]
    del config["registrations"]["2"]["link"]
    public, _ = tr.report(*scoped, START, END)
    assert public["漏条"] == tr.UNKNOWN and public["明道云写错"] == 0


def test_prior_order_monitored_and_new_period_counts(scoped):
    _, _, api = scoped
    api.add("1", "1", "p", "A", "robot", returned=True)
    api.logs["1"][-1]["content"]["_createdAt"] = END.isoformat()
    public, _ = tr.report(*scoped, END, END + timedelta(days=1))
    assert public["收到申请"] == public["出卡"] == public["建单"] == 0
    assert public["退回公告条目"] == 1 and public["冻结通知已发"] == 1


def test_good_card_and_ambiguous_policy(dataset):
    _, _, api = dataset
    api.rows["1"]["x"] = 0
    public, _ = tr.report(*dataset, START, END)
    assert public["无需修改已核"] == 1 and public["卡片核对未知"] == 1
    api.add("other", "2", "p", "B", "human")
    public, _ = tr.report(*dataset, START, END)
    assert public["漏条"] == tr.UNKNOWN and public["卡片核对未知"] == 2


def test_real_ledger_event_shape_with_test_and_inflight(dataset):
    path, _, api = dataset
    ledger = path / "trial-orders.jsonl"
    api.add("test", "1", "other", "A", "robot")
    with ledger.open("a") as f:
        for event in ("create_reserved", "create_attempt"):
            f.write(json.dumps(dict(event=event, registration="t", name="【测试】fixture", is_test=True, utc=AT)) + "\n")
        for event, reg, rid, test in [("created", "t", "test", True), ("create_attempt", "u", None, False)]:
            f.write(json.dumps(dict(event=event, registration=reg, record_id=rid,
                                    name="【测试】fixture", is_test=test, utc=AT)) + "\n")
    public, _ = tr.report(*dataset, START, END)
    assert public["建单已核"] == 1 and public["明道云写错已核"] == 0
    assert public["建单"] == public["明道云写错"] == tr.UNKNOWN


@pytest.fixture
def scoped(dataset, monkeypatch):
    path, config, api = dataset
    config.update(bot_scan={"view": "v", "coverage": "full_table"}, required_fields=["x", "y"])
    original = api._request

    def search(action, rid=None, *, body=None, params=None):
        if action == "search" and body["filter"]["children"][0]["field"] == "_createdBy":
            who, since, until = body["filter"]["children"]
            assert who == dict(type="condition", field="_createdBy", operator="eq", value="robot")
            assert (since["field"], since["operator"], until["field"], until["operator"]) == (
                "_createdAt", "gte", "_createdAt", "lt")
            rows = [r for r in api.rows.values() if r["_createdBy"] == who["value"]
                    and tr.utc(since["value"]) <= tr.utc(r["_createdAt"]) < tr.utc(until["value"])]
            return {"rows": rows, "total": len(rows), "wholeTableMatchedCount": len(rows)}
        return original(action, rid, body=body, params=params)

    monkeypatch.setattr(api, "_request", search)
    return dataset


def test_scan_unregistered_link(scoped):
    scoped[2].add("extra", "outside", "q", "A", "robot")
    public, _ = tr.report(*scoped, START, END)
    assert public["明道云写错已核"] == 1


def test_first_discovery_after_creation_day(scoped):
    scoped[2].add("extra", "1", "q", "A", "robot")
    next_end = END + timedelta(days=1)
    public, private = tr.report(*scoped, END, next_end)
    assert public["明道云写错已核"] == public["新发现的历史异常"] == 1
    previous = scoped[0] / f"trial-report-{END.date()}.json"
    previous.touch(mode=0o600)
    previous.write_text(json.dumps(private))
    public, _ = tr.report(*scoped, next_end, next_end + timedelta(days=1))
    assert public["新发现的历史异常"] == 0


def test_missing_scan_scope_unknown(scoped):
    del scoped[1]["bot_scan"]
    assert tr.report(*scoped, START, END)[0]["明道云写错"] == tr.UNKNOWN


def created_row(ledger):
    return next(r for line in ledger.read_text().splitlines() if (r := json.loads(line))["event"] == "created")


def replace_created(ledger, value):
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    ledger.write_text("".join(json.dumps(value if r["event"] == "created" else r) + "\n" for r in rows))


def add_event(path, **changes):
    ledger = path / "trial-orders.jsonl"
    row = created_row(ledger)
    with ledger.open("a") as stream:
        stream.write(json.dumps(row | changes) + "\n")


@pytest.mark.parametrize("conflict", ["country", "test", "malformed"])
def test_confirmed_error_survives_conflict(scoped, conflict):
    path, _, api = scoped
    add_event(path, event="readback_compared", 结论="写错")
    if conflict == "country":
        api.rows["1"]["area"] = "B"
    elif conflict == "test":
        add_event(path, registration="other", is_test=True)
    else:
        add_event(path, event="invalid")
    public, private = tr.report(*scoped, START, END)
    assert public["明道云写错已核"] == 1 and private["建议"] == "回退"
    assert public["写入核对未知"] > 0


@pytest.mark.parametrize("unconfirmed", [False, True])
def test_partial_equal_snapshots(scoped, unconfirmed):
    path, _, _ = scoped
    ledger = path / "trial-orders.jsonl"
    row = created_row(ledger) | dict(written={"x": 0}, displayed={"x": 0})
    replace_created(ledger, row)
    if unconfirmed:
        add_event(path, event="readback_unconfirmed", 结论="未确认")
    assert tr.report(*scoped, START, END)[0]["明道云写错"] == tr.UNKNOWN


@pytest.mark.parametrize("damage", ["delete_charge", "delete_seal"])
def test_charge_seal_damage(scoped, damage):
    path, _, _ = scoped
    with sqlite3.connect(path / "trial-050.sqlite3") as db:
        db.execute("DELETE FROM charge WHERE model='haiku'")
        store = Store.__new__(Store)
        store.db = db
        store._seal_ledger()
        db.commit()
        assert tr.report(*scoped, START, END)[0]["总费用美元"] == "0.000400000"
        db.execute("DELETE FROM " + ("charge" if damage == "delete_charge" else "ledger_check"))
    assert tr.report(*scoped, START, END)[0]["总费用美元"] == tr.UNKNOWN


@pytest.mark.parametrize("alias", ["direct", "hardlink", "symlink"])
def test_output_alias_rejected_before_report(scoped, monkeypatch, alias):
    import os
    path, config, api = scoped
    target = path / "trial-report-2026-01-01.json"
    entry = path / config["entry_db"]
    if alias == "direct":
        entry.rename(target)
        config["entry_db"] = target.name
    elif alias == "symlink":
        target.symlink_to(entry)
    else:
        os.link(entry, target)
    config["official"] = dict(token_key="SYNTH", app_id_key="SYNTH")
    cfg = path / "trial-report.json"
    cfg.touch(mode=0o600)
    cfg.write_text(json.dumps(config))
    monkeypatch.setenv("SYNTH", "fixture")
    monkeypatch.setattr(tr, "Official", lambda *_: api)
    monkeypatch.setattr(tr, "report", lambda *_: pytest.fail("已进入取数阶段"))
    monkeypatch.setattr(sys, "argv", ["report", "--private-dir", str(path), "--end", END.isoformat()])
    with pytest.raises(ValueError):
        tr.main()


def test_verified_empty_scan_and_intact_snapshots(scoped):
    public, _ = tr.report(*scoped, START, END)
    assert public["明道云写错"] == public["写入核对未知"] == 0


def test_unconfirmed_with_complete_snapshots(scoped):
    add_event(scoped[0], event="readback_unconfirmed", 结论="未确认")
    public, _ = tr.report(*scoped, START, END)
    assert public["明道云写错"] == tr.UNKNOWN and public["明道云写错已核"] == 0


def test_late_scan_failure_keeps_confirmed_rows(scoped, monkeypatch):
    api = scoped[2]
    api.add("extra", "outside", "q", "A", "robot")
    original = api.page_rows

    def pages(fetch, size):
        yield from original(fetch, size)
        raise tr.Stop()

    monkeypatch.setattr(api, "page_rows", pages)
    public, _ = tr.report(*scoped, START, END)
    assert public["明道云写错"] == tr.UNKNOWN and public["明道云写错已核"] == 1


def test_rerun_preserves_previously_confirmed_external_error(scoped):
    path, _, api = scoped
    api.add("extra", "outside", "q", "A", "robot")
    public, private = tr.report(*scoped, START, END)
    assert public["明道云写错已核"] == 1
    previous = path / f"trial-report-{START.date()}.json"
    previous.touch(mode=0o600)
    previous.write_text(json.dumps(private))
    api.rows["extra"]["area"] = "B"
    public, private = tr.report(*scoped, START, END)
    assert public["明道云写错已核"] == 1 and private["建议"] == "回退"


def test_external_updated_detail_lacks_creation_country(scoped):
    scoped[2].add("extra", "outside", "q", "B", "robot", late=True)
    public, private = tr.report(*scoped, START, END)
    assert public["明道云写错"] == public["新发现的历史异常"] == tr.UNKNOWN
    assert "extra" in private["写入差异核对"]


def test_round2_narrow_view(scoped, monkeypatch):
    path, config, api = scoped
    rid = "10000000-0000-4000-8000-000000000001"
    (path / "trial-orders.jsonl").write_text("")
    api.rows.clear()
    api.add(rid, "outside", "p", "A", "robot")
    api.page_limit = 4
    monkeypatch.setattr(api, "page_rows", tr.Official.page_rows.__get__(api))
    assert tr.report(*scoped, START, END)[0]["明道云写错已核"] == 1
    monkeypatch.setattr(api, "_request", lambda *a, **kw: {"rows": [], "total": 0})
    public, _ = tr.report(*scoped, START, END)
    assert public["明道云写错"] == tr.UNKNOWN and public["写入核对未知"] > 0


def test_round2_second_identity(scoped):
    path, _, api = scoped
    api.add("second", "outside", "p", "A", "robot")
    add_event(path, record_id="second")
    add_event(path, record_id="second", event="readback_compared", 结论="写错")
    public, private = tr.report(*scoped, START, END)
    assert public["明道云写错已核"] == 1 and private["写错"] == ["second"]
    assert public["明道云写错"] == tr.UNKNOWN and private["建议"] == "回退"


def test_round2_unproven_test(scoped):
    path, _, api = scoped
    api.add("extra", "outside", "q", "A", "robot")
    assert tr.report(*scoped, START, END)[0]["明道云写错已核"] == 1
    add_event(path, record_id="extra", is_test=True)
    assert tr.report(*scoped, START, END)[0]["明道云写错已核"] == 1


def test_round2_truncated_manifest(scoped):
    path, config, _ = scoped
    config.pop("required_fields")
    ledger = path / "trial-orders.jsonl"
    row = created_row(ledger)
    ledger.write_text(json.dumps(row | dict(fields=["x"], written={"x": 0}, displayed={"x": 0})) + "\n")
    add_event(path, event="readback_compared", 结论="一致")
    public, _ = tr.report(*scoped, START, END)
    assert public["明道云写错"] == tr.UNKNOWN and public["写入核对未知"] > 0


def test_round2_late_readback(scoped):
    path, _, _ = scoped
    ledger = path / "trial-orders.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows if not r["event"].startswith("readback")))
    add_event(path, event="readback_compared", 结论="写错", utc=(END + timedelta(minutes=1)).isoformat())
    assert tr.report(*scoped, END, END + timedelta(days=1))[0]["明道云写错已核"] == 1
    assert tr.report(*scoped, START, END)[0]["明道云写错"] == tr.UNKNOWN


def cli_fixture(path, config, api, monkeypatch, *args):
    config["official"] = dict(token_key="SYNTH", app_id_key="SYNTH")
    cfg = path / "trial-report.json"
    cfg.touch(mode=0o600)
    cfg.write_text(json.dumps(config))
    monkeypatch.setenv("SYNTH", "fixture")
    monkeypatch.setattr(tr, "Official", lambda *_: api)
    monkeypatch.setattr(sys, "argv", ["report", "--private-dir", str(path), *args])


def test_round2_shifted_window(scoped, monkeypatch):
    path, config, api = scoped
    api.add("extra", "outside", "q", "A", "robot")
    api.rows["extra"]["_createdAt"] = api.rows["extra"]["_updatedAt"] = (START + timedelta(minutes=30)).isoformat()
    cli_fixture(*scoped, monkeypatch, "--end", END.isoformat())
    tr.main()
    target, = path.rglob("trial-report-2026-01-01.json")
    before = target.read_bytes()
    assert "extra" in json.loads(before)["写错"]
    cli_fixture(*scoped, monkeypatch, "--end", (END + timedelta(hours=1)).isoformat())
    with pytest.raises(ValueError):
        tr.main()
    assert target.read_bytes() == before


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_round2_resolved_sidecar(scoped, monkeypatch, alias):
    import os
    path, config, api = scoped
    entry = path / config["entry_db"]
    actual = path / "actual.sqlite"
    entry.rename(actual)
    entry.symlink_to(actual)
    for target in (path / "trial-summary.json", path / "reports/summary/trial-summary.json"):
        (path / "reports").mkdir(mode=0o700, exist_ok=True)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not target.exists():
            target.touch(mode=0o600)
    targets = [path / "trial-summary.json", path / "reports/summary/trial-summary.json"]
    for suffix, target in zip(("-wal", "-journal"), targets):
        sidecar = actual.with_name(actual.name + suffix)
        if alias == "symlink":
            sidecar.symlink_to(target)
        else:
            os.link(target, sidecar)
    cli_fixture(*scoped, monkeypatch, "--whole-trial")
    monkeypatch.setattr(tr, "report", lambda *_: pytest.fail("进入取数阶段"))
    with pytest.raises(ValueError):
        tr.main()


def test_round2_incomplete_approval_ids(scoped):
    config = scoped[1]
    assert tr.report(*scoped, START, END)[0]["卡片驳回"] == 1
    config.pop("approval_ids")
    assert tr.report(*scoped, START, END)[0]["卡片驳回"] == tr.UNKNOWN
    config["approval_ids"] = []
    public, _ = tr.report(*scoped, START, END)
    assert public["卡片驳回"] == tr.UNKNOWN and public["卡片驳回已核"] == 1


def test_round2_summary_history(scoped):
    path, _, api = scoped
    api.add("extra", "outside", "q", "A", "robot")
    end = END + timedelta(days=1)
    public, private = tr.report(*scoped, END, end)
    assert public["新发现的历史异常已核"] == 1
    previous = path / f"trial-report-{END.date()}.json"
    previous.touch(mode=0o600)
    previous.write_text(json.dumps(private))
    public, _ = tr.report(*scoped, START, START + timedelta(days=14))
    assert public["明道云写错已核"] == public["新发现的历史异常已核"] == 1


def test_round2_archived_cost_and_rejection(scoped):
    path, config, _ = scoped
    public, private = tr.report(*scoped, START, END)
    assert public["总费用美元已核"] == public["总费用美元"] == "0.000420000"
    assert public["卡片驳回已核"] == public["卡片驳回"] == 1
    archive = path / "trial-report-2026-01-01.json"
    archive.touch(mode=0o600)
    archive.write_text(json.dumps(private))
    with sqlite3.connect(path / "trial-050.sqlite3") as db:
        db.execute("DELETE FROM charge")
    with sqlite3.connect(path / config["entry_db"]) as db:
        db.execute("DELETE FROM feishu_approvals")
    for end in (END, START + timedelta(days=14)):
        public, _ = tr.report(*scoped, START, end)
        assert public["卡片驳回"] == public["总费用美元"] == tr.UNKNOWN
        assert public["卡片驳回已核"] == 1
        assert public["总费用美元已核"] == "0.000420000"
        assert public["sonnet费用美元已核"] == "0.000400000"
        assert public["haiku费用美元已核"] == "0.000020000"


def test_round2_wrong_archive_window(scoped, monkeypatch):
    cli_fixture(*scoped, monkeypatch, "--end", END.isoformat())
    tr.main()
    path, = scoped[0].rglob("trial-report-2026-01-01.json")
    archive = json.loads(path.read_text())
    archive["窗口起点"] = (START + timedelta(hours=1)).isoformat()
    path.write_text(json.dumps(archive))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        tr.main()
    assert path.read_bytes() == before


def test_round2_shifted_start(scoped, monkeypatch):
    cli_fixture(*scoped, monkeypatch, "--start", AT, "--end", END.isoformat())
    with pytest.raises(ValueError):
        tr.main()
    assert not (scoped[0] / "reports").exists()


def test_round2_new_directory_contains_input(scoped, monkeypatch):
    path, config, _ = scoped
    cli_fixture(*scoped, monkeypatch, "--end", END.isoformat())
    (path / "reports").mkdir(mode=0o700)
    dest = path / "reports/2026-01-01"
    dest.mkdir(mode=0o700)
    (dest / "alias").hardlink_to(path / config["entry_db"])
    monkeypatch.setattr(tr, "report", lambda *_: pytest.fail("已进入取数阶段"))
    with pytest.raises(ValueError):
        tr.main()
    assert [p.name for p in dest.iterdir()] == ["alias"]


def test_round2_mismatched_table_count(scoped, monkeypatch):
    api = scoped[2]
    original = api._request

    def restricted(action, rid=None, *, body=None, params=None):
        response = original(action, rid, body=body, params=params)
        if "wholeTableMatchedCount" in response:
            return response | {"wholeTableMatchedCount": response["total"] + 1}
        return response

    monkeypatch.setattr(api, "_request", restricted)
    api.add("visible", "outside", "q", "A", "robot")
    public, _ = tr.report(*scoped, START, END)
    assert public["明道云写错已核"] == 1 and public["明道云写错"] == tr.UNKNOWN


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("kind", ["symbolic", "hard"])
def test_round2_isolated_target_sidecar(scoped, monkeypatch, suffix, kind):
    path, config, _ = scoped
    actual = path / "actual.sqlite"
    entry = path / config["entry_db"]
    entry.rename(actual)
    entry.symlink_to(actual)
    target = path / "reports/summary/trial-summary.json"
    target.parent.parent.mkdir(mode=0o700)
    target.parent.mkdir(mode=0o700)
    target.touch(mode=0o600)
    side = actual.with_name(actual.name + suffix)
    if kind == "hard":
        side.hardlink_to(target)
    else:
        side.symlink_to(target)
    cli_fixture(*scoped, monkeypatch, "--whole-trial")
    monkeypatch.setattr(tr, "report", lambda *_: pytest.fail("已进入取数阶段"))
    with pytest.raises(ValueError):
        tr.main()
    assert target.read_bytes() == b""


def test_round2_registration_across_test_flags(scoped):
    path, _, api = scoped
    ledger = path / "trial-orders.jsonl"
    api.add("test", "outside", "p", "A", "robot")
    with ledger.open("a") as stream:
        for event in ("create_reserved", "create_attempt", "created"):
            stream.write(json.dumps(dict(event=event, registration="r", is_test=True, utc=AT,
                                         name="【测试】fixture", record_id="test" if event == "created" else None)) + "\n")
    add_event(path, event="readback_compared", 结论="写错")
    public, private = tr.report(*scoped, START, END)
    assert public["明道云写错已核"] == 1 and public["明道云写错"] == tr.UNKNOWN
    assert private["登记冲突"] == {"r": ["1", "test"]} and private["写错"] == ["1"]


def test_round2_readback_keeps_record_verdict(scoped):
    add_event(scoped[0], registration="other", event="readback_compared", 结论="写错")
    public, private = tr.report(*scoped, START, END)
    assert public["明道云写错已核"] == 1 and public["明道云写错"] == tr.UNKNOWN
    assert private["写错"] == ["1"] and private["建议"] == "回退"


def test_round2_partial_charge_columns():
    public = tr.charge_counts([
        dict(id=1, model="sonnet", inputs=100, outputs=20),
        dict(id=2, model="sonnet", inputs=None, outputs=None),
        dict(id=3, model="haiku", inputs=100, outputs=20),
    ])
    assert public["总费用美元"] == public["sonnet费用美元"] == tr.UNKNOWN
    assert public["总费用美元已核"] == "0.000420000"
    assert public["sonnet费用美元已核"] == "0.000400000"
    assert public["haiku费用美元已核"] == public["haiku费用美元"] == "0.000020000"


def test_round2_late_consistent_readback(scoped):
    path, _, _ = scoped
    add_event(path, event="readback_unconfirmed", 结论="未确认")
    add_event(path, event="readback_compared", 结论="一致", utc=(END + timedelta(minutes=1)).isoformat())
    public, _ = tr.report(*scoped, START, END)
    assert public["明道云写错"] == tr.UNKNOWN and public["明道云写错已核"] == 0
    public, _ = tr.report(*scoped, END, END + timedelta(days=1))
    assert public["明道云写错"] == public["明道云写错已核"] == 0


def test_round2_charge_identity_conflict(scoped):
    path = scoped[0]
    public, private = tr.report(*scoped, START, END)
    assert public["总费用美元"] == "0.000420000"
    archive = path / "trial-report-2026-01-01.json"
    archive.touch(mode=0o600)
    archive.write_text(json.dumps(private))
    with sqlite3.connect(path / "trial-050.sqlite3") as db:
        db.execute("UPDATE charge SET inputs=101 WHERE model='sonnet'")
        store = Store.__new__(Store)
        store.db = db
        store._seal_ledger()
    for _ in range(2):
        public, private = tr.report(*scoped, START, END)
        assert public["总费用美元"] == public["sonnet费用美元"] == tr.UNKNOWN
        assert public["总费用美元已核"] == "0.000422000"
        assert public["haiku费用美元"] == "0.000020000"
        archive.write_text(json.dumps(private))

COVERAGE = ("已回读核对单数", "机器人建单台账总单数", "建单国家已回读核对单数", "建单国家机器人建单台账总单数")


def test_fixed_reasons_accumulate_without_record_content(dataset):
    path, config, _ = dataset
    secret = "synthetic-private-marker"
    ledger = path / "trial-orders.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    for row in rows:
        if row.get("record_id"):
            row["record_id"] = secret
        row.pop("request_fields", None)
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows if row["event"] != "readback_compared"))
    config["approval_ids"] = [secret]
    public, private = tr.report(*dataset, START, END)
    assert set(public["明道云写错原因"]) >= {tr.Reason.MANIFEST, tr.Reason.READ, tr.Reason.SCOPE}
    assert public["卡片驳回原因"] == [tr.Reason.REJECTIONS]
    assert "收到申请原因" not in public
    assert secret not in json.dumps([public, private["指标"]], ensure_ascii=False)
    for key, value in public.items():
        if value == tr.UNKNOWN:
            phrases = public[key + "原因"]
            assert phrases and set(phrases) <= set(tr.Reason)
            assert private["指标"][key + "原因"] == phrases


def test_ledger_coverage_by_window_and_country(scoped):
    path, _, api = scoped
    ledger = path / "trial-orders.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    for rid, country, verdict in [("wrong", "A", "写错"), ("unfinished", "A", None), ("other", "B", "一致")]:
        api.add(rid, rid, "p", country, "robot")
        with ledger.open("a") as stream:
            for row in rows:
                if row["event"] == "card_published" or row["event"] == "readback_compared" and not verdict:
                    continue
                copy = row | {"registration": rid}
                if "record_id" in copy:
                    copy.update(record_id=rid, country=country)
                if "结论" in copy:
                    copy["结论"] = verdict
                stream.write(json.dumps(copy) + "\n")
    api.add("outside-ledger", "outside", "p", "A", "robot")
    for start, expected in [(START, [3, 4, 2, 3]), (END, [0, 0, 0, 0])]:
        public, private = tr.report(*scoped, start, start + tr.DAY)
        for metrics in (public, private["指标"]):
            assert [metrics[k] for k in COVERAGE] == expected
            assert all(k + "原因" not in metrics for k in COVERAGE)


@pytest.mark.parametrize("damaged", [False, True])
def test_ledger_coverage_unreadable(scoped, damaged):
    ledger = scoped[0] / "trial-orders.jsonl"
    if damaged:
        with ledger.open("a") as stream:
            stream.write("synthetic-private-corruption")
    else:
        ledger.unlink()
    public, private = tr.report(*scoped, START, END)
    for metrics in (public, private["指标"]):
        for key in COVERAGE:
            assert metrics[key] == tr.UNKNOWN
            assert metrics[key + "原因"] == [tr.Reason.LEDGER]
        assert "synthetic-private-corruption" not in json.dumps(metrics)
