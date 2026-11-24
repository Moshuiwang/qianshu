"""转交侧监督。仅合成回放构造，生产无装配入口。"""

import _thread
import json
import os
import socket
import struct
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from tempfile import mkstemp
from threading import Event, Lock

from .._approval_protocol import ENVELOPE, PACKET_LIMIT, SNAPSHOT

FRESH = 2.0
POLL = 0.05


def child_memory_limit():
    """在启动线程测当时的可用量，给转交保留至少四分之三。"""
    with Path("/proc/meminfo").open() as stream:
        available = next((int(line.split()[1]) * 1024 for line in stream
                          if line.startswith("MemAvailable:")), 0)
    root = Path("/sys/fs/cgroup")
    membership = Path("/proc/self/cgroup").read_text().splitlines()
    group = next((line[3:] for line in membership if line.startswith("0::")), None)
    if group is None or ".." in Path(group).parts:
        raise ValueError("E-TRIAL-DISABLED")
    current = root / group.lstrip("/")
    while True:
        if not current.is_dir():
            raise ValueError("E-TRIAL-DISABLED")
        ceiling = current / "memory.max"
        if ceiling.exists():
            value = ceiling.read_text().strip()
            if value != "max":
                used = int((current / "memory.current").read_text())
                available = min(available, int(value) - used)
        if current == root:
            break
        current = current.parent
    limit = min(128 << 20, available // 4)
    if limit < 32 << 20:
        raise ValueError("E-TRIAL-DISABLED")
    return limit


class Generation:
    def __init__(self, epoch, serial, channel):
        self.epoch, self.serial = epoch, serial
        self.waiting = deque()
        self.channel = channel
        self.sent = self.read = self.done = 0
        self.snapshot = 0.0
        self.accounting = Lock()


class Sidecar:
    def __init__(self, directory: Path, *, fault_hook: Path | None = None):
        self.directory = directory
        self.fault_hook = fault_hook
        self.epoch = self.active = self.serial = 0
        self.beat = 0.0
        self.current = None
        self.stopping = Event()
        self.rejected = self.failures = self.unconfirmed = self.card_drops = 0
        self.alive = False
        self.last_snapshot = 0.0
        self.io_errors = 0
        self.started = False

    def connect(self):
        self.epoch += 1
        self.active = self.epoch
        self.pulse()
        if not self.started:
            self.started = True
            # 首连之后才启动。低层线程不等 Python 启动屏障，也不登记退出 join。
            try:
                _thread.start_new_thread(self._publish, ())
                _thread.start_new_thread(self._supervise, ())
            except RuntimeError:
                self.failures += 1

    def pulse(self):
        self.beat = time.monotonic()

    def disconnect(self):
        self.active = 0

    def stop(self):
        self.disconnect()
        self.stopping.set()

    def offer(self, payload, started, deadline):
        gen = self.current
        now = time.monotonic()
        if (gen is not None and gen.epoch == self.active and started <= now < deadline
                and 1 + ENVELOPE.size + len(payload) <= PACKET_LIMIT
                and gen.snapshot and now - gen.snapshot < FRESH
                and gen.accounting.acquire(blocking=False)):
            try:
                if gen is self.current and gen.sent - gen.done < 64:
                    data = b"D" + ENVELOPE.pack(gen.epoch, gen.serial, started, deadline) + payload
                    if gen.channel.send(data) == len(data):
                        gen.sent += 1
                        gen.waiting.append((gen.sent, started, deadline))
                        return True
            except OSError:
                pass
            finally:
                gen.accounting.release()
        self.rejected += 1
        return False

    def _launch(self, endpoint):
        # setpriv 在进入解释器前设置 PDEATHSIG。检查注册前父死亡的竞态，
        # 再由 prlimit 在 exec Python 前限制整个地址空间。
        limit = child_memory_limit()
        boot = Path(__file__).parents[1] / "__main__.py"
        args = ["/usr/bin/setpriv", "--pdeathsig", "KILL", "/bin/sh", "-c",
                '[ "$PPID" = "$1" ] || exit 125; shift; exec "$@"', "approval",
                str(os.getpid()), "/usr/bin/prlimit", f"--as={limit}:{limit}", "--",
                sys.executable, "-I", "-S", "-B", str(boot), str(endpoint.fileno()),
                str(os.getpid()), str(self.directory)]
        if self.fault_hook:
            args.append(str(self.fault_hook))
        return subprocess.Popen(args, pass_fds=(endpoint.fileno(),), env={},
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)

    def _snapshots(self, gen):
        # 快消费者可在一个采样周期内完成多次，按原采样时间排空反向快照。
        for _ in range(128):
            try:
                packet = gen.channel.recv(SNAPSHOT.size + 1)
            except BlockingIOError:
                return True
            if len(packet) != SNAPSHOT.size:
                return False
            sampled, read, done = SNAPSHOT.unpack(packet)
            with gen.accounting:
                if (not gen.done <= done <= read <= gen.sent or read < gen.read
                        or not gen.snapshot <= sampled <= time.monotonic()):
                    return False
                while gen.waiting and gen.waiting[0][0] <= read:
                    gen.waiting.popleft()
                gen.read, gen.done = read, done
                self.last_snapshot = gen.snapshot = sampled
        return True

    def _generation(self, epoch):
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_NONBLOCK)
        with parent, child:
            for endpoint in (parent, child):
                endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2 * PACKET_LIMIT)
            child_pid = self._launch(child)
            child.close()
            self.serial += 1
            gen = Generation(epoch, self.serial, parent)
            started = time.monotonic()
            self.current = gen
            self.alive = True
            try:
                while child_pid.poll() is None:  # waitpid(WNOHANG)
                    now = time.monotonic()
                    if self.active != epoch or self.stopping.is_set():
                        break
                    if gen.waiting and now >= gen.waiting[0][2]:
                        break
                    if now - (gen.snapshot or started) > 5 or now - self.beat > FRESH:
                        break
                    try:
                        parent.send(b"H" + struct.pack("!d", self.beat))
                    except BlockingIOError:
                        pass
                    if not self._snapshots(gen):
                        break
                    time.sleep(POLL)
            finally:
                with gen.accounting:
                    self.unconfirmed += gen.sent - gen.done
                    self.current = None
                parent.close()  # EOF 先冻结，独立于子进程清理路径强杀。
                end = time.monotonic() + 2
                kill_failed = False
                while child_pid.poll() is None:
                    if time.monotonic() >= end:
                        try:
                            child_pid.kill()
                        except OSError:
                            if not kill_failed:
                                self.failures += 1
                                kill_failed = True
                    time.sleep(POLL)
                self.alive = False
            return gen.snapshot > started + 30

    def _supervise(self):
        delay = 1
        while not self.stopping.is_set():
            epoch = self.active
            if not epoch:
                self.stopping.wait(POLL)
                continue
            try:
                stable = self._generation(epoch)
            except (OSError, ValueError, subprocess.SubprocessError):
                stable = False
            if epoch == self.active:
                self.failures += 1
                if stable:
                    delay = 1
                self.stopping.wait(delay)
                delay = min(60, delay * 2)

    def status(self):
        now = time.monotonic()
        gen = self.current
        age = now - self.last_snapshot if self.last_snapshot else None
        state = "disabled"
        backlog = oldest = 0
        unconfirmed = self.unconfirmed
        if gen:
            with gen.accounting:
                unconfirmed = self.unconfirmed
                if gen is self.current:
                    if gen.snapshot and gen.epoch == self.active:
                        state = "ready" if now - gen.snapshot < FRESH else "stale"
                    oldest = max(0, now - gen.waiting[0][1]) if gen.waiting else 0
                    backlog = gen.sent - gen.read
                    unconfirmed += gen.sent - gen.done
        return dict(state=state, alive=self.alive, snapshot_age=age, sampled=now, oldest_wait=oldest,
                    backlog=backlog, unconfirmed=unconfirmed, rejected=self.rejected,
                    card_drops=self.card_drops, failures=self.failures, io_errors=self.io_errors)

    def _publish(self):
        recorded = 0
        while True:
            state = self.status()
            try:
                self.directory.mkdir(mode=0o700, exist_ok=True)
                if self.directory.stat().st_mode & 0o077 or self.directory.stat().st_uid != os.getuid():
                    raise PermissionError
                if self.failures > recorded:
                    count = self.failures
                    with (self.directory / "supervisor.log").open("a") as stream:
                        stream.write(f"CRITICAL E-TRIAL-DISABLED {count - recorded}\n")
                    recorded = count
                fd, name = mkstemp(dir=self.directory, prefix=".status-")
                try:
                    with os.fdopen(fd, "w") as stream:
                        json.dump(state, stream)
                    os.replace(name, self.directory / "status.json")
                finally:
                    Path(name).unlink(missing_ok=True)
            except OSError:
                self.io_errors += 1
            if self.stopping.is_set():
                return
            self.stopping.wait(0.1)


def publish_disabled(directory):
    observer = Sidecar(directory)
    observer.stop()
    observer._publish()


def read_status(directory):
    """只读巡检。发布者本身停顿时也使最后一次就绪过期。"""
    try:
        state = json.loads((directory / "status.json").read_text())
    except FileNotFoundError:
        return dict(state="disabled", alive=False, snapshot_age=None, sampled=time.monotonic(),
                    backlog=0, oldest_wait=0, unconfirmed=0, rejected=0, card_drops=0, failures=0, io_errors=0)
    elapsed = time.monotonic() - state["sampled"]
    if state["snapshot_age"] is not None:
        state["snapshot_age"] += max(0, elapsed)
    if elapsed < 0 or elapsed >= FRESH or (state["snapshot_age"] or 0) >= FRESH:
        if state["state"] == "ready":
            state["state"] = "stale"
    return state
