"""申请解析可用的平台适配器。配置只能选择已登记的适配器。"""

from .anthropic import streaming_model

ADAPTERS = {"anthropic": streaming_model}
