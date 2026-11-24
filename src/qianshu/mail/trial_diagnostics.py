"""收信服务的有界后台写者。故障文件与常规日志使用独立线程。"""

import fcntl
import logging
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from queue import Full, Queue
from threading import Event, Thread, current_thread

from qianshu.feishu.handoff import _file


CLOSE_FAILED = "收信登记库关闭失败，保留脏标记，下次收信先对账。"
CLOSE_TIMEOUT = "收信登记库关闭超时，保留脏标记，下次收信先对账。"


class Writer:
    def __init__(self, write, name):
        self.write = write
        self.pending = Queue(maxsize=64)
        self.stopping = Event()
        self.dropped = 0
        self.thread = Thread(target=self._run, name=name, daemon=True)
        self.thread.start()

    def post(self, value):
        try:
            self.pending.put_nowait(value)
        except Full:
            self.dropped += 1

    def _run(self):
        while not self.stopping.is_set() or not self.pending.empty():
            value = self.pending.get()
            if value is not None:
                try:
                    self.write(value)
                except Exception:
                    self.dropped += 1

    def finish(self):
        self.stopping.set()
        # 唤醒空队列；队列已满时写者本就有工作。
        try:
            self.pending.put_nowait(None)
        except Full:
            pass
        self.thread.join(0.1)


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
def listener_logs(service_logger):
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

    def forward(record):
        if current_thread() is writer.thread:
            return True
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
