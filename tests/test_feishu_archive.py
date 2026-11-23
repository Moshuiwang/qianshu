import json
import shutil
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from unittest.mock import Mock

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
        if "preview_fingerprint" not in kwargs:
            try:
                preview = a.preview_archive_batch(b, config=cfg, sensitive_values=kwargs.get("sensitive_values", ()))
                args["preview_fingerprint"] = preview.fingerprint
            except ValueError:
                pass
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
        while not owner.done() and not stub.landed.wait(0.1):
            continue
        assert stub.landed.is_set()
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
    # 必需明细被整段遮盖时不得交付链接（审核 P1-5）；敏感片段在任何情况下都不得出站。
    assert isinstance(run(batch, sensitive_values=(secret,)), a.ArchiveFailed)
    payload = json.dumps(stub.writes, ensure_ascii=False)
    assert all(s not in payload for s in [header, secret, fragment, *parts])


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


def test_unavailable_grant_freezes_late_ack(setup):
    _, path, _, stub, register, run = setup
    register()
    stub.pause = "Public"
    with ThreadPoolExecutor(1) as pool:
        owner = pool.submit(run)
        assert stub.landed.wait(15)
        try:
            path.chmod(0o640)
            assert isinstance(run(), a.ArchiveUnknown)
        finally:
            path.chmod(0o600)
            stub.release.set()
        assert isinstance(owner.result(), a.ArchiveUnknown)
    writes = list(stub.writes)
    assert isinstance(run(), a.ArchiveUnknown) and stub.writes == writes


def test_journal_restore_in_place_stops(setup):
    batch, _, _, stub, register, run = setup
    register()
    journal = a._journal(batch.registration)
    backup = journal.with_suffix(".backup")
    shutil.copyfile(journal, backup)
    assert isinstance(run(), a.ArchiveSuccess)
    writes = list(stub.writes)
    shutil.copyfile(backup, journal)
    assert isinstance(run(), a.ArchiveUnknown) and stub.writes == writes


def excerpt_batch(batch, field, context, quote):
    body = batch.body + "\n" + context
    fields = dict(batch.policies[0].groups[0])
    fields[field] = SourceExcerpt(start=body.rindex(quote), text=quote)
    return replace(batch, body=body, policies=(a.ArchivePolicy("定向政策", (fields,)),))


def test_thread_topic_fold_blocks_even_with_previously_matching_preview(setup):
    batch, _, cfg, _, register, run = setup
    detail = "模拟折行"
    plain = excerpt_batch(batch, "使用场景", detail, detail)
    old_preview = a.preview_archive_batch(plain, config=cfg)
    context = "> Thread-Topic: 模拟字段\r\n> \t" + detail
    batch = excerpt_batch(batch, "使用场景", context, detail)
    register(batch, preview_fingerprint=old_preview.fingerprint)
    transport = Mock()
    assert run(batch, transport=transport) == a.ArchiveFailed("prepare", "material")
    transport.request.assert_not_called()
    with pytest.raises(ValueError):
        a.preview_archive_batch(batch, config=cfg)


@pytest.mark.parametrize("anchor", ["tHrEaD-ToPiC", "Thread-Index", "Sent"])
def test_preview_masks_entire_header_block(setup, anchor):
    batch, _, cfg, _, _, _ = setup
    context = (
        "> Extra!: 甲\r\n>  乙\r\n"
        f"> {anchor}: 丙\r\n> \t丁\r\n"
        "> Extra-Tail: 戊\r\n>  己\r\n>\r\nScenario: 可见明细"
    )
    batch = excerpt_batch(batch, "使用场景", context, context)
    preview = a.preview_archive_batch(batch, config=cfg)
    assert preview.blocks[3].split("　", 1)[1] == "[已脱敏]\r\n>\r\nScenario: 可见明细"


@pytest.mark.parametrize("label,quote,allowed", [
    ("> From: synthetic\r\n> \t", "模拟续行", False),
    ("Scenario: ", "仅面向现有用户", True),
    # 紧跟已知头字段的「标签:」行属于同一头部块，按块遮盖（第 2 轮口径），必需明细缺失即失败。
    ("> From: synthetic\r\n> Scenario: ", "仅面向现有用户", False),
    (">From: synthetic\r\n> ", "模拟续行", False),
    ("Subject: ", "模拟主题", False),
])
def test_required_scenario_visibility(setup, label, quote, allowed):
    batch, _, _, stub, register, run = setup
    batch = excerpt_batch(batch, "使用场景", label + quote, quote)
    register(batch)
    result = run(batch)
    if allowed:
        assert isinstance(result, a.ArchiveSuccess)
        assert quote in json.dumps(stub.writes, ensure_ascii=False)
    else:
        assert isinstance(result, a.ArchiveFailed) and not stub.writes


def test_sdk_project_log_hides_document_ids(setup, monkeypatch):
    import io
    import logging
    import httpx
    from lark_oapi.core.token import TokenManager
    from qianshu.logs import setup_logging

    _, _, cfg, stub, register, run = setup
    monkeypatch.setattr(TokenManager.cache, "get", lambda key: "synthetic")
    client = httpx.AsyncClient

    def reply(req):
        body = json.loads(req.content) if req.content else None
        data = stub.request(req.method, req.url.path, query=dict(req.url.params), body=body)
        return httpx.Response(200, json={"code": 0, "data": data})

    monkeypatch.setattr(httpx, "AsyncClient", lambda: client(transport=httpx.MockTransport(reply)))
    stream = io.StringIO()
    root = logging.getLogger()
    with monkeypatch.context() as patch:
        # 与测试顺序无关：其他测试可能全局禁用日志或改动子日志器，这里只在本测试内复位。
        previous_disable = logging.root.manager.disable
        try:
            logging.disable(logging.NOTSET)
            patch.setattr(root, "handlers", [])
            patch.setattr(root, "level", logging.INFO)
            for name in ("httpx", "httpcore", "qianshu", "lark_oapi", "Lark"):
                child = logging.getLogger(name)
                patch.setattr(child, "disabled", False)
                patch.setattr(child, "propagate", True)
                patch.setattr(child, "level", logging.NOTSET)
                patch.setattr(child, "handlers", [])
            patch.setattr("sys.stderr", stream)
            setup_logging(cfg)
            register()
            assert isinstance(run(transport=None), a.ArchiveSuccess)
            logging.getLogger("httpx").info("ordinary request")
        finally:
            logging.disable(previous_disable)
    assert "archive_request " in stream.getvalue()
    assert "ordinary request" in stream.getvalue()
    assert all(s not in stream.getvalue() for s in ("Created", "Target", "/open-apis/"))


@pytest.mark.parametrize("binding", ["missing", "old_preview", "matching"])
def test_x8b_preview_binds_exact_published_blocks(setup, binding):
    batch, _, cfg, stub, register, run = setup
    batch = replace(batch, purpose="X8b")
    original = a.preview_archive_batch(batch, config=cfg)
    quote = "政策原文\n内部审批备注\n模拟终端账号"
    batch = excerpt_batch(batch, "政策内容", quote, quote)
    preview = a.preview_archive_batch(batch, config=cfg)
    assert quote in preview.body and preview.fingerprint != original.fingerprint
    digest = {"missing": None, "old_preview": original.fingerprint, "matching": preview.fingerprint}[binding]
    register(batch, owner_release="X8b", preview_fingerprint=digest)
    result = run(batch)
    if binding == "matching":
        assert isinstance(result, a.ArchiveSuccess)
        paragraphs = [child["text"]["elements"][0]["text_run"]["content"] for child in stub.pages["Created"]]
        assert tuple(paragraphs) == preview.blocks
    else:
        assert isinstance(result, a.ArchiveFailed) and stub.writes == []


@pytest.mark.parametrize("parts", [
    ("可公开说明\n> From: synthetic\r\n> \t模拟续行", "模拟续行"),
    ("可公开说明材料禁止", "材料禁止"),
])
def test_partially_visible_required_excerpt(setup, parts):
    batch, _, _, stub, register, run = setup
    text, hidden = parts
    batch = excerpt_batch(batch, "使用场景", text, text)
    sensitive = ("组合密钥材料禁止披露",)
    register(batch, sensitive_values=sensitive)
    assert isinstance(run(batch, sensitive_values=sensitive), a.ArchiveSuccess)
    payload = json.dumps(stub.writes, ensure_ascii=False)
    assert "可公开说明" in payload and "[已脱敏]" in payload and hidden not in payload


def test_split_secret_with_visible_detail_on_both_sides(setup):
    batch, _, _, stub, register, run = setup
    left, right = "活动说明材料", "禁止数量说明"
    body = batch.body + "\n" + left + "\n" + right
    fields = dict(batch.policies[0].groups[0])
    for field, quote in zip(a._FIELDS[:2], (left, right), strict=True):
        fields[field] = SourceExcerpt(start=body.index(quote), text=quote)
    batch = replace(batch, body=body, policies=(a.ArchivePolicy("定向政策", (fields,)),))
    sensitive = ("组合密钥材料禁止披露",)
    register(batch, sensitive_values=sensitive)
    assert isinstance(run(batch, sensitive_values=sensitive), a.ArchiveSuccess)
    payload = json.dumps(stub.writes, ensure_ascii=False)
    assert "活动说明[已脱敏]" in payload and "[已脱敏]数量说明" in payload
    assert "材料" not in payload and "禁止" not in payload


_SCENARIO = "Scenario: 仅面向现有用户"
_QUOTED_FROM = "> From: synthetic"
_FOLDS = (
    "> From: synthetic\r\n \t\r\n> X-Internal: 合成内部字段值",
    "> From: synthetic\n> X-Internal: synthetic\n>> \t合成内部字段值",
)
_SAME_DEPTH = "> Extra!: 甲\r\n>  乙\r\n\t> Subject: 丙\r\n>Tail: 尾字段值\r\n>\t丁"
_NESTED = ("> From: 甲", ">> Extra: 引用明细", "> Sent: 乙",
           "> > Subject: 丙", "\t>> Tail: 丁", ">> \t折行")


# ec8917f 对下列合成批次生成的预览指纹。
_OLD_PREVIEW_FINGERPRINT = "011cda37eca52f6187ecefed553fa78c412dda245750b1083f60f45e96e3d09b"


def published_paragraphs(stub):
    return tuple(child["text"]["elements"][0]["text_run"]["content"] for child in stub.pages["Created"])


@pytest.mark.parametrize("lines", [(_SCENARIO, _QUOTED_FROM), (_QUOTED_FROM, _SCENARIO)])
def test_unquoted_scenario_adjacent_to_quoted_header(setup, lines):
    batch, _, cfg, stub, register, run = setup
    batch = excerpt_batch(batch, "使用场景", "\n".join(lines), _SCENARIO)
    preview = a.preview_archive_batch(batch, config=cfg)
    assert preview.blocks[3].split("　", 1)[1] == _SCENARIO
    register(batch, preview_fingerprint=preview.fingerprint)
    assert isinstance(run(batch), a.ArchiveSuccess)
    assert published_paragraphs(stub) == preview.blocks


@pytest.mark.parametrize("context", (*_FOLDS, _SAME_DEPTH))
def test_required_fold_or_same_depth_tail_stops_publication(setup, context):
    batch, _, _, _, register, run = setup
    detail = "尾字段值" if context == _SAME_DEPTH else "合成内部字段值"
    batch = excerpt_batch(batch, "使用场景", context, detail)
    register(batch)
    transport = Mock()
    assert run(batch, transport=transport) == a.ArchiveFailed("prepare", "material")
    transport.request.assert_not_called()


@pytest.mark.parametrize("headers", [_SAME_DEPTH, "\n".join(_NESTED), "\r\n".join(_NESTED)])
def test_quoted_header_block_remains_fully_masked(setup, headers):
    batch, _, cfg, _, _, _ = setup
    assert list(a._header_blocks(headers)) == [(0, len(headers))]
    context = headers + "\r\n可见明细"
    batch = excerpt_batch(batch, "使用场景", context, context)
    preview = a.preview_archive_batch(batch, config=cfg)
    assert preview.blocks[3].split("　", 1)[1] == "[已脱敏]\r\n可见明细"


@pytest.mark.parametrize("use_old", [True, False], ids=["old", "new"])
def test_quote_depth_changes_preview_registration(setup, use_old):
    batch, _, cfg, stub, register, run = setup
    context = "可见明细\n" + _QUOTED_FROM + "\n" + _SCENARIO
    batch = excerpt_batch(batch, "使用场景", context, context)
    new = a.preview_archive_batch(batch, config=cfg)
    assert _SCENARIO in new.body
    assert _OLD_PREVIEW_FINGERPRINT != new.fingerprint
    register(batch, preview_fingerprint=_OLD_PREVIEW_FINGERPRINT if use_old else new.fingerprint)
    if use_old:
        assert run(batch) == a.ArchiveFailed("prepare", "preview")
        assert stub.writes == []
    else:
        assert isinstance(run(batch), a.ArchiveSuccess)
        assert published_paragraphs(stub) == new.blocks


_FOLDED_SUBJECT = "> From: 甲\r\n> \tsUbJeCt: 合成折行\r\nX-Internal: 合成内部字段值"
_TWO_FOLDS = (_QUOTED_FROM, ">  合成续行一", "\t合成续行二")
_SCENARIO_AFTER_FOLDS = "\r\n".join((*_TWO_FOLDS, _SCENARIO))
_SCENARIO_BEFORE_FOLDS = "\r\n".join((_SCENARIO, *_TWO_FOLDS))


# 合成原文、ec8917f 实测遮盖区间、允许从区间内扣除的紧邻物理行。
_BASELINE_CASES = (
    (_FOLDED_SUBJECT, ((0, 48),), ()),
    (_SCENARIO_AFTER_FOLDS, ((0, 54),), ()),
    (_SCENARIO_BEFORE_FOLDS, ((0, 54),), ()),
    (_FOLDS[0], ((0, 44),), ()),
    (_FOLDS[1], ((0, 53),), ()),
    (_SAME_DEPTH, ((0, 50),), ()),
    (">From: 甲\n> 续行", ((0, 13),), ()),
    ("> Extra: 甲\n>  续行\n> From: 乙\n>\n" + _SCENARIO, ((0, 26),), ()),
    ("Subject: 甲\n" + _SCENARIO, ((0, 28),), ()),
    (_SCENARIO + "\n \t\nSubject: 甲", ((0, 31),), ()),
    ("> Extra: 甲\n\n" + _SCENARIO, (), ()),
    ("\n".join(_NESTED), ((0, 68),), ()),
    ("\r\n".join(_NESTED), ((0, 73),), ()),
    ("> From: 甲\n" + _SCENARIO + "\n 续行", ((0, 31),), ()),
    (_SCENARIO + "\r\n>> \t折行\r\n>> Subject: 甲", ((0, 40),), ()),
    (_SCENARIO + "\n" + _QUOTED_FROM, ((0, 35),), (_SCENARIO,)),
    (_QUOTED_FROM + "\n" + _SCENARIO, ((0, 35),), (_SCENARIO,)),
    ("> fRoM: 合成甲\r\nScenario: 合成明细\r\nX-Internal: 合成内部字段值", ((0, 48),), ()),
    ("> fRoM: 合成甲\r\n>>\t合成折行\r\nX-Internal: 合成内部字段值", ((0, 41),), ()),
    ("Subject: 甲\n" + _SCENARIO + "\n" + _QUOTED_FROM, ((0, 46),), ()),
)


def test_quote_depth_mask_covers_baseline():
    def covered(spans):
        return {i for start, end in spans for i in range(start, end)}

    for body, baseline_spans, ordinary_fields in _BASELINE_CASES:
        allowed = covered((body.index(field), body.index(field) + len(field)) for field in ordinary_fields)
        old = covered(baseline_spans)
        new = covered(a._header_blocks(body))
        assert new == old - allowed, body


@pytest.mark.parametrize("intervening", ["Scenario: 合成明细", ">>\t合成折行"])
def test_nonadjacent_internal_field_stops_publication(setup, intervening):
    batch, _, _, stub, register, run = setup
    detail = "合成内部字段值"
    context = f"> fRoM: 合成甲\r\n{intervening}\r\nX-Internal: {detail}"
    batch = excerpt_batch(batch, "使用场景", context, detail)
    register(batch)
    transport = Mock(wraps=stub)
    assert run(batch, transport=transport) == a.ArchiveFailed("prepare", "material")
    transport.request.assert_not_called()


def test_folded_subject_keeps_internal_field_masked(setup):
    batch, _, _, stub, register, run = setup
    batch = excerpt_batch(batch, "使用场景", _FOLDED_SUBJECT, "合成内部字段值")
    register(batch)
    transport = Mock(wraps=stub)
    assert run(batch, transport=transport) == a.ArchiveFailed("prepare", "material")
    transport.request.assert_not_called()


@pytest.mark.parametrize("context", [_SCENARIO_AFTER_FOLDS, _SCENARIO_BEFORE_FOLDS])
def test_scenario_adjacent_to_folded_header_stops_publication(setup, context):
    batch, _, _, stub, register, run = setup
    batch = excerpt_batch(batch, "使用场景", context, _SCENARIO)
    register(batch)
    transport = Mock(wraps=stub)
    assert run(batch, transport=transport) == a.ArchiveFailed("prepare", "material")
    transport.request.assert_not_called()


_NEIGHBOR_LINES = {
    "start": (),
    "end": (),
    "single": ("> From: 甲",),
    "fold-last": ("> Subject: 乙", "> \t合成续行"),
    "fold-first": ("> Subject: 乙", "> \t合成续行"),
    "blank": ("",),
    "prose": ("合成正文",),
}
_ABOVE = ("start", "blank", "prose", "single", "fold-last")
_BELOW = ("end", "blank", "prose", "single", "fold-first")
_MATRIX_FIELD = "Extra: 合成内部字段值"


def _neighbor_body(above, below):
    lines = (*_NEIGHBOR_LINES[above], _MATRIX_FIELD, *_NEIGHBOR_LINES[below])
    return "\r\n".join(lines) + ("\r\n" if below == "blank" else "")


# ec8917f 实测遮盖区间，行与列分别对应 _ABOVE 和 _BELOW。
_NEIGHBOR_BASELINE = (
    ((), (), (), ((0, 25),), ((0, 37),)),
    ((), (), (), ((2, 27),), ((2, 39),)),
    ((), (), (), ((6, 31),), ((6, 43),)),
    (((0, 25),), ((0, 25),), ((0, 25),), ((0, 36),), ((0, 48),)),
    (((0, 37),), ((0, 37),), ((0, 37),), ((0, 48),), ((0, 60),)),
)


@pytest.mark.parametrize("below", _BELOW)
@pytest.mark.parametrize("above", _ABOVE)
def test_required_field_neighbor_matrix(setup, above, below):
    body = _neighbor_body(above, below)
    start = body.index(_MATRIX_FIELD)
    field_positions = set(range(start, start + len(_MATRIX_FIELD)))
    baseline = _NEIGHBOR_BASELINE[_ABOVE.index(above)][_BELOW.index(below)]
    old_mask = {i for left, right in baseline for i in range(left, right)}
    allowed = ((above == "single" and below in ("blank", "prose", "end"))
               or (below == "single" and above in ("blank", "prose", "start")))
    expected_mask = old_mask - field_positions if allowed else old_mask

    batch, _, cfg, stub, register, run = setup
    excerpt = SourceExcerpt(start=start, text=_MATRIX_FIELD)
    group = dict.fromkeys(a._FIELDS, excerpt)
    batch = replace(batch, body=body, policies=(a.ArchivePolicy("定向政策", (group,)),))
    if expected_mask & field_positions:
        register(batch)
        transport = Mock(wraps=stub)
        assert run(batch, transport=transport) == a.ArchiveFailed("prepare", "material")
        transport.request.assert_not_called()
    else:
        preview = a.preview_archive_batch(batch, config=cfg)
        assert preview.blocks[3].split("　", 1)[1] == _MATRIX_FIELD
        register(batch, preview_fingerprint=preview.fingerprint)
        assert isinstance(run(batch), a.ArchiveSuccess)
        assert published_paragraphs(stub) == preview.blocks

    actual_mask = {i for left, right in a._header_blocks(body) for i in range(left, right)}
    assert actual_mask == expected_mask


def test_baseline_header_blocks_match_frozen_spans():
    for body, spans, _ in _BASELINE_CASES:
        assert tuple(a._baseline_header_blocks(body)) == spans
    for above, row in zip(_ABOVE, _NEIGHBOR_BASELINE, strict=True):
        for below, spans in zip(_BELOW, row, strict=True):
            assert tuple(a._baseline_header_blocks(_neighbor_body(above, below))) == spans


@pytest.mark.parametrize("lines", [
    ("> From: 甲", _MATRIX_FIELD, "> X-Internal: 合成同层字段", "> Subject: 乙"),
    ("> Subject: 乙", "> X-Internal: 合成同层字段", _MATRIX_FIELD, "> From: 甲"),
    ("> From: 甲", _MATRIX_FIELD, ">> X-Internal: 合成字段", ">>  合成续行", ">> Subject: 乙"),
    (">> Subject: 乙", ">> X-Internal: 合成字段", ">>  合成续行", _MATRIX_FIELD, "> From: 甲"),
])
def test_masked_neighbor_keeps_required_field_private(setup, lines):
    body = "\r\n".join(lines)
    batch, _, _, stub, register, run = setup
    batch = excerpt_batch(batch, "使用场景", body, "合成内部字段值")
    register(batch)
    transport = Mock(wraps=stub)
    assert run(batch, transport=transport) == a.ArchiveFailed("prepare", "material")
    transport.request.assert_not_called()
    assert list(a._header_blocks(body)) == [(0, len(body))]
