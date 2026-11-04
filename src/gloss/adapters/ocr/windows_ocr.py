"""Windows.Media.Ocr as an ``OCREngine``: a manual, low-quality option (ADR-0001 section 2).

Measured on the golden screenshots it falls well short of the accuracy gates, so:

- ``ALLOW_AUTO_FALLBACK = False``: it is never entered from a fallback list, only chosen by the
  user as ``ocr.engine``;
- every block is ``low_quality`` and has ``confidence=None`` (the API gives no score);
- a missing Windows language pack raises ``AdapterUnavailable`` with the exact command to run.
  Another language's recogniser is never used instead.

Windows reads one language per engine object, so one engine is made per requested language and
the results are merged line by line. Heavy WinRT imports happen inside ``open``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Sequence
from typing import Any

import numpy as np

from gloss.core.contracts import (
    AdapterContext,
    AdapterUnavailable,
    Frame,
    GlossError,
    Health,
    PermanentError,
    Rect,
    TextBlock,
    TransientError,
)
from gloss.core.registry import register_adapter
from gloss.services.ocr_lang import classify_line, detect_lang
from gloss.services.ocr_preprocess import PreprocessConfig, prepare
from gloss.services.ocr_regions import (
    centre_inside,
    clamp_rect,
    iou,
    plan_regions,
    reading_order,
)
from gloss.services.ocr_settings import read_bool, read_languages, read_number

log = logging.getLogger(__name__)

# Gloss code -> (Windows language tag, name of the Windows capability that adds the recogniser)
WINDOWS_LANGS = {
    "zh": ("zh-Hans-CN", "Language.OCR~~~zh-CN~0.0.1.0"),
    "en": ("en-US", "Language.OCR~~~en-US~0.0.1.0"),
    "ja": ("ja", "Language.OCR~~~ja-JP~0.0.1.0"),
}
_CJK_GAP = re.compile(
    r"(?<=[⺀-鿿぀-ヿ豈-﫿＀-￯])\s+"
    r"(?=[⺀-鿿぀-ヿ豈-﫿＀-￯])"
)
_CJK = re.compile(r"[⺀-鿿぀-ヿ豈-﫿]")


def _tidy(text: str) -> str:
    """Windows puts a space between every pair of CJK characters. Take those out."""
    return _CJK_GAP.sub("", " ".join(text.split()))


@register_adapter("ocr", "windows")
class WindowsOCREngine:
    name = "windows"
    REQUIRES_CONTRACT = ">=1.0,<2.0"
    ALLOW_AUTO_FALLBACK = False

    def __init__(self, context: AdapterContext) -> None:
        s = context.settings
        self.languages: tuple[str, ...] = read_languages(s)
        self._margin = read_number(s, "region_margin_px", 16, minimum=0, maximum=256, integer=True)
        self._skip_langs = tuple(str(x) for x in s.get("skip_translation_langs", ()))
        self._pre = PreprocessConfig(
            upscale=read_number(s, "upscale", 1.0, minimum=1.0, maximum=4.0),
            upscale_below_px=read_number(
                s, "upscale_below_px", 0, minimum=0, maximum=4096, integer=True
            ),
            invert_dark=read_bool(s, "invert_dark", False),
            max_side=read_number(s, "max_side_len", 2000, minimum=256, maximum=8192, integer=True),
        )
        self._engines: list[tuple[str, Any]] = []
        self._winrt: Any = None
        self._max_dim = 0
        self._health = Health("down", "Windows OCR is not open")
        self.last_ms = 0.0

    # ---- lifecycle -----------------------------------------------------------------------

    def open(self) -> None:
        self.close()
        try:
            self._start()
        except GlossError as exc:
            self._health = Health("down", str(exc))
            raise
        except Exception as exc:
            self._health = Health("down", f"Windows OCR failed to start ({type(exc).__name__})")
            raise AdapterUnavailable(
                f"Windows OCR could not start ({type(exc).__name__}). It needs Windows 10 or "
                "11 and the winrt packages from 'uv sync'."
            ) from exc
        self._health = Health("degraded", "Low-quality OCR: text may be wrong or missing")

    def _start(self) -> None:
        for code in self.languages:
            if code not in WINDOWS_LANGS:
                raise PermanentError(f"'{code}' is not a language the Windows OCR engine can read")
        try:
            from winrt.windows.globalization import Language
            from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
            from winrt.windows.media.ocr import OcrEngine
            from winrt.windows.storage.streams import DataWriter
        except ImportError as exc:
            raise AdapterUnavailable(
                "The Windows OCR packages are missing. Run 'uv sync' in the Gloss folder."
            ) from exc
        engines: list[tuple[str, Any]] = []
        missing: list[str] = []
        for code in self.languages:
            tag, capability = WINDOWS_LANGS[code]
            engine = None
            if OcrEngine.is_language_supported(Language(tag)):
                engine = OcrEngine.try_create_from_language(Language(tag))
            if engine is None:
                missing.append(
                    f"{code}: open PowerShell as administrator and run "
                    f'Add-WindowsCapability -Online -Name "{capability}"'
                )
            else:
                engines.append((code, engine))
        if missing:
            raise AdapterUnavailable(
                "Windows OCR is missing a language pack. Fix, then restart Gloss: "
                + "; ".join(missing)
            )
        self._engines = engines
        self._max_dim = int(OcrEngine.max_image_dimension)
        self._winrt = (SoftwareBitmap, BitmapPixelFormat, DataWriter)

    def close(self) -> None:
        self._engines = []
        self._winrt = None
        self._health = Health("down", "Windows OCR is not open")

    def health(self) -> Health:
        return self._health

    # ---- reading -------------------------------------------------------------------------

    def recognize(self, frame: Frame, regions: Sequence[Rect] | None) -> Sequence[TextBlock]:
        if not self._engines:
            raise AdapterUnavailable("Windows OCR is not open. Call open() first.")
        height, width = frame.image.shape[:2]
        started = time.perf_counter()
        blocks: list[TextBlock] = []
        try:
            for crop, keep in plan_regions(regions, width, height, self._margin):
                blocks.extend(self._read_crop(frame.image, crop, keep, width, height))
        except GlossError:
            raise
        except Exception as exc:
            self._health = Health("degraded", f"The last OCR call failed ({type(exc).__name__})")
            raise TransientError(f"Windows OCR call failed ({type(exc).__name__})") from exc
        self.last_ms = (time.perf_counter() - started) * 1000
        self._health = Health("degraded", "Low-quality OCR: text may be wrong or missing")
        return reading_order(blocks)

    def _read_crop(
        self, image: np.ndarray, crop: Rect, keep: Rect, width: int, height: int
    ) -> list[TextBlock]:
        bgr, scale = prepare(image[crop.y : crop.bottom, crop.x : crop.right], self._pre)
        shrink = 1.0
        if max(bgr.shape[:2]) > self._max_dim:
            import cv2

            shrink = self._max_dim / max(bgr.shape[:2])
            bgr = cv2.resize(bgr, None, fx=shrink, fy=shrink, interpolation=cv2.INTER_AREA)
        scale *= shrink
        bgra = np.dstack([bgr, np.full(bgr.shape[:2], 255, np.uint8)])
        candidates: list[tuple[int, str, TextBlock]] = []
        for code, engine in self._engines:
            result = self._run(engine, bgra)
            for line in result.lines:
                rects = [w.bounding_rect for w in line.words]
                text = _tidy(line.text)
                if not rects or not text:
                    continue
                left = min(r.x for r in rects) / scale + crop.x
                top = min(r.y for r in rects) / scale + crop.y
                right = max(r.x + r.width for r in rects) / scale + crop.x
                bottom = max(r.y + r.height for r in rects) / scale + crop.y
                box = clamp_rect(
                    Rect(
                        int(left),
                        int(top),
                        int(right + 0.999) - int(left),
                        int(bottom + 0.999) - int(top),
                    ),
                    width,
                    height,
                )
                if box is None or not centre_inside(box, keep):
                    continue
                lang, needs = classify_line(text, self._skip_langs)
                block = TextBlock(
                    text=text,
                    bbox=box,
                    confidence=None,
                    lang=lang,
                    low_quality=True,
                    needs_translation=needs,
                )
                candidates.append((self._preference(code, text), code, block))
        return self._merge(candidates)

    @staticmethod
    def _preference(code: str, text: str) -> int:
        """Which engine to believe about a line when several read the same place.

        The Chinese engine often turns Latin letters it cannot place into a stray Han
        character, so a line that is mostly Latin with at most one Han character goes to the
        English engine. A line with real CJK content goes to the CJK engines.
        """
        cjk = len(_CJK.findall(text))
        letters = sum(1 for c in text if c.isalpha())
        if cjk >= 2 or (cjk and cjk * 2 >= letters):
            if code == "ja" and detect_lang(text) == "ja":
                return 4
            return 3 if code in ("zh", "ja") else 0
        return 2 if code == "en" else 1

    @staticmethod
    def _merge(candidates: list[tuple[int, str, TextBlock]]) -> list[TextBlock]:
        chosen: list[tuple[int, TextBlock]] = []
        for rank, _code, block in candidates:
            for i, (other_rank, other) in enumerate(chosen):
                if iou(block.bbox, other.bbox) >= 0.4:
                    if (rank, len(block.text)) > (other_rank, len(other.text)):
                        chosen[i] = (rank, block)
                    break
            else:
                chosen.append((rank, block))
        return [block for _rank, block in chosen]

    def _run(self, engine: Any, bgra: np.ndarray) -> Any:
        software_bitmap, pixel_format, data_writer = self._winrt
        height, width = bgra.shape[:2]
        writer = data_writer()
        writer.write_bytes(bgra.tobytes())
        bitmap = software_bitmap.create_copy_from_buffer(
            writer.detach_buffer(), pixel_format.BGRA8, width, height
        )

        async def read() -> Any:
            return await engine.recognize_async(bitmap)

        return asyncio.run(read())
