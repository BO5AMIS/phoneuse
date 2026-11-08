"""The video hub on Windows (PyAV/FFmpeg; hub.py is the GStreamer one used on Linux).

UxPlay sends the phone's stream here as RTP (screen.HUB_RTP_PORT). iOS sends a full
keyframe only when mirroring starts, so anything that starts later couldn't decode
the phone's own stream. The hub decodes the stream once, keeps the last frame, and
encodes it again (NVENC on an NVIDIA GPU, x264 otherwise): no B-frames, no delay,
keyframes only on request. When a viewer subscribes it gets a keyframe of the last
frame at once, so viewers (the frames service, the browser mirror) can restart at
any time. Only a hub restart needs Screen Mirroring restarted, so the hub does
nothing else.

Viewers connect to the "hub" service (ipc.py) and send one line:

  subscribe    the reply "ok" is followed by the stream: for each encoded frame a
               5-byte header (flags: 1 = keyframe; length, big endian) and an
               Annex B access unit. The first frame is a keyframe. A viewer that
               reads too slowly skips ahead to the next keyframe.
  keyframe     encode the last frame again as a keyframe
  status       "ok live=1 mirroring=1 size=WxH ..." (live: it has a picture of the phone)

Unlike the Linux hub's UDP viewers, a subscription lasts as long as its connection:
nothing to renew, nothing to time out.
"""
import collections
import socket
import struct
import threading
import time
from fractions import Fraction

import av
import av.codec.context
from av.video.frame import PictureType

from . import ipc, screen
from .config import CFG
from .rtp import Depacketizer

HEADER = struct.Struct("!BI")
QUEUE = 90  # frames a viewer may fall behind before it skips to a keyframe
AIRPLAY_CHECK = 1.0  # how often to check whether a phone is mirroring (s)
MIRROR_GONE = 3  # checks in a row without the phone's connection before mirroring counts as ended


def log(*a):
    print(time.strftime("%H:%M:%S"), "hub:", *a, flush=True)


ENCODERS = {
    # Constant quality, low enough to keep small text sharp; every frame leaves the
    # encoder as soon as it is encoded (measured 2.7 ms on an RTX 4080 at 1206x2622).
    "h264_nvenc": {"preset": "p2", "tune": "ull", "zerolatency": "1", "delay": "0", "rc": "vbr",
                   "cq": "19", "forced-idr": "1", "bf": "0"},
    "libx264": {"preset": "veryfast", "tune": "zerolatency", "crf": "18", "forced-idr": "1",
                "x264-params": "keyint=infinite:scenecut=0:bframes=0"},
    "h264_mf": {},
}


def open_encoder(width, height, skip=()):
    """The first encoder that opens, in ENCODERS order, leaving out `skip` (encoders
    that opened but then failed to encode, e.g. an NVENC session limit)."""
    names = list(ENCODERS) if CFG.get("encoder", "auto") == "auto" else [CFG["encoder"]]
    names = [n for n in names if n not in skip] or names
    errors = []
    for name in names:
        try:
            e = av.CodecContext.create(name, "w")
            e.width, e.height, e.pix_fmt = width, height, "yuv420p"
            e.time_base = Fraction(1, 1000)
            e.framerate = Fraction(60, 1)
            e.gop_size = 1 << 30  # keyframes only when asked for
            e.max_b_frames = 0
            e.options = dict(ENCODERS.get(name, {}))
            e.open()
            return e
        except Exception as ex:
            errors.append(f"{name}: {ex}")
    raise RuntimeError("no H.264 encoder works: " + "; ".join(errors))


def open_decoder():
    d = av.CodecContext.create("h264", "r")
    # Hand out each frame as soon as it is decoded: iOS doesn't declare that frames
    # never need reordering, and a decoder waiting for a reorder would hold every
    # frame until the next one arrives (the phone sends frames only on change).
    d.flags |= av.codec.context.Flags.low_delay
    d.thread_type = "SLICE"  # frame threading would add a frame of delay per thread
    d.thread_count = 4
    return d


class Viewer:
    def __init__(self, conn):
        self.conn = conn
        self.queue = collections.deque()
        self.cv = threading.Condition()
        self.synced = False  # has had a keyframe
        self.closed = False

    def put(self, data, key):
        with self.cv:
            if key:
                self.synced = True
            if not self.synced:
                return False
            if len(self.queue) >= QUEUE:  # too slow: drop the backlog, start again at a keyframe
                self.queue.clear()
                self.synced = False
                return True  # (asks for a keyframe)
            self.queue.append(HEADER.pack(1 if key else 0, len(data)) + data)
            self.cv.notify()
            return False

    def run(self):
        try:
            while not self.closed:
                with self.cv:
                    while not self.queue and not self.closed:
                        self.cv.wait(1.0)
                    batch = list(self.queue)
                    self.queue.clear()
                if batch:
                    self.conn.sendall(b"".join(batch))
        except OSError:
            pass
        finally:
            self.closed = True


class Hub:
    def __init__(self):
        self.decoder = open_decoder()
        self.encoder = None
        self.encoder_name = None
        self.last = None  # last decoded frame (av.VideoFrame)
        self.pending = None  # newest decoded frame not yet encoded
        self.key_wanted = False
        self.cv = threading.Condition()
        self.viewers = set()
        self.viewers_lock = threading.Lock()
        self.mirroring = False
        self.t0 = time.monotonic()
        self.last_pts = -1
        self.depay = Depacketizer()
        self.counts = {"received": 0, "decoded": 0, "held": 0, "encoded": 0, "errors": 0}
        self.failed_encoders = {}  # encoder name -> encode failures in a row

    # ---- in: RTP from UxPlay, decoded once ----
    def receive(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 << 20)
        s.bind(("127.0.0.1", screen.HUB_RTP_PORT))
        log("receiving the phone's video on port", screen.HUB_RTP_PORT)
        restarts = 0
        while True:
            pkt = s.recv(65535)
            try:
                for au in self.depay.push(pkt):
                    self.decode(au)
            except Exception as e:  # never let one bad packet stop the hub's only input
                log("receive error:", e)
            if self.depay.restarts != restarts:
                restarts = self.depay.restarts
                log("a new stream started (the phone or UxPlay started mirroring again)")

    def decode(self, au):
        self.counts["received"] += 1
        try:
            frames = self.decoder.decode(av.Packet(au))
        except av.error.FFmpegError as e:
            self.counts["errors"] += 1
            if self.counts["errors"] in (1, 10, 100) or self.counts["errors"] % 1000 == 0:
                log(f"decode error ({self.counts['errors']} so far): {e}")
            return
        if not frames and self.counts["decoded"]:
            # A frame went in and none came out: the decoder is holding it back, and
            # the mirror would show every change one frame late. Say so once.
            self.counts["held"] += 1
            if self.counts["held"] == 5:
                log("the decoder holds frames back (each change shows one frame late)")
        for f in frames:
            self.counts["decoded"] += 1
            with self.cv:
                self.last = self.pending = f
                self.cv.notify()

    # ---- out: encoded again, to every viewer ----
    def encode_loop(self):
        while True:
            with self.cv:
                while self.pending is None and not (self.key_wanted and self.last is not None):
                    self.cv.wait()
                frame = self.pending if self.pending is not None else self.last
                key = self.key_wanted
                self.pending, self.key_wanted = None, False
            if frame is None:  # mirroring ended meanwhile
                continue
            try:
                self.encode(frame, key)
                if self.encoder is not None:
                    self.failed_encoders.pop(self.encoder.name, None)
            except Exception as e:
                name = self.encoder.name if self.encoder is not None else "?"
                n = self.failed_encoders[name] = self.failed_encoders.get(name, 0) + 1
                log(f"encode error ({name}, {n} in a row): {e}")
                self.encoder = None  # open a fresh one with the next frame
                time.sleep(0.5)  # don't spin on an error that repeats
                with self.cv:
                    self.key_wanted = True

    def encode(self, frame, key):
        if self.encoder is None or (self.encoder.width, self.encoder.height) != (frame.width, frame.height):
            # An encoder that keeps failing (opens, then can't encode) is skipped next time.
            bad = {n for n, count in self.failed_encoders.items() if count >= 3}
            self.encoder = open_encoder(frame.width, frame.height, skip=bad)
            if self.encoder.name != self.encoder_name:
                log(f"encoding with {self.encoder.name} at {frame.width}x{frame.height}")
            self.encoder_name = self.encoder.name
            key = True
        if frame.format.name != "yuv420p":
            frame = frame.reformat(format="yuv420p")
        pts = int((time.monotonic() - self.t0) * 1000)
        frame.pts = self.last_pts = max(pts, self.last_pts + 1)
        frame.pict_type = PictureType.I if key else PictureType.NONE
        for packet in self.encoder.encode(frame):
            self.counts["encoded"] += 1
            self.broadcast(bytes(packet), packet.is_keyframe)

    def broadcast(self, data, key):
        resync = False
        with self.viewers_lock:
            for v in list(self.viewers):
                if v.closed:
                    self.viewers.discard(v)
                elif v.put(data, key):
                    resync = True
        if resync:
            self.request_keyframe()

    def request_keyframe(self):
        with self.cv:
            self.key_wanted = True
            self.cv.notify()

    # ---- control ----
    def watch_airplay(self):
        # Mirroring starts at the first sighting of the phone's connection and ends after
        # MIRROR_GONE checks in a row without it: one odd reading of the connection
        # table must not blank the picture.
        misses = 0
        while True:
            m = screen.airplay_mirroring()
            if m:
                misses = 0
                if not self.mirroring:
                    log("phone mirroring")
                    self.mirroring = True
            elif m is False:
                misses += 1
                if self.mirroring and misses >= MIRROR_GONE:
                    log("mirroring ended")
                    self.mirroring = False
            if not self.mirroring and self.last is not None:
                with self.cv:
                    self.last = None  # the last frame no longer shows the phone
            time.sleep(AIRPLAY_CHECK)

    def status(self):
        f = self.last
        size = f"{f.width}x{f.height}" if f is not None else "-"
        return (f"ok live={int(f is not None and self.mirroring)} mirroring={int(self.mirroring)} size={size} "
                f"encoder={self.encoder_name or '-'} viewers={len(self.viewers)} received={self.counts['received']} "
                f"decoded={self.counts['decoded']} held={self.counts['held']} encoded={self.counts['encoded']} "
                f"errors={self.counts['errors']} lost={self.depay.lost}")

    def client(self, conn, rfile):
        for line in rfile:
            cmd = line.decode(errors="replace").strip()
            if cmd == "subscribe":
                conn.sendall(b"ok\n")
                v = Viewer(conn)
                with self.viewers_lock:
                    self.viewers.add(v)
                log("viewer joined;", len(self.viewers), "now")
                self.request_keyframe()
                v.run()  # until the viewer goes away
                with self.viewers_lock:
                    self.viewers.discard(v)
                log("viewer left;", len(self.viewers), "now")
                return
            if cmd == "keyframe":
                self.request_keyframe()
                conn.sendall(b"ok\n")
            elif cmd == "status":
                conn.sendall((self.status() + "\n").encode())
            else:
                conn.sendall(b"err unknown command\n")


def subscribe(on_frame, on_state=None, stop=None):
    """Viewer side: call on_frame(data, is_keyframe) for every frame the hub sends,
    reconnecting whenever the hub (re)starts. Runs in the calling thread.
    on_state(True/False) is told when the subscription starts and ends."""
    while stop is None or not stop.is_set():
        try:
            with ipc.connect("hub", timeout=5.0) as s:
                s.sendall(b"subscribe\n")
                f = s.makefile("rb")
                if not f.readline().startswith(b"ok"):
                    raise OSError("the hub refused the subscription")
                s.settimeout(None)
                if on_state:
                    on_state(True)
                while True:
                    head = f.read(HEADER.size)
                    if len(head) < HEADER.size:
                        break
                    flags, n = HEADER.unpack(head)
                    data = f.read(n)
                    if len(data) < n:
                        break
                    try:
                        on_frame(data, bool(flags & 1))
                    except Exception as e:  # a viewer's bug must not end its subscription for good
                        log("viewer error:", e)
        except OSError:
            pass
        if on_state:
            on_state(False)
        time.sleep(0.5)


def main():
    from .windows import precise_timers
    precise_timers()
    hub = Hub()
    threading.Thread(target=hub.receive, daemon=True).start()
    threading.Thread(target=hub.encode_loop, daemon=True).start()
    threading.Thread(target=hub.watch_airplay, daemon=True).start()
    ipc.serve("hub", hub.client, log=lambda *a: log(*a))


if __name__ == "__main__":
    main()
