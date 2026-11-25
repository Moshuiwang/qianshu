"""转交与合成消费者之间的定长包头。"""

from struct import Struct

PACKET_LIMIT = 8192
ENVELOPE = Struct("!QQdd")  # 原连接、消费代次、首片单调时刻、截止。
SNAPSHOT = Struct("!dQQ")  # 采样单调时刻、已读、已办结。
SERVICE_STATUS = Struct("!BQQ")  # 建单开关、人工核对数、待核通知数。
# 段 3（G516 乙）：当前有效测试卡（有无、收信登记号、版本、授权登记号 UTF-8 定长补零）。只在有测试卡时追加；
# 监督进程只用它选应答文案，旧帧、缺字段或字段不一致一律按非测试卡处理。
TEST_CARD = Struct("!BQQ64s")
