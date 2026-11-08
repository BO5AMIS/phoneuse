"""Client for the HID service's command socket (hid_daemon.py on Linux,
windows/hid.py on Windows; both speak the same line protocol)."""
import threading

from . import ipc


class Hid:
    """One connection to the HID service. Thread-safe: commands are serialized."""

    def __init__(self, timeout=None):
        self.sock = ipc.connect("hid", timeout=5.0)
        self.sock.settimeout(timeout)
        self.f = self.sock.makefile("rb")
        self.lock = threading.Lock()

    def cmd(self, line):
        with self.lock:
            self.sock.sendall((line + "\n").encode())
            reply = self.f.readline().decode(errors="replace").strip()
        if not reply:
            raise RuntimeError("the HID service closed the connection")
        if not reply.startswith("ok"):
            raise RuntimeError(reply)
        return reply

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
