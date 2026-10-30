"""Capture contract checks, plus a conforming fake and deliberately broken ones."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

import numpy as np

from gloss.core.contracts import (
    AdapterContext,
    AdapterUnavailable,
    CaptureTarget,
    Frame,
    Health,
    MonitorInfo,
    Rect,
)
from testkit.core import (
    COMMON_CHECKS,
    Broken,
    Subject,
    assert_health,
    call,
    check_close_is_always_safe,
    check_health_is_instant_in_every_state,
    expect_error,
    hostile_settings_breakers,
    identity_breakers,
    registered_subject,
)

PORT = "capture"

# ---- checks ------------------------------------------------------------------------------


def grab_frame(adapter: Any, tries: int = 40) -> Frame:
    """Grab until a frame arrives. A push-based adapter may need a moment for its first."""
    for _ in range(tries):
        frame = call(adapter.grab)
        if frame is not None:
            assert isinstance(frame, Frame), f"grab() returned {type(frame).__name__}"
            return frame
        time.sleep(0.05)
    raise AssertionError(f"grab() returned None {tries} times in a row after a successful open")


def primary_of(monitors: Sequence[MonitorInfo]) -> MonitorInfo:
    return next(m for m in monitors if m.is_primary)


def open_and_grab(adapter: Any, target: CaptureTarget) -> Frame:
    try:
        call(adapter.open, target)
    except AdapterUnavailable as exc:
        raise AssertionError(f"open({target}) for a connected monitor failed: {exc}") from exc
    return grab_frame(adapter)


def check_list_monitors_works_before_open(subject: Subject) -> None:
    adapter = subject.new()
    monitors = call(adapter.list_monitors)
    assert isinstance(monitors, Sequence) and not isinstance(monitors, str), (
        "list_monitors() must return a sequence"
    )
    assert len(monitors) >= 1, "a desktop has at least one monitor"
    ids = [m.monitor_id for m in monitors]
    for m in monitors:
        assert isinstance(m, MonitorInfo), f"got {type(m).__name__} instead of MonitorInfo"
        assert isinstance(m.monitor_id, str) and m.monitor_id, "monitor_id must be a string"
        assert isinstance(m.bounds, Rect) and m.bounds.area > 0, "bounds must be a Rect"
        assert m.dpi_scale > 0, "dpi_scale must be positive (1.0 = 100%)"
    assert len(set(ids)) == len(ids), f"monitor IDs must be unique, got {ids}"
    primaries = [m for m in monitors if m.is_primary]
    assert len(primaries) == 1, f"exactly one monitor must be primary, got {len(primaries)}"
    assert (primaries[0].bounds.x, primaries[0].bounds.y) == (0, 0), (
        "the primary monitor sits at (0, 0) of the virtual desktop"
    )
    again = [m.monitor_id for m in call(adapter.list_monitors)]
    assert again == ids, "monitor IDs must be stable between calls"
    assert all(i.startswith("\\\\.\\") for i in ids), (
        f"monitor IDs come from the Windows device name (like \\\\.\\DISPLAY1), got {ids}"
    )


def check_monitor_geometry_matches_the_operating_system(subject: Subject) -> None:
    """Bounds are physical pixels at desktop positions, exactly as Windows reports them."""
    assert subject.os_truth is not None, "this check needs an operating-system probe"
    truth = dict(subject.os_truth())
    adapter = subject.new()
    monitors = {m.monitor_id: m for m in call(adapter.list_monitors)}
    assert set(monitors) == set(truth), (
        f"adapter lists {sorted(monitors)}, Windows has {sorted(truth)}"
    )
    for monitor_id, rect in truth.items():
        assert monitors[monitor_id].bounds == rect, (
            f"{monitor_id}: adapter says {monitors[monitor_id].bounds}, Windows says {rect}. "
            "Bounds must be physical pixels, not DPI-scaled logical pixels."
        )


def check_monitor_frames_cover_the_whole_monitor(subject: Subject) -> None:
    adapter = subject.new()
    monitors = call(adapter.list_monitors)
    primary = primary_of(monitors)
    frame = open_and_grab(adapter, CaptureTarget("monitor"))
    assert frame.region == primary.bounds, (
        f"a monitor target with no ID means the primary monitor: {primary.bounds}, "
        f"got a frame at {frame.region}"
    )
    assert isinstance(frame.source_id, str) and frame.source_id, "source_id must be set"
    assert abs(frame.dpi_scale - primary.dpi_scale) < 0.01, "frame and monitor disagree on DPI"
    age = time.monotonic() - frame.timestamp
    assert -1 < age < 30, "timestamp must come from time.monotonic() and be recent"
    for monitor in monitors:
        frame = open_and_grab(adapter, CaptureTarget("monitor", monitor_id=monitor.monitor_id))
        assert frame.region == monitor.bounds, (
            f"{monitor.monitor_id}: frame region {frame.region} is not the monitor's "
            f"physical bounds {monitor.bounds}"
        )
        call(adapter.close)


def check_region_frames_use_physical_desktop_coordinates(subject: Subject) -> None:
    adapter = subject.new()
    for monitor in call(adapter.list_monitors):
        region = Rect(monitor.bounds.x + 8, monitor.bounds.y + 8, 64, 48)
        frame = open_and_grab(adapter, CaptureTarget("region", region=region))
        assert frame.region == region, (
            f"asked for {region} (physical, desktop coordinates), got {frame.region}. "
            "Monitors left of or above the primary have negative coordinates."
        )
        assert frame.image.shape == (48, 64, 3)
        call(adapter.close)


def check_frames_own_their_pixels_and_are_independent(subject: Subject) -> None:
    adapter = subject.new()
    region = Rect(8, 8, 64, 48)
    first = open_and_grab(adapter, CaptureTarget("region", region=region))
    assert first.image.dtype == np.uint8 and first.image.shape == (48, 64, 3)
    assert first.image.flags.owndata, (
        "Frame.image must own its memory, not be a view into the capture library's buffer"
    )
    kept = first.image.copy()
    for _ in range(3):
        later = grab_frame(adapter)
        assert not np.shares_memory(first.image, later.image), (
            "two frames share memory, so a later grab changes an earlier frame"
        )
    assert np.array_equal(first.image, kept), "a frame changed after grab() returned"


def check_fresh_means_changed_or_unknown(subject: Subject) -> None:
    adapter = subject.new()
    target = CaptureTarget("region", region=Rect(8, 8, 64, 48))
    previous = open_and_grab(adapter, target)
    assert previous.fresh is True, "the first frame after open() must have fresh=True"
    for _ in range(8):
        frame = grab_frame(adapter)
        assert isinstance(frame.fresh, bool), "Frame.fresh must be a real bool"
        if not frame.fresh:
            assert np.array_equal(previous.image, frame.image), (
                "fresh=False promises the pixels repeat the previous frame, but they changed"
            )
        assert frame.timestamp >= previous.timestamp, "timestamps must not go backwards"
        previous = frame
    call(adapter.close)
    again = open_and_grab(adapter, target)
    assert again.fresh is True, "the first frame after reopening must have fresh=True"
    call(adapter.close)


def check_targets_that_cannot_be_captured_are_unavailable(subject: Subject) -> None:
    adapter = subject.new()
    monitors = call(adapter.list_monitors)
    bad_targets = [
        CaptureTarget("monitor", monitor_id="\\\\.\\DISPLAY_NO_SUCH_MONITOR"),
        CaptureTarget("window", window_title="Gloss test: no window has this title 7f3a9"),
        CaptureTarget("window", hwnd=1),
        CaptureTarget("region", region=Rect(10**6, 10**6, 64, 48)),
    ]
    for target in bad_targets:
        expect_error(adapter.open, AdapterUnavailable, target)
    # A refused open leaves the adapter usable.
    frame = open_and_grab(adapter, CaptureTarget("monitor", monitor_id=monitors[0].monitor_id))
    assert frame.region == monitors[0].bounds
    call(adapter.close)


def check_target_can_change_after_close(subject: Subject) -> None:
    adapter = subject.new()
    first = open_and_grab(adapter, CaptureTarget("monitor"))
    call(adapter.close)
    region = Rect(first.region.x + 16, first.region.y + 16, 32, 24)
    second = open_and_grab(adapter, CaptureTarget("region", region=region))
    assert second.region == region
    call(adapter.close)


def check_grab_without_an_open_target_is_safe(subject: Subject) -> None:
    adapter = subject.new()
    assert call(adapter.grab) is None, "grab() before open() must return None, not a frame"
    open_and_grab(adapter, CaptureTarget("monitor"))
    call(adapter.close)
    assert call(adapter.grab) is None, "grab() after close() must return None, not a frame"


def check_health_tracks_a_running_capture(subject: Subject) -> None:
    adapter = subject.new()
    open_and_grab(adapter, CaptureTarget("monitor"))
    state = assert_health(adapter, "while capturing").state
    assert state in ("ok", "degraded"), f"health() says {state!r} while frames are arriving"
    call(adapter.close)


CHECKS = (
    *COMMON_CHECKS,
    check_list_monitors_works_before_open,
    check_monitor_frames_cover_the_whole_monitor,
    check_region_frames_use_physical_desktop_coordinates,
    check_frames_own_their_pixels_and_are_independent,
    check_fresh_means_changed_or_unknown,
    check_targets_that_cannot_be_captured_are_unavailable,
    check_target_can_change_after_close,
    check_grab_without_an_open_target_is_safe,
    check_health_tracks_a_running_capture,
    check_monitor_geometry_matches_the_operating_system,
)
# Needs the real operating system, so registered adapters run these on Windows only.
WINDOWS_ONLY = frozenset({check_monitor_geometry_matches_the_operating_system})

# ---- fakes -------------------------------------------------------------------------------

PRIMARY = MonitorInfo("\\\\.\\DISPLAY1", Rect(0, 0, 1280, 720), 1.5, True)
LEFT = MonitorInfo("\\\\.\\DISPLAY2", Rect(-800, 0, 800, 600), 1.0, False)
FAKE_MONITORS = (PRIMARY, LEFT)
FAKE_OS_TRUTH = tuple((m.monitor_id, m.bounds) for m in FAKE_MONITORS)


class FakeCapture:
    """Follows the capture contract. Alternates a changed frame with a repeated one."""

    name = "fake_good"
    REQUIRES_CONTRACT = ">=1.0,<2.0"

    def __init__(self, context: AdapterContext) -> None:
        self._target: CaptureTarget | None = None
        self._region = Rect(0, 0, 0, 0)
        self._dpi = 1.0
        self._n = 0
        self._last: np.ndarray | None = None

    def list_monitors(self) -> Sequence[MonitorInfo]:
        return FAKE_MONITORS

    def _resolve(self, target: CaptureTarget) -> tuple[Rect, float]:
        if target.mode == "monitor":
            for m in self.list_monitors():
                wanted = (
                    m.is_primary
                    if target.monitor_id is None
                    else (m.monitor_id == target.monitor_id)
                )
                if wanted:
                    return m.bounds, m.dpi_scale
        elif target.mode == "region" and target.region is not None:
            r = target.region
            for m in self.list_monitors():
                if m.bounds.x <= r.x and m.bounds.y <= r.y and r.right <= m.bounds.right:
                    if r.bottom <= m.bounds.bottom:
                        return r, m.dpi_scale
        raise AdapterUnavailable("that target is not on any monitor")

    def open(self, target: CaptureTarget) -> None:
        self._region, self._dpi = self._resolve(target)
        self._target, self._n, self._last = target, 0, None

    def grab(self) -> Frame | None:
        if self._target is None:
            return None
        self._n += 1
        repeat = self._last is not None and self._n % 2 == 0
        if not repeat:
            r = self._region
            self._last = np.full((r.h, r.w, 3), self._n % 251, dtype=np.uint8)
        assert self._last is not None
        return Frame(
            self._last.copy(), "fake", self._region, time.monotonic(), self._dpi, not repeat
        )

    def close(self) -> None:
        self._target = None

    def health(self) -> Health:
        return Health("ok" if self._target is not None else "down", "")


class LogicalBoundsCapture(FakeCapture):
    name = "fake_logical_bounds"

    def list_monitors(self) -> Sequence[MonitorInfo]:
        return tuple(
            MonitorInfo(
                m.monitor_id,
                Rect(
                    m.bounds.x,
                    m.bounds.y,
                    int(m.bounds.w / m.dpi_scale),
                    int(m.bounds.h / m.dpi_scale),
                ),
                m.dpi_scale,
                m.is_primary,
            )
            for m in FAKE_MONITORS
        )


class ScaledRegionCapture(FakeCapture):
    name = "fake_scaled_region"

    def _resolve(self, target: CaptureTarget) -> tuple[Rect, float]:
        region, dpi = super()._resolve(target)
        if target.mode == "region":
            region = Rect(int(region.x / dpi), int(region.y / dpi), region.w, region.h)
        return region, dpi


class SharedBufferCapture(FakeCapture):
    name = "fake_shared_buffer"

    def grab(self) -> Frame | None:
        if self._target is None:
            return None
        r = self._region
        if self._last is None or self._last.shape != (r.h, r.w, 3):
            self._last = np.zeros((r.h, r.w, 3), dtype=np.uint8)
        self._last[:] = (self._last + 1) % 251  # reuses one buffer for every frame
        return Frame(self._last, "fake", r, time.monotonic(), self._dpi, True)


class BufferViewCapture(FakeCapture):
    name = "fake_buffer_view"

    def grab(self) -> Frame | None:
        if self._target is None:
            return None
        r = self._region
        view = np.frombuffer(bytearray(r.w * r.h * 3), dtype=np.uint8).reshape(r.h, r.w, 3)
        return Frame(view, "fake", r, time.monotonic(), self._dpi, True)


class FirstFrameStaleCapture(FakeCapture):
    name = "fake_first_stale"

    def grab(self) -> Frame | None:
        frame = super().grab()
        if frame is not None and self._n == 1:
            return Frame(frame.image, "fake", frame.region, frame.timestamp, frame.dpi_scale, False)
        return frame


class LyingFreshCapture(FakeCapture):
    name = "fake_lying_fresh"

    def grab(self) -> Frame | None:
        frame = super().grab()
        if frame is None:
            return None
        pixels = frame.image.copy()
        pixels[0, 0, 0] = (self._n * 7) % 251  # always changes, yet claims no change
        return Frame(pixels, "fake", frame.region, frame.timestamp, frame.dpi_scale, self._n == 1)


class RawErrorCapture(FakeCapture):
    name = "fake_raw_error"

    def _resolve(self, target: CaptureTarget) -> tuple[Rect, float]:
        if target.mode == "monitor" and target.monitor_id:
            raise KeyError(target.monitor_id)
        return super()._resolve(target)


class OpensAnythingCapture(FakeCapture):
    name = "fake_opens_anything"

    def _resolve(self, target: CaptureTarget) -> tuple[Rect, float]:
        try:
            return super()._resolve(target)
        except AdapterUnavailable:
            return PRIMARY.bounds, PRIMARY.dpi_scale


class CloseTwiceCapture(FakeCapture):
    name = "fake_close_twice"

    def close(self) -> None:
        if getattr(self, "_closed", False):
            raise RuntimeError("already closed")
        self._closed = True
        super().close()


class SlowHealthCapture(FakeCapture):
    name = "fake_slow_health"

    def health(self) -> Health:
        time.sleep(0.2)
        return super().health()


class BadHealthStateCapture(FakeCapture):
    name = "fake_bad_health"

    def health(self) -> Health:
        return Health("fine")  # type: ignore[arg-type]


class DuplicateIdsCapture(FakeCapture):
    name = "fake_duplicate_ids"

    def list_monitors(self) -> Sequence[MonitorInfo]:
        return (PRIMARY, MonitorInfo(PRIMARY.monitor_id, LEFT.bounds, 1.0, False))


class TwoPrimariesCapture(FakeCapture):
    name = "fake_two_primaries"

    def list_monitors(self) -> Sequence[MonitorInfo]:
        return (PRIMARY, MonitorInfo(LEFT.monitor_id, LEFT.bounds, 1.0, True))


class UnstableIdsCapture(FakeCapture):
    name = "fake_unstable_ids"
    _calls = 0

    def list_monitors(self) -> Sequence[MonitorInfo]:
        type(self)._calls += 1
        return (MonitorInfo(f"\\\\.\\DISPLAY{self._calls}", PRIMARY.bounds, 1.5, True),)


class LibraryIndexIdsCapture(FakeCapture):
    name = "fake_index_ids"

    def list_monitors(self) -> Sequence[MonitorInfo]:
        return (MonitorInfo("1", PRIMARY.bounds, 1.5, True),)


class GrabBeforeOpenCrashCapture(FakeCapture):
    name = "fake_grab_crash"

    def grab(self) -> Frame | None:
        if self._target is None:
            raise AttributeError("no capture session yet")
        return super().grab()


class ReopenBrokenCapture(FakeCapture):
    name = "fake_reopen_broken"
    _opened = False

    def open(self, target: CaptureTarget) -> None:
        if self._opened:
            raise RuntimeError("cannot open twice")
        self._opened = True
        super().open(target)


class StaleAfterCloseCapture(FakeCapture):
    name = "fake_stale_after_close"

    def close(self) -> None:
        self._keep = self._target  # keeps capturing after close
        super().close()

    def grab(self) -> Frame | None:
        if self._target is None and getattr(self, "_keep", None) is not None:
            self._target = self._keep
        return super().grab()


class BackwardsTimeCapture(FakeCapture):
    name = "fake_backwards_time"

    def grab(self) -> Frame | None:
        frame = super().grab()
        if frame is None:
            return None
        return Frame(
            frame.image,
            "fake",
            frame.region,
            frame.timestamp - self._n,
            frame.dpi_scale,
            frame.fresh,
        )


class DownWhileCapturingCapture(FakeCapture):
    name = "fake_down_while_capturing"

    def health(self) -> Health:
        return Health("down", "")


class WrongDpiCapture(FakeCapture):
    name = "fake_wrong_dpi"

    def grab(self) -> Frame | None:
        frame = super().grab()
        if frame is None:
            return None
        return Frame(frame.image, "fake", frame.region, frame.timestamp, 1.0, frame.fresh)


def fake_subject(adapter: type = FakeCapture, *, via_registry: bool = True) -> Subject:
    return registered_subject(
        PORT,
        adapter.name,
        adapter,
        via_registry=via_registry,
        open_args=(CaptureTarget("monitor"),),
        scripted=True,
        os_truth=lambda: FAKE_OS_TRUTH,
    )


BROKEN = (
    Broken(
        LogicalBoundsCapture,
        check_monitor_geometry_matches_the_operating_system,
        "monitor bounds in DPI-scaled logical pixels",
    ),
    Broken(
        ScaledRegionCapture,
        check_region_frames_use_physical_desktop_coordinates,
        "region converted to logical coordinates",
    ),
    Broken(
        SharedBufferCapture,
        check_frames_own_their_pixels_and_are_independent,
        "every frame reuses one buffer",
    ),
    Broken(
        BufferViewCapture,
        check_frames_own_their_pixels_and_are_independent,
        "frame is a view into a library buffer",
    ),
    Broken(FirstFrameStaleCapture, check_fresh_means_changed_or_unknown, "first frame fresh=False"),
    Broken(
        LyingFreshCapture, check_fresh_means_changed_or_unknown, "fresh=False but pixels changed"
    ),
    Broken(
        RawErrorCapture,
        check_targets_that_cannot_be_captured_are_unavailable,
        "raises KeyError instead of AdapterUnavailable",
    ),
    Broken(
        OpensAnythingCapture,
        check_targets_that_cannot_be_captured_are_unavailable,
        "silently opens the primary monitor for an unknown one",
    ),
    Broken(CloseTwiceCapture, check_close_is_always_safe, "second close() raises"),
    Broken(SlowHealthCapture, check_health_is_instant_in_every_state, "health() blocks"),
    Broken(BadHealthStateCapture, check_health_is_instant_in_every_state, "invalid health state"),
    Broken(DuplicateIdsCapture, check_list_monitors_works_before_open, "two monitors with one ID"),
    Broken(TwoPrimariesCapture, check_list_monitors_works_before_open, "two primary monitors"),
    Broken(UnstableIdsCapture, check_list_monitors_works_before_open, "IDs change between calls"),
    Broken(
        LibraryIndexIdsCapture,
        check_list_monitors_works_before_open,
        "IDs are library indexes, not Windows device names",
    ),
    Broken(
        GrabBeforeOpenCrashCapture,
        check_grab_without_an_open_target_is_safe,
        "grab() before open() raises AttributeError",
    ),
    Broken(ReopenBrokenCapture, check_target_can_change_after_close, "cannot reopen"),
    Broken(
        StaleAfterCloseCapture,
        check_grab_without_an_open_target_is_safe,
        "still returns frames after close()",
    ),
    Broken(BackwardsTimeCapture, check_fresh_means_changed_or_unknown, "timestamps go backwards"),
    Broken(
        WrongDpiCapture,
        check_monitor_frames_cover_the_whole_monitor,
        "frame DPI differs from monitor DPI",
    ),
)

BROKEN = (
    *BROKEN,
    Broken(
        DownWhileCapturingCapture,
        check_health_tracks_a_running_capture,
        "health() says down while frames are arriving",
    ),
)
BROKEN = (*BROKEN, *identity_breakers(FakeCapture), *hostile_settings_breakers(FakeCapture))
