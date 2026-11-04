"""Region and line geometry shared by the OCR adapters and the TextTracker.

All rectangles are frame coordinates (physical pixels).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from rapidfuzz import fuzz

from gloss.core.contracts import Rect, TextBlock

__all__ = [
    "centre_inside",
    "clamp_rect",
    "dedupe_blocks",
    "intersection_area",
    "iou",
    "merge_regions",
    "plan_regions",
    "reading_order",
]


def intersection_area(a: Rect, b: Rect) -> int:
    width = min(a.right, b.right) - max(a.x, b.x)
    height = min(a.bottom, b.bottom) - max(a.y, b.y)
    return width * height if width > 0 and height > 0 else 0


def iou(a: Rect, b: Rect) -> float:
    inter = intersection_area(a, b)
    union = a.area + b.area - inter
    return inter / union if union > 0 else 0.0


def clamp_rect(rect: Rect, width: int, height: int) -> Rect | None:
    """Cut ``rect`` to the frame. Return None when nothing of it is left."""
    left, top = max(rect.x, 0), max(rect.y, 0)
    right, bottom = min(rect.right, width), min(rect.bottom, height)
    if right <= left or bottom <= top:
        return None
    return Rect(left, top, right - left, bottom - top)


def merge_regions(rects: Iterable[Rect]) -> list[Rect]:
    """Join rectangles that overlap, so no pixel is read twice. Touching ones stay apart."""
    pending = list(rects)
    merged = True
    while merged:
        merged = False
        out: list[Rect] = []
        for rect in pending:
            for i, other in enumerate(out):
                if intersection_area(rect, other) > 0:
                    left, top = min(rect.x, other.x), min(rect.y, other.y)
                    right, bottom = max(rect.right, other.right), max(rect.bottom, other.bottom)
                    out[i] = Rect(left, top, right - left, bottom - top)
                    merged = True
                    break
            else:
                out.append(rect)
        pending = out
    return pending


def plan_regions(
    regions: Sequence[Rect] | None, width: int, height: int, margin: int = 0
) -> list[tuple[Rect, Rect]]:
    """Turn the caller's regions into ``(crop, keep)`` pairs, both in frame coordinates.

    ``keep`` is the clamped region. ``crop`` is ``keep`` grown by ``margin`` pixels, so a line
    that touches the edge is read whole. Only lines whose centre is in ``keep`` are returned.
    ``None`` means the whole frame.
    """
    if regions is None:
        return [(Rect(0, 0, width, height),) * 2] if width > 0 and height > 0 else []
    clamped = [r for r in (clamp_rect(rect, width, height) for rect in regions) if r is not None]
    plan = []
    for keep in merge_regions(clamped):
        grown = Rect(keep.x - margin, keep.y - margin, keep.w + 2 * margin, keep.h + 2 * margin)
        crop = clamp_rect(grown, width, height)
        if crop is not None:
            plan.append((crop, keep))
    return plan


def centre_inside(box: Rect, area: Rect) -> bool:
    cx, cy = box.x + box.w / 2, box.y + box.h / 2
    return area.x <= cx < area.right and area.y <= cy < area.bottom


def reading_order(blocks: Sequence[TextBlock]) -> list[TextBlock]:
    """Top to bottom, then left to right. Boxes on one row (mostly overlapping in y) share it."""
    ordered = sorted(blocks, key=lambda b: (b.bbox.y, b.bbox.x))
    rows: list[list[TextBlock]] = []
    for block in ordered:
        for row in rows:
            first = row[0].bbox
            overlap = min(first.bottom, block.bbox.bottom) - max(first.y, block.bbox.y)
            if overlap > 0.5 * min(first.h, block.bbox.h):
                row.append(block)
                break
        else:
            rows.append([block])
    return [b for row in rows for b in sorted(row, key=lambda b: b.bbox.x)]


def dedupe_blocks(blocks: Sequence[TextBlock], min_iou: float = 0.5) -> list[TextBlock]:
    """Drop a line read twice because two crops overlapped. Keep the more confident read."""
    kept: list[TextBlock] = []
    for block in blocks:
        for i, other in enumerate(kept):
            if iou(block.bbox, other.bbox) >= min_iou and fuzz.ratio(block.text, other.text) >= 80:
                if (block.confidence or 0.0) > (other.confidence or 0.0):
                    kept[i] = block
                break
        else:
            kept.append(block)
    return kept
