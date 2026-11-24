"""G448：第三方审核 2 条 P1 与纠错中断中间态的回归测试。"""
from types import SimpleNamespace

import httpx

from qianshu.feishu.cards import CardLine, detail_pages
from qianshu.mail import trial as trial_module
from qianshu.mail.trial_state import BODY_NOTICE_COUNTS_SQL, Hold
from test_trial_journey import journey, mixed


def _call(outputs):
    return SimpleNamespace(input_tokens=10, output_tokens=outputs, partial_input_tokens=None,
                           partial_output_tokens=None, failure_stage=None)


def _drive(monkeypatch, calls, *, refuse_correction=False):
    """替换解析流水线：按 calls 依次走 reserve / result，模拟首轮 + 纠错两次模型调用。"""
    hooks = {}
    request = httpx.Request("POST", "https://synthetic.invalid", json={"model": "claude-sonnet-5-5", "max_tokens": 100})

    def model(config, reserve, result):
        hooks.update(reserve=reserve, result=result)

    class Pipeline:
        def __init__(self, **kwargs):
            pass

        def __call__(self, application):
            for index, call in enumerate(calls):
                if index and refuse_correction:
                    hooks["trial"].switches = dict(hooks["trial"].switches, MAIL_TRIAL_CARD_ENABLED="false")
                try:
                    with hooks["reserve"](request):
                        pass
                except Hold:
                    return detail_pages("合成", [CardLine("合成", "合成")])
                hooks["result"](call)
            return detail_pages("合成", [CardLine("合成", "合成")])

    monkeypatch.setattr(trial_module, "trial_model", model)
    monkeypatch.setattr(trial_module, "Pipeline", Pipeline)
    return hooks


def _parse(journey, monkeypatch, calls, **kwargs):
    worker, _, _, cards = journey(mixed(("plain", "合成申请正文")), "是")
    hooks = _drive(monkeypatch, calls, **kwargs)
    hooks["trial"] = worker
    worker.poll()
    worker.poll()
    return worker, cards


def test_first_call_incomplete_usage_survives_successful_correction(journey, monkeypatch):
    worker, _ = _parse(journey, monkeypatch, [_call(None), _call(5)])
    row = worker.store.row("SELECT status FROM registration")
    assert row["status"] == "manual"


def test_both_calls_complete_still_done(journey, monkeypatch):
    worker, _ = _parse(journey, monkeypatch, [_call(5), _call(5)])
    assert worker.store.row("SELECT status FROM registration")["status"] == "done"


def test_correction_interrupted_goes_manual_with_notice(journey, monkeypatch):
    worker, cards = _parse(journey, monkeypatch, [_call(5), _call(5)], refuse_correction=True)
    row = worker.store.row("SELECT status, reason FROM registration")
    assert (row["status"], row["reason"]) == ("manual", "correction_interrupted")
    assert worker.store.row("SELECT status FROM notice WHERE kind='interrupted'")["status"] == "sent"
    assert (False, trial_module._NOTICES["interrupted"]) in cards
    # 未发出前计入「待发」，与正文提示同一只读计数
    with worker.store.write() as db:
        db.execute("UPDATE notice SET status='pending' WHERE kind='interrupted'")
    assert tuple(worker.store.db.execute(BODY_NOTICE_COUNTS_SQL).fetchone())[0] >= 1


def test_refreeze_after_legacy_reconcile_sends_new_notice(tmp_path):
    from qianshu.mail.trial_state import Store

    store = Store(tmp_path / "trial")
    with store.write() as db:
        db.execute("INSERT INTO mailbox(scope,generation,validity,cursor,peak,state) VALUES('s',1,2,3,3,'active')")
    store.freeze_legacy("s", 1)
    with store.write() as db:
        db.execute("UPDATE notice SET status='sent' WHERE kind='freeze'")
        db.execute("UPDATE mailbox SET state='active',reason=NULL WHERE scope='s'")  # 人工对账恢复，代次不变
    store.freeze("s", "missing")
    rows = list(store.db.execute("SELECT event, status FROM notice WHERE kind='freeze' ORDER BY rowid"))
    assert [r["status"] for r in rows] == ["sent", "pending"]
    assert rows[0]["event"] != rows[1]["event"]
    store.freeze("s", "mapping")  # 已冻结时重复冻结不刷屏
    assert store.row("SELECT count(*) FROM notice WHERE kind='freeze'")[0] == 2
