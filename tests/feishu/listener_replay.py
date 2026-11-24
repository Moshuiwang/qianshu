"""正常 CLI 的外部端点、合成时钟和只读计数探针。两个基线也载入同一夹具。"""

import asyncio
import contextlib
import fcntl
import json
import os
import socket
import sqlite3
import sys
import time
from pathlib import Path

from websockets.asyncio.connection import Connection
from websockets.protocol import CLIENT, SERVER, Protocol

from qianshu.feishu import Message, handoff, sdk

case = Path.cwd()
ws = sdk.ws
fault = (case / "fault").read_text()
flood = fault in ("c2", "c2_partial")
clock = [1000]
db = case.parent / "private" / "handoff.db"


def record(tag, value=0):
    with (case / "observed").open("a") as stream:
        stream.write(json.dumps([tag, value, time.monotonic()]) + "\n")


def encode(key, data, *, count=1, seq=0, kind="event"):
    result = ws.Frame(method=ws.FrameType.DATA.value, SeqID=0, LogID=0, service=1, payload=data)
    for k, v in (("type", kind), ("message_id", key), ("sum", str(count)), ("seq", str(seq))):
        head = result.headers.add()
        head.key, head.value = k, v
    return result.SerializeToString()


def payload(kind):
    msg = {"sender": {"sender_type": "user", "sender_id": {"open_id": "ou_owner"}},
           "message": {"message_id": "live", "chat_id": "group", "chat_type": "group",
                       "create_time": "1001000", "message_type": "text", "content": "{}",
                       "mentions": [{"key": "@_user_1", "id": {"open_id": "ou_bot"}}]}}
    return json.dumps({"schema": "2.0", "header": {"app_id": "app", "event_type": kind},
                       "event": msg if kind == "im.message.receive_v1" else {}}).encode()


async def wait_for(path):
    while not path.exists():
        await asyncio.sleep(0.01)


async def wire_limits(peer, group):
    from qianshu.feishu.frame_budget import MESSAGE, PART
    for key in ("large-a", "large-b"):
        for seq in range(4):
            await peer.send(encode(key, b"x" * PART, count=6, seq=seq))
    for frame in (encode("large-a", b"x", count=6, seq=4),  # 每组
                  encode("overflow", b"x", count=2),         # 未知组全局
                  encode("large-b", b"x", count=6, seq=0),
                  encode("long-fragment", bytes(PART + 1), count=2),
                  encode("count", b"x", count=99999999),
                  encode("sequence", b"x", count=2, seq=2),
                  encode("negative", b"x", count=2, seq=-1),
                  encode("h" * 4097, b"x", count=2),
                  encode("forged", b'{"event":{"header":{"app_id":"app","event_type":"im.message.receive_v1"}}',
                         count=2)):
        await peer.send(frame)
    await peer.send(encode("cross", group[:group.index(b'"event":')], count=2))
    await wait_for(case / "before-drop")
    # 解压后单消息超限由真实 WebSocket 协议结束连接。
    await peer.send(bytes(MESSAGE + 1))


async def conversation(peer, client, generation):

    card = payload("card.action.trigger")
    group = payload("im.message.receive_v1")
    if fault == "wire" and generation == 1:
        await wire_limits(peer, group)
        return
    if fault in {"wire", "unwritable"}:
        # 在物理套接字上留下未读字节，检查卡应答立即拒绝。
        peer.transport.pause_reading()
        padding = bytes(1 << 20) if fault == "unwritable" else b"pressure"
        client.protocol.send_binary(padding)
        client.send_data()
        await peer.send(encode("pressure", card, kind="card"))
        await wait_for(case / "card-refused")
        if fault == "wire":
            peer.transport.resume_reading()
            assert await peer.recv() == padding
            await peer.send(encode("cross", group[group.index(b'"event":'):], count=2, seq=1))
    if fault == "none":
        for kind in ("card", "event"):
            await peer.send(encode("ordinary-" + kind, card, kind=kind))
            while True:
                response = ws.Frame()
                response.ParseFromString(await peer.recv())
                if response.method == ws.FrameType.DATA.value:
                    record("ordinary", [kind, json.loads(response.payload)])
                    break
    if flood or fault in {"consumer", "read_disk", "native_gil"}:
        from qianshu.feishu.sidecar import read_status
        while read_status(case / "approval")["state"] != "ready":
            await asyncio.sleep(0.01)
        await peer.send(encode("trigger", card))
    if not flood and fault not in {"none", "healthy", "lease", "wire", "unwritable"}:
        await wait_for(case / "hit")
        if fault == "read_disk":
            await asyncio.sleep(3.3)
    if fault in {"memory", "killed", "lease", "crash"}:
        await wait_for(case / "approval" / "supervisor.log")
    if fault == "crash":
        while len((case / "generations").read_text().splitlines()) < 3:
            await asyncio.sleep(0.02)
    if flood:
        # 先耗完未知额度，再投有平台头的群首片和第二波洪峰。
        fragment = card[:card.index(b'"event":')] if fault == "c2_partial" else b'{"event":'
        for i in range(18):
            await peer.send(encode(f"pending-{i}", fragment, count=2, kind="event" if i % 2 else "card"))
        split = 12 if fault == "c2_partial" else group.index(b'"event":')
        record("first")
        await peer.send(encode("group", group[:split], count=2))
        for i in range(120):
            await peer.send(encode(f"flood-{i}", card, kind="event" if i % 2 else "card"))
        await peer.send(encode("group", group[split:], count=2, seq=1))
    else:
        record("first")
        await peer.send(encode("group", group))
    (case / "stimulus").touch()
    await peer.send(encode("repeat", group))
    if fault == "unwritable":
        await asyncio.sleep(3.3)
        (case / "still-blocked").touch()
        return
    while True:
        frame = ws.Frame()
        frame.ParseFromString(await peer.recv())
        if frame.method == ws.FrameType.DATA.value:
            key = ws._get_by_key(frame.headers, ws.HEADER_MESSAGE_ID)
            record("ack", [key, json.loads(frame.payload)])


async def discover(self):
    self._reconnect_nonce = 0
    clock[0] = 1020 if (case / "takeover").exists() else 1010
    return "?device_id=synthetic&service_id=1"


class BufferedEndpoint(asyncio.Transport):
    """无网络回放端点。只用于无背压的生产开关对照。"""

    def __init__(self, protocol, probe):
        self.connection = protocol
        self.probe = probe
        self.closed = False
        self.limit = 65536

    def set_write_buffer_limits(self, high=None, low=None):
        self.limit = high or 65536

    def get_write_buffer_limits(self):
        return self.limit // 4, self.limit

    def get_write_buffer_size(self):
        return 0

    def get_extra_info(self, name, default=None):
        return self if name == "socket" else default

    def getsockopt(self, level, option):
        assert (level, option) == (socket.SOL_SOCKET, socket.SO_SNDBUF)
        return self.limit

    def is_closing(self):
        return self.closed

    def can_write_eof(self):
        return False

    def write(self, data):
        asyncio.get_running_loop().call_soon(self.peer.connection.data_received, data)

    def pause_reading(self):
        pass

    def resume_reading(self):
        pass

    def close(self):
        if not self.closed:
            self.closed = True
            self.probe.close()
            asyncio.get_running_loop().call_soon(self.connection.connection_lost, None)

    abort = close


async def dial(url, **options):
    record("dial")
    loop = asyncio.get_running_loop()
    endpoints = socket.socketpair()
    connections = []
    generation = len((case / "observed").read_text().split('"dial"')) - 1
    if generation > (2 if fault == "wire" else 1):
        for sock in endpoints:
            sock.close()
        raise ws.ClientException(1, "合成端点超过拨号预算")
    buffered = False
    try:
        endpoints[0].send(b"H")
        assert endpoints[1].recv(1) == b"H"
    except PermissionError:
        if fault != "none":
            for sock in endpoints:
                sock.close()
            raise ws.ClientException(1, "沙箱内 Unix 端点发送不可用") from None
        buffered = True
        record("buffered")
        if hasattr(sdk, "fcntl"):
            import termios
            from types import SimpleNamespace

            def outq(endpoint, operation, buf):
                assert isinstance(endpoint, BufferedEndpoint) and operation == termios.TIOCOUTQ
                return bytes(4)  # 内存端点始终一次接收整个包。

            sdk.fcntl = SimpleNamespace(ioctl=outq)
    for side, sock in zip((CLIENT, SERVER), endpoints):
        conn = Connection(Protocol(side, max_size=options.get("max_size", 1 << 20)),
                          write_limit=options.get("write_limit", 65536), close_timeout=0.05)
        if buffered:
            conn.connection_made(BufferedEndpoint(conn, sock))
        else:
            await loop.create_unix_connection(lambda: conn, sock=sock)
        connections.append(conn)
    if buffered:
        connections[0].transport.peer = connections[1].transport
        connections[1].transport.peer = connections[0].transport
    asyncio.create_task(conversation(connections[1], connections[0], generation))
    return connections[0]


def history(self, start, end, stop):
    while not (case / "stimulus").exists():
        if stop.wait(0.01):
            return
    record("history", [start, end])
    yield Message("backfill", "group", "ou_owner", 1005000, "text", "{}", ("bot",))


def install():
    assert Path(handoff.__file__).resolve().is_relative_to(Path(os.environ["REPLAY_SOURCE"]))
    record("installed")
    handoff._MACHINE_LEASE = case.parent / "machine" / "listener.lock"
    ws.websockets.connect = dial
    sdk._Connection._discover_endpoint = discover
    sdk._Transport.handoff_history = history
    sdk._Transport.react = lambda self, mid, **kw: record("react", mid)
    original = handoff.MentionLog.__init__

    def initialize(self, *args, **kwargs):
        return original(self, *args, clock=lambda: clock[0], **kwargs)

    handoff.MentionLog.__init__ = initialize
    locked = fcntl.flock
    opened = sqlite3.connect

    def flock(fd, flags):
        result = locked(fd, flags)
        if os.readlink(f"/proc/self/fd/{fd}") == str(db):
            record("lock")
        return result

    def connect(path, *args, **kwargs):
        connection = opened(path, *args, **kwargs)
        if Path(path) == db:
            record("open")
        return connection

    fcntl.flock, sqlite3.connect = flock, connect
    if fault not in {"none", "wire", "unwritable"}:
        from qianshu.feishu.sidecar import Sidecar
        sidecar = Sidecar(case / "approval", fault_hook=Path(__file__).with_name("listener_fault.py"))
        handoff.HandoffService._sidecar = sidecar
        if flood:
            # 消费者在首次取得数据包时卡住，后续投递填满真实内核队列。
            (case / "fault").write_text("consumer")
    if flood or fault in {"wire", "unwritable"}:
        original_frame = sdk._Connection._handle_message

        async def observe(self, *args):
            await original_frame(self, *args)
            if fault in {"wire", "unwritable"}:
                record("wire", [self._generation, self._budget.decoded, self._budget.rejected,
                                self._budget.peak_bytes, self.card_drops])
                if "cross" in self._budget.groups and self._generation == 1:
                    (case / "before-drop").touch()
                if self.card_drops:
                    (case / "card-refused").touch()
            else:
                record("budget", [self._budget.rejected, self._budget.peak_groups,
                                  self._budget.peak_bytes, self.card_drops, sidecar.rejected])

        sdk._Connection._handle_message = observe
        if fault == "unwritable":
            queued = sdk._Connection._queued_ack

            async def measured(self, *args):
                await queued(self, *args)
                record("expired", self.ack_drops)

            sdk._Connection._queued_ack = measured
    if (case / "switches").exists():
        from qianshu.mail import imap

        @contextlib.contextmanager
        def mailbox(config):
            record("mail")
            class Mailbox:
                def select(self, folder, readonly=True):
                    assert readonly
                    return "OK", [b"0"]
                def response(self, name):
                    return name, [b"1"]
            from types import SimpleNamespace
            yield SimpleNamespace(_connection=Mailbox())

        imap.connect = mailbox

    def network_guard(event, args):
        if event == "socket.connect" and args[0].family in (socket.AF_INET, socket.AF_INET6):
            record("external")
            raise AssertionError("unexpected network")

    sys.addaudithook(network_guard)
