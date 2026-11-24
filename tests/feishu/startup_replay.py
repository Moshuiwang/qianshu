import asyncio
from unittest.mock import AsyncMock, Mock

from qianshu.feishu import handoff, sdk
from tests.feishu.test_handoff import FakeFeishu
from tests.feishu.test_sdk import FakeSocket, encoded_frame


def observe(patch, completed=None):
    """运行真实转交循环，直到业务完成或收到合成卡片的应答。"""
    fake = FakeFeishu()
    fake._require_window = lambda: None
    patch.setattr(handoff, "_Transport", lambda *args: fake)
    listen = Mock(wraps=handoff.MentionLog.listening)
    patch.setattr(handoff.MentionLog, "listening", listen)
    patch.setattr(sdk._Connection, "_discover_endpoint", AsyncMock(
        return_value="wss://synthetic.invalid/?device_id=d&service_id=1"))
    socket = FakeSocket()
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
