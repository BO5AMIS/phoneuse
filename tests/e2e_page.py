"""Does the browser mirror page decode and draw the hub's video? (Windows, no phone.)

Starts the hub and the browser mirror with a synthetic stream (as e2e_video.py),
opens the page in headless Edge, and reads back over the DevTools protocol
whether the page's decoder is playing and the canvas holds a picture (pixel
statistics, so nobody needs to look at a screenshot).

Run: .venv\\Scripts\\python tests\\e2e_page.py
"""
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from e2e_video import check, fake_mirroring, start, stream, wait_for, FAILS  # noqa: E402
from phoneuse import ipc, screen  # noqa: E402
from phoneuse.config import CFG, runtime_path  # noqa: E402
from phoneuse.windows import appwindow  # noqa: E402

CDP_PORT = 9333
PROBE = """(() => {
  const c = document.getElementById('video');
  let mean = 0, n = 0, distinct = new Set();
  if (c.width > 1) {
    const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
    for (let i = 0; i < d.length; i += 4 * 1009) { mean += d[i] + d[i + 1] + d[i + 2]; n++; distinct.add(d[i] >> 4 << 8 | d[i + 1] >> 4 << 4 | d[i + 2] >> 4); }
  }
  return JSON.stringify({ webcodecs: 'VideoDecoder' in window, playing, hasPicture, configured,
                          width: c.width, height: c.height, mean: n ? mean / n / 3 : 0, colours: distinct.size,
                          note: document.getElementById('note').textContent.trim() });
})()"""


async def evaluate(ws_url, expr):
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(ws_url, max_msg_size=0) as ws:
            await ws.send_str(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                          "params": {"expression": expr, "returnByValue": True}}))
            while True:
                m = json.loads((await ws.receive(timeout=10)).data)
                if m.get("id") == 1:
                    return m["result"]["result"].get("value")


def main():
    log = open(os.path.join(ROOT, ".scratch", "e2e-page.log"), "w")
    procs = []
    edge = None
    try:
        mirroring = fake_mirroring()  # noqa: F841 (held open)
        procs.append(start("avhub", log))
        wait_for(lambda: screen.hub_request("status")[0].startswith("ok"), 10)
        procs.append(stream(60000, motion=True))
        wait_for(lambda: "live=1" in screen.hub_request("status")[0], 15)
        procs.append(start("web", log))
        port = CFG["web_port"]
        wait_for(lambda: ipc.http_get(port, "/")[0] == 200, 10)

        profile = runtime_path("test-edge-profile")
        edge = subprocess.Popen([appwindow.browser(), "--headless=new", f"--remote-debugging-port={CDP_PORT}",
                                 f"--user-data-dir={profile}", "--window-size=520,1060", "--no-first-run",
                                 f"http://localhost:{port}/"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def page():
            with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=2) as r:
                return next((t for t in json.load(r) if t.get("type") == "page" and f":{port}" in t.get("url", "")), None)
        target = wait_for(page, 15)
        check(target is not None, "headless Edge opened the browser mirror")
        if not target:
            return
        result = None
        end = time.time() + 15
        while time.time() < end:
            result = json.loads(asyncio.run(evaluate(target["webSocketDebuggerUrl"], PROBE)))
            if result["playing"] and result["colours"] > 20:
                break
            time.sleep(0.5)
        print("page:", result)
        check(result["webcodecs"], "the browser has WebCodecs")
        check(result["playing"] and result["hasPicture"], "the page's decoder is playing")
        check(result["width"] == 1206 and result["height"] == 2622, f"canvas is the phone's size ({result['width']}x{result['height']})")
        check(result["colours"] > 20 and result["mean"] > 10, f"canvas shows the picture (mean {result['mean']:.0f}, {result['colours']} colours)")
        check(result["note"] == "", f"no warning note over the picture ({result['note']!r})")
    finally:
        if edge:  # Edge runs as a tree of processes: stop all of them
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(edge.pid)], capture_output=True)
        for p in reversed(procs):
            p.terminate()
        log.close()
    print("\n" + ("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED"))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
