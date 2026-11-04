"""Screen capture with ``mss`` (GDI). Primary backend for monitor and region modes.

Window mode here is "follow the window": the window's rectangle is looked up again on every
grab, so a moved or resized window keeps working. A minimised or closed window gives no frame
and an honest ``health()``. Unlike ``wgc`` it captures whatever is on top of the window.

``fresh`` is False when the raw pixels equal the previous grab, which lets the change detector
skip a static screen at almost no cost. Black frames come back like any other frame; the
shared check for them lives in ``gloss.services.black_frame``.

``open``, ``grab`` and ``close`` run on the capture worker thread, which also owns the ``mss``
handle (its GDI device contexts belong to the creating thread).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence
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
    TransientError,
)
from gloss.core.registry import register_adapter
from gloss.services.capture_common import bgra_to_rgb, parse_settings, resolve_screen_target
from gloss.services.dpi import ensure_per_monitor_v2
from gloss.services.monitors import (
    intersection,
    list_monitors,
    monitor_with_most_overlap,
    virtual_desktop,
)
from gloss.services.window_lookup import find_window, is_minimized, is_window, window_rect

log = logging.getLogger(__name__)

_MONITOR_REFRESH_S = 2.0


@register_adapter("capture", "mss")
class MssCapture:
    name = "mss"
    REQUIRES_CONTRACT = ">=1.0,<2.0"

    def __init__(self, context: AdapterContext) -> None:
        self._settings = parse_settings(context.settings)
        self._sct: Any = None
        self._target: CaptureTarget | None = None
        self._region: Rect | None = None  # fixed for monitor and region modes
        self._dpi = 1.0
        self._hwnd: int | None = None
        self._monitors: tuple[MonitorInfo, ...] = ()
        self._monitors_at = 0.0
        self._prev_raw: np.ndarray | None = None
        self._prev_rgb: np.ndarray | None = None
        self._prev_region: Rect | None = None
        self._health = Health("down", "capture is not open")
        self._health_lock = threading.Lock()

    # -- port -----------------------------------------------------------------------------

    def list_monitors(self) -> Sequence[MonitorInfo]:
        ensure_per_monitor_v2()
        return list_monitors()

    def open(self, target: CaptureTarget) -> None:
        self.close()
        ensure_per_monitor_v2()  # before mss is imported, so mss does not pick v1 first
        monitors = tuple(list_monitors())
        region: Rect | None = None
        dpi = 1.0
        hwnd: int | None = None
        if target.mode == "window":
            hwnd = find_window(target.hwnd, target.window_title)
            if hwnd is None:
                raise AdapterUnavailable("that window was not found")
        else:
            resolved = resolve_screen_target(target, monitors)
            region, dpi = resolved.region, resolved.monitor.dpi_scale
        try:
            import mss

            sct = mss.MSS()
        except Exception as exc:
            raise AdapterUnavailable("the mss screen capture library could not start") from exc
        self._sct, self._target, self._region, self._dpi, self._hwnd = (
            sct,
            target,
            region,
            dpi,
            hwnd,
        )
        self._monitors, self._monitors_at = monitors, time.monotonic()
        self._prev_raw = self._prev_rgb = self._prev_region = None
        self._set_health(Health("ok", "capturing with mss"))

    def grab(self) -> Frame | None:
        if self._sct is None or self._target is None:
            return None
        try:
            return self._grab()
        except GlossError:
            raise
        except Exception as exc:
            self._set_health(Health("degraded", "a screen grab failed"))
            raise TransientError("mss could not grab the screen") from exc

    def close(self) -> None:
        sct, self._sct = self._sct, None
        self._target = self._region = self._hwnd = None
        self._prev_raw = self._prev_rgb = self._prev_region = None
        if sct is not None:
            try:
                sct.close()
            except Exception:
                log.debug("mss close failed", exc_info=True)
        self._set_health(Health("down", "capture is not open"))

    def health(self) -> Health:
        with self._health_lock:
            return self._health

    # -- internals ------------------------------------------------------------------------

    def _set_health(self, health: Health) -> None:
        with self._health_lock:
            self._health = health

    def _fresh_monitors(self) -> tuple[MonitorInfo, ...]:
        now = time.monotonic()
        if now - self._monitors_at > _MONITOR_REFRESH_S:
            self._monitors, self._monitors_at = tuple(list_monitors()), now
        return self._monitors

    def _window_area(self) -> tuple[Rect, float] | None:
        """Where the followed window is now, clipped to the desktop, or None."""
        assert self._hwnd is not None
        if not is_window(self._hwnd):
            self._set_health(Health("down", "the captured window was closed"))
            return None
        if is_minimized(self._hwnd):
            self._set_health(Health("degraded", "the captured window is minimised"))
            return None
        rect = window_rect(self._hwnd)
        monitors = self._fresh_monitors()
        clipped = intersection(rect, virtual_desktop(monitors)) if rect else None
        monitor = monitor_with_most_overlap(monitors, clipped) if clipped else None
        if clipped is None or monitor is None:
            self._set_health(Health("degraded", "the captured window is off screen"))
            return None
        self._set_health(Health("ok", "capturing with mss"))
        return clipped, monitor.dpi_scale

    def _grab(self) -> Frame | None:
        if self._hwnd is not None:
            area = self._window_area()
            if area is None:
                return None
            region, dpi = area
        else:
            assert self._region is not None
            region, dpi = self._region, self._dpi
        shot = self._sct.grab(
            {"left": region.x, "top": region.y, "width": region.w, "height": region.h}
        )
        stamp = time.monotonic()
        region = Rect(region.x, region.y, int(shot.width), int(shot.height))
        raw = np.frombuffer(shot.raw, dtype=np.uint8).reshape(region.h, region.w, 4)
        same = (
            self._prev_raw is not None
            and self._prev_rgb is not None
            and self._prev_region == region
            and np.array_equal(raw, self._prev_raw)
        )
        if same:
            assert self._prev_rgb is not None
            rgb = self._prev_rgb.copy()  # frames must not share memory with each other
        else:
            rgb = bgra_to_rgb(raw)
            # Only this adapter holds the extra reference, and nobody changes frame pixels.
            self._prev_raw, self._prev_rgb, self._prev_region = raw, rgb, region
        return Frame(rgb, self.name, region, stamp, dpi, fresh=not same)
