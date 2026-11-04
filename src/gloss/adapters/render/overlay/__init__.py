"""The click-through overlay: English text drawn next to the original, on top of everything.

One transparent window per monitor. Every window is frameless, never takes focus, and lets
mouse clicks fall through to what is underneath (``WS_EX_LAYERED | WS_EX_TRANSPARENT |
WS_EX_NOACTIVATE``). Windows also drops a topmost window below others sometimes, so a timer
raises each visible window again.

Coordinates: ``RenderItem.bbox`` is in physical desktop pixels. This is the only place that
converts them to Qt's logical pixels, per monitor, with that monitor's own scale. The maths
sits in ``physical_to_logical`` and ``pick_screen`` so it can be tested without a display.

Lines are kept by ``line_id``. ``show`` adds or updates lines. ``clear`` removes all of them,
and ``remove`` removes some. The pipeline calls these when the source text goes away. If
``linger_s`` is set above zero in ``[render.adapters.overlay]``, a line also expires by
itself after that many seconds.

Exclusive-fullscreen games cannot be overlaid by any window. Tell the user to use borderless
windowed mode.

The Qt code is in ``_qt.py`` and is imported by ``open()`` only, so discovering the adapter
loads no GUI library. Native-window helpers are repeated from the panel package on purpose:
adapters may not import each other. When the shared capture-exclusion helper lands in
``gloss.services``, change ``_exclude_hwnd`` in ``_qt.py`` only.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from gloss.core.contracts import (
    AdapterContext,
    AdapterUnavailable,
    FrameMeta,
    Health,
    Rect,
    RenderItem,
)
from gloss.core.registry import register_adapter

log = logging.getLogger(__name__)

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10

DEFAULT_FONT_SIZE = 14
DEFAULT_REASSERT_MS = 2000
DEFAULT_MAX_WIDTH_RATIO = 0.5
DEFAULT_LINGER_S = 0.0
DEFAULT_MIN_FONT_PX = 9
DEFAULT_MAX_FONT_PX = 28
DEFAULT_MAX_LINES = 2
CHIP_PAD = 4
CHIP_GAP = 2
CHIP_RADIUS = 4

# ---- coordinates (no Qt display needed) ---------------------------------------------------


@dataclass(frozen=True)
class ScreenGeometry:
    """One monitor. ``x`` and ``y`` are its physical desktop origin, which Qt also uses as the
    origin of the monitor's logical rectangle. ``w`` and ``h`` are physical pixels.
    ``dpr`` is the monitor's scale (1.0 = 100%)."""

    x: int
    y: int
    w: int
    h: int
    dpr: float

    @property
    def logical_w(self) -> float:
        return self.w / self.dpr

    @property
    def logical_h(self) -> float:
        return self.h / self.dpr


def pick_screen(bbox: Rect, screens: Sequence[ScreenGeometry]) -> int | None:
    """Return the index of the monitor that holds the centre of ``bbox``, else the nearest."""
    if not screens:
        return None
    cx, cy = bbox.x + bbox.w / 2, bbox.y + bbox.h / 2
    best, best_distance = 0, float("inf")
    for index, s in enumerate(screens):
        dx = max(s.x - cx, 0, cx - (s.x + s.w))
        dy = max(s.y - cy, 0, cy - (s.y + s.h))
        if dx == 0 and dy == 0:
            return index
        distance = dx * dx + dy * dy
        if distance < best_distance:
            best, best_distance = index, distance
    return best


def physical_to_logical(bbox: Rect, screen: ScreenGeometry) -> tuple[float, float, float, float]:
    """Convert a physical desktop rectangle to logical pixels inside ``screen``'s window."""
    return (
        (bbox.x - screen.x) / screen.dpr,
        (bbox.y - screen.y) / screen.dpr,
        bbox.w / screen.dpr,
        bbox.h / screen.dpr,
    )


# ---- settings -----------------------------------------------------------------------------


def _number(
    settings: Mapping[str, Any], key: str, default: float, low: float, high: float
) -> float:
    value = settings.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return float(value) if low <= value <= high else default


@dataclass(frozen=True)
class _OverlaySettings:
    font_size: int
    reassert_ms: int
    max_width_ratio: float
    linger_s: float
    min_font_px: int
    max_font_px: int
    max_lines: int
    clip_to_capture: bool

    @classmethod
    def parse(cls, settings: Mapping[str, Any]) -> _OverlaySettings:
        return cls(
            font_size=int(_number(settings, "font_size", DEFAULT_FONT_SIZE, 6, 96)),
            reassert_ms=int(
                _number(settings, "topmost_reassert_ms", DEFAULT_REASSERT_MS, 100, 600000)
            ),
            max_width_ratio=_number(settings, "max_width_ratio", DEFAULT_MAX_WIDTH_RATIO, 0.1, 1.0),
            linger_s=_number(settings, "linger_s", DEFAULT_LINGER_S, 0, 86400),
            min_font_px=int(_number(settings, "min_font_px", DEFAULT_MIN_FONT_PX, 4, 96)),
            max_font_px=int(_number(settings, "max_font_px", DEFAULT_MAX_FONT_PX, 4, 200)),
            max_lines=int(_number(settings, "max_lines", DEFAULT_MAX_LINES, 1, 10)),
            clip_to_capture=settings.get("clip_to_capture", True) is not False,
        )


# ---- chip placement (pure maths; the text measuring function is passed in) -----------------


@dataclass(frozen=True)
class LineBox:
    """One translated line and where its original sits (physical desktop pixels)."""

    key: str
    text: str
    bbox: Rect
    low_quality: bool


@dataclass(frozen=True)
class ChipLayout:
    """Where and how to draw one chip, in the monitor window's logical pixels."""

    key: str
    x: float
    y: float
    w: float
    h: float
    font_px: int
    lines: tuple[str, ...]
    low_quality: bool


Measure = Callable[[str, int], float]  # (text, font pixel size) -> width in logical pixels
ELLIPSIS = "…"


def _elide(text: str, font_px: int, width: float, measure: Measure) -> str:
    """Shorten ``text`` until it and an ellipsis fit in ``width``."""
    text = text.rstrip()
    if measure(text, font_px) <= width:
        return text
    low, high = 0, len(text)
    while low < high:  # the longest prefix that still fits with the ellipsis
        mid = (low + high + 1) // 2
        if measure(text[:mid].rstrip() + ELLIPSIS, font_px) <= width:
            low = mid
        else:
            high = mid - 1
    return text[:low].rstrip() + ELLIPSIS


def wrap_text(
    text: str, font_px: int, width: float, measure: Measure, max_lines: int
) -> tuple[str, ...]:
    """Break ``text`` into at most ``max_lines`` lines no wider than ``width``. Breaks at
    spaces when it can, anywhere otherwise (Chinese has no spaces). What does not fit is cut off
    with an ellipsis on the last line."""
    rest = " ".join(text.split())
    lines: list[str] = []
    while rest and len(lines) < max_lines:
        low, high = 1, len(rest)
        while low < high:  # the longest prefix that fits
            mid = (low + high + 1) // 2
            if measure(rest[:mid], font_px) <= width:
                low = mid
            else:
                high = mid - 1
        if low >= len(rest):
            lines.append(rest)
            rest = ""
            break
        space = rest.rfind(" ", 1, low + 1)
        cut = space if space > 0 else low
        lines.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        lines[-1] = _elide(lines[-1] + " " + rest, font_px, width, measure)
    return tuple(lines)


def _line_height(font_px: int) -> int:
    return round(font_px * 1.2)


def layout_chips(
    lines: Sequence[LineBox],
    screen: ScreenGeometry,
    cfg: _OverlaySettings,
    measure: Measure,
    region: Rect | None,
) -> list[ChipLayout]:
    """Place one chip per line, on top of its own original text.

    A chip keeps its line's x, y and height, and is never moved by another chip. The English is
    sized from the line height, shrunk until it fits on one line, widened to the right (never
    past the capture region or ``max_width_ratio``), and only as a last resort wrapped downward
    into the free space above the next line, or cut with an ellipsis. Lines outside the capture
    region are left out when ``clip_to_capture`` is on.
    """
    clip = region if (cfg.clip_to_capture and region is not None) else None
    boxes = []
    for line in lines:
        if clip is not None:
            centre_x = line.bbox.x + line.bbox.w / 2
            centre_y = line.bbox.y + line.bbox.h / 2
            if not (clip.x <= centre_x < clip.right and clip.y <= centre_y < clip.bottom):
                continue
        x, y, w, h = physical_to_logical(line.bbox, screen)
        boxes.append((line, x, y, w, h))

    limit_right = screen.logical_w
    limit_bottom = screen.logical_h
    if clip is not None:
        limit_right = min(limit_right, (clip.right - screen.x) / screen.dpr)
        limit_bottom = min(limit_bottom, (clip.bottom - screen.y) / screen.dpr)
    max_w = screen.logical_w * cfg.max_width_ratio
    min_px = min(cfg.min_font_px, cfg.max_font_px)
    max_px = max(cfg.min_font_px, cfg.max_font_px)

    chips: list[ChipLayout] = []
    for line, x, y, w, h in boxes:
        avail = max(min(x + max(w, max_w), limit_right) - x, min(w, screen.logical_w - x), 8.0)
        start = min(max_px, max(min_px, round(h * 0.7)))
        placed: ChipLayout | None = None
        for px in range(start, min_px - 1, -1):
            need = measure(line.text, px) + 2 * CHIP_PAD
            if need <= avail:
                height = max(h, _line_height(px) + 2)
                placed = ChipLayout(
                    line.key, x, y, max(w, need), height, px, (line.text,), line.low_quality
                )
                break
        if placed is None:
            lh = _line_height(min_px)
            below = [
                by
                for (other, bx, by, bw, _bh) in boxes
                if other is not line and by > y + h / 2 and bx < x + avail and bx + bw > x
            ]
            next_top = min(below) if below else limit_bottom
            free = max(h, next_top - y - CHIP_GAP)
            rows = max(1, min(cfg.max_lines, int((free - 4) // lh)))
            wrapped = wrap_text(line.text, min_px, avail - 2 * CHIP_PAD, measure, rows)
            height = max(h, len(wrapped) * lh + 4) if len(wrapped) > 1 else max(h, lh + 2)
            placed = ChipLayout(line.key, x, y, avail, height, min_px, wrapped, line.low_quality)
        chips.append(placed)
    return chips


@dataclass
class _Line:
    line_id: str
    text: str
    bbox: Rect
    low_quality: bool
    expires: float | None


# ---- the adapter --------------------------------------------------------------------------


@register_adapter("render", "overlay")
class OverlayRenderer:
    name = "overlay"
    REQUIRES_CONTRACT = ">=1.0,<2.0"

    def __init__(self, context: AdapterContext) -> None:
        self._cfg = _OverlaySettings.parse(context.settings)
        self._ctl: Any = None

    # -- lifecycle (Qt main thread)
    def open(self) -> None:
        if self._ctl is not None:
            return
        try:
            from gloss.adapters.render.overlay import _qt

            controller = _qt.OverlayController(self._cfg)
            controller.open()
        except Exception as exc:
            raise AdapterUnavailable(f"The overlay window could not be created: {exc}") from exc
        self._ctl = controller

    def close(self) -> None:
        controller, self._ctl = self._ctl, None
        if controller is not None:
            controller.close()

    # -- any thread
    def show(self, items: Sequence[RenderItem], frame_meta: FrameMeta | None) -> None:
        controller = self._ctl
        if controller is None:
            return
        expires = time.monotonic() + self._cfg.linger_s if self._cfg.linger_s > 0 else None
        fresh: list[_Line] = []
        for item in tuple(items):
            result = item.result
            text = result.text.strip()
            if item.bbox is None or result.status != "ok" or not text:
                continue  # Nowhere to draw it, or nothing worth covering the original with.
            fresh.append(_Line(result.line_id, text, item.bbox, item.low_quality, expires))
        if fresh:
            region = frame_meta.region if frame_meta is not None else None
            controller.post(lambda: controller.add(fresh, region))

    def remove(self, line_ids: Iterable[str]) -> None:
        """Take specific lines off the screen, for example when the source line scrolled away."""
        controller = self._ctl
        ids = tuple(line_ids)
        if controller is not None:
            controller.post(lambda: controller.remove(ids))

    def clear(self) -> None:
        controller = self._ctl
        if controller is not None:
            controller.post(controller.clear)

    def set_visible(self, visible: bool) -> None:
        controller = self._ctl
        wanted = bool(visible)
        if controller is not None:
            controller.post(lambda: controller.set_visible(wanted))

    def set_status(self, health: Health, banner: str = "") -> None:
        controller = self._ctl
        text = banner or ""
        if controller is not None:
            controller.post(lambda: controller.set_banner(text))

    def health(self) -> Health:
        controller = self._ctl
        if controller is None:
            return Health("down", "The overlay is not open.")
        return controller.health()

    @property
    def windows(self) -> tuple[Any, ...]:
        controller = self._ctl
        return controller.windows if controller is not None else ()
