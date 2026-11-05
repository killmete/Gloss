"""Finds which parts of the screen changed, so OCR runs only where it must.

For each fresh frame the detector makes a small grayscale copy, cuts it into small cells
(a quarter of a tile on a side), and compares it with the previous frame. Changed cells
group into tile-aligned rectangles, padded a little so a line of text is never cut in half.
Rectangles are in frame coordinates (``(0, 0)`` is the frame's top-left pixel), ready for
``OCREngine.recognize``.

- A frame with ``fresh=False`` is skipped without looking at a single pixel.
- If a big dirty area is a chat that merely scrolled, the scroll is detected
  (``gloss.services.scroll_shift``, tolerant of animation) and only the newly exposed band,
  plus anything else that really changed, is reported. The shift itself is in
  ``ChangeResult.scrolls`` for the text tracker.
- Animated parts (avatar frames, badges, gift effects) change on almost every frame. A cell
  that changed in at least ``animated_min_changes`` of the last ``animated_window`` frames is
  "animated" and is not reported, except once every ``animated_recheck_s`` seconds so text
  there is still read eventually. A large change next to an animated cell, or a whole-frame
  change, frees the mark; a scroll moves it with the content.
- The first frame, and any frame whose size or screen position differs from the last one,
  is reported as one full-frame rectangle.

Not thread-safe: use one detector per capture worker.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass

import numpy as np

from gloss.core.contracts import Frame, Rect
from gloss.services.scroll_shift import (
    dirty_mask_after_shift,
    find_scroll,
    luma,
    mask_to_rects,
    quantize,
)


@dataclass(frozen=True, slots=True)
class ScrollShift:
    """A scrolled area. Content moved up by ``dy`` pixels (down if negative)."""

    area: Rect  # frame coordinates
    dy: int


@dataclass(frozen=True, slots=True)
class ChangeResult:
    """What changed since the previous frame.

    ``dirty`` is empty exactly when ``changed`` is False, and then OCR can be skipped.
    ``full`` is True when the whole frame is reported because there was no usable history.
    """

    changed: bool
    dirty: tuple[Rect, ...] = ()
    scrolls: tuple[ScrollShift, ...] = ()
    full: bool = False


def small_gray(rgb: np.ndarray, scale: int) -> np.ndarray:
    """Grayscale copy reduced by ``scale`` (1, 2, 4 or 8): the mean of (R + 2G + B) / 4."""
    height, width = rgb.shape[0] // scale * scale, rgb.shape[1] // scale * scale
    total: np.ndarray | None = None
    for channel, weight in ((0, 1), (1, 2), (2, 1)):
        plane = rgb[:height, :width, channel]
        block: np.ndarray | None = None
        for dy in range(scale):
            for dx in range(scale):
                view = plane[dy::scale, dx::scale]
                if block is None:
                    block = view.astype(np.uint16)
                else:
                    block += view
        assert block is not None
        if weight == 2:
            block <<= 1
        total = block if total is None else total + block
    assert total is not None
    shift = 2 + 2 * (scale.bit_length() - 1)
    return (total >> shift).astype(np.uint8)


def merge_rects(rects: list[Rect]) -> list[Rect]:
    """Merge rectangles that overlap or touch, until none do."""
    items = list(rects)
    merged = True
    while merged and len(items) > 1:
        merged = False
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i], items[j]
                if a.x <= b.right and b.x <= a.right and a.y <= b.bottom and b.y <= a.bottom:
                    left, top = min(a.x, b.x), min(a.y, b.y)
                    right, bottom = max(a.right, b.right), max(a.bottom, b.bottom)
                    items[i] = Rect(left, top, right - left, bottom - top)
                    del items[j]
                    merged = True
                    break
            if merged:
                break
    return items


def _padded(rect: Rect, pad: int, width: int, height: int) -> Rect:
    left, top = max(0, rect.x - pad), max(0, rect.y - pad)
    right, bottom = min(width, rect.right + pad), min(height, rect.bottom + pad)
    return Rect(left, top, right - left, bottom - top)


def _reduce_any(mask: np.ndarray, px: int) -> np.ndarray:
    """True for each ``px`` x ``px`` block that holds any True."""
    h, w = mask.shape
    rows, cols = -(-h // px), -(-w // px)
    if (rows * px, cols * px) != (h, w):
        padded = np.zeros((rows * px, cols * px), dtype=bool)
        padded[:h, :w] = mask
        mask = padded
    return mask.reshape(rows, px, cols, px).any(axis=(1, 3))


def _expand(cells: np.ndarray, px: int, h: int, w: int) -> np.ndarray:
    return np.repeat(np.repeat(cells, px, axis=0), px, axis=1)[:h, :w]


def _grow(cells: np.ndarray, n: int) -> np.ndarray:
    """``cells`` with every True spread ``n`` cells in all eight directions."""
    out = cells.copy()
    for _ in range(n):
        g = out.copy()
        g[1:] |= out[:-1]
        g[:-1] |= out[1:]
        g[:, 1:] |= out[:, :-1]
        g[:, :-1] |= out[:, 1:]
        g[1:, 1:] |= out[:-1, :-1]
        g[:-1, :-1] |= out[1:, 1:]
        g[1:, :-1] |= out[:-1, 1:]
        g[:-1, 1:] |= out[1:, :-1]
        out = g
    return out


def _cell_groups(cells: np.ndarray) -> list[Rect]:
    """Bounding boxes (in cells) of touching groups of True cells."""
    runs: list[Rect] = []
    for y in range(cells.shape[0]):
        xs = np.flatnonzero(cells[y])
        if xs.size == 0:
            continue
        for run in np.split(xs, np.flatnonzero(np.diff(xs) > 1) + 1):
            runs.append(Rect(int(run[0]), y, int(run[-1]) - int(run[0]) + 1, 1))
    return merge_rects(runs)


@dataclass(frozen=True, slots=True)
class _Scroll:
    """A scroll found in one area, with the quantised grayscale of the area before and after."""

    area: Rect
    dy: int
    before: np.ndarray
    after: np.ndarray


class ChangeDetector:
    """See the module doc. ``tile_size`` comes from ``capture.tile_size`` in config.

    ``padding`` grows each rectangle in pixels. ``downscale`` (1, 2, 4 or 8) shrinks the
    image before comparing. ``quantize_bits`` ignores differences in the lowest bits of gray,
    so faint noise, such as video compression, does not count. ``full_frame_fraction`` is the
    share of dirty tiles above which the whole frame is reported.

    Scroll: ``scroll_min_agreement`` is the share of comparable column strips that must agree
    on the shift. Animation: see the module doc. ``animated_max_share`` is the share of the
    frame above which "animated" is not trusted (a video), and ``animated_unmark_cells`` is
    the size, in cells, of a change next to an animated cell that frees the cells around it.
    """

    def __init__(
        self,
        tile_size: int = 64,
        *,
        padding: int = 8,
        downscale: int = 2,
        quantize_bits: int = 3,
        full_frame_fraction: float = 0.6,
        detect_scroll: bool = True,
        scroll_min_tiles: int = 4,
        scroll_min_agreement: float = 0.7,
        animated_window: int = 8,
        animated_min_changes: int = 3,
        animated_recheck_s: float = 2.0,
        animated_max_share: float = 0.5,
        animated_unmark_cells: int = 16,
    ) -> None:
        if downscale not in (1, 2, 4, 8):
            raise ValueError("downscale must be 1, 2, 4 or 8")
        if tile_size < downscale * 4:
            raise ValueError("tile_size is too small for the downscale")
        if padding < 0 or not 0 < full_frame_fraction <= 1 or not 0 <= quantize_bits <= 7:
            raise ValueError("padding, full_frame_fraction or quantize_bits is out of range")
        if not 0 < scroll_min_agreement <= 1 or not 0 < animated_max_share <= 1:
            raise ValueError("scroll_min_agreement or animated_max_share is out of range")
        if not 0 < animated_min_changes <= animated_window or animated_recheck_s < 0:
            raise ValueError("animated_* settings are out of range")
        self._tile = tile_size
        self._ts = tile_size // downscale  # tile edge in downscaled pixels
        self._k = 4 if self._ts % 4 == 0 else 1  # cells per tile edge
        self._cs = self._ts // self._k  # cell edge in downscaled pixels
        self._cpx = self._cs * downscale  # cell edge in frame pixels
        self._scale = downscale
        self._padding = padding
        self._bits = quantize_bits
        self._full_fraction = full_frame_fraction
        self._detect_scroll = detect_scroll
        self._scroll_min_tiles = scroll_min_tiles
        self._scroll_agreement = scroll_min_agreement
        self._anim_window = animated_window
        self._anim_min = animated_min_changes
        self._anim_recheck = animated_recheck_s
        self._anim_max_share = animated_max_share
        self._anim_unmark = animated_unmark_cells
        self._hist: collections.deque[np.ndarray] = collections.deque(maxlen=animated_window)
        self.reset()

    def reset(self) -> None:
        """Forget history: the next frame is reported in full."""
        self._gray: np.ndarray | None = None
        self._image: np.ndarray | None = None
        self._region: Rect | None = None
        self._source = ""
        self._hist.clear()
        self._anim = np.zeros((0, 0), dtype=bool)
        self._last = np.zeros((0, 0), dtype=np.float64)  # when each cell was last reported

    # -- public ---------------------------------------------------------------------------

    def detect(self, frame: Frame) -> ChangeResult:
        height, width = frame.image.shape[:2]
        full = Rect(0, 0, width, height)
        if not frame.fresh and self._gray is not None and self._region == frame.region:
            return ChangeResult(False)
        gray = self._prepare(frame.image)
        if (
            self._gray is None
            or self._gray.shape != gray.shape
            or self._region != frame.region
            or self._source != frame.source_id
        ):
            self._remember(frame, gray)
            self._start_history(gray, frame.timestamp)
            return ChangeResult(True, (full,), full=True)
        changed = _reduce_any(gray != self._gray, self._cs)
        previous_image, previous_anim = self._image, self._anim
        assert previous_image is not None
        self._remember(frame, gray)
        if not changed.any():
            self._hist.append(changed)
            self._anim = self._flags()
            return ChangeResult(False)
        now = frame.timestamp
        fresh_tiles = _reduce_any(changed & ~previous_anim, self._k)
        mostly_dirty = fresh_tiles.mean() >= self._full_fraction
        groups = [full] if mostly_dirty else self._tile_groups(fresh_tiles, width, height)
        areas: list[_Scroll] = []
        if self._detect_scroll:
            for group in groups:
                found = self._find_scroll(previous_image, frame.image, group, previous_anim)
                if found is not None:
                    areas.append(found)
        if mostly_dirty and not areas:
            self._start_history(gray, now)
            return ChangeResult(True, (full,))
        # Animation bookkeeping: marks move with a scroll, then this frame's changes count.
        masks = [self._apply_scroll(changed, sc) for sc in areas]
        self._hist.append(changed.copy())  # a copy: un-marking edits the history
        anim = self._flags()
        self._unmark(anim, changed & ~anim & ~previous_anim)
        if anim.mean() > self._anim_max_share:  # a video, not an avatar: trust nothing
            anim[:] = False
        due = (now - self._last) >= self._anim_recheck
        report = changed & (~anim | due)
        self._last[report] = now
        self._anim = anim
        rects: list[Rect] = []
        scrolls: list[ScrollShift] = []
        cpx = self._cpx
        for sc, mask in zip(areas, masks, strict=True):
            area = sc.area
            scrolls.append(ScrollShift(area, sc.dy))
            r0, c0 = area.y // cpx, area.x // cpx
            rows = min(-(-area.h // cpx), report.shape[0] - r0)
            cols = min(-(-area.w // cpx), report.shape[1] - c0)
            cells = report[r0 : r0 + rows, c0 : c0 + cols]
            keep = _expand(cells, cpx, area.h, area.w)
            if keep.shape != mask.shape:  # cells do not reach the very edge
                padded = np.zeros(mask.shape, dtype=bool)
                padded[: keep.shape[0], : keep.shape[1]] = keep
                keep = padded
            gap = max(1, self._tile // 4)
            for r in mask_to_rects(mask & keep, gap):
                rects.append(Rect(area.x + r.x, area.y + r.y, r.w, r.h))
            report[r0 : r0 + rows, c0 : c0 + cols] = False
        rects.extend(self._tile_groups(_reduce_any(report, self._k), width, height))
        if not rects:
            return ChangeResult(False, (), tuple(scrolls))
        padded_rects = [_padded(r, self._padding, width, height) for r in rects]
        return ChangeResult(True, tuple(merge_rects(padded_rects)), tuple(scrolls))

    def animated_share(self, rect: Rect) -> float:
        """Share (0..1) of ``rect`` (frame pixels) that lies on cells marked animated.

        0.0 when nothing is marked, when animation is not trusted, or when ``rect`` is empty
        or outside the frame. Safe to call from another thread while ``detect`` runs.
        """
        anim, cpx = self._anim, self._cpx  # one snapshot; detect() swaps the array whole
        rows, cols = anim.shape
        if rect.w <= 0 or rect.h <= 0 or not anim.any():
            return 0.0
        x0, y0 = max(0, rect.x), max(0, rect.y)
        x1, y1 = min(cols * cpx, rect.x + rect.w), min(rows * cpx, rect.y + rect.h)
        if x1 <= x0 or y1 <= y0:
            return 0.0
        c0, c1 = x0 // cpx, -(-x1 // cpx)
        r0, r1 = y0 // cpx, -(-y1 // cpx)

        def overlap(lo: int, hi: int, first: int, last: int) -> np.ndarray:
            edges = np.arange(first, last + 1) * cpx
            return np.minimum(edges[1:], hi) - np.maximum(edges[:-1], lo)

        wx = overlap(x0, x1, c0, c1)
        wy = overlap(y0, y1, r0, r1)
        hit = anim[r0:r1, c0:c1]
        share = float(wy @ hit @ wx) / float((x1 - x0) * (y1 - y0))
        return min(1.0, max(0.0, share))

    # -- internals ------------------------------------------------------------------------

    def _remember(self, frame: Frame, gray: np.ndarray) -> None:
        self._gray, self._image = gray, frame.image
        self._region, self._source = frame.region, frame.source_id

    def _prepare(self, rgb: np.ndarray) -> np.ndarray:
        """The small quantised grayscale copy that frames are compared on."""
        return quantize(small_gray(rgb, self._scale), self._bits)

    def _start_history(self, gray: np.ndarray, now: float) -> None:
        shape = (-(-gray.shape[0] // self._cs), -(-gray.shape[1] // self._cs))
        self._hist.clear()
        self._anim = np.zeros(shape, dtype=bool)
        self._last = np.full(shape, now, dtype=np.float64)

    def _flags(self) -> np.ndarray:
        return np.sum(self._hist, axis=0) >= self._anim_min

    def _find_scroll(
        self, previous: np.ndarray, current: np.ndarray, group: Rect, anim: np.ndarray
    ) -> _Scroll | None:
        if group.h < self._scroll_min_tiles * self._tile:
            return None
        window = (slice(group.y, group.bottom), slice(group.x, group.right))
        before = quantize(luma(previous[window]), self._bits)
        after = quantize(luma(current[window]), self._bits)
        cpx = self._cpx
        r0, c0 = group.y // cpx, group.x // cpx
        cells = anim[r0 : r0 + -(-group.h // cpx), c0 : c0 + -(-group.w // cpx)]
        ignore = None
        if cells.any():
            ignore = _expand(cells, cpx, group.h, group.w)
            if ignore.shape != before.shape:
                padded = np.zeros(before.shape, dtype=bool)
                padded[: ignore.shape[0], : ignore.shape[1]] = ignore
                ignore = padded
        match = find_scroll(before, after, min_agreement=self._scroll_agreement, ignore=ignore)
        if match is None:
            return None
        x0 = match.x0 // cpx * cpx  # align to cells
        x1 = min(group.w, -(-match.x1 // cpx) * cpx)
        # Grayscale of the scrolled window is computed here once and reused by _apply_scroll.
        area = Rect(group.x + x0, group.y, x1 - x0, group.h)
        return _Scroll(area, match.dy, before[:, x0:x1], after[:, x0:x1])

    def _apply_scroll(self, changed: np.ndarray, scroll: _Scroll) -> np.ndarray:
        """Move animation marks with the scroll and put into ``changed`` only what the scroll
        cannot explain. Returns the mask of unexplained pixels in the area."""
        area, dy = scroll.area, scroll.dy
        mask = dirty_mask_after_shift(scroll.before, scroll.after, dy)
        cpx = self._cpx
        cells = _reduce_any(mask, cpx)
        r0, c0 = area.y // cpx, area.x // cpx
        rows = min(cells.shape[0], changed.shape[0] - r0)
        cols = min(cells.shape[1], changed.shape[1] - c0)
        k = round(dy / cpx)
        sl = (slice(r0, r0 + rows), slice(c0, c0 + cols))
        for arr, empty in [*((h, False) for h in self._hist), (self._last, -np.inf)]:
            part = arr[sl].copy()
            moved = np.full_like(part, empty)
            if 0 < k < rows:
                moved[: rows - k] = part[k:]
            elif -rows < k < 0:
                moved[-k:] = part[: rows + k]
            elif k == 0:
                moved = part
            arr[sl] = moved
        changed[sl] = cells[:rows, :cols]
        return mask

    def _unmark(self, anim: np.ndarray, loose: np.ndarray) -> None:
        """A big change touching animated cells frees them (a new message, a layout change)."""
        if not anim.any():
            return
        near = _grow(anim, 2)
        for g in _cell_groups(loose):
            if g.area < self._anim_unmark or not near[g.y : g.bottom, g.x : g.right].any():
                continue
            y0, y1, x0, x1 = max(0, g.y - 2), g.bottom + 2, max(0, g.x - 2), g.right + 2
            for h in self._hist:
                h[y0:y1, x0:x1] = False
            anim[y0:y1, x0:x1] = False
            self._last[y0:y1, x0:x1] = -np.inf

    def _tile_groups(self, dirty: np.ndarray, width: int, height: int) -> list[Rect]:
        """Touching dirty tiles merged, as pixel rectangles."""
        tile = self._tile
        out = []
        for g in _cell_groups(dirty):
            x0, y0 = g.x * tile, g.y * tile
            x1, y1 = min(width, g.right * tile), min(height, g.bottom * tile)
            out.append(Rect(x0, y0, x1 - x0, y1 - y0))
        return out
