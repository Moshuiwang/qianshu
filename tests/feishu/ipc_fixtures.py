import socket
import subprocess

import pytest


def require_endpoint(kind):
    a, b = socket.socketpair(type=kind)
    with a, b:
        try:
            a.send(b"H")
            assert b.recv(1) == b"H"
            a.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16384)
            if kind == socket.SOCK_SEQPACKET:
                a.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        except PermissionError:
            pytest.skip(f"沙箱内未跑：{kind.name} 的 I/O 被拒绝")


@pytest.fixture(scope="module")
def stream_io():
    require_endpoint(socket.SOCK_STREAM)


@pytest.fixture(scope="module")
def packet_io(stream_io):
    require_endpoint(socket.SOCK_SEQPACKET)


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    root = tmp_path_factory.mktemp("main-2a")
    for name in subprocess.check_output(["git", "ls-tree", "-r", "--name-only", "48d57ed", "src"],
                                        text=True).splitlines():
        dest = root / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(subprocess.check_output(["git", "show", f"48d57ed:{name}"]))
    return root / "src"
