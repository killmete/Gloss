"""The overlay: physical-to-logical maths, content rules, click-through and re-asserting topmost."""

from __future__ import annotations

import sys

import pytest
from ui_qt_helpers import flush_deletes, get_app, item, pump

from gloss.adapters.render.overlay import (
    OverlayRenderer,
    ScreenGeometry,
    physical_to_logical,
    pick_screen,
)
from gloss.adapters.render.overlay import _qt as overlay_qt
from gloss.core.contracts import AdapterContext, Health, Rect

# A 100% monitor to the left of the primary, the primary at 150%, and one at 200% below it.
LEFT = ScreenGeometry(-1920, 0, 1920, 1080, 1.0)
PRIMARY = ScreenGeometry(0, 0, 2880, 1620, 1.5)
BELOW = ScreenGeometry(0, 1620, 3840, 2160, 2.0)
SCREENS = [LEFT, PRIMARY, BELOW]


def test_a_point_on_a_100_percent_monitor_maps_one_to_one():
    assert physical_to_logical(Rect(-1800, 40, 300, 20), LEFT) == (120.0, 40.0, 300.0, 20.0)


def test_a_point_on_a_150_percent_monitor_is_scaled_down():
    assert physical_to_logical(Rect(300, 150, 300, 90), PRIMARY) == (200.0, 100.0, 200.0, 60.0)


def test_a_point_on_a_200_percent_monitor_is_relative_to_its_own_origin():
    assert physical_to_logical(Rect(400, 1820, 200, 100), BELOW) == (200.0, 100.0, 100.0, 50.0)


def test_each_box_goes_to_the_monitor_that_holds_its_centre():
    assert pick_screen(Rect(-1800, 10, 100, 20), SCREENS) == 0
    assert pick_screen(Rect(10, 10, 100, 20), SCREENS) == 1
    assert pick_screen(Rect(100, 2000, 100, 20), SCREENS) == 2


def test_a_box_straddling_two_monitors_goes_with_its_centre():
    assert pick_screen(Rect(-60, 100, 100, 20), SCREENS) == 0  # centre at x=-10
    assert pick_screen(Rect(-40, 100, 100, 20), SCREENS) == 1  # centre at x=10


def test_a_box_off_every_monitor_goes_to_the_nearest():
    assert pick_screen(Rect(-5000, 0, 10, 10), SCREENS) == 0
    assert pick_screen(Rect(0, 0, 1, 1), []) is None


@pytest.fixture
def overlay():
    get_app()
    renderer = OverlayRenderer(AdapterContext({"font_size": 14}))
    renderer.open()
    renderer.set_visible(True)
    yield renderer
    renderer.close()
    flush_deletes()


def chips(renderer):
    return [(c.text, c.low_quality) for w in renderer.windows for c in w.chips]


def primary_window(renderer):
    """The window over the monitor whose physical origin is (0, 0), which is always primary."""
    return next(w for w in renderer.windows if (w.physical().x, w.physical().y) == (0, 0))


def test_draws_english_for_lines_that_have_a_position(overlay):
    overlay.show([item("Hello", bbox=Rect(10, 10, 100, 20))], None)
    assert chips(overlay) == [("Hello", False)]
    assert any(w.isVisible() for w in overlay.windows)


def test_a_chip_sits_at_the_converted_position(overlay):
    window = primary_window(overlay)
    scale = window.physical().dpr
    overlay.show([item("Hi", bbox=Rect(200, 120, 80, 20))], None)
    (chip,) = window.chips
    assert chip.rect.x() == pytest.approx(200 / scale)
    assert chip.rect.y() == pytest.approx(120 / scale)


def test_lines_without_a_position_or_text_are_left_out(overlay):
    overlay.show(
        [
            item("no box", line_id="a", bbox=None),
            item("", line_id="b", status="failed", bbox=Rect(0, 0, 50, 10)),
            item("", line_id="c", status="passthrough", bbox=Rect(0, 0, 50, 10)),
            item("   ", line_id="d", bbox=Rect(0, 0, 50, 10)),
        ],
        None,
    )
    assert chips(overlay) == []
    assert not any(w.isVisible() for w in overlay.windows)


def test_low_quality_lines_are_marked(overlay):
    overlay.show([item("Rough", bbox=Rect(10, 10, 100, 20), low_quality=True)], None)
    assert chips(overlay) == [("Rough", True)]


def test_clear_removes_everything_and_hides_the_windows(overlay):
    overlay.show([item("Hello", bbox=Rect(10, 10, 100, 20))], None)
    overlay.clear()
    assert chips(overlay) == []
    assert not any(w.isVisible() for w in overlay.windows)


def test_remove_takes_off_only_the_named_lines(overlay):
    overlay.show(
        [
            item("A", line_id="a", bbox=Rect(10, 10, 100, 20)),
            item("B", line_id="b", bbox=Rect(10, 200, 100, 20)),
        ],
        None,
    )
    overlay.remove(["a"])
    assert chips(overlay) == [("B", False)]


def test_a_new_result_for_the_same_line_replaces_the_old(overlay):
    overlay.show([item("old", line_id="a", bbox=Rect(10, 10, 100, 20))], None)
    overlay.show([item("new", line_id="a", bbox=Rect(10, 10, 100, 20))], None)
    assert chips(overlay) == [("new", False)]


def test_chips_that_would_overlap_are_pushed_apart(overlay):
    overlay.show(
        [
            item("first", line_id="a", bbox=Rect(10, 10, 100, 20)),
            item("second", line_id="b", bbox=Rect(10, 12, 100, 20)),
        ],
        None,
    )
    rects = [c.rect for w in overlay.windows for c in w.chips]
    assert len(rects) == 2
    assert not rects[0].intersects(rects[1])


def test_identical_content_is_not_laid_out_again(overlay):
    box = Rect(10, 10, 100, 20)
    overlay.show([item("Same", bbox=box)], None)
    window = next(w for w in overlay.windows if w.chips)
    before = window.chips
    overlay.show([item("Same", bbox=box)], None)
    assert window.chips is before  # nothing changed, so nothing was redrawn (no flicker)


def test_the_banner_shows_even_with_no_lines(overlay):
    overlay.set_status(Health("degraded", "x"), "Low-quality OCR: text may be wrong or missing")
    banners = [w for w in overlay.windows if w.banner_rect is not None]
    assert len(banners) == 1
    assert banners[0].isVisible()
    overlay.set_status(Health("ok"), "")
    assert not any(w.isVisible() for w in overlay.windows)


def test_lines_expire_when_a_linger_time_is_set():
    get_app()
    renderer = OverlayRenderer(AdapterContext({"linger_s": 0.05}))
    renderer.open()
    renderer.set_visible(True)
    try:
        renderer.show([item("Brief", bbox=Rect(10, 10, 100, 20))], None)
        assert chips(renderer)
        pump(0.1)
        renderer._ctl.tick()
        assert chips(renderer) == []
    finally:
        renderer.close()


def test_topmost_timer_uses_the_configured_interval():
    get_app()
    renderer = OverlayRenderer(AdapterContext({"topmost_reassert_ms": 1234}))
    renderer.open()
    try:
        assert renderer._ctl.timer.interval() == 1234
        assert renderer._ctl.timer.isActive()
    finally:
        renderer.close()


def test_topmost_is_reasserted_on_each_tick(overlay, monkeypatch):
    overlay.show([item("Hello", bbox=Rect(10, 10, 100, 20))], None)
    raised: list[int] = []
    monkeypatch.setattr(overlay_qt, "_raise_hwnd", lambda hwnd: raised.append(hwnd))
    overlay._ctl.tick()
    if sys.platform == "win32":
        assert raised, "a visible overlay window must be raised again on every tick"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows window styles")
def test_windows_are_layered_click_through_and_never_take_focus(overlay):
    import ctypes

    overlay.show([item("Hello", bbox=Rect(10, 10, 100, 20))], None)
    pump(0.1)
    user32 = ctypes.WinDLL("user32")
    user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    window = next(w for w in overlay.windows if w.isVisible())
    style = user32.GetWindowLongPtrW(ctypes.c_void_p(int(window.winId())), -20)
    wanted = overlay_qt.WS_EX_LAYERED | overlay_qt.WS_EX_TRANSPARENT
    wanted |= overlay_qt.WS_EX_NOACTIVATE
    assert style & wanted == wanted


def test_health_reports_open_state(overlay):
    assert overlay.health().state in ("ok", "degraded")
    overlay.close()
    assert overlay.health().state == "down"
