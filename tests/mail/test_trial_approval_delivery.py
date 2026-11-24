import json

import pytest

from test_trial_g448 import _call, _drive
from test_trial_journey import journey, mixed


@pytest.mark.parametrize("lost", [False, True])
def test_mail_pages_keep_fixed_key_and_trusted_receipt(journey, monkeypatch, lost):
    from qianshu.feishu.trial_approval import Journal

    worker, _, _, _ = journey(mixed(("plain", "合成申请正文")), "是")
    journal = Journal(worker.store.db)
    journal.rotate("session", "连接")
    worker.approval_setup = lambda: ({}, {}, "ou_config")
    hooks = _drive(monkeypatch, [_call(2)])
    hooks["trial"] = worker
    sent = []

    def deliver(card, key, interactive, check):
        check()
        assert interactive
        assert worker.store.row("SELECT status FROM card_intent WHERE uuid=?", key)[0] == "unknown"
        assert worker.store.row("SELECT status FROM registration")[0] == "sending"
        sent.append((key, card))
        return None if lost and len(sent) == 2 else f"receipt{len(sent)}"

    worker.send = deliver
    worker.poll()
    snap = json.loads(worker.store.row("SELECT snapshot FROM trial_card")[0])
    assert snap["pages"][:len(sent)] == [card for _, card in sent]
    receipts = list(worker.store.db.execute("""SELECT i.uuid,d.message FROM card_intent i
                   LEFT JOIN trial_delivery d USING(uuid) ORDER BY page"""))
    assert [r[0] for r in receipts] == [key for key, _ in sent]
    assert [r[1] for r in receipts] == [None if lost and i == 2 else f"receipt{i}"
                                        for i in range(1, len(sent)+1)]
    assert worker.store.row("SELECT status FROM registration")[0] == ("manual" if lost else "done")
    if lost:
        assert worker.store.row("SELECT body FROM trial_notification WHERE kind='closed'")
    assert not worker.store.row("SELECT 1 FROM trial_task")


def test_parse_uses_same_trusted_tag_snapshot_as_card(journey, monkeypatch):
    from qianshu.feishu.trial_approval import Journal
    from qianshu.mail import trial
    from datetime import date
    worker, _, _, _ = journey(mixed(("plain", "合成申请正文")), "是")
    Journal(worker.store.db).rotate("session", "连接")
    setup = []
    def load():
        setup.append(1)
        return {}, {"tags": [
            {"name": "合成标签", "id": "one", "end": "2030-01-31"},
            {"name": "冲突", "id": "two", "end": "2030-01-31"},
            {"name": "冲突", "id": "three", "end": "2030-02-01"},
        ]}, "ou_config"

    worker.approval_setup = load
    hooks = _drive(monkeypatch, [_call(2)])
    hooks["trial"] = worker
    pipeline = trial.Pipeline
    dates = []
    def capture(**kwargs):
        dates.append(kwargs["tag_dates"])
        return pipeline(**kwargs)
    monkeypatch.setattr(trial, "Pipeline", capture)
    worker.send = lambda card, key, interactive, check: check() or "receipt" + key
    worker.poll()
    assert setup == [1] and dates == [{"合成标签": date(2030, 1, 31)}]
