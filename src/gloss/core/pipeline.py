"""The running pipeline: capture, change detection, OCR, text tracking, translation, display.

Three stage workers, one thread each (see "Threading and timeouts" in ``ports.py``):

- **capture** grabs at ``capture.fps_max``, checks for black frames, runs the change detector
  and posts the result to the OCR mailbox.
- **ocr** reads the changed regions, feeds the TextTracker and queues new stable lines.
- **translate** collects lines into batches and hands finished lines to the sink.

The capture-to-OCR mailbox holds one piece of work: the latest frame, with the dirty
regions of every frame it replaced merged in, so dropping a stale frame never loses a
change. The translate queue holds up to ``reliability.queue_size`` full batches; beyond that
the oldest line is dropped and the status says so.

A watchdog thread applies the per-call timeouts. A stage whose call runs too long is
abandoned (Python cannot stop a thread) and replaced by a fresh worker with fresh adapters.
The stuck thread notices when it wakes and exits without touching shared state. The
watchdog also combines every stage's health and reports it to the sink when it changes.

The pipeline knows ports only. Adapters come from the registry, by the names in config.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from gloss.core.config import GlossConfig
from gloss.core.contracts import (
    AdapterContext,
    AdapterUnavailable,
    CaptureSource,
    CaptureTarget,
    Frame,
    FrameMeta,
    GlossError,
    Health,
    PermanentError,
    Rect,
    RenderItem,
    SecretStore,
    TextBlock,
    TranslationItem,
    TranslationResult,
)
from gloss.core.registry import Registry
from gloss.services.batching import collect_batch
from gloss.services.black_frame import BlackFrameCheck
from gloss.services.change_detector import ChangeDetector, ChangeResult
from gloss.services.reliability import CircuitBreaker
from gloss.services.text_tracker import TextTracker, TrackerConfig, TrackerUpdate

log = logging.getLogger(__name__)

_RANK = {"ok": 0, "degraded": 1, "down": 2}
_IDLE_WAIT_S = 0.5  # how long an idle worker sleeps before checking for stop or new work


def worst(*healths: Health) -> Health:
    """The most serious health. Ties keep the first, so callers list the likely cause first."""
    best = Health("ok")
    for health in healths:
        if _RANK[health.state] > _RANK[best.state]:
            best = health
    return best


class ResultSink(Protocol):
    """Where the pipeline sends its output. Every method must return at once, from any thread.

    ``GlossShell`` is the real one. ``remove_lines`` is for lines that left the screen.
    """

    def show_results(self, items: Sequence[RenderItem], frame_meta: FrameMeta | None) -> None: ...

    def remove_lines(self, line_ids: Sequence[str]) -> None: ...

    def set_health(self, health: Health) -> None: ...


class TranslationStage(Protocol):
    """What the translate worker needs: a ``TranslationService`` plus whatever it owns."""

    def open(self) -> None: ...

    def translate(
        self, items: Sequence[TranslationItem], source_lang: str | None = None
    ) -> tuple[TranslationResult, ...]: ...

    def health(self) -> Health: ...

    def retry_unavailable(self) -> None: ...

    def close(self) -> None: ...


# ---- adapter chains (capture and OCR) ------------------------------------------------------


@dataclass
class _Link:
    name: str
    breaker: CircuitBreaker
    adapter: Any = None
    disabled: str = ""  # set after a PermanentError, for this session
    unavailable_until: float = 0.0
    reason: str = ""


class _Chain:
    """The primary adapter and its fallbacks for one stage, with the failure model applied.

    Not thread safe: it belongs to one stage worker, which opens, calls and closes adapters.
    """

    def __init__(
        self,
        port: str,
        names: Sequence[str],
        make: Callable[[str], Any],
        config: GlossConfig,
        clock: Callable[[], float],
        hangs: dict[str, int] | None = None,
    ) -> None:
        """``hangs`` counts calls per adapter that the watchdog gave up on, in earlier workers."""
        r = config.reliability
        self.port = port
        self._make = make
        self._clock = clock
        self._cooldown_s = r.breaker_cooldown_s
        self.links = [
            _Link(n, CircuitBreaker(r.breaker_failures, r.breaker_cooldown_s, clock)) for n in names
        ]
        self.active: int | None = None
        for link in self.links:
            # A call that hangs every time must still trip the breaker and reach the fallback.
            for _ in range((hangs or {}).get(link.name, 0)):
                link.breaker.record_failure()

    def get(self, opener: Callable[[Any], None]) -> tuple[int, Any] | None:
        """Return the first usable adapter, opening it if needed, or None if none works."""
        now = self._clock()
        for index, link in enumerate(self.links):
            if link.disabled or link.unavailable_until > now:
                continue
            if not link.breaker.allow():
                continue
            if link.adapter is None:
                try:
                    adapter = self._make(link.name)
                    opener(adapter)
                except Exception as exc:
                    self.failed(index, exc)
                    continue
                link.adapter = adapter
            self._use(index)
            return index, link.adapter
        self.active = None
        return None

    def _use(self, index: int) -> None:
        if self.active != index:
            if index > 0:
                log.warning("%s: using fallback %s", self.port, self.links[index].name)
            for later in self.links[index + 1 :]:
                self._close(later)
        self.active = index

    def succeeded(self, index: int) -> None:
        self.links[index].breaker.record_success()

    def failed(self, index: int, exc: BaseException) -> None:
        """Apply the failure model to one failed open or call."""
        link = self.links[index]
        name = type(exc).__name__
        if isinstance(exc, AdapterUnavailable):
            self._close(link)
            link.unavailable_until = self._clock() + self._cooldown_s
            link.reason = str(exc) or "it is not available on this PC"
            link.breaker.release_trial()
        elif isinstance(exc, PermanentError):
            self._close(link)
            link.disabled = str(exc) or "it reported a setup problem"
            link.breaker.release_trial()
        else:
            # Log the type only: a foreign message could carry screen text.
            log.warning("%s adapter %s failed: %s", self.port, link.name, name)
            link.reason = str(exc) if isinstance(exc, GlossError) else f"unexpected {name}"
            link.breaker.record_failure()
            if link.breaker.state != "closed":
                self._close(link)
        if self.active == index:
            self.active = None

    def skip(self, index: int, reason: str) -> None:
        """Stop using one adapter until ``reset`` (for example: it captures only black)."""
        link = self.links[index]
        self._close(link)
        link.unavailable_until = float("inf")
        link.reason = reason

    def next_usable(self, after: int) -> int | None:
        now = self._clock()
        for index in range(after + 1, len(self.links)):
            link = self.links[index]
            if not link.disabled and link.unavailable_until <= now and link.breaker.state != "open":
                return index
        return None

    def open_link(self, index: int, opener: Callable[[Any], None]) -> Any | None:
        link = self.links[index]
        if link.adapter is None:
            try:
                adapter = self._make(link.name)
                opener(adapter)
            except Exception as exc:
                self.failed(index, exc)
                return None
            link.adapter = adapter
        return link.adapter

    def release(self, index: int) -> None:
        """Close an adapter that was opened only for a trial."""
        if index != self.active:
            self._close(self.links[index])

    def reset(self) -> None:
        """Close everything and forget unavailability, for a new target. Disabled stays."""
        for link in self.links:
            self._close(link)
            link.unavailable_until = 0.0
        self.active = None

    def close(self) -> None:
        for link in self.links:
            self._close(link)
        self.active = None

    def _close(self, link: _Link) -> None:
        adapter, link.adapter = link.adapter, None
        if adapter is not None:
            try:
                adapter.close()
            except Exception:  # Closing must never crash the app.
                log.warning("%s adapter %s did not close cleanly", self.port, link.name)

    def health(self) -> Health:
        """Summarise for the status dot. Call from the owning worker."""
        if not self.links:
            return Health("down", f"No {self.port} method is configured.")
        if self.active is None:
            reasons = [lk.disabled or lk.reason for lk in self.links if lk.disabled or lk.reason]
            why = f" ({reasons[0]})" if reasons else ""
            return Health("down", f"No {self.port} method is working right now{why}.")
        link = self.links[self.active]
        try:
            own = link.adapter.health() if link.adapter is not None else Health("ok")
        except Exception:
            own = Health("degraded", f"The {self.port} method did not report its state.")
        if self.active > 0:
            first = self.links[0]
            why = first.disabled or first.reason or "it failed"
            return worst(own, Health("degraded", f"Using the backup {self.port} method: {why}."))
        return own


# ---- stage plumbing -------------------------------------------------------------------------


class _Watch:
    """When a stage's current call started, so the watchdog can spot a hang."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._since: float | None = None

    def begin(self) -> None:
        with self._lock:
            self._since = self._clock()

    def end(self) -> None:
        with self._lock:
            self._since = None

    def busy_for(self) -> float:
        with self._lock:
            return 0.0 if self._since is None else self._clock() - self._since


@dataclass
class _Work:
    """The OCR mailbox's content. ``regions`` None means the whole frame."""

    frame: Frame
    regions: list[Rect] | None
    scroll_dy: int = 0
    unchanged: bool = False
    scroll_area: Rect | None = None


class _Mailbox:
    """Latest frame wins, but the changes of replaced frames are merged in."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._work: _Work | None = None

    def post(self, frame: Frame, change: ChangeResult) -> None:
        with self._cond:
            old = self._work
            if not change.changed:
                if old is None:
                    self._work = _Work(frame, [], unchanged=True)
            else:
                regions = None if change.full else list(change.dirty)
                dy = change.scrolls[0].dy if len(change.scrolls) == 1 else 0
                area = change.scrolls[0].area if len(change.scrolls) == 1 else None
                if len(change.scrolls) > 1:
                    regions = None
                if old is None or old.unchanged:
                    self._work = _Work(frame, regions, dy, scroll_area=area)
                elif (
                    old.regions is not None
                    and regions is not None
                    and old.scroll_area is not None
                    and old.scroll_area == area
                    and old.frame.region == frame.region
                ):
                    # Two scrolls of one area: the older exposed bands moved with the newer shift.
                    h, w = frame.image.shape[:2]
                    bounds = Rect(0, 0, w, h)
                    shifted = [_clip(r.translated(0, -dy), bounds) for r in old.regions]
                    kept = [r for r in shifted if r is not None]
                    self._work = _Work(frame, kept + regions, old.scroll_dy + dy, scroll_area=area)
                elif (
                    old.regions is None
                    or regions is None
                    or old.scroll_dy
                    or dy
                    or old.frame.region != frame.region
                ):
                    self._work = _Work(frame, None)  # too much to merge: read it all
                else:
                    self._work = _Work(frame, old.regions + regions)
            self._cond.notify()

    def take(self, timeout: float) -> _Work | None:
        with self._cond:
            if self._work is None:
                self._cond.wait(timeout)
            work, self._work = self._work, None
            return work

    def clear(self) -> None:
        with self._cond:
            self._work = None


@dataclass(frozen=True, slots=True)
class _Pending:
    """One line waiting for translation."""

    line_id: str
    text: str
    low_quality: bool
    group_id: str | None
    bbox: Rect | None  # desktop coordinates


@dataclass
class _Stage:
    name: str
    timeout_s: float
    watch: _Watch
    generation: int = 0
    thread: threading.Thread | None = None
    health: Health = field(default_factory=lambda: Health("ok"))
    note: Health = field(default_factory=lambda: Health("ok"))  # set by the watchdog
    calling: str = ""  # the adapter whose call is running, for the watchdog
    hangs: dict[str, int] = field(default_factory=dict)  # adapter name -> calls given up on


# ---- the pipeline ---------------------------------------------------------------------------


class Pipeline:
    """Start with ``start()``; every other public method is safe from any thread."""

    def __init__(
        self,
        config: GlossConfig,
        *,
        registry: Registry,
        sink: ResultSink,
        make_translation: Callable[[], TranslationStage],
        secrets: SecretStore | None = None,
        target: CaptureTarget | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._registry = registry
        self._sink = sink
        self._make_translation = make_translation
        self._secrets = secrets
        self._clock = clock
        r = config.reliability
        retry_s = (r.retry_max * r.retry_max_delay_ms) / 1000
        self._stages = {
            "capture": _Stage("capture", r.capture_timeout_ms / 1000, _Watch(clock)),
            "ocr": _Stage("ocr", r.ocr_timeout_ms / 1000, _Watch(clock)),
            # The service may retry, so allow every attempt its timeout plus the waits.
            "translate": _Stage(
                "translate",
                (r.retry_max + 1) * r.translate_timeout_ms / 1000 + retry_s,
                _Watch(clock),
            ),
        }
        self._lock = threading.RLock()  # guards generations, the tracker and line state
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._mailbox = _Mailbox()
        t = config.translate
        self._lines: queue.Queue[_Pending] = queue.Queue(maxsize=r.queue_size * t.batch_max_lines)
        self._dropped_lines = 0
        self._tracker = TextTracker(
            TrackerConfig(
                stable_reads=config.ocr.stable_reads,
                dedupe_window_s=config.ocr.dedupe_window_s,
                dedupe_max_lines=config.ocr.dedupe_max_lines,
                skip_langs=(t.target_lang,),
            )
        )
        self._bboxes: dict[str, Rect] = {}  # line_id -> desktop bbox, for lines on screen
        self._shown: dict[str, RenderItem] = {}  # line_id -> what was last shown
        self._latest_meta: FrameMeta | None = None
        self._scrolled_at = float("-inf")  # timestamp of the newest frame moved by a scroll
        self._target = target
        self._target_serial = 0
        self._paused = False
        self._low_quality = False
        self._ocr_serial = 0
        self._retry_translation = False
        self._reread = False  # set when OCR lost a frame, so the next one is read in full
        self._in_flight: list[_Pending] = []  # the batch being translated
        self._watchdog: threading.Thread | None = None
        self._last_reported: Health | None = None

    # ---- control (any thread)

    def start(self) -> None:
        for name in self._stages:
            self._spawn(name)
        self._watchdog = threading.Thread(
            target=self._watch_loop, name="gloss-watchdog", daemon=True
        )
        self._watchdog.start()

    def stop(self, timeout_s: float = 2.0) -> None:
        """Ask every worker to finish. A stuck worker is left behind (it is a daemon)."""
        self._stop.set()
        self._wake.set()
        for stage in self._stages.values():
            if stage.thread is not None:
                stage.thread.join(timeout_s)
        if self._watchdog is not None:
            self._watchdog.join(timeout_s)

    def set_target(self, target: CaptureTarget | None) -> None:
        with self._lock:
            self._target = target
            self._target_serial += 1
        self._wake.set()

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self._wake.set()

    def set_low_quality(self, on: bool) -> None:
        """Switch OCR to ``ocr.low_quality_engine`` for this session, or back."""
        with self._lock:
            self._low_quality = on
            self._ocr_serial += 1

    def retry_translation(self) -> None:
        """Try unavailable translators again, after the user stores an API key."""
        self._retry_translation = True

    def health(self) -> Health:
        stages = self._stages.values()
        overall = worst(*(s.note for s in stages), *(s.health for s in stages))
        if self._dropped_lines:
            overall = worst(
                overall,
                Health("degraded", "Translation could not keep up, so some lines were skipped."),
            )
        if self._paused:
            return Health(overall.state, "Paused." if overall.state == "ok" else overall.detail)
        return overall

    # ---- workers

    def _spawn(self, name: str) -> None:
        stage = self._stages[name]
        with self._lock:
            stage.generation += 1
            generation = stage.generation
            stage.watch = _Watch(self._clock)  # a stuck old worker keeps its own
        body = {
            "capture": self._capture_loop,
            "ocr": self._ocr_loop,
            "translate": self._translate_loop,
        }
        stage.thread = threading.Thread(
            target=self._guarded,
            args=(name, body[name], generation),
            name=f"gloss-{name}",
            daemon=True,
        )
        stage.thread.start()

    def _current(self, name: str, generation: int) -> bool:
        return not self._stop.is_set() and self._stages[name].generation == generation

    def _guarded(self, name: str, body: Callable[[int], None], generation: int) -> None:
        """Run a worker loop; a crash restarts it instead of ending the stage."""
        try:
            body(generation)
        except Exception as exc:
            log.error("The %s worker crashed (%s); restarting it", name, type(exc).__name__)
            if name == "translate" and self._current(name, generation):
                lost, self._in_flight = list(self._in_flight), []
                self._show_failed(lost)
            self._stages[name].health = Health(
                "degraded", f"The {name} step restarted after an error."
            )
            # Wait first, so a stage that fails at once every time cannot spin.
            self._stop.wait(self._config.reliability.retry_max_delay_ms / 1000)
            with self._lock:
                if self._current(name, generation):
                    self._spawn(name)

    def _capture_loop(self, generation: int) -> None:
        cfg = self._config
        stage = self._stages["capture"]
        watch = stage.watch
        detector = ChangeDetector(tile_size=cfg.capture.tile_size)
        black = BlackFrameCheck(cfg.capture.black_frame_reads, cfg.capture.black_frame_max_mean)
        choice = cfg.capture.active
        names = self._registry.resolve_chain("capture", choice.source, choice.fallbacks)
        chain = _Chain(
            "capture",
            names,
            lambda n: self._create("capture", n),
            cfg,
            self._clock,
            self._stages["capture"].hangs,
        )
        busy_interval = 1.0 / cfg.capture.fps_max
        idle_interval = 1.0 / cfg.capture.idle_fps
        last_change = float("-inf")
        serial = -1
        target: CaptureTarget | None = None
        try:
            while self._current("capture", generation):
                with self._lock:
                    if serial != self._target_serial:
                        serial, target = self._target_serial, self._target
                        chain.reset()
                        detector.reset()
                        black.reset()
                    if self._reread:
                        self._reread = False
                        detector.reset()
                if self._paused or target is None:
                    stage.health = (
                        Health("ok")
                        if target
                        else Health("degraded", "Choose a screen area to start translating.")
                    )
                    self._wake.wait(_IDLE_WAIT_S)
                    self._wake.clear()
                    continue
                started = self._clock()
                opener = _target_opener(target)
                got = chain.get(opener)
                frame: Frame | None = None
                if got is not None:
                    index, source = got
                    stage.calling = chain.links[index].name
                    watch.begin()
                    try:
                        frame = source.grab()
                    except Exception as exc:
                        if self._current("capture", generation):
                            chain.failed(index, exc)
                    else:
                        if self._current("capture", generation):
                            chain.succeeded(index)
                            stage.hangs.pop(chain.links[index].name, None)
                    finally:
                        watch.end()
                    if not self._current("capture", generation):
                        return  # given up on: leave the stage's state to the new worker
                    if frame is not None:
                        stage.note = Health("ok")
                    if frame is not None and black.observe(frame):
                        frame = self._black_trial(chain, index, opener, black, detector) or frame
                stage.health = worst(chain.health(), black.health())
                if frame is not None:
                    change = detector.detect(frame)
                    if change.changed:
                        last_change = self._clock()
                    if change.scrolls:
                        self._follow_scroll(frame, change)
                    self._mailbox.post(frame, change)
                # Look often while the screen is changing, and rarely once it is still.
                busy = self._clock() - last_change < cfg.capture.idle_after_s
                interval = busy_interval if busy else idle_interval
                self._stop.wait(max(0.0, interval - (self._clock() - started)))
        finally:
            chain.close()  # this worker's own adapters, even if it was given up on

    def _black_trial(
        self,
        chain: _Chain,
        index: int,
        opener: Callable[[Any], None],
        black: BlackFrameCheck,
        detector: ChangeDetector,
    ) -> Frame | None:
        trial_index = chain.next_usable(index)
        source = chain.open_link(trial_index, opener) if trial_index is not None else None
        if trial_index is None or source is None:
            black.trial_unavailable()
            return None
        try:
            trial = source.grab()
        except Exception as exc:
            chain.failed(trial_index, exc)
            black.record_trial(None)
            return None
        if black.record_trial(trial):
            chain.skip(index, "it captured only black for this target")
            black.reset()
            detector.reset()
            return trial
        chain.release(trial_index)
        return None

    def _ocr_loop(self, generation: int) -> None:
        stage = self._stages["ocr"]
        watch = stage.watch
        chain: _Chain | None = None
        serial = -1
        try:
            while self._current("ocr", generation):
                with self._lock:
                    if serial != self._ocr_serial or chain is None:
                        serial = self._ocr_serial
                        if chain is not None:
                            chain.close()
                        chain = self._ocr_chain()
                work = self._mailbox.take(_IDLE_WAIT_S)
                if work is None:
                    continue
                if work.unchanged:
                    with self._lock:
                        if not self._current("ocr", generation):
                            return
                        self._publish(self._tracker.unchanged(now=self._clock()), work.frame)
                    continue
                got = chain.get(lambda engine: engine.open())
                if got is None:
                    stage.health = chain.health()
                    self._reread = True
                    continue
                index, engine = got
                stage.calling = chain.links[index].name
                watch.begin()
                try:
                    blocks = engine.recognize(work.frame, work.regions)
                except Exception as exc:
                    if self._current("ocr", generation):
                        chain.failed(index, exc)
                        stage.health = chain.health()
                        self._reread = True
                    continue
                finally:
                    watch.end()
                with self._lock:
                    if not self._current("ocr", generation):
                        return  # given up on: leave the stage's state to the new worker
                    chain.succeeded(index)
                    stage.hangs.pop(chain.links[index].name, None)
                    stage.health = chain.health()
                    stage.note = Health("ok")
                    if work.scroll_dy:
                        h, w = work.frame.image.shape[:2]
                        self._tracker.scroll(-work.scroll_dy, frame=Rect(0, 0, w, h))
                    scanned = work.regions
                    update = self._tracker.update(blocks, now=self._clock(), scanned=scanned)
                    self._publish(update, work.frame)
        finally:
            if chain is not None:
                chain.close()

    def _ocr_chain(self) -> _Chain:
        o = self._config.ocr
        if self._low_quality:
            names = self._registry.resolve_chain("ocr", o.low_quality_engine)
        else:
            names = self._registry.resolve_chain("ocr", o.engine, o.fallbacks)
        return _Chain(
            "ocr",
            names,
            lambda n: self._create("ocr", n),
            self._config,
            self._clock,
            self._stages["ocr"].hangs,
        )

    def _follow_scroll(self, frame: Frame, change: ChangeResult) -> None:
        """Move shown lines with a detected scroll at once, before OCR confirms them."""
        ox, oy = frame.region.x, frame.region.y
        moved: list[RenderItem] = []
        gone: list[str] = []
        with self._lock:
            for shift in change.scrolls:
                area = shift.area.translated(ox, oy)
                for line_id, bbox in list(self._bboxes.items()):
                    cx, cy = bbox.x + bbox.w // 2, bbox.y + bbox.h // 2
                    if not (area.x <= cx < area.right and area.y <= cy < area.bottom):
                        continue
                    new = bbox.translated(0, -shift.dy)
                    if _clip(new, area) is None:
                        gone.append(line_id)
                        self._bboxes.pop(line_id, None)
                        self._shown.pop(line_id, None)
                        continue
                    self._bboxes[line_id] = new
                    shown = self._shown.get(line_id)
                    if shown is not None:
                        item = RenderItem(shown.result, new, shown.low_quality, shown.group_id)
                        self._shown[line_id] = item
                        moved.append(item)
            self._scrolled_at = frame.timestamp
            self._latest_meta = frame.meta
            meta = self._latest_meta
        if gone:
            self._sink.remove_lines(gone)
        if moved:
            self._sink.show_results(moved, meta)

    def _publish(self, update: TrackerUpdate, frame: Frame) -> None:
        """Act on one tracker update. Called with ``self._lock`` held, on the OCR worker."""
        stale = frame.timestamp < self._scrolled_at  # older than a scroll already shown
        if not stale:
            self._latest_meta = frame.meta
        ox, oy = frame.region.x, frame.region.y
        threshold = self._config.render.move_threshold_px
        moved: list[RenderItem] = []
        for block in update.visible:
            if block.line_id is None:
                continue
            bbox = block.bbox.translated(ox, oy)
            old = self._bboxes.get(block.line_id)
            if old is not None and (stale or _settled(old, bbox, threshold)):
                continue  # OCR jitter, or a position from before the scroll
            self._bboxes[block.line_id] = bbox
            shown = self._shown.get(block.line_id)
            if shown is not None:
                item = RenderItem(shown.result, bbox, shown.low_quality, shown.group_id)
                self._shown[block.line_id] = item
                moved.append(item)
        for block in update.new:
            if block.line_id is None or not block.needs_translation:
                continue
            self._queue_line(block, block.bbox.translated(ox, oy))
        if update.dropped:
            for line_id in update.dropped:
                self._bboxes.pop(line_id, None)
                self._shown.pop(line_id, None)
            self._sink.remove_lines(update.dropped)
        if moved:
            self._sink.show_results(moved, self._latest_meta)

    def _queue_line(self, block: TextBlock, bbox: Rect) -> None:
        pending = _Pending(block.line_id or "", block.text, block.low_quality, block.group_id, bbox)
        while True:
            try:
                self._lines.put_nowait(pending)
                return
            except queue.Full:
                try:
                    self._lines.get_nowait()
                    self._dropped_lines += 1
                except queue.Empty:
                    pass

    def _translate_loop(self, generation: int) -> None:
        stage = self._stages["translate"]
        watch = stage.watch
        t = self._config.translate
        service: TranslationStage | None = None
        try:
            service = self._make_translation()
            watch.begin()
            try:
                service.open()
            finally:
                watch.end()
            stage.health = service.health()
            while self._current("translate", generation):
                if self._retry_translation:
                    self._retry_translation = False
                    service.retry_unavailable()
                batch = collect_batch(
                    self._lines,
                    t.batch_max_lines,
                    t.batch_max_wait_ms / 1000,
                    first_timeout_s=_IDLE_WAIT_S,
                )
                if not batch:
                    continue
                items = [TranslationItem(p.line_id, p.text, p.low_quality) for p in batch]
                self._in_flight = batch
                watch.begin()
                try:
                    results = service.translate(items, source_lang=None)
                finally:
                    watch.end()
                if not self._current("translate", generation):
                    return  # the watchdog gave up on this batch and showed it as failed
                self._in_flight = []
                stage.health = service.health()
                stage.note = Health("ok")
                if self._lines.empty():
                    self._dropped_lines = 0
                self._show(batch, results)
        finally:
            if service is not None:
                try:
                    service.close()
                except Exception:
                    log.warning("The translation service did not close cleanly")

    def _show(self, batch: Sequence[_Pending], results: Sequence[TranslationResult]) -> None:
        items: list[RenderItem] = []
        with self._lock:
            for pending, result in zip(batch, results, strict=True):
                bbox = self._bboxes.get(pending.line_id, pending.bbox)
                item = RenderItem(result, bbox, pending.low_quality, pending.group_id)
                self._shown[pending.line_id] = item
                items.append(item)
            meta = self._latest_meta
        self._sink.show_results(items, meta)

    def _create(self, port: str, name: str) -> Any:
        settings = self._config.adapter_settings(port, name)
        return self._registry.create(port, name, AdapterContext(settings, self._secrets))

    # ---- watchdog

    def _watch_loop(self) -> None:
        tick = min(s.timeout_s for s in self._stages.values()) / 4
        while not self._stop.wait(tick):
            for name, stage in self._stages.items():
                if stage.watch.busy_for() > stage.timeout_s:
                    self._restart(name)
            health = self.health()
            if health != self._last_reported:
                self._last_reported = health
                try:
                    self._sink.set_health(health)
                except Exception:
                    log.warning("Could not report the status")

    def _restart(self, name: str) -> None:
        stage = self._stages[name]
        log.warning("The %s step did not answer in %.1f s; restarting it", name, stage.timeout_s)
        stage.note = Health("degraded", f"The {name} step stopped responding and was restarted.")
        if name in ("capture", "ocr") and stage.calling:
            stage.hangs[stage.calling] = stage.hangs.get(stage.calling, 0) + 1
        lost: list[_Pending] = []
        if name == "ocr":
            self._reread = True
        if name == "translate":
            lost, self._in_flight = list(self._in_flight), []
        self._spawn(name)
        self._show_failed(lost)

    def _show_failed(self, lost: Sequence[_Pending]) -> None:
        """Show lines that will never get a translation as failed, rather than drop them."""
        if lost:
            failed = [
                TranslationResult(p.line_id, p.text, "", "", None, False, 0, "failed", "other")
                for p in lost
            ]
            self._show(lost, failed)


def _clip(rect: Rect, bounds: Rect) -> Rect | None:
    x, y = max(rect.x, bounds.x), max(rect.y, bounds.y)
    right, bottom = min(rect.right, bounds.right), min(rect.bottom, bounds.bottom)
    if right <= x or bottom <= y:
        return None
    return Rect(x, y, right - x, bottom - y)


def _settled(old: Rect, new: Rect, threshold_px: int) -> bool:
    """True when ``new`` differs from ``old`` by no more than OCR jitter."""
    limit = max(threshold_px, old.h // 4)
    return (
        abs(new.x - old.x) <= limit
        and abs(new.y - old.y) <= limit
        and abs(new.right - old.right) <= limit
        and abs(new.bottom - old.bottom) <= limit
    )


def _target_opener(target: CaptureTarget) -> Callable[[CaptureSource], None]:
    def opener(source: CaptureSource) -> None:
        source.open(target)

    return opener


__all__ = ["Pipeline", "ResultSink", "TranslationStage", "worst"]
