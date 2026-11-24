import json
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
def dataset(tmp_path):
    config = dict(trial_start=START.isoformat(), build_country="A", bot_account="robot",
                  controls=dict(link="link", policy="policy", country="area", node_key="node",
                                return_nodes=["returned"], reason_key="reason"),
                  registrations={}, cards={}, entry_scope="s", approval_ids=["a"], entry_db="entry.sqlite")
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
        db.execute("INSERT INTO charge(model,at,inputs,outputs) VALUES('sonnet',?,100,20)", (AT,))
        db.execute("INSERT INTO charge(model,at,inputs,outputs) VALUES('haiku',?,100,20)", (AT,))
    store.db.close()
    with sqlite3.connect(tmp_path / "entry.sqlite") as db:
        db.execute("CREATE TABLE feishu_approvals(scope,approval_id,status,decided_at)")
        db.executemany("INSERT INTO feishu_approvals VALUES('s',?,'reject',?)",
                       [(k, START.timestamp()) for k in ("a", "unrelated")])
    event = dict(utc=AT, registration="r", is_test=False, event="created", record_id="1",
                 approved=True, written=DRAFT, displayed=DRAFT, country="A")
    ledger.write_text(json.dumps(event) + "\n")
    return tmp_path, config, api


def test_all_countries_fields_and_unknown_not_clean(dataset):
    public, private = tr.report(*dataset, START, END)
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


def test_returns_items_and_unregistered_writes_and_misses(dataset):
    path, config, api = dataset
    api.add("1", "1", "p", "A", "robot", returned=True)
    api.add("4", "1", "q", "A", "robot")
    api.add("5", "2", "q", "B", "robot")
    api.add("6", "2", "r", "B", "human")
    api.add("7", "outside", "s", "A", "robot")
    public, private = tr.report(*dataset, START, END)
    assert public["漏条"] == public["明道云写错"] == public["退回公告条目"] == 1
    assert public["建单"] == 2 and private["写错"] == ["4"] and private["漏条"] == ["6"]
    assert private["建议"] == "回退"
    notice, = private["退回公告"]
    assert notice["record_id"] == "1" and notice["notify"] == "配置负责人"
    assert notice["reason"] == "原因未读到，请到明道云查看"
    api.logs["1"][-1]["content"]["reason"] = "private-reason"
    assert tr.report(*dataset, START, END)[1]["退回公告"][0]["reason"] == "private-reason"
    api.logs["1"][-1]["content"]["node"] = "withdrawn"
    assert tr.report(*dataset, START, END)[0]["退回公告条目"] == 0


@pytest.mark.parametrize("mutation", [{"displayed": {"x": 9}}, {"approved": False}, {"comparison": "写错"}])
def test_unapproved_or_mismatched_write(dataset, mutation):
    ledger = dataset[0] / "trial-orders.jsonl"
    row = json.loads(ledger.read_text())
    if "comparison" in mutation:
        row = dict(row, event="readback_unconfirmed", 结论="写错")
        with ledger.open("a") as f:
            f.write(json.dumps(row))
    else:
        ledger.write_text(json.dumps(row | mutation))
    public, private = tr.report(*dataset, START, END)
    assert public["明道云写错"] == 1 and private["建议"] == "回退"


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
    assert all(isinstance(v, int) or v == tr.UNKNOWN or k.endswith("美元") for k, v in output.items())
    detail = path / "trial-report-2026-01-01.json"
    assert detail.stat().st_mode & 0o777 == 0o600 and len(json.loads(detail.read_text())["卡片"]) == 3
    assert before == [p.read_bytes() for p in inputs]


def test_failed_detail_and_missing_link_stay_unknown(dataset, monkeypatch):
    _, config, api = dataset
    api.add("extra", "1", "q", "A", "robot")
    read = api.read

    def fail_one(rid):
        if rid == "extra":
            raise tr.Stop()
        return read(rid)

    monkeypatch.setattr(api, "read", fail_one)
    public, _ = tr.report(*dataset, START, END)
    assert public["漏条"] == public["明道云写错"] == tr.UNKNOWN
    assert public["写入核对未知"] == 1 and public["卡片核对未知"] == 2
    del api.rows["extra"]
    del config["registrations"]["2"]["link"]
    public, _ = tr.report(*dataset, START, END)
    assert public["漏条"] == tr.UNKNOWN and public["明道云写错"] == 0


def test_prior_order_monitored_and_new_period_counts(dataset):
    _, _, api = dataset
    api.add("1", "1", "p", "A", "robot", returned=True)
    api.logs["1"][-1]["content"]["_createdAt"] = END.isoformat()
    public, _ = tr.report(*dataset, END, END + timedelta(days=1))
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
        for event, reg, rid, test in [("created", "t", "test", True), ("create_attempt", "u", None, False)]:
            f.write(json.dumps(dict(event=event, registration=reg, record_id=rid,
                                    name="fixture", is_test=test, utc=AT)) + "\n")
    public, _ = tr.report(*dataset, START, END)
    assert public["建单已核"] == 1 and public["明道云写错已核"] == 0
    assert public["建单"] == public["明道云写错"] == tr.UNKNOWN
