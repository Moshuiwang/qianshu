"""转交与合成消费者之间的定长包头。"""

from struct import Struct

PACKET_LIMIT = 8192
ENVELOPE = Struct("!QQdd")  # 原连接、消费代次、首片单调时刻、截止。
SNAPSHOT = Struct("!dQQ")  # 采样单调时刻、已读、已办结。
