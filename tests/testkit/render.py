"""Renderer contract checks, plus a conforming fake and deliberately broken ones."""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from typing import Any

from gloss.core.contracts import (
    AdapterContext,
    FrameMeta,
    Health,
    Rect,
    RenderItem,
    TranslationResult,
)
from testkit.core import (
    COMMON_CHECKS,
    RENDER_CALL_DEADLINE_S,
    WDA_EXCLUDEFROMCAPTURE,
    Broken,
    Subject,
    call,
    check_close_is_always_safe,
    check_health_is_instant_in_every_state,
    hostile_settings_breakers,
    identity_breakers,
    registered_subject,
)

PORT = "render"
FAKE_DEADLINE_S = 0.15
META = FrameMeta("test", Rect(-1920, 0, 800, 400), time.monotonic(), 1.5)


def result(text: str, *, status: str = "ok", line_id: str = "L0") -> TranslationResult:
    if status == "failed":
        return TranslationResult(line_id, "原文", "", "p", "m", False, 5, "failed", "refused")
    shown = "原文" if status == "passthrough" else text
    return TranslationResult(line_id, "原文", shown, "p", "m", False, 5, status)  # type: ignore[arg-type]


def varied_items() -> list[RenderItem]:
    """Every kind of line a renderer will see."""
    return [
        RenderItem(result("Hello"), Rect(10, 10, 100, 20)),
        RenderItem(result("No position"), None),
        RenderItem(result("On a monitor left of the primary"), Rect(-1800, -40, 300, 20)),
        RenderItem(result("Zero size"), Rect(5, 5, 0, 0)),
        RenderItem(result("", status="failed", line_id="L4"), Rect(0, 0, 50, 10)),
        RenderItem(result("", status="passthrough", line_id="L5"), None, low_quality=True),
        RenderItem(result("长 " * 2500, line_id="L6"), Rect(1, 1, 9, 9), group_id="g1"),
        RenderItem(result("😀🎮 mixed العربية", line_id="L7"), Rect(3, 3, 3, 3), group_id="g1"),
        RenderItem(result("   ", line_id="L8"), None),
    ]


def quiet(fn: Any, *args: Any) -> None:
    """Run a renderer method that has no business raising anything at all."""
    try:
        fn(*args)
    except Exception as exc:
        raise AssertionError(
            f"{getattr(fn, '__qualname__', fn)} raised {type(exc).__name__}: {exc}. "
            "A renderer shows what it is given and never raises for valid input."
        ) from exc


def every_call(adapter: Any) -> None:
    """Exercise each method that may be called from any thread, with awkward arguments."""
    items = varied_items()
    for frame_meta in (META, None):
        adapter.show(items, frame_meta)
        adapter.show(tuple(items), frame_meta)
    adapter.show([], None)
    adapter.clear()
    adapter.clear()
    adapter.set_visible(False)
    adapter.set_visible(True)
    for state in ("ok", "degraded", "down"):
        adapter.set_status(Health(state), "")  # type: ignore[arg-type]
        adapter.set_status(Health(state, "one readable sentence"), "banner: 低质量 mode")
    adapter.set_status(Health("ok"))


# ---- checks ------------------------------------------------------------------------------


def check_every_method_accepts_awkward_input(subject: Subject) -> None:
    adapter = subject.opened()
    quiet(every_call, adapter)


def check_calls_return_at_once(subject: Subject) -> None:
    adapter = subject.opened()
    limit = subject.timeout_s if subject.timeout_s is not None else RENDER_CALL_DEADLINE_S
    many = [
        RenderItem(result(f"line {i}", line_id=f"L{i}"), Rect(i, i, 50, 10)) for i in range(200)
    ]
    steps = {
        "show": lambda: adapter.show(many, META),
        "clear": adapter.clear,
        "set_visible": lambda: adapter.set_visible(True),
        "set_status": lambda: adapter.set_status(Health("degraded", "slow"), "banner"),
    }
    for name, step in steps.items():
        start = time.perf_counter()
        call(step)
        took = time.perf_counter() - start
        assert took < limit, (
            f"{name}() took {took * 1000:.0f} ms. It must return at once and hand the work to "
            "the Qt main thread."
        )


def check_methods_work_from_any_thread(subject: Subject) -> None:
    adapter = subject.opened()
    box: list[BaseException | None] = []

    def work() -> None:
        try:
            every_call(adapter)
            box.append(None)
        except BaseException as exc:
            box.append(exc)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(10.0)
    assert box, "calls from a worker thread did not finish within 10 s"
    assert box[0] is None, (
        f"a call from a non-main thread raised {type(box[0]).__name__}: {box[0]}. Only open() "
        "and close() are tied to the Qt main thread."
    )


def check_methods_before_open_and_after_close_are_safe(subject: Subject) -> None:
    adapter = subject.new()
    call(every_call, adapter)  # never opened
    subject.open(adapter)
    call(adapter.close)
    call(every_call, adapter)  # closed


def check_every_window_is_excluded_from_screen_capture(subject: Subject) -> None:
    """SPEC section 2.1 and acceptance 4: otherwise Gloss would read its own output."""
    assert subject.window_probe is not None, "this check needs a window probe"
    adapter = subject.opened()
    call(adapter.set_visible, True)
    call(adapter.show, varied_items(), META)
    deadline = time.monotonic() + 1.0
    windows = subject.window_probe(adapter)
    while not [w for w in windows if w[1]] and time.monotonic() < deadline:
        time.sleep(0.05)
        windows = subject.window_probe(adapter)
    visible = [w for w in windows if w[1]]
    assert visible, "no visible window was found after show(), so nothing could be checked"
    leaks = [hex(hwnd) for hwnd, _, affinity in visible if affinity != WDA_EXCLUDEFROMCAPTURE]
    assert not leaks, (
        f"windows {leaks} can be captured. Call SetWindowDisplayAffinity(hwnd, "
        "WDA_EXCLUDEFROMCAPTURE) on every Gloss window."
    )


CHECKS = (
    *COMMON_CHECKS,
    check_every_method_accepts_awkward_input,
    check_calls_return_at_once,
    check_methods_work_from_any_thread,
    check_methods_before_open_and_after_close_are_safe,
    check_every_window_is_excluded_from_screen_capture,
)
# Real renderers are probed with the Windows API, so only run these on Windows.
WINDOWS_ONLY = frozenset({check_every_window_is_excluded_from_screen_capture})

# ---- fakes -------------------------------------------------------------------------------


class FakeRenderer:
    """Follows the renderer contract: records calls and returns at once."""

    name = "fake_good"
    REQUIRES_CONTRACT = ">=1.0,<2.0"

    def __init__(self, context: AdapterContext) -> None:
        self._lock = threading.Lock()
        self._shown: list[RenderItem] = []
        self._visible = False
        self._status = Health("ok")
        self._banner = ""
        self.windows: list[tuple[int, bool, int]] = []
        self._open = False

    def open(self) -> None:
        self._open = True
        self.windows = [(1001, True, WDA_EXCLUDEFROMCAPTURE)]

    def show(self, items: Sequence[RenderItem], frame_meta: FrameMeta | None) -> None:
        with self._lock:
            self._shown = list(items)

    def clear(self) -> None:
        with self._lock:
            self._shown = []

    def set_visible(self, visible: bool) -> None:
        self._visible = visible

    def set_status(self, health: Health, banner: str = "") -> None:
        with self._lock:
            self._status, self._banner = health, banner

    def close(self) -> None:
        self._open = False
        self.windows = []

    def health(self) -> Health:
        return Health("ok" if self._open else "down", "")


class SlowShowRenderer(FakeRenderer):
    name = "fake_slow_show"

    def show(self, items, frame_meta):
        time.sleep(FAKE_DEADLINE_S * 2)
        super().show(items, frame_meta)


class MainThreadOnlyRenderer(FakeRenderer):
    name = "fake_main_thread_only"

    def show(self, items, frame_meta):
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("QWidget used outside the GUI thread")
        super().show(items, frame_meta)


class CrashesOnNoBboxRenderer(FakeRenderer):
    name = "fake_crashes_no_bbox"

    def show(self, items, frame_meta):
        for item in items:
            _ = item.bbox.x  # type: ignore[union-attr]
        super().show(items, frame_meta)


class CrashesOnNoFrameRenderer(FakeRenderer):
    name = "fake_crashes_no_frame"

    def show(self, items, frame_meta):
        _ = frame_meta.dpi_scale  # type: ignore[union-attr]
        super().show(items, frame_meta)


class CrashesOnFailedLineRenderer(FakeRenderer):
    name = "fake_crashes_failed_line"

    def show(self, items, frame_meta):
        for item in items:
            _ = item.result.text[0]  # a failed line has empty text
        super().show(items, frame_meta)


class CrashesOnBannerRenderer(FakeRenderer):
    name = "fake_crashes_banner"

    def set_status(self, health, banner=""):
        if health.state == "down":
            _ = banner[0]
        super().set_status(health, banner)


class CrashesBeforeOpenRenderer(FakeRenderer):
    name = "fake_crashes_before_open"

    def show(self, items, frame_meta):
        if not self._open:
            raise AttributeError("no window yet")
        super().show(items, frame_meta)


class CrashesAfterCloseRenderer(FakeRenderer):
    name = "fake_crashes_after_close"

    def clear(self):
        if not self._open and self.windows == [] and getattr(self, "_was_open", False):
            raise RuntimeError("wrapped C/C++ object has been deleted")
        super().clear()

    def open(self) -> None:
        self._was_open = True
        super().open()


class CloseTwiceRenderer(FakeRenderer):
    name = "fake_close_twice"

    def close(self):
        if not self._open:
            raise RuntimeError("already destroyed")
        super().close()


class SlowHealthRenderer(FakeRenderer):
    name = "fake_slow_health"

    def health(self):
        time.sleep(0.2)
        return super().health()


class CapturableWindowRenderer(FakeRenderer):
    name = "fake_capturable_window"

    def open(self) -> None:
        super().open()
        self.windows = [(1001, True, 0)]


class SecondWindowCapturableRenderer(FakeRenderer):
    name = "fake_second_window_capturable"

    def open(self) -> None:
        super().open()
        self.windows = [(1001, True, WDA_EXCLUDEFROMCAPTURE), (1002, True, 0)]


class NoWindowRenderer(FakeRenderer):
    name = "fake_no_window"

    def open(self) -> None:
        super().open()
        self.windows = []


def fake_subject(adapter: type = FakeRenderer, *, via_registry: bool = True) -> Subject:
    return registered_subject(
        PORT,
        adapter.name,
        adapter,
        via_registry=via_registry,
        timeout_s=FAKE_DEADLINE_S,
        window_probe=lambda a: list(a.windows),
    )


BROKEN = (
    Broken(SlowShowRenderer, check_calls_return_at_once, "show() blocks"),
    Broken(MainThreadOnlyRenderer, check_methods_work_from_any_thread, "show() only works on main"),
    Broken(CrashesOnNoBboxRenderer, check_every_method_accepts_awkward_input, "bbox=None crash"),
    Broken(
        CrashesOnNoFrameRenderer, check_every_method_accepts_awkward_input, "frame_meta=None crash"
    ),
    Broken(
        CrashesOnFailedLineRenderer,
        check_every_method_accepts_awkward_input,
        "failed line (empty text) crashes",
    ),
    Broken(CrashesOnBannerRenderer, check_every_method_accepts_awkward_input, "empty banner crash"),
    Broken(
        CrashesBeforeOpenRenderer,
        check_methods_before_open_and_after_close_are_safe,
        "show() before open() raises AttributeError",
    ),
    Broken(
        CrashesAfterCloseRenderer,
        check_methods_before_open_and_after_close_are_safe,
        "clear() after close() raises",
    ),
    Broken(CloseTwiceRenderer, check_close_is_always_safe, "close() twice raises"),
    Broken(SlowHealthRenderer, check_health_is_instant_in_every_state, "health() blocks"),
    Broken(
        CapturableWindowRenderer,
        check_every_window_is_excluded_from_screen_capture,
        "window without WDA_EXCLUDEFROMCAPTURE",
    ),
    Broken(
        SecondWindowCapturableRenderer,
        check_every_window_is_excluded_from_screen_capture,
        "second window forgotten",
    ),
    Broken(
        NoWindowRenderer,
        check_every_window_is_excluded_from_screen_capture,
        "no window found, so nothing proves the rule (probe would be blind)",
    ),
)

BROKEN = (*BROKEN, *identity_breakers(FakeRenderer), *hostile_settings_breakers(FakeRenderer))
