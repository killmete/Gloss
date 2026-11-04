"""Measurement (not a gate): box jitter, scroll latency and OCR time on a golden chat image.

Run: uv run python tests/bench/pipeline_motion_bench.py
Needs the local golden images and the RapidOCR models. Real ChangeDetector, RapidOCR (fast),
TextTracker and Pipeline; a fake translator; a fake capture that serves crops of the image.
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from testkit.golden import IMAGES  # noqa: E402
from testkit.pipeline import CaptureScript, RecordingSink, fast_config  # noqa: E402

from gloss.adapters.ocr import rapidocr_engine  # noqa: E402
from gloss.core.contracts import (  # noqa: E402
    AdapterContext,
    CaptureTarget,
    Frame,
    Health,
    Rect,
    TranslationResponse,
    TranslationResult,
)
from gloss.core.pipeline import Pipeline  # noqa: E402
from gloss.core.registry import Registry  # noqa: E402

W, H, STEP = 1071, 724, 60
SOURCES = ("Screenshot 2026-10-30 220641", "Screenshot 2026-10-30 220714")


class Spy:
    name = "fake"
    REQUIRES_CONTRACT = ">=1.0,<2.0"
    calls = 0

    def __init__(self, context):
        pass

    def open(self):
        pass

    def translate(self, request):
        type(self).calls += 1
        return TranslationResponse(
            tuple(
                TranslationResult(i.line_id, i.text, f"EN<{i.text}>", "fake", "m", False, 1)
                for i in request.items
            )
        )

    def close(self):
        pass

    def health(self):
        return Health("ok")


class TimedSink(RecordingSink):
    def __init__(self):
        super().__init__()
        self.events: list[tuple[float, list]] = []

    def show_results(self, items, frame_meta):
        super().show_results(items, frame_meta)
        self.events.append((time.monotonic(), list(items)))


def build_canvas() -> np.ndarray:
    parts = []
    for stem in SOURCES:
        with Image.open(IMAGES / f"{stem}.png") as pic:
            arr = np.array(pic.convert("RGB"), dtype=np.uint8)
        parts.append(arr[:, arr.shape[1] - W :])  # the right-hand W columns
    return np.ascontiguousarray(np.vstack(parts))


def quiet(sink, seconds=1.5, timeout=90.0):
    last, since, end = len(sink.events), time.monotonic(), time.monotonic() + timeout
    while time.monotonic() < end:
        time.sleep(0.05)
        n = len(sink.events) + Spy.calls * 1000
        if n != last:
            last, since = n, time.monotonic()
        elif time.monotonic() - since >= seconds:
            return
    raise SystemExit("never went quiet")


def main() -> None:
    canvas = build_canvas()
    config = fast_config(
        capture=("cap",),
        ocr=("rapidocr",),
        provider="fake",
        fps=12,
        idle_fps=4,
        timeout_ms=30000,
        translate_timeout_ms=8000,
    )
    registry = Registry()
    registry.register("ocr", "rapidocr", rapidocr_engine.RapidOCREngine)
    registry.register("translate", "fake", Spy)
    top = np.ascontiguousarray(canvas[:H])
    cam = CaptureScript("cap", top).register(registry)
    sink = TimedSink()

    from gloss.app import make_translation_factory

    tmp = Path(tempfile.mkdtemp())
    import gloss.core.config as cfgmod

    cfgmod.user_config_path = lambda: tmp / "config.toml"
    pipeline = Pipeline(
        config,
        registry=registry,
        sink=sink,
        make_translation=make_translation_factory(config, registry, None),
        target=CaptureTarget("region", region=Rect(0, 0, W, H)),
    )
    pipeline.start()
    try:
        t0 = time.monotonic()
        quiet(sink)
        print(f"initial full read + translation settled in {time.monotonic() - t0:.1f} s")
        first_lines = {i.result.line_id for _, items in sink.events for i in items}
        print(f"lines shown: {len(first_lines)}")

        # (1) static screen, tiny noise in an avatar-sized patch
        rng = np.random.default_rng(0)
        boxes = {i.result.line_id: i.bbox for _, items in sink.events for i in items}
        px, py = next(
            (x, y)
            for y in range(0, H - 48, 16)
            for x in range(0, W - 48, 16)
            if all(
                x + 48 + 20 < b.x or x - 20 > b.right or y + 48 + 20 < b.y or y - 20 > b.bottom
                for b in boxes.values()
            )
        )
        print(f"    noise patch at ({px}, {py}), clear of every text box")

        def noise_phase(label: str, x0: int, y0: int) -> None:
            seen = {i.result.line_id for _, items in sink.events for i in items}
            before = len(sink.events)
            end = time.monotonic() + 5.0
            while time.monotonic() < end:
                noisy = top.copy()
                patch = noisy[y0 : y0 + 48, x0 : x0 + 48].astype(np.int16)
                patch += rng.integers(-3, 4, patch.shape)
                noisy[y0 : y0 + 48, x0 : x0 + 48] = np.clip(patch, 0, 255).astype(np.uint8)
                cam.image = noisy
                time.sleep(0.1)
            cam.image = top
            time.sleep(1.0)
            items = [i for _, batch in sink.events[before:] for i in batch]
            new = {i.result.line_id for i in items} - seen
            print(
                f"({label}) static 5 s with noise at ({x0}, {y0}): "
                f"show_results events {len(sink.events) - before}, box moves {len(items)}, "
                f"spurious new lines {len(new)}"
            )
            quiet(sink)

        noise_phase("1a clear of text", px, py)
        noise_phase("1b over a 56-px text line at y=86", 20, 100)

        # (2) scroll 60 px every 100 ms for 1 s
        seen_before = {i.result.line_id for _, items in sink.events for i in items}
        stamps = []
        for k in range(1, 11):
            cam.image = np.ascontiguousarray(canvas[k * STEP : k * STEP + H])
            stamps.append(time.monotonic())
            time.sleep(0.1)
        scroll_end = stamps[-1]
        lat = []
        for ts in stamps:
            nxt = next((t for t, _ in sink.events if t >= ts), None)
            if nxt is not None:
                lat.append((nxt - ts) * 1000)
        print(
            f"(2a) scroll frame -> boxes move: median {statistics.median(lat):.0f} ms, "
            f"max {max(lat):.0f} ms over {len(lat)} of {len(stamps)} steps"
        )
        quiet(sink, seconds=2.0)
        new_events = [
            t
            for t, items in sink.events
            if t >= scroll_end and any(i.result.line_id not in seen_before for i in items)
        ]
        if new_events:
            print(
                "(2b) scroll end -> first new line translated "
                f"{(new_events[0] - scroll_end) * 1000:.0f} ms, "
                f"last {(new_events[-1] - scroll_end) * 1000:.0f} ms"
            )
        else:
            print("(2b) no new lines were translated after the scroll")
    finally:
        pipeline.stop(timeout_s=5.0)

    # (3) OCR time
    settings = config.adapter_settings("ocr", "rapidocr")
    engine = rapidocr_engine.RapidOCREngine(AdapterContext(settings))
    engine.open()
    frame = Frame(top, "bench", Rect(0, 0, W, H), time.monotonic(), 1.0)
    engine.recognize(frame, None)  # warm-up

    def timed(regions):
        runs = []
        for _ in range(3):
            s = time.perf_counter()
            engine.recognize(frame, regions)
            runs.append((time.perf_counter() - s) * 1000)
        return statistics.median(runs)

    print(
        f"(3) OCR full {W}x{H}: {timed(None):.0f} ms; "
        f"exposed band {W}x{STEP}: {timed([Rect(0, H - STEP, W, STEP)]):.0f} ms"
    )
    engine.close()


if __name__ == "__main__":
    main()
