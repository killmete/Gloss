"""Process DPI awareness. Gloss works in physical pixels everywhere (contracts/models.py).

``ensure_per_monitor_v2()`` is the helper ``app.py`` calls as its very first step, before
anything imports ``mss`` or Qt: ``mss`` sets its own (older, per-monitor v1) awareness when it
is first used, and Qt wants V2, and only the first caller wins. The call is safe to repeat.

``physical_pixels()`` is for library code that cannot know what ran first. It makes the
current thread per-monitor V2 for a moment, so a Win32 query returns real pixels, and then
restores the thread. It never touches the whole process.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import sys
from collections.abc import Iterator

log = logging.getLogger(__name__)

# DPI_AWARENESS_CONTEXT handle values from windef.h.
_CTX_UNAWARE = -1
_CTX_SYSTEM = -2
_CTX_PER_MONITOR = -3
_CTX_PER_MONITOR_V2 = -4

_user32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform == "win32" else None


def _bind() -> None:
    if _user32 is None:
        return
    handle = ctypes.c_void_p
    _user32.SetProcessDpiAwarenessContext.argtypes = [handle]
    _user32.SetProcessDpiAwarenessContext.restype = ctypes.c_int
    _user32.SetThreadDpiAwarenessContext.argtypes = [handle]
    _user32.SetThreadDpiAwarenessContext.restype = handle
    _user32.GetThreadDpiAwarenessContext.argtypes = []
    _user32.GetThreadDpiAwarenessContext.restype = handle
    _user32.AreDpiAwarenessContextsEqual.argtypes = [handle, handle]
    _user32.AreDpiAwarenessContextsEqual.restype = ctypes.c_int


_bind()


def _ctx(value: int) -> ctypes.c_void_p:
    return ctypes.c_void_p(value)


def current_awareness() -> str:
    """One of ``per_monitor_v2``, ``per_monitor``, ``system``, ``unaware`` or ``unknown``."""
    if _user32 is None:
        return "unknown"
    try:
        current = _user32.GetThreadDpiAwarenessContext()
        for label, value in (
            ("per_monitor_v2", _CTX_PER_MONITOR_V2),
            ("per_monitor", _CTX_PER_MONITOR),
            ("system", _CTX_SYSTEM),
            ("unaware", _CTX_UNAWARE),
        ):
            if _user32.AreDpiAwarenessContextsEqual(current, _ctx(value)):
                return label
    except (OSError, AttributeError):
        log.debug("DPI awareness query failed", exc_info=True)
    return "unknown"


def ensure_per_monitor_v2() -> str:
    """Make this process per-monitor-DPI-aware V2, if nothing has chosen yet.

    Returns the awareness in force afterwards (see ``current_awareness``). If another
    caller already fixed the awareness, Windows refuses the change, and the result tells
    the caller what it got instead. It never raises.
    """
    if _user32 is None:
        return "unknown"
    try:
        if current_awareness() != "per_monitor_v2":
            _user32.SetProcessDpiAwarenessContext(_ctx(_CTX_PER_MONITOR_V2))
    except (OSError, AttributeError):
        log.debug("could not set per-monitor DPI awareness", exc_info=True)
    result = current_awareness()
    if result != "per_monitor_v2":
        log.warning("Process DPI awareness is %s, not per_monitor_v2", result)
    return result


@contextlib.contextmanager
def physical_pixels() -> Iterator[None]:
    """Run a block with this thread per-monitor V2, then put the thread back."""
    previous = None
    if _user32 is not None:
        try:
            previous = _user32.SetThreadDpiAwarenessContext(_ctx(_CTX_PER_MONITOR_V2))
        except (OSError, AttributeError):
            previous = None
    try:
        yield
    finally:
        if previous:
            with contextlib.suppress(OSError, AttributeError):
                _user32.SetThreadDpiAwarenessContext(previous)
