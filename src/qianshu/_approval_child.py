"""私有继承端点的合成消费者。此入口不加载 SDK 或任何业务依赖。"""

import fcntl
import logging
import os
import select
import signal
import socket
import struct
import sys
import time
from pathlib import Path

from ._approval_protocol import ENVELOPE, PACKET_LIMIT, SNAPSHOT


def snapshot(channel, read, done):
    try:
        channel.send(SNAPSHOT.pack(time.monotonic(), read, done))
    except BlockingIOError:
        pass


def run():
    fd, expected, folder, *hook = sys.argv[1:]
    channel = socket.socket(fileno=int(fd))
    channel.setblocking(False)
    parent = int(expected)
    peer, uid, _ = struct.unpack("3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if (peer != parent or os.getppid() != parent or uid != os.getuid()
            or channel.type != socket.SOCK_SEQPACKET or channel.family != socket.AF_UNIX):
        return 1
    os.umask(0o077)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
    signal.alarm(5)
    directory = Path(folder)
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.stat().st_mode & 0o077 or directory.stat().st_uid != uid:
        return 1
    logger = logging.getLogger("approval")
    logger.propagate = False
    logger.addHandler(logging.FileHandler(directory / "consumer.log"))
    try:
        with (directory / "consumer.lock").open("a") as lock, channel:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            barrier = None
            if hook:
                import runpy
                barrier = runpy.run_path(hook[0])["barrier"]
                barrier("start")
            read = done = 0
            binding = None
            expires = time.monotonic() + 5
            try:
                while time.monotonic() < expires:
                    if select.select([channel], [], [], 0.1)[0]:
                        packet = channel.recv(PACKET_LIMIT + 1)
                        if not packet or len(packet) > PACKET_LIMIT:
                            break
                        if len(packet) == 9 and packet[:1] == b"H":
                            beat, = struct.unpack("!d", packet[1:])
                            expires = beat + 5
                            remaining = expires - time.monotonic()
                            if not 0 < remaining <= 5:
                                break
                            signal.setitimer(signal.ITIMER_REAL, remaining)
                        elif packet.startswith(b"D"):
                            epoch, serial, started, deadline = ENVELOPE.unpack_from(packet, 1)
                            if binding is None:
                                binding = epoch, serial
                            if binding != (epoch, serial) or not started <= time.monotonic() < deadline:
                                raise ValueError
                            read += 1
                            snapshot(channel, read, done)
                            if barrier:
                                barrier("consume")
                            done += 1
                        else:
                            break
                    if barrier:
                        barrier("tick")
                    snapshot(channel, read, done)
            finally:
                signal.alarm(2)
                if barrier:
                    barrier("close")
        return 0
    except Exception:
        logger.critical("CRITICAL E-TRIAL-DISABLED 1")
        return 1
