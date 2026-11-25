"""认证连接上的有界重组。平台事件头与用户可控的 event 内容分开读取。"""

import json
from dataclasses import dataclass, field

MESSAGE = 1 << 20  # WebSocket 解压后的单消息上限
PART = 256 << 10
GROUP_BYTES = MESSAGE
GLOBAL_BYTES = 4 * GROUP_BYTES
MAX_GROUPS = 16
RESERVED_GROUPS = 2
MAX_PARTS = 64
HEADER_BYTES = 4096


def _varint(data, pos):
    value = 0
    for shift in range(0, 70, 7):
        byte = data[pos]
        pos += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, pos
    raise ValueError


def bounded_wire(data):
    """在 SDK 分配重复 protobuf Header 对象之前限制线上的字段数量与长度。"""
    if not isinstance(data, bytes) or len(data) > MESSAGE:
        return False
    pos = fields = headers = header_bytes = 0
    try:
        while pos < len(data):
            fields += 1
            if fields > 32:
                return False
            tag, pos = _varint(data, pos)
            if tag & 7 == 0:
                _, pos = _varint(data, pos)
            elif tag & 7 == 2:
                size, pos = _varint(data, pos)
                if tag >> 3 == 5:
                    headers += 1
                    header_bytes += size
                if headers > 16 or header_bytes > HEADER_BYTES or size > PART:
                    return False
                pos += size
            else:
                return False
        return pos == len(data) and fields > 0
    except (ValueError, IndexError):
        return False


def current_app(header, settings):
    return (isinstance(header, dict) and header.get("app_id") == settings.app_id
            and (not settings.tenant_key or header.get("tenant_key") == settings.tenant_key))


def platform_event_prefix(payload, settings):
    # 仅接受顶层 schema / header 前缀；不在正文或字符串里搜索类型。
    # 如果首片在平台头内部截断，只可占未知额度。
    decoder = json.JSONDecoder()
    text = payload[:HEADER_BYTES].decode("utf-8", errors="surrogateescape").lstrip()
    if not text.startswith("{"):
        return None
    text = text[1:].lstrip()
    try:
        for _ in range(2):
            key, end = decoder.raw_decode(text)
            text = text[end:].lstrip()
            if not text.startswith(":") or key not in ("schema", "header"):
                return None
            value_text = text[1:].lstrip()
            value, end = decoder.raw_decode(value_text)
            if key == "header":
                return value.get("event_type") if current_app(value, settings) else None
            text = value_text[end:].lstrip()
            if value != "2.0" or not text.startswith(","):
                return None
            text = text[1:].lstrip()
    except (ValueError, RecursionError):
        pass
    return None


@dataclass
class Group:
    kind: str
    count: int
    start: float
    deadline: float
    lane: str
    parts: dict = field(default_factory=dict)
    size: int = 0


class FrameBudget:
    def __init__(self):
        self.groups = {}
        self.rejected = self.decoded = 0
        self.peak_bytes = self.peak_groups = 0

    def expire(self, now):
        for key in [key for key, group in self.groups.items() if group.deadline <= now]:
            del self.groups[key]

    def pending_messages(self):
        return any(group.lane != "card" for group in self.groups.values())

    def reject(self):
        self.rejected += 1

    def accept(self, frame, settings, start, deadline):
        headers = {}
        total = 0
        if len(frame.headers) > 16 or len(frame.payload) > PART:
            return self.reject()
        for h in frame.headers:
            total += len(h.key.encode()) + len(h.value.encode())
            if total > HEADER_BYTES or h.key in headers:
                return self.reject()
            headers[h.key] = h.value
        kind = headers.get("type")
        key = headers.get("message_id")
        count = headers.get("sum", "")
        seq = headers.get("seq", "")
        if (kind not in ("event", "card") or not key or len(key) > 256
                or not (count.isascii() and count.isdecimal() and len(count) <= 2
                        and seq.isascii() and seq.isdecimal() and len(seq) <= 2)):
            return self.reject()
        count, seq = int(count), int(seq)
        if not 0 <= seq < count <= MAX_PARTS:
            return self.reject()
        self.expire(start)
        group = self.groups.get(key)
        if count == 1 and group is None:
            return kind, frame.payload, start, deadline
        size = len(frame.payload)
        occupied = sum(g.size for g in self.groups.values())
        if size + occupied > GLOBAL_BYTES:
            return self.reject()
        reservations = sum(g.lane == "message" for g in self.groups.values())
        unknown_bytes = sum(g.size for g in self.groups.values() if g.lane != "message")
        if group is None:
            if len(self.groups) >= MAX_GROUPS:
                return self.reject()
            event = platform_event_prefix(frame.payload, settings) if seq == 0 and kind == "event" else None
            lane = "unknown"
            if kind == "card" or event == "card.action.trigger":
                lane = "card"
            elif event == "im.message.receive_v1":
                lane = "message"
            if lane == "message" and (reservations + 1) * GROUP_BYTES + unknown_bytes > GLOBAL_BYTES:
                return self.reject()
            if lane != "message" and len(self.groups) - reservations >= MAX_GROUPS - RESERVED_GROUPS:
                return self.reject()
            # 能确认为卡的片也不能占尽未知额度；平台头截断的群首片
            # 在纯已识别卡洪峰后仍有一个完整组的位置。
            cards = sum(g.lane == "card" for g in self.groups.values())
            if lane == "card" and cards >= MAX_GROUPS - RESERVED_GROUPS - 1:
                return self.reject()
            group = Group(kind, count, start, deadline, lane)
        if count != group.count or kind != group.kind or seq in group.parts:
            return self.reject()
        unknown_limit = GLOBAL_BYTES - max(RESERVED_GROUPS, reservations) * GROUP_BYTES
        if group.lane == "card":
            card_bytes = sum(g.size for g in self.groups.values() if g.lane == "card")
            if size + card_bytes > unknown_limit - GROUP_BYTES:
                return self.reject()
        if size + group.size > GROUP_BYTES or (group.lane != "message" and size + unknown_bytes > unknown_limit):
            return self.reject()
        self.groups[key] = group
        group.parts[seq] = frame.payload
        group.size += size
        self.peak_bytes = max(self.peak_bytes, occupied + size)
        self.peak_groups = max(self.peak_groups, len(self.groups))
        if len(group.parts) == count:
            del self.groups[key]
            return kind, b"".join(group.parts[i] for i in range(count)), group.start, group.deadline
