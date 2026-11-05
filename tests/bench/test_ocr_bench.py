"""OCR latency per region class at the default "fast" profile (SPEC section 7).

Synthetic chat pictures only. The p95 gates are the SPEC's "4-8 core" column, which applies to
any machine; the stricter "this PC" column is guarded by the baseline comparison instead
(a 20% rule on the recorded p95 values).
"""

from __future__ import annotations

import time

import numpy as np
import pytest
from testkit.bench import chat_canvas
from testkit.pipeline import base_config

from gloss.adapters.ocr.rapidocr_engine import RapidOCREngine
from gloss.core.contracts import AdapterContext, Frame, Rect

pytestmark = pytest.mark.bench

WIDTH = 720
GATES_MS = {"band": 500, "panel": 1000, "page": 4000}  # SPEC section 7, 4-8 core column


@pytest.fixture(scope="module")
def engine():
    config = base_config()
    assert config.ocr.profile == "fast", "the bench measures the default profile"
    ocr = RapidOCREngine(AdapterContext(config.adapter_settings("ocr", "rapidocr")))
    ocr.open()
    try:
        yield ocr
    finally:
        ocr.close()


def _runs(engine, frame: Frame, regions, count: int) -> list[float]:
    engine.recognize(frame, regions)  # warm-up (first run allocates)
    times = []
    for _ in range(count):
        start = time.perf_counter()
        blocks = engine.recognize(frame, regions)
        times.append((time.perf_counter() - start) * 1000)
        assert blocks, "the synthetic picture produced no text, so the timing means nothing"
    return times


def _record(metrics, label: str, times: list[float]) -> float:
    metrics.record(f"ocr_{label}_p50_ms", float(np.percentile(times, 50)), "ms")
    return metrics.record(f"ocr_{label}_p95_ms", float(np.percentile(times, 95)), "ms")


def _frame(canvas, height: int) -> Frame:
    return Frame(canvas.view(0, height), "bench", Rect(0, 0, WIDTH, height), time.monotonic(), 1.0)


def test_ocr_new_lines_band(engine, metrics) -> None:
    """1-5 lines: the strip a scroll exposes. Read as a region of a full-size frame."""
    canvas = chat_canvas(lines=14, width=WIDTH)
    frame = _frame(canvas, 14 * canvas.line_h)
    band = Rect(0, 8 * canvas.line_h, WIDTH, 2 * canvas.line_h)
    p95 = _record(metrics, "band", _runs(engine, frame, [band], 20))
    assert p95 <= GATES_MS["band"], f"band p95 {p95:.0f} ms (gate {GATES_MS['band']} ms)"


def test_ocr_chat_panel(engine, metrics) -> None:
    """6-25 lines: a whole chat panel read in one go."""
    canvas = chat_canvas(lines=14, width=WIDTH)
    frame = _frame(canvas, 14 * canvas.line_h)
    p95 = _record(metrics, "panel", _runs(engine, frame, None, 12))
    assert p95 <= GATES_MS["panel"], f"panel p95 {p95:.0f} ms (gate {GATES_MS['panel']} ms)"


def test_ocr_dense_page(engine, metrics) -> None:
    """26-150 lines: a whole dense page (not live)."""
    canvas = chat_canvas(lines=48, width=WIDTH, line_h=32, font_px=18)
    frame = _frame(canvas, 48 * canvas.line_h)
    p95 = _record(metrics, "page", _runs(engine, frame, None, 6))
    assert p95 <= GATES_MS["page"], f"page p95 {p95:.0f} ms (gate {GATES_MS['page']} ms)"
