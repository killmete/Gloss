"""Keeps Gloss's own windows out of screen captures.

Call ``exclude_from_capture(hwnd)`` for every top-level Gloss window after it is created or
shown, and again if Qt recreates the native window (for example when click-through flags are
toggled). Windows 10 version 2004 and newer then draw the window on the monitor but leave it
out of every capture API (measured with GDI, DXGI and WGC in docs/research/capture.md).
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

log = logging.getLogger(__name__)

WDA_NONE = 0x0
WDA_MONITOR = 0x1
WDA_EXCLUDEFROMCAPTURE = 0x11

if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    _user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
    _user32.GetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowDisplayAffinity.restype = wintypes.BOOL


def display_affinity(hwnd: int) -> int | None:
    """The window's current display affinity, or None if Windows would not say."""
    if sys.platform != "win32":
        return None
    value = wintypes.DWORD()
    if not _user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(value)):
        return None
    return int(value.value)


def is_excluded(hwnd: int) -> bool:
    return display_affinity(hwnd) == WDA_EXCLUDEFROMCAPTURE


def exclude_from_capture(hwnd: int) -> bool:
    """Hide a window of this process from screen capture. Returns True if it took effect.

    Never raises. The window must be a top-level window created by this process.
    """
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        if not _user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
            log.warning(
                "Could not hide a Gloss window from screen capture (error %s)",
                ctypes.get_last_error(),
            )
            return False
    except (OSError, ctypes.ArgumentError):
        log.warning("Could not hide a Gloss window from screen capture", exc_info=True)
        return False
    return is_excluded(hwnd)
