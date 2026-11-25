"""正常 CLI 回放。只为审批解释器替换外部依赖和指定的故障点。"""

import asyncio
import base64
import json
import sys
import time
from pathlib import Path

import listener_replay as replay


def install():
    def no_dns(event, args):
        if event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"):
            replay.record("external")
            raise AssertionError("离线父进程禁止地址解析")
    sys.addaudithook(no_dns)
    from qianshu.feishu import sidecar
    popen = sidecar.subprocess.Popen

    def launch(command, *args, **kwargs):
        if isinstance(command, list) and "--service" in command:
            idx = command.index("-B") + 1
            command = list(command)
            command[idx] = str(Path(__file__).with_name("approval_replay_child.py"))
            # 子进程的标准错误留在合成目录，失败时可直接读到栈。
            with (replay.case / "child.stderr").open("ab") as log:
                return popen(command, *args, **dict(kwargs, stderr=log))
        return popen(command, *args, **kwargs)

    sidecar.subprocess.Popen = launch
    if (Path(replay.sdk.__file__).parent / "card_reply.py").is_file():
        from qianshu.feishu import card_reply
        from types import SimpleNamespace
        real_ioctl = card_reply.fcntl.ioctl

        def outq(sock, operation, buffer):
            if isinstance(sock, replay.BufferedEndpoint) and operation == card_reply.termios.TIOCOUTQ:
                return bytes(4)
            return real_ioctl(sock, operation, buffer)

        card_reply.fcntl = SimpleNamespace(ioctl=outq)
    replay.conversation = conversation
    if (replay.case / "scenario").read_text() == "reconnect":
        # 既有离线拨号夹具的双连接预算。此处已经完成普通生产入口的装配。
        replay.fault = "wire"


async def conversation(peer, client, generation):
    case = replay.case
    message = json.loads(replay.payload("im.message.receive_v1"))
    message["header"]["tenant_key"] = "tenant"
    original = json.dumps(message).encode()
    async def receive():
        while True:
            frame = replay.ws.Frame()
            frame.ParseFromString(await peer.recv())
            if frame.method == replay.ws.FrameType.DATA.value:
                key = replay.ws._get_by_key(frame.headers, replay.ws.HEADER_MESSAGE_ID)
                data = json.loads(frame.payload)
                if "data" in data:
                    toast = json.loads(base64.b64decode(data["data"]))["toast"]["content"]
                    replay.record("toast", [key, toast])
                replay.record("ack", [key, data])
    task = asyncio.create_task(receive())
    try:
        seq = 0 if generation == 1 else 2
        while True:
            command = case / f"input-{seq}"
            await replay.wait_for(command)
            value = json.loads(command.read_text())
            if value == "disconnect":
                await peer.close()
                return
            if value == "flood":
                flood = json.loads(replay.payload("card.action.trigger"))
                flood["header"]["tenant_key"] = "tenant"
                for i in range(128):
                    await peer.send(replay.encode(f"flood-{i}", json.dumps(flood).encode(),
                                                  kind="card" if i % 2 else "event"))
            elif value == "group":
                replay.record("first")
                await peer.send(replay.encode("group", original))
                await peer.send(replay.encode("repeat", original))
                (case / "stimulus").touch()
            else:
                if isinstance(value, dict):
                    reg, action = value["reg"], value["action"]
                    event_id, stamp = value["event_id"], value["stamp"]
                else:
                    reg, action = value
                    event_id, stamp = str(seq), int(time.time() * 1000)
                fault = (case / "scenario").read_text()
                actor = "ou_other" if fault == "outside_list" else "ou_actor"
                if fault == "expired_click":
                    stamp -= 121000
                elif fault == "clock_overlap":
                    stamp += 5000
                data = dict(schema="2.0", header=dict(app_id="app", tenant_key="tenant", event_id=event_id,
                            event_type="card.action.trigger", create_time=str(stamp)),
                            event=dict(operator={"open_id": actor},
                                       context=dict(open_chat_id="group", open_message_id=f"msg{reg}"),
                                       action=dict(value=dict(format=1, registration=reg, version=1,
                                                              card=f"card{reg}", action=action))))
                payload = json.dumps(data).encode()
                kind = "card" if seq % 2 else "event"
                if isinstance(value, dict) and value.get("fragment_kind"):
                    kind = value["fragment_kind"]
                    mid = len(payload) // 2
                    for part, chunk in enumerate((payload[:mid], payload[mid:])):
                        await peer.send(replay.encode(str(seq), chunk, kind=kind, count=2, seq=part))
                else:
                    await peer.send(replay.encode(str(seq), payload, kind=kind))
            seq += 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
