#!/usr/bin/python
"""phoneuse: see and operate an iPhone from a PC (Linux or Windows).

Setup and running:
  phoneuse install              one-time setup. Linux: Bluetooth HID service (sudo) and
                               bluetoothd config. Windows: UxPlay, the firewall rule
                               (asks for admin once). Both: the local vision models
  phoneuse doctor               check every part of the setup and say how to fix it
  phoneuse up                   mirror window + mouse control (click/drag on the mirror)
  phoneuse down                 Windows: also stop the Bluetooth keyboard and mouse, which
                               keep running after `up` stops (see up_windows)
  phoneuse status               Bluetooth link and mirror window
  phoneuse done                 end the agent session: control goes back to the user
  phoneuse unlock               on the lock screen: ask the owner to approve entering the
                               passcode they saved in the browser mirror's settings
  phoneuse view [--print]       open the browser mirror (http://localhost:PORT) in the
                               default browser; --print only prints its address

Agent commands. Coordinates are fractions (0..1) of the screenshot, or phone
points if any value is > 1 (screen_points in ~/.config/phoneuse/config.toml):
  phoneuse screenshot [PATH]    PNG of the phone screen, once it has stopped changing
  phoneuse tap X Y              tap
  phoneuse longpress X Y [S]    press and hold (default 0.8 s)
  phoneuse swipe X0 Y0 X1 Y1 [S]  drag from one point to another over S seconds
  phoneuse scroll up|down [N]   scroll the content by N half-screens (default 1)
  phoneuse scroll-by DY [X]     scroll exactly DY: a screen fraction, or phone points if above 1;
                                positive shows what is below, negative what is above (X: column, default 0.5)
  phoneuse home | back | switcher | control-center | notifications | dismiss
  phoneuse open APP             open an app by name (search, type, Enter)
  phoneuse type [--append] TEXT  type text (US layout); \\n presses Enter. Replaces what
                               the field holds (Cmd+A first) unless --append
  phoneuse key COMBO            e.g. enter, escape, cmd+h, cmd+space, shift+tab
  phoneuse keyboard             show or hide the iPhone's on-screen keyboard (Eject key)
  phoneuse app pair             pair the companion app (docs/COMPANION.md); --print for the link only
  phoneuse awake [on|off]       keep the screen awake: a tiny pointer nudge every second
                                while nobody uses the phone (also in the page's settings)
  phoneuse cursor [on|off]      whether the pointer works (AssistiveTouch); on/off asks
                                Siri out loud on this PC's speakers to switch it
  phoneuse bridge [status|setup|find|flash|log]
                                the ESP32 Bluetooth bridge: the phone pairs with it,
                                phoneuse drives it over Wi-Fi (setup is over BLE)
  phoneuse calibrate            measure how far the pointer moves on this phone (~10 s;
                                runs by itself the first time a phone's pointer is on)
  phoneuse where                debugging: where the pointer appears (needs Pointer
                               Control colour Orange)

Any action command can end with --text to print the screen as text afterwards
(as `phoneuse text`), e.g. `phoneuse tap 0.3 0.5 --text`.

Reading the screen without images (local models; a few hundred tokens instead
of ~1,300 for a screenshot):
  phoneuse text [--json] [--all] every line of text on screen with its centre (x, y), and
                               on/off switches as [switch on] / [switch off]; --all keeps
                               the status bar and stray icon glyphs
  phoneuse find WHAT            where an element is, from a plain description
                               ("clear search (X) button", "Settings app icon")
  phoneuse tap-on WHAT          find it and tap it; prints where it tapped

iPhone settings this relies on: AssistiveTouch on, Dwell Control off.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

from . import calibrate, config, ipc, phones, screen, touch
from .config import CFG, IS_WINDOWS, NAME, SCREEN_PT, runtime_path
from .hidclient import Hid

PKG = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(PKG)  # the repository checkout
WEB_PORT = int(CFG["web_port"])

PT = SCREEN_PT
LAST_ACTION = runtime_path("phoneuse-last-action")


def uxplay_path():
    """The AirPlay receiver: `uxplay` on the PATH, or on Windows the MSYS2 build
    (scripts/windows/build-uxplay.sh), unless config.toml names one."""
    if CFG.get("uxplay", "auto") != "auto":
        return CFG["uxplay"]
    if IS_WINDOWS:
        for p in (r"C:\msys64\ucrt64\bin\uxplay.exe", shutil.which("uxplay")):
            if p and os.path.exists(p):
                return p
        return None
    return shutil.which("uxplay") or "uxplay"


def device_id():
    """A fixed AirPlay device ID for this PC (a random, locally administered MAC address,
    made once). On Windows UxPlay would take the first adapter's MAC, often a Hyper-V
    or WSL adapter whose address changes, and the iPhone would see a new receiver."""
    from .config import config_dir
    path = os.path.join(config_dir(), "airplay-id")
    try:
        with open(path) as f:
            mac = f.read().strip()
        if re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
            return mac
    except FileNotFoundError:
        pass
    b = bytearray(os.urandom(6))
    b[0] = (b[0] & 0xFC) | 0x02  # unicast, locally administered
    mac = ":".join(f"{x:02x}" for x in b)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(mac)
    return mac


def mirror_args():
    """UxPlay receives AirPlay and passes the phone's H.264 stream on as RTP to the
    video hub (hub.py, avhub.py), which serves the mirror window and the browser mirror.
    The display size offered is the phone's own screen (read afresh: the phone may have
    changed since `phoneuse up` started)."""
    pts, _ = config.screen_points()
    args = [uxplay_path(), "-p", "-n", NAME, "-nh",  # -p: fixed ports; -nh: no "@hostname" after the name
            "-vrtp", f"config-interval=1 pt=96 mtu=60000 ! udpsink host=127.0.0.1 port={screen.HUB_RTP_PORT} sync=false",
            "-vsync", "no", "-s", f"{pts[0] * 3}x{pts[1] * 3}@60", "-fps", "60"]
    if IS_WINDOWS:
        args += ["-m", device_id()]
    # Audio: mirrored audio already plays unsynced (-vsync no); keep the sink's buffer
    # short and ask the phone to plan for 0.1 s instead of 0.25 s.
    if not CFG.get("audio", True):
        return args + ["-a"]
    sink = "wasapisink low-latency=true" if IS_WINDOWS else "pulsesink buffer-time=40000 latency-time=10000"
    return args + ["-as", sink, "-al", "0.1"]


def uxplay_args():
    """mirror_args(), with no audio sink while the companion app has turned the
    mirror's audio off (companion.py: it stops UxPlay, which then starts again)."""
    from .companion import AUDIO_OFF
    args = mirror_args()
    if os.path.exists(AUDIO_OFF) and "-as" in args:
        i = args.index("-as")
        args = args[:i] + ["-as", "0"] + args[i + 2:]
    return args


# UxPlay buffers its output when it goes to a pipe; stdbuf makes it line-buffered.
# (The Windows build turns buffering off itself: scripts/windows/build-uxplay.sh.)
UNBUFFERED = ["stdbuf", "-oL", "-eL"] if not IS_WINDOWS and shutil.which("stdbuf") else []
UXPLAY_LOG = runtime_path("phoneuse-uxplay.log")
MIRRORING_PHONE = runtime_path("mirroring-phone")  # name of the phone that last started mirroring
SENDER = re.compile(r"connection request from (.+) \((.*)\) with deviceID = (\S+)")


# UxPlay lines worth showing in `phoneuse up`'s own output (the rest go to the log).
NOTABLE = re.compile(r"\*\*\* ERROR|Connection closed|connection request from|Initialized server socket|"
                     r"mDNS|DNS-SD|failed", re.I)


def follow_sender(stream, echo=False):
    """Log UxPlay's output and tell the HID daemon which phone is mirroring, so the
    Bluetooth mouse and keyboard go to that phone. With echo, also print the lines
    that matter (errors, connections) to this console."""
    with open(UXPLAY_LOG, "a", encoding="utf-8", errors="replace") as log:
        for line in stream:
            log.write(line)
            log.flush()
            if echo and NOTABLE.search(line) and not SENDER.search(line):
                print(time.strftime("%H:%M:%S"), "uxplay:", line.strip(), flush=True)
            m = SENDER.search(line)
            if not m:
                continue
            print(time.strftime("%H:%M:%S"), f"mirroring: {m[1]} ({m[2]})", flush=True)
            phones.seen(m[1], m[2])
            try:  # read back by a restarted HID service (Windows)
                with open(MIRRORING_PHONE, "w", encoding="utf-8") as f:
                    f.write(m[1])
            except OSError:
                pass
            note_model(m[2])
            try:
                print(time.strftime("%H:%M:%S"), Hid().cmd("mirror " + m[1]), flush=True)
            except Exception as e:
                print(time.strftime("%H:%M:%S"), f"could not tell the HID daemon: {e}", flush=True)
            if phones.needs_calibration(m[1]):
                threading.Thread(target=calibrate_when_ready, args=(m[1],), daemon=True).start()


def calibrate_when_ready(name, wait=180):
    """A phone without a calibration started mirroring: once its Bluetooth link is
    up, it is unlocked and its pointer is on, measure it (about 10 s, hovering
    only). If the pointer is off, `phoneuse cursor on` calibrates after switching it."""
    until = time.time() + wait
    while time.time() < until:
        time.sleep(5)
        if phones.current() != name or not phones.needs_calibration(name):
            return
        try:
            st = Hid().cmd("status")
            from . import unlock
            if "connected=True" not in st or not screen.mirror_live() or unlock.locked(screen_text):
                continue
            h = hid()
            state = cursor_state(h)
            if state == "unknown":
                continue
            if state == "on":
                print(time.strftime("%H:%M:%S"), calibrate.calibrate(h, name, log=lambda *_: None, alive=agent_turn), flush=True)
            agent_done()
            return
        except Exception as e:
            print(time.strftime("%H:%M:%S"), f"calibration skipped: {e}", flush=True)
            return


def note_model(model):
    """Remember the mirroring phone's model: its screen size comes from it
    (config.screen_points). Written only when it changes, because the services that
    plan gestures and scale screenshots restart when this file does."""
    if not model or model == config.mirroring_model():
        return
    try:
        with open(config.MODEL_FILE, "w", encoding="utf-8") as f:
            f.write(model)
    except OSError:
        return
    pts, source = config.screen_points()
    if source == "config.toml":
        msg = f"screen: {pts[0]} x {pts[1]} points, from config.toml"
    elif model in config.MODEL_POINTS:
        msg = f"screen: {pts[0]} x {pts[1]} points ({model})"
    else:
        msg = (f"screen: {model} isn't a model phoneuse knows; assuming {pts[0]} x {pts[1]} points. "
               f"Set screen_points in {config.config_path()} to your iPhone's screen in points.")
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def point(xs, ys):
    """Parse a coordinate pair: fractions, or iPhone points if either is > 1."""
    x, y = float(xs), float(ys)
    if x > 1 or y > 1:
        x, y = x / PT[0], y / PT[1]
    if not (0 <= x <= 1 and 0 <= y <= 1):
        die(f"coordinate out of range: {xs} {ys}")
    return x, y


def need_mirror():
    """Stop unless the mirror shows the phone: acting or reading blind is unsafe."""
    if not screen.mirror_live():
        die("The mirror isn't showing the phone, so nothing was done. Start Screen Mirroring "
            f"to {NAME} on the iPhone (Control Center > Screen Mirroring), then check `phoneuse status`.")


LAST_SEEN = runtime_path("phoneuse-last-seen")
NOTES = []  # remarks for the agent, printed (CLI) or returned (MCP) with the action


ACTION = ""  # what the agent is doing now, in a few words (the companion app's Live Activity)

VERBS = {"open": "Opening", "open_app": "Opening", "tap-on": "Tapping", "tap_on": "Tapping", "tap": "Tapping",
         "longpress": "Long-pressing", "long_press": "Long-pressing", "swipe": "Swiping", "scroll": "Scrolling",
         "scroll_by": "Scrolling", "type": "Typing", "type_text": "Typing", "key": "Pressing",
         "press_key": "Pressing", "back": "Going back", "home": "Going home", "switcher": "Opening the app switcher",
         "control-center": "Opening Control Center", "notifications": "Opening notifications",
         "dismiss": "Closing an overlay", "unlock": "Asking to unlock", "cursor": "Switching the pointer",
         "keyboard": "Showing the keyboard", "toggle_keyboard": "Showing the keyboard", "calibrate": "Calibrating",
         "look": "Looking", "wait": "Waiting", "screenshot": "Looking", "text": "Reading the screen"}


def describe(cmd, what=""):
    """A few words for the Live Activity: "Opening Gmail", "Tapping “Wi-Fi”"."""
    verb = VERBS.get(cmd, cmd.replace("_", " ").capitalize())
    what = str(what or "").strip()
    if cmd in ("open", "open_app") and what:
        return f"{verb} {what}"[:60]
    if cmd in ("tap-on", "tap_on") and what:
        return f"{verb} “{what[:40]}”"
    if cmd in ("key", "press_key") and what:
        return f"{verb} {what}"[:60]
    if cmd == "navigate" and what:
        return VERBS.get(what, f"Going to {what}")
    return verb


def agent_phase(phase):
    """Tell the companion app's Live Activity what the agent is doing between actions:
    "reading" the screen, or "thinking" (its model works out the next step)."""
    import json
    try:
        ipc.http_post(WEB_PORT, "/api/agent/phase", json.dumps({"phase": phase}).encode(), timeout=1)
    except OSError:
        pass


def agent_turn():
    """Before an agent action: tell the browser mirror an agent is driving (it
    glows and locks its own input), and stop if the user has taken over. Also
    notes when the screen changed since the agent last looked without it acting
    (the user may be using the phone, or something is animating)."""
    import atexit, json
    # A CLI command (bin/phoneuse, or bin\phoneuse.exe on Windows): the agent thinks
    # again once it exits.
    if os.path.basename(sys.argv[0]).lower() in ("phoneuse", "phoneuse.exe"):
        atexit.register(agent_phase, "thinking")
    try:
        # up to 2 minutes when the owner asked to approve each agent session (SessionRequests in web.py)
        code, body = ipc.http_post(WEB_PORT, "/api/agent/act", json.dumps({"action": ACTION}).encode(), timeout=130)
        if code == 409:
            die(json.loads(body).get("result", "the user took over the phone; stop and wait"))
    except (OSError, ValueError):
        pass  # no browser mirror running: nothing to tell
    try:
        seen_at = os.path.getmtime(LAST_SEEN)
        acted_at = os.path.getmtime(LAST_ACTION) if os.path.exists(LAST_ACTION) else 0
        # only when the agent looked after its own last action (else its own frames count)
        if time.time() - seen_at < 600 and seen_at >= acted_at:
            seen, now = int(open(LAST_SEEN).read()), frame_seq()
            if now is not None and now > seen + 1:  # one late change is usually the end of an animation
                NOTES.append("note: the screen changed since your last look without an action from you "
                             "(someone may be using the phone, or it is animating); look before relying on it")
    except (OSError, ValueError):
        pass


def hid():
    """The Bluetooth link, for commands that act on the phone."""
    need_mirror()
    agent_turn()
    try:
        h = Hid()
    except OSError:
        die("The Bluetooth HID service isn't running. " + ("It runs with `phoneuse up`." if IS_WINDOWS
                                                            else "Run: phoneuse install"))
    st = h.cmd("status")
    if "connected=True" not in st:
        try:
            h.cmd("connect")
        except RuntimeError as e:
            where = os.environ.get("COMPUTERNAME", "this PC") if IS_WINDOWS else NAME
            die(f"iPhone not connected over Bluetooth ({e}). On the phone: Settings > Bluetooth > {where}.")
    return AgentHid(h)


class DotPoster:
    """Posts the agent's dot position to the browser mirror: the latest one only,
    one post at a time and in order, from one shared thread, so an 8 ms drag
    never waits on the page."""

    def __init__(self):
        self.latest = None
        self.event = threading.Event()
        self.lock = threading.Lock()  # guards latest (quick)
        self.posting = threading.Lock()  # one post at a time
        threading.Thread(target=self._run, daemon=True).start()

    def show(self, dot):
        with self.lock:
            self.latest = dot
        self.event.set()

    def show_now(self, dot):
        # Presses and releases post before returning: the process may end next.
        with self.posting:
            with self.lock:
                self.latest = dot
            self._post(dot)

    def _run(self):
        while True:
            self.event.wait()
            self.event.clear()
            with self.posting:
                with self.lock:
                    dot = self.latest
                self._post(dot)
            time.sleep(1 / 60)

    def _post(self, dot):
        try:
            x, y, down = dot
            body = f'{{"x": {x}, "y": {y}, "down": {str(down).lower()}}}'.encode()
            ipc.http_post(WEB_PORT, "/api/agent/cursor", body, timeout=0.3)
        except (OSError, ValueError, TypeError):
            pass  # the dot is a courtesy; never slow or fail the action


_dots = None


class AgentHid:
    """The Bluetooth link for agent actions: also shows where the agent points on
    the browser mirror (a glowing dot). Absolute pointer commands place the dot;
    relative drag reports move it by the points they move the touch, so it
    follows swipes and scrolls."""

    def __init__(self, h):
        global _dots
        self.h = h
        self.first_seq = None  # picture-change count just before the action's first command
        self.dot = None  # (x, y, down) where the touch or pointer is
        if _dots is None:
            _dots = DotPoster()
        self.dots = _dots

    def cmd(self, line):
        if self.first_seq is None and line != "status":
            # Taken here, after any OCR or grounding lookup the action did first:
            # changes during a lookup must not count as this action's result.
            self.first_seq = frame_seq()
        r = self.h.cmd(line)
        try:
            parts = line.split()
            if parts[0] == "a":
                self.dot = (float(parts[1]), float(parts[2]), parts[3] != "0")
                self.dots.show_now(self.dot)
            elif parts[0] == "m" and self.dot:
                ux, uy = int(parts[1]), int(parts[2])
                mag = (ux * ux + uy * uy) ** 0.5
                if mag:
                    moved = touch.points_for(mag)
                    x, y, down = self.dot
                    self.dot = (min(max(x + moved * ux / mag / SCREEN_PT[0], 0.0), 1.0),
                                min(max(y + moved * uy / mag / SCREEN_PT[1], 0.0), 1.0), down)
                    self.dots.show(self.dot)
        except (IndexError, ValueError):
            pass
        return r


def frame_seq():
    """The count of picture changes (frames service; None without it): taken
    before an action, so a later read can wait for that action's result."""
    try:
        line, _ = screen.frames_request("status", timeout=1)
        return int(line.split("seq=")[1].split()[0])
    except (OSError, IndexError, ValueError):
        return None


def acted(seq_before=None):
    """Note that an action happened, and the frame count from before it."""
    with open(LAST_ACTION, "w") as f:
        f.write("" if seq_before is None else str(seq_before))


def settle():
    """Wait until the phone's screen is still: after a recent action, until its
    first frame has arrived and frames have stopped; otherwise at once. Driven
    by the hub's frame clock, no fixed delays."""
    since = None
    try:
        if time.time() - os.path.getmtime(LAST_ACTION) < 5:
            txt = open(LAST_ACTION).read().strip()
            since = int(txt) if txt else None
    except (FileNotFoundError, ValueError):
        pass
    try:
        # After an action: up to 1.5 s for its first change, then 0.25 s of stillness.
        # Measured over Bluetooth LE (iPhone 14 Pro Max, iOS 26.5): the Home key's first
        # change came up to 0.9 s after it, an app launch from Spotlight later still, and
        # a tap's hover highlight came before its page transition with a short gap, so a
        # 1 s wait and 0.1 s of stillness returned the old screen.
        line, _ = screen.frames_request(f"settle {since} 0.25 1.5" if since is not None else "settle",
                                        timeout=6)
        with open(LAST_SEEN, "w") as f:  # what the agent has now seen (see agent_turn)
            f.write(line.split("seq=")[1].split()[0])
        note = None
        if "still=0" in line:
            note = "note: the screen was still changing when read; look again if it matters"
        elif "ambient=1" in line or float(line.split("ignored=")[1].split()[0]) > 0.05:
            note = "note: parts of the screen keep moving (video or animation); read the rest as settled"
        if note and note not in NOTES:
            NOTES.append(note)
        if since is not None:  # this action's result has been waited for: later reads needn't wait again
            acted(None)
    except (OSError, IndexError):
        time.sleep(0.3)  # no hub (older setup): a short pause instead


def key_combo(h, combo):
    parts = combo.lower().replace("-", "+").split("+")
    mods = {"cmd": "gui", "command": "gui", "ctrl": "ctrl", "control": "ctrl", "alt": "alt",
            "option": "alt", "opt": "alt", "shift": "shift", "gui": "gui"}
    key = parts[-1]
    h.cmd("key " + " ".join([key] + [mods[p] for p in parts[:-1]]))


# ---------- gestures (recipes verified on iOS 26, see RECIPES.md) ----------

def home(h):
    # The Home key (consumer page), not Cmd+H: on the home screen's Today view (the page
    # left of the first) Cmd+H and Cmd+Space did nothing, and the Home key went home
    # (iPhone 14 Pro Max, iOS 26.5). Elsewhere both work.
    h.cmd("consumer home")
    # An app opened from Spotlight goes back to Spotlight, not to the home screen
    # (measured: `open Settings`, then Home showed Spotlight's results), so Home again,
    # a second later: past iOS's double-press window (a double press opens the
    # switcher), it is another single press. On the home screen it does nothing (or
    # goes to the first page).
    time.sleep(1.0)
    h.cmd("consumer home")
    # iOS holds a single Home press back ~0.5 s. A read right after waited for "the
    # first change", and something moving on the old screen counted as one: it
    # returned the app instead of the home screen. Once the animation runs, a read
    # waits for it to end.
    time.sleep(0.6)


def back(h):
    # finger sliding in from the very left edge (touch.press settles the pointer first)
    touch.swipe(h, 0.0, 0.5, 0.8, 0.5, duration=0.35)


def switcher(h):
    touch.swipe(h, 0.5, 1.0, 0.5, 0.62, duration=0.45, hold_end=0.6)


def control_center(h):
    touch.swipe(h, 0.88, 0.0, 0.88, 0.45, duration=0.6)


def notifications(h):
    touch.swipe(h, 0.15, 0.0, 0.15, 0.7, duration=0.6)


def dismiss(h):
    """Close Control Center, Notification Center, the switcher, or go home from an app."""
    touch.swipe(h, 0.5, 1.0, 0.5, 0.15, duration=0.12)


def scroll(h, direction, n=1.0):
    """Scroll the content by n half-screens (0.45 of the screen height each)."""
    scroll_by(h, 0.45 * n if direction == "down" else -0.45 * n)


def scroll_by(h, dy, x=0.5):
    """Scroll exactly dy (screen fraction, or phone points if |dy| > 1): positive
    shows what is below. A drag that slows down before it lifts, so iOS doesn't
    fling and the content stops where the finger lifts; long scrolls take
    several drags."""
    if abs(dy) > 1:
        dy /= SCREEN_PT[1]
    left = abs(dy)
    while left > 0.005:
        d = min(left, 0.7)
        y0, y1 = (0.85, 0.85 - d) if dy > 0 else (0.15, 0.15 + d)
        touch.scroll_drag(h, x, y0, x, y1)
        left -= d


def steplog(*a):
    if os.environ.get("PHONEUSE_STEPLOG"):
        print(f"{time.time():.3f}", *a, file=sys.stderr, flush=True)


def step(h, command, quiet=0.1, change=1.0):
    """Send one HID command and wait until the phone has shown its result (the
    frames service), instead of a fixed pause. `quiet`: how long the picture
    must stay still, longer where results stream in (search)."""
    seq = frame_seq()
    steplog("send", command)
    h.cmd(command)
    if seq is None:
        time.sleep(0.8)  # no frames service (older setup)
        return
    try:
        screen.frames_request(f"settle {seq} {quiet} {change}", timeout=6)
        acted(None)  # settled: reads in the same command needn't wait for a change again
        steplog("settled", command)
    except OSError:
        time.sleep(0.8)


def refuse_if_locked():
    """Stop before typing into the lock screen: typed text and Enter there would
    go to the passcode field, and a submitted wrong passcode counts as a failed
    attempt. Only the owner may unlock the phone."""
    from . import unlock
    if unlock.locked(screen_text):  # the passcode prompt, or the lock screen's clock and date
        die("The phone is locked (passcode screen). Nothing was typed. Run `phoneuse unlock` to "
            "ask the owner to approve an unlock, or ask them to unlock it.")


# Spoken to Siri to switch AssistiveTouch, which the phone's pointer needs (rendered
# with Piper, voice en_US-lessac-medium).
SOUNDS = os.path.join(PKG, "sounds")


def play_sound(wav):
    """Play a WAV file on this PC's speakers and wait until it has played."""
    if IS_WINDOWS:
        import winsound
        winsound.PlaySound(wav, winsound.SND_FILENAME)
    else:
        subprocess.run(["pw-play", wav], check=True, timeout=15)


def cursor_state(h):
    """Is the phone's pointer working (AssistiveTouch on)? Moves the pointer between
    two spots twice (A, B, A, B). With AssistiveTouch on, the pointer is part of
    the mirrored picture: near each spot, the two frames with the pointer there
    match each other and differ from the two with it away, in a pointer-sized blob
    centred on the spot. A moving wallpaper or video changes between visits too,
    so it can't pass for the pointer. Returns "on", "off", or "unknown" when the
    screen keeps changing by itself near the spots."""
    import numpy as np
    spots = [(0.30, 0.42), (0.70, 0.58)]
    # Wake the pointer first: after a while unused it hides, and it fades back in on the
    # next move, so the first picture would catch it half-drawn (measured: "off" once,
    # then "on" twice, with AssistiveTouch on).
    h.cmd(f"a {spots[1][0]} {spots[1][1]} 0")
    time.sleep(0.5)
    shots = []
    for x, y in spots + spots:
        h.cmd(f"a {x} {y} 0")
        time.sleep(0.4)
        shots.append(screen.grab_rgb().astype(np.int16))
    if any(s.shape != shots[0].shape for s in shots):
        return "unknown"
    H, W, _ = shots[0].shape
    r = int(W * 0.07)

    def near(img, spot):
        cx, cy = int(spot[0] * W), int(spot[1] * H)
        return img[max(cy - r, 0):cy + r, max(cx - r, 0):cx + r]

    def diff(a, b):
        return np.abs(a - b).max(axis=2) > 40

    seen = []
    for i, spot in enumerate(spots):
        here = [near(shots[k], spot) for k in (i, i + 2)]  # frames with the pointer at this spot
        away = [near(shots[k], spot) for k in (1 - i, 3 - i)]
        if diff(here[0], here[1]).mean() > 0.005 or diff(away[0], away[1]).mean() > 0.005:
            return "unknown"  # something else moves here
        moved = diff(here[0], away[0]) & diff(here[1], away[1])
        ys, xs = np.nonzero(moved)
        # the pointer: a blob of some size, centred on the spot (the crop's middle)
        seen.append(moved.mean() > 0.02 and abs(xs.mean() - moved.shape[1] / 2) < r * 0.4
                    and abs(ys.mean() - moved.shape[0] / 2) < r * 0.4)
    return "on" if all(seen) else "off"


def siri_open(before, wait=2.5):
    """Wait until Siri is listening: its orb appears at the bottom centre about
    1-1.4 s after the Siri key goes down. True once the picture there changed."""
    import numpy as np
    H, W = before.shape[:2]
    box = (slice(int(H * 0.85), H), slice(int(W * 0.3), int(W * 0.7)))
    until = time.time() + wait
    while time.time() < until:
        time.sleep(0.2)
        now = screen.grab_rgb().astype(np.int16)
        if now.shape == before.shape and (np.abs(now[box] - before[box]).max(axis=2) > 40).mean() > 0.08:
            return True
    return False


def cursor_set(h, on):
    """Turn AssistiveTouch on or off by asking Siri out loud: hold the Siri key,
    wait until Siri shows it is listening, play the request on this PC's speakers
    (the phone must hear them), let go. Asks a second time if the first request
    didn't take."""
    import numpy as np
    refuse_if_locked()
    want = "on" if on else "off"
    state = cursor_state(h)
    if state == want:
        return f"cursor {state} (already)"
    wav = os.path.join(SOUNDS, f"assistivetouch-{want}.wav")
    heard = False
    for _ in range(2):
        before = screen.grab_rgb().astype(np.int16)
        h.cmd("consumer siri down")
        try:
            if not siri_open(before):
                continue  # let go (finally) and hold again
            heard = True
            play_sound(wav)
            time.sleep(0.3)
        finally:
            h.cmd("consumer siri up")
        # Siri switches it, answers, and closes; its animation reads as "unknown" meanwhile.
        state, until = "unknown", time.time() + 10
        while time.time() < until:
            time.sleep(1.0)
            state = cursor_state(h)
            if state == want:
                if on and phones.needs_calibration():  # first time on: measure it
                    return f"cursor {state}; " + calibrate.calibrate(h, alive=agent_turn)
                return f"cursor {state}"
    if not heard:
        return (f"cursor {state} (wanted {want}): Siri didn't open when the Siri key was held. "
                "Check Settings → Siri: Press Side Button for Siri on, and Siri not restricted.")
    return (f"cursor {state} (wanted {want}): Siri opened but didn't switch it. Check that the phone "
            "is near this PC's speakers and their volume is up, then try again.")


def awake_cmd(arg):
    """Keep the screen awake (or not): the browser mirror's server nudges the pointer
    about once a second while nobody uses the phone, so Auto-Lock never fires."""
    if arg not in (None, "on", "off"):
        die("usage: phoneuse awake [on|off]")
    try:
        if arg is None:
            _, body = ipc.http_get(WEB_PORT, "/api/awake", timeout=5)
        else:
            _, body = ipc.http_post(WEB_PORT, "/api/awake", json.dumps({"on": arg == "on"}).encode(), timeout=5)
        on = json.loads(body)["on"]
    except (OSError, ValueError, KeyError) as e:
        die(f"The browser mirror's server isn't running ({e}); start `phoneuse up`.")
    return "keep awake " + ("on" if on else "off")


def open_app(h, name):
    # Home first (often already there; also wakes a locked phone). iOS holds a single
    # Home press back ~0.5 s (a double press opens the switcher), and Cmd+Space sent
    # while an app is still closing is ignored. Waiting for the first change wasn't
    # enough: a page that was still updating (iPhone Storage) counted as that change,
    # Cmd+Space went out mid-animation, and the name was typed into nothing. So: wait
    # out the hold-back, then until the picture has been still for 0.3 s.
    t_home = time.time()
    h.cmd("consumer home")
    time.sleep(0.7)
    try:
        screen.frames_request("settle - 0.3 0", timeout=6)
    except OSError:
        time.sleep(0.6)
    refuse_if_locked()
    if big_change_since(t_home):
        # An app was closed. The first Spotlight opened after that doesn't take typed
        # keys (measured: 12 of 12 times the name was lost; the next one always took
        # it), so open it once and close it before the real one.
        h.cmd("key space gui")
        time.sleep(1.0)
        h.cmd("key escape")
        time.sleep(0.8)
    # Measured over Bluetooth LE (iPhone 14 Pro Max, iOS 26.5): Cmd+Space didn't open
    # Spotlight about one time in four (as one report or Cmd first, after a Home press
    # or not); with the name typed and the results shown, Enter once left the home
    # screen. So every step is checked on the screen: Spotlight open (else Escape and
    # Cmd+Space again), the app among the results (else type it again), and the app
    # itself is tapped. Without the screen-reading models: the old blind sequence.
    hit = None
    for attempt in range(3):
        if not open_spotlight(h):
            continue
        # Spotlight may keep the last search in its field: select it so the name
        # replaces it. (Appended, "Settings" became "SettingsSettings", no app
        # matched, and Enter opened Google Maps.)
        h.cmd("key a gui")
        step(h, "type " + name, quiet=0.35)  # search results arrive in waves
        hit = spotlight_hit(name)
        if hit != "missing":
            break
        h.cmd("key escape")
        time.sleep(0.8)
    if isinstance(hit, tuple):
        t_tap = time.time()
        touch.tap(h, *hit)
        # The launch changes the whole picture; the pointer's hover highlight on the
        # result came first and, taken for the launch, ended the wait early.
        while time.time() - t_tap < 2.5 and not big_change_since(t_tap, frac=0.3):
            time.sleep(0.1)
        try:
            screen.frames_request("settle - 0.3 0", timeout=6)
        except OSError:
            time.sleep(1.0)
    elif hit == "missing":
        # Never Enter here: the field may still hold the last search, and Enter then
        # opened that app instead (TestFlight asked, Settings opened).
        h.cmd("key escape")
        die(f"'{name}' wasn't among Spotlight's results twice, so nothing was opened. Check the "
            "app's name as it appears under its icon, or look at the screen.")
    else:  # the screen can't be read (no models): Enter takes Spotlight's top hit
        step(h, "key enter", quiet=0.3)


SPOTLIGHT_MARKS = ("top hit", "suggestions", "siri suggestions", "show more", "show less")


def big_change_since(t0, frac=0.2):
    """Did the picture change over `frac` of its area since time t0 (frames service
    history)? False when it can't tell."""
    import json
    try:
        line, _ = screen.frames_request(f"history {t0}", timeout=2)
        return any(f >= frac for _, f, _ in json.loads(line))
    except (OSError, ValueError):
        return False


def open_spotlight(h, tries=3):
    """Cmd+Space until Spotlight is open: 1.6 s later the picture must differ from the
    one before over most of its area (Spotlight blurs the screen behind its results).
    Cmd+Space toggles Spotlight, so it is pressed again only when it isn't open."""
    import numpy as np

    def small():
        img = screen.grab_rgb()
        return img[::16, ::16].mean(axis=2)

    for _ in range(tries):
        try:
            before = small()
        except RuntimeError:  # no picture: press and hope
            h.cmd("key space gui")
            time.sleep(1.2)
            return True
        h.cmd("key space gui")
        time.sleep(1.0)  # it opens 0.2-0.5 s after the key, then animates
        after = small()
        if before.shape == after.shape and float((np.abs(after - before) > 25).mean()) > 0.3:
            try:  # let it finish opening; its field takes keys only a moment after
                screen.frames_request("settle - 0.2 0", timeout=6)
            except OSError:
                pass
            time.sleep(0.3)
            return True
    return False


def _plain(text):
    return "".join(c for c in text.casefold() if c.isalnum())


def spotlight_hit(name):
    """Where Spotlight lists the app `name`, "missing" when it isn't among the results,
    None when the screen can't be read (no models). The topmost line that is exactly
    the name: the top hit's label, or the app in the Apps section when another app is
    the top hit (typing "Gmail" puts Mail first). Never a near match (Gmail -> Mail).
    A label cut short ("Google Ma…") counts when it starts the name."""
    try:
        lines = screen_text()
    except SystemExit:
        return None
    want = _plain(name)
    results = [(x, y, t) for x, y, t in lines if 0.06 < y < 0.88]  # not the search field at the bottom
    for x, y, t in results:
        if _plain(t) == want:
            return x, y
    for x, y, t in results:
        cut = t.rstrip()
        if (cut.endswith("…") or cut.endswith("...")) and len(_plain(cut)) >= 4 and want.startswith(_plain(cut)):
            return x, y
    return "missing"


def type_text(h, text):
    refuse_if_locked()
    for i, chunk in enumerate(text.replace("\\n", "\n").split("\n")):
        if i:
            h.cmd("key enter")
        if chunk:
            h.cmd("type " + chunk)


# ---------- screen ----------

LOOK = runtime_path("phoneuse-look.npy")


def _small(rgb):
    """Grey, a quarter of the size: enough to tell whether an area moved."""
    import numpy as np
    return np.asarray(rgb, dtype=np.float32)[::4, ::4].mean(axis=2)


def save_look(rgb):
    """Remember what the agent was just shown (a screenshot, a text read, or the
    frame an MCP action returned): taps by position are checked against it."""
    import numpy as np
    try:
        np.save(LOOK, _small(rgb))
    except OSError:
        pass


def moved_since_look(fx, fy):
    """Has the area around (fx, fy) changed since the agent's last look? Taps go
    by position, so a tap computed from an old picture of a feed that has since
    scrolled lands on something else. Returns a reason, or None."""
    import numpy as np
    try:
        if time.time() - os.path.getmtime(LOOK) > 300:
            return None  # no recent look: the agent isn't working from a picture
        then = np.load(LOOK)
        settle()
        now = _small(screen.grab_rgb())
    except (OSError, ValueError, RuntimeError):
        return None
    if now.shape != then.shape:
        return None
    h, w = now.shape
    x0, x1 = int(max(0, fx - 0.08) * w), int(min(1, fx + 0.08) * w)
    y0, y1 = int(max(0, fy - 0.04) * h), int(min(1, fy + 0.04) * h)
    a, b = then[y0:y1, x0:x1], now[y0:y1, x0:x1]
    # Compare content, not background: text on a plain page is a few percent of
    # the pixels, so measure the changed pixels against the content ones.
    ink = (np.abs(a - np.median(a)) > 24) | (np.abs(b - np.median(b)) > 24)
    if ink.sum() < 30:
        return None  # an empty area: nothing there to miss
    changed = float(((np.abs(b - a) > 24) & ink).sum() / ink.sum())
    if changed > 0.4:  # the pointer alone is far smaller than this
        return (f"the screen around ({fx:.2f}, {fy:.2f}) changed since your last look ({changed:.0%} of it): "
                "the content moved, so this position may hit something else. Look again (screenshot or "
                "--text), then tap. Add --force to tap anyway (e.g. a fixed tab bar after a page change).")
    return None


def screenshot(path):
    need_mirror()
    settle()
    try:
        png = screen.grab_png()
    except RuntimeError:
        die(f"No mirror window. Run `phoneuse up` and start Screen Mirroring to '{NAME}' on the iPhone.")
    with open(path, "wb") as f:
        f.write(png)
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(png))
    save_look(img.convert("RGB"))
    w, hgt = img.size
    print(f"{path} ({w}x{hgt} px = {PT[0]}x{PT[1]} pt; tap with fractions of this image or points)")


# ---------- vision (OCR + grounding, see vision.py) ----------

VISION_PY = os.path.join(ROOT, ".venv-vision", "Scripts", "python.exe") if IS_WINDOWS else \
    os.path.join(ROOT, ".venv-vision", "bin", "python")
VISION_LOG = runtime_path("phoneuse-vision.log")


def vision(req, timeout=90):
    import json

    def ask():
        with ipc.connect("vision", timeout=timeout) as s:
            s.sendall((json.dumps(req) + "\n").encode())
            return json.loads(s.makefile("rb").readline())

    try:
        return ask()
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    if not os.path.exists(VISION_PY):
        die("Vision models aren't installed. Run: " + (r"scripts\windows\setup-vision.ps1" if IS_WINDOWS
                                                       else f"{ROOT}/scripts/setup-vision.sh"))
    logf = open(VISION_LOG, "a")
    if IS_WINDOWS:  # detached, no console window; it outlives this command and keeps the models loaded
        subprocess.Popen([VISION_PY, "-m", "phoneuse.vision"], cwd=ROOT, stdout=logf, stderr=logf,
                         stdin=subprocess.DEVNULL, creationflags=0x08000000 | 0x00000200)
    else:
        subprocess.Popen([VISION_PY, "-m", "phoneuse.vision"], cwd=ROOT, stdout=logf, stderr=logf, start_new_session=True)
    for _ in range(240):
        time.sleep(0.5)
        try:
            return ask()
        except (FileNotFoundError, ConnectionRefusedError):
            continue
    die(f"vision service didn't start; see {VISION_LOG}")


def fresh_screenshot():
    """A screenshot for reading, once the screen has settled (see settle)."""
    path = runtime_path(f"phoneuse-shot-{os.getpid()}.png")
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):
        screenshot(path)
    return path


STATUS_BAR = 0.055  # lines above this (time, signal, battery) are left out of `text`
BACK_GLYPHS = {"く", "<", "‹", "〈", "〈"}  # how OCR reads the back chevron at the top left


def screen_text(show_all=False, shot=None):
    """The screen as text lines [(x, y, text)]: OCR plus the state of on/off
    switches, which OCR can't read. Drops the status bar and icon glyphs OCR
    mistakes for text (lines without a letter or digit) unless show_all. Takes a
    settled screenshot unless given one (`shot`, a PNG path, which it leaves)."""
    import numpy as np
    from PIL import Image
    own = shot is None
    shot = shot or fresh_screenshot()
    r = vision({"op": "text", "image": shot})
    rgb = np.asarray(Image.open(shot).convert("RGB"))
    if own:
        os.unlink(shot)
    if "error" in r:
        die(r["error"])
    lines = [(ln["x"], ln["y"], "[back]" if ln["text"].strip() in BACK_GLYPHS and ln["x"] < 0.15 else ln["text"])
             for ln in r["lines"]
             if show_all or (ln["y"] >= STATUS_BAR and any(c.isalnum() for c in ln["text"]))]
    lines += [(x, y, "[switch on]" if on else "[switch off]") for x, y, on in screen.find_switches(rgb)]
    return sorted(lines, key=lambda t: (round(t[1] / 0.012), t[0]))  # rows top to bottom, then left to right


def text_cmd(args):
    import json
    lines = screen_text("--all" in args)
    if "--json" in args:
        print(json.dumps([{"x": x, "y": y, "text": t} for x, y, t in lines]))
        return
    for x, y, t in lines:
        print(f"{x:.3f} {y:.3f}  {t}")
    if NOTES:
        print("\n".join(NOTES))


FILLER = {"row", "button", "icon", "link", "tab", "cell", "item", "the", "label", "option"}
# Descriptions of things that aren't text; only these fall back to the grounding model.
VISUAL = {"icon", "button", "image", "picture", "logo", "symbol", "glyph", "arrow", "switch",
          "toggle", "avatar", "photo", "thumbnail", "chevron", "x", "plus", "+", "gear", "badge"}


def text_target(what):
    """Position of an on-screen text line matching `what` (ignoring words like
    "row" or "button"), or None. Exact, unlike the grounding model, which always
    answers even when the element isn't on screen."""
    words = [w for w in what.lower().replace('"', "").split() if w not in FILLER]
    if not words:
        return None
    want = " ".join(words)
    lines = [(x, y, t.lower().strip()) for x, y, t in screen_text() if y >= STATUS_BAR]
    exact = [(x, y) for x, y, t in lines if t == want]
    if exact:
        return exact[0]
    starts = [(x, y) for x, y, t in lines if t.startswith(want)]
    if len(starts) == 1:
        return starts[0]
    # OCR misreads a character now and then ("Message" for "iMessage" when the
    # text cursor covers the i): take the single closest line if it is close.
    import difflib
    scored = sorted(((difflib.SequenceMatcher(None, want, t).ratio(), x, y) for x, y, t in lines), reverse=True)
    if scored and scored[0][0] >= 0.85 and (len(scored) == 1 or scored[1][0] < scored[0][0] - 0.1):
        return scored[0][1], scored[0][2]
    return None


def tap_on(h, what):
    """Tap an element by description: on-screen text exactly (OCR), else, for
    visual descriptions only, the grounding model. Returns what it did."""
    hit = text_target(what)
    if hit:
        x, y = hit
        how = "text"
    elif not VISUAL.intersection(what.lower().replace('"', "").split()):
        die(f"'{what}' isn't on screen as text, so nothing was tapped. Scroll to it, or describe "
            "something visual (\"gear icon\", \"back button\") to use the grounding model.")
    else:
        x, y = find(what)
        how = "grounding (check the result)"
        # find always answers, even when the element isn't there; it then tends to
        # point at the status bar. Never tap there: the Dynamic Island in its middle
        # opens the Stop Mirroring popup.
        if y < 0.06:
            die(f"refusing to tap the status bar at {x:.3f} {y:.3f}: '{what}' is probably not on screen")
    touch.tap(h, x, y)
    return f"tapped {x:.3f} {y:.3f} by {how}"


def find(what):
    shot = fresh_screenshot()
    r = vision({"op": "find", "image": shot, "what": what})
    os.unlink(shot)
    if "error" in r:
        die(f"not found: {what}")
    return r["x"], r["y"]


# ---------- up / install / status ----------

def up():
    return up_windows() if IS_WINDOWS else up_linux()


HID_LOG = runtime_path("phoneuse-hid.log")
HID_SOURCES = ("windows/hid.py", "hidproto.py", "ipc.py")


def hid_status():
    """The HID service's status line, or None when it isn't running."""
    try:
        return ipc.request("hid", "status", timeout=3)[0]
    except OSError:
        return None


# Started by a short-lived middle process, so the HID service has no living parent:
# stopping `phoneuse up` with its process tree (taskkill /T, as tools do) can't reach it.
_SPAWN = ("import subprocess, sys\n"
          "log = open(sys.argv[1], 'a', encoding='utf-8')\n"
          "subprocess.Popen(sys.argv[3:], stdout=log, stderr=log, stdin=subprocess.DEVNULL,"
          " creationflags=int(sys.argv[2]))\n")


def start_hid():
    """Windows: start the Bluetooth HID service on its own, detached and outside any
    job, so it outlives `phoneuse up`. A copy that finds one already running exits
    without touching Bluetooth (windows/hid.py)."""
    flags = 0x08000000 | 0x00000200  # no window, own process group (Ctrl+C in this console doesn't reach it)
    # Out of the job this command may itself be in (a terminal's or a tool's that kills
    # its processes when it closes), when that job allows it.
    for extra in (0x01000000, 0):  # CREATE_BREAKAWAY_FROM_JOB
        try:
            subprocess.Popen([sys.executable, "-c", _SPAWN, HID_LOG, str(flags | extra),
                              sys.executable, "-u", "-m", "phoneuse.windows.hid"],
                             cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env=dict(os.environ, PYTHONUTF8="1"), creationflags=flags | extra).wait(15)
            return
        except OSError:
            if not extra:
                raise


def down():
    """Stop the Bluetooth keyboard and mouse (Windows; on Linux the systemd service
    phoneuse-hid owns them). The phone drops it until `phoneuse up` starts it again;
    an iPhone may then need pairing again (see up_windows)."""
    if not IS_WINDOWS:
        die("On Linux the Bluetooth HID service is the systemd service phoneuse-hid: "
            "sudo systemctl stop phoneuse-hid")
    if hid_status() is None:
        print("The Bluetooth HID service isn't running.")
        return
    print(ipc.request("hid", "shutdown", timeout=3)[0])
    for _ in range(20):
        time.sleep(0.25)
        if hid_status() is None:
            print("Stopped. phoneuse up starts it again.")
            return
    die("The HID service didn't stop.")


def up_linux():
    if subprocess.run(["systemctl", "is-active", "--quiet", "phoneuse-hid"]).returncode != 0:
        die("Bluetooth HID service isn't running. Run: phoneuse install")
    print(f"Starting the mirror (AirPlay receiver '{NAME}'). On the iPhone: Control Center > Screen Mirroring > {NAME}.")
    stop = threading.Event()
    procs = {}

    def supervise(module, label, watch):
        """Run phoneuse.<module>; start it again when it exits or when one of the
        `watch` files changes. (The page itself is served fresh on every load.)"""
        files = [os.path.join(PKG, f) for f in watch]
        mtimes = lambda: [os.path.getmtime(f) for f in files if os.path.exists(f)]
        while not stop.is_set():
            seen = mtimes()
            procs[module] = subprocess.Popen([sys.executable, "-m", f"phoneuse.{module}"], cwd=ROOT)
            while procs[module].poll() is None and not stop.is_set():
                time.sleep(1)
                if mtimes() != seen:
                    print(time.strftime("%H:%M:%S"), f"{label} code changed; restarting it", flush=True)
                    procs[module].terminate()
                    procs[module].wait(5)
                    break
            else:
                if stop.is_set():
                    break
                print(time.strftime("%H:%M:%S"), f"{label} exited ({procs[module].returncode}); starting it again.", flush=True)
                time.sleep(1)

    def start_vision():
        # The screen-reading models (OCR and grounding) load as soon as phoneuse
        # starts, not on an agent's first look. Skipped if they aren't installed.
        if os.path.exists(os.path.join(ROOT, ".venv-vision", "bin", "python")):
            try:
                import contextlib, io
                with contextlib.redirect_stderr(io.StringIO()):
                    vision({"op": "ping"}, timeout=120)
            except SystemExit:
                pass
    threading.Thread(target=start_vision, daemon=True).start()

    def keep_hub():
        # The video hub (hub.py): decodes the phone's stream and serves it to the
        # window and the browser mirror. Started first; rarely restarts.
        while not stop.is_set():
            procs["hub"] = subprocess.Popen([sys.executable, "-m", "phoneuse.hub"], cwd=ROOT)
            rc = procs["hub"].wait()
            if stop.is_set():
                break
            print(time.strftime("%H:%M:%S"), f"video hub exited ({rc}); starting it again. "
                  "Reconnect Screen Mirroring on the iPhone.", flush=True)
            time.sleep(1)
    threading.Thread(target=keep_hub, daemon=True).start()

    def keep_frames():
        # The frames service (frames.py): settle and screenshots for agents. It
        # joins the hub as a viewer, so it restarts freely, also on code changes.
        supervise("frames", "frames service", ["frames.py", "screen.py", config.MODEL_FILE])
    threading.Thread(target=keep_frames, daemon=True).start()

    def keep_window():
        # The mirror window (window.py) outlives UxPlay restarts and phone
        # reconnects; it waits for the next stream by itself.
        while not stop.is_set():
            procs["window"] = subprocess.Popen([sys.executable, "-m", "phoneuse.window"], cwd=ROOT)
            rc = procs["window"].wait()
            if stop.is_set():
                break
            print(time.strftime("%H:%M:%S"), f"mirror window exited ({rc}); opening it again.", flush=True)
            time.sleep(1)
    threading.Thread(target=keep_window, daemon=True).start()

    def keep_web():
        # The browser mirror (web.py), on localhost only. Restarts when its code
        # changes: it rejoins the hub, so the phone keeps mirroring.
        supervise("web", "browser mirror", ["web.py", "pointer.py", "touch.py", "phones.py", "unlock.py", "screen.py",
                                            "companion.py", "apns.py", "bridge.py", "bridge_web.py", config.MODEL_FILE])
    threading.Thread(target=keep_web, daemon=True).start()

    def keep_mirror():
        # If UxPlay dies, start it again so the phone can reconnect without
        # restarting `phoneuse up`. The companion app turns the audio off or on by
        # changing the audio-off file and stopping UxPlay (companion.py).
        from .companion import UXPLAY_PID
        while not stop.is_set():
            args = uxplay_args()
            env = dict(os.environ)
            out = subprocess.DEVNULL
            if os.environ.get("PHONEUSE_TRACE"):  # per-element GStreamer latency, for tuning
                env.update(GST_TRACERS="latency(flags=pipeline+element)", GST_DEBUG="GST_TRACER:7")
                out = open(runtime_path("phoneuse-gst-trace.log"), "w")
            if out is subprocess.DEVNULL:  # read UxPlay's output for the sender's name
                procs["mirror"] = subprocess.Popen(UNBUFFERED + args, stdout=subprocess.PIPE,
                                                   stderr=subprocess.STDOUT, env=env, text=True, errors="replace")
                threading.Thread(target=follow_sender, args=(procs["mirror"].stdout,), daemon=True).start()
            else:
                procs["mirror"] = subprocess.Popen(args, stdout=out, stderr=out, env=env)
            with open(UXPLAY_PID, "w") as f:
                f.write(str(procs["mirror"].pid))
            rc = procs["mirror"].wait()
            if stop.is_set():
                break
            print(time.strftime("%H:%M:%S"), f"mirror exited ({rc}); restarting it. Reconnect Screen Mirroring on the iPhone.", flush=True)
            time.sleep(1)
    threading.Thread(target=keep_mirror, daemon=True).start()
    try:
        h = Hid()
        from . import bridge, bridge_web
        if bridge.saved() and bridge.saved().get("use", True):  # input through the ESP32 bridge
            found = bridge.resolve()
            print("Bridge:", bridge_web.use_bridge(found).replace("ok ", "") if found
                  else "set up, but not answering on this network", flush=True)
        print("Bluetooth:", h.cmd("status").replace("ok ", ""))
        print("Mouse control on: hover, click and drag on the mirror window. Ctrl+C stops everything.")
        print(f"Browser mirror: http://localhost:{CFG['web_port']}")
        # The clicker runs as its own process and restarts by itself when its code
        # changes, so edits don't drop the AirPlay connection.
        watched = [os.path.join(PKG, f) for f in ("mirror_input.py", "pointer.py", "touch.py", "screen.py",
                                                  "hidclient.py", config.MODEL_FILE)]
        mtimes = lambda: [os.path.getmtime(f) for f in watched if os.path.exists(f)]
        while not stop.is_set():
            seen = mtimes()
            procs["clicker"] = subprocess.Popen([sys.executable, "-m", "phoneuse.mirror_input"], cwd=ROOT)
            while procs["clicker"].poll() is None:
                time.sleep(1)
                if mtimes() != seen:
                    print(time.strftime("%H:%M:%S"), "clicker code changed; reloading it (mirror stays up)", flush=True)
                    procs["clicker"].terminate()
                    procs["clicker"].wait(3)
                    break
            else:
                print(time.strftime("%H:%M:%S"), "clicker exited; restarting it", flush=True)
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if procs.get("clicker") and procs["clicker"].poll() is None:
            procs["clicker"].terminate()
        for name in ("mirror", "window", "web", "frames", "hub"):
            p = procs.get(name)
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(3)
                except subprocess.TimeoutExpired:
                    p.kill()


def up_windows():
    """Windows: the video hub, the frames service, the browser mirror and UxPlay, each
    restarted when it exits (frames and the browser mirror also when their code
    changes); the mirror opens in a window of its own. They are in a job object, so all
    of them stop with this command. The Bluetooth HID service (Bluetooth LE) is
    started if it isn't running and keeps running after this command stops
    (keep_hid; `phoneuse down` stops it)."""
    from .windows import appwindow
    from .windows import procs as wprocs
    if not wprocs.single_instance("phoneuse-up"):
        die("phoneuse up is already running on this PC (it owns the AirPlay and Bluetooth services).")
    ux = uxplay_path()
    if not ux:
        die("UxPlay (the AirPlay receiver) isn't installed. Run scripts\\windows\\install.ps1, "
            "or `phoneuse doctor` to see what is missing.")
    job = wprocs.Job()
    stop = threading.Event()
    procs = {}

    def say(*a):
        print(time.strftime("%H:%M:%S"), *a, flush=True)

    def keep(name, label, start, watch=()):
        """Run a service and start it again when it exits, backing off when it keeps
        failing; with `watch`, also when one of those source files changes."""
        files = [os.path.join(PKG, f) for f in watch]
        mtimes = lambda: [os.path.getmtime(f) for f in files if os.path.exists(f)]
        fails = 0
        while not stop.is_set():
            seen, started = mtimes(), time.time()
            try:
                p = procs[name] = start()
            except OSError as e:
                say(f"{label} can't start: {e}")
                stop.wait(10)
                continue
            while p.poll() is None and not stop.is_set():
                stop.wait(1)
                if files and mtimes() != seen:
                    say(f"{label} code changed; restarting it")
                    p.terminate()
                    p.wait(5)
                    break
            else:
                if stop.is_set():
                    break
                fails = fails + 1 if time.time() - started < 15 else 0
                delay = min(30, 2 ** min(fails, 5))
                say(f"{label} exited ({p.returncode}); starting it again in {delay} s")
                stop.wait(delay)

    def module(name):
        env = dict(os.environ, PYTHONUTF8="1")  # logs may hold any text (phone names, OCR)
        return lambda: job.popen([sys.executable, "-u", "-m", f"phoneuse.{name}"], cwd=ROOT,
                                 stdin=subprocess.DEVNULL, env=env)

    def uxplay():
        env = dict(os.environ)
        env["PATH"] = os.path.dirname(ux) + os.pathsep + env.get("PATH", "")  # its DLLs and GStreamer
        p = job.popen(uxplay_args(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                      env=env, text=True, encoding="utf-8", errors="replace")
        threading.Thread(target=follow_sender, args=(p.stdout,), kwargs={"echo": True}, daemon=True).start()
        from .companion import UXPLAY_PID
        try:  # the companion app's audio switch stops UxPlay by this pid
            with open(UXPLAY_PID, "w") as f:
                f.write(str(p.pid))
        except OSError:
            pass
        return p

    # The Bluetooth HID service isn't one of this command's services: it runs on its own
    # and outlives it (keep_hid). Each start publishes its GATT services anew, and an
    # iPhone that was connected while they disappeared and came back can keep using a
    # stale copy of them and stop using the keyboard and mouse until it is paired again
    # (measured on an iPhone 14 Pro Max; Bluetooth off/on on either side didn't help).
    # The hub stays up while you edit code: a hub restart needs Screen Mirroring started
    # again. The frames service and the browser mirror restart freely, also when a
    # phone of another model starts mirroring (config.MODEL_FILE: its screen size).
    services = [
        ("hub", "video hub", module("avhub"), ()),
        ("frames", "frames service", module("frames"), ("frames.py", "screen.py", "ipc.py", config.MODEL_FILE)),
        ("web", "browser mirror", module("web"),
         ("web.py", "pointer.py", "touch.py", "phones.py", "unlock.py", "screen.py", "companion.py", "apns.py",
          "bridge.py", "bridge_web.py", config.MODEL_FILE)),
    ]
    say(f"Starting phoneuse. AirPlay receiver: {NAME}; Bluetooth keyboard and mouse: "
        f"{os.environ.get('COMPUTERNAME', 'this PC')}. Ctrl+C stops it; the Bluetooth keyboard "
        "and mouse keep running (phoneuse down stops them).")

    def follow_hid_log():
        # The HID service's output, in this console like the other services'.
        with open(HID_LOG, "a+", encoding="utf-8", errors="replace") as f:
            f.seek(0, os.SEEK_END)
            while not stop.is_set():
                line = f.readline()
                if line:
                    print(line.rstrip("\n"), flush=True)
                else:
                    stop.wait(0.3)

    def keep_hid():
        st = hid_status()
        if st is not None:
            started = float(st.split("started=")[1].split()[0]) if "started=" in st else 0.0
            newest = max(os.path.getmtime(os.path.join(PKG, f)) for f in HID_SOURCES)
            say("Bluetooth HID service: already running" + (
                "; its code is older than the checkout (`phoneuse down`, then `phoneuse up`, updates it; "
                "an iPhone may need pairing again after that)" if newest > started else ""))
        while not stop.is_set():
            if hid_status() is None:
                say("starting the Bluetooth HID service")
                start_hid()
                stop.wait(10)  # binding and publishing take a few seconds
            stop.wait(3)

    threading.Thread(target=follow_hid_log, daemon=True).start()
    threading.Thread(target=keep_hid, daemon=True).start()
    for name, label, start, watch in services:
        threading.Thread(target=keep, args=(name, label, start, watch), daemon=True).start()
    time.sleep(1.0)  # the hub listens before UxPlay sends it anything
    threading.Thread(target=keep, args=("uxplay", "AirPlay receiver (UxPlay)", uxplay), daemon=True).start()

    def warm_vision():
        # The screen-reading models load as soon as phoneuse starts, not on an
        # agent's first look. Skipped if they aren't installed.
        if os.path.exists(VISION_PY):
            try:
                import contextlib, io
                with contextlib.redirect_stderr(io.StringIO()):
                    vision({"op": "ping"}, timeout=180)
            except SystemExit:
                pass
    threading.Thread(target=warm_vision, daemon=True).start()

    def first_status():
        url = f"http://localhost:{WEB_PORT}/"
        for _ in range(40):
            try:
                ipc.http_get(WEB_PORT, "/")
                break
            except OSError:
                time.sleep(0.25)
        try:
            st = Hid().cmd("status")
            say("Bluetooth:", "connected to " + st.split("peer=")[1].split(" connected_phones")[0]
                if "connected=True" in st else
                f"waiting for the iPhone. First time: on the iPhone, Settings > Bluetooth > "
                f"{os.environ.get('COMPUTERNAME', 'this PC')}.")
        except (OSError, RuntimeError) as e:
            say("Bluetooth:", e)
        say(f"Mirror: {url} . On the iPhone: Control Center > Screen Mirroring > {NAME}.")
        from . import doctor
        fw = doctor.firewall_state(ux)
        if fw in ("block", "missing"):
            say(("Windows Firewall blocks UxPlay" if fw == "block" else "No Windows Firewall rule for UxPlay")
                + f": the iPhone won't see {NAME}. Fix it once, " + doctor.FIREWALL_FIX.format(ux=ux))
        if CFG.get("app_window", True) and not appwindow.open_window(url):
            import webbrowser
            webbrowser.open(url)
    threading.Thread(target=first_status, daemon=True).start()

    try:
        while not stop.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        say("stopping")
    finally:
        stop.set()
        for name in ("uxplay", "web", "frames", "hub"):
            p = procs.get(name)
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(3)
                except subprocess.TimeoutExpired:
                    p.kill()


def status():
    if IS_WINDOWS:
        try:
            st = Hid().cmd("status").replace("ok ", "")
            print("HID service: active")
            print("Bluetooth:", st)
        except OSError:
            print("HID service: not running (start `phoneuse up`)")
        try:
            line, _ = screen.hub_request("status", timeout=2)
            print("Video hub:", line.replace("ok ", ""))
        except OSError:
            print("Video hub: not running (start `phoneuse up`)")
        live = screen.mirror_live()
        print("Mirror:", "showing the phone" if live else
              f"not mirroring (input to the phone is off). On the iPhone: Control Center > Screen Mirroring > {NAME}")
        print(f"Browser mirror: http://localhost:{WEB_PORT}")
        return
    svc = subprocess.run(["systemctl", "is-active", "phoneuse-hid"], capture_output=True, text=True).stdout.strip()
    print("HID service:", svc)
    if svc == "active":
        print("Bluetooth:", Hid().cmd("status").replace("ok ", ""))
    try:
        at, size = screen.mirror_window()
        live = "showing the phone" if screen.mirror_live() else "not mirroring (input to the phone is off)"
        print("Mirror window:", at, size, live)
    except RuntimeError:
        print(f"Mirror window: none (run `phoneuse up`, then Screen Mirroring > {NAME} on the iPhone)")


def view(print_only):
    """The browser mirror's address; opens it (Windows: in its own window) unless print_only."""
    url = f"http://localhost:{WEB_PORT}"
    try:
        ipc.http_get(WEB_PORT, "/")
    except OSError:
        die(f"The browser mirror isn't running at {url}. Start `phoneuse up`.")
    if not print_only:
        if IS_WINDOWS:
            from .windows import appwindow
            if not appwindow.open_window(url + "/"):
                os.startfile(url)
        else:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(url)


def agent_done():
    """End the agent's session: the browser mirror stops glowing and its mouse and
    keyboard reach the phone again, at once (not after 20 idle seconds)."""
    try:
        ipc.http_post(WEB_PORT, "/api/agent/done")
    except OSError:
        pass
    print("ok: session ended; control is back with the user")


def app_cmd(args):
    """`phoneuse app pair`: make a one-time pairing link and open it on the phone,
    which asks to open it in the PhoneUse app. `--print` only prints the link."""
    import json
    if args[:1] != ["pair"]:
        die("usage: phoneuse app pair [--print]")
    try:
        _, body = ipc.http_post(WEB_PORT, "/api/companion/pair", timeout=5)
        link = json.loads(body)["link"]
    except (OSError, ValueError, KeyError) as e:
        die(f"The browser mirror didn't answer ({e}). Is `phoneuse up` running?")
    if "--print" in args:
        return print(link)
    h = hid()
    open_app(h, "Safari")
    step(h, "key l gui")  # the address bar
    h.cmd("type " + link)
    step(h, "key enter", quiet=0.3)
    print("Opened the pairing link on the phone. If Safari asks, tap Open to open it in PhoneUse. "
          "The link works once, for 10 minutes.")


def unlock_cmd():
    """Ask the owner to unlock the phone with the saved passcode. Waits for their
    answer (up to 2 minutes); the passcode never passes through here."""
    import json
    print("Asked the owner to approve an unlock (browser mirror or notification); waiting...", flush=True)
    try:
        code, body = ipc.http_post(WEB_PORT, "/api/unlock", timeout=150)
    except OSError as e:
        die(f"The browser mirror isn't running ({e}). Start `phoneuse up`.")
    try:
        result = json.loads(body).get("result", "")
    except ValueError:
        result = body.decode(errors="replace")
    if code >= 400:
        die(result or f"HTTP {code}")
    print(result)


def main():
    if IS_WINDOWS:  # agents read our output through pipes: UTF-8, never the console's code page
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, args = a[0], a[1:]
    global ACTION
    ACTION = describe(cmd, " ".join(x for x in args if x != "--text"))
    # `--text` at the end of an action: print the screen as text afterwards, saving
    # a second call (only as the last argument, so `type` can still type it).
    then_text = bool(args) and args[-1] == "--text" and cmd not in ("text", "find", "screenshot")
    if then_text:
        args = args[:-1]
    if cmd == "install" and IS_WINDOWS:
        sys.exit(subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                 os.path.join(ROOT, "scripts", "windows", "install.ps1")] + args).returncode)
    if cmd == "doctor":
        from . import doctor
        sys.exit(doctor.main(args))
    if cmd == "install":
        subprocess.run(["sudo", os.path.join(ROOT, "scripts", "install.sh")], check=True)
        if not os.path.exists(os.path.join(ROOT, ".venv-vision")):
            import shutil
            if shutil.which("uv"):
                subprocess.run([os.path.join(ROOT, "scripts", "setup-vision.sh")], check=True)
            else:
                print("skipping the screen-reading models (phoneuse text/find): install uv, then run scripts/setup-vision.sh")
        return
    if cmd == "up":
        return up()
    if cmd == "app":  # app pair: pair the companion app (docs/COMPANION.md)
        return app_cmd(args)
    if cmd == "down":
        return down()
    if cmd == "status":
        return status()
    if cmd == "unlock":
        return unlock_cmd()
    if cmd == "done":
        return agent_done()
    if cmd == "bridge":  # the ESP32 Bluetooth bridge: setup, status, firmware
        from . import bridge
        return bridge.main(args)
    if cmd == "awake":  # awake [on|off]: keep the screen awake (the page server nudges the pointer)
        return print(awake_cmd(args[0] if args else None))
    if cmd == "view":
        return view("--print" in args)
    if cmd == "text":
        return text_cmd(args)
    if cmd == "find":
        x, y = find(" ".join(args))
        print(f"{x:.3f} {y:.3f}")
        return
    if cmd == "screenshot":
        paths = [x for x in args if not x.startswith("--")]  # (--keep-pointer: now always)
        return screenshot(paths[0] if paths else runtime_path("phoneuse.png"))
    force = "--force" in args
    args = [a for a in args if a != "--force"]
    if cmd in ("tap", "longpress", "swipe") and not force and len(args) >= 2:
        why = moved_since_look(*point(*args[:2]))
        if why:
            die("Not tapped: " + why)
    h = hid()
    seq_before = frame_seq()
    if cmd == "tap":
        touch.tap(h, *point(*args[:2]))
    elif cmd == "longpress":
        touch.long_press(h, *point(*args[:2]), hold=float(args[2]) if len(args) > 2 else 0.8)
    elif cmd == "swipe":
        x0, y0 = point(*args[0:2]); x1, y1 = point(*args[2:4])
        touch.swipe(h, x0, y0, x1, y1, duration=float(args[4]) if len(args) > 4 else 0.3)
    elif cmd == "scroll":
        scroll(h, args[0] if args else "down", float(args[1]) if len(args) > 1 else 1.0)
    elif cmd == "scroll-by":
        scroll_by(h, float(args[0]), float(args[1]) if len(args) > 1 else 0.5)
    elif cmd in ("home", "back", "switcher", "dismiss", "notifications"):
        globals()[cmd](h)
    elif cmd == "control-center":
        control_center(h)
    elif cmd == "open":
        open_app(h, " ".join(args))
    elif cmd == "type":
        # The text replaces what the field holds (Cmd+A first): fields often keep
        # earlier text. --append adds to it instead.
        if args and args[0] == "--append":
            args = args[1:]
        else:
            key_combo(h, "cmd+a")
        type_text(h, " ".join(args))
    elif cmd == "key":
        key_combo(h, args[0])
    elif cmd == "keyboard":  # Eject: show or hide the on-screen keyboard
        h.cmd("eject")
    elif cmd == "cursor":  # cursor [on|off]: report, or switch AssistiveTouch through Siri
        if not args:
            print("cursor", cursor_state(h))
        elif args[0] in ("on", "off"):
            print(cursor_set(h, args[0] == "on"))
        else:
            die("usage: phoneuse cursor [on|off]")
    elif cmd == "calibrate":  # measure the mirroring phone's pointer (drags use it)
        print(calibrate.calibrate(h, alive=agent_turn))
    elif cmd == "tap-on":
        print(tap_on(h, " ".join(args)))
    elif cmd == "where":
        p = screen.find_orange(screen.grab_rgb())
        print(f"{p[0]:.3f} {p[1]:.3f}" if p else "not found")
        return
    else:
        die(f"unknown command: {cmd} (phoneuse --help)")
    acted(h.first_seq if getattr(h, "first_seq", None) is not None else seq_before)
    print("ok")
    if then_text:
        for x, y, t in screen_text():
            print(f"{x:.3f} {y:.3f}  {t}")
    if NOTES:
        print("\n".join(NOTES))


if __name__ == "__main__":
    main()
