"""End-to-end replay: a chat screenshot, then the same chat scrolled, then unchanged frames,
through the whole real pipeline.

Real: ChangeDetector, RapidOCR (fast profile), TextTracker, TranslationService with its SQLite
cache in a temporary folder, the pipeline's workers and the app's own
``make_translation_factory``. Fake: the capture source (serves crops of a golden screenshot)
and the translator (counts what it is asked, so no network, no key, no spend). The adapters
live in a private Registry.

The screenshot is a local, gitignored golden image, so on a machine without it (CI) the test
skips and says why. It takes a few seconds, so it runs in the normal ``uv run pytest``.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from rapidfuzz import fuzz
from testkit.golden import EXPECTED, IMAGES
from testkit.pipeline import CaptureScript, RecordingSink, fast_config, wait_until

from gloss.core.contracts import (
    AdapterContext,
    AdapterUnavailable,
    CaptureTarget,
    Health,
    Rect,
    TranslationItem,
    TranslationRequest,
    TranslationResponse,
    TranslationResult,
)
from gloss.core.pipeline import Pipeline
from gloss.core.registry import Registry, default_registry

CHAT = "Screenshot 2026-10-30 220807"
SCROLL_PX = 120
ORIGIN = (300, 200)  # where the captured area sits on the (virtual) desktop
CJK = re.compile("[⺀-鿿぀-ヿ豈-﫿]")


def _needs_files() -> Path:
    image = IMAGES / f"{CHAT}.png"
    if not image.is_file() or not (EXPECTED / f"{CHAT}.txt").is_file():
        pytest.skip(
            f"golden chat screenshot '{CHAT}' is not on this machine: tests/golden/images/ is "
            "gitignored because it holds other people's chats"
        )
    return image


class SpyTranslator:
    """A fake provider. Not LOCAL_ONLY, so it also proves the cloud switch is on."""

    name = "fake"
    REQUIRES_CONTRACT = ">=1.0,<2.0"
    requests: list[tuple[str, ...]] = []

    def __init__(self, context: AdapterContext) -> None:
        pass

    def open(self) -> None:
        pass

    def translate(self, request: TranslationRequest) -> TranslationResponse:
        type(self).requests.append(tuple(i.text for i in request.items))
        results = tuple(
            TranslationResult(i.line_id, i.text, f"EN<{i.text}>", "fake", "fake-model", False, 1)
            for i in request.items
        )
        return TranslationResponse(results, None)

    def close(self) -> None:
        pass

    def health(self) -> Health:
        return Health("ok")


@dataclass
class Replay:
    sink: RecordingSink
    image_shape: tuple[int, int]
    sent_lines: list[tuple[str, str]] = field(default_factory=list)  # (line_id, text)
    provider_texts: list[str] = field(default_factory=list)
    after_a: int = 0
    after_b: int = 0
    after_unchanged: int = 0
    ocr_calls_before_unchanged: int = 0
    ocr_calls_after_unchanged: int = 0
    health_at_end: Health = Health("ok")
    sink_health_at_end: Health | None = None
    restart_results: tuple[TranslationResult, ...] = ()
    restart_provider_calls: int = 0
    scroll_px: int = SCROLL_PX


def _settle(count, quiet: float = 1.2, timeout: float = 60.0, minimum: int = 1) -> int:
    """Wait for ``count()`` to reach ``minimum``, then to stop changing for ``quiet`` seconds."""
    wait_until(lambda: count() >= minimum, timeout, f"at least {minimum} translated line(s)")
    last, since = count(), time.monotonic()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        time.sleep(0.05)
        now = count()
        if now != last:
            last, since = now, time.monotonic()
        elif time.monotonic() - since >= quiet:
            return now
    raise AssertionError("the pipeline never went quiet")


@pytest.fixture(scope="module")
def replay(tmp_path_factory):
    image_path = _needs_files()
    from gloss.adapters.ocr import rapidocr_engine  # registers itself in the default registry

    engine_cls = rapidocr_engine.RapidOCREngine
    config = fast_config(
        capture=("cap",),
        ocr=("rapidocr",),
        provider="fake",
        fps=10,
        timeout_ms=30000,
        translate_timeout_ms=8000,
    )
    # The probe tells "no models on this PC" apart from a real failure.
    probe = default_registry.create(
        "ocr", "rapidocr", AdapterContext(config.adapter_settings("ocr", "rapidocr"))
    )
    try:
        probe.open()
    except AdapterUnavailable as exc:
        pytest.skip(f"the RapidOCR models are not on this machine: {exc}")
    finally:
        probe.close()

    with Image.open(image_path) as picture:
        full = np.array(picture.convert("RGB"), dtype=np.uint8)
    height = full.shape[0] - SCROLL_PX
    scene_a = np.ascontiguousarray(full[:height])
    scene_b = np.ascontiguousarray(full[SCROLL_PX:])  # content moved up by SCROLL_PX

    registry = Registry()
    assert registry.register("ocr", "rapidocr", engine_cls)
    assert registry.register("translate", "fake", SpyTranslator)
    SpyTranslator.requests = []
    camera = CaptureScript("cap", scene_a).register(registry)
    sink = RecordingSink()

    user_dir = tmp_path_factory.mktemp("appdata")
    patch = pytest.MonkeyPatch()
    patch.setattr("gloss.core.config.user_config_path", lambda: user_dir / "config.toml")
    from gloss.app import make_translation_factory

    factory = make_translation_factory(config, registry, None)
    sent: list[tuple[str, str]] = []

    class Recording:
        """The real stage, plus a note of every line the pipeline hands to it."""

        def __init__(self) -> None:
            self._inner = factory()

        def open(self):
            self._inner.open()

        def translate(self, items, source_lang=None):
            sent.extend((i.line_id, i.text) for i in items)
            return self._inner.translate(items, source_lang=source_lang)

        def health(self):
            return self._inner.health()

        def retry_unavailable(self):
            self._inner.retry_unavailable()

        def close(self):
            self._inner.close()

    pipeline = Pipeline(
        config,
        registry=registry,
        sink=sink,
        make_translation=Recording,
        target=CaptureTarget("region", region=Rect(ORIGIN[0], ORIGIN[1], 1, 1)),
    )
    out = Replay(sink, (height, full.shape[1]))
    try:
        pipeline.start()
        # A: the chat as first seen. Unchanged frames follow while we wait.
        out.after_a = _settle(lambda: len(sent), minimum=3)
        # B: the same chat scrolled up, so new messages appear at the bottom.
        camera.image = scene_b
        before = out.after_a
        wait_until(lambda: len(sent) > before, 60, "the newly scrolled-in lines")
        out.after_b = _settle(lambda: len(sent))
        # Unchanged frames for a while: nothing may be read or sent again.
        out.ocr_calls_before_unchanged = camera.grabs
        time.sleep(2.0)
        out.after_unchanged = len(sent)
        out.ocr_calls_after_unchanged = camera.grabs
        out.health_at_end = pipeline.health()
        wait_until(lambda: sink.last_health() == Health("ok"), 10, "ok to reach the sink")
        out.sink_health_at_end = sink.last_health()
    finally:
        pipeline.stop(timeout_s=5.0)
        patch.undo()

    out.sent_lines = list(sent)
    out.provider_texts = [t for batch in SpyTranslator.requests for t in batch]

    # Restart: a new service on the same database must answer from the cache.
    patch = pytest.MonkeyPatch()
    patch.setattr("gloss.core.config.user_config_path", lambda: user_dir / "config.toml")
    try:
        calls_before = len(SpyTranslator.requests)
        stage = make_translation_factory(config, registry, None)()
        try:
            stage.open()
            items = [TranslationItem(f"r{n}", t) for n, (_, t) in enumerate(out.sent_lines)]
            out.restart_results = stage.translate(items)
        finally:
            stage.close()
        out.restart_provider_calls = len(SpyTranslator.requests) - calls_before
    finally:
        patch.undo()
    return out


# ---- assertions -----------------------------------------------------------------------------


def test_every_stable_line_is_sent_for_translation_once(replay):
    ids = [i for i, _ in replay.sent_lines]
    assert len(ids) == len(set(ids)), "a line was sent twice (re-translated after scroll?)"
    assert len(ids) >= 5


def test_nothing_is_sent_again_after_the_scroll_settles_or_on_unchanged_frames(replay):
    assert replay.after_unchanged == replay.after_b
    assert replay.after_b > replay.after_a  # the scroll did reveal new lines


def test_the_scroll_sent_only_new_lines_not_the_ones_already_translated(replay):
    first = Counter(t for _, t in replay.sent_lines[: replay.after_a])
    later = [t for _, t in replay.sent_lines[replay.after_a :]]
    # A scrolled line keeps its identity. Allow a rare second read of a text (a repeated chat
    # message), but most of what the scroll sent must be new text.
    repeats = [t for t in later if first[t] and len(t) >= 4]
    assert len(repeats) <= max(1, len(later) // 4), f"re-sent after scroll: {repeats}"


def test_the_provider_is_asked_for_each_distinct_text_at_most_once(replay):
    counts = Counter(replay.provider_texts)
    assert counts, "the fake translator was never called"
    assert max(counts.values()) == 1, {t: n for t, n in counts.items() if n > 1}


def test_english_and_number_only_lines_are_not_sent(replay):
    odd = [t for _, t in replay.sent_lines if not CJK.search(t)]
    # Chinese chat: everything worth sending has CJK text. Latin lines may sneak in only when
    # they are real sentences, never bare numbers, times or ids.
    bare = [t for t in odd if not re.search(r"[A-Za-z]{3,}", t)]
    assert not bare, f"numbers/times were sent: {bare}"
    assert len(odd) <= max(1, len(replay.sent_lines) // 5), f"English lines sent: {odd}"


def test_render_items_carry_desktop_coordinates(replay):
    first: dict[str, Rect] = {}
    for item in replay.sink.snapshot():
        if item.result.status == "ok" and item.bbox is not None:
            first.setdefault(item.result.line_id, item.bbox)  # where each line first appeared
    assert first
    height, width = replay.image_shape
    ox, oy = ORIGIN
    for line_id, b in first.items():
        assert b.x >= ox - 2 and b.y >= oy - 2, f"{line_id}: {b} lacks the region offset"
        assert b.right <= ox + width + 2 and b.bottom <= oy + height + 2, f"{line_id}: {b}"
    assert replay.sink.metas[-1].region == Rect(ox, oy, width, height)


def test_a_line_that_scrolled_out_of_the_captured_area_is_removed_not_left_on_screen(replay):
    """The first chat line scrolls off the top. The tracker moves it to negative frame
    coordinates (Rect y = -114 here) and never scans that area again, so it never counts a
    miss: the pipeline keeps it 'visible', re-sends it with a bbox above the captured area, and
    never calls remove_lines for it. On a long chat this also grows the tracker without bound."""
    height, width = replay.image_shape
    ox, oy = ORIGIN
    region = Rect(ox, oy, width, height)
    last: dict[str, Rect] = {}
    for item in replay.sink.snapshot():
        if item.result.status == "ok" and item.bbox is not None:
            last[item.result.line_id] = item.bbox
    outside = {
        line_id: b
        for line_id, b in last.items()
        if b.bottom <= region.y or b.y >= region.bottom or b.right <= region.x
    }
    stale = {line_id: b for line_id, b in outside.items() if line_id not in replay.sink.removed}
    assert not stale, f"lines left outside the captured area {region} and not removed: {stale}"


def test_scrolled_lines_move_up_on_screen_and_keep_their_translation(replay):
    by_id: dict[str, list[Rect]] = {}
    for item in replay.sink.snapshot():
        if item.result.status == "ok" and item.bbox is not None:
            by_id.setdefault(item.result.line_id, []).append(item.bbox)
    moved = [boxes for boxes in by_id.values() if len(boxes) > 1]
    assert moved, "no line was re-positioned after the scroll"
    shifts = [boxes[0].y - boxes[-1].y for boxes in moved]
    assert any(abs(s - replay.scroll_px) <= 12 for s in shifts), f"unexpected shifts {shifts}"


def test_translations_reach_the_sink_for_what_was_sent(replay):
    shown = Counter(i.result.source_text for i in replay.sink.shown("ok"))
    sent = Counter(t for _, t in replay.sent_lines)
    assert not replay.sink.shown("failed")
    assert set(sent) <= set(shown)
    assert all(i.result.text == f"EN<{i.result.source_text}>" for i in replay.sink.shown("ok"))


def test_no_chat_line_is_missed(replay):
    expected = (EXPECTED / f"{CHAT}.txt").read_text(encoding="utf-8").splitlines()
    wanted = [line for line in expected if CJK.search(line) and len(line) >= 6]
    sent = [t for _, t in replay.sent_lines]
    found = [w for w in wanted if any(fuzz.partial_ratio(w, s) >= 80 for s in sent)]
    recall = len(found) / len(wanted)
    missed = [w for w in wanted if w not in found]
    assert recall >= 0.8, f"recall {recall:.0%}; missed {missed}"


def test_lines_are_shown_in_reading_order_within_a_batch(replay):
    # The OCR output order is top to bottom; items of one batch share that order.
    ys = [i.bbox.y for i in replay.sink.shown("ok")][: max(2, replay.after_a)]
    firsts = ys[: min(len(ys), 6)]
    assert firsts == sorted(firsts)


def test_after_a_restart_the_translation_cache_answers_without_the_provider(replay):
    assert replay.restart_results
    assert replay.restart_provider_calls == 0
    assert all(r.cached for r in replay.restart_results if r.status == "ok")
    assert all(r.status == "ok" for r in replay.restart_results)
    assert all(r.text == f"EN<{r.source_text}>" for r in replay.restart_results)


def test_overall_health_is_ok_at_the_end(replay):
    assert replay.health_at_end == Health("ok")
    assert replay.sink_health_at_end == Health("ok")
