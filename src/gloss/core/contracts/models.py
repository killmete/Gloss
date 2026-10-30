"""Data carried between stages. Frozen dataclasses of plain types and numpy arrays only.

Coordinates are always physical pixels. Three coordinate spaces exist, and each field says
which one it uses:

- **Desktop:** the Windows virtual desktop. Values can be negative, for a monitor left of
  or above the primary. Used by ``Frame.region``, ``MonitorInfo.bounds``,
  ``CaptureTarget.region`` and ``RenderItem.bbox``.
- **Frame:** ``(0, 0)`` is the top-left pixel of ``Frame.image``. Used by ``TextBlock.bbox``
  and the ``regions`` passed to ``OCREngine.recognize``.
- **Logical (Qt) coordinates** never appear here. Only renderers convert to them.

Sequences are tuples, so a frozen object stays immutable all the way down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np

HealthState = Literal["ok", "degraded", "down"]
CaptureMode = Literal["monitor", "region", "window"]
LineStatus = Literal["ok", "passthrough", "failed"]
FailureReason = Literal["refused", "missing", "truncated", "invalid_output", "other"]


@dataclass(frozen=True, slots=True)
class Rect:
    """A rectangle in physical pixels. ``w`` and ``h`` may be zero, never negative."""

    x: int
    y: int
    w: int
    h: int

    def __post_init__(self) -> None:
        if self.w < 0 or self.h < 0:
            raise ValueError("Rect width and height must not be negative")

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def area(self) -> int:
        return self.w * self.h

    def translated(self, dx: int, dy: int) -> Rect:
        return Rect(self.x + dx, self.y + dy, self.w, self.h)


@dataclass(frozen=True, slots=True)
class MonitorInfo:
    """One monitor.

    ``monitor_id`` is opaque and comes from the Windows device name (for example
    ``\\\\.\\DISPLAY1``), never from a library's monitor index. Every capture adapter must
    return the same ID for the same monitor, because ``mss`` and ``windows-capture`` number
    monitors differently.
    """

    monitor_id: str
    bounds: Rect  # desktop coordinates
    dpi_scale: float  # 1.0 = 100%
    is_primary: bool = False


@dataclass(frozen=True, slots=True)
class CaptureTarget:
    """What to capture. Coordinates are desktop coordinates.

    - ``monitor``: ``monitor_id``, or the primary monitor if None.
    - ``region``: ``region`` is required. ``monitor_id`` is optional.
    - ``window``: ``hwnd`` or ``window_title`` is required.
    """

    mode: CaptureMode
    monitor_id: str | None = None
    region: Rect | None = None
    hwnd: int | None = None
    window_title: str | None = None

    def __post_init__(self) -> None:
        if self.mode == "region" and self.region is None:
            raise ValueError("region mode needs a region")
        if self.mode == "window" and self.hwnd is None and self.window_title is None:
            raise ValueError("window mode needs hwnd or window_title")


@dataclass(frozen=True, slots=True)
class FrameMeta:
    """A frame's details without its pixels, for renderers."""

    source_id: str
    region: Rect  # desktop coordinates
    timestamp: float  # time.monotonic() seconds
    dpi_scale: float  # 1.0 = 100%


@dataclass(frozen=True, slots=True, eq=False)
class Frame:
    """One captured image.

    ``image`` has shape ``(region.h, region.w, 3)``, dtype uint8, RGB order. It must own its
    memory (no view into a capture library's buffer), and nothing may change it after
    ``grab`` returns, because it is shared between threads.

    ``fresh`` is False when the adapter knows the pixels repeat the previous frame, so the
    change detector can skip hashing. True means "changed or unknown". Compared by identity.
    """

    image: np.ndarray
    source_id: str
    region: Rect  # desktop coordinates of the image's top-left pixel
    timestamp: float  # time.monotonic() seconds
    dpi_scale: float  # 1.0 = 100%
    fresh: bool = True

    def __post_init__(self) -> None:
        if self.image.dtype != np.uint8:
            raise ValueError("Frame.image must be uint8")
        if self.image.shape != (self.region.h, self.region.w, 3):
            raise ValueError("Frame.image shape must be (region.h, region.w, 3)")
        if self.dpi_scale <= 0:
            raise ValueError("Frame.dpi_scale must be positive")

    @property
    def meta(self) -> FrameMeta:
        return FrameMeta(self.source_id, self.region, self.timestamp, self.dpi_scale)


@dataclass(frozen=True, slots=True)
class TextBlock:
    """One line of text found by OCR.

    ``bbox`` is in frame coordinates. ``confidence`` is 0.0 to 1.0, or None when the engine
    gives no score. None means unknown: it is not 0 or 1, and it skips ``min_confidence``.
    ``lang`` is a lowercase code such as ``zh``, or None if undetected.

    ``line_id`` and ``group_id`` start as None and are set by the TextTracker.
    ``group_id`` ties together the lines of one chat message, when layout makes that clear.

    ``low_quality`` is True for all output of an engine that fails the accuracy thresholds.
    Such lines are tagged on screen and never written to the translation cache.

    ``needs_translation`` is False for lines with nothing to translate, such as bare numbers.
    """

    text: str
    bbox: Rect  # frame coordinates
    confidence: float | None
    lang: str | None
    line_id: str | None = None
    low_quality: bool = False
    needs_translation: bool = True
    group_id: str | None = None

    def __post_init__(self) -> None:
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("TextBlock.confidence must be between 0 and 1, or None")


@dataclass(frozen=True, slots=True)
class GlossaryEntry:
    """A preferred translation. ``note`` is shown in "explain" mode."""

    term: str
    translation: str
    note: str | None = None


@dataclass(frozen=True, slots=True)
class Glossary:
    """Glossary entries. ``version`` changes whenever the content changes."""

    version: str = "0"
    entries: tuple[GlossaryEntry, ...] = ()


@dataclass(frozen=True, slots=True)
class ContextLine:
    """An already translated line, sent for reference only.

    Both texts are already redacted when an adapter receives them.
    """

    source_text: str
    translated_text: str


@dataclass(frozen=True, slots=True)
class TranslationItem:
    """One line to translate. ``text`` is already redacted when an adapter receives it."""

    line_id: str
    text: str
    low_quality: bool = False


@dataclass(frozen=True, slots=True)
class TranslationRequest:
    """One batch of lines. ``source_lang`` is None for auto-detect."""

    items: tuple[TranslationItem, ...]
    source_lang: str | None
    target_lang: str
    context: tuple[ContextLine, ...] = ()
    glossary: Glossary = field(default_factory=Glossary)


@dataclass(frozen=True, slots=True)
class TranslationResult:
    """The outcome for one line.

    ``status``:
    - ``ok``: ``text`` is a translation.
    - ``passthrough``: ``text`` is the original, deliberately untranslated (``null``).
    - ``failed``: this line alone failed. ``text`` is empty and ``failure`` says why.
      Failed lines move down the chain and do not count toward the circuit breaker.

    ``rough`` marks output of a provider that ignores the glossary and context, so the
    interface can label it. ``cached`` is set by the TranslationService; adapters leave it
    False. ``failure`` values are extended additively, so treat an unknown value as "other".
    """

    line_id: str
    source_text: str
    text: str
    provider: str
    model: str | None
    cached: bool
    latency_ms: int
    status: LineStatus = "ok"
    failure: FailureReason | None = None
    rough: bool = False

    def __post_init__(self) -> None:
        if (self.status == "failed") != (self.failure is not None):
            raise ValueError("failure must be set exactly when status is 'failed'")
        if self.status == "failed" and self.text:
            raise ValueError("A failed line must have empty text")


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Tokens billed for one provider call, taken from the provider's own report.

    ``input_tokens`` excludes cached tokens, matching the Anthropic ``usage`` fields. Thinking
    tokens, if any, are included in ``output_tokens``.
    """

    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True, slots=True)
class TranslationResponse:
    """What ``Translator.translate`` returns. ``usage`` is None for providers with no cost."""

    results: tuple[TranslationResult, ...]
    usage: TokenUsage | None = None


@dataclass(frozen=True, slots=True)
class RenderItem:
    """One line to display, in reading order.

    ``bbox`` is in desktop coordinates, or None if the line has no position. The pipeline
    fills it from ``TextBlock.bbox`` plus ``Frame.region``, because a batch can mix lines
    from different frames. ``group_id`` is copied from ``TextBlock``.
    """

    result: TranslationResult
    bbox: Rect | None = None
    low_quality: bool = False
    group_id: str | None = None


@dataclass(frozen=True, slots=True)
class Health:
    """A stage's state. ``detail`` is one readable sentence and holds no chat text or keys."""

    state: HealthState
    detail: str = ""


@dataclass(frozen=True, slots=True)
class CacheKey:
    """Identifies one cached translation. ``normalised_text`` is the redacted text."""

    provider: str
    model: str | None
    target_lang: str
    glossary_version: str
    normalised_text: str
