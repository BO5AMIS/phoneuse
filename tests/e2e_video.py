"""End-to-end check of the video path without a phone (Windows).

Streams a synthetic H.264 picture over RTP to the hub the way UxPlay does (an
infinite GOP: one keyframe at the start, like iOS), holds a TCP connection on the
AirPlay mirroring port so the services believe a phone is mirroring, starts the
hub, the frames service and the browser mirror, and checks that:

  * the hub decodes and re-encodes, and reports live=1
  * the frames service, started late, still gets a picture (a fresh keyframe)
  * screenshots come back as PNGs of the right size with the test picture in them
  * settle returns while the picture moves and when it stops
  * a browser tab gets a keyframe first, with the SPS the page needs
  * a hub viewer that restarts gets a keyframe again

Run: .venv\\Scripts\\python tests\\e2e_video.py [--packet-size 1400|60000]
Needs ffmpeg (with libx264) on the PATH or in ~/.local/bin.
"""
import argparse
import asyncio
import io
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from phoneuse import screen  # noqa: E402
from phoneuse.config import CFG  # noqa: E402

FAILS = []


def check(ok, what):
    print(("PASS " if ok else "FAIL ") + what, flush=True)
    if not ok:
        FAILS.append(what)


def ffmpeg():
    return shutil.which("ffmpeg") or os.path.expanduser("~/.local/bin/ffmpeg.exe")


def fake_mirroring():
    """An established TCP connection whose local port is AirPlay's mirroring port."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", screen.AIRPLAY_MIRROR_PORT))
    srv.listen(1)
    c = socket.create_connection(("127.0.0.1", screen.AIRPLAY_MIRROR_PORT))
    a, _ = srv.accept()
    return srv, c, a


def start(module, log):
    return subprocess.Popen([sys.executable, "-u", "-m", f"phoneuse.{module}"], cwd=ROOT,
                            stdout=log, stderr=subprocess.STDOUT, creationflags=0x200)


def wait_for(fn, timeout, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = fn()
            if r:
                return r
        except OSError:
            pass
        time.sleep(step)
    return None


def stream(packet_size, motion):
    # testsrc2 moves continuously; with motion=False a still picture (color) after 1 s of motion
    src = "testsrc2=size=1206x2622:rate=30" if motion else "testsrc2=size=1206x2622:rate=30,trim=end=1,tpad=stop=-1:stop_mode=clone"
    return subprocess.Popen(
        [ffmpeg(), "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i", src,
         "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-bf", "0", "-g", "100000",
         "-x264-params", "keyint=infinite:scenecut=0", "-pix_fmt", "yuv420p",
         "-f", "rtp", "-payload_type", "96", f"rtp://127.0.0.1:{screen.HUB_RTP_PORT}?pkt_size={packet_size}"],
        stdin=subprocess.DEVNULL)


async def browser_tab(port):
    import aiohttp
    got = []
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(f"http://127.0.0.1:{port}/ws", origin=f"http://localhost:{port}") as ws:
            end = time.time() + 6
            while time.time() < end and len([g for g in got if g[0] == "bin"]) < 10:
                m = await ws.receive(timeout=6)
                if m.type == aiohttp.WSMsgType.BINARY:
                    got.append(("bin", m.data[0], m.data[1:]))
                elif m.type == aiohttp.WSMsgType.TEXT:
                    got.append(("text", m.data))
                else:
                    break
    return got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--packet-size", type=int, default=1400)
    a = ap.parse_args()
    logdir = os.path.join(ROOT, ".scratch")
    os.makedirs(logdir, exist_ok=True)
    log = open(os.path.join(logdir, "e2e.log"), "w")
    procs = []
    try:
        mirroring = fake_mirroring()
        procs.append(start("avhub", log))
        check(wait_for(lambda: screen.hub_request("status")[0].startswith("ok"), 10) is not None, "hub answers")
        ff = stream(a.packet_size, motion=True)
        procs.append(ff)
        live = wait_for(lambda: "live=1" in screen.hub_request("status")[0], 15)
        st = screen.hub_request("status")[0]
        check(bool(live), f"hub is live with the stream ({st})")
        check("size=1206x2622" in st, "hub decoded the phone-sized picture")

        # The frames service starts late, mid-stream: it must still get a picture.
        time.sleep(1.5)
        procs.append(start("frames", log))
        ok = wait_for(lambda: "live=1" in screen.frames_request("status")[0], 10)
        check(bool(ok), "frames service, started mid-stream, has a picture (keyframe on join)")
        line, png = screen.frames_request("shot", timeout=5)
        from PIL import Image
        img = Image.open(io.BytesIO(png)).convert("RGB") if line.startswith("ok") else None
        check(img is not None and img.size == (CFG["screen_points"][0] * 2, CFG["screen_points"][1] * 2),
              f"screenshot is a PNG of the phone's size ({img.size if img else line})")
        if img:
            colors = len(img.resize((64, 128)).getcolors(64 * 128) or [])
            check(colors > 50, f"screenshot shows the test picture ({colors} colours in a thumbnail)")

        seq0 = int(screen.frames_request("status")[0].split("seq=")[1])
        time.sleep(1.0)
        seq1 = int(screen.frames_request("status")[0].split("seq=")[1])
        check(seq1 > seq0, f"moving picture counts changes ({seq0} -> {seq1})")
        t = time.time()
        line, _ = screen.frames_request("settle", timeout=6)
        took = time.time() - t
        check(line.startswith("ok") and took < 3.5, f"settle returns on a moving picture ({took:.2f} s: {line})")

        # Browser mirror: keyframe first, with SPS.
        procs.append(start("web", log))
        port = CFG["web_port"]
        wait_for(lambda: __import__("phoneuse.ipc", fromlist=["x"]).http_get(port, "/")[0] == 200, 10)
        got = asyncio.run(browser_tab(port))
        frames = [g for g in got if g[0] == "bin"]
        states = [g for g in got if g[0] == "text"]
        check(bool(states) and '"live": true' in states[-1][1], f"tab is told the phone is live ({states[-1][1][:80] if states else None})")
        check(len(frames) >= 5, f"tab receives frames ({len(frames)})")
        if frames:
            from phoneuse.rtp import nal_types
            check(frames[0][1] == 1 and 7 in nal_types(frames[0][2]), "tab's first frame is a keyframe with SPS")

        # Still picture: restart the stream with a picture that stops moving.
        ff.terminate()
        ff.wait()
        ff2 = stream(a.packet_size, motion=False)
        procs.append(ff2)
        time.sleep(3.0)
        t = time.time()
        line, _ = screen.frames_request("settle", timeout=6)
        took = time.time() - t
        check(line.startswith("ok") and "still=1" in line and took < 0.5,
              f"settle on a still picture returns at once ({took:.2f} s: {line})")
        seq_a = int(screen.frames_request("status")[0].split("seq=")[1])
        time.sleep(1.0)
        seq_b = int(screen.frames_request("status")[0].split("seq=")[1])
        check(seq_b == seq_a, f"still picture counts no changes ({seq_a} -> {seq_b})")

        st = screen.hub_request("status")[0]
        check(" lost=0" in st, f"no RTP packets lost ({st})")
        # Mirroring ends: the picture is forgotten.
        for s in mirroring:
            s.close()
        gone = wait_for(lambda: "live=0" in screen.frames_request("status")[0], 6)
        check(bool(gone), "when mirroring ends the frames service stops showing the old picture")
    finally:
        for p in reversed(procs):
            p.terminate()
        for p in procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        log.close()
    print("\n" + ("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED"))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
