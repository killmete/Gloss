"""Pixel preparation before OCR: colour order, optional upscaling, optional dark-theme inversion.

Every step is off or neutral by default and is switched on only when a measurement on the
golden screenshots says it helps (see tests/unit/ocr and the Phase 3 report). Nothing here
writes into the caller's array: a read-only frame must stay untouched.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = ["PreprocessConfig", "prepare"]


@dataclass(frozen=True, slots=True)
class PreprocessConfig:
    """``upscale`` multiplies a crop shorter than ``upscale_below_px``; 1.0 means never.

    ``invert_dark`` flips a crop whose mean brightness is under ``dark_luma`` (0-255).
    ``max_side`` stops an upscale from growing a crop past what the detector can use.
    """

    upscale: float = 1.0
    upscale_below_px: int = 0
    invert_dark: bool = False
    dark_luma: float = 90.0
    max_side: int = 2000

    def __post_init__(self) -> None:
        if not 1.0 <= self.upscale <= 4.0:
            raise ValueError("upscale must be between 1 and 4")
        if self.upscale_below_px < 0 or self.max_side < 32:
            raise ValueError("upscale_below_px must not be negative and max_side must be >= 32")
        if not 0.0 <= self.dark_luma <= 255.0:
            raise ValueError("dark_luma must be between 0 and 255")


def prepare(rgb: np.ndarray, config: PreprocessConfig) -> tuple[np.ndarray, float]:
    """Return ``(bgr_image, scale)``. Divide a box coordinate by ``scale`` to get back to input
    pixels. The result is a new contiguous array."""
    bgr = np.ascontiguousarray(rgb[:, :, ::-1])
    if config.invert_dark and float(bgr.mean()) < config.dark_luma:
        bgr = 255 - bgr
    height, width = bgr.shape[:2]
    scale = 1.0
    if config.upscale > 1.0 and height < config.upscale_below_px:
        scale = min(config.upscale, config.max_side / max(height, width))
    if scale > 1.0:
        import cv2

        bgr = cv2.resize(
            bgr, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_CUBIC
        )
    else:
        scale = 1.0
    return bgr, scale
