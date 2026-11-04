"""Finds which parts of the screen changed, so OCR runs only where it must.

For each fresh frame the detector makes a small grayscale copy, cuts it into tiles, hashes
every tile, and compares the hashes with the previous frame. Neighbouring dirty tiles merge
into rectangles, padded a little so a line of text is never cut in half. Rectangles are in
frame coordinates (``(0, 0)`` is the frame's top-left pixel), ready for ``OCREngine.recognize``.

- A frame with ``fresh=False`` is skipped without looking at a single pixel.
- If a big dirty area is a chat that merely scrolled, the scroll is detected
  (``gloss.services.scroll_shift``) and only the newly exposed band, plus anything else that
  really changed, is reported. The shift itself is in ``ChangeResult.scrolls`` for the text
  tracker.
- The first frame, and any frame whose size or screen position differs from the last one,
  is reported as one full-frame rectangle.

Not thread-safe: use one detector per capture worker.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass

import numpy as np

from gloss.core.contracts import Frame, Rect
from gloss.services.scroll_shift import (
    dirty_mask_after_shift,
    find_vertical_shift,
    luma,
    mask_to_rects,
    quantize,
)

_SEED = 0x71E5


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


@functools.lru_cache(maxsize=8)
def _tile_weights(tile: int) -> np.ndarray:
    rng = np.random.default_rng(_SEED)
    return rng.integers(1, 2**32, size=(tile, tile), dtype=np.uint32)


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


class ChangeDetector:
    """See the module doc. ``tile_size`` comes from ``capture.tile_size`` in config.

    ``padding`` grows each rectangle in pixels. ``downscale`` (1, 2, 4 or 8) shrinks the
    image before hashing. ``quantize_bits`` ignores differences in the lowest bits of gray,
    so faint noise, such as video compression, does not count. ``full_frame_fraction`` is the
    share of dirty tiles above which the whole frame is reported.
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
    ) -> None:
        if downscale not in (1, 2, 4, 8):
            raise ValueError("downscale must be 1, 2, 4 or 8")
        if tile_size < downscale * 4:
            raise ValueError("tile_size is too small for the downscale")
        if padding < 0 or not 0 < full_frame_fraction <= 1 or not 0 <= quantize_bits <= 7:
            raise ValueError("padding, full_frame_fraction or quantize_bits is out of range")
        self._tile = tile_size
        self._ts = tile_size // downscale  # tile edge in downscaled pixels
        self._scale = downscale
        self._padding = padding
        self._bits = quantize_bits
        self._full_fraction = full_frame_fraction
        self._detect_scroll = detect_scroll
        self._scroll_min_tiles = scroll_min_tiles
        self._hashes: np.ndarray | None = None
        self._image: np.ndarray | None = None
        self._region: Rect | None = None
        self._source = ""

    def reset(self) -> None:
        """Forget history: the next frame is reported in full."""
        self._hashes = self._image = self._region = None

    # -- public ---------------------------------------------------------------------------

    def detect(self, frame: Frame) -> ChangeResult:
        height, width = frame.image.shape[:2]
        full = Rect(0, 0, width, height)
        if not frame.fresh and self._hashes is not None and self._region == frame.region:
            return ChangeResult(False)
        hashes = self._tile_hashes(frame.image)
        if (
            self._hashes is None
            or self._hashes.shape != hashes.shape
            or self._region != frame.region
            or self._source != frame.source_id
        ):
            self._remember(frame, hashes)
            return ChangeResult(True, (full,), full=True)
        dirty = hashes != self._hashes
        previous_image = self._image
        self._remember(frame, hashes)
        count = int(dirty.sum())
        if count == 0:
            return ChangeResult(False)
        mostly_dirty = count >= self._full_fraction * dirty.size
        groups = [full] if mostly_dirty else self._tile_groups(dirty, width, height)
        rects: list[Rect] = []
        scrolls: list[ScrollShift] = []
        for group in groups:
            refined = None
            if self._detect_scroll and previous_image is not None:
                refined = self._refine_scroll(previous_image, frame.image, group)
            if refined is None:
                rects.append(group)
            else:
                shift, parts = refined
                scrolls.append(shift)
                rects.extend(parts)
        if mostly_dirty and not scrolls:
            return ChangeResult(True, (full,))
        padded = [_padded(r, self._padding, width, height) for r in rects]
        return ChangeResult(True, tuple(merge_rects(padded)), tuple(scrolls))

    # -- internals ------------------------------------------------------------------------

    def _remember(self, frame: Frame, hashes: np.ndarray) -> None:
        self._hashes, self._image = hashes, frame.image
        self._region, self._source = frame.region, frame.source_id

    def _tile_hashes(self, rgb: np.ndarray) -> np.ndarray:
        gray = quantize(small_gray(rgb, self._scale), self._bits)
        ts = self._ts
        rows, cols = -(-gray.shape[0] // ts), -(-gray.shape[1] // ts)
        padded = np.zeros((rows * ts, cols * ts), dtype=np.uint32)
        padded[: gray.shape[0], : gray.shape[1]] = gray
        tiles = padded.reshape(rows, ts, cols, ts)
        weights = _tile_weights(ts)[None, :, None, :]
        return (tiles * weights).sum(axis=(1, 3), dtype=np.uint32)

    def _tile_groups(self, dirty: np.ndarray, width: int, height: int) -> list[Rect]:
        """Runs of dirty tiles per row, merged when they touch, as pixel rectangles."""
        runs: list[Rect] = []
        for ty in range(dirty.shape[0]):
            xs = np.flatnonzero(dirty[ty])
            if xs.size == 0:
                continue
            breaks = np.flatnonzero(np.diff(xs) > 1) + 1
            for run in np.split(xs, breaks):
                runs.append(Rect(int(run[0]), ty, int(run[-1]) - int(run[0]) + 1, 1))
        tile = self._tile
        out = []
        for g in merge_rects(runs):
            x0, y0 = g.x * tile, g.y * tile
            x1, y1 = min(width, g.right * tile), min(height, g.bottom * tile)
            out.append(Rect(x0, y0, x1 - x0, y1 - y0))
        return out

    def _refine_scroll(
        self, previous: np.ndarray, current: np.ndarray, area: Rect
    ) -> tuple[ScrollShift, list[Rect]] | None:
        if area.h < self._scroll_min_tiles * self._tile:
            return None
        window = (slice(area.y, area.bottom), slice(area.x, area.right))
        before = quantize(luma(previous[window]), self._bits)
        after = quantize(luma(current[window]), self._bits)
        shift = find_vertical_shift(before, after)
        if shift is None:
            return None
        mask = dirty_mask_after_shift(before, after, shift)
        gap = max(1, self._tile // 4)
        parts = [Rect(area.x + r.x, area.y + r.y, r.w, r.h) for r in mask_to_rects(mask, gap)]
        return ScrollShift(area, shift), parts
