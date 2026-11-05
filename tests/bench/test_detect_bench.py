"""Gate: ChangeDetector.detect stays inside the frame budget.

Two kinds of picture, because they stress different paths:
- random noise that scrolls (worst case for scroll matching; the original gate), and
- a chat-like picture (flat background, bubbles, text) that scrolls one line at a time, or
  repeats unchanged. This is what a real chat looks like to the detector.
"""

from __future__ import annotations

import time

import numpy as np
import pytest
from testkit.bench import chat_canvas, noise_image

from gloss.core.contracts import Frame, Rect
from gloss.services.change_detector import ChangeDetector

pytestmark = pytest.mark.bench

HEIGHT, WIDTH, STEP, FRAMES = 1300, 1000, 24, 20
BUDGET_MS = 40.0  # target is ~25 ms; headroom for slower CI machines. HEAD-before-fix was 96 ms.


def _time_frames(frames: list[np.ndarray], region: Rect) -> tuple[list[float], int]:
    detector = ChangeDetector()
    detector.detect(Frame(frames[0], "bench", region, 0.0, 1.0, True))
    times, scrolls = [], 0
    for i, image in enumerate(frames[1:], start=1):
        frame = Frame(image, "bench", region, i / 12, 1.0, True)
        start = time.perf_counter()
        result = detector.detect(frame)
        times.append((time.perf_counter() - start) * 1000)
        scrolls += bool(result.scrolls)
    return times, scrolls


def test_detect_scrolling_region_within_budget(metrics) -> None:
    tall = noise_image(HEIGHT + STEP * (FRAMES + 1), WIDTH)
    region = Rect(0, 0, WIDTH, HEIGHT)
    frames = [np.ascontiguousarray(tall[i * STEP : i * STEP + HEIGHT]) for i in range(FRAMES + 1)]
    times, found = _time_frames(frames, region)
    assert found >= FRAMES // 2, "the scroll was not recognised"
    median = metrics.record("detect_noise_scroll_ms", float(np.median(times)), "ms")
    assert median < BUDGET_MS, f"detect took {median:.1f} ms per frame (budget {BUDGET_MS} ms)"


def test_detect_chat_scroll_within_budget(metrics) -> None:
    canvas = chat_canvas(lines=60, width=WIDTH)
    view_h, step = 720, canvas.line_h
    region = Rect(0, 0, WIDTH, view_h)
    frames = [canvas.view(i * step, view_h) for i in range(FRAMES + 1)]
    times, found = _time_frames(frames, region)
    assert found >= FRAMES // 2, "the chat scroll was not recognised"
    median = metrics.record("detect_chat_scroll_ms", float(np.median(times)), "ms")
    assert median < BUDGET_MS, f"detect took {median:.1f} ms per frame (budget {BUDGET_MS} ms)"


def test_detect_unchanged_chat_is_cheap(metrics) -> None:
    """A static screen is the common case: it must cost far less than a scroll."""
    canvas = chat_canvas(lines=20, width=WIDTH)
    region = Rect(0, 0, WIDTH, 720)
    base = canvas.view(0, 720)
    frames = [base.copy() for _ in range(FRAMES + 1)]
    times, _ = _time_frames(frames, region)
    median = metrics.record("detect_chat_static_ms", float(np.median(times)), "ms")
    assert median < BUDGET_MS / 2, f"an unchanged frame took {median:.1f} ms"
