"""Pipeline benchmarks: CPU, RAM, scroll-to-boxes, scroll-to-new-line and frame-to-line time.

The real Pipeline, the real ChangeDetector, the real RapidOCR (fast profile), the real text
tracker and the real TranslationService with its SQLite cache (in a temporary folder). Fake:
the capture source (serves crops of a synthetic chat) and the translator (answers at once,
counts its calls). No network, no key, no screen of the user's.

The scenario runs once for the module and the tests read its numbers. Phases, in order:
  1. first read of a chat panel
  2. an unchanged screen        -> idle CPU, and the number of translator calls (must be 0)
  3. a lively scrolling chat    -> active CPU
  4. one-line scrolls, spaced   -> time until boxes move; time until the new line is shown
  5. lines appearing in place   -> time from the frame to the shown line (no scroll)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
import psutil
import pytest
from rapidfuzz import fuzz
from testkit.bench import Canvas, chat_canvas
from testkit.pipeline import CaptureScript, RecordingSink, fast_config

from gloss.adapters.ocr import rapidocr_engine
from gloss.core.contracts import (
    CaptureTarget,
    Health,
    Rect,
    TranslationResponse,
    TranslationResult,
)
from gloss.core.pipeline import Pipeline
from gloss.core.registry import Registry

pytestmark = pytest.mark.bench

W, VIEW_LINES = 720, 12
LINES = 70
IDLE_S, BUSY_S, BUSY_STEP_S = 6.0, 8.0, 1.0
SCROLL_STEPS, APPEND_STEPS = 10, 8
GIVE_UP_S = 8.0  # a line not shown after this long counts as missed
MATCH = 60  # fuzz ratio (0-100) for "this OCR text is that line"


class FakeTranslator:
    """Answers immediately with ``EN<text>`` and counts what it was asked."""

    name = "fake"
    REQUIRES_CONTRACT = ">=1.0,<2.0"
    calls = 0

    def __init__(self, context) -> None:
        pass

    def open(self) -> None:
        pass

    def translate(self, request):
        type(self).calls += 1
        return TranslationResponse(
            tuple(
                TranslationResult(i.line_id, i.text, f"EN<{i.text}>", "fake", "m", False, 1)
                for i in request.items
            )
        )

    def close(self) -> None:
        pass

    def health(self) -> Health:
        return Health("ok")


class TimedSink(RecordingSink):
    """Remembers when each show_results call happened and which texts it carried."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[float, list[str]]] = []

    def show_results(self, items, frame_meta) -> None:
        super().show_results(items, frame_meta)
        self.events.append((time.monotonic(), [i.result.source_text for i in items]))


def _cpu_seconds() -> float:
    t = psutil.Process().cpu_times()
    return t.user + t.system


@dataclass
class Scenario:
    values: dict[str, float] = field(default_factory=dict)
    missed: list[str] = field(default_factory=list)
    translator_calls_while_idle: int = -1
    order_ok: bool = True


def _quiet(sink: TimedSink, seconds: float = 1.2, timeout: float = 60.0) -> None:
    """Wait until nothing has been shown for ``seconds``."""
    last, since, end = len(sink.events), time.monotonic(), time.monotonic() + timeout
    while time.monotonic() < end:
        time.sleep(0.05)
        if len(sink.events) != last:
            last, since = len(sink.events), time.monotonic()
        elif time.monotonic() - since >= seconds:
            return
    raise AssertionError("the pipeline never went quiet")


def _first_event_after(sink: TimedSink, t0: float, wanted: str | None) -> float | None:
    """Time of the first show_results call after ``t0`` (carrying ``wanted``, if given)."""
    for when, texts in list(sink.events):
        if when < t0:
            continue
        if wanted is None or any(fuzz.ratio(t, wanted) >= MATCH for t in texts):
            return when
    return None


def _wait_for(sink: TimedSink, t0: float, wanted: str) -> float | None:
    end = t0 + GIVE_UP_S
    while time.monotonic() < end:
        found = _first_event_after(sink, t0, wanted)
        if found is not None:
            return (found - t0) * 1000
        time.sleep(0.01)
    return None


def _percentiles(values: list[float]) -> tuple[float, float]:
    return float(np.percentile(values, 50)), float(np.percentile(values, 95))


def _run(canvas: Canvas, tmp_path) -> Scenario:
    config = fast_config(
        capture=("cap",),
        ocr=("rapidocr",),
        provider="fake",
        fps=12,  # the shipped defaults: fps_max 12, idle_fps 4, idle after 1 s
        idle_fps=4,
        idle_after_s=1.0,
        timeout_ms=30000,
        translate_timeout_ms=8000,
    )
    registry = Registry()
    registry.register("ocr", "rapidocr", rapidocr_engine.RapidOCREngine)
    registry.register("translate", "fake", FakeTranslator)
    view_h = VIEW_LINES * canvas.line_h
    cam = CaptureScript("cap", canvas.view(0, view_h)).register(registry)
    sink = TimedSink()

    import gloss.core.config as config_module
    from gloss.app import make_translation_factory

    mp = pytest.MonkeyPatch()
    mp.setattr(config_module, "user_config_path", lambda: tmp_path / "config.toml")
    FakeTranslator.calls = 0
    pipeline = Pipeline(
        config,
        registry=registry,
        sink=sink,
        make_translation=make_translation_factory(config, registry, None),
        target=CaptureTarget("region", region=Rect(0, 0, W, view_h)),
    )
    s = Scenario()
    top = 0

    def scroll_to(line: int) -> None:
        cam.image = canvas.view(line * canvas.line_h, view_h)

    try:
        pipeline.start()
        # 1. the first read
        start = time.monotonic()
        _quiet(sink)
        s.values["first_read_s"] = time.monotonic() - start
        assert sink.shown(), "nothing was read from the synthetic chat"

        # 2. idle: the screen does not change
        time.sleep(2.0)  # let the capture loop drop to idle_fps
        calls, cpu0, t0 = FakeTranslator.calls, _cpu_seconds(), time.monotonic()
        time.sleep(IDLE_S)
        s.values["cpu_idle_pct"] = (_cpu_seconds() - cpu0) / (time.monotonic() - t0) * 100
        s.translator_calls_while_idle = FakeTranslator.calls - calls

        # 3. busy: a busy chat gets a new line every BUSY_STEP_S
        cpu0, t0 = _cpu_seconds(), time.monotonic()
        steps = int(BUSY_S / BUSY_STEP_S)
        for _ in range(steps):
            top += 1
            scroll_to(top)
            time.sleep(BUSY_STEP_S)
        s.values["cpu_busy_pct"] = (_cpu_seconds() - cpu0) / (time.monotonic() - t0) * 100
        _quiet(sink)

        # 4. one-line scrolls, each given time to settle
        box_ms, line_ms = [], []
        for _ in range(SCROLL_STEPS):
            top += 1
            wanted = canvas.texts[top + VIEW_LINES - 1]  # the line the scroll exposes
            t0 = time.monotonic()
            scroll_to(top)
            moved = _wait_for(sink, t0, None)  # boxes move: the first update after the scroll
            shown = _wait_for(sink, t0, wanted)
            if moved is not None:
                box_ms.append(moved)
            if shown is None:
                s.missed.append(f"scroll step {top}: {wanted}")
            else:
                line_ms.append(shown)
            _quiet(sink)
        s.values["scroll_boxes_p50_ms"], s.values["scroll_boxes_p95_ms"] = _percentiles(box_ms)
        s.values["scroll_line_p50_ms"], s.values["scroll_line_p95_ms"] = _percentiles(
            line_ms or [GIVE_UP_S * 1000]
        )

        # 5. lines appear in place (a half-empty chat filling up), no scroll
        top = top + VIEW_LINES + 2  # lines never seen before (seen lines are de-duplicated)
        assert top + VIEW_LINES + 1 < LINES
        first = 4
        bg = np.array(canvas.background, dtype=np.uint8)
        base = canvas.view(top * canvas.line_h, view_h)
        shown_lines = first
        partial = base.copy()
        partial[shown_lines * canvas.line_h :] = bg
        cam.image = partial
        _quiet(sink)
        e2e = []
        for _ in range(APPEND_STEPS):
            wanted = canvas.texts[top + shown_lines]
            shown_lines += 1
            nxt = base.copy()
            nxt[shown_lines * canvas.line_h :] = bg
            t0 = time.monotonic()
            cam.image = nxt
            took = _wait_for(sink, t0, wanted)
            if took is None:
                s.missed.append(f"in-place line {shown_lines}: {wanted}")
            else:
                e2e.append(took)
            _quiet(sink)
        s.values["e2e_line_p50_ms"], s.values["e2e_line_p95_ms"] = _percentiles(
            e2e or [GIVE_UP_S * 1000]
        )

        info = psutil.Process().memory_info()
        s.values["ram_peak_mb"] = getattr(info, "peak_wset", info.rss) / 2**20
    finally:
        pipeline.stop(timeout_s=5.0)
        mp.undo()
    return s


@pytest.fixture(scope="module")
def scenario(tmp_path_factory) -> Scenario:
    canvas = chat_canvas(lines=LINES, width=W)
    return _run(canvas, tmp_path_factory.mktemp("pipeline_bench"))


def test_idle_cpu_and_no_api_calls_on_an_unchanged_screen(scenario, metrics) -> None:
    cpu = metrics.record("cpu_idle_pct", scenario.values["cpu_idle_pct"], "pct")
    assert scenario.translator_calls_while_idle == 0, "an unchanged screen caused translator calls"
    assert cpu < 3.0, f"idle CPU {cpu:.1f}% of one core (SPEC: < 3%)"


def test_active_cpu_while_a_chat_scrolls(scenario, metrics) -> None:
    cpu = metrics.record("cpu_busy_pct", scenario.values["cpu_busy_pct"], "pct")
    assert cpu < 25.0, f"active CPU {cpu:.1f}% of one core (SPEC: < 25%)"


def test_ram(scenario, metrics) -> None:
    ram = metrics.record("ram_peak_mb", scenario.values["ram_peak_mb"], "mb")
    assert ram < 400, f"peak RAM {ram:.0f} MB (SPEC: < 400 MB)"


def test_scroll_to_boxes_moving_and_to_new_lines(scenario, metrics) -> None:
    """Fast path: boxes follow the scroll. Then the newly exposed line is read and shown."""
    p50b = metrics.record("scroll_boxes_p50_ms", scenario.values["scroll_boxes_p50_ms"], "ms")
    metrics.record("scroll_boxes_p95_ms", scenario.values["scroll_boxes_p95_ms"], "ms")
    p50 = metrics.record("scroll_line_p50_ms", scenario.values["scroll_line_p50_ms"], "ms")
    p95 = metrics.record("scroll_line_p95_ms", scenario.values["scroll_line_p95_ms"], "ms")
    assert p50b < p50, "boxes should move before the new line appears"
    assert not [m for m in scenario.missed if m.startswith("scroll")], scenario.missed
    assert p50 <= 1500 and p95 <= 3000, f"new line after a scroll p50 {p50:.0f} / p95 {p95:.0f} ms"


def test_end_to_end_frame_to_shown_line(scenario, metrics) -> None:
    p50 = metrics.record("e2e_line_p50_ms", scenario.values["e2e_line_p50_ms"], "ms")
    p95 = metrics.record("e2e_line_p95_ms", scenario.values["e2e_line_p95_ms"], "ms")
    assert not [m for m in scenario.missed if m.startswith("in-place")], scenario.missed
    assert p50 <= 1500 and p95 <= 3000, f"frame to shown line p50 {p50:.0f} / p95 {p95:.0f} ms"
