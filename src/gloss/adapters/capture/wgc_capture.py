"""Screen capture with Windows Graphics Capture, through ``windows-capture``.

Primary backend for window mode (a covered, moved or resized window keeps working, with no
rectangle polling) and the fallback for monitor and region modes when ``mss`` shows black.

WGC pushes a frame only when the content changes, so a static window emits nothing after its
first frame. This adapter keeps the newest frame and hands it out again with ``fresh=False``.
The library's callback runs on its own native thread: it copies just the area needed, as RGB,
into a per-session slot and returns at once. ``minimum_update_interval`` is set from ``fps_max``
so the callback is not flooded.

Monitors are numbered by the library from 1 in ``EnumDisplayMonitors`` order, and ``None`` is
NOT the primary monitor, so this adapter always passes an explicit index obtained from the
shared monitor helper. A frame whose size disagrees with the monitor refuses to open.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from gloss.core.contracts import (
    AdapterContext,
    AdapterUnavailable,
    CaptureTarget,
    Frame,
    GlossError,
    Health,
    MonitorInfo,
    Rect,
)
from gloss.core.registry import register_adapter
from gloss.services.capture_common import bgra_to_rgb, parse_settings, resolve_screen_target
from gloss.services.dpi import ensure_per_monitor_v2
from gloss.services.monitors import (
    list_monitors,
    monitor_with_most_overlap,
    windows_capture_order,
)
from gloss.services.window_lookup import find_window, is_minimized, is_window, window_rect

log = logging.getLogger(__name__)

_POLL_S = 0.01


@dataclass(slots=True)
class _Slot:
    """What the native callback hands to ``grab``. One per open session."""

    lock: threading.Lock
    crop: Rect | None  # area to keep, in the captured surface's pixels (None = all of it)
    image: np.ndarray | None = None
    size: tuple[int, int] = (0, 0)  # the whole surface (width, height)
    seq: int = 0
    stamp: float = 0.0
    closed: bool = False
    error: str = ""
    stopped: bool = False  # set by close(): late callbacks do nothing


def _make_handlers(slot: _Slot) -> tuple[Any, Any]:
    def on_frame_arrived(frame: Any, _control: Any) -> None:
        if slot.stopped:
            return
        try:
            buffer = frame.frame_buffer
            height, width = buffer.shape[:2]
            if slot.crop is not None:
                c = slot.crop
                x1, y1 = min(c.right, width), min(c.bottom, height)
                if c.x >= x1 or c.y >= y1:
                    return
                buffer = buffer[c.y : y1, c.x : x1]
            rgb = bgra_to_rgb(buffer)
        except Exception as exc:  # never let an exception escape into the native thread
            slot.error = f"a frame could not be read ({type(exc).__name__})"
            return
        previous = slot.image  # only this thread ever replaces it
        if previous is not None and previous.shape == rgb.shape and np.array_equal(previous, rgb):
            return  # WGC reports any change on the monitor; our area is the same, so not fresh
        with slot.lock:
            slot.image, slot.size = rgb, (width, height)
            slot.seq += 1
            slot.stamp = time.monotonic()
            slot.error = ""

    def on_closed() -> None:
        slot.closed = True

    return on_frame_arrived, on_closed


@register_adapter("capture", "wgc")
class WgcCapture:
    name = "wgc"
    REQUIRES_CONTRACT = ">=1.0,<2.0"

    def __init__(self, context: AdapterContext) -> None:
        self._settings = parse_settings(context.settings)
        self._control: Any = None
        self._session: Any = None  # keeps the library object alive while it captures
        self._slot: _Slot | None = None
        self._target: CaptureTarget | None = None
        self._origin = Rect(0, 0, 0, 0)  # desktop position of the slot image (screen modes)
        self._hwnd: int | None = None
        self._dpi = 1.0
        self._last_seq = 0
        self._last_stamp = 0.0
        self._last_origin = Rect(0, 0, 0, 0)
        self._monitors: tuple[MonitorInfo, ...] = ()
        self._health = Health("down", "capture is not open")
        self._health_lock = threading.Lock()

    # -- port -----------------------------------------------------------------------------

    def list_monitors(self) -> Sequence[MonitorInfo]:
        ensure_per_monitor_v2()
        return list_monitors()

    def open(self, target: CaptureTarget) -> None:
        self.close()
        ensure_per_monitor_v2()
        monitors = tuple(list_monitors())
        monitor_index: int | None = None
        hwnd: int | None = None
        crop: Rect | None = None
        origin = Rect(0, 0, 0, 0)
        dpi = 1.0
        monitor: MonitorInfo | None = None
        if target.mode == "window":
            hwnd = find_window(target.hwnd, target.window_title)
            if hwnd is None:
                raise AdapterUnavailable("that window was not found")
            rect = window_rect(hwnd)
            monitor = monitor_with_most_overlap(monitors, rect) if rect else None
            dpi = monitor.dpi_scale if monitor else 1.0
        else:
            resolved = resolve_screen_target(target, monitors)
            monitor, origin, dpi = resolved.monitor, resolved.region, resolved.monitor.dpi_scale
            bounds = monitor.bounds
            inside = (
                bounds.x <= origin.x
                and bounds.y <= origin.y
                and origin.right <= bounds.right
                and origin.bottom <= bounds.bottom
            )
            if not inside:
                raise AdapterUnavailable("wgc cannot capture a region across several monitors")
            crop = Rect(origin.x - bounds.x, origin.y - bounds.y, origin.w, origin.h)
            try:
                monitor_index = windows_capture_order().index(monitor.monitor_id) + 1
            except ValueError as exc:
                raise AdapterUnavailable("that monitor is not available to wgc") from exc
        slot = _Slot(threading.Lock(), crop)
        try:
            import windows_capture  # heavy: imports OpenCV, so only when needed

            session = windows_capture.WindowsCapture(
                cursor_capture=False,
                minimum_update_interval=self._settings.update_interval_ms,
                monitor_index=monitor_index,
                window_hwnd=hwnd,
            )
            on_frame, on_closed = _make_handlers(slot)
            session.event(_named(on_frame, "on_frame_arrived"))
            session.event(_named(on_closed, "on_closed"))
            control = session.start_free_threaded()
        except Exception as exc:
            raise AdapterUnavailable("Windows Graphics Capture could not start") from exc
        self._session, self._control, self._slot, self._target = session, control, slot, target
        self._origin, self._hwnd, self._dpi, self._monitors = origin, hwnd, dpi, monitors
        self._last_seq, self._last_stamp, self._last_origin = 0, 0.0, origin
        self._set_health(Health("degraded", "waiting for the first frame"))
        self._wait_for_first_frame(slot, monitor)

    def grab(self) -> Frame | None:
        slot = self._slot
        if slot is None or self._target is None:
            return None
        try:
            return self._grab(slot)
        except GlossError:
            raise
        except Exception as exc:
            self._set_health(Health("degraded", "a frame could not be delivered"))
            log.debug("wgc grab failed: %s", type(exc).__name__)
            return None

    def close(self) -> None:
        slot, control = self._slot, self._control
        self._slot = self._control = self._session = self._target = self._hwnd = None
        if slot is not None:
            slot.stopped = True
        if control is not None:
            try:
                control.stop()
            except Exception:
                log.debug("wgc stop failed", exc_info=True)
        self._set_health(Health("down", "capture is not open"))

    def health(self) -> Health:
        with self._health_lock:
            return self._health

    # -- internals ------------------------------------------------------------------------

    def _set_health(self, health: Health) -> None:
        with self._health_lock:
            self._health = health

    def _wait_for_first_frame(self, slot: _Slot, monitor: MonitorInfo | None) -> None:
        deadline = time.monotonic() + self._settings.first_frame_timeout_ms / 1000
        while time.monotonic() < deadline and slot.seq == 0 and not slot.closed:
            time.sleep(_POLL_S)
        if slot.seq == 0:
            if slot.closed:
                self.close()
                raise AdapterUnavailable("Windows Graphics Capture stopped straight away")
            return  # a static or minimised window may deliver nothing yet; grab() reports it
        if monitor is not None and self._hwnd is None:
            width, height = slot.size
            if (width, height) != (monitor.bounds.w, monitor.bounds.h):
                self.close()
                raise AdapterUnavailable("wgc and Windows disagree about that monitor's size")

    def _window_origin(self) -> Rect | None:
        assert self._hwnd is not None
        if not is_window(self._hwnd):
            self._set_health(Health("down", "the captured window was closed"))
            return None
        if is_minimized(self._hwnd):
            self._set_health(Health("degraded", "the captured window is minimised"))
            return None
        return window_rect(self._hwnd)

    def _grab(self, slot: _Slot) -> Frame | None:
        if slot.closed:
            self._set_health(Health("down", "Windows stopped the capture"))
            return None
        origin = self._origin
        window_area: Rect | None = None
        if self._hwnd is not None:
            window_area = self._window_origin()
            if window_area is None:
                return None
        with slot.lock:
            image, seq, stamp = slot.image, slot.seq, slot.stamp
        if image is None:
            detail = slot.error or "waiting for the first frame"
            self._set_health(Health("degraded", detail))
            return None
        fresh = seq != self._last_seq
        if window_area is not None:
            origin = Rect(window_area.x, window_area.y, image.shape[1], image.shape[0])
            monitor = monitor_with_most_overlap(self._monitors, origin)
            self._dpi = monitor.dpi_scale if monitor else self._dpi
        else:
            origin = Rect(origin.x, origin.y, image.shape[1], image.shape[0])
        moved = origin != self._last_origin
        now = time.monotonic()
        stamp = max(stamp if fresh else now, self._last_stamp)
        self._last_seq, self._last_stamp, self._last_origin = seq, stamp, origin
        self._set_health(Health("ok", "capturing with Windows Graphics Capture"))
        # A window that only moved has the same pixels, but the change detector must know.
        return Frame(image.copy(), self.name, origin, stamp, self._dpi, fresh=fresh or moved)


def _named(function: Any, name: str) -> Any:
    """windows-capture picks the handler to replace by the function's ``__name__``."""
    function.__name__ = name
    return function
