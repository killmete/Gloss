"""Capture adapters against a real screen: window follow, monitor mapping, cached frames.

These draw a small magenta window and look for it in what each backend returns, so they prove
the pixels and positions are right, not only that frames arrive. Windows only.
"""

from __future__ import annotations

import sys
import time

import pytest
from capture_window_helper import ColorWindow, pixel_fraction

from gloss.core.contracts import AdapterContext, AdapterUnavailable, CaptureTarget, Rect
from gloss.core.registry import default_registry, discover

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")

BACKENDS = ["mss", "wgc"]


def make(name: str, **settings):
    discover()
    return default_registry.create("capture", name, AdapterContext(settings))


def grab_until(adapter, predicate=lambda f: True, seconds=4.0):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        frame = adapter.grab()
        if frame is not None:
            last = frame
            if predicate(frame):
                return frame
        time.sleep(0.05)
    return last


def monitors_of(name):
    return list(make(name).list_monitors())


@pytest.fixture(params=BACKENDS)
def backend(request):
    adapter = make(request.param)
    yield adapter
    adapter.close()


def test_both_backends_are_registered_under_short_names():
    discover()
    assert {"mss", "wgc"} <= set(default_registry.names("capture"))


@pytest.mark.parametrize("name", BACKENDS)
def test_each_monitor_shows_its_own_window_and_not_the_others(name):
    """Proves the monitor ID to library index mapping, which differs between libraries."""
    monitors = monitors_of(name)
    for target_monitor in monitors:
        b = target_monitor.bounds
        with ColorWindow(b.x + 100, b.y + 100, 300, 200):
            for monitor in monitors:
                adapter = make(name)
                adapter.open(CaptureTarget("monitor", monitor_id=monitor.monitor_id))
                try:
                    frame = grab_until(adapter, lambda f: f.fresh)
                    assert frame is not None
                    assert frame.region == monitor.bounds
                    local = frame.image[100:300, 100:400]
                    seen = pixel_fraction(local) > 0.9
                    assert seen == (monitor is target_monitor), (
                        f"{name}: window is on {target_monitor.monitor_id}; "
                        f"capture of {monitor.monitor_id} {'has' if seen else 'lacks'} it"
                    )
                finally:
                    adapter.close()


@pytest.mark.parametrize("name", BACKENDS)
def test_region_mode_returns_the_window_pixels_at_physical_coordinates(name):
    primary = next(x for x in monitors_of(name) if x.is_primary)
    b = primary.bounds
    with ColorWindow(b.x + 200, b.y + 150, 240, 120):
        adapter = make(name)
        region = Rect(b.x + 210, b.y + 160, 200, 80)
        adapter.open(CaptureTarget("region", region=region))
        try:
            frame = grab_until(adapter)
            assert frame.region == region
            assert pixel_fraction(frame.image) > 0.95
        finally:
            adapter.close()


def test_window_follow_tracks_moves_minimise_and_close(backend):
    primary = next(x for x in backend.list_monitors() if x.is_primary)
    b = primary.bounds
    window = ColorWindow(b.x + 120, b.y + 120, 320, 200, overlapped=True)
    with window:
        backend.open(CaptureTarget("window", window_title=window.title))
        frame = grab_until(backend, lambda f: pixel_fraction(f.image) > 0.5)
        assert frame is not None and pixel_fraction(frame.image) > 0.5
        assert abs(frame.region.x - (b.x + 120)) <= 16 and abs(frame.region.y - (b.y + 120)) <= 16
        assert backend.health().state in ("ok", "degraded")

        window.move(b.x + 400, b.y + 300)
        moved = grab_until(
            backend, lambda f: abs(f.region.x - (b.x + 400)) <= 16 and f.fresh, seconds=5
        )
        assert moved is not None and abs(moved.region.x - (b.x + 400)) <= 16
        assert abs(moved.region.y - (b.y + 300)) <= 16
        assert pixel_fraction(moved.image) > 0.5

        window.minimize()
        assert backend.grab() is None
        assert backend.health().state == "degraded"
        assert "minimised" in backend.health().detail

    time.sleep(0.3)
    assert backend.grab() is None  # window closed: no frame, no exception
    assert backend.health().state == "down"
    backend.close()


def test_window_can_be_found_by_handle_and_missing_ones_are_unavailable(backend):
    primary = next(x for x in backend.list_monitors() if x.is_primary)
    with ColorWindow(primary.bounds.x + 80, primary.bounds.y + 80, 200, 100) as window:
        backend.open(CaptureTarget("window", hwnd=window.hwnd))
        assert grab_until(backend) is not None
        backend.close()
        with pytest.raises(AdapterUnavailable):
            backend.open(CaptureTarget("window", window_title="Gloss test: nothing has this 91c"))
        with pytest.raises(AdapterUnavailable):
            backend.open(CaptureTarget("window", hwnd=1))


def test_wgc_hands_out_the_cached_frame_when_nothing_changed():
    primary = next(x for x in monitors_of("wgc") if x.is_primary)
    b = primary.bounds
    window = ColorWindow(b.x + 100, b.y + 100, 260, 160, overlapped=True)
    with window:
        adapter = make("wgc")
        adapter.open(CaptureTarget("window", window_title=window.title))
        try:
            first = grab_until(adapter, lambda f: f.fresh)
            assert first is not None and first.fresh
            time.sleep(0.4)  # a static window delivers nothing more
            seen = [adapter.grab() for _ in range(3)]
            repeated = [f for f in seen if f is not None and not f.fresh]
            assert repeated, "a static window should repeat its last frame with fresh=False"
            for frame in repeated:
                assert frame.image.shape == first.image.shape
                assert (frame.image == first.image).all()
                assert frame.image is not first.image
                assert frame.timestamp >= first.timestamp
        finally:
            adapter.close()


def test_mss_marks_a_static_screen_as_not_fresh_and_a_change_as_fresh():
    primary = next(x for x in monitors_of("mss") if x.is_primary)
    b = primary.bounds
    with ColorWindow(b.x + 100, b.y + 100, 200, 100) as window:
        adapter = make("mss")
        # fully inside our own opaque topmost window, so nothing else can repaint it
        adapter.open(CaptureTarget("region", region=Rect(b.x + 110, b.y + 110, 180, 80)))
        try:
            first = adapter.grab()
            assert first.fresh
            if pixel_fraction(first.image) < 0.9:
                pytest.skip("control grab did not show the test window; screen locked?")
            second = adapter.grab()
            assert not second.fresh and (second.image == first.image).all()
            assert second.image is not first.image
            window.recolor((0, 200, 0))
            changed = adapter.grab()
            assert changed.fresh and not (changed.image == first.image).all()
        finally:
            adapter.close()


@pytest.mark.parametrize("name", BACKENDS)
def test_bad_settings_are_a_gloss_error_not_a_crash(name):
    from gloss.core.contracts import GlossError

    for bad in ({"fps_max": -5}, {"fps_max": "fast"}, {"first_frame_timeout_ms": -1}):
        with pytest.raises(GlossError):
            make(name, **bad)
    make(name, fps_max=10, unknown_key=1)  # unknown keys are ignored


def test_wgc_refuses_a_region_across_monitors():
    monitors = monitors_of("wgc")
    if len(monitors) < 2:
        pytest.skip("needs two monitors")
    left, right = sorted(monitors, key=lambda x: x.bounds.x)[:2]
    span = Rect(left.bounds.right - 50, left.bounds.y + 10, 100, 50)
    if span.right > right.bounds.x + right.bounds.w or left.bounds.right != right.bounds.x:
        pytest.skip("the monitors do not touch side by side")
    adapter = make("wgc")
    with pytest.raises(AdapterUnavailable):
        adapter.open(CaptureTarget("region", region=span))
    mss_adapter = make("mss")
    mss_adapter.open(CaptureTarget("region", region=span))  # mss can do it
    try:
        assert grab_until(mss_adapter).region == span
    finally:
        mss_adapter.close()
