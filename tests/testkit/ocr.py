"""OCR contract checks, plus a conforming fake and deliberately broken ones."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

import numpy as np
from rapidfuzz import fuzz

from gloss.core.config import load_config
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
from testkit.core import (
    COMMON_CHECKS,
    Broken,
    Subject,
    call,
    check_close_is_always_safe,
    check_health_is_instant_in_every_state,
    hostile_settings_breakers,
    identity_breakers,
    registered_subject,
)
from testkit.discovery import NO_USER_CONFIG
from testkit.frames import (
    CANVAS_H,
    CANVAS_W,
    make_frame,
    normalise,
    overlap_share,
    row_boxes,
    text_frame,
)

PORT = "ocr"
FAKE_TIMEOUT_S = 0.3
ROW_BOXES = row_boxes()
TOP_TEXT, TOP_BOX = ROW_BOXES[0]
BOTTOM_TEXT, BOTTOM_BOX = ROW_BOXES[1]
BOTTOM_HALF = Rect(0, CANVAS_H // 2, CANVAS_W, CANVAS_H // 2)
TOP_HALF = Rect(0, 0, CANVAS_W, CANVAS_H // 2)
# Where the frame sits on the desktop. A monitor left of the primary has negative values.
DESKTOP_REGIONS = (Rect(1000, 500, CANVAS_W, CANVAS_H), Rect(-1920, -200, CANVAS_W, CANVAS_H))


def ocr_timeout_s(subject: Subject | None = None) -> float:
    """The limit a call must meet. Fakes set their own so the suite stays fast."""
    if subject is not None and subject.timeout_s is not None:
        return subject.timeout_s
    return load_config(user_path=NO_USER_CONFIG).config.reliability.ocr_timeout_ms / 1000


def needs_english(adapter: Any) -> None:
    if "en" not in adapter.languages:
        import pytest

        pytest.skip(f"{adapter.name} does not read English, and the test text is English")


def recognise(adapter: Any, frame: Frame, regions: Sequence[Rect] | None) -> list[TextBlock]:
    blocks = call(adapter.recognize, frame, regions)
    assert isinstance(blocks, Sequence) and not isinstance(blocks, str), (
        f"recognize() must return a sequence, got {type(blocks).__name__}"
    )
    assert all(isinstance(b, TextBlock) for b in blocks), "recognize() must return TextBlocks"
    return list(blocks)


def block_for(blocks: Sequence[TextBlock], text: str) -> TextBlock | None:
    wanted = normalise(text)
    best = max(blocks, key=lambda b: fuzz.partial_ratio(wanted, normalise(b.text)), default=None)
    if best is not None and fuzz.partial_ratio(wanted, normalise(best.text)) >= 85:
        return best
    return None


def assert_block_fields(blocks: Sequence[TextBlock], frame: Frame) -> None:
    low = {b.low_quality for b in blocks}
    assert len(low) <= 1, "low_quality is a property of the engine, so one call must not mix it"
    for b in blocks:
        assert b.text == b.text.strip() and b.text, f"empty or unstripped text {b.text!r}"
        assert "\n" not in b.text and "\r" not in b.text, (
            "one TextBlock is one line of text; split multi-line output"
        )
        assert b.confidence is None or 0.0 <= b.confidence <= 1.0
        assert b.lang is None or (b.lang == b.lang.lower() and b.lang.isalpha()), (
            f"lang must be a lowercase code or None, got {b.lang!r}"
        )
        assert b.line_id is None, "line_id is set by the TextTracker, not by OCR"
        assert isinstance(b.needs_translation, bool) and isinstance(b.low_quality, bool)
        inside = (
            b.bbox.x >= 0
            and b.bbox.y >= 0
            and b.bbox.right <= frame.region.w
            and b.bbox.bottom <= frame.region.h
        )
        assert inside, (
            f"bbox {b.bbox} is not inside the frame (0, 0, {frame.region.w}, {frame.region.h}). "
            "TextBlock.bbox uses frame coordinates, never desktop coordinates."
        )


def assert_top_to_bottom(blocks: Sequence[TextBlock]) -> None:
    tops = [b.bbox.y for b in blocks]
    assert tops == sorted(tops), "blocks must be ordered top to bottom"


# ---- checks ------------------------------------------------------------------------------


def check_declares_languages(subject: Subject) -> None:
    adapter = subject.new()
    langs = adapter.languages
    assert isinstance(langs, tuple) and langs, "languages must be a non-empty tuple"
    assert all(isinstance(x, str) and x == x.lower() and 2 <= len(x) <= 3 for x in langs), (
        f"languages must be lowercase codes like 'zh', got {langs}"
    )


def check_empty_regions_means_no_blocks(subject: Subject) -> None:
    adapter = subject.opened()
    assert recognise(adapter, text_frame(), []) == [], "an empty regions list returns no blocks"
    assert recognise(adapter, text_frame(), (Rect(10, 10, 0, 0),)) == [], (
        "a zero-area region returns no blocks"
    )


def check_blank_frame_has_no_text_and_is_quick(subject: Subject) -> None:
    adapter = subject.opened()
    frame = make_frame(1920, 1080)
    start = time.perf_counter()
    blocks = recognise(adapter, frame, None)
    took = time.perf_counter() - start
    assert blocks == [], f"a blank frame produced text: {[b.text for b in blocks]}"
    assert took < ocr_timeout_s(subject), (
        f"a blank 1080p frame took {took:.1f} s, over reliability.ocr_timeout_ms"
    )


def check_whole_frame_reads_both_lines_in_frame_coordinates(subject: Subject) -> None:
    adapter = subject.opened()
    needs_english(adapter)
    frame = text_frame()
    blocks = recognise(adapter, frame, None)
    assert_block_fields(blocks, frame)
    assert_top_to_bottom(blocks)
    for text, box in ROW_BOXES:
        found = block_for(blocks, text)
        assert found is not None, f"did not read {text!r}; got {[b.text for b in blocks]}"
        assert overlap_share(box, found.bbox) >= 0.5, (
            f"{text!r} is at {box} but its bbox is {found.bbox}"
        )


def check_frame_position_on_the_desktop_never_leaks_into_bboxes(subject: Subject) -> None:
    adapter = subject.opened()
    needs_english(adapter)
    for where in DESKTOP_REGIONS:
        frame = text_frame(region=where)
        blocks = recognise(adapter, frame, None)
        assert_block_fields(blocks, frame)
        found = block_for(blocks, TOP_TEXT)
        assert found is not None, f"did not read {TOP_TEXT!r} for a frame at {where}"
        assert overlap_share(TOP_BOX, found.bbox) >= 0.5, (
            f"frame at desktop {where}: bbox {found.bbox} should stay in frame coordinates"
        )


def check_regions_limit_the_search_but_keep_frame_coordinates(subject: Subject) -> None:
    adapter = subject.opened()
    needs_english(adapter)
    frame = text_frame()
    blocks = recognise(adapter, frame, [BOTTOM_HALF])
    assert_block_fields(blocks, frame)
    assert block_for(blocks, TOP_TEXT) is None, "text outside the region was returned"
    found = block_for(blocks, BOTTOM_TEXT)
    assert found is not None, f"did not read {BOTTOM_TEXT!r} inside the region"
    assert overlap_share(BOTTOM_BOX, found.bbox) >= 0.5, (
        f"bbox {found.bbox} is not in frame coordinates (text is at {BOTTOM_BOX}). "
        "A bbox relative to the region's corner is the classic mistake."
    )
    both = recognise(adapter, frame, [TOP_HALF, BOTTOM_HALF])
    assert_top_to_bottom(both)
    assert len([b for b in both if block_for([b], TOP_TEXT)]) == 1, "a line was returned twice"
    assert block_for(both, BOTTOM_TEXT) is not None


def check_odd_regions_are_clamped_or_refused_never_a_crash(subject: Subject) -> None:
    adapter = subject.opened()
    frame = text_frame()
    for region in (Rect(-50, -50, 99999, 99999), Rect(CANVAS_W + 10, CANVAS_H + 10, 40, 40)):
        try:
            blocks = recognise(adapter, frame, [region])
        except GlossError:
            continue
        assert_block_fields(blocks, frame)


def check_reads_a_read_only_frame_without_changing_it(subject: Subject) -> None:
    """Frames are shared between threads, so an adapter must never write into one."""
    adapter = subject.opened()
    frame = text_frame()
    frame.image.flags.writeable = False
    before = frame.image.copy()
    recognise(adapter, frame, None)
    recognise(adapter, frame, [BOTTOM_HALF])
    assert np.array_equal(frame.image, before), "recognize() changed the frame it was given"


def check_block_fields_follow_the_contract(subject: Subject) -> None:
    adapter = subject.opened()
    needs_english(adapter)
    frame = text_frame()
    blocks = recognise(adapter, frame, None)
    assert blocks, "no text was read from a clean black-on-white frame"
    assert_block_fields(blocks, frame)


def check_same_frame_gives_the_same_text(subject: Subject) -> None:
    adapter = subject.opened()
    needs_english(adapter)
    frame = text_frame()
    first = [b.text for b in recognise(adapter, frame, None)]
    second = [b.text for b in recognise(adapter, frame, None)]
    assert first == second, f"the same frame read differently: {first} then {second}"


def check_unsupported_language_is_refused_not_guessed(subject: Subject) -> None:
    """ports.py: raise AdapterUnavailable (or PermanentError), never guess or call it transient."""
    adapter = subject.new({"languages": ["zz"]})
    try:
        call(adapter.open)
        blocks = call(adapter.recognize, text_frame(), None)
    except (AdapterUnavailable, PermanentError):
        return
    except GlossError as exc:
        raise AssertionError(
            f"an unsupported language raised {type(exc).__name__}. It is not a temporary "
            "problem, so only AdapterUnavailable or PermanentError is acceptable."
        ) from exc
    finally:
        call(adapter.close)
    raise AssertionError(
        f"languages=['zz'] was accepted and recognize() returned {len(blocks)} blocks. "
        "An engine with no recogniser for a language must raise, not guess."
    )


def check_open_without_models_is_unavailable_not_a_crash(subject: Subject) -> None:
    adapter = subject.new()
    try:
        call(adapter.open)
    except AdapterUnavailable:
        pass  # The documented answer for a missing model or language pack.
    finally:
        call(adapter.close)


CHECKS = (
    *COMMON_CHECKS,
    check_declares_languages,
    check_empty_regions_means_no_blocks,
    check_blank_frame_has_no_text_and_is_quick,
    check_whole_frame_reads_both_lines_in_frame_coordinates,
    check_frame_position_on_the_desktop_never_leaks_into_bboxes,
    check_regions_limit_the_search_but_keep_frame_coordinates,
    check_odd_regions_are_clamped_or_refused_never_a_crash,
    check_reads_a_read_only_frame_without_changing_it,
    check_block_fields_follow_the_contract,
    check_same_frame_gives_the_same_text,
    check_unsupported_language_is_refused_not_guessed,
    check_open_without_models_is_unavailable_not_a_crash,
)
WINDOWS_ONLY: frozenset = frozenset()


def check_windows_ocr_is_never_an_automatic_fallback_and_is_low_quality(
    subject: Subject,
) -> None:
    """ADR-0001: the Windows engine is opt-in only, and all of its output is low quality."""
    adapter = subject.opened()
    cls = type(adapter)
    assert getattr(cls, "ALLOW_AUTO_FALLBACK", True) is False, (
        "a low-accuracy engine must set ALLOW_AUTO_FALLBACK = False"
    )
    blocks = recognise(adapter, text_frame(), None)
    assert all(b.low_quality for b in blocks), "every block from this engine is low_quality"


# ---- fakes -------------------------------------------------------------------------------


def _ink(frame: Frame, box: Rect) -> bool:
    """True if the box holds dark pixels, so the fake only 'reads' text that is really there."""
    patch = frame.image[box.y : box.bottom, box.x : box.right]
    return bool(patch.size and patch.min() < 128)


class FakeOCR:
    """Follows the OCR contract. It 'reads' the two known test rows from the pixels."""

    name = "fake_good"
    REQUIRES_CONTRACT = ">=1.0,<2.0"
    languages = ("en",)
    ALLOW_AUTO_FALLBACK = True
    LOW_QUALITY = False

    def __init__(self, context: AdapterContext) -> None:
        self._open = False
        self._langs = tuple(context.settings.get("languages", ("en",)))
        if not isinstance(self._langs, tuple) or not all(isinstance(x, str) for x in self._langs):
            self._langs = ("en",)

    def open(self) -> None:
        self._open = True

    def _regions(self, frame: Frame, regions: Sequence[Rect] | None) -> list[Rect]:
        if regions is None:
            return [Rect(0, 0, frame.region.w, frame.region.h)]
        return list(regions)

    def _make(self, text: str, box: Rect) -> TextBlock:
        return TextBlock(text, box, 0.97, "en", low_quality=self.LOW_QUALITY)

    def recognize(self, frame: Frame, regions: Sequence[Rect] | None) -> Sequence[TextBlock]:
        if not set(self._langs) <= set(self.languages):
            raise AdapterUnavailable("no recogniser for the requested language")
        found = []
        for text, box in ROW_BOXES:
            if any(overlap_share(box, r) > 0.5 for r in self._regions(frame, regions)):
                if _ink(frame, box):
                    found.append(self._make(text, box))
        return sorted(found, key=lambda b: b.bbox.y)

    def close(self) -> None:
        self._open = False

    def health(self) -> Health:
        return Health("ok" if self._open else "down", "")


class DesktopCoordsOCR(FakeOCR):
    name = "fake_desktop_coords"

    def _make(self, text: str, box: Rect) -> TextBlock:
        return super()._make(text, box.translated(self._dx, self._dy))

    def recognize(self, frame: Frame, regions: Sequence[Rect] | None) -> Sequence[TextBlock]:
        self._dx, self._dy = frame.region.x, frame.region.y
        return super().recognize(frame, regions)


class RegionRelativeOCR(FakeOCR):
    name = "fake_region_relative"

    def recognize(self, frame: Frame, regions: Sequence[Rect] | None) -> Sequence[TextBlock]:
        blocks = super().recognize(frame, regions)
        if not regions:
            return blocks
        origin = regions[0]
        return [
            TextBlock(b.text, b.bbox.translated(-origin.x, -origin.y), b.confidence, b.lang)
            for b in blocks
        ]


class IgnoresRegionsOCR(FakeOCR):
    name = "fake_ignores_regions"

    def recognize(self, frame: Frame, regions: Sequence[Rect] | None) -> Sequence[TextBlock]:
        return super().recognize(frame, None if regions else regions)


class EmptyMeansWholeFrameOCR(FakeOCR):
    name = "fake_empty_means_all"

    def recognize(self, frame: Frame, regions: Sequence[Rect] | None) -> Sequence[TextBlock]:
        return super().recognize(frame, regions or None)


class MultilineBlockOCR(FakeOCR):
    name = "fake_multiline"

    def recognize(self, frame: Frame, regions: Sequence[Rect] | None) -> Sequence[TextBlock]:
        blocks = list(super().recognize(frame, regions))
        if len(blocks) < 2:
            return blocks
        top, bottom = blocks[0], blocks[1]
        joined = Rect(0, top.bbox.y, frame.region.w, bottom.bbox.bottom - top.bbox.y)
        return [TextBlock(f"{top.text}\n{bottom.text}", joined, 0.9, "en")]


class BottomFirstOCR(FakeOCR):
    name = "fake_bottom_first"

    def recognize(self, frame: Frame, regions: Sequence[Rect] | None) -> Sequence[TextBlock]:
        return list(reversed(list(super().recognize(frame, regions))))


class WritesIntoFrameOCR(FakeOCR):
    name = "fake_writes_frame"

    def recognize(self, frame: Frame, regions: Sequence[Rect] | None) -> Sequence[TextBlock]:
        frame.image[0, 0, 0] = 1  # in-place preprocessing
        return super().recognize(frame, regions)


class BlankHallucinationOCR(FakeOCR):
    name = "fake_hallucinates"

    def recognize(self, frame: Frame, regions: Sequence[Rect] | None) -> Sequence[TextBlock]:
        blocks = super().recognize(frame, regions)
        if regions != [] and not blocks and frame.region.w > 100:
            return [TextBlock("noise", Rect(0, 0, 10, 10), None, None)]
        return blocks


class MixedLowQualityOCR(FakeOCR):
    name = "fake_mixed_quality"

    def recognize(self, frame: Frame, regions: Sequence[Rect] | None) -> Sequence[TextBlock]:
        blocks = list(super().recognize(frame, regions))
        return [
            TextBlock(b.text, b.bbox, b.confidence, b.lang, low_quality=(i == 0))
            for i, b in enumerate(blocks)
        ]


class SetsLineIdOCR(FakeOCR):
    name = "fake_sets_line_id"

    def _make(self, text: str, box: Rect) -> TextBlock:
        return TextBlock(text, box, 0.9, "en", line_id="L1")


class UpperCaseLangOCR(FakeOCR):
    name = "fake_uppercase_lang"

    def _make(self, text: str, box: Rect) -> TextBlock:
        return TextBlock(text, box, 0.9, "EN")


class PaddedTextOCR(FakeOCR):
    name = "fake_padded_text"

    def _make(self, text: str, box: Rect) -> TextBlock:
        return TextBlock(f" {text} ", box, 0.9, "en")


class RawErrorOCR(FakeOCR):
    name = "fake_raw_error"

    def recognize(self, frame: Frame, regions: Sequence[Rect] | None) -> Sequence[TextBlock]:
        if regions and regions[0].x < 0:
            raise IndexError("slice outside the image")
        return super().recognize(frame, regions)


class GuessesLanguageOCR(FakeOCR):
    name = "fake_guesses_language"

    def recognize(self, frame: Frame, regions: Sequence[Rect] | None) -> Sequence[TextBlock]:
        self._langs = ("en",)  # quietly reads with the wrong recogniser
        return super().recognize(frame, regions)


class TransientForLanguageOCR(FakeOCR):
    name = "fake_transient_language"

    def recognize(self, frame: Frame, regions: Sequence[Rect] | None) -> Sequence[TextBlock]:
        if not set(self._langs) <= set(self.languages):
            raise TransientError("try again")
        return super().recognize(frame, regions)


class PermanentForLanguageOCR(FakeOCR):
    name = "fake_permanent_language"

    def recognize(self, frame: Frame, regions: Sequence[Rect] | None) -> Sequence[TextBlock]:
        if not set(self._langs) <= set(self.languages):
            raise PermanentError("language not supported")
        return super().recognize(frame, regions)


class SlowOCR(FakeOCR):
    name = "fake_slow"

    def recognize(self, frame: Frame, regions: Sequence[Rect] | None) -> Sequence[TextBlock]:
        if frame.region.w > 1000:
            time.sleep(FAKE_TIMEOUT_S + 0.2)
        return super().recognize(frame, regions)


class FlakyOCR(FakeOCR):
    name = "fake_flaky"
    _n = 0

    def _make(self, text: str, box: Rect) -> TextBlock:
        type(self)._n += 1
        return super()._make(f"{text}{self._n}", box)


class CloseCrashOCR(FakeOCR):
    name = "fake_close_crash"

    def close(self) -> None:
        if not self._open:
            raise RuntimeError("not open")
        super().close()


class OpenCrashOCR(FakeOCR):
    name = "fake_open_crash"

    def open(self) -> None:
        raise OSError("model file missing")


class SlowHealthOCR(FakeOCR):
    name = "fake_slow_health"

    def health(self) -> Health:
        time.sleep(0.2)
        return super().health()


class BadLanguagesOCR(FakeOCR):
    name = "fake_bad_languages"
    languages = ["zh", "EN"]  # type: ignore[assignment]


class LowQualityEngine(FakeOCR):
    name = "windows"
    LOW_QUALITY = True
    ALLOW_AUTO_FALLBACK = False


class UnflaggedWindowsOCR(FakeOCR):
    name = "windows"


def fake_subject(adapter: type = FakeOCR, *, via_registry: bool = True) -> Subject:
    return registered_subject(
        PORT, adapter.name, adapter, via_registry=via_registry, timeout_s=FAKE_TIMEOUT_S
    )


BROKEN = (
    Broken(
        DesktopCoordsOCR,
        check_frame_position_on_the_desktop_never_leaks_into_bboxes,
        "bboxes include the frame's desktop position",
    ),
    Broken(
        RegionRelativeOCR,
        check_regions_limit_the_search_but_keep_frame_coordinates,
        "bboxes relative to the region corner",
    ),
    Broken(
        IgnoresRegionsOCR,
        check_regions_limit_the_search_but_keep_frame_coordinates,
        "reads outside the regions",
    ),
    Broken(
        EmptyMeansWholeFrameOCR,
        check_empty_regions_means_no_blocks,
        "an empty region list reads the whole frame",
    ),
    Broken(MultilineBlockOCR, check_block_fields_follow_the_contract, "one block, two lines"),
    Broken(BottomFirstOCR, check_whole_frame_reads_both_lines_in_frame_coordinates, "wrong order"),
    Broken(
        WritesIntoFrameOCR,
        check_reads_a_read_only_frame_without_changing_it,
        "writes into the shared frame",
    ),
    Broken(
        BlankHallucinationOCR,
        check_blank_frame_has_no_text_and_is_quick,
        "invents text on a blank frame",
    ),
    Broken(
        MixedLowQualityOCR,
        check_block_fields_follow_the_contract,
        "low_quality differs inside one call",
    ),
    Broken(SetsLineIdOCR, check_block_fields_follow_the_contract, "OCR sets line_id"),
    Broken(UpperCaseLangOCR, check_block_fields_follow_the_contract, "lang is not lowercase"),
    Broken(PaddedTextOCR, check_block_fields_follow_the_contract, "text has padding"),
    Broken(
        RawErrorOCR,
        check_odd_regions_are_clamped_or_refused_never_a_crash,
        "IndexError escapes for an odd region",
    ),
    Broken(
        GuessesLanguageOCR,
        check_unsupported_language_is_refused_not_guessed,
        "reads an unsupported language anyway",
    ),
    Broken(
        TransientForLanguageOCR,
        check_unsupported_language_is_refused_not_guessed,
        "TransientError for an unsupported language",
    ),
    Broken(SlowOCR, check_blank_frame_has_no_text_and_is_quick, "ignores the OCR time limit"),
    Broken(FlakyOCR, check_same_frame_gives_the_same_text, "same frame reads differently"),
    Broken(CloseCrashOCR, check_close_is_always_safe, "close() without open() raises"),
    Broken(
        OpenCrashOCR,
        check_open_without_models_is_unavailable_not_a_crash,
        "OSError instead of AdapterUnavailable",
    ),
    Broken(SlowHealthOCR, check_health_is_instant_in_every_state, "health() blocks"),
    Broken(BadLanguagesOCR, check_declares_languages, "languages is a list with 'EN'"),
)


# Name-specific rules. They apply to an adapter registered as "windows", and to these fakes.
WINDOWS_RULE_BROKEN = Broken(
    UnflaggedWindowsOCR,
    check_windows_ocr_is_never_an_automatic_fallback_and_is_low_quality,
    "the Windows-style engine can be an automatic fallback and is not marked low quality",
)

BROKEN = (*BROKEN, *identity_breakers(FakeOCR), *hostile_settings_breakers(FakeOCR))
