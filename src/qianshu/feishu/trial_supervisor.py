"""首连之后选择审批、只收信或历史恢复。试运行配置读取只占用监督线程。"""

import fcntl
import os
from threading import Event

from qianshu.config import trial_config
from qianshu.mail import _fingerprint

from . import handoff
from .sidecar import Sidecar
from .state import CALLBACK_RESPONSE_MARGIN


TEST_PENDING = "测试授权核对中，请以群公告为准"


class TrialSupervisor(Sidecar):
    def __init__(self, args, path, service, logs):
        self.args, self.path, self.service, self.logs = args, path, service, logs
        settings = service.settings
        target = _fingerprint(settings.app_id, settings.tenant_key or "", settings.chat_id, str(path.absolute()))
        paths = (args.env_file, args.business_env_file, args.mingdao_config, args.trial_switches)
        super().__init__(handoff._MACHINE_LEASE.parent,
                         service_args=(*(str(p.absolute()) if p else "" for p in paths), target))
        self.mode = service.initial_trial_mode
        self.probe = args.mode_probe
        self.receive_closed = Event()
        self.close_failed = False
        self.create_enabled = False

    def click(self, payload, started, deadline, data):
        if self.mode == "cards":
            event = data.get("event")
            action = event.get("action") if isinstance(event, dict) else None
            value = action.get("value") if isinstance(action, dict) else None
            if self.offer(payload, started, deadline - CALLBACK_RESPONSE_MARGIN):
                if not self.create_enabled and isinstance(value, dict) and value.get("action") == "approve":
                    # 段 3（G516 乙）：子进程状态帧报告的当前有效测试卡只用来选文案；不写建单结果，以群公告为准。
                    test = getattr(self, "test_card", None)
                    if (test and type(value.get("registration")) is int and type(value.get("version")) is int
                            and (value["registration"], value["version"]) == test[:2]):
                        return TEST_PENDING
                    return "建单未开启"
                return "已收到点击，请以卡片更新为准"
        return "审批暂不可用"

    def _generation(self, epoch):
        if self.probe is not None:
            if not self.probe.done.wait(2):
                raise ValueError("E-TRIAL-DISABLED")
            switches = self.probe.values
            self.probe = None
            if switches is None:
                self.mode = "unavailable"
                raise ValueError("E-TRIAL-DISABLED")
        else:
            switches = trial_config(self.args.trial_switches)
        if switches.get("MAIL_TRIAL_CARD_ENABLED") == "true":
            self.mode = "cards"
            # 不把名单解析、私有材料检查或业务依赖导入放在转交线程。
            return super()._generation(epoch)
        if switches.get("MAIL_TRIAL_RECEIVE_ENABLED") == "true":
            from qianshu.mail.trial import receive_worker
            self.mode = "receive"
            try:
                with receive_worker(self.args, self.service.settings, self.path, lambda: switches,
                                    self.service, self.logs) as fault:
                    while not self.stopping.wait(1):
                        if dict(trial_config(self.args.trial_switches)) != dict(switches):
                            break
                self.close_failed = fault.is_set()
            finally:
                self.receive_closed.set()
        else:
            self.mode = "off"
            self._recover_history()
            while not self.stopping.wait(1):
                if dict(trial_config(self.args.trial_switches)) != dict(switches):
                    break
        return True

    def _recover_history(self):
        from .trial_approval import has_approval_history
        if not has_approval_history(self.path.with_name("trial-050.sqlite3")):
            return
        fd = handoff._file(self.directory / "consumer.lock", create=True)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.manual_checks, self.pending_notices = recover_disabled(self.args, self.path)
        finally:
            os.close(fd)


def recover_disabled(args, handoff_path):
    """调用者持有审批租约。没有审批登记的旧收信库无需恢复。"""
    path = handoff_path.with_name("trial-050.sqlite3")
    from .trial_approval import has_approval_history, journal_at
    if not has_approval_history(path):
        return 0, 0
    from .trial_consumer import load_approval_settings
    from .trial_listener import Listener
    settings, target = load_approval_settings(args)
    if target != handoff_path:
        raise ValueError("E-TRIAL-DISABLED")
    Listener(path, settings, lambda: {}, defer_rotation=True).maintain()
    with journal_at(path) as j:
        return j.notice_counts()
