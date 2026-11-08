"""Check the vision service without a phone: OCR finds text drawn at known places,
the service starts on demand through ipc.py, and runs on the GPU when there is one.

Run: .venv\\Scripts\\python tests\\vision_check.py
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from phoneuse import cli  # noqa: E402
from phoneuse.config import runtime_path  # noqa: E402

WORDS = {"Wi-Fi": (0.25, 0.30), "Bluetooth": (0.30, 0.40), "General": (0.28, 0.55), "Done": (0.85, 0.08)}


def main():
    img = Image.new("RGB", (804, 1748), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("segoeui.ttf", 44)
    except OSError:
        font = ImageFont.load_default(44)
    for word, (x, y) in WORDS.items():
        w, h = d.textbbox((0, 0), word, font=font)[2:]
        d.text((x * 804 - w / 2, y * 1748 - h / 2), word, fill="black", font=font)
    path = runtime_path("vision-check.png")
    img.save(path)
    t = time.time()
    r = cli.vision({"op": "ping"}, timeout=240)
    print(f"service up in {time.time() - t:.1f} s: {r}")
    t = time.time()
    r = cli.vision({"op": "text", "image": path})
    took = time.time() - t
    lines = {ln["text"]: (ln["x"], ln["y"]) for ln in r.get("lines", [])}
    print(f"OCR in {took:.2f} s:", lines)
    fails = 0
    for word, (x, y) in WORDS.items():
        got = lines.get(word)
        ok = got is not None and abs(got[0] - x) < 0.03 and abs(got[1] - y) < 0.02
        fails += not ok
        print(("PASS " if ok else "FAIL ") + f"{word} at {got} (drawn at {x}, {y})")
    t = time.time()
    r = cli.vision({"op": "find", "image": path, "what": "Bluetooth text"})
    print(f"grounding in {time.time() - t:.2f} s: {r}")
    ok = "x" in r and abs(r["x"] - 0.30) < 0.08 and abs(r["y"] - 0.40) < 0.04
    fails += not ok
    print(("PASS " if ok else "FAIL ") + "grounding finds 'Bluetooth'")
    log = open(cli.VISION_LOG, errors="replace").read()[-600:]
    print("vision log tail:", log)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
