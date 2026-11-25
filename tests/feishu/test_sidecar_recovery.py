"""监督线程的故障停用与按退避自动恢复（进程内监督 + 轻量替身子进程，不载入业务依赖）。

真实生产子进程每类故障各一例见 test_approval_runtime.py；本文件逐个覆盖监督可观察到的
全部故障形态：启动前异常退出、被信号终止、启动挂起无快照、快照格式错、就绪后异常退出、
就绪后卡死不再出快照。
"""

import subprocess
import sys
import threading
import time

import pytest

from qianshu.feishu.sidecar import Sidecar

# 替身只用标准库，按包头定义发反向快照；收到 EOF 即退出。
CHILD = r"""
import os, select, signal, socket, struct, sys, time
channel = socket.socket(fileno=int(sys.argv[1]))
mode = sys.argv[2]
if mode == "exit":
    sys.exit(1)
if mode == "kill":
    os.kill(os.getpid(), signal.SIGKILL)
if mode == "hang":
    time.sleep(60)
def snapshot():
    if mode == "garbage":
        channel.send(b"x")
    else:
        channel.send(struct.pack("!dQQ", time.monotonic(), 0, 0) + struct.pack("!BQQ", 1, 0, 0))
snapshot()
if mode == "stall":
    time.sleep(60)
ends = time.monotonic() + (0.3 if mode == "late_exit" else 60)
while time.monotonic() < ends:
    if select.select([channel], [], [], 0.05)[0] and not channel.recv(16384):
        break
    snapshot()
sys.exit(1 if mode == "late_exit" else 0)
"""


class Recorder(threading.Event):
    """记录监督的退避间隔，实际只等很短，避免用例空等 1+2+4… 秒。"""

    def __init__(self):
        super().__init__()
        self.delays = []

    def wait(self, timeout=None):
        if timeout is not None and timeout >= 1:
            self.delays.append(timeout)
            timeout = 0.05
        return super().wait(timeout)


class Probe(Sidecar):
    startup_timeout = 0.5
    snapshot_timeout = 0.5

    def __init__(self, directory, modes):
        super().__init__(directory, service_args=("synthetic",))
        self.modes = list(modes)
        self.children = []
        self.stopping = Recorder()

    def _launch(self, endpoint):
        mode = self.modes.pop(0) if self.modes else "healthy"
        child = subprocess.Popen([sys.executable, "-I", "-S", "-c", CHILD, str(endpoint.fileno()), mode],
                                 pass_fds=(endpoint.fileno(),), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(child)
        return child


def wait_for(check, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    pytest.fail("监督未在时限内到达预期状态")


@pytest.fixture
def supervised(tmp_path):
    started = []

    def run(modes):
        directory = tmp_path / "machine"
        sidecar = Probe(directory, modes)
        sidecar.connect()
        # 转交长连在线：心跳保持新鲜，避免与连接断开的停用路径混淆。
        sidecar.beat = time.monotonic() + 3600
        started.append(sidecar)
        return sidecar

    yield run
    for sidecar in started:
        sidecar.stop()
        wait_for(lambda: not sidecar.alive, 5)
        for child in sidecar.children:
            child.wait(5)


FAULT_MODES = ["exit", "kill", "hang", "garbage", "late_exit", "stall"]


@pytest.mark.parametrize("mode", FAULT_MODES)
def test_each_fault_disables_then_recovers_after_one_second(supervised, mode):
    sidecar = supervised([mode])
    if mode == "hang":
        # 故障中：子进程在但无快照，不就绪，点击投递被拒（父进程只回固定提示）。
        wait_for(lambda: sidecar.alive, 5)
        now = time.monotonic()
        assert sidecar.status()["state"] != "ready"
        assert not sidecar.offer(b"{}", now, now + 2) and sidecar.rejected == 1
    wait_for(lambda: sidecar.failures >= 1, 5)
    wait_for(lambda: sidecar.status()["state"] == "ready", 5)
    assert sidecar.stopping.delays[:1] == [1]
    assert sidecar.failures == 1
    now = time.monotonic()
    assert sidecar.offer(b"{}", now, now + 2)
    assert sidecar.children[0].poll() is not None


def test_backoff_doubles_while_fault_persists_and_recovers_when_it_clears(supervised):
    sidecar = supervised(FAULT_MODES)
    wait_for(lambda: sidecar.status()["state"] == "ready" and sidecar.failures == len(FAULT_MODES), 20)
    assert sidecar.stopping.delays == [1, 2, 4, 8, 16, 32]
    assert len(sidecar.children) == len(FAULT_MODES) + 1
    assert all(child.poll() is not None for child in sidecar.children[:-1])


def test_backoff_is_capped(supervised):
    sidecar = supervised(["exit"] * 8)
    wait_for(lambda: sidecar.status()["state"] == "ready", 20)
    assert sidecar.stopping.delays == [1, 2, 4, 8, 16, 32, 60, 60]
