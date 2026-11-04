"""Helpers shared by the capture adapters (adapters may not import each other).

Settings checking, turning a ``CaptureTarget`` into a desktop rectangle, and the pixel-format
conversion every backend needs. No third-party capture library is imported here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from gloss.core.contracts import (
    AdapterUnavailable,
    CaptureTarget,
    MonitorInfo,
    PermanentError,
    Rect,
)
from gloss.services.monitors import (
    find_monitor,
    intersection,
    monitor_with_most_overlap,
    virtual_desktop,
)

DEFAULT_FPS_MAX = 12.0
DEFAULT_FIRST_FRAME_TIMEOUT_MS = 1500
MAX_FPS = 240.0


@dataclass(frozen=True, slots=True)
class CaptureSettings:
    """What a capture adapter reads from its config table.

    ``fps_max`` lives in ``[capture]``, not in the adapter table, so the pipeline should
    copy it into the settings it passes to the adapter. Left out, it defaults to 4.
    """

    fps_max: float = DEFAULT_FPS_MAX
    first_frame_timeout_ms: int = DEFAULT_FIRST_FRAME_TIMEOUT_MS

    @property
    def update_interval_ms(self) -> int:
        return max(1, int(1000 / self.fps_max))


def _number(settings: Mapping[str, Any], key: str, default: float) -> float:
    value = settings.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PermanentError(f"capture setting '{key}' must be a number")
    return float(value)


def parse_settings(settings: Mapping[str, Any]) -> CaptureSettings:
    """Check the settings this module knows. Unknown keys are ignored."""
    fps = _number(settings, "fps_max", DEFAULT_FPS_MAX)
    if not 0 < fps <= MAX_FPS:
        raise PermanentError(f"capture setting 'fps_max' must be above 0 and at most {MAX_FPS:g}")
    wait = _number(settings, "first_frame_timeout_ms", DEFAULT_FIRST_FRAME_TIMEOUT_MS)
    if not 0 <= wait <= 60_000:
        raise PermanentError("capture setting 'first_frame_timeout_ms' must be 0 to 60000")
    return CaptureSettings(fps, int(wait))


@dataclass(frozen=True, slots=True)
class ScreenTarget:
    """A monitor or region target resolved to desktop pixels."""

    region: Rect
    monitor: MonitorInfo  # the monitor holding most of the region, for its DPI scale


def resolve_screen_target(target: CaptureTarget, monitors: Sequence[MonitorInfo]) -> ScreenTarget:
    """Resolve ``monitor`` and ``region`` targets. Raises AdapterUnavailable if impossible.

    A region is clipped to the virtual desktop, so one that hangs off the edge still works,
    but one with no pixels on any monitor is unavailable.
    """
    if target.mode == "monitor":
        monitor = find_monitor(monitors, target.monitor_id)
        return ScreenTarget(monitor.bounds, monitor)
    if target.mode == "region" and target.region is not None:
        if target.monitor_id is not None:
            find_monitor(monitors, target.monitor_id)  # must exist
        clipped = intersection(target.region, virtual_desktop(monitors))
        monitor = monitor_with_most_overlap(monitors, clipped) if clipped else None
        if clipped is None or monitor is None:
            raise AdapterUnavailable("that region is not on any monitor")
        return ScreenTarget(clipped, monitor)
    raise AdapterUnavailable(f"'{target.mode}' targets are not a screen area")


def bgra_to_rgb(bgra: np.ndarray) -> np.ndarray:
    """Return a new C-contiguous RGB copy of a BGRA image. It owns its memory."""
    height, width = bgra.shape[:2]
    rgb = np.empty((height, width, 3), dtype=np.uint8)
    rgb[:, :, 0] = bgra[:, :, 2]
    rgb[:, :, 1] = bgra[:, :, 1]
    rgb[:, :, 2] = bgra[:, :, 0]
    return rgb
