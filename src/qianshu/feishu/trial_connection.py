"""试运行单连接。沿用群帧接收、重组与确认，只在重组后分流卡片。"""

import base64
import json
import time

from .card_reply import try_card_write
from .sdk import _Connection, _approval_warning, ws


class TrialConnection(_Connection):
    _trial_cards = True

    def __init__(self, service):
        super().__init__(service, approvals=False)
        self._card_pause_until = 0.0
        self._fragment = None
        self._fragment_until = self._other_fragments_until = 0.0

    def _reserve_fragment_reply(self, key, deadline, *, incomplete):
        # 未识别的 EVENT 分片可能承载群消息。只保留最后一个组和此前组的最迟期限。
        # 重组完成释放本组写端预留，交错的其他组保守等到其首片期限。
        if incomplete:
            if key != self._fragment:
                self._other_fragments_until = max(self._other_fragments_until, self._fragment_until)
                self._fragment = key
            self._fragment_until = deadline
        elif key == self._fragment:
            self._fragment = None
            self._fragment_until = 0.0

    def _trial_reply(self, frame, kind, payload, origin, started, deadline):
        # 群事件一直由原 SDK 分发。只对未注册的事件检查是否为卡片。
        supervisor = self.service._sidecar
        mode = supervisor.mode if supervisor else self.service.initial_trial_mode
        if mode in ("off", "receive"):
            return False
        try:
            data = json.loads(payload)
            header = data.get("header") if isinstance(data, dict) else None
        except (ValueError, RecursionError):
            header = None
        trigger = isinstance(header, dict) and header.get("event_type") == "card.action.trigger"
        if kind != "card" and not trigger:
            return False
        text = "审批暂不可用"
        settings = self.service.settings
        if (supervisor and trigger and origin is self.service._origin and header.get("app_id") == settings.app_id
                and (not settings.tenant_key or header.get("tenant_key") == settings.tenant_key)):
            text = supervisor.click(payload, started, deadline, data)
        now = time.monotonic()
        if (origin is self._conn and origin.close_code is None and not self._lock.locked()
                and max(self._card_pause_until, self._fragment_until, self._other_fragments_until) <= now < deadline):
            # 最高每 50 毫秒一个短提示。已有群回包占用写锁或线路积压时立即放弃。
            self._card_pause_until = now + 0.05
            response = ws.Response(code=200)
            response.data = base64.b64encode(ws.JSON.marshal(_approval_warning(text)).encode())
            frame.payload = ws.JSON.marshal(response).encode()
            elapsed = frame.headers.add()
            elapsed.key, elapsed.value = ws.HEADER_BIZ_RT, str(round((now - started) * 1000))
            if try_card_write(self.service, origin, frame.SerializeToString()):
                return True
        if supervisor:
            supervisor.card_drops += 1
        return True
