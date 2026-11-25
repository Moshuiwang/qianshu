"""仅在测试装配审批子进程时使用的有界收帧与回包。"""

import asyncio
import base64
import json
import time
from http import HTTPStatus

from .card_reply import try_card_write
from .frame_budget import MESSAGE, FrameBudget, bounded_wire, current_app
from .sdk import _CALLBACK_BUDGET, _Connection, _approval_warning, _before_send, ws


class InjectedConnection(_Connection):
    _injected_options = dict(max_size=MESSAGE, max_queue=8, write_limit=65536)

    def __init__(self, service):
        super().__init__(service, approvals=False)
        self._budget = FrameBudget()
        self.card_drops = self.ack_drops = self.pending_acks = 0

    async def _disconnect(self):
        self._budget.groups.clear()
        await super()._disconnect()

    async def _handle_message(self, msg, dispatch=None):
        if dispatch is None:
            dispatch = self._frame_timing(self._conn)
        origin, _, _ = dispatch
        if origin is None or origin is not self._conn:
            return
        if not bounded_wire(msg):
            self._budget.reject()
            return
        task = asyncio.current_task()
        self.frames.add(task)
        token = self._dispatch.set(dispatch)
        try:
            frame = ws.Frame()
            frame.ParseFromString(msg)
            if not frame.IsInitialized():
                self._budget.reject()
            elif frame.method == ws.FrameType.DATA.value:
                await self._handle_data_frame(frame)
            elif frame.method == ws.FrameType.CONTROL.value:
                await self._handle_control_frame(frame)
            else:
                self._budget.reject()
        except Exception:
            self._budget.reject()
        finally:
            self._dispatch.reset(token)
            self.frames.discard(task)

    async def _handle_data_frame(self, frame):
        origin, started, deadline = self._dispatch.get()
        if origin is not self._conn:
            return
        headers = frame.headers
        completed = self._budget.accept(frame, self.service.settings, started, deadline)
        if completed is None:
            return
        frame_type, payload, started, deadline = completed

        _before_send(self.service, "callback_headers", [(h.key, h.value) for h in headers])
        response = ws.Response(code=HTTPStatus.OK)
        timing = self._dispatch.set((origin, started, deadline))
        try:
            self._budget.decoded += 1
            envelope = json.loads(payload)
            header = envelope.get("header") if isinstance(envelope, dict) else None
            event_type = header.get("event_type") if isinstance(header, dict) else None
            message_event = event_type == "im.message.receive_v1"
            bound = origin is self.service._origin and current_app(header, self.service.settings)
            if frame_type == "card" or not (bound and message_event):
                sidecar = self.service._sidecar
                if bound and event_type == "card.action.trigger":
                    sidecar.offer(payload, started, deadline)
                self._card_reply(origin, frame, started, ignored_message=message_event)
                return
            result = self._event_handler._do_without_validation(payload)
            if result is not None:
                encoded = ws.JSON.marshal(await result).encode()
                _before_send(self.service, "callback_content", encoded)
                response.data = base64.b64encode(encoded)
        except Exception:
            self._budget.reject()
            self._card_reply(origin, frame, started)
            return
        finally:
            self._dispatch.reset(timing)
        header = headers.add()
        header.key = ws.HEADER_BIZ_RT
        header.value = str(round((time.monotonic() - started) * 1000))
        frame.payload = ws.JSON.marshal(response).encode()
        remaining = started + _CALLBACK_BUDGET - time.monotonic()
        if remaining <= 0:
            self.ack_drops += 1
            return
        encoded = frame.SerializeToString()
        if self.pending_acks < 8 and len(encoded) <= 8192:
            self.pending_acks += 1
            task = asyncio.create_task(self._queued_ack(origin, encoded, started + _CALLBACK_BUDGET))
            self.frames.add(task)
            task.add_done_callback(self.frames.discard)
        else:
            self.ack_drops += 1

    async def _send_ack(self, origin, encoded, remaining):
        async with asyncio.timeout(remaining):
            async with self._lock:
                if origin is self._conn and origin.close_code is None:
                    _before_send(self.service, "callback_response", encoded)
                    await origin.send(encoded)

    async def _queued_ack(self, origin, encoded, deadline):
        try:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                await self._send_ack(origin, encoded, remaining)
            else:
                self.ack_drops += 1
        except Exception:
            self.ack_drops += 1
        finally:
            self.pending_acks -= 1

    def _drop_card(self):
        self.card_drops += 1
        self.service._sidecar.card_drops = self.card_drops

    def _card_reply(self, origin, frame, started, *, ignored_message=False):
        # 转交 CLI 从未注册卡片处理器，保留 main 的 CARD 警告和 EVENT 500。
        now = time.monotonic()
        transport = getattr(origin, "transport", None)
        if (transport is None or origin is not self._conn or origin.close_code is not None
                or self._lock.locked() or self.pending_acks
                or now >= started + _CALLBACK_BUDGET or self._budget.pending_messages()):
            self._drop_card()
            return
        kind = ws._get_by_key(frame.headers, ws.HEADER_TYPE)
        response = ws.Response(code=HTTPStatus.OK if ignored_message else HTTPStatus.INTERNAL_SERVER_ERROR)
        if kind == "card":
            warning = _approval_warning("卡片回调格式无效")
            response = ws.Response(code=HTTPStatus.OK)
            response.data = base64.b64encode(ws.JSON.marshal(warning).encode())
        header = frame.headers.add()
        header.key, header.value = ws.HEADER_BIZ_RT, str(round((now - started) * 1000))
        frame.payload = ws.JSON.marshal(response).encode()
        encoded = frame.SerializeToString()
        if not try_card_write(self.service, origin, encoded):
            self._drop_card()
