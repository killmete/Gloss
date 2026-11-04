"""Finds a window by handle or title and reports where it is, in physical pixels.

Used by the capture adapters for follow-window mode. Every function tolerates a window that
vanishes between two calls and answers None or False instead of raising.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes

from gloss.core.contracts import Rect
from gloss.services.dpi import physical_pixels

_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_DWMWA_CLOAKED = 14

if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
    _hwnd = ctypes.c_void_p
    _user32.IsWindow.argtypes = [_hwnd]
    _user32.IsWindowVisible.argtypes = [_hwnd]
    _user32.IsIconic.argtypes = [_hwnd]
    _user32.GetWindowRect.argtypes = [_hwnd, ctypes.POINTER(wintypes.RECT)]
    _user32.GetWindowTextLengthW.argtypes = [_hwnd]
    _user32.GetWindowTextW.argtypes = [_hwnd, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowThreadProcessId.argtypes = [_hwnd, ctypes.POINTER(wintypes.DWORD)]
    _dwm.DwmGetWindowAttribute.argtypes = [_hwnd, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    _ENUM_WINDOWS = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, wintypes.LPARAM)
    _user32.EnumWindows.argtypes = [_ENUM_WINDOWS, wintypes.LPARAM]


def is_window(hwnd: int) -> bool:
    return sys.platform == "win32" and bool(_user32.IsWindow(hwnd))


def is_minimized(hwnd: int) -> bool:
    return sys.platform == "win32" and bool(_user32.IsIconic(hwnd))


def window_title(hwnd: int) -> str:
    if not is_window(hwnd):
        return ""
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _cloaked(hwnd: int) -> bool:
    value = wintypes.DWORD()
    result = _dwm.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(value), 4)
    return result == 0 and value.value != 0


def window_rect(hwnd: int) -> Rect | None:
    """The visible frame of the window in desktop pixels, or None if it is gone or minimised."""
    if not is_window(hwnd) or is_minimized(hwnd):
        return None
    box = wintypes.RECT()
    with physical_pixels():
        if _dwm.DwmGetWindowAttribute(
            hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(box), ctypes.sizeof(box)
        ):
            if not _user32.GetWindowRect(hwnd, ctypes.byref(box)):
                return None
    width, height = box.right - box.left, box.bottom - box.top
    if width <= 0 or height <= 0:
        return None
    return Rect(box.left, box.top, width, height)


def find_window(
    hwnd: int | None = None, title: str | None = None, *, exclude_own: bool = False
) -> int | None:
    """Resolve a window target. ``hwnd`` wins if it is a live window, else search by title.

    A title matches by case-insensitive substring among visible, uncloaked top-level windows,
    and an exact match is preferred. Minimised windows still count.
    """
    if sys.platform != "win32":
        return None
    if hwnd is not None and is_window(hwnd):
        return int(hwnd)
    if not title:
        return None
    wanted = title.casefold()
    own_pid = os.getpid()
    exact: list[int] = []
    partial: list[int] = []

    def visit(handle: int | None, _lparam: int) -> bool:
        if handle is None or not _user32.IsWindowVisible(handle) or _cloaked(handle):
            return True
        if exclude_own:
            owner = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
            if owner.value == own_pid:
                return True
        text = window_title(handle).casefold()
        if text == wanted:
            exact.append(int(handle))
        elif text and wanted in text:
            partial.append(int(handle))
        return True

    callback = _ENUM_WINDOWS(visit)
    _user32.EnumWindows(callback, 0)
    if exact:
        return exact[0]
    return partial[0] if partial else None
