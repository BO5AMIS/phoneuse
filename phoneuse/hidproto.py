"""What phoneuse's Bluetooth mouse and keyboard look like to the iPhone: the HID report
descriptor and key tables, shared by the Linux daemon (hid_daemon.py, Bluetooth
Classic) and the Windows service (windows/hid.py, Bluetooth LE). The same reports
travel over either link.
"""

# Report 1: boot-style keyboard (mods, reserved, 6 keys). Report 2: mouse (buttons, x, y, wheel).
REPORT_MAP = bytes.fromhex(
    "05010906a1018501050719e029e71500250175019508810295017508810395067508150025650507190029658100c0"
    + "05010902a10185020901a1000509190129031500250195037501810295017505810305010930093109381581257f750895038106c0c0"
    # Report 3: absolute pointer (buttons, X and Y 0..32767). Lands the AssistiveTouch
    # pointer at exact coordinates.
    + "05010902a10185030901a1000509190129031500250195037501810295017505810305010930093115002"
    "6ff7f751095028102c0c0"
    # Report 4: consumer control, one bit per key (CONSUMER_KEYS, in this order).
    + "050c0901a10185041500250175019501"
    + "09b88102" "09cf8102" "09408102" "0a21028102"  # Eject, Voice Command, Menu, AC Search
    + "95048103c0"
)

# Input reports: id -> payload length in bytes (without the id).
REPORT_SIZES = {1: 8, 2: 4, 3: 5, 4: 1}

# Consumer keys, by bit of report 4. Eject shows or hides the on-screen keyboard while
# a hardware keyboard (this one) is connected; Voice Command is the Siri key; Menu
# goes home; AC Search opens Spotlight.
CONSUMER_KEYS = {"eject": 0, "siri": 1, "home": 2, "search": 3}

# ---------- key tables (US layout) ----------
MODS = {"ctrl": 0x01, "shift": 0x02, "alt": 0x04, "gui": 0x08, "cmd": 0x08,
        "rctrl": 0x10, "rshift": 0x20, "ralt": 0x40, "rgui": 0x80}
NAMED = {"enter": 40, "return": 40, "esc": 41, "escape": 41, "backspace": 42, "tab": 43,
         "space": 44, "right": 79, "left": 80, "down": 81, "up": 82, "delete": 76,
         "home": 74, "end": 77, "pageup": 75, "pagedown": 78}
CHARS = {}
for _i, _c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    CHARS[_c] = (4 + _i, 0)
    CHARS[_c.upper()] = (4 + _i, 0x02)
for _i, _c in enumerate("1234567890"):
    CHARS[_c] = (30 + _i, 0)
for _i, _c in enumerate("!@#$%^&*()"):
    CHARS[_c] = (30 + _i, 0x02)
for _plain, _shifted, _code in [("-", "_", 45), ("=", "+", 46), ("[", "{", 47), ("]", "}", 48),
                                ("\\", "|", 49), (";", ":", 51), ("'", '"', 52), ("`", "~", 53),
                                (",", "<", 54), (".", ">", 55), ("/", "?", 56)]:
    CHARS[_plain] = (_code, 0)
    CHARS[_shifted] = (_code, 0x02)
CHARS[" "] = (44, 0)
CHARS["\n"] = (40, 0)
CHARS["\t"] = (43, 0)
BUTTONS = {"left": 1, "right": 2, "middle": 4}


def absolute(fx, fy, buttons):
    """Report 3 payload: buttons, then x and y as 0..32767."""
    x, y = (max(0, min(32767, round(float(v) * 32767))) for v in (fx, fy))
    return [buttons & 0x07, x & 0xFF, x >> 8, y & 0xFF, y >> 8]


class AbsoluteNudge:
    """iOS moves the pointer on an absolute report (3) only when its position differs
    from the previous absolute report's. After relative moves (2), the same spot again
    would leave the pointer where they took it (measured over Bluetooth LE: parking
    twice at one spot with a drag between left it at the drag's end). `before` returns
    a report 0.1 pt off to send first in that case."""

    def __init__(self):
        self.last = None  # the last absolute payload
        self.moved = False  # relative motion since then

    def before(self, report_id, payload):
        if report_id == 2:
            self.moved = self.moved or bool(payload[1] or payload[2])
            return []
        if report_id != 3:
            return []
        payload = list(payload)
        extra = []
        if self.moved and self.last is not None and payload[1:] == self.last[1:]:
            x = payload[1] | payload[2] << 8
            x = x - 8 if x >= 8 else x + 8
            extra.append([self.last[0], x & 0xFF, x >> 8, payload[3], payload[4]])
        self.last, self.moved = payload, False
        return extra


def key_code(name, mod_names=()):
    """(usage, modifier bits) for `key NAME [MOD..]`."""
    name, mods = name.lower(), 0
    for m in mod_names:
        mods |= MODS[m.lower()]
    if name in NAMED:
        return NAMED[name], mods
    code, shift = CHARS[name]
    return code, mods | shift


def passive(cmd, args):
    """Reports that change nothing on the phone by themselves: hovers, motion
    without a button, all keys up. While released (Linux daemon) they are dropped
    instead of reconnecting, so moving the mouse over the mirror doesn't bring the
    keyboard back."""
    try:
        if cmd == "a":
            return len(args) < 3 or int(args[2]) == 0
        if cmd in ("m", "md"):
            return (len(args) < 3 or int(args[2]) == 0) and (len(args) < 4 or int(args[3]) == 0)
        if cmd == "k":
            return int(args[0]) == 0 and not any(int(c) for c in args[1:])
        if cmd == "consumer":
            return args[-1:] == ["up"]
    except ValueError:
        return False
    return cmd in ("move", "corner", "up")
