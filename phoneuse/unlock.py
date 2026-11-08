"""Unlocking the iPhone with its passcode, only with the owner's approval each time.

The owner saves the passcode once, in the browser mirror's settings panel. On
Linux it is kept in ~/.config/phoneuse/passcode, readable only by the owner (mode
600 in a 700 directory); on Windows in %APPDATA%/phoneuse/passcode.dpapi,
encrypted with DPAPI so that only the same Windows user can read it. It is never
written to a log. An agent that meets the lock screen asks for an unlock
(`phoneuse unlock`). The owner approves or denies that request on the page or in
a desktop notification. On approval this module types the passcode on the
phone's keyboard, once: if the phone is still locked afterwards, it reports the
failure and does not retry, so a wrong code can't pile up failed attempts.
The passcode never reaches the agent, its output, or the logs.
"""
import os
import re
import time

from .config import IS_WINDOWS, config_dir
from .hidclient import Hid

PASSCODE_FILE = os.path.join(config_dir(), "passcode.dpapi" if IS_WINDOWS else "passcode")
MAX_ATTEMPTS_PER_HOUR = 3
_attempts = []


def saved():
    try:
        return os.path.getsize(PASSCODE_FILE) > 0
    except OSError:
        return False


def save(code):
    code = code.strip()
    if not (4 <= len(code) <= 32) or not code.isprintable():
        raise ValueError("a passcode is 4 to 32 characters")
    d = os.path.dirname(PASSCODE_FILE)
    os.makedirs(d, mode=0o700, exist_ok=True)
    tmp = PASSCODE_FILE + ".tmp"
    if IS_WINDOWS:
        from .windows import dpapi
        with open(tmp, "wb") as f:
            f.write(dpapi.protect(code.encode()))
        os.replace(tmp, PASSCODE_FILE)
        return
    os.chmod(d, 0o700)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(code)
    os.chmod(tmp, 0o600)
    os.replace(tmp, PASSCODE_FILE)


def forget():
    try:
        os.unlink(PASSCODE_FILE)
    except FileNotFoundError:
        pass


def _read():
    try:
        st = os.stat(PASSCODE_FILE)
    except FileNotFoundError:
        raise RuntimeError("no passcode saved (browser mirror → settings)")
    if IS_WINDOWS:
        from .windows import dpapi
        with open(PASSCODE_FILE, "rb") as f:
            try:
                code = dpapi.unprotect(f.read()).decode().strip()
            except OSError:
                raise RuntimeError("the saved passcode can't be decrypted (saved by another Windows user?); "
                                   "save it again in the browser mirror's settings")
        if not code:
            raise RuntimeError("no passcode saved (browser mirror → settings)")
        return code
    if st.st_mode & 0o077:
        raise RuntimeError(f"{PASSCODE_FILE} is readable by others; refusing to use it (chmod 600)")
    with open(PASSCODE_FILE) as f:
        code = f.read().strip()
    if not code:
        raise RuntimeError("no passcode saved (browser mirror → settings)")
    return code


def locked(screen_text):
    """True when the screen shows the lock screen (its big clock under the date)
    or the passcode prompt."""
    lines = screen_text()
    words = " ".join(t for _, _, t in lines).lower()
    if "passcode" in words or ("emergency" in words and "cancel" in words):
        return True
    clock = [y for x, y, t in lines if re.fullmatch(r"\d{1,2}:\d{2}", t.strip()) and 0.1 < y < 0.32 and 0.3 < x < 0.7]
    date = [y for x, y, t in lines if re.match(r"(mon|tue|wed|thu|fri|sat|sun)", t.strip().lower()) and y < 0.2]
    return bool(clock and date and min(date) < max(clock))


def unlock(screen_text):
    """Enter the saved passcode once. Returns a short result for the agent."""
    now = time.time()
    _attempts[:] = [t for t in _attempts if now - t < 3600]
    if len(_attempts) >= MAX_ATTEMPTS_PER_HOUR:
        return "refused: 3 unlock attempts in the last hour; ask the owner to unlock the phone"
    if not locked(screen_text):
        return "not locked"
    code = _read()
    _attempts.append(now)
    h = Hid()
    h.cmd("key space")  # shows the passcode entry (hardware keyboards type into it)
    time.sleep(0.6)
    submits_itself = code.isdigit() and len(code) in (4, 6)  # numeric codes submit on the last digit
    h.cmd("type " + code)
    del code
    if not submits_itself:
        h.cmd("key enter")
    time.sleep(1.5)
    return "still locked: the passcode didn't work; not retrying" if locked(screen_text) else "unlocked"
