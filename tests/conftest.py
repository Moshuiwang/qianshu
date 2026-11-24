"""测试进程的自保护（#174）：外层被停掉时 pytest 与它 fork 的子进程都必须跟着退出，子进程内存有上限。"""
import ctypes
import multiprocessing
import os
import resource
import signal
import sys

import pytest

_PR_SET_PDEATHSIG = 1
# 子进程在 fork 时的地址空间之上最多再多用这么多；正常用例子进程常驻约 190MB，增量远小于此。
CHILD_EXTRA_ADDRESS_SPACE = 1 << 30


def exit_with_parent(sig: int) -> None:
    """父进程退出时本进程收到 sig；设置前父进程已退出则立即退出。仅 Linux 生效。"""
    if not sys.platform.startswith("linux"):
        return
    parent = os.getppid()
    if ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_PDEATHSIG, sig, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) 失败")
    if os.getppid() != parent:
        os._exit(1)


def _address_space() -> int:
    with open("/proc/self/status") as status:
        for line in status:
            if line.startswith("VmSize:"):
                return int(line.split()[1]) * 1024
    raise RuntimeError("读不到 VmSize")


def _bounded_child(target, args, kwargs):
    exit_with_parent(signal.SIGKILL)
    if sys.platform.startswith("linux"):
        limit = _address_space() + CHILD_EXTRA_ADDRESS_SPACE
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    target(*args, **kwargs)


def pytest_configure(config):
    # 外层（编排脚本、Codex、终端）被停掉时 pytest 不得变成孤儿继续跑。
    # xdist 等工作进程同样适用；SIGTERM 不跑 Python 清理，子进程靠各自的设置退出。
    exit_with_parent(signal.SIGTERM)


@pytest.fixture
def fork_process():
    """以 fork 方式启动子进程：父进程消失即被 SIGKILL、地址空间有上限、用例结束必定收尾。

    用法：process = fork_process(target, *args)；返回已启动的 multiprocessing.Process。
    """
    context = multiprocessing.get_context("fork")
    started = []

    def start(target, *args, **kwargs):
        process = context.Process(target=_bounded_child, args=(target, args, kwargs), daemon=True)
        process.start()
        started.append(process)
        return process

    try:
        yield start
    finally:
        for process in started:
            if process.is_alive():
                process.terminate()
                process.join(5)
            if process.is_alive():
                process.kill()
            process.join()
