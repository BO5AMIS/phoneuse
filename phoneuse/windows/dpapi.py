"""Windows DPAPI: encrypt small secrets so that only this Windows user (on this PC)
can decrypt them. Used for the saved iPhone passcode (unlock.py)."""
import ctypes
from ctypes import wintypes

crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
CRYPTPROTECT_UI_FORBIDDEN = 0x1
ENTROPY = b"phoneuse passcode"


class Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _take(blob):
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        kernel32.LocalFree(blob.pbData)


def protect(data):
    src, _keep = _blob(data)
    ent, _keep2 = _blob(ENTROPY)
    out = Blob()
    if not crypt32.CryptProtectData(ctypes.byref(src), "phoneuse", ctypes.byref(ent), None, None,
                                    CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise ctypes.WinError(ctypes.get_last_error())
    return _take(out)


def unprotect(data):
    src, _keep = _blob(data)
    ent, _keep2 = _blob(ENTROPY)
    out = Blob()
    if not crypt32.CryptUnprotectData(ctypes.byref(src), None, ctypes.byref(ent), None, None,
                                      CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise ctypes.WinError(ctypes.get_last_error())
    return _take(out)
