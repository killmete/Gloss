"""The one place that lists monitors and gives them stable IDs.

``mss`` and ``windows-capture`` number monitors differently (docs/research/capture.md, "Monitor
index mismatch"), so every capture adapter lists monitors through this module and maps the
Windows device name (for example ``\\\\.\\DISPLAY1``) to its own library's index. A bare index
never goes into config or a ``CaptureTarget``.

Bounds are real pixels at real desktop positions, read with ``EnumDisplaySettings`` and so
independent of the process's DPI awareness. ``dpi_scale`` is read in a per-monitor V2 thread
context for the same reason.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from collections.abc import Sequence
from ctypes import wintypes
from dataclasses import dataclass

from gloss.core.contracts import AdapterUnavailable, MonitorInfo, Rect
from gloss.services.dpi import physical_pixels

log = logging.getLogger(__name__)

_MONITORINFOF_PRIMARY = 0x1
_ENUM_CURRENT_SETTINGS = -1
_MDT_EFFECTIVE_DPI = 0
_BASE_DPI = 96.0


class _MonitorInfoEx(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
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


_ENUM_PROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
)

if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _shcore = ctypes.WinDLL("shcore", use_last_error=True)
    _user32.EnumDisplayMonitors.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        _ENUM_PROC,
        wintypes.LPARAM,
    ]
    _user32.EnumDisplayMonitors.restype = wintypes.BOOL
    _user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MonitorInfoEx)]
    _user32.GetMonitorInfoW.restype = wintypes.BOOL
    _user32.EnumDisplaySettingsW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(_DevMode),
    ]
    _user32.EnumDisplaySettingsW.restype = wintypes.BOOL
    _shcore.GetDpiForMonitor.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.POINTER(wintypes.UINT),
        ctypes.POINTER(wintypes.UINT),
    ]
    _shcore.GetDpiForMonitor.restype = ctypes.c_long


@dataclass(frozen=True, slots=True)
class _Raw:
    device: str
    rect: Rect
    primary: bool
    dpi_scale: float


def _physical_rect(device: str, fallback: wintypes.RECT) -> Rect:
    mode = _DevMode()
    mode.dmSize = ctypes.sizeof(mode)
    if _user32.EnumDisplaySettingsW(device, _ENUM_CURRENT_SETTINGS, ctypes.byref(mode)):
        if mode.dmPelsWidth and mode.dmPelsHeight:
            return Rect(
                mode.dmPositionX, mode.dmPositionY, int(mode.dmPelsWidth), int(mode.dmPelsHeight)
            )
    return Rect(
        fallback.left,
        fallback.top,
        max(0, fallback.right - fallback.left),
        max(0, fallback.bottom - fallback.top),
    )


def _enumerate() -> list[_Raw]:
    """Monitors in ``EnumDisplayMonitors`` order, which is also windows-capture's order."""
    if sys.platform != "win32":
        raise AdapterUnavailable("monitor listing needs Windows")
    found: list[_Raw] = []

    def visit(hmon: int | None, _hdc: int, _rect: object, _lparam: int) -> bool:
        info = _MonitorInfoEx()
        info.cbSize = ctypes.sizeof(info)
        if not _user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return True
        dpi_x, dpi_y = wintypes.UINT(), wintypes.UINT()
        scale = 1.0
        if _shcore.GetDpiForMonitor(hmon, _MDT_EFFECTIVE_DPI, dpi_x, dpi_y) == 0 and dpi_x.value:
            scale = dpi_x.value / _BASE_DPI
        found.append(
            _Raw(
                info.szDevice,
                _physical_rect(info.szDevice, info.rcMonitor),
                bool(info.dwFlags & _MONITORINFOF_PRIMARY),
                scale,
            )
        )
        return True

    callback = _ENUM_PROC(visit)
    with physical_pixels():
        if not _user32.EnumDisplayMonitors(None, None, callback, 0):
            raise AdapterUnavailable("Windows could not list the monitors")
    if not found:
        raise AdapterUnavailable("Windows reports no monitors")
    return found


def list_monitors() -> tuple[MonitorInfo, ...]:
    """Connected monitors, with Windows device names as IDs and physical bounds."""
    return tuple(MonitorInfo(m.device, m.rect, m.dpi_scale, m.primary) for m in _enumerate())


def windows_capture_order() -> tuple[str, ...]:
    """Monitor IDs in the order ``windows-capture`` counts them, as a 1-based ``monitor_index``.

    Only the ``wgc`` adapter should need this. Callers must not store the numbers.
    """
    return tuple(m.device for m in _enumerate())


def primary_monitor(monitors: Sequence[MonitorInfo]) -> MonitorInfo:
    for m in monitors:
        if m.is_primary:
            return m
    raise AdapterUnavailable("no primary monitor found")


def find_monitor(monitors: Sequence[MonitorInfo], monitor_id: str | None) -> MonitorInfo:
    """The monitor with this ID, or the primary one for None. Unknown IDs are unavailable."""
    if monitor_id is None:
        return primary_monitor(monitors)
    for m in monitors:
        if m.monitor_id == monitor_id:
            return m
    raise AdapterUnavailable("that monitor is not connected")


def intersection(a: Rect, b: Rect) -> Rect | None:
    """The overlap of two rectangles, or None if they share no pixels."""
    left, top = max(a.x, b.x), max(a.y, b.y)
    right, bottom = min(a.right, b.right), min(a.bottom, b.bottom)
    if right <= left or bottom <= top:
        return None
    return Rect(left, top, right - left, bottom - top)


def monitor_with_most_overlap(monitors: Sequence[MonitorInfo], rect: Rect) -> MonitorInfo | None:
    """The monitor that holds most of ``rect``, or None if it is on no monitor."""
    best: MonitorInfo | None = None
    best_area = 0
    for m in monitors:
        overlap = intersection(m.bounds, rect)
        if overlap is not None and overlap.area > best_area:
            best, best_area = m, overlap.area
    return best


def virtual_desktop(monitors: Sequence[MonitorInfo]) -> Rect:
    """The smallest rectangle holding every monitor."""
    left = min(m.bounds.x for m in monitors)
    top = min(m.bounds.y for m in monitors)
    right = max(m.bounds.right for m in monitors)
    bottom = max(m.bounds.bottom for m in monitors)
    return Rect(left, top, right - left, bottom - top)
