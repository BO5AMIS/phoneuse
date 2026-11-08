"""The mirror window on Windows: the browser mirror in a window of its own (Edge or
Chrome in app mode, no tabs or address bar), sized like the phone. It is an
ordinary window: move it, resize it (the picture stays aspect-fit), close it;
`phoneuse view` opens it again."""
import ctypes
import os
import shutil
import subprocess

from ..config import CFG, PHONE_ASPECT, runtime_path

BROWSERS = [
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
]
BEZEL = 30 / 498  # as screen.BEZEL


def browser():
    for p in BROWSERS:
        p = os.path.expandvars(p)
        if os.path.exists(p):
            return p
    return shutil.which("msedge") or shutil.which("chrome")


def window_size():
    """Phone-shaped, about 85% of the work area's height (in CSS pixels)."""
    try:
        user32 = ctypes.WinDLL("user32")
        dpi = user32.GetDpiForSystem() or 96
        height = user32.GetSystemMetrics(17)  # SM_CYFULLSCREEN: the work area's height
        height = int(height * 96 / dpi * 0.85)
    except (OSError, AttributeError):
        height = 900
    dw, dh = 1 + 2 * BEZEL, 1 / PHONE_ASPECT + 2 * BEZEL
    return int(height * dw / dh), height + 30  # + the title bar


def open_window(url=None):
    """Open the mirror window. Returns False when no Chromium browser is installed
    (the caller then opens the page in the default browser)."""
    url = url or f"http://localhost:{CFG['web_port']}/"
    exe = browser()
    if not exe:
        return False
    w, h = window_size()
    # A profile of its own makes it a separate browser instance: the window gets
    # the requested size and doesn't join the user's browsing session.
    profile = runtime_path("window-profile")
    subprocess.Popen([exe, f"--app={url}", f"--window-size={w},{h}", f"--user-data-dir={profile}",
                      "--no-first-run", "--no-default-browser-check", "--disable-features=Translate"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    return True
