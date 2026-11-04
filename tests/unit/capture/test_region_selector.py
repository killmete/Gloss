"""Region selector backend: physical-pixel geometry across monitors, no Qt."""

from __future__ import annotations

import pytest

from gloss.core.contracts import MonitorInfo, Rect
from gloss.services import region as r

LEFT = MonitorInfo("\\\\.\\DISPLAY2", Rect(-1920, 0, 1920, 1080), 1.0, False)
MAIN = MonitorInfo("\\\\.\\DISPLAY1", Rect(0, 0, 3840, 2160), 1.5, True)  # 4K at 150 percent
ABOVE = MonitorInfo("\\\\.\\DISPLAY3", Rect(0, -1080, 1920, 1080), 1.0, False)
MONITORS = (LEFT, MAIN, ABOVE)


def test_normalize_rect_accepts_a_drag_in_any_direction():
    assert r.normalize_rect(100, 50, 30, 10) == Rect(30, 10, 70, 40)
    assert r.normalize_rect(5, 5, 5, 5) == Rect(5, 5, 0, 0)


def test_clamp_clips_to_the_virtual_desktop_and_rejects_outside():
    assert r.clamp_to_desktop(Rect(-2000, 100, 300, 50), MONITORS) == Rect(-1920, 100, 220, 50)
    assert r.clamp_to_desktop(Rect(10**6, 10**6, 5, 5), MONITORS) is None


def test_region_across_two_monitors_is_reported_with_both_parts():
    rect = Rect(-100, 200, 300, 100)
    parts = r.monitors_touched(rect, MONITORS)
    assert [(m.monitor_id, p) for m, p in parts] == [
        (LEFT.monitor_id, Rect(-100, 200, 100, 100)),
        (MAIN.monitor_id, Rect(0, 200, 200, 100)),
    ]
    assert r.spans_monitors(rect, MONITORS)
    assert not r.spans_monitors(Rect(10, 10, 50, 50), MONITORS)


def test_negative_coordinates_survive_validation():
    assert r.validate_region(Rect(-500, 20, 100, 40), MONITORS) == Rect(-500, 20, 100, 40)


def test_validate_refuses_tiny_and_off_screen_regions_with_a_readable_message():
    with pytest.raises(r.InvalidRegion, match="at least 16"):
        r.validate_region(Rect(0, 0, 5, 200), MONITORS)
    with pytest.raises(r.InvalidRegion, match="not on any monitor"):
        r.validate_region(Rect(10**6, 0, 100, 100), MONITORS)
    assert r.validate_region(Rect(0, 0, 8, 8), MONITORS, min_size=4) == Rect(0, 0, 8, 8)


def test_region_target_is_tagged_with_the_biggest_monitor():
    target = r.region_target(Rect(-100, 200, 300, 100), MONITORS)
    assert target.mode == "region" and target.monitor_id == MAIN.monitor_id
    assert target.region == Rect(-100, 200, 300, 100)


def test_logical_to_physical_uses_the_monitors_own_scale():
    # Qt shows the 4K monitor at 150 percent as 2560 x 1440 logical pixels.
    screen = Rect(0, 0, 2560, 1440)
    assert r.monitor_scale(MAIN, 2560) == pytest.approx(1.5)
    assert r.logical_to_physical(Rect(100, 40, 200, 80), screen, MAIN) == Rect(150, 60, 300, 120)
    assert r.physical_to_logical(Rect(150, 60, 300, 120), screen, MAIN) == Rect(100, 40, 200, 80)


def test_logical_to_physical_handles_a_screen_left_of_the_primary():
    screen = Rect(-1920, 0, 1920, 1080)  # 100 percent: logical equals physical
    assert r.logical_to_physical(Rect(-1900, 10, 50, 50), screen, LEFT) == Rect(-1900, 10, 50, 50)


def test_fractional_scale_round_trips_within_a_pixel():
    mon = MonitorInfo("\\\\.\\DISPLAY9", Rect(0, 0, 1920, 1080), 1.25, True)
    screen = Rect(0, 0, 1536, 864)
    physical = r.logical_to_physical(Rect(33, 21, 101, 55), screen, mon)
    back = r.physical_to_logical(physical, screen, mon)
    assert abs(back.x - 33) <= 1 and abs(back.w - 101) <= 1


def test_restore_region_drops_a_region_whose_monitor_is_gone():
    assert r.restore_region(Rect(-500, 20, 100, 40), (MAIN,)) is None
    assert r.restore_region(Rect(-500, 20, 100, 40), MONITORS) == Rect(-500, 20, 100, 40)
    # Hanging off the edge is clipped, not dropped; the gap between monitors is not a monitor.
    assert r.restore_region(Rect(3800, 100, 200, 40), (MAIN,)) == Rect(3800, 100, 40, 40)
    assert r.restore_region(Rect(-1500, -500, 100, 100), MONITORS) is None
