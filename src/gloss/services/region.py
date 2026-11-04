"""Backend logic for the region selector: pure geometry, no Qt, no Windows calls.

The selector window (in ``ui/``) turns mouse drags into rectangles; everything about where
those rectangles may sit and how they convert between coordinate spaces is here, so it can be
tested without a screen. Everything is in physical pixels of the virtual desktop unless a name
says ``logical`` (Qt's device-independent pixels, which only the UI sees).

Typical use:

    rect = normalize_rect(press_x, press_y, release_x, release_y)   # desktop pixels
    rect = validate_region(rect, monitors)                          # clip, enforce min size
    target = region_target(rect, monitors)                          # a CaptureTarget
"""

from __future__ import annotations

from collections.abc import Sequence

from gloss.core.contracts import CaptureTarget, MonitorInfo, PermanentError, Rect
from gloss.services.monitors import intersection, monitor_with_most_overlap, virtual_desktop

DEFAULT_MIN_SIZE = 16


class InvalidRegion(PermanentError):
    """The chosen region cannot be captured. The message is safe to show the user."""


def normalize_rect(x1: int, y1: int, x2: int, y2: int) -> Rect:
    """A rectangle from two opposite corners, dragged in any direction."""
    left, top = min(x1, x2), min(y1, y2)
    return Rect(left, top, abs(x2 - x1), abs(y2 - y1))


def clamp_to_desktop(rect: Rect, monitors: Sequence[MonitorInfo]) -> Rect | None:
    """Clip a rectangle to the virtual desktop. None if no pixel of it is on the desktop."""
    return intersection(rect, virtual_desktop(monitors))


def monitors_touched(rect: Rect, monitors: Sequence[MonitorInfo]) -> list[tuple[MonitorInfo, Rect]]:
    """Each monitor the rectangle overlaps, with the overlapping part in desktop pixels."""
    parts = []
    for monitor in monitors:
        overlap = intersection(rect, monitor.bounds)
        if overlap is not None:
            parts.append((monitor, overlap))
    return parts


def spans_monitors(rect: Rect, monitors: Sequence[MonitorInfo]) -> bool:
    return len(monitors_touched(rect, monitors)) > 1


def validate_region(
    rect: Rect, monitors: Sequence[MonitorInfo], *, min_size: int = DEFAULT_MIN_SIZE
) -> Rect:
    """Clip to the desktop and refuse empty or tiny selections. Raises InvalidRegion."""
    clipped = clamp_to_desktop(rect, monitors)
    if clipped is None or not monitors_touched(clipped, monitors):
        raise InvalidRegion("That area is not on any monitor.")
    if clipped.w < min_size or clipped.h < min_size:
        raise InvalidRegion(f"Pick an area at least {min_size} pixels wide and tall.")
    return clipped


def region_target(rect: Rect, monitors: Sequence[MonitorInfo]) -> CaptureTarget:
    """A region ``CaptureTarget``, tagged with the monitor holding most of it."""
    monitor = monitor_with_most_overlap(monitors, rect)
    return CaptureTarget("region", monitor_id=monitor.monitor_id if monitor else None, region=rect)


def monitor_scale(monitor: MonitorInfo, logical_width: int) -> float:
    """Physical pixels per logical pixel, from the monitor's width as Qt reports it."""
    if logical_width <= 0:
        raise ValueError("logical_width must be positive")
    return monitor.bounds.w / logical_width


def logical_to_physical(rect: Rect, screen_logical: Rect, monitor: MonitorInfo) -> Rect:
    """Convert a rectangle Qt reports for one screen into physical desktop pixels.

    ``screen_logical`` is that screen's geometry in Qt's logical pixels and ``monitor`` the
    same screen as Windows lists it. The scale is taken from the two widths, so it stays right
    when Qt rounds a fractional scale such as 125 or 150 percent.
    """
    scale = monitor_scale(monitor, screen_logical.w)
    left = monitor.bounds.x + round((rect.x - screen_logical.x) * scale)
    top = monitor.bounds.y + round((rect.y - screen_logical.y) * scale)
    right = monitor.bounds.x + round((rect.right - screen_logical.x) * scale)
    bottom = monitor.bounds.y + round((rect.bottom - screen_logical.y) * scale)
    return Rect(left, top, max(0, right - left), max(0, bottom - top))


def physical_to_logical(rect: Rect, screen_logical: Rect, monitor: MonitorInfo) -> Rect:
    """The inverse of ``logical_to_physical``, for drawing the saved region in Qt."""
    scale = monitor_scale(monitor, screen_logical.w)
    left = screen_logical.x + round((rect.x - monitor.bounds.x) / scale)
    top = screen_logical.y + round((rect.y - monitor.bounds.y) / scale)
    right = screen_logical.x + round((rect.right - monitor.bounds.x) / scale)
    bottom = screen_logical.y + round((rect.bottom - monitor.bounds.y) / scale)
    return Rect(left, top, max(0, right - left), max(0, bottom - top))


def restore_region(saved: Rect, monitors: Sequence[MonitorInfo]) -> Rect | None:
    """A saved region that is still valid on the current monitors, or None.

    Monitors come and go between sessions, so the pipeline calls this before reusing a region
    from config. A region that no longer fits is dropped, never guessed at.
    """
    try:
        return validate_region(saved, monitors)
    except InvalidRegion:
        return None
