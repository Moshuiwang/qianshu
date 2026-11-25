"""群回执沿用 SDK 写路径。仅卡片提示可使用即时写入余量。"""

import fcntl
import socket
import struct
import termios

from .sdk import _before_send


def try_card_write(service, origin, encoded):
    transport = getattr(origin, "transport", None)
    if transport is None:
        return False
    # asyncio 的 TLS 层和底层传输均须有完整余量。单循环中检查到
    # write 之间无 await；Sans-I/O 同步接收整个帧，不进入 drain。
    try:
        tls = getattr(transport, "_ssl_protocol", None)
        raw = tls._transport if tls else transport
        if (len(encoded) > 4096 or raw is None or any(
                t.is_closing() or t.get_write_buffer_size() or t.get_write_buffer_limits()[1] < 16384
                for t in (transport, raw))):
            return False
        sock = raw.get_extra_info("socket")
        if (sock is None or sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF) // 2 < 16384
                or struct.unpack("i", fcntl.ioctl(sock, termios.TIOCOUTQ, bytes(4)))[0]):
            return False
        _before_send(service, "callback_response", encoded)
        origin.protocol.send_binary(encoded)
        origin.send_data()
    except Exception:
        return False
    return True
