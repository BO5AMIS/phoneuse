"""`phoneuse doctor`: check every part of the setup and say how to fix what isn't right.

Each check prints one line: ok, note (works, but worth knowing), or FAIL with the
fix underneath. Exits 1 when anything failed. `--quick` skips the slow checks
(the vision models load torch).
"""
import importlib
import json
import os
import shutil
import subprocess
import sys

from . import cli, ipc
from .config import CFG, IS_WINDOWS, NAME, SERVICE_PORTS, config_path, runtime_path

ROOT = cli.ROOT
RESULTS = []
COLORS = {"ok": "\033[32m", "note": "\033[33m", "FAIL": "\033[31m"}


def report(level, what, fix=None):
    RESULTS.append(level)
    color = COLORS[level] if sys.stdout.isatty() else ""
    reset = "\033[0m" if color else ""
    print(f"  {color}{level:<5}{reset} {what}")
    if fix:
        for line in fix.splitlines():
            print(f"        {line}")


def run(args, timeout=20, **kw):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, errors="replace", **kw)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def powershell(script, timeout=30):
    return run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=timeout)


INSTALL = r"powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1"


# ---------- checks ----------

def check_python():
    v = sys.version_info
    if v < (3, 11):
        report("FAIL", f"Python {v.major}.{v.minor}: phoneuse needs 3.11 or newer", "install Python 3.11+, then " + INSTALL)
    else:
        report("ok", f"Python {v.major}.{v.minor}.{v.micro} ({sys.executable})")


def check_packages():
    mods = ["av", "numpy", "PIL", "aiohttp", "psutil", "mcp",
            "winrt.windows.devices.bluetooth", "winrt.windows.devices.bluetooth.genericattributeprofile",
            "winrt.windows.storage.streams", "winrt.windows.ui.notifications"]
    missing = []
    for m in mods:
        try:
            importlib.import_module(m)
        except Exception:
            missing.append(m)
    if missing:
        report("FAIL", "Python packages missing: " + ", ".join(missing), INSTALL + "   (or: .venv\\Scripts\\pip install -e .)")
    else:
        report("ok", "Python packages (PyAV, aiohttp, Windows Runtime Bluetooth, MCP SDK)")


def check_encoder():
    try:
        from .avhub import open_encoder
        e = open_encoder(640, 1392)
        name = e.name
        if name == "h264_nvenc":
            report("ok", "video encoder: NVENC (NVIDIA GPU)")
        else:
            report("note", f"video encoder: {name} (no NVENC: the hub uses the CPU, ~2-4 ms more per frame)")
    except Exception as e:
        report("FAIL", f"no H.264 encoder works: {e}", "update the GPU driver, or set encoder = \"libx264\" in " + config_path())


def check_uxplay():
    ux = cli.uxplay_path()
    if not ux or not os.path.exists(ux):
        report("FAIL", "UxPlay (the AirPlay receiver) isn't installed", INSTALL)
        return None
    env = dict(os.environ, PATH=os.path.dirname(ux) + os.pathsep + os.environ.get("PATH", ""))
    code, out = run([ux, "-v"], env=env)
    version = next((ln.split(";")[0].replace(" version", "") for ln in out.splitlines() if "version" in ln.lower()), out[:80])
    if code != 0 and "version" not in out.lower():
        report("FAIL", f"UxPlay doesn't run: {out[:200]}", INSTALL)
        return None
    report("ok", f"{version.strip()} ({ux})")
    inspect = os.path.join(os.path.dirname(ux), "gst-inspect-1.0.exe")
    if os.path.exists(inspect):
        missing = [el for el in ("rtph264pay", "udpsink", "h264parse", "wasapisink", "avdec_aac")
                   if run([inspect, el], env=env, timeout=60)[0] != 0]
        if missing:
            report("FAIL", "GStreamer elements UxPlay needs are missing: " + ", ".join(missing), INSTALL)
        else:
            report("ok", "GStreamer elements for UxPlay (RTP out, WASAPI audio)")
    return ux


def firewall_state(ux):
    """"allow", "block" (an enabled inbound Block rule for UxPlay wins over any Allow),
    "missing", or "unknown" when the rules can't be read."""
    ux = ux.replace("'", "''")  # a PowerShell single-quoted string
    script = (f"$r = Get-NetFirewallApplicationFilter -Program '{ux}' -ErrorAction SilentlyContinue | "
              "Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object { $_.Enabled -eq 'True' -and "
              "$_.Direction -eq 'Inbound' }; @($r | ForEach-Object { \"$($_.Action)\" }) | ConvertTo-Json -Compress")
    code, out = powershell(script)
    try:
        actions = json.loads(out) if out else []
    except ValueError:
        return "unknown"
    actions = actions if isinstance(actions, list) else [actions]
    if "Block" in actions:
        return "block"
    return "allow" if "Allow" in actions else "missing"


FIREWALL_FIX = ("as admin: powershell -ExecutionPolicy Bypass -File scripts\\windows\\firewall.ps1 -Program \"{ux}\"\n"
                "(or rerun install.ps1, which asks for admin rights once)")


def check_firewall(ux):
    if not ux:
        return
    state = firewall_state(ux)
    fix = FIREWALL_FIX.format(ux=ux)
    if state == "block":
        report("FAIL", "Windows Firewall BLOCKS UxPlay (a rule from a dismissed 'Allow access?' prompt): "
               "the iPhone won't see the receiver", fix)
    elif state == "missing":
        report("FAIL", "no Windows Firewall rule lets the iPhone reach UxPlay", fix)
    elif state == "unknown":
        report("note", "couldn't read the Windows Firewall rules")
    else:
        report("ok", "Windows Firewall lets the local network reach UxPlay")


def check_network():
    code, out = powershell("Get-NetConnectionProfile | ForEach-Object { \"$($_.InterfaceAlias)|$($_.Name)|$($_.NetworkCategory)\" }")
    nets = [ln.split("|") for ln in out.splitlines() if ln.count("|") == 2]
    if not nets:
        report("note", "couldn't read the network profiles")
        return
    desc = "; ".join(f"{a} ({n}, {c})" for a, n, c in nets)
    report("ok", f"network: {desc}. The iPhone must be on the same network (same Wi-Fi, no guest isolation)")


def check_ports():
    import psutil
    wanted = {("tcp", 7000), ("tcp", 7001), ("tcp", 7100), ("udp", 6000), ("udp", 6001), ("udp", 7011),
              ("tcp", int(CFG["web_port"]))} | {("tcp", p) for p in SERVICE_PORTS.values()}
    ours = {"uxplay.exe", "python.exe", "pythonw.exe", "phoneuse.exe"}
    taken = []
    try:
        for c in psutil.net_connections(kind="inet"):
            kind = "tcp" if c.type == 1 else "udp"
            if (kind, c.laddr.port) in wanted and (kind == "udp" or c.status == psutil.CONN_LISTEN):
                try:
                    who = psutil.Process(c.pid).name() if c.pid else "?"
                except psutil.Error:
                    who = "?"
                if who.lower() not in ours:
                    taken.append(f"{kind.upper()} {c.laddr.port} ({who})")
    except psutil.Error as e:
        report("note", f"couldn't check the ports ({e})")
        return
    if taken:
        report("FAIL", "ports phoneuse needs are taken by other programs: " + ", ".join(sorted(set(taken))),
               "close those programs (another AirPlay receiver?), or change web_port/service_port in " + config_path())
    else:
        report("ok", "AirPlay, browser mirror and service ports are free (or phoneuse's own)")


def check_bluetooth():
    import asyncio

    async def probe():
        from winrt.windows.devices.bluetooth import BluetoothAdapter
        from winrt.windows.devices.radios import RadioState
        a = await BluetoothAdapter.get_default_async()
        if a is None:
            return None
        r = await a.get_radio_async()
        return a.is_low_energy_supported, a.is_peripheral_role_supported, r is None or r.state == RadioState.ON
    try:
        res = asyncio.run(probe())
    except Exception as e:
        report("FAIL", f"Bluetooth: can't ask Windows ({e})", INSTALL)
        return
    if res is None:
        report("FAIL", "no Bluetooth adapter", "phoneuse needs Bluetooth to act as the iPhone's mouse and keyboard")
    elif not (res[0] and res[1]):
        report("FAIL", "this Bluetooth adapter can't act as a Bluetooth LE peripheral (keyboard/mouse)",
               "a different adapter is needed (most from 2018 on can)")
    elif not res[2]:
        report("FAIL", "Bluetooth is off", "turn it on: Settings > Bluetooth & devices")
    else:
        report("ok", "Bluetooth adapter can be a Bluetooth LE keyboard and mouse")


def check_services():
    """The running services. Returns the mirrored picture's size ("WxH") when the
    phone is mirroring."""
    try:
        st = ipc.request("hid", "status", timeout=3)[0]
    except OSError:
        report("note", "phoneuse isn't running (start it with `phoneuse up`; the rest of these checks don't need it)")
        return None
    size = None
    if "connected=True" in st:
        peer = st.split("peer=")[1].split(" connected_phones")[0]
        interval = st.split("interval_ms=")[1].split()[0]
        report("ok", f"Bluetooth: connected to {peer} (connection interval {interval} ms)")
    elif "published=False" in st:
        report("FAIL", "Bluetooth: the keyboard/mouse service isn't published: " + st.split("problem=")[1])
    else:
        report("note", f"Bluetooth: no iPhone connected. Pair once: on the iPhone, Settings > Bluetooth > "
                       f"{os.environ.get('COMPUTERNAME', 'this PC')}")
    try:
        hub = ipc.request("hub", "status", timeout=3)[0]
        if "live=1" in hub:
            size = hub.split("size=")[1].split()[0]
            report("ok", f"mirror: the phone is mirroring ({size})")
        else:
            report("note", f"mirror: not mirroring. On the iPhone: Control Center > Screen Mirroring > {NAME}")
        if " lost=0" not in hub:
            report("note", "video: some RTP packets were lost on the way from UxPlay (" + hub.split("lost=")[1] + ")")
    except OSError:
        report("FAIL", "the video hub isn't running (phoneuse up restarts it; see its output)")
    return size


def check_screen(size):
    """The screen size gestures are planned in, against the mirrored picture's shape
    (`size`, "WxH", when mirroring): a model phoneuse doesn't know, or Display Zoom,
    shows up as a picture of another shape. Then whether drags are calibrated."""
    from . import config, phones
    pts, source = config.screen_points()
    known = source == "config.toml" or source in config.MODEL_POINTS
    report("ok" if known else "note", f"screen: {pts[0]} x {pts[1]} points ({source})",
           None if known else f"set screen_points in {config_path()} to your iPhone's screen in points")
    if size and "x" in size:
        w, h = (int(v) for v in size.split("x"))
        # The phone fits its screen into the display UxPlay offers, keeping its shape (a
        # pixel of rounding: ~0.04%). Many iPhones share a shape within 0.1%, so this
        # only catches a clearly wrong size, such as an iPhone 17 Pro's (402 x 874) for
        # an iPhone 14 Pro Max (430 x 932): 0.3% apart.
        if abs((w / h) / (pts[0] / pts[1]) - 1) > 0.0025:
            report("FAIL", f"screen: the mirrored picture ({size}) isn't the shape of a {pts[0]} x {pts[1]} point "
                           "screen, so drags and swipes go the wrong distance",
                   f"set screen_points in {config_path()} to your iPhone's screen in points (Display Zoom "
                   "changes them)")
    name = phones.current()
    if name:
        e = phones.entry(name)
        if e.get("drag_pts"):
            report("ok", f"drags: {name} is calibrated ({e.get('calibrated', 'date unknown')})")
        elif phones.needs_calibration(name):
            report("note", f"drags: {name} isn't calibrated yet, so drags and swipes may go the wrong distance "
                           "(it is measured by itself once its pointer is on)",
                   "phoneuse calibrate (with the phone mirroring)")


def check_browser():
    from .windows import appwindow
    b = appwindow.browser()
    if b:
        report("ok", f"mirror window: {os.path.basename(b)} in app mode")
    else:
        report("note", "no Edge or Chrome: the mirror opens in your default browser (needs WebCodecs)")


def check_vision(quick):
    if not os.path.exists(cli.VISION_PY):
        report("note", "screen-reading models aren't installed (phoneuse text / find / tap-on)",
               r"powershell -ExecutionPolicy Bypass -File scripts\windows\setup-vision.ps1" if IS_WINDOWS
               else "scripts/setup-vision.sh")
        return
    if quick:
        report("ok", "screen-reading models installed (not loaded: --quick)")
        return
    # OCR on the GPU is real only if a session actually opens on CUDA: onnxruntime lists
    # the CUDA provider even when its DLLs are missing, then quietly runs on the CPU.
    probe = (
        "import glob, json, os, sys, importlib\n"
        "import onnxruntime as o, rapidocr\n"
        "sys.platform == 'win32' and hasattr(o, 'preload_dlls') and o.preload_dlls()\n"
        "t = importlib.import_module('torch')\n"
        "ocr = False\n"
        "m = sorted(glob.glob(os.path.join(os.path.dirname(rapidocr.__file__), '**', '*.onnx'), recursive=True))\n"
        "if m and 'CUDAExecutionProvider' in o.get_available_providers():\n"
        "    try:\n"
        "        s = o.InferenceSession(m[0], providers=['CUDAExecutionProvider'])\n"
        "        ocr = s.get_providers()[0] == 'CUDAExecutionProvider'\n"
        "    except Exception:\n"
        "        pass\n"
        "print(json.dumps([t.cuda.is_available(), ocr]))\n")
    code, out = run([cli.VISION_PY, "-c", probe], timeout=180, cwd=ROOT)
    try:
        torch_cuda, ort_cuda = json.loads(out.splitlines()[-1])
    except (ValueError, IndexError):
        report("FAIL", f"screen-reading models don't load: {out[-300:]}",
               r"powershell -ExecutionPolicy Bypass -File scripts\windows\setup-vision.ps1")
        return
    if torch_cuda and ort_cuda:
        report("ok", "screen-reading models (OCR and grounding on the GPU)")
    else:
        report("note", f"screen-reading models on the CPU (GPU: grounding {torch_cuda}, OCR {ort_cuda}); slower but fine")


def check_linux():
    ux = shutil.which("uxplay")
    report("ok" if ux else "FAIL", f"UxPlay: {ux or 'not found'}", None if ux else "install uxplay (AUR/distro package)")
    code, out = run(["systemctl", "is-active", "phoneuse-hid"])
    report("ok" if out == "active" else "FAIL", f"HID service: {out}", None if out == "active" else "phoneuse install")


def main(args=()):
    quick = "--quick" in args
    print("phoneuse doctor" + (" (quick)" if quick else ""))
    check_python()
    if IS_WINDOWS:
        check_packages()
        check_bluetooth()
        check_encoder()
        ux = check_uxplay()
        check_firewall(ux)
        check_network()
        check_ports()
        check_browser()
        check_screen(check_services())
    else:
        check_linux()
        check_screen(None)
    check_vision(quick)
    print("\n  On the iPhone: Settings > Accessibility > Touch > AssistiveTouch on, Dwell Control off.")
    print(f"  Logs: {runtime_path('')}")
    failed = RESULTS.count("FAIL")
    print(f"\n{failed} problem(s) to fix." if failed else "\nAll set.")
    return 1 if failed else 0
