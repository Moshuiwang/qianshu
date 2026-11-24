"""常驻转交启动后才装配的试运行审批。"""

import logging
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import replace

from .sdk import _approval_warning, lark

_LOG = logging.getLogger(__name__)
_RETRY = 1


class ApprovalLifecycle:
    def __init__(self, args, path, load_settings):
        self._args = args
        self._path = path
        self._load_settings = load_settings
        self._worker = None
        self._stop = None
        self._dispatcher = None
        self._retry_at = 0
        self._closed = False

    @contextmanager
    def _boundary(self):
        try:
            yield
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            self._dispatcher = None
            _LOG.critical("E-TRIAL-DISABLED")

    def try_enable(self, client):
        with self._boundary():
            if self._closed or (self._worker is not None and self._worker.is_alive()):
                return
            if time.monotonic() < self._retry_at:
                return
            self._retry_at = time.monotonic() + _RETRY
            self._stop = threading.Event()
            self._worker = threading.Thread(target=self._supervise, args=(client, self._stop),
                                            name="trial-approvals")
            self._worker.start()

    def disable(self):
        self._closed = True
        self._dispatcher = None
        with self._boundary():
            if self._stop is not None:
                self._stop.set()
        with self._boundary():
            if self._worker is not None and self._worker.ident is not None:
                self._worker.join()

    async def handle(self, payload):
        with self._boundary():
            dispatcher = self._dispatcher
            if dispatcher is not None:
                return await dispatcher._do_without_validation(payload)
        return _approval_warning("审批暂不可用")

    def _switches(self):
        from qianshu.config import trial_config

        return trial_config(self._args.trial_switches) if self._args.trial_switches else {}

    def _supervise(self, client, stop):
        with self._boundary():
            while not self._closed and not stop.is_set():
                with self._boundary():
                    self._session(client, stop)
                stop.wait(_RETRY)

    def _session(self, client, stop):
        from qianshu.mail import trial

        config = self._switches()
        receive = config.get("MAIL_TRIAL_RECEIVE_ENABLED") == "true"
        cards = config.get("MAIL_TRIAL_CARD_ENABLED") == "true"
        state = self._path.with_name("trial-050.sqlite3")
        if not receive and not trial.has_approval_history(state):
            return

        settings, path = self._load_settings()
        base = client.service.settings
        if path != self._path or replace(settings, trial_credentials=(),
                                         trial_credentials_complete=base.trial_credentials_complete) != base:
            raise ValueError("转交配置已变化")
        builder = lark.EventDispatcherHandler.builder("", "")
        listener = None
        scheduled = None
        published = False

        def available():
            return (cards and self._dispatcher is dispatcher and not self._closed and not stop.is_set()
                    and listener.origin is client.service._origin and listener.origin is not None)

        async def record(data):
            if not available() or not client._from_current_app(data):
                return _approval_warning("审批暂不可用")
            origin, _, deadline = client._dispatch.get()
            return await listener.record(data, origin, deadline)

        dispatcher = builder.register_p2_card_action_trigger(record).build()
        with ExitStack() as resources:
            try:
                if cards or not receive or trial.has_approval_history(state):
                    listener = trial.Listener(state, settings, self._switches, defer_rotation=True)
                    listener.current_connection = lambda origin: available() and origin is client.service._origin
                if receive:
                    resources.enter_context(trial.mail_worker(self._args, settings, self._path, self._switches,
                                                              cards=cards, available=available,
                                                              sender=listener.transport if listener else None))
                while not self._closed and not stop.is_set() and self._switches() == config:
                    if listener is not None:
                        origin = client.service._origin
                        if cards and origin is not scheduled:
                            listener.request_rotation(origin, "连接代次变更" if origin else "审批连接断开")
                            scheduled = origin
                        listener.maintain()
                    if cards and not published:
                        self._dispatcher = dispatcher
                        published = True
                    if stop.wait(_RETRY) or (cards and self._dispatcher is not dispatcher):
                        break
            finally:
                self._dispatcher = None
                if cards and listener is not None:
                    listener.rotate(None, "服务关闭")
