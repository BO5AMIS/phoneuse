"""The browser mirror: the phone's screen, with mouse and keyboard, on a local
web page (http://localhost:PORT, `web_port` in config.toml).

The video comes from the hub (hub.py on Linux, as RTP over localhost; avhub.py on
Windows, as a subscription). When a tab joins, this server asks the hub for a
keyframe, so the tab shows the phone right away.

Each tab gets encoded frames over a WebSocket and decodes them itself
(WebCodecs). Mouse and keyboard events come back over the same WebSocket and
go to the phone through pointer.py, like the mirror window's. Input is only
accepted while the phone is mirroring and the tab is showing it.

Listens on 127.0.0.1 only: anyone who can open the page can control the phone.

Unlocking: the page's settings panel saves the phone's passcode in
the config directory (unlock.py). `phoneuse unlock` asks to use it (POST /api/unlock); the owner answers on
the page or in a desktop notification, and only then is it typed (unlock.py).
"""
import asyncio
import json
import os
import secrets
import sys
import threading
import time

from aiohttp import web

from . import screen, unlock
from .config import CFG, IS_WINDOWS, NAME, SCREEN_PT, config_path
from .hidclient import Hid
from .pointer import Keys, Pointer

# Where the pointer rests after Bluetooth (re)connects, and where keep-awake nudges
# it until someone moves it: the bottom middle, above the home bar, where a resting
# pointer covers nothing and touches nothing.
PARK = (0.5, 0.94)

HERE = os.path.dirname(os.path.realpath(__file__))
PAGE = os.path.join(HERE, "web", "index.html")
PORT = int(CFG.get("web_port", 8777))
RTP_PORT = screen.WEB_RTP_PORT


def log(*a):
    print(time.strftime("%H:%M:%S"), "web:", *a, flush=True)


class QuietErrors:
    """Logs an error once while it keeps repeating (every input fails while the HID
    service restarts), then how many more there were."""

    def __init__(self):
        self.last, self.since, self.more = None, 0.0, 0

    def __call__(self, label, e):
        msg, now = f"{label} {e}", time.monotonic()
        if msg == self.last and now - self.since < 10:
            self.more += 1
            return
        if self.more:
            log(f"(the error before repeated {self.more} more times)")
        log(msg)
        self.last, self.since, self.more = msg, now, 0


input_error = QuietErrors()


class AvStream:
    """Windows: the hub's stream (avhub.py), as H.264 access units for the tabs."""

    def __init__(self, on_frame):
        from .avhub import subscribe
        self.subscribed = False
        threading.Thread(target=subscribe, args=(on_frame, self._state), daemon=True).start()

    def _state(self, on):
        self.subscribed = on

    @staticmethod
    def keyframe():
        """Ask the hub for a keyframe (a tab joined or its decoder started over)."""
        try:
            screen.hub_request("keyframe")
        except OSError as e:
            log("hub:", e)


class Stream:
    """Linux: the hub's stream (hub.py), as H.264 access units for the tabs."""

    def __init__(self, on_frame):
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        self.Gst = Gst
        self.on_frame = on_frame  # (bytes, is_keyframe), called from a GStreamer thread
        rtp = "application/x-rtp,media=video,encoding-name=H264,clock-rate=90000,payload=96"
        self.pipe = Gst.parse_launch(
            f'udpsrc address=127.0.0.1 port={RTP_PORT} buffer-size=8388608 caps="{rtp}" '
            "! rtph264depay ! h264parse config-interval=-1 ! video/x-h264,stream-format=byte-stream,alignment=au "
            "! appsink name=enc sync=false async=false emit-signals=true")
        self.pipe.get_by_name("enc").connect("new-sample", self._frame)
        bus = self.pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._error)
        self.pipe.set_state(Gst.State.PLAYING)

    def _frame(self, sink):
        Gst = self.Gst
        buf = sink.emit("pull-sample").get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            self.on_frame(bytes(info.data), not buf.has_flags(Gst.BufferFlags.DELTA_UNIT))
            buf.unmap(info)
        return Gst.FlowReturn.OK

    @staticmethod
    def keyframe():
        """Ask the hub for a keyframe (a tab joined or its decoder started over)."""
        try:
            screen.hub_request("keyframe")
        except OSError as e:
            log("hub:", e)

    def _error(self, _bus, msg):
        err, _ = msg.parse_error()
        log("stream error:", err.message)


class Mirror:
    def __init__(self):
        self.loop = None
        self.tabs = set()
        self.stream = (AvStream if IS_WINDOWS else Stream)(self._frame)
        self.mirroring = False
        self.hub_live = False
        self.hid = None
        self.taken_over = False
        self.agent_until = 0.0
        self.bluetooth = True  # the phone's Bluetooth link (mouse and keyboard)
        self.last_frame_at = 0.0  # when the phone's picture last changed
        self.last_input_at = 0.0  # when the page or an agent last sent input
        self.awake = os.path.exists(AWAKE_FILE)  # keep the screen awake (see keep_awake)
        self.screen_dark = False  # a keep-awake nudge drew no frame: the screen is off
        self.hover_at = PARK  # where the page's pointer last was (screen fractions)
        self.agent_action = ""  # what the agent is doing, for the companion app's Live Activity
        self.agent_phase = "thinking"  # acting (sending input), reading (the screen) or thinking (between actions)
        self.agent_steps = 0  # actions in this agent session
        self.agent_started = 0.0  # when this agent session started
        self.unlocks = None
        self.sessions = None

    def get_hid(self):
        if self.hid is None:
            self.hid = Hid()
        return self.hid

    def live(self):
        return self.mirroring and self.hub_live

    # ---- keep awake: stop Auto-Lock while nobody uses the phone ----
    AWAKE_EVERY = 1.0  # seconds between nudges
    AWAKE_QUIET = 2.0  # skip nudges this long after real input

    def set_awake(self, on):
        self.awake = on
        if on:
            os.makedirs(os.path.dirname(AWAKE_FILE), exist_ok=True)
            open(AWAKE_FILE, "w").close()
        elif os.path.exists(AWAKE_FILE):
            os.remove(AWAKE_FILE)
        self.screen_dark = False
        log("keep awake", "on" if on else "off")
        self.broadcast_state()

    async def keep_awake(self):
        """While on, move the pointer a few pixels and back about once a second so
        Auto-Lock never fires. Real input (the page, an agent) takes over; a nudge
        that draws no new frame means the screen is off (the pointer always shows
        when it moves), so nudging pauses until the picture changes again rather
        than waking a phone that was just locked."""
        # Absolute moves: they land exactly on every phone, while a small relative
        # move can shift the pointer less than a pixel at a slow tracking speed.
        sign, misses = 1, 0
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(self.AWAKE_EVERY)
            if (not self.awake or self.screen_dark or not self.live() or self.agent() != "idle"
                    or time.time() - self.last_input_at < self.AWAKE_QUIET):
                continue
            before = self.last_frame_at
            try:
                x, y = self.hover_at
                x = min(max(x, 0.02), 0.98) + 0.01 * sign
                r = await loop.run_in_executor(None, self.get_hid().cmd, f"a {x:.4f} {min(max(y, 0.0), 1.0):.4f} 0")
            except Exception:
                self.hid = None
                continue
            sign = -sign
            if "dropped" in r:  # the link was busy; this nudge never left
                continue
            await asyncio.sleep(0.8)
            if self.last_frame_at != before or time.time() - self.last_input_at < self.AWAKE_QUIET:
                misses = 0
                continue
            misses += 1
            if misses >= 3:  # three nudges in a row drew nothing: the screen is off
                misses = 0
                self.screen_dark = True
                log("keep awake: no picture after three nudges; the screen is off, pausing")

    # Releasing the link while idle (so iOS shows its own keyboard) is off: after
    # phoneuse drops the link, iOS refuses phoneuse's reconnects ("Connection
    # refused", measured 2026-11-07) until the owner taps PhoneUse in Bluetooth
    # settings, so the next input could never reach the phone. The daemon keeps
    # its release command for a better trigger.
    RELEASE_IDLE = None

    def bluetooth_up(self):
        """Whether the link works: connected, or released on purpose."""
        try:
            st = self.get_hid().cmd("status")
            return "connected=True" in st or "released=True" in st
        except Exception:
            self.hid = None
            return False

    # ---- agent sessions: who is driving the phone ----
    AGENT_IDLE = 20.0  # an agent session ends this long after its last action (s)

    def agent(self):
        """"driving" while an agent acts (the page shows it and takes no input),
        "paused" after the user took over, else "idle"."""
        if self.taken_over:
            return "paused"
        return "driving" if time.time() < self.agent_until else "idle"

    def agent_acts(self, action="", phase="acting"):
        """Called before every agent action (CLI or MCP). False while the user has
        taken over: the agent must stop."""
        if self.taken_over:
            return False
        was = self.agent()
        if was != "driving":
            self.agent_started, self.agent_steps = time.time(), 0
        self.agent_steps += 1
        self.agent_action = action or self.agent_action
        self.agent_phase = phase
        msg = json.dumps({"type": "agent-action", "action": self.agent_action, "steps": self.agent_steps})
        for tab in list(self.tabs):
            asyncio.ensure_future(tab.ws.send_str(msg))
        self.agent_until = time.time() + self.AGENT_IDLE
        self.last_input_at = time.time()
        if was != "driving":
            self.broadcast_state()
        # Tell the pages when the session runs out, so none keeps showing (and
        # obeying) a session that is over.
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.call_later, self.AGENT_IDLE + 0.1, self._agent_expired)
        return True

    def _agent_expired(self):
        if self.agent() == "idle":
            self.broadcast_state()

    def take_over(self, take):
        self.taken_over = take
        self.agent_until = 0.0
        if take and self.sessions:
            self.sessions.end()
        self.broadcast_state()

    def broadcast_state(self):
        for tab in list(self.tabs):
            tab.state()

    def _frame(self, data, key):
        self.last_frame_at = time.time()
        if self.screen_dark:
            self.screen_dark = False
        if self.loop is None:  # the hub's first keyframe can beat the server's start; tabs ask again
            return
        self.loop.call_soon_threadsafe(self._send_frame, data, key)

    def _send_frame(self, data, key):
        for tab in list(self.tabs):
            if key:
                tab.synced = True
            if tab.synced:
                tab.send(bytes([1 if key else 0]) + data)

    async def watch(self):
        # Stay subscribed to the hub, and follow whether the phone is mirroring and
        # the hub has a picture of it.
        loop = asyncio.get_running_loop()
        joined = False
        while True:
            mirroring = False
            try:
                if not IS_WINDOWS:  # Linux viewers renew their UDP subscription; Windows ones stay connected
                    line, _ = await loop.run_in_executor(None, screen.hub_request, f"{'keep' if joined else 'join'} {RTP_PORT}")
                    joined = line.startswith("ok")
                line, _ = await loop.run_in_executor(None, screen.hub_request, "status")
                self.hub_live = "live=1" in line
                mirroring = "mirroring=1" in line
            except OSError:
                joined, self.hub_live = False, False  # the hub is (re)starting
            if not IS_WINDOWS:
                mirroring = screen.airplay_mirroring()
            bt = await loop.run_in_executor(None, self.bluetooth_up)
            changed = mirroring != self.mirroring or bt != self.bluetooth
            if bt and not self.bluetooth:  # (re)connected: rest the pointer where it touches nothing
                self.hover_at = PARK
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None, self.get_hid().cmd, f"a {PARK[0]} {PARK[1]} 0")
                    log("Bluetooth back: pointer parked at the bottom middle")
                except Exception:
                    self.hid = None
            self.mirroring, self.bluetooth = mirroring, bt
            for tab in list(self.tabs):
                if changed or tab.told_live != self.live() or tab.told_agent != self.agent():
                    tab.state()
            await asyncio.sleep(0.5)


class Tab:
    """One browser tab: its video feed and its input."""

    def __init__(self, mirror, ws):
        self.mirror, self.ws = mirror, ws
        self.synced = False  # has had a keyframe
        self.playing = False  # has shown a frame (the page says so)
        self.told_live = False
        self.told_agent = None
        self.pointer = Pointer(mirror.get_hid, log)
        self.keys = Keys(mirror.get_hid)
        self.queue = asyncio.Queue(maxsize=8)

    def send(self, data):
        # Drop frames for a tab that can't keep up rather than queueing delay;
        # it gets a keyframe to start again from.
        if self.queue.full():
            self.synced = False
            while not self.queue.empty():
                self.queue.get_nowait()
            self.mirror.stream.keyframe()
            return
        self.queue.put_nowait(data)

    def state(self):
        live = self.mirror.live()
        self.told_live = live
        self.told_agent = self.mirror.agent()
        msg = json.dumps({"type": "state", "live": live, "mirroring": self.mirror.mirroring,
                          "agent": self.told_agent, "bluetooth": self.mirror.bluetooth,
                          "awake": self.mirror.awake,
                          "name": NAME, "screen": SCREEN_PT,
                          "bezel": screen.BEZEL, "radius": screen.SCREEN_RADIUS})
        asyncio.ensure_future(self.ws.send_str(msg))

    async def pump(self):
        while True:
            await self.ws.send_bytes(await self.queue.get())

    def handle(self, msg):
        t = msg.get("t")
        if t in ("move", "down", "up", "scroll", "mod", "key", "consumer"):
            self.mirror.last_input_at = time.time()
            if "x" in msg:
                self.mirror.hover_at = (float(msg["x"]), float(msg["y"]))
        if t in ("move", "down", "up", "scroll", "mod", "key", "consumer", "blur") and os.path.exists(INPUT_TRACE + ".on"):
            # Debugging: `touch $XDG_RUNTIME_DIR/phoneuse-input.jsonl.on` logs the page's input.
            with open(INPUT_TRACE, "a") as f:
                f.write(json.dumps({"at": time.time(), **msg}) + "\n")
        if t == "playing":
            self.playing = True
            return
        if t == "keyframe":  # the tab's decoder failed and starts over
            self.synced = False
            self.mirror.stream.keyframe()
            return
        # While an agent drives, the page's mouse and keyboard don't reach the phone
        # (mixing in input would confuse the agent); the user takes over first.
        if t == "blur" or not (self.playing and self.mirror.live()) or self.mirror.agent() == "driving":
            self.pointer.release()
            self.keys.release_all()
            return
        if t == "move":
            self.pointer.hover(msg["x"], msg["y"])
        elif t == "down":
            self.pointer.press(msg["x"], msg["y"])
        elif t == "up":
            self.pointer.release()
        elif t == "scroll":  # fractions of the screen to drag the content by
            self.pointer.hover(msg["x"], msg["y"])
            self.pointer.scroll(msg["dx"] * SCREEN_PT[0], msg["dy"] * SCREEN_PT[1])
        elif t == "mod":
            self.keys.modifier(int(msg["bit"]), bool(msg["down"]))
        elif t == "key":
            self.keys.key(int(msg["usage"]), bool(msg["down"]))
        elif t == "consumer" and msg.get("key") in ("siri", "home", "search", "eject"):
            self.keys.consumer(msg["key"], bool(msg["down"]))

    def close(self):
        try:
            self.pointer.close()
            self.keys.release_all()
        except Exception as e:
            log("closing tab:", e)


AWAKE_FILE = os.path.join(os.path.dirname(config_path()), "keep-awake")  # exists while "keep the screen awake" is on
INPUT_TRACE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "phoneuse-input.jsonl")


def same_origin(req):
    """Requests from the page itself, not from other websites (which could send
    the browser here): their Origin must be this server."""
    return req.headers.get("Origin") in (f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}")


class UnlockRequests:
    """`phoneuse unlock` asks; the owner answers on the page or in a desktop
    notification; only then is the saved passcode typed (unlock.py)."""

    def __init__(self, mirror):
        self.mirror = mirror
        self.pending = None  # {"id", "event", "approved", "notice", "since"}
        self.companion = None

    def broadcast(self, msg):
        for tab in list(self.mirror.tabs):
            asyncio.ensure_future(tab.ws.send_str(json.dumps(msg)))

    async def ask(self, req):
        if self.pending:
            return web.json_response({"result": "another unlock request is waiting for the owner"}, status=409)
        if not await asyncio.get_running_loop().run_in_executor(None, unlock.saved):
            return web.json_response({"result": "no passcode saved: the owner can save one in the browser "
                                                "mirror's settings, or unlock the phone themselves"}, status=409)
        rid = secrets.token_hex(8)
        p = self.pending = {"id": rid, "event": asyncio.Event(), "approved": False, "notice": None,
                            "since": time.time()}
        self.broadcast({"type": "unlock-request", "id": rid})
        if self.companion:  # the companion app's notification, with Unlock and Deny
            asyncio.get_running_loop().run_in_executor(None, self.companion.push_unlock, rid)
        asyncio.ensure_future(self._notify(p))
        log("unlock requested; waiting for the owner")
        try:
            await asyncio.wait_for(p["event"].wait(), 120)
        except asyncio.TimeoutError:
            pass
        finally:
            self.pending = None
            self.broadcast({"type": "unlock-done"})
            if p["notice"] and p["notice"].returncode is None:
                p["notice"].kill()
        if not p["event"].is_set():
            return web.json_response({"result": "no answer from the owner within 2 minutes"})
        if not p["approved"]:
            log("unlock denied")
            return web.json_response({"result": "denied by the owner"})
        result = await asyncio.get_running_loop().run_in_executor(None, self._unlock)
        log("unlock:", result)
        return web.json_response({"result": result})

    @staticmethod
    def _unlock():
        try:
            from . import cli
            return unlock.unlock(cli.screen_text)
        except BaseException as e:  # cli helpers exit on errors; never take the server down
            return f"failed: {e}"

    async def _notify(self, p):
        # A desktop notification with Unlock / Deny, where the notification daemon supports actions.
        # Windows: a notification that opens the page, where the owner answers.
        if IS_WINDOWS:
            from .windows import notify
            await asyncio.get_running_loop().run_in_executor(
                None, notify.show, "Unlock the iPhone?",
                "An agent asks to enter your saved passcode. Answer in the PhoneUse window.",
                f"http://localhost:{PORT}/")
            return
        try:
            p["notice"] = await asyncio.create_subprocess_exec(
                "notify-send", "-a", "phoneuse", "-u", "critical", "--wait", "-A", "unlock=Unlock", "-A", "deny=Deny",
                "Unlock the iPhone?", f"An agent asks to enter your saved passcode. Answer here or at "
                f"http://localhost:{PORT}", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await p["notice"].communicate()
            answer = out.decode().strip()
            if answer in ("unlock", "deny") and self.pending is p:
                p["approved"] = answer == "unlock"
                p["event"].set()
        except (OSError, asyncio.CancelledError):
            pass

    async def answer(self, req):
        if not same_origin(req):
            return web.Response(status=403)
        body = await req.json()
        p = self.pending
        if not p or body.get("id") != p["id"]:
            return web.json_response({"ok": False})
        p["approved"] = bool(body.get("approve"))
        p["event"].set()
        return web.json_response({"ok": True})


ASK_FILE = os.path.join(os.path.dirname(config_path()), "ask-before-session")  # exists: ask before each agent session


def ask_before_session():
    return os.path.exists(ASK_FILE)


def set_ask_before_session(on):
    if on:
        open(ASK_FILE, "w").close()
    elif os.path.exists(ASK_FILE):
        os.remove(ASK_FILE)


class SessionRequests:
    """With "ask before an agent session" on, an agent's first action waits until the owner
    allows the session: in the companion app's notification, a desktop notification or the
    browser mirror. Once allowed, the agent keeps the phone until it hands it back
    (`phoneuse done`), the owner takes over, or it stays idle for ASK_GAP seconds."""

    ASK_GAP = 300.0
    WAIT = 120.0

    def __init__(self, mirror):
        self.mirror = mirror
        self.pending = None  # {"id", "event", "approved", "notice", "since", "action"}
        self.allowed_until = 0.0  # the allowed session lasts while actions keep coming
        self.companion = None

    def needed(self):
        return ask_before_session() and time.time() > self.allowed_until

    def acted(self):
        if ask_before_session():
            self.allowed_until = time.time() + self.ASK_GAP

    def end(self):
        self.allowed_until = 0.0

    def broadcast(self, msg):
        for tab in list(self.mirror.tabs):
            asyncio.ensure_future(tab.ws.send_str(json.dumps(msg)))

    async def ask(self, action):
        """True once the owner allows the session; else the reason it can't start."""
        p = self.pending
        if not p:  # concurrent first actions share one request
            p = self.pending = {"id": secrets.token_hex(8), "event": asyncio.Event(), "approved": False,
                                "notice": None, "since": time.time(), "action": action}
            self.broadcast({"type": "session-request", "id": p["id"], "action": action})
            if self.companion:
                asyncio.get_running_loop().run_in_executor(None, self.companion.push_session, p["id"], action)
            asyncio.ensure_future(self._notify(p))
            log("an agent asks to use the phone; waiting for the owner")
        try:
            await asyncio.wait_for(asyncio.shield(p["event"].wait()), self.WAIT)
        except asyncio.TimeoutError:
            pass
        finally:
            if self.pending is p:
                self.pending = None
                self.broadcast({"type": "session-done"})
                if p["notice"] and p["notice"].returncode is None:
                    p["notice"].kill()
        if not p["event"].is_set():
            return "The owner didn't allow the agent session within 2 minutes. Stop and tell the user."
        if not p["approved"]:
            log("agent session declined")
            return "The owner declined the agent session. Stop; don't retry unless the user asks you to."
        self.allowed_until = time.time() + self.ASK_GAP
        log("agent session allowed")
        return True

    def answer_id(self, rid, approve):
        p = self.pending
        if not p or rid != p["id"]:
            return False
        p["approved"] = bool(approve)
        p["event"].set()
        return True

    async def answer(self, req):
        if not same_origin(req):
            return web.Response(status=403)
        body = await req.json()
        return web.json_response({"ok": self.answer_id(body.get("id"), body.get("approve"))})

    async def setting(self, req):
        # The browser mirror's settings: ask before an agent session, on or off.
        if req.method == "POST":
            if not same_origin(req):
                return web.Response(status=403)
            set_ask_before_session(bool((await req.json()).get("on")))
        return web.json_response({"on": ask_before_session()})

    async def _notify(self, p):
        try:
            p["notice"] = await asyncio.create_subprocess_exec(
                "notify-send", "-a", "phoneuse", "-u", "critical", "--wait", "-A", "allow=Allow", "-A", "deny=Deny",
                "Let an agent use the iPhone?", f"{p['action'] or 'An agent'} wants to start. Answer here or at "
                f"http://localhost:{PORT}", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await p["notice"].communicate()
            choice = out.decode().strip()
            if choice in ("allow", "deny"):
                self.answer_id(p["id"], choice == "allow")
        except OSError:
            pass


def app(mirror):
    unlocks = mirror.unlocks = UnlockRequests(mirror)
    sessions = mirror.sessions = SessionRequests(mirror)

    async def page(_req):
        return web.FileResponse(PAGE, headers={"Cache-Control": "no-cache"})

    async def passcode(req):
        # The page's settings panel: whether a passcode is saved, save one, or remove
        # it. The passcode itself is never sent back.
        loop = asyncio.get_running_loop()
        if req.method != "GET" and not same_origin(req):
            return web.Response(status=403)
        try:
            if req.method == "POST":
                await loop.run_in_executor(None, unlock.save, (await req.json()).get("code", ""))
            elif req.method == "DELETE":
                await loop.run_in_executor(None, unlock.forget)
        except (ValueError, OSError) as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"saved": await loop.run_in_executor(None, unlock.saved)})

    async def awake(req):
        # Keep the screen awake: the page's setting and `phoneuse awake on|off`.
        # Other websites can't switch it (a browser always sends their Origin).
        if req.headers.get("Origin") and not same_origin(req):
            return web.Response(status=403)
        if req.method == "POST":
            mirror.set_awake(bool((await req.json()).get("on")))
        return web.json_response({"on": mirror.awake})

    async def ws(req):
        sock = web.WebSocketResponse(max_msg_size=1 << 16, heartbeat=20)
        await sock.prepare(req)
        tab = Tab(mirror, sock)
        mirror.tabs.add(tab)
        tab.state()
        mirror.stream.keyframe()
        pump = asyncio.ensure_future(tab.pump())
        log("tab joined;", len(mirror.tabs), "open")
        try:
            async for m in sock:
                if m.type == web.WSMsgType.TEXT:
                    try:
                        tab.handle(json.loads(m.data))
                    except Exception as e:
                        input_error("input error:", e)
                        mirror.hid = None
        finally:
            pump.cancel()
            mirror.tabs.discard(tab)
            tab.close()
            log("tab left;", len(mirror.tabs), "open")
        return sock

    async def agent_act(req):
        # The CLI and MCP server call this before every agent action.
        try:
            action = str((await req.json()).get("action", ""))[:80]
        except ValueError:
            action = ""
        if not mirror.taken_over and mirror.sessions.needed():
            allowed = await mirror.sessions.ask(action)
            if allowed is not True:
                return web.json_response({"ok": False, "result": allowed}, status=409)
        if mirror.agent_acts(action):
            mirror.sessions.acted()
            return web.json_response({"ok": True})
        return web.json_response({"ok": False, "result": "The user took over the phone. Stop and wait; "
                                  "continue only when they tell you to (they resume you on the page)."},
                                 status=409)

    async def agent_phase(req):
        # The CLI and MCP server: "reading" while the agent reads the screen, "thinking" once an
        # action returns (its model works out the next step).
        try:
            phase = (await req.json()).get("phase")
        except ValueError:
            phase = None
        if mirror.agent() == "driving" and phase in ("reading", "thinking"):
            mirror.agent_phase = phase
        return web.json_response({"ok": True})

    async def keyboard(req):
        # The page's keyboard button: Eject shows or hides the on-screen keyboard.
        if not same_origin(req):
            return web.Response(status=403)
        try:
            await asyncio.get_running_loop().run_in_executor(None, mirror.get_hid().cmd, "eject")
            return web.json_response({"ok": True})
        except Exception as e:
            mirror.hid = None
            return web.json_response({"ok": False, "error": str(e)}, status=503)

    async def agent_cursor(req):
        # Where the agent points or touches, for the page's glowing dot.
        b = await req.json()
        msg = json.dumps({"type": "agent-cursor", "x": float(b["x"]), "y": float(b["y"]), "down": bool(b.get("down"))})
        for tab in list(mirror.tabs):
            asyncio.ensure_future(tab.ws.send_str(msg))
        return web.json_response({"ok": True})

    async def agent_done(_req):
        # The agent's session is over: control goes back to the page at once.
        mirror.agent_until = 0.0
        mirror.sessions.end()
        mirror.broadcast_state()
        return web.json_response({"ok": True})

    async def agent_takeover(req):
        if not same_origin(req):
            return web.Response(status=403)
        mirror.take_over(bool((await req.json()).get("take")))
        return web.json_response({"agent": mirror.agent()})

    a = web.Application()
    a.router.add_get("/", page)
    a.router.add_get("/ws", ws)
    a.router.add_route("*", "/api/passcode", passcode)
    a.router.add_post("/api/unlock", unlocks.ask)
    a.router.add_post("/api/agent/act", agent_act)
    a.router.add_post("/api/agent/phase", agent_phase)
    a.router.add_post("/api/agent/takeover", agent_takeover)
    a.router.add_post("/api/agent/cursor", agent_cursor)
    a.router.add_post("/api/agent/done", agent_done)
    a.router.add_post("/api/keyboard", keyboard)
    a.router.add_route("*", "/api/awake", awake)
    from .bridge_web import BridgeRoutes
    BridgeRoutes(same_origin).add(a)
    a.router.add_post("/api/unlock/answer", unlocks.answer)
    a.router.add_post("/api/session/answer", sessions.answer)
    a.router.add_route("*", "/api/session/ask", sessions.setting)

    async def companion_pair(req):
        # `phoneuse app pair`: a one-time link that pairs the companion app.
        if req.headers.get("Origin") and not same_origin(req):
            return web.Response(status=403)
        from . import companion
        return web.json_response({"link": await asyncio.get_running_loop().run_in_executor(None, companion.new_pairing)})
    a.router.add_post("/api/companion/pair", companion_pair)
    return a


def main():
    if IS_WINDOWS:
        from .windows import precise_timers
        precise_timers()
    else:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import GLib, Gst
        Gst.init(None)
        # GStreamer's bus signals need a GLib main loop; run one beside asyncio.
        threading.Thread(target=GLib.MainLoop().run, daemon=True).start()
    mirror = Mirror()

    async def start():
        mirror.loop = asyncio.get_running_loop()
        runner = web.AppRunner(app(mirror), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", PORT).start()
        log(f"browser mirror on http://localhost:{PORT}")
        asyncio.ensure_future(mirror.keep_awake())
        try:  # the companion app's API on the local network (companion.py)
            from .companion import Companion
            c = mirror.unlocks.companion = mirror.sessions.companion = Companion(mirror, mirror.unlocks)
            await c.start()
        except OSError as e:
            log("companion API off:", e)
        await mirror.watch()

    try:
        asyncio.run(start())
    except OSError as e:
        log("can't serve:", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
