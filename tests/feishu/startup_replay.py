import asyncio
import base64
import json
import logging
from uuid import uuid4
from unittest.mock import AsyncMock, Mock

from qianshu.feishu import handoff, sdk
from tests.feishu.test_handoff import FakeFeishu
from tests.feishu.test_sdk import FakeSocket, encoded_frame


def observe(patch, completed=None):
    """运行真实转交循环，直到业务完成或收到合成卡片的应答。"""
    # SDK 缓存从线程的默认循环取任务，监听器使用 SDK 模块循环。两者须在 CLI 内一致。
    run = handoff.HandoffService.run

    def with_sdk_loop(service):
        try:
            previous = asyncio.get_event_loop()
        except RuntimeError:
            previous = None
        asyncio.set_event_loop(sdk.ws.loop)
        try:
            return run(service)
        finally:
            asyncio.set_event_loop(previous)

    patch.setattr(handoff.HandoffService, "run", with_sdk_loop)
    fake = FakeFeishu()
    fake._require_window = lambda: None
    patch.setattr(handoff, "_Transport", lambda *args: fake)
    listen = Mock(wraps=handoff.MentionLog.listening)
    patch.setattr(handoff.MentionLog, "listening", listen)
    patch.setattr(sdk._Connection, "_discover_endpoint", AsyncMock(
        return_value="?device_id=d&service_id=1"))
    socket = FakeSocket()
    # 只替换底层物理写端，保留原卡片编码和分流。
    from qianshu.feishu import trial_connection

    def write(service, origin, encoded):
        frame = sdk.ws.Frame()
        frame.ParseFromString(encoded)
        response = json.loads(frame.payload)
        socket.callback_responses.append(json.loads(base64.b64decode(response["data"])))
        return True

    patch.setattr(trial_connection, "try_card_write", write)
    patch.setattr(handoff, "_MACHINE_LEASE", handoff._MACHINE_LEASE.parent / str(uuid4()) / "listener.lock")
    dial = AsyncMock(return_value=socket)
    patch.setattr(sdk.ws.websockets, "connect", dial)
    serve = handoff.HandoffService._serve

    async def bounded(service, client):
        running = asyncio.create_task(serve(service, client))
        try:
            async with asyncio.timeout(12):
                while client.receiver is None:
                    await asyncio.sleep(.05)
                if completed is None:
                    diagnostic = handoff._MACHINE_LEASE.parent / "supervisor.log"
                    while not diagnostic.exists() or not diagnostic.read_text().splitlines():
                        await asyncio.sleep(.05)
                    severity, code, count = diagnostic.read_text().splitlines()[-1].split()
                    assert severity == "CRITICAL" and code == "E-TRIAL-DISABLED" and int(count) >= 1
                    logging.getLogger("qianshu").critical(code)
                    card = encoded_frame(dict(schema="2.0", header=dict(app_id="app", event_id="card",
                        event_type="card.action.trigger"), event={}), sdk.ws.MessageType.CARD)
                    await client._handle_message(card, client._frame_timing(socket))
                else:
                    while not completed.is_set():
                        await asyncio.sleep(.05)
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    patch.setattr(handoff.HandoffService, "_serve", bounded)
    return listen, dial, socket
