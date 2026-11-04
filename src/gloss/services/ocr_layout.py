"""Group OCR lines into messages when the layout makes it clear.

A chat message is usually a small header (name, time) over one or more wrapped text lines, all
left-aligned and tightly stacked, or a name and a time on one row. The rule here is
deliberately strict: two lines are linked only if they have a similar height and are either on
one row with a small gap, or stacked with a small gap and aligned on the left. Evenly spaced
menu rows have a bigger gap, so they stay separate. A wrong split costs nothing, but a wrong
merge would hand the translator an unrelated sentence.
"""

from __future__ import annotations

from collections.abc import Sequence

from gloss.core.contracts import Rect

__all__ = ["group_rects"]


def _linked(
    a: Rect, b: Rect, stacked_gap: float, row_gap: float, height_ratio: float, opens: bool = False
) -> bool:
    """True if ``b`` continues the message that ``a`` is part of. ``a`` is read first."""
    small, large = min(a.h, b.h), max(a.h, b.h)
    if small <= 0 or large / small > height_ratio:
        return False
    overlap_y = min(a.bottom, b.bottom) - max(a.y, b.y)
    if overlap_y >= 0.6 * small:  # one row: name + time, or a short bubble beside a label
        return 0 <= b.x - a.right <= row_gap * small
    if opens:
        return False
    gap = b.y - a.bottom
    if not -0.2 * small <= gap <= stacked_gap * small:
        return False
    aligned = abs(a.x - b.x) <= 1.0 * small
    overlap_x = min(a.right, b.right) - max(a.x, b.x)
    return aligned and overlap_x > 0


def group_rects(
    rects: Sequence[Rect],
    *,
    stacked_gap: float = 1.0,
    row_gap: float = 3.0,
    height_ratio: float = 1.8,
    max_lines: int = 8,
    starts: Sequence[bool] | None = None,
) -> list[int]:
    """Return a group number per rectangle. The input must be in reading order.

    ``starts[i]`` says line ``i`` opens a new message (for example it holds a time stamp such
    as ``22:05:23``), so it is never stacked onto the line above it.

    Group numbers start at 0 and count up in order of first appearance.
    """
    count = len(rects)
    parent = list(range(count))
    size = [1] * count

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(count):
        for j in range(i + 1, count):
            if rects[j].y - rects[i].bottom > stacked_gap * max(rects[i].h, 1) * 2:
                continue  # too far below to link, and later rows are even further
            if _linked(
                rects[i], rects[j], stacked_gap, row_gap, height_ratio, starts and starts[j]
            ):
                a, b = find(i), find(j)
                if a != b and size[a] + size[b] <= max_lines:
                    parent[b] = a
                    size[a] += size[b]
    numbers: dict[int, int] = {}
    return [numbers.setdefault(find(i), len(numbers)) for i in range(count)]
