"""Regression: an area chosen on the second monitor must capture, not fail with
'that monitor is not connected' (the selector's Qt screen name is not a Windows device name)."""

from __future__ import annotations

import pytest
from testkit.pipeline import base_config

from gloss import app
from gloss.core.contracts import CaptureTarget, MonitorInfo, Rect
from gloss.services.capture_common import resolve_screen_target

D1 = "\\\\.\\DISPLAY1"
D2 = "\\\\.\\DISPLAY2"
RIGHT = MonitorInfo(D1, Rect(0, 0, 1920, 1080), 1.0, True)
LEFT = MonitorInfo(D2, Rect(-1920, 0, 1920, 1080), 1.0)
MONITORS = [RIGHT, LEFT]


@pytest.fixture(autouse=True)
def two_monitors(monkeypatch):
    monkeypatch.setattr("gloss.services.monitors.list_monitors", lambda: list(MONITORS))


def test_area_on_the_left_monitor_gets_that_monitors_windows_id():
    target = app._region_target(Rect(-1500, 100, 400, 300))
    assert target.mode == "region" and target.region == Rect(-1500, 100, 400, 300)
    assert target.monitor_id == D2


def test_area_on_the_primary_monitor_gets_the_primary_id():
    assert app._region_target(Rect(100, 100, 400, 300)).monitor_id == D1


def test_the_target_resolves_in_a_capture_adapter_without_error():
    target = app._region_target(Rect(-1500, 100, 400, 300))
    resolved = resolve_screen_target(target, MONITORS)
    assert resolved.monitor.monitor_id == D2


def test_a_qt_model_name_cannot_leak_into_the_target():
    # on_region ignores the selector's id, and _region_target takes only the rect.
    target = app._region_target(Rect(-1500, 100, 400, 300))
    assert target.monitor_id != "GS24F14"
    assert target.monitor_id in {m.monitor_id for m in MONITORS}


def test_a_failure_to_list_monitors_falls_back_to_an_untagged_region(monkeypatch):
    def boom():
        raise OSError("no")

    monkeypatch.setattr("gloss.services.monitors.list_monitors", boom)
    target = app._region_target(Rect(0, 0, 10, 10))
    assert target == CaptureTarget("region", region=Rect(0, 0, 10, 10))


def _config_with_region(region):
    c = base_config()
    return c.model_copy(update={"capture": c.capture.model_copy(update={"region": region})})


def test_saved_region_on_the_second_monitor_is_restored_there():
    target = app._saved_region(_config_with_region([-1500, 100, 400, 300]))
    assert target.monitor_id == D2
    assert target.region == Rect(-1500, 100, 400, 300)
    assert resolve_screen_target(target, MONITORS).monitor.monitor_id == D2


def test_saved_region_on_a_monitor_that_is_gone_is_dropped(monkeypatch):
    monkeypatch.setattr("gloss.services.monitors.list_monitors", lambda: [RIGHT])
    assert app._saved_region(_config_with_region([-1500, 100, 400, 300])) is None


def test_no_saved_region_means_no_target():
    assert app._saved_region(_config_with_region([])) is None
