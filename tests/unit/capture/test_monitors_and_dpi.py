"""Monitor IDs and process DPI, checked against Windows itself. Windows only."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest
from testkit import win32

from gloss.core.contracts import AdapterUnavailable, Rect
from gloss.services import monitors as m

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")


def test_monitor_ids_are_windows_device_names_with_physical_bounds():
    found = m.list_monitors()
    truth = dict(win32.os_monitors())
    assert {x.monitor_id: x.bounds for x in found} == truth
    assert all(x.monitor_id.startswith("\\\\.\\") for x in found)
    assert sum(x.is_primary for x in found) == 1
    assert all(x.dpi_scale >= 1.0 for x in found)


def test_ids_are_stable_between_calls():
    assert [x.monitor_id for x in m.list_monitors()] == [x.monitor_id for x in m.list_monitors()]


def test_windows_capture_order_names_the_same_monitors():
    assert sorted(m.windows_capture_order()) == sorted(x.monitor_id for x in m.list_monitors())


def test_find_monitor_by_id_and_default_primary():
    found = m.list_monitors()
    primary = next(x for x in found if x.is_primary)
    assert m.find_monitor(found, None) == primary
    assert m.find_monitor(found, primary.monitor_id) == primary
    with pytest.raises(AdapterUnavailable):
        m.find_monitor(found, "\\\\.\\DISPLAY_NO_SUCH_MONITOR")


def test_rectangle_helpers():
    a, b = Rect(0, 0, 100, 100), Rect(50, 50, 100, 100)
    assert m.intersection(a, b) == Rect(50, 50, 50, 50)
    assert m.intersection(a, Rect(100, 0, 10, 10)) is None  # touching edges share no pixel
    found = m.list_monitors()
    desktop = m.virtual_desktop(found)
    assert all(m.intersection(desktop, x.bounds) == x.bounds for x in found)


def test_per_monitor_v2_is_set_before_mss_and_stays_set():
    """In a fresh process, so earlier tests cannot have chosen the awareness first."""
    code = textwrap.dedent(
        """
        from gloss.services.dpi import ensure_per_monitor_v2, current_awareness
        assert current_awareness() == "unaware", current_awareness()
        assert ensure_per_monitor_v2() == "per_monitor_v2"
        import mss
        with mss.MSS() as sct:
            sct.grab({"left": 0, "top": 0, "width": 8, "height": 8})
        assert current_awareness() == "per_monitor_v2", current_awareness()
        assert ensure_per_monitor_v2() == "per_monitor_v2"  # safe to repeat
        print("ok")
        """
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0 and "ok" in done.stdout, done.stderr


def test_physical_pixels_restores_the_thread():
    from gloss.services.dpi import current_awareness, physical_pixels

    before = current_awareness()
    with physical_pixels():
        assert current_awareness() == "per_monitor_v2"
    assert current_awareness() == before
