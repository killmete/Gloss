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
from dataclasses import dataclass

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


def _equal_pairs(
    current: np.ndarray, previous: np.ndarray, wanted: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """All (i, j) with ``current[i] == previous[j]`` and ``wanted[i]``.

    Sort plus binary search, so the cost follows the number of matches, not rows squared.
    """
    order = np.argsort(previous, kind="stable")
    sorted_prev = previous[order]
    rows = np.flatnonzero(wanted)
    values = current[rows]
    lo = np.searchsorted(sorted_prev, values, side="left")
    hi = np.searchsorted(sorted_prev, values, side="right")
    counts = hi - lo
    total = int(counts.sum())
    if total == 0:
        empty = np.empty(0, dtype=np.intp)
        return empty, empty
    out_rows = np.repeat(rows, counts)
    # Position of each match inside its run, then index into the sorted order.
    run_start = np.repeat(np.cumsum(counts) - counts, counts)
    offsets = np.arange(total) - run_start + np.repeat(lo, counts)
    return out_rows, order[offsets]


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
    rows, cols = _equal_pairs(cur_hash, prev_hash, informative)
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


@dataclass(frozen=True, slots=True)
class ScrollMatch:
    """A scroll found by ``find_scroll``: shift ``dy`` over columns ``x0`` to ``x1``."""

    dy: int
    x0: int
    x1: int  # exclusive


def find_scroll(
    previous: np.ndarray,
    current: np.ndarray,
    *,
    strip_width: int = 32,
    min_rows: int = 6,
    min_overlap: float = 0.25,
    min_agreement: float = 0.7,
    strip_agreement: float = 0.5,
    min_cover: float = 0.2,
    static_share: float = 0.9,
    ignore: np.ndarray | None = None,
) -> ScrollMatch | None:
    """Find a vertical scroll even while parts of the picture animate.

    The image is cut into column strips. Each strip votes, row by row, for the shift that
    makes its rows equal (a row of a strip is equal only if every pixel in it is). Animated
    strips find no partner and simply do not vote. The winning shift counts if at least
    ``min_agreement`` of the strips that hold content agree with it (each strip agrees if
    ``strip_agreement`` of its content rows do), within the widest run of strips that
    covers at least ``min_cover`` of the width. Strips that did not change at all
    (``static_share`` of their rows equal in place: a header, a fixed overlay) are not
    compared. ``ignore`` is an optional mask of the same shape, True where pixels are known
    to be animated: rows touching it do not count. ``x0``/``x1`` bound the agreeing strips.
    """
    if previous.shape != current.shape or previous.ndim != 2:
        return None
    height, width = current.shape
    limit = height - max(1, int(height * min_overlap))
    if limit < 1 or width < strip_width:
        return None
    count = width // strip_width
    votes = np.zeros((count, 2 * height + 1), dtype=np.int32)
    info_rows = np.zeros((count, height), dtype=bool)
    static = np.zeros(count, dtype=bool)
    for s in range(count):
        a = slice(s * strip_width, (s + 1) * strip_width if s < count - 1 else width)
        cur, prev = current[:, a], previous[:, a]
        info = cur.max(axis=1) != cur.min(axis=1)
        if ignore is not None:
            info &= ~ignore[:, a].any(axis=1)
        info_rows[s] = info
        if int(info.sum()) < min_rows:
            continue
        ch, ph = row_hashes(cur), row_hashes(prev)
        if int(((ch == ph) & info).sum()) >= static_share * int(info.sum()):
            static[s] = True
            continue
        rows, cols = _equal_pairs(ch, ph, info)
        shifts = cols - rows
        keep = (shifts != 0) & (np.abs(shifts) <= limit)
        if keep.any():
            votes[s] = np.bincount(shifts[keep] + height, minlength=2 * height + 1)
    total = votes.sum(axis=0)
    best = int(total.argmax())
    shift = best - height
    if shift == 0 or int(total[best]) < min_rows:
        return None
    start, stop = max(0, -shift), min(height, height - shift)
    overlap_info = info_rows[:, start:stop].sum(axis=1)
    content = (overlap_info >= min_rows) & ~static
    if not content.any():
        return None
    agree = content & (votes[:, best] >= strip_agreement * np.maximum(overlap_info, 1))
    # The scrolled area is the widest run of strips that starts and ends on an agreeing strip
    # and in which at least ``min_agreement`` of the content strips agree. Strips outside it
    # (a fixed side panel, an animated effect) are not part of the scroll.
    agree_sum, content_sum = np.cumsum(agree), np.cumsum(content)
    best_run: tuple[int, int] | None = None
    for i in np.flatnonzero(agree):
        for j in np.flatnonzero(agree)[::-1]:
            if j < i or (best_run is not None and j - i <= best_run[1] - best_run[0]):
                break
            a_n = agree_sum[j] - agree_sum[i] + 1
            c_n = content_sum[j] - content_sum[i] + 1
            if a_n >= min_agreement * c_n:
                best_run = (int(i), int(j))
                break
    if best_run is None or best_run[1] - best_run[0] + 1 < max(1, min_cover * count):
        return None
    i, j = best_run
    x1 = width if j == count - 1 else (j + 1) * strip_width
    return ScrollMatch(shift, i * strip_width, x1)
