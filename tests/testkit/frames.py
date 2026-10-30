"""Synthetic frames for capture, OCR and change-detection tests."""

from __future__ import annotations

import re
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from gloss.core.contracts import Frame, Rect

CANVAS_W = 800
CANVAS_H = 400
# Two lines of text, one in each half of the canvas.
ROWS = (("HELLO WORLD", (60, 40)), ("SECOND LINE", (60, 280)))
FONT_SIZE = 36
_FONT = ImageFont.load_default(size=FONT_SIZE)


def make_frame(
    w: int = 64,
    h: int = 48,
    *,
    region: Rect | None = None,
    fill: int = 255,
    fresh: bool = True,
    dpi_scale: float = 1.0,
    source_id: str = "test",
) -> Frame:
    region = region if region is not None else Rect(0, 0, w, h)
    image = np.full((region.h, region.w, 3), fill, dtype=np.uint8)
    return Frame(image, source_id, region, time.monotonic(), dpi_scale, fresh)


def row_boxes() -> tuple[tuple[str, Rect], ...]:
    """The text and pixel box of each row, in frame coordinates."""
    scratch = ImageDraw.Draw(Image.new("RGB", (CANVAS_W, CANVAS_H)))
    boxes = []
    for text, xy in ROWS:
        left, top, right, bottom = scratch.textbbox(xy, text, font=_FONT)
        boxes.append((text, Rect(left, top, right - left, bottom - top)))
    return tuple(boxes)


def text_frame(*, region: Rect | None = None, rows: tuple[int, ...] = (0, 1)) -> Frame:
    """Black text on white. ``region`` sets where the frame sits on the desktop."""
    image = Image.new("RGB", (CANVAS_W, CANVAS_H), "white")
    draw = ImageDraw.Draw(image)
    for index in rows:
        text, xy = ROWS[index]
        draw.text(xy, text, font=_FONT, fill="black")
    array = np.array(image, dtype=np.uint8)
    where = region if region is not None else Rect(0, 0, CANVAS_W, CANVAS_H)
    return Frame(array, "test", where, time.monotonic(), 1.0)


def normalise(text: str) -> str:
    return re.sub(r"\W+", "", text).lower()


def overlap_share(inner: Rect, outer: Rect) -> float:
    """Share of ``inner``'s area that lies inside ``outer``."""
    width = min(inner.right, outer.right) - max(inner.x, outer.x)
    height = min(inner.bottom, outer.bottom) - max(inner.y, outer.y)
    if width <= 0 or height <= 0 or inner.area == 0:
        return 0.0
    return width * height / inner.area
