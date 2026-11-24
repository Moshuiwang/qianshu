"""生产 CLI 离线端点。只有传输与收信 close 故障由夹具提供。"""
import asyncio
import json
import sqlite3
from threading import Event

import listener_replay as replay


def frames(scenario):
    encode = replay.encode
    group = replay.payload("im.message.receive_v1")
    card = replay.payload("card.action.trigger")
    split = group.index(b'"event_type"')
    if scenario == "burst":
        for i in range(9):
            yield encode(str(i), group.replace(b'"live"', f'"m{i}"'.encode()))
    elif scenario == "single":
        yield encode("group", group)
    else:
        if scenario == "unknown":
            assert card[:split] == group[:split]
            for i in range(16):
                yield encode(str(i), card[:split], count=2)
        yield encode("group", group[:split], count=2)
        if scenario == "interleaved":
            yield encode("card", card, kind="card")
        yield encode("group", group[split:], count=2, seq=1)
    if scenario != "burst":
        yield encode("repeat", group)


def install(scenario, close_fault):
    sdk = replay.sdk
    discover = sdk._Connection._discover_endpoint

    async def capture(client):
        nonlocal listener
        listener = client
        return await discover(client)

    async def offline_token(self, *args, **kwargs):
        raise RuntimeError("离线回放未开放群通知端点")

    sdk._Transport._tenant_token = offline_token
    listener = None
    sdk._Connection._discover_endpoint = capture

    class Endpoint:
        close_code = None

        def __init__(self):
            self.frames = iter(frames(scenario))
            self.writable = asyncio.Event()

        async def recv(self):
            raw = next(self.frames, None)
            if raw is not None:
                return raw
            asyncio.get_running_loop().call_later(0.05, self.writable.set)
            # 全部已调度的回包完成后再让观察端读结果。
            await asyncio.sleep(0)
            await asyncio.gather(*(listener.frames - {asyncio.current_task()}))
            (replay.case / "stimulus").touch()
            await asyncio.Future()

        async def send(self, raw):
            frame = replay.ws.Frame()
            frame.ParseFromString(raw)
            if frame.method == replay.ws.FrameType.DATA.value:
                if scenario == "burst":
                    await self.writable.wait()
                key = replay.ws._get_by_key(frame.headers, replay.ws.HEADER_MESSAGE_ID)
                replay.record("response", [key, json.loads(frame.payload)])

        async def close(self):
            replay.record("closed")

    async def dial(url, **options):
        return Endpoint()

    sdk.ws.websockets.connect = dial
    if close_fault:
        opened = sqlite3.connect

        class FaultyClose(sqlite3.Connection):
            def close(self):
                replay.record("mail-close")
                if close_fault == "raise":
                    raise OSError
                Event().wait()

        def connect(path, *args, **kwargs):
            if str(path).endswith("/trial-050.sqlite3"):
                kwargs["factory"] = FaultyClose
            return opened(path, *args, **kwargs)

        sqlite3.connect = connect
