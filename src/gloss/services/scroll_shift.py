"""Scroll-shift detection: find that a chat scrolled, so only the newly exposed band is dirty.

When a chat scrolls up by 30 pixels, every tile of it changes, yet 95 percent of the text is
the same text one step higher and OCR already read it. This module finds that vertical
shift between two images of the same area, and reports which pixels are really new.

The images are 2-D arrays of equal shape, normally the quantised grayscale from
``luma``/``quantize``. Matching is exact on row hashes, so noisy or animated content simply
finds no shift, and the caller keeps its ordinary dirty tiles. Wrong answers are avoided over
missed ones: a shift is reported only if almost all informative rows in the overlap agree.

Convention: shift ``s`` means ``current[i] == previous[i + s]``. A chat that scrolls up (new
lines appear at the bottom) has a positive shift.
"""

from __future__ import annotations

import functools

import numpy as np

from gloss.core.contracts import Rect

_SEED = 0x6C055


@functools.lru_cache(maxsize=16)
def _weights(width: int) -> np.ndarray:
    rng = np.random.default_rng(_SEED)
    return rng.integers(1, 2**32, size=width, dtype=np.uint32)


def luma(rgb: np.ndarray) -> np.ndarray:
    """Grayscale (uint8) of an RGB image, using integer weights 77/150/29 out of 256."""
    r = rgb[:, :, 0].astype(np.uint16)
    g = rgb[:, :, 1].astype(np.uint16)
    b = rgb[:, :, 2].astype(np.uint16)
    return ((r * 77 + g * 150 + b * 29) >> 8).astype(np.uint8)


def quantize(gray: np.ndarray, bits: int) -> np.ndarray:
    """Drop the lowest ``bits`` bits, so faint noise does not look like a change."""
    return gray >> bits if bits else gray


def row_hashes(image: np.ndarray) -> np.ndarray:
    """One 32-bit hash per row. Equal rows give equal hashes."""
    weights = _weights(image.shape[1])
    return (image.astype(np.uint32) * weights).sum(axis=1, dtype=np.uint32)


def find_vertical_shift(
    previous: np.ndarray,
    current: np.ndarray,
    *,
    min_rows: int = 8,
    min_overlap: float = 0.25,
    min_agreement: float = 0.8,
    max_shift: int | None = None,
) -> int | None:
    """Return the shift ``s`` (see the module doc) or None if the content did not scroll.

    - ``min_rows``: at least this many informative rows must agree.
    - ``min_overlap``: the overlapping part must be at least this share of the height.
    - ``min_agreement``: this share of the informative rows in the overlap must agree.
    A row is informative if it is not one flat colour, because blank rows match anything.
    """
    if previous.shape != current.shape or previous.ndim != 2:
        return None
    height = current.shape[0]
    limit = height - max(1, int(height * min_overlap)) if max_shift is None else max_shift
    if limit < 1:
        return None
    informative = current.max(axis=1) != current.min(axis=1)
    if int(informative.sum()) < min_rows:
        return None
    prev_hash, cur_hash = row_hashes(previous), row_hashes(current)
    matches = (cur_hash[:, None] == prev_hash[None, :]) & informative[:, None]
    rows, cols = np.nonzero(matches)
    shifts = cols - rows
    keep = (shifts != 0) & (np.abs(shifts) <= limit)
    if not keep.any():
        return None
    votes = np.bincount(shifts[keep] + height, minlength=2 * height + 1)
    best = int(votes.argmax())
    count = int(votes[best])
    shift = best - height
    # How many informative rows of the current image have a partner row at all?
    start, stop = max(0, -shift), min(height, height - shift)
    overlap_info = int(informative[start:stop].sum())
    if count < min_rows or overlap_info == 0 or count < min_agreement * overlap_info:
        return None
    return shift


def dirty_mask_after_shift(previous: np.ndarray, current: np.ndarray, shift: int) -> np.ndarray:
    """Pixels that are neither unchanged in place nor explained by the scroll."""
    height = current.shape[0]
    explained = previous == current  # static parts, such as a header or a scrollbar
    if shift > 0:
        explained[: height - shift] |= current[: height - shift] == previous[shift:]
    elif shift < 0:
        explained[-shift:] |= current[-shift:] == previous[: height + shift]
    return ~explained


def mask_to_rects(mask: np.ndarray, row_gap: int = 1) -> list[Rect]:
    """Tight rectangles around groups of dirty rows. Groups closer than ``row_gap`` merge."""
    rows = np.flatnonzero(mask.any(axis=1))
    if rows.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(rows) > row_gap) + 1
    rects: list[Rect] = []
    for group in np.split(rows, breaks):
        top, bottom = int(group[0]), int(group[-1]) + 1
        cols = np.flatnonzero(mask[top:bottom].any(axis=0))
        rects.append(Rect(int(cols[0]), top, int(cols[-1]) - int(cols[0]) + 1, bottom - top))
    return rects
