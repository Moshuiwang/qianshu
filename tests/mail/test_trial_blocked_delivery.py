from qianshu.mingdao.create_order import OutletConflict
from test_trial_g448 import _call, _drive
from test_trial_journey import journey, mixed


def test_local_card_block_records_manual_handoff(journey, monkeypatch):
    worker, _, _, _ = journey(mixed(("plain", "合成申请正文")), "是")
    _drive(monkeypatch, [_call(2)])["trial"] = worker
    messages = []

    def deliver(content, key, card, check):
        check()
        if card:
            raise OutletConflict()
        messages.append(content)
        return "receipt"

    worker.send = deliver
    worker.poll()
    worker.flush()
    assert worker.store.row("SELECT status FROM card_intent")[0] == "blocked"
    assert worker.store.row("SELECT status FROM registration")[0] == "manual"
    assert any("转人工" in text and "未建单" in text for text in messages)


def test_blocked_received_notice_does_not_mark_transport_unknown(journey):
    worker, _, _, _ = journey(mixed(("plain", "合成申请正文")), "是")
    with worker.store.write():
        worker.store.notice("received:1", worker.scope, 1, "received")
    delivered = []

    def send(content, key, card, claim):
        claim()
        if content.startswith("收到"):
            raise OutletConflict()
        delivered.append(content)
        return "receipt"

    worker.send = send
    worker.flush()
    worker.flush()
    assert worker.store.row("SELECT status FROM notice WHERE event='received:1'")[0] == "blocked"
    assert worker.store.row("SELECT reason FROM registration")[0] == "outlet_blocked"
    assert len(delivered) == 1 and "登记 1" in delivered[0]
