"""The frames service: watches the phone's picture for agents. It tells when the
screen has settled after an action, and hands out screenshots.

It is a viewer of the video hub like any other (hub.py on Linux, avhub.py on
Windows), so it gets a keyframe whenever it starts: unlike the hub, it can restart
at any time without Screen Mirroring being restarted. Control is the "frames"
service (ipc.py), one line per request:

  status       "ok live=1 seq=N": live while it has a picture of the phone; N
               counts meaningful picture changes (not pointer moves or carets)
  settle [N|-] [Q] [C]  wait until the picture is still for Q seconds (default 0.1);
               with N, first up to C seconds (default 1) for a change after N
  shot         PNG of the current picture (2 px per phone point):
               "ok LENGTH\\n" + bytes
"""
import io
import os
import threading
import time

import numpy as np

from . import ipc, screen
from .config import IS_WINDOWS, SCREEN_PT

DYNAMIC_AFTER = 1.0  # a region moving continuously this long (s) is video/animation: ignored
AMBIENT_AFTER = 0.4  # after this long (s), only big motion keeps settle waiting
BIG = 0.05  # a change of at least this share of the picture is big (a page, a scroll)
SHOT_SIZE = (SCREEN_PT[0] * 2, SCREEN_PT[1] * 2)


def log(*a):
    print(time.strftime("%H:%M:%S"), "frames:", *a, flush=True)


class Frames:
    """Change counting and settling, fed one luma thumbnail per decoded frame by a
    source (GstSource on Linux, AvSource on Windows), which also makes screenshots."""

    def __init__(self):
        self.source = None
        self.seq = 0  # meaningful picture changes (see changed)
        self.frame_at = 0.0  # when the last meaningful change arrived
        self.ref = None  # luma thumbnail at the last meaningful change
        self.prev_thumb = None
        self.cv = threading.Condition()
        self.history = []
        self.dynamic_frac = 0.0  # share of the picture currently ignored as video/animation
        self.last_frac = 0.0  # size of the last counted change (share of the picture)
        self.big_at = 0.0  # when the picture last changed in a big way (>= BIG)
        self.last_still = True
        self.ambient = False
        self.last_move = self.moving_since = None

    def feed(self, thumb):
        """A decoded frame's luma thumbnail (every 16th pixel), or None when it
        couldn't be read (counts as a change)."""
        if thumb is None or self.changed(thumb):
            with self.cv:
                self.seq += 1
                self.frame_at = time.time()
                self.cv.notify_all()

    def changed(self, thumb):
        """Did this frame change the picture meaningfully? Compares a coarse luma
        thumbnail (every 16th pixel) with the one at the last change: more than
        0.4% of its cells must differ. The pointer moving, a blinking caret or a
        small spinner stay under that, so they don't keep the screen "moving"."""
        thumb = thumb.astype(np.int16)
        now = time.time()
        prev = self.prev_thumb
        self.prev_thumb = thumb
        if prev is None or prev.shape != thumb.shape or self.ref is None or self.ref.shape != thumb.shape:
            self.ref = thumb
            self.last_move = np.full(thumb.shape, now)
            self.moving_since = np.full(thumb.shape, now)
            return True
        # Per cell: when it last moved, and since when it has been moving without a
        # pause. A cell that has moved continuously for over DYNAMIC_AFTER seconds
        # (a playing video, an animated banner) is ignored: page transitions and
        # scroll flings finish well within that, video never does.
        moved = np.abs(thumb - prev) > 12
        paused = now - self.last_move > 0.6  # pixels in a video pause now and then
        self.moving_since[moved & paused] = now
        self.last_move[moved] = now
        dynamic = (now - self.moving_since > DYNAMIC_AFTER) & (now - self.last_move < 0.6)
        # Grow by 3 cells (~16 pt) so a whole video rectangle is covered, not only
        # the cells that happened to move.
        for _ in range(3):
            d = dynamic.copy()
            d[1:, :] |= dynamic[:-1, :]
            d[:-1, :] |= dynamic[1:, :]
            d[:, 1:] |= dynamic[:, :-1]
            d[:, :-1] |= dynamic[:, 1:]
            dynamic = d
        differs = (np.abs(thumb - self.ref) > 12) & ~dynamic
        frac = float(differs.mean())
        self.dynamic_frac = float(dynamic.mean())
        self.history.append((now, round(frac, 4), round(float(moved.mean()), 4)))  # for studying settle
        del self.history[:-3000]
        if frac > 0.004:
            self.ref = np.where(dynamic, self.ref, thumb)
            self.last_frac = frac
            if frac >= BIG:
                self.big_at = now
            return True
        return False

    def settle(self, since=None, change=1.0, quiet=0.1, limit=3.0):
        """Wait until the picture is still. Measured (history, PHONEUSE_STEPLOG):
        an action's first visible change comes ~0.1-0.15 s after the input ends,
        animations run 0.4-0.7 s (scroll flings ~1-2.5 s), and frames during an
        animation are <50 ms apart, so 0.1 s without a meaningful change means
        it ended; 1 s without any change means the action changed nothing. (The Home
        key is the slow one: iOS holds a single press back to rule out a double
        press, and its first change came 0.5 s after the command had returned, so a
        0.6 s wait read the old screen.) With
        `since` (the change count before an action), first wait for that action's
        first visible change, up to `change` seconds (an action may change nothing).
        Then wait until nothing meaningful has changed for `quiet` seconds. At once
        when nothing happens."""
        start = time.time()
        with self.cv:
            if since is not None:
                while self.seq <= since and time.time() - start < change:
                    self.cv.wait(change - (time.time() - start))
            still = False
            ambient = False
            while time.time() - start < limit:
                idle = time.time() - self.frame_at
                if idle >= quiet:
                    still = True
                    break
                # After AMBIENT_AFTER s (time for small animations like a switch to
                # finish), only big motion (a page moving, a scroll coasting) keeps
                # us waiting; small local changes that never stop (tiles cycling,
                # clips swapping) are ambient: return, and say so.
                now = time.time()
                if now - start > AMBIENT_AFTER and now - self.big_at >= quiet:
                    still = ambient = True
                    break
                self.cv.wait(min(quiet - idle, 0.05))
            self.ambient = ambient
            self.last_still = still
            return self.seq

    def client(self, conn, rfile):
        for line in rfile:
            cmd, _, arg = line.decode(errors="replace").strip().partition(" ")
            try:
                if cmd == "status":
                    conn.sendall(f"ok live={int(self.source.live())} seq={self.seq}\n".encode())
                elif cmd == "settle":  # settle [SINCE|-] [QUIET] [CHANGE]
                    a = arg.split()
                    since = int(a[0]) if a and a[0] != "-" else None
                    quiet = float(a[1]) if len(a) > 1 else 0.1
                    change = float(a[2]) if len(a) > 2 else 1.0
                    seq = self.settle(since, change=change, quiet=quiet)
                    conn.sendall(f"ok seq={seq} still={int(self.last_still)} ambient={int(self.ambient)} "
                                 f"ignored={self.dynamic_frac:.3f}\n".encode())
                elif cmd == "history":  # history SINCE_EPOCH: frames after that time, as JSON
                    import json
                    t0 = float(arg or 0)
                    conn.sendall((json.dumps([h for h in self.history if h[0] >= t0]) + "\n").encode())
                elif cmd == "shot":
                    png = self.source.screenshot()
                    conn.sendall(b"err not mirroring\n" if png is None else f"ok {len(png)}\n".encode() + png)
                else:
                    conn.sendall(b"err unknown command\n")
            except OSError:
                return
            except Exception as e:
                log("control error:", e)
                conn.sendall(f"err {e}\n".encode())


class AvSource:
    """Windows: frames from avhub.py over its subscription, decoded with PyAV."""

    def __init__(self, frames):
        import av
        import av.codec.context
        self.av = av
        self.frames = frames
        self.last = None  # last decoded av.VideoFrame
        self.mirroring = False
        self.shot_lock = threading.Lock()
        self.decoder = None

    def start(self):
        from .avhub import subscribe
        threading.Thread(target=subscribe, args=(self._frame, self._subscribed), daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()

    def _subscribed(self, on):
        if on:
            self.decoder = self._new_decoder()
            log("watching the hub's stream")

    def _new_decoder(self):
        d = self.av.CodecContext.create("h264", "r")
        d.flags |= self.av.codec.context.Flags.low_delay
        d.thread_type = "SLICE"
        return d

    def _frame(self, data, key):
        try:
            frames = self.decoder.decode(self.av.Packet(data))
        except self.av.error.FFmpegError as e:
            # The hub sends keyframes only when asked: ask, or every later frame fails too.
            log("decode error:", e, "(asking the hub for a keyframe)")
            self.decoder = self._new_decoder()
            try:
                screen.hub_request("keyframe")
            except OSError:
                pass
            return
        for f in frames:
            self.last = f
            plane = f.planes[0]
            y = np.frombuffer(plane, np.uint8).reshape(plane.height, plane.line_size)
            self.frames.feed(y[::16, :plane.width:16])

    def _watch(self):
        # Forget the picture when mirroring ends (the hub knows, and debounces it), or
        # when the hub has been unreachable for a few tries in a row (it is gone).
        failures = 0
        while True:
            try:
                line, _ = screen.hub_request("status", timeout=2)
                self.mirroring = "mirroring=1" in line
                failures = 0
            except OSError:
                failures += 1
                if failures >= 3:
                    self.mirroring = False
            if not self.mirroring:
                self.last = None
            time.sleep(0.5)

    def live(self):
        return self.last is not None and self.mirroring

    def screenshot(self):
        with self.shot_lock:
            f = self.last
            if f is None:
                return None
            img = f.to_image().resize(SHOT_SIZE)  # PIL; bilinear, like the Linux videoscale
            buf = io.BytesIO()
            img.save(buf, "PNG", compress_level=1)
            return buf.getvalue()


class GstSource:
    """Linux: frames from hub.py as RTP, decoded with GStreamer."""

    def __init__(self, frames):
        import gi
        gi.require_version("Gst", "1.0")
        gi.require_version("GstVideo", "1.0")
        from gi.repository import GLib, Gst, GstVideo
        from .config import decoder
        self.Gst, self.GstVideo, self.GLib = Gst, GstVideo, GLib
        Gst.init(None)
        self.frames = frames
        self.last = None
        self.caps = None
        self.vinfo = None
        self.shot_lock = threading.Lock()
        dec = os.environ.get("PHONEUSE_DECODER") or decoder()
        rtp = "application/x-rtp,media=video,encoding-name=H264,clock-rate=90000,payload=96"
        # System memory: NVDEC's own CUDA surfaces get reused while we hold the last one.
        download = "video/x-raw ! " if dec.startswith("nvh264dec") else ""
        w, h = SHOT_SIZE
        self.pipe = Gst.parse_launch(
            f'udpsrc address=127.0.0.1 port={screen.FRAMES_RTP_PORT} buffer-size=8388608 caps="{rtp}" '
            f"! rtph264depay ! h264parse ! {dec} ! {download}appsink name=raw sync=false async=false emit-signals=true "
            f"appsrc name=shot format=time do-timestamp=true ! videoconvert ! videoscale "
            f"! video/x-raw,format=RGB,width={w},height={h} ! pngenc compression-level=1 "
            f"! appsink name=png sync=false async=false max-buffers=1 drop=true")
        self.shot_src = self.pipe.get_by_name("shot")
        self.png = self.pipe.get_by_name("png")
        self.pipe.get_by_name("raw").connect("new-sample", self._decoded)
        bus = self.pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", lambda _b, m: log("stream error:", m.parse_error()[0].message))

    def start(self):
        self.pipe.set_state(self.Gst.State.PLAYING)
        threading.Thread(target=self._stay_joined, daemon=True).start()
        threading.Thread(target=self.GLib.MainLoop().run, daemon=True).start()

    def _decoded(self, sink):
        Gst = self.Gst
        sample = sink.emit("pull-sample")
        if self.caps is None or not sample.get_caps().is_equal(self.caps):
            self.caps = sample.get_caps()
            self.vinfo = self.GstVideo.VideoInfo.new_from_caps(self.caps)
            self.shot_src.set_property("caps", self.caps)
        self.last = sample.get_buffer()
        self.frames.feed(self._thumb(self.last))
        return Gst.FlowReturn.OK

    def _thumb(self, buf):
        try:
            ok, m = buf.map(self.Gst.MapFlags.READ)
            if not ok:
                return None
            try:
                vi = self.vinfo
                stride, h, w = vi.stride[0], vi.height, vi.width
                y = np.frombuffer(m.data, np.uint8, count=stride * h).reshape(h, stride)
                return y[::16, :w:16].copy()
            finally:
                buf.unmap(m)
        except Exception:
            return None

    def _stay_joined(self):
        # Keep subscribed to the hub; a first join (or one after the hub restarts)
        # brings a keyframe. Forget the picture when mirroring ends.
        joined = False
        while True:
            try:
                line, _ = screen.hub_request(f"{'keep' if joined else 'join'} {screen.FRAMES_RTP_PORT}")
                joined = line.startswith("ok")
            except OSError:
                joined = False
            if self.last is not None and not screen.airplay_mirroring():
                self.last = None
            time.sleep(1)

    def live(self):
        return self.last is not None

    def screenshot(self):
        Gst = self.Gst
        with self.shot_lock:
            if self.last is None:
                return None
            while self.png.emit("try-pull-sample", 0):  # drop a stale one
                pass
            self.shot_src.emit("push-buffer", self.last.copy())
            sample = self.png.emit("try-pull-sample", 2 * Gst.SECOND)
            if sample is None:
                return None
            buf = sample.get_buffer()
            return buf.extract_dup(0, buf.get_size())


def main():
    if IS_WINDOWS:
        from .windows import precise_timers
        precise_timers()
    frames = Frames()
    frames.source = (AvSource if IS_WINDOWS else GstSource)(frames)
    frames.source.start()
    ipc.serve("frames", frames.client, log=lambda *a: log(*a))


if __name__ == "__main__":
    main()
