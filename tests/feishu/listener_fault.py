"""在独立解释器内运行的合成故障屏障。"""

import asyncio
import ctypes
import json
import logging
import os
import resource
import signal
import sys
import threading
import time
from pathlib import Path

folder = Path(sys.argv[3]).parent
fault = (folder / "fault").read_text()
phase = "consume" if fault in {"consumer", "read_disk", "native_gil"} else "start"


def mark(name):
    with (folder / name).open("a") as stream:
        stream.write(f"{os.getpid()} {time.monotonic()}\n")


def barrier(point):
    if point == "start":
        mark("generations")
        files = []
        for fd in Path("/proc/self/fd").iterdir():
            try:
                files.append(os.readlink(fd))
            except FileNotFoundError:
                pass
        death = ctypes.c_int()
        assert ctypes.CDLL(None).prctl(2, ctypes.byref(death), 0, 0, 0) == 0
        facts = dict(death=death.value, cap=resource.getrlimit(resource.RLIMIT_AS)[0],
                     sockets=sum(x.startswith("socket:") for x in files),
                     inherited=any(x.endswith(("listener.lock", "handoff.db", "parent.log")) for x in files),
                     sdk="lark_oapi" in sys.modules)
        (folder / "bootstrap").write_text(json.dumps(facts))
    if fault.startswith("close_"):
        if point == "tick":
            raise EOFError
        if point == "close":
            mark("hit")
            if fault == "close_raise":
                raise OSError
            threading.Event().wait()
        return
    if point != phase or fault == "healthy":
        return
    mark("hit")
    if fault == "read_disk":
        with (folder / "disk").open("rb") as stream:
            stream.read(1)
    elif fault == "native_gil":
        ctypes.PyDLL(None).sleep(30)
    elif fault in {"startup", "consumer"}:
        threading.Event().wait()
    elif fault == "log":
        logger = logging.getLogger("approval")
        logger.handlers[0].emit = lambda record: threading.Event().wait()
        logger.critical("CRITICAL E-TRIAL-DISABLED 1")
    elif fault == "pool":
        async def blocked_exit():
            asyncio.get_running_loop().run_in_executor(None, threading.Event().wait)
        asyncio.run(blocked_exit())
    elif fault == "memory":
        bytearray(resource.getrlimit(resource.RLIMIT_AS)[0])
    elif fault in {"crash", "killed"}:
        os.kill(os.getpid(), signal.SIGKILL)
    elif fault == "import":
        __import__("_synthetic_missing_dependency")
    elif fault == "members":
        json.loads("[")
    elif fault in {"constructor", "stop_event", "dispatcher"}:
        raise RuntimeError(fault)
