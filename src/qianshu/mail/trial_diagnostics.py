"""收信服务的有界后台写者。故障文件与常规日志使用独立线程。"""

import fcntl
import logging
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread, current_thread

from qianshu.feishu.handoff import _file


CLOSE_FAILED = "收信登记库关闭失败，保留脏标记，下次收信先对账。"
CLOSE_TIMEOUT = "收信登记库关闭超时，保留脏标记，下次收信先对账。"


class Writer:
    def __init__(self, write, name):
        self.write = write
        self.pending = Queue(maxsize=64)
        self.stopping = Event()
        self.lock = Lock()
        self.dropped = 0
        self.thread = Thread(target=self._run, name=name, daemon=True)
        self.thread.start()

    def post(self, value):
        with self.lock:
            if not self.stopping.is_set():
                try:
                    self.pending.put_nowait(value)
                    return
                except Full:
                    pass
            self.dropped += 1

    def _run(self):
        while not self.stopping.is_set() or not self.pending.empty():
            value = self.pending.get()
            if value is not None:
                try:
                    self.write(value)
                except Exception:
                    with self.lock:
                        self.dropped += 1

    def finish(self):
        with self.lock:
            self.stopping.set()
        # 唤醒空队列；队列已满时写者本就有工作。
        try:
            self.pending.put_nowait(None)
        except Full:
            pass
        self.thread.join(0.1)
        if self.thread.is_alive():
            with self.lock:
                while True:
                    try:
                        value = self.pending.get_nowait()
                    except Empty:
                        break
                    if value is not None:
                        self.dropped += 1
                # 写者可能刚检查完队列，正进入 get()。
                self.pending.put_nowait(None)


def fault_writer(path):
    def append(message):
        line = f"{datetime.now(UTC).isoformat()} CRITICAL {message}\n".encode()
        fd = _file(path.with_suffix(".fault"), create=True)
        try:
            fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) | os.O_APPEND)
            os.write(fd, line)
        finally:
            os.close(fd)

    return Writer(append, "trial-fault-writer")


@contextmanager
def listener_logs(service_logger, *, socket_logs=False):
    # 监听和收信的业务线程可能正持锁，根处理器只由后台线程调用。
    def emit(record):
        if isinstance(record, logging.LogRecord):
            logging.getLogger(record.name).callHandlers(record)
        else:
            # 将固定的故障文本同时送到原有 CRITICAL 日志目的地。
            service_logger.critical(record)

    writer = Writer(emit, "trial-log-writer")
    loggers = [logging.getLogger(name) for name in
               ("qianshu.feishu.handoff", "qianshu.feishu.sdk", "qianshu.mail.trial")]

    if socket_logs:
        loggers.extend(logging.getLogger(name) for name in ("websockets.client", "asyncio"))

    def forward(record):
        if current_thread() is writer.thread:
            return True
        if socket_logs and record.name in {"websockets.client", "asyncio"}:
            # 套接字帧与循环异常可能带整帧、凭据或任务 repr，不转交原记录。
            record = logging.LogRecord(record.name, record.levelno, "", 0, "feishu_connection_diagnostic",
                                       (), None)
        writer.post(record)
        return False

    for logger in loggers:
        logger.addFilter(forward)
    try:
        yield writer
    finally:
        for logger in loggers:
            logger.removeFilter(forward)
        writer.finish()
