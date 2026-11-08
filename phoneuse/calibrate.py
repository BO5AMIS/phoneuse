"""Measure how far one relative mouse report moves this phone's pointer, per
report size. iOS scales relative motion by AssistiveTouch's Tracking Speed, which
differs between phones (and settings), so drags need each phone's own table
(touch.REF_PTS is the iPhone 17 Pro's, at its setting). Saved per phone in
phones.json; touch.py uses the mirroring phone's table.

Each size: the pointer goes to a start point (absolute report, exact on every
phone), N relative reports of that size move it up (button 0: a hover, nothing is
tapped), then it goes back to the start. The pointer is drawn into the mirrored
picture, so the pixels that changed between "at start" and "moved" but match
between "at start" and "back" are the pointer at both places; animations on the
screen differ between the two "at start" frames and drop out.
"""
import re
import time

import numpy as np

from . import phones, screen, touch
from .config import SCREEN_PT

SIZES = [28, 20, 12, 8, 4, 2, 1]  # largest first: each one's travel is aimed from the last
MAX_REPORTS = 150  # ~1.2 s at most per size
START = (0.5, 0.85)
TRAVEL = 0.3  # aim each measurement at about this share of the screen height
SETTLE = 0.35  # the mirror trails the phone by ~0.1-0.2 s
COLUMN = 0.06  # the pointer moves straight up: spots farther than this from START's x aren't it


def _settle(seconds=SETTLE):
    time.sleep(seconds)
    return screen.grab_rgb().astype(np.int16)


def _pointer_spots(at_start, moved, back):
    """Centres (fractions) of the pointer-shaped changes: changed between at_start
    and moved, unchanged between at_start and back."""
    if not (at_start.shape == moved.shape == back.shape):
        return []
    h, w = at_start.shape[:2]
    mask = (np.abs(moved - at_start).max(axis=2) > 40) & (np.abs(back - at_start).max(axis=2) < 20)
    spots = []
    for cells, xs, ys in screen._components(mask):
        if cells >= 3:  # a few 4-px cells at least: not noise
            spots.append((cells, float(xs.mean()) / w, float(ys.mean()) / h))
    return spots


def _to_start(h):
    """Put the pointer on START. An absolute report with the same position as the
    previous one is ignored after relative moves (iOS sees no change in the
    absolute axes; measured on an iPhone 15 Pro), so step beside it first."""
    h.cmd(f"a {START[0] + 0.03} {START[1]} 0")
    time.sleep(0.03)
    h.cmd(f"a {START[0]} {START[1]} 0")


def link_waits(h):
    """How many reports have waited for the Bluetooth link to come back (the Windows
    HID service; 0 elsewhere)."""
    m = re.search(r"waited_for_link=(\d+)", h.cmd("status"))
    return int(m[1]) if m else 0


def measure(h, units, reports, tries=3):
    """Points (fraction * SCREEN_PT) that `reports` relative reports of `units`
    moved the pointer straight up, or None if the pointer wasn't found. Measured
    again when the Bluetooth link dropped during the move (Windows: the HID service
    rides out drops, but the motion in reports lost at a drop is gone)."""
    for _ in range(tries):
        _to_start(h)
        at_start = _settle()
        drops = link_waits(h)
        for _ in range(reports):
            h.cmd(f"m 0 {-units} 0 0")
            time.sleep(touch.TICK)
        moved = _settle()
        if link_waits(h) == drops:
            break
    else:
        return None
    _to_start(h)
    back = _settle()
    # The pointer moved straight up: only spots above the start, in its column. A
    # text cursor blinking in phase elsewhere (a new note's, top left) passes the
    # three-frame test, and when the pointer had moved too little to see, the
    # farthest spot was the cursor (measured over Bluetooth LE).
    spots = [s for s in _pointer_spots(at_start, moved, back)
             if abs(s[1] - START[0]) < COLUMN and s[2] < START[1]]
    if not spots:
        return None
    # The spot farthest from the start is where the pointer went.
    far = max(spots, key=lambda s: (s[1] - START[0]) ** 2 + (s[2] - START[1]) ** 2)
    dx, dy = (far[1] - START[0]) * SCREEN_PT[0], (far[2] - START[1]) * SCREEN_PT[1]
    if (dx * dx + dy * dy) ** 0.5 < 6:  # that's the start spot itself: it didn't move visibly
        return None
    return -dy


def _ref(units):
    """Points per report in the 17 Pro table (touch.REF_PTS), interpolated."""
    us = [u for u, _ in touch.REF_PTS]
    for (u0, p0), (u1, p1) in zip(touch.REF_PTS, touch.REF_PTS[1:]):
        if u0 <= units <= u1:
            return p0 + (p1 - p0) * (units - u0) / (u1 - u0)
    return touch.REF_PTS[-1][1] * units / us[-1]


def _default(units):
    """Points per report in the table drags use before calibration (touch.DEFAULT_PTS:
    the 17 Pro's over Classic, an LE table on Windows), interpolated."""
    pts = touch.DEFAULT_PTS
    for (u0, p0), (u1, p1) in zip(pts, pts[1:]):
        if u0 <= units <= u1:
            return p0 + (p1 - p0) * (units - u0) / (u1 - u0)
    return pts[-1][1] * units / pts[-1][0]


def run(h, sizes=SIZES, log=print, alive=None):
    """Measure points per report for each size; returns [(units, points)] sorted by
    size, or None if the measurement didn't hold together. Phones differ a lot (an
    iPhone 15 Pro moved 2-22% as far as the 17 Pro table, least for small reports),
    so each size's report count comes from the size measured before it. The first
    guess comes from this link's default table: over Bluetooth LE the 17 Pro table
    guessed 2 reports for 28 units, too few to measure, and the error carried on."""
    table, last = [], None
    for u in sizes:
        if alive:
            alive()
        guess = last[1] * u / last[0] if last else _default(u)
        reports = min(MAX_REPORTS, max(2, round(TRAVEL * SCREEN_PT[1] / guess)))
        moved = measure(h, u, reports)
        if moved is None:
            log(f"  {u:2d} units: pointer not found")
            continue
        per = moved / reports
        last = (u, per)
        table.append((u, round(per, 3)))
        log(f"  {u:2d} units x {reports:3d}: {per:7.3f} pt per report ({per / _ref(u):.2f}x the 17 Pro)")
    table.sort()
    if len(table) < len(sizes) - 1 or any(b[1] <= a[1] for a, b in zip(table, table[1:])):
        log("  the sizes didn't grow steadily: something else moved on screen")
        return None
    if not plausible(table):
        log("  bigger reports moved less per unit: the pointer kept landing on the same spots "
            "(it snaps to icons and buttons); measure on a still, empty screen, such as a new note")
        return None
    return table


def plausible(table, slack=0.85):
    """iOS accelerates the pointer, so a bigger report moves at least about as far per
    unit as a smaller one (the 17 Pro table: 3.6 pt per unit at 1, 7.1 at 28). Measured
    on a home screen, where the pointer snaps to app icons, every size seemed to move
    the pointer one row of icons: 16 pt per unit at 1 down to 6 at 28, monotonic and
    wrong."""
    per = [p / u for u, p in table]
    return all(b >= a * slack for a, b in zip(per, per[1:]))


def calibrate(h, name=None, log=print, alive=None):
    """Measure the mirroring phone and save its table. Returns a one-line result.
    alive() runs before each size: it keeps the agent session open, so the browser
    mirror's keep-awake nudges stay off while the measurement runs."""
    name = name or phones.current()
    if not name:
        return "calibrate: no phone is mirroring"
    if alive:
        alive()
        time.sleep(1.0)  # a keep-awake nudge sent just before the session began lands first
    table = run(h, log=log, alive=alive)
    _to_start(h)
    if not table:
        return "calibrate: failed (the screen kept changing, or the pointer is off); kept the old table"
    phones.save_calibration(name, table)
    touch.refresh(force=True)
    k = sorted(p / _ref(u) for u, p in table)
    return (f"calibrated {name}: a report moves the pointer {k[0]:.2f}x-{k[-1]:.2f}x as far as "
            f"on the iPhone 17 Pro; drags and swipes now use this phone's table")
