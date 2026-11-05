"""Gate: ChangeDetector.detect stays inside the frame budget on a scrolling chat region."""

from __future__ import annotations

import time

import numpy as np
import pytest

from gloss.core.contracts import Frame, Rect
from gloss.services.change_detector import ChangeDetector

pytestmark = pytest.mark.bench

HEIGHT, WIDTH, STEP, FRAMES = 1300, 1000, 24, 20
BUDGET_MS = 40.0  # target is ~25 ms; headroom for slower CI machines. HEAD-before-fix was 96 ms.


def test_detect_scrolling_region_within_budget() -> None:
    rng = np.random.default_rng(0)
    tall = rng.integers(0, 256, (HEIGHT + STEP * (FRAMES + 1), WIDTH, 3), dtype=np.uint8)
    region = Rect(0, 0, WIDTH, HEIGHT)

    def frame(i: int) -> Frame:
        image = np.ascontiguousarray(tall[i * STEP : i * STEP + HEIGHT])
        return Frame(image, "bench", region, i / 12, 1.0, True)

    detector = ChangeDetector()
    detector.detect(frame(0))
    times, found = [], 0
    for i in range(1, FRAMES + 1):
        start = time.perf_counter()
        result = detector.detect(frame(i))
        times.append((time.perf_counter() - start) * 1000)
        found += bool(result.scrolls)
    assert found >= FRAMES // 2, "the scroll was not recognised"
    median = float(np.median(times))
    assert median < BUDGET_MS, f"detect took {median:.1f} ms per frame (budget {BUDGET_MS} ms)"
