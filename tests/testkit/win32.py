"""Asks Windows itself, so checks do not trust the adapter's own account of things.

Only meaningful on Windows. Importing this module is safe anywhere.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from typing import Any

from gloss.core.contracts import Rect

ATTACHED_TO_DESKTOP = 0x1
ENUM_CURRENT_SETTINGS = -1


class _DisplayDevice(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("DeviceName", wintypes.WCHAR * 32),
        ("DeviceString", wintypes.WCHAR * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", wintypes.WCHAR * 128),
        ("DeviceKey", wintypes.WCHAR * 128),
    ]


class _DevMode(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", wintypes.WCHAR * 32),
        ("dmSpecVersion", wintypes.WORD),
        ("dmDriverVersion", wintypes.WORD),
        ("dmSize", wintypes.WORD),
        ("dmDriverExtra", wintypes.WORD),
        ("dmFields", wintypes.DWORD),
        ("dmPositionX", ctypes.c_long),
        ("dmPositionY", ctypes.c_long),
        ("dmDisplayOrientation", wintypes.DWORD),
        ("dmDisplayFixedOutput", wintypes.DWORD),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", wintypes.WCHAR * 32),
        ("dmLogPixels", wintypes.WORD),
        ("dmBitsPerPel", wintypes.DWORD),
        ("dmPelsWidth", wintypes.DWORD),
        ("dmPelsHeight", wintypes.DWORD),
        ("dmDisplayFlags", wintypes.DWORD),
        ("dmDisplayFrequency", wintypes.DWORD),
        ("dmICMMethod", wintypes.DWORD),
        ("dmICMIntent", wintypes.DWORD),
        ("dmMediaType", wintypes.DWORD),
        ("dmDitherType", wintypes.DWORD),
        ("dmReserved1", wintypes.DWORD),
        ("dmReserved2", wintypes.DWORD),
        ("dmPanningWidth", wintypes.DWORD),
        ("dmPanningHeight", wintypes.DWORD),
    ]


def os_monitors() -> list[tuple[str, Rect]]:
    """Each attached monitor's device name and physical desktop rectangle.

    ``EnumDisplaySettings`` reports real pixels whatever this process's DPI awareness is.
    """
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    found: list[tuple[str, Rect]] = []
    index = 0
    while True:
        device = _DisplayDevice()
        device.cb = ctypes.sizeof(device)
        if not user32.EnumDisplayDevicesW(None, index, ctypes.byref(device), 0):
            break
        index += 1
        if not device.StateFlags & ATTACHED_TO_DESKTOP:
            continue
        mode = _DevMode()
        mode.dmSize = ctypes.sizeof(mode)
        if user32.EnumDisplaySettingsW(
            device.DeviceName, ENUM_CURRENT_SETTINGS, ctypes.byref(mode)
        ):
            rect = Rect(mode.dmPositionX, mode.dmPositionY, mode.dmPelsWidth, mode.dmPelsHeight)
            found.append((device.DeviceName, rect))
    return found


def own_windows(_adapter: Any = None) -> list[tuple[int, bool, int]]:
    """(hwnd, visible, display affinity) for every top-level window of this process."""
    if sys.platform != "win32":
        return []
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    pid = os.getpid()
    found: list[tuple[int, bool, int]] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd: int, _lparam: int) -> bool:
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            affinity = wintypes.DWORD()
            user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity))
            found.append((int(hwnd), bool(user32.IsWindowVisible(hwnd)), int(affinity.value)))
        return True

    user32.EnumWindows(callback_type(visit), 0)
    return found
