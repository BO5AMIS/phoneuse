"""phoneuse settings: defaults, overridden by config.toml in the config directory
(~/.config/phoneuse on Linux, %APPDATA%\\phoneuse on Windows).

Example config.toml:

    name = "PhoneUse"                 # AirPlay receiver name (and Bluetooth name on Linux)
    screen_points = "auto"           # your iPhone's screen in points, such as [402, 874]; auto: from
                                     # the model of the phone that last started mirroring
    decoder = "auto"                 # Linux: GStreamer H.264 decoder; auto picks NVDEC if present
    encoder = "auto"                 # Windows: the hub's H.264 encoder (h264_nvenc, libx264, h264_mf)
    mirror_width = 498               # Linux: mirror window width in logical pixels
    web_port = 8777                  # browser mirror: http://localhost:8777
    uxplay = "auto"                  # Windows: path to uxplay.exe (auto: C:\\msys64\\ucrt64\\bin)
    audio = true                     # play the phone's audio on this PC
    app_window = true                # Windows: `phoneuse up` opens the mirror in its own window
    service_port = 47810             # Windows: first of the local ports phoneuse's services use
"""
import os
import shutil
import subprocess
import sys

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

IS_WINDOWS = sys.platform == "win32"

DEFAULTS = {
    "name": "PhoneUse",
    "screen_points": "auto",
    "decoder": "auto",
    "encoder": "auto",
    "mirror_width": 498,
    "web_port": 8777,
    "uxplay": "auto",
    "audio": True,
    "app_window": True,
    "service_port": 47810,
}


def config_dir():
    if IS_WINDOWS:
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "phoneuse")
    base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(base, "phoneuse")


def config_path():
    return os.path.join(config_dir(), "config.toml")


def _runtime_dir():
    """Logs, sockets and small state files that only matter while phoneuse runs."""
    if IS_WINDOWS:
        d = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "phoneuse", "run")
        os.makedirs(d, exist_ok=True)
        return d
    return os.environ.get("XDG_RUNTIME_DIR", "/tmp")


def load():
    cfg = dict(DEFAULTS)
    if tomllib is None:
        return cfg
    try:
        with open(config_path(), "rb") as f:
            cfg.update(tomllib.load(f))
    except FileNotFoundError:
        pass
    return cfg


CFG = load()
NAME = CFG["name"]
RUNTIME_DIR = _runtime_dir()


def runtime_path(name):
    return os.path.join(RUNTIME_DIR, name)


# The screen in points (Display Zoom off) by model identifier, as AirPlay reports it
# ("iPhone15,3"). Gestures are planned in points and screenshots are scaled to them, so
# a wrong size makes every drag and swipe off by the same factor (taps aren't affected).
_SCREENS = {
    (320, 568): "8,4",
    (375, 667): "8,1 9,1 9,3 10,1 10,4 12,8 14,6",
    (414, 736): "8,2 9,2 9,4 10,2 10,5",
    (375, 812): "10,3 10,6 11,2 12,3 13,1 14,4",
    (414, 896): "11,4 11,6 11,8 12,1 12,5",
    (390, 844): "13,2 13,3 14,2 14,5 14,7 17,5",
    (428, 926): "13,4 14,3 14,8",
    (393, 852): "15,2 15,4 16,1 17,3",
    (430, 932): "15,3 15,5 16,2 17,4",
    (402, 874): "17,1 18,1 18,3",
    (440, 956): "17,2 18,2",
    (420, 912): "18,4",
}
MODEL_POINTS = {f"iPhone{m}": pts for pts, models in _SCREENS.items() for m in models.split()}
DEFAULT_POINTS = (402, 874)  # iPhone 17 Pro
MODEL_FILE = runtime_path("mirroring-model")  # written by cli.follow_sender


def mirroring_model():
    """The model identifier of the iPhone that last started mirroring, or None."""
    try:
        with open(MODEL_FILE, encoding="utf-8-sig") as f:
            return f.read().strip() or None
    except OSError:
        return None


def screen_points():
    """The phone's screen in points, and where that came from: config.toml's
    screen_points, else the model of the phone that last started mirroring, else
    an iPhone 17 Pro's."""
    configured = CFG.get("screen_points", "auto")
    if isinstance(configured, (list, tuple)) and len(configured) == 2 and min(configured) > 0:
        return (int(configured[0]), int(configured[1])), "config.toml"
    model = mirroring_model()
    if model in MODEL_POINTS:
        return MODEL_POINTS[model], model
    return DEFAULT_POINTS, (f"unknown model {model}" if model else "no phone has mirrored yet")


SCREEN_PT, SCREEN_SOURCE = screen_points()
PHONE_ASPECT = SCREEN_PT[0] / SCREEN_PT[1]


def refresh_screen():
    """For long-running processes that import phoneuse's modules (the MCP server):
    when a phone with another screen size has started mirroring since, update the
    size everywhere it was imported. The services restart instead (`phoneuse up`
    watches MODEL_FILE). True when it changed."""
    global SCREEN_PT, SCREEN_SOURCE, PHONE_ASPECT
    pts, source = screen_points()
    if pts == SCREEN_PT:
        return False
    old_pts, old_aspect = SCREEN_PT, PHONE_ASPECT
    SCREEN_PT, SCREEN_SOURCE, PHONE_ASPECT = pts, source, pts[0] / pts[1]
    for name, mod in list(sys.modules.items()):
        if name == "phoneuse" or name.startswith("phoneuse."):
            for attr, old, new in (("SCREEN_PT", old_pts, pts), ("PT", old_pts, pts),
                                   ("PHONE_ASPECT", old_aspect, PHONE_ASPECT)):
                if getattr(mod, attr, None) == old:
                    setattr(mod, attr, new)
    return True


# Local services. On Linux these are unix sockets (the HID daemon's is root-owned and
# group-writable); on Windows, TCP ports on 127.0.0.1 guarded by a per-user token (ipc.py).
HID_SOCKET = "/run/phoneuse-hid.sock"
VISION_SOCKET = runtime_path("phoneuse-vision.sock")
HUB_SOCKET = runtime_path("phoneuse-hub.sock")
FRAMES_SOCKET = runtime_path("phoneuse-frames.sock")
SERVICE_PORTS = {name: int(CFG["service_port"]) + i for i, name in enumerate(("hid", "hub", "frames", "vision"))}


def decoder():
    """H.264 decoder for the mirror (Linux, GStreamer). Frames must leave the decoder as
    soon as they are decoded: iOS only sends a frame when the screen changes and doesn't
    declare 'no reordering', so a default decoder holds each frame until the next arrives
    (measured 200 ms). nvh264dec with compliance=flexible releases them in ~1 ms;
    the software decoder releases them immediately."""
    d = CFG["decoder"]
    if d != "auto":
        return d
    if shutil.which("gst-inspect-1.0"):
        r = subprocess.run(["gst-inspect-1.0", "nvh264dec"], capture_output=True)
        if r.returncode == 0:
            return "nvh264dec max-display-delay=0 compliance=flexible"
    return "avdec_h264"
