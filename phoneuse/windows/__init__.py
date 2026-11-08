"""The Windows side of phoneuse: things that need Windows APIs (Bluetooth LE through
the Windows Runtime, job objects, DPAPI, notifications)."""
import ctypes
import sys


def precise_timers():
    """Ask Windows for 1 ms timer resolution in this process (the default is 15.6 ms,
    which would stretch 8 ms drag ticks and asyncio timeouts)."""
    if sys.platform == "win32":
        try:
            ctypes.WinDLL("winmm").timeBeginPeriod(1)
        except OSError:
            pass
