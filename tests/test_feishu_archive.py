import json
import shutil
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date

import pytest

from qianshu.config import Config
from qianshu.draft import SourceExcerpt
from qianshu.feishu import archive as a


class Transport:
    def __init__(self):
        self.pages = {name: ["历史行"] for name in ("Target", "Public")}
        self.writes = []
        self.permission = {}
        self.reject = self.lose = self.pause = None
        self.landed = threading.Event()
        self.release = threading.Event()

    def request(self, method, path, *, query, body):
        if path.endswith("get_node"):
            return {"node": {"obj_token": "Target", "obj_type": "docx"}}
        if method == "GET" and not path.endswith("/public"):
            return {"document": {"document_id": path.split("/")[-1]}}
        if method == "GET":
            if self.lose == "verify":
                raise TimeoutError
            return {"permission_public": self.permission}
        self.writes.append((method, path, query, body))
        if method == "PATCH":
            stage = "share"
        elif path.endswith("/children"):
            stage = path.split("/")[-4]
        else:
            stage = "create"
        if self.reject == stage:
            raise a.ArchiveRejected
        if stage == "share":
            self.permission = body
            result = {}
        elif stage == "create":
            self.pages["Created"] = []
            result = {"document": {"document_id": "Created"}}
        else:
            assert body["index"] == -1
            self.pages[stage].extend(body["children"])
            result = {"children": [{"block_id": f"block{i}"} for i in range(len(body["children"]))]}
        if self.pause == stage:
            self.landed.set()
            assert self.release.wait(5)
        if self.lose == stage:
            raise TimeoutError
        return result


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(a, "_JOURNALS", tmp_path / "journals")
    monkeypatch.setattr(a, "_WAIT", .04)
    path = tmp_path / "registration.db"
    a.initialize_archive_ledger(path)
    domain = "https://" + "feishu.cn"
    cfg = Config({a._ROUTES["定向政策"]: domain + "/wiki/Node",
                  a._ROUTES["公开政策"]: domain + "/docx/Public",
                  a._SHARE: "anyone_readable", "FEISHU_APP_ID": "syntheticapp",
                  "FEISHU_APP_SECRET": "synthetickey"})
    texts = ("活动原文", "数量原文", "政策原文", "场景原文", "标签原文")
    body = "\n".join(texts)
    group = {f: SourceExcerpt(start=body.index(v), text=v) for f, v in zip(a._FIELDS, texts)}
    batch = a.VerifiedArchiveBatch("batch", "revision", date(2030, 1, 1), "区域甲", "X8a", body,
                                   tuple(a.ArchivePolicy(k, (group,)) for k in a._ROUTES), True)
    transport = Transport()

    def register(b=batch, **kwargs):
        args = dict(approved_country="区域甲", email_verified=True, owner_release="X8a",
                    expires=time.time()+60, sensitive_values=())
        a.register_archive_batch(b, ledger_path=path, **(args | kwargs))

    def run(b=batch, **kwargs):
        return a.archive_batch(b, **(dict(ledger_path=path, config=cfg, transport=transport) | kwargs))

    return batch, path, cfg, transport, register, run


@pytest.mark.parametrize("kinds", [("定向政策",), ("公开政策",), ("公开政策", "定向政策", "公开政策")])
def test_append_only_route_and_cached_link(setup, kinds):
    batch, _, _, stub, register, run = setup
    batch = replace(batch, policies=tuple(replace(batch.policies[0], kind=k) for k in kinds))
    register(batch)
    result = run(batch)
    assert isinstance(result, a.ArchiveSuccess)
    assert result.url.endswith("/docx/Created")
    before = list(stub.writes)
    assert run(batch) == result and stub.writes == before
    for kind, name in zip(a._ROUTES, ("Target", "Public")):
        old, *rows = stub.pages[name]
        assert old == "历史行" and len(rows) == int(kind in kinds)
        if rows:
            text, link = rows[0]["text"]["elements"]
            assert text["text_run"]["content"] == f"【测试】20300101　区域甲　{kinds.count(kind)} 条　"
            assert link["text_run"]["text_element_style"]["link"]["url"] == result.url
    assert stub.permission == {"external_access_entity": "open", "link_share_entity": "anyone_readable"}


@pytest.mark.parametrize("stage", ["create", "Created", "share", "Target", "Public", "verify"])
@pytest.mark.parametrize("fault", ["lose", "reject"])
def test_partial_result_is_terminal(setup, stage, fault):
    _, _, _, stub, register, run = setup
    register()
    if stage == "verify" and fault == "reject":
        stub.permission = {}
        original = stub.request

        def wrong_permission(method, path, **kwargs):
            if method == "GET" and path.endswith("/public"):
                return {"permission_public": {"external_access_entity": "closed", "link_share_entity": "off"}}
            return original(method, path, **kwargs)

        stub.request = wrong_permission
    else:
        setattr(stub, fault, stage)
    result = run()
    assert isinstance(result, a.ArchiveUnknown if fault == "lose" else a.ArchiveFailed)
    assert not hasattr(result, "url")
    before = list(stub.writes)
    assert run() == result and stub.writes == before
    if stage == "Public":
        assert len(stub.pages["Target"]) == 2


def test_late_summary_ack_cannot_thaw_unknown(setup):
    _, _, _, stub, register, run = setup
    register()
    stub.pause = "Public"
    with ThreadPoolExecutor(1) as pool:
        owner = pool.submit(run)
        assert stub.landed.wait(3)
        try:
            result = run()
            assert isinstance(result, a.ArchiveUnknown) and not hasattr(result, "url")
        finally:
            stub.release.set()
        assert owner.result() == result
    writes = list(stub.writes)
    assert run() == result and writes == stub.writes


def test_old_ledger_snapshot_cannot_recreate_doc(setup):
    _, path, _, stub, register, run = setup
    copy = path.with_suffix(".backup")
    empty = path.with_suffix(".empty")
    shutil.copy2(path, empty)
    register()
    shutil.copy2(path, copy)
    assert isinstance(run(), a.ArchiveSuccess)
    before = list(stub.writes)
    for snapshot in (empty, copy):
        assert isinstance(run(ledger_path=snapshot), a.ArchiveUnknown)
    assert before == stub.writes and sum(path == a._DOCS for _, path, _, _ in before) == 1
    # 即使把建档前的快照拷回原 inode，独立执行记录仍保有终态。
    shutil.copyfile(copy, path)
    assert isinstance(run(), a.ArchiveSuccess) and before == stub.writes


@pytest.mark.parametrize("case", ["unverified", "country", "unauthorized", "release", "attachments",
                                  "expired", "summary", "share", "unregistered", "revision"])
def test_gates_before_first_write(setup, case):
    batch, _, cfg, stub, register, run = setup
    args = {"unverified": {"email_verified": False}, "country": {"approved_country": "区域乙"},
            "unauthorized": {"owner_release": ""}, "release": {"owner_release": "X8b"},
            "expired": {"expires": time.time()-1}}
    if case == "attachments":
        batch = replace(batch, attachments_resolved=False)
    if case != "unregistered":
        register(batch, **args.get(case, {}))
    if case == "revision":
        batch = replace(batch, version="other")
    config = dict(cfg)
    if case == "summary":
        config[a._ROUTES["公开政策"]] = config[a._ROUTES["定向政策"]]
    if case == "share":
        config[a._SHARE] = "anyone_editable"
    result = run(batch, config=Config(config))
    assert isinstance(result, a.ArchiveFailed) and not hasattr(result, "url")
    assert stub.writes == []


@pytest.mark.parametrize("mode", ["folded_header", "credential_fragment", "split_fragment"])
def test_sensitive_excerpt_never_leaves_transport(setup, mode):
    batch, _, _, stub, register, run = setup
    secret = "组合密钥材料禁止披露"
    fragment = "材料禁止"
    header = "折叠头的续行"
    context = f"From: synthetic\r\n\t{header}\r\n\r\n凭据 {secret}\n{fragment}\n材料\n禁止"
    body = batch.body + "\n" + context
    fields = dict(batch.policies[0].groups[0])
    parts = {"folded_header": [header], "credential_fragment": [fragment],
             "split_fragment": ["材料", "禁止"]}[mode]
    for key, text in zip(fields, parts):
        fields[key] = SourceExcerpt(start=body.rindex(text), text=text)
    batch = replace(batch, body=body, policies=(a.ArchivePolicy("定向政策", (fields,)),))
    register(batch, sensitive_values=(secret,))
    assert isinstance(run(batch, sensitive_values=(secret,)), a.ArchiveSuccess)
    payload = json.dumps(stub.writes, ensure_ascii=False)
    assert all(s not in payload for s in [header, secret, fragment, *parts])
    assert "[已脱敏]" in payload and "标签原文" in payload


def test_lost_local_receipt_freezes_without_replay(setup):
    batch, _, _, stub, register, run = setup
    register()
    with sqlite3.connect(a._journal(batch.registration)) as db:
        db.execute("""CREATE TRIGGER lose_receipt BEFORE INSERT ON receipts
                      BEGIN SELECT RAISE(ABORT, 'synthetic'); END""")
    result = run()
    assert isinstance(result, a.ArchiveUnknown)
    assert len(stub.writes) == 1
    assert run() == result and len(stub.writes) == 1


def test_crash_after_claim_cannot_replay(setup):
    _, _, _, stub, register, run = setup
    register()
    original = stub.request

    def crash(method, path, **kwargs):
        response = original(method, path, **kwargs)
        if path == a._DOCS:
            raise SystemExit
        return response

    stub.request = crash
    with pytest.raises(SystemExit):
        run()
    assert isinstance(run(), a.ArchiveUnknown)
    assert len(stub.writes) == 1


def test_crash_between_confirmed_steps_resumes_once(setup, monkeypatch):
    _, _, _, stub, register, run = setup
    register()
    authorized = a._authorized

    def crash(*args):
        if stub.writes:
            raise SystemExit
        return authorized(*args)

    with monkeypatch.context() as patch:
        patch.setattr(a, "_authorized", crash)
        with pytest.raises(SystemExit):
            run()
    assert isinstance(run(), a.ArchiveSuccess)
    assert sum(path == a._DOCS for _, path, _, _ in stub.writes) == 1


def test_changed_sensitive_registry_blocks_export(setup):
    _, _, _, stub, register, run = setup
    register(sensitive_values=("合成登记材料",))
    assert isinstance(run(), a.ArchiveFailed) and not stub.writes


def test_expired_grant_blocks_next_write(setup):
    _, path, _, stub, register, run = setup
    register()
    request = stub.request

    def expire(method, uri, **kwargs):
        result = request(method, uri, **kwargs)
        if uri == a._DOCS:
            with sqlite3.connect(path) as db:
                db.execute("UPDATE grants SET expires=0")
        return result

    stub.request = expire
    assert isinstance(run(), a.ArchiveFailed)
    assert len(stub.writes) == 1


def test_sdk_builds_fixed_requests_offline(setup, monkeypatch):
    import lark_oapi
    from types import SimpleNamespace

    _, _, _, stub, register, run = setup

    async def reply(client, req):
        assert req.token_types == {lark_oapi.AccessTokenType.TENANT}
        assert req.uri.startswith("/open-apis/")
        data = stub.request(req.http_method.name, req.uri, query=dict(req.queries), body=req.body)
        return SimpleNamespace(raw=SimpleNamespace(status_code=200, content=json.dumps({"code": 0, "data": data})))

    monkeypatch.setattr(lark_oapi.Client, "arequest", reply)
    register()
    assert isinstance(run(transport=None), a.ArchiveSuccess)
    assert len(stub.pages["Public"]) == len(stub.pages["Target"]) == 2


def test_missing_history_cannot_be_reinitialized(setup):
    batch, _, _, stub, register, run = setup
    register()
    assert isinstance(run(), a.ArchiveSuccess)
    before = list(stub.writes)
    journal = a._journal(batch.registration)
    journal.rename(journal.with_suffix(".offline"))
    assert isinstance(run(), a.ArchiveUnknown)
    with pytest.raises(ValueError):
        register()
    assert not journal.exists() and stub.writes == before


def test_unreadable_share_permission_remains_unknown(setup):
    _, _, _, stub, register, run = setup
    register()
    request = stub.request

    def deny_read(method, path, **kwargs):
        if method == "GET" and path.endswith("/public"):
            raise a.ArchiveRejected
        return request(method, path, **kwargs)

    stub.request = deny_read
    result = run()
    assert isinstance(result, a.ArchiveUnknown) and not hasattr(result, "url")
    assert run() == result
    assert stub.pages["Public"] == stub.pages["Target"] == ["历史行"]
