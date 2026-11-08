"""Connections between phoneuse's own processes: the HID service, the video hub, the
frames service, the vision service, and the browser mirror's local HTTP API.

Linux: unix sockets (paths in config.py), protected by file permissions.

Windows: TCP on 127.0.0.1 (config.SERVICE_PORTS). Every connection starts with the
line "auth TOKEN": the token is a random secret in the runtime directory
(%LOCALAPPDATA%\\phoneuse\\run\\token), which only this Windows user can read.
Without it any web page could drive the phone: a browser's POST to 127.0.0.1 sends
its body as lines, and these protocols are line based.

Connecting to a port nobody listens on takes 2 s on Windows (it retries the SYN);
connect() turns those retries off, so a stopped service is noticed at once.
"""
import http.client
import os
import secrets
import socket
import threading
import time

from .config import FRAMES_SOCKET, HID_SOCKET, HUB_SOCKET, IS_WINDOWS, SERVICE_PORTS, VISION_SOCKET, runtime_path

PATHS = {"hid": HID_SOCKET, "hub": HUB_SOCKET, "frames": FRAMES_SOCKET, "vision": VISION_SOCKET}
TOKEN_FILE = runtime_path("token")
_token = None


def token():
    """This user's service token, created by whichever process needs it first."""
    global _token
    if _token:
        return _token
    for _ in range(50):
        try:
            with open(TOKEN_FILE) as f:
                t = f.read().strip()
            if t:
                _token = t
                return t
        except FileNotFoundError:
            try:
                fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass  # another process is writing it
            else:
                with os.fdopen(fd, "w") as f:
                    f.write(secrets.token_hex(16))
                continue
        time.sleep(0.02)
    raise RuntimeError(f"can't read the service token in {TOKEN_FILE}")


def fail_fast(sock):
    """Windows: make connect() to a closed local port fail at once instead of after 2 s."""
    if not IS_WINDOWS:
        return
    import ctypes
    from ctypes import wintypes

    class InitialRto(ctypes.Structure):
        _fields_ = [("Rtt", ctypes.c_ushort), ("MaxSynRetransmissions", ctypes.c_ubyte)]

    params = InitialRto(0xFFFF, 0xFE)  # RTT unspecified, no SYN retransmissions
    out = wintypes.DWORD()
    ctypes.WinDLL("ws2_32").WSAIoctl(ctypes.c_size_t(sock.fileno()), wintypes.DWORD(0x98000011),  # SIO_TCP_INITIAL_RTO
                                     ctypes.byref(params), ctypes.sizeof(params), None, 0, ctypes.byref(out), None, None)


def _tcp(port, timeout):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        fail_fast(s)
        s.settimeout(timeout)
        s.connect(("127.0.0.1", port))
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return s
    except BaseException:
        s.close()
        raise


def connect(name, timeout=None):
    """A connected socket to one of phoneuse's services ("hid", "hub", "frames", "vision")."""
    if IS_WINDOWS:
        s = _tcp(SERVICE_PORTS[name], timeout)
        s.sendall(f"auth {token()}\n".encode())
        return s
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(PATHS[name])
    return s


def listen(name, backlog=16):
    """The listening socket for a service. Fails if another copy already serves it."""
    if IS_WINDOWS:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Exclusive: a second copy of the service must fail, not share the port.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("127.0.0.1", SERVICE_PORTS[name]))
        s.listen(backlog)
        token()  # make sure clients can find it
        return s
    path = PATHS[name]
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    os.chmod(path, 0o600)
    s.listen(backlog)
    return s


def authorized(rfile):
    """Server side, on a new connection: read and check the auth line (Windows).
    False means: close the connection without answering."""
    if not IS_WINDOWS:
        return True
    line = rfile.readline(200)
    return line.strip() == f"auth {token()}".encode()


def serve(name, handler, log=print):
    """Accept connections forever, each in its own thread: handler(conn, rfile) with
    rfile = conn.makefile("rb"), after the auth line. Runs in the calling thread."""
    s = listen(name)
    log(f"control on {describe(name)}")

    def client(conn):
        with conn:
            try:
                if IS_WINDOWS:
                    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                rfile = conn.makefile("rb")
                if authorized(rfile):
                    handler(conn, rfile)
            except (ConnectionError, OSError):
                pass

    while True:
        conn, _ = s.accept()
        threading.Thread(target=client, args=(conn,), daemon=True).start()


def describe(name):
    return f"127.0.0.1:{SERVICE_PORTS[name]}" if IS_WINDOWS else PATHS[name]


def request(name, cmd, timeout=3.0):
    """One line request to a service; returns (reply line, payload). A reply
    "ok LENGTH" to "shot" is followed by LENGTH bytes of payload."""
    with connect(name, timeout) as s:
        s.sendall(cmd.encode() + b"\n")
        f = s.makefile("rb")
        line = f.readline().decode().strip()
        data = f.read(int(line.split()[1])) if cmd == "shot" and line.startswith("ok ") else b""
        return line, data


# ---------- the browser mirror's local API (web.py) ----------

class _FastHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = _tcp(self.port, self.timeout) if IS_WINDOWS else socket.create_connection(
            (self.host, self.port), self.timeout)


def http_post(port, path, body=b"{}", timeout=2.0):
    """POST JSON to the browser mirror on 127.0.0.1:port; returns (status, body bytes).
    Raises OSError when it isn't running (at once, also on Windows)."""
    c = _FastHTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        c.request("POST", path, body=body, headers={"Content-Type": "application/json"})
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def http_get(port, path="/", timeout=2.0):
    c = _FastHTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        c.request("GET", path)
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()
