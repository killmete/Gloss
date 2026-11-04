"""A window flagged WDA_EXCLUDEFROMCAPTURE must be absent from a real mss grab. Windows only."""

from __future__ import annotations

import sys

import pytest
from capture_window_helper import ColorWindow, pixel_fraction

from gloss.core.contracts import AdapterContext, CaptureTarget, Rect
from gloss.core.registry import default_registry, discover
from gloss.services import capture_exclusion as ce
from gloss.services.monitors import list_monitors

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")


def grab_region(name: str, region: Rect):
    discover()
    adapter = default_registry.create("capture", name, AdapterContext())
    adapter.open(CaptureTarget("region", region=region))
    try:
        frame = None
        for _ in range(20):
            frame = adapter.grab()
            if frame is not None:
                break
        assert frame is not None
        return frame.image
    finally:
        adapter.close()


@pytest.mark.parametrize("backend", ["mss", "wgc"])
def test_flagged_window_is_absent_from_a_real_grab(backend):
    primary = next(m for m in list_monitors() if m.is_primary).bounds
    box = Rect(primary.x + 160, primary.y + 160, 240, 140)
    background = pixel_fraction(grab_region(backend, box))  # magenta-ish pixels already there
    with ColorWindow(box.x, box.y, box.w, box.h) as window:
        before = pixel_fraction(grab_region(backend, box))
        if before < 0.9:
            pytest.skip(f"control grab did not show the test window ({before:.2f}); screen locked?")
        assert ce.display_affinity(window.hwnd) == ce.WDA_NONE

        assert ce.exclude_from_capture(window.hwnd) is True
        assert ce.is_excluded(window.hwnd)
        after = pixel_fraction(grab_region(backend, box))
        assert after <= background + 0.03, (
            f"{backend} still captured the excluded window "
            f"(magenta share {after:.2f}, was {background:.2f} before the window existed)"
        )


def test_exclusion_can_be_repeated_and_bad_handles_are_harmless():
    primary = next(m for m in list_monitors() if m.is_primary).bounds
    with ColorWindow(primary.x + 50, primary.y + 50, 100, 60) as window:
        assert ce.exclude_from_capture(window.hwnd) and ce.exclude_from_capture(window.hwnd)
    assert ce.exclude_from_capture(0) is False
    assert ce.exclude_from_capture(1) is False  # not a window
    assert ce.display_affinity(1) is None
