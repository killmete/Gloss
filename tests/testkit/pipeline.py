"""Fakes and helpers for pipeline tests: no screen, no Qt, no network.

``Rig`` wires a real ``Pipeline`` to scripted capture and OCR adapters in a private
``Registry`` (the default registry is never touched), a scripted translation stage and a
recording sink. Scripts are plain objects the test mutates while the pipeline runs.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

import numpy as np

from gloss.core.config import GlossConfig, load_config
from gloss.core.contracts import (
    CaptureTarget,
    Frame,
    FrameMeta,
    Health,
    Rect,
    RenderItem,
    TextBlock,
    TranslationItem,
    TranslationResult,
)
from gloss.core.pipeline import Pipeline
from gloss.core.registry import Registry
from testkit.discovery import NO_USER_CONFIG

REQUIRES = ">=1.0,<2.0"
BLOCK_S = 30.0  # a scripted hang lasts this long unless the test releases it earlier


def wait_until(
    predicate: Callable[[], Any], timeout: float = 8.0, what: str = "the condition"
) -> Any:
    """Poll until ``predicate`` is truthy. Returns its value; fails with ``what`` on timeout."""
    end = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > end:
            raise AssertionError(f"timed out after {timeout:.0f} s waiting for {what}")
        time.sleep(0.01)


def stays(predicate: Callable[[], Any], seconds: float = 0.3) -> bool:
    """True if ``predicate`` is truthy the whole time."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not predicate():
            return False
        time.sleep(0.01)
    return True


# ---- config ---------------------------------------------------------------------------------


def base_config() -> GlossConfig:
    return load_config(user_path=NO_USER_CONFIG).config


def fast_config(
    *,
    capture: Sequence[str] = ("cap",),
    ocr: Sequence[str] = ("ocr",),
    provider: str = "fake",
    translate_fallbacks: Sequence[str] = (),
    fps: float = 60.0,
    timeout_ms: int = 400,
    translate_timeout_ms: int | None = None,
    queue_size: int = 2,
    batch_max_lines: int = 20,
    breaker_failures: int = 3,
    black_reads: int = 3,
    cloud: bool = True,
) -> GlossConfig:
    """The default config with a fast frame rate, short timeouts and the fakes' names."""
    c = base_config()
    sources = c.capture.sources.model_copy(
        update={
            "region": c.capture.sources.region.model_copy(
                update={"source": capture[0], "fallbacks": list(capture[1:])}
            )
        }
    )
    return c.model_copy(
        update={
            "capture": c.capture.model_copy(
                update={
                    "mode": "region",
                    "fps_max": fps,
                    "sources": sources,
                    "black_frame_reads": black_reads,
                }
            ),
            "ocr": c.ocr.model_copy(
                update={"engine": ocr[0] if ocr else "none_registered", "fallbacks": list(ocr[1:])}
            ),
            "translate": c.translate.model_copy(
                update={
                    "provider": provider,
                    "fallbacks": list(translate_fallbacks),
                    "batch_max_lines": batch_max_lines,
                    "batch_max_wait_ms": 20,
                }
            ),
            "reliability": c.reliability.model_copy(
                update={
                    "capture_timeout_ms": timeout_ms,
                    "ocr_timeout_ms": timeout_ms,
                    "translate_timeout_ms": translate_timeout_ms or timeout_ms,
                    "retry_max": 0,
                    "queue_size": queue_size,
                    "breaker_failures": breaker_failures,
                }
            ),
            "privacy": c.privacy.model_copy(update={"cloud_translation": cloud}),
        }
    )


# ---- scripted capture -----------------------------------------------------------------------


def solid(w: int = 200, h: int = 120, value: int = 255) -> np.ndarray:
    return np.full((h, w, 3), value, dtype=np.uint8)


def with_patch(image: np.ndarray, x: int = 10, y: int = 10, size: int = 30) -> np.ndarray:
    """A copy of ``image`` with a dark square: a small, local change."""
    out = image.copy()
    out[y : y + size, x : x + size] = 0
    return out


class CaptureScript:
    """Controls one fake capture adapter. ``image`` is what the next grab returns."""

    def __init__(self, name: str, image: np.ndarray | None = None) -> None:
        self.name = name
        self.image = image
        self.grabs = 0
        self.opens = 0
        self.closes = 0
        self.targets: list[CaptureTarget] = []
        self.open_error: Exception | None = None
        self.grab_error: Exception | None = None
        self.bad_frame_once = False  # the next grab returns a non-Frame, to crash the worker
        self.hold = threading.Event()  # set = a grab blocks until released
        self.hold.set()

    def hang(self) -> None:
        self.hold.clear()

    def release(self) -> None:
        self.hold.set()

    def register(self, registry: Registry) -> CaptureScript:
        script = self

        class FakeCapture:
            name = script.name
            REQUIRES_CONTRACT = REQUIRES

            def __init__(self, context: Any) -> None:
                self._origin = (0, 0)

            def list_monitors(self) -> Sequence[Any]:
                return ()

            def open(self, target: CaptureTarget) -> None:
                script.opens += 1
                script.targets.append(target)
                if script.open_error is not None:
                    raise script.open_error
                if target.region is not None:
                    self._origin = (target.region.x, target.region.y)

            def grab(self) -> Frame | None:
                script.grabs += 1
                script.hold.wait(BLOCK_S)
                if script.bad_frame_once:
                    script.bad_frame_once = False
                    return object()  # type: ignore[return-value]
                if script.grab_error is not None:
                    raise script.grab_error
                image = script.image
                if image is None:
                    return None
                h, w = image.shape[:2]
                region = Rect(self._origin[0], self._origin[1], w, h)
                return Frame(image, script.name, region, time.monotonic(), 1.0)

            def close(self) -> None:
                script.closes += 1

            def health(self) -> Health:
                return Health("ok")

        assert registry.register("capture", self.name, FakeCapture)
        return self


# ---- scripted OCR ---------------------------------------------------------------------------


def block(text: str, y: int = 10, x: int = 5, w: int = 120, h: int = 20, lang: str = "zh") -> Any:
    return TextBlock(text, Rect(x, y, w, h), 0.95, lang)


class OcrScript:
    """Controls one fake OCR engine. ``blocks`` is what each read returns (frame coordinates).

    ``waits`` is a list of events: each read pops the first and waits for it, so a test can
    make the first read hang and hold the second one until it has looked at the status.
    """

    def __init__(self, name: str, blocks: Sequence[TextBlock] = ()) -> None:
        self.name = name
        self.blocks = list(blocks)
        self.calls: list[tuple[Rect, list[Rect] | None]] = []
        self.opens = 0
        self.open_error: Exception | None = None
        self.read_error: Exception | None = None
        self.waits: list[threading.Event] = []

    def register(self, registry: Registry) -> OcrScript:
        script = self

        class FakeOCR:
            name = script.name
            REQUIRES_CONTRACT = REQUIRES
            languages = ("zh", "en", "ja")

            def __init__(self, context: Any) -> None:
                pass

            def open(self) -> None:
                script.opens += 1
                if script.open_error is not None:
                    raise script.open_error

            def recognize(self, frame: Frame, regions: Sequence[Rect] | None) -> Sequence[Any]:
                script.calls.append((frame.region, None if regions is None else list(regions)))
                if script.waits:
                    script.waits.pop(0).wait(BLOCK_S)
                if script.read_error is not None:
                    raise script.read_error
                return list(script.blocks)

            def close(self) -> None:
                pass

            def health(self) -> Health:
                return Health("ok")

        assert registry.register("ocr", self.name, FakeOCR)
        return self


# ---- scripted translation stage -------------------------------------------------------------


class StageScript:
    """Controls the translation stage the pipeline builds (a fresh stage each time)."""

    def __init__(self) -> None:
        self.batches: list[list[TranslationItem]] = []
        self.stages_made = 0
        self.closed = 0
        self.waits: list[threading.Event] = []
        self.error: Exception | None = None
        self.crash_once = False
        self.state = Health("ok")

    @property
    def texts(self) -> list[str]:
        return [i.text for batch in self.batches for i in batch]

    def factory(self) -> Callable[[], Any]:
        script = self

        class Stage:
            def open(self) -> None:
                pass

            def translate(
                self, items: Sequence[TranslationItem], source_lang: str | None = None
            ) -> tuple[TranslationResult, ...]:
                script.batches.append(list(items))
                if script.waits:
                    script.waits.pop(0).wait(BLOCK_S)
                if script.crash_once:
                    script.crash_once = False
                    raise RuntimeError("scripted translate-stage crash")
                if script.error is not None:
                    raise script.error
                return tuple(
                    TranslationResult(i.line_id, i.text, f"EN:{i.text}", "fake", "m", False, 1)
                    for i in items
                )

            def health(self) -> Health:
                return script.state

            def retry_unavailable(self) -> None:
                pass

            def close(self) -> None:
                script.closed += 1

        def make() -> Stage:
            script.stages_made += 1
            return Stage()

        return make


# ---- recording sink -------------------------------------------------------------------------


class RecordingSink:
    """A ``ResultSink`` that keeps everything it is given."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.items: list[RenderItem] = []
        self.metas: list[FrameMeta | None] = []
        self.removed: list[str] = []
        self.healths: list[Health] = []
        self.fail_set_health = 0  # the next N set_health calls raise

    def show_results(self, items: Sequence[RenderItem], frame_meta: FrameMeta | None) -> None:
        with self._lock:
            self.items.extend(items)
            self.metas.append(frame_meta)

    def remove_lines(self, line_ids: Sequence[str]) -> None:
        with self._lock:
            self.removed.extend(line_ids)

    def set_health(self, health: Health) -> None:
        with self._lock:
            self.healths.append(health)
            if self.fail_set_health > 0:
                self.fail_set_health -= 1
                raise RuntimeError("scripted sink failure")

    def snapshot(self) -> list[RenderItem]:
        with self._lock:
            return list(self.items)

    def shown(self, status: str = "ok") -> list[RenderItem]:
        return [i for i in self.snapshot() if i.result.status == status]

    def texts(self, status: str = "ok") -> list[str]:
        return [i.result.source_text for i in self.shown(status)]

    def states(self) -> list[str]:
        with self._lock:
            return [h.state for h in self.healths]

    def last_health(self) -> Health | None:
        with self._lock:
            return self.healths[-1] if self.healths else None


# ---- the rig --------------------------------------------------------------------------------


class Rig:
    """A registry, scripts, a sink and a pipeline. ``close`` releases every scripted hang."""

    def __init__(self, config: GlossConfig, target: CaptureTarget | None = None) -> None:
        self.config = config
        self.registry = Registry()
        self.sink = RecordingSink()
        self.stage = StageScript()
        self.captures: dict[str, CaptureScript] = {}
        self.ocrs: dict[str, OcrScript] = {}
        self.target = target
        self.pipeline: Pipeline | None = None
        self._events: list[threading.Event] = []

    def capture(self, name: str, image: np.ndarray | None = None) -> CaptureScript:
        script = CaptureScript(name, image).register(self.registry)
        self.captures[name] = script
        return script

    def ocr(self, name: str, blocks: Sequence[TextBlock] = ()) -> OcrScript:
        script = OcrScript(name, blocks).register(self.registry)
        self.ocrs[name] = script
        return script

    def event(self) -> threading.Event:
        ev = threading.Event()
        self._events.append(ev)
        return ev

    def start(self, *, no_target: bool = False) -> Pipeline:
        target = self.target or CaptureTarget("region", region=Rect(100, 50, 200, 120))
        if no_target:
            target = None
        self.pipeline = Pipeline(
            self.config,
            registry=self.registry,
            sink=self.sink,
            make_translation=self.stage.factory(),
            target=target,
        )
        self.pipeline.start()
        return self.pipeline

    def close(self) -> None:
        for ev in self._events:
            ev.set()
        for script in self.captures.values():
            script.release()
        if self.pipeline is not None:
            self.pipeline.stop(timeout_s=1.0)
