"""首片在平台 event_type 前截断时，经真实 SDK 收帧循环验证入场。"""

import asyncio
import json
import importlib.util
import subprocess
from types import SimpleNamespace

import pytest

from qianshu.feishu import handoff, sdk
from qianshu.feishu.sidecar import Sidecar


def fragment(key, seq, payload):
    ws = sdk.ws
    frame = ws.Frame(method=ws.FrameType.DATA.value, SeqID=0, LogID=0, service=1,
                     payload=payload)
    for name, value in ((ws.HEADER_TYPE, "event"), (ws.HEADER_MESSAGE_ID, key),
                        (ws.HEADER_SUM, "2"), (ws.HEADER_SEQ, str(seq))):
        header = frame.headers.add()
        header.key, header.value = name, value
    return frame.SerializeToString()


async def replay(prefix_size, incomplete, connection, sidecar=None):
    prefix = b'{"schema":"2.0","header":{"app_id":"app",'.ljust(prefix_size)
    # Each unfinished event can be completed as either type. The IDs are opaque.
    card_end = b'"event_type":"card.action.trigger"},"event":{}}'
    assert json.loads(prefix + card_end)["header"]["event_type"] == "card.action.trigger"
    event = {"sender": {"sender_type": "user", "sender_id": {"open_id": "actor"}},
             "message": {"message_id": "live", "chat_id": "group", "chat_type": "group",
                         "create_time": "1001000", "message_type": "text", "content": "{}",
                         "mentions": [{"key": "@_user_1", "id": {"open_id": "bot"}}]}}
    group_end = b'"event_type":"im.message.receive_v1"},"event":' + json.dumps(event).encode() + b'}'
    frames = [fragment(str(i), 0, prefix) for i in range(incomplete + 1)]
    key = str(incomplete)
    frames.append(fragment(key, 1, group_end))
    sent = []
    forwarded = []
    drained = asyncio.Event()
    pending = iter(frames)

    async def recv():
        raw = next(pending, None)
        if raw is not None:
            return raw
        drained.set()
        await asyncio.Future()

    async def send(raw):
        frame = sdk.ws.Frame()
        frame.ParseFromString(raw)
        sent.append((sdk.ws._get_by_key(frame.headers, sdk.ws.HEADER_MESSAGE_ID),
                     json.loads(frame.payload)["code"]))

    socket = SimpleNamespace(close_code=None, recv=recv, send=send)
    settings = SimpleNamespace(app_id="app", app_secret="synthetic", tenant_key=None,
                               bot_open_id="bot")
    entrance = SimpleNamespace(inject_message=lambda msg, **kw: forwarded.append(msg.message_id))
    service = SimpleNamespace(_origin=socket, settings=settings, entrance=entrance,
                              _sidecar=sidecar, _callback_response_margin=0.1)
    client = connection(service)
    client._conn = socket
    receiver = asyncio.create_task(client._receive_message_loop())
    try:
        async with asyncio.timeout(3):
            await drained.wait()
            while client.frames - {receiver}:
                await asyncio.gather(*(client.frames - {receiver}))
    finally:
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
    return forwarded, sent


@pytest.mark.parametrize(("prefix_size", "incomplete"), [(64, 14), (256 << 10, 8)],
                         ids=["unknown-slots-full", "unknown-bytes-full"])
def test_truncated_group_after_truncated_card_events(prefix_size, incomplete, tmp_path):
    path = tmp_path / "baseline.py"
    path.write_bytes(subprocess.check_output(["git", "show", "917dc13:src/qianshu/feishu/sdk.py"]))
    spec = importlib.util.spec_from_file_location("qianshu.feishu._baseline", path)
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    original = lambda service: baseline._Connection(service, approvals=False)
    assert asyncio.run(replay(prefix_size, incomplete, original)) == (["live"], [(str(incomplete), 200)])
    # 仅测试注入通道允许拒绝首片不可归类的群组。
    assert asyncio.run(replay(prefix_size, incomplete, handoff.HandoffService._connection,
                              Sidecar(tmp_path))) == ([], [])
