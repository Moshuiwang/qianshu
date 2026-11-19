"""飞书群入口、结果汇报与审批卡片。

编排入口为 FeishuService(load_settings(), window, state_path, process).run()。
state_path 指向工作区外的私有 SQLite 文件，process 接收 Application 并返回 Report。
离线编排可用 Entrance 和进程内替身 Transport，调用 inject_message 注入。
审批卡片决定保存在同一状态库的 feishu_approvals 表。#11 负责在提交资格认领时
于同一事务内校验绑定、有效期、审批状态和当次授权，并完成其余台账门禁。
"""

from .cards import Report, mention
from .entrance import Application, Entrance, Message, fingerprint
from .sdk import FeishuService
from .settings import Settings, Window, load_settings
from .state import State

__all__ = [
    "Application", "Entrance", "FeishuService", "Message", "Report", "Settings", "State",
    "Window", "fingerprint", "load_settings", "mention",
]
