"""SPEC acceptance 4: Gloss's own windows never appear in a screen capture.

The first group asks Windows for each window's display affinity. The second group takes real
``mss`` captures and checks the pixels, with a control that proves the test could see the
window if it were not excluded.
"""

from __future__ import annotations

import ctypes
import sys
import time

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget
from ui_qt_helpers import flush_deletes, get_app, item, pump

from gloss.adapters.render.overlay import OverlayRenderer
from gloss.adapters.render.panel import PanelRenderer
from gloss.core.contracts import AdapterContext, Rect
from gloss.ui.native import WDA_EXCLUDEFROMCAPTURE, display_affinity, exclude_widget

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")

SOLID = QColor(18, 171, 52)


def affinity(widget: QWidget) -> int | None:
    return display_affinity(int(widget.winId()))


class Solid(QWidget):
    """A plain window filled with one colour that is unlikely to be on the desktop."""

    def __init__(self, x: int, y: int, w: int, h: int) -> None:
        super().__init__(
            None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setGeometry(x, y, w, h)
        self.setAutoFillBackground(True)
        palette = self.palette()
        palette.setColor(self.backgroundRole(), SOLID)
        self.setPalette(palette)


def set_affinity(widget: QWidget, value: int) -> None:
    user32 = ctypes.WinDLL("user32")
    user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    assert user32.SetWindowDisplayAffinity(ctypes.c_void_p(int(widget.winId())), value)


def grab(left: int, top: int, width: int, height: int) -> np.ndarray:
    import mss

    with mss.MSS() as sct:
        shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
    return np.asarray(shot)[:, :, :3][:, :, ::-1].copy()  # BGRA to RGB


def share_of_solid(image: np.ndarray) -> float:
    target = np.array([SOLID.red(), SOLID.green(), SOLID.blue()])
    return float(np.mean(np.all(np.abs(image.astype(int) - target) <= 3, axis=2)))


def primary_geometry():
    from PySide6.QtGui import QGuiApplication

    screen = QGuiApplication.primaryScreen()
    return screen.geometry(), float(screen.devicePixelRatio())


def quiet_origin(height: int) -> tuple[int, int]:
    """Logical top-left near the bottom-left of the primary monitor. Other programs' test
    windows (and the control windows of other Gloss tests) tend to sit near the top-left, so
    a window here is not covered by them."""
    geometry, _scale = primary_geometry()
    return geometry.x() + 40, geometry.y() + geometry.height() - height - 80


def solid_is_seen(window: QWidget, box: tuple[int, int, int, int]) -> bool:
    """True once a capture shows the window's colour. Raises the window each time, since another
    topmost window may sit on top for a moment."""
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        window.raise_()
        pump(0.15)
        if share_of_solid(grab(*box)) > 0.9:
            return True
    return False


def physical_box(x: int, y: int, w: int, h: int) -> tuple[int, int, int, int]:
    """Logical window rect on the primary monitor to the physical rect mss wants."""
    geometry, scale = primary_geometry()
    return (
        geometry.x() + round(x * scale),
        geometry.y() + round(y * scale),
        round(w * scale),
        round(h * scale),
    )


# ---- what Windows reports -----------------------------------------------------------------


def test_helper_sets_the_exclusion_flag():
    get_app()
    window = Solid(100, 100, 200, 100)
    try:
        window.show()
        assert exclude_widget(window)
        assert affinity(window) == WDA_EXCLUDEFROMCAPTURE
    finally:
        window.close()


def test_the_app_wide_guard_excludes_any_window_when_it_is_shown():
    get_app()
    window = Solid(100, 100, 200, 100)  # an ordinary window nobody excluded by hand
    try:
        window.show()
        pump(0.1)
        assert affinity(window) == WDA_EXCLUDEFROMCAPTURE
    finally:
        window.close()


def test_exclusion_is_applied_again_when_qt_recreates_the_native_window():
    get_app()
    window = Solid(100, 100, 200, 100)
    try:
        window.show()
        pump(0.1)
        old = int(window.winId())
        window.setWindowFlag(Qt.WindowType.Tool, True)  # forces a new native window
        window.show()
        pump(0.2)
        assert affinity(window) == WDA_EXCLUDEFROMCAPTURE
        assert int(window.winId()) != 0 and old != 0
    finally:
        window.close()


def test_panel_window_is_excluded_before_it_is_ever_shown(tmp_path):
    get_app()
    panel = PanelRenderer(AdapterContext({"state_file": str(tmp_path / "s.ini")}))
    panel.open()
    try:
        assert not panel.window.isVisible()
        assert affinity(panel.window) == WDA_EXCLUDEFROMCAPTURE
    finally:
        panel.close()
        flush_deletes()


def test_every_overlay_window_is_excluded_before_it_is_ever_shown():
    get_app()
    overlay = OverlayRenderer(AdapterContext({}))
    overlay.open()
    try:
        assert overlay.windows
        for window in overlay.windows:
            assert affinity(window) == WDA_EXCLUDEFROMCAPTURE
    finally:
        overlay.close()
        flush_deletes()


def test_ui_windows_are_excluded(tmp_path):
    from gloss.core.config import load_config
    from gloss.ui.api_key_dialog import ApiKeyDialog
    from gloss.ui.settings_window import SettingsWindow

    get_app()
    config = load_config(user_path=tmp_path / "none.toml").config

    class Store:
        def get(self, name):
            return None

        def set(self, name, value):
            pass

        def delete(self, name):
            pass

    for window in (
        SettingsWindow(config, user_path=tmp_path / "c.toml"),
        ApiKeyDialog(Store(), "x"),
    ):
        try:
            window.show()
            pump(0.1)
            assert affinity(window) == WDA_EXCLUDEFROMCAPTURE
        finally:
            window.close()


def test_region_selector_windows_are_excluded():
    from gloss.ui.region_selector import RegionSelector

    get_app()
    selector = RegionSelector()
    selector.start()
    try:
        assert selector._windows
        pump(0.1)
        for window in selector._windows:
            assert affinity(window) == WDA_EXCLUDEFROMCAPTURE
    finally:
        selector.cancel()
        pump(0.1)


# ---- real captures ------------------------------------------------------------------------


def capture_works_here() -> bool:
    """A locked or remote session can return black. Then these tests cannot prove anything."""
    try:
        return bool(grab(0, 0, 64, 64).std() >= 0) and grab(0, 0, 64, 64).any()
    except Exception:
        return False


@pytest.fixture
def need_capture():
    get_app()
    if not capture_works_here():
        pytest.skip("this desktop cannot be captured (locked or remote session)")


def test_control_a_window_that_is_not_excluded_is_seen_by_mss(need_capture):
    qx, qy = quiet_origin(160)
    box = physical_box(qx, qy, 240, 160)
    window = Solid(qx, qy, 240, 160)
    try:
        window.show()
        pump(0.5)
        set_affinity(window, 0)  # undo the app-wide guard, to see what a capture would see
        pump(0.5)
        assert solid_is_seen(window, box), "the control window must be visible to mss"
    finally:
        window.close()


def test_an_excluded_window_is_not_in_an_mss_capture(need_capture):
    qx, qy = quiet_origin(160)
    box = physical_box(qx, qy, 240, 160)
    window = Solid(qx, qy, 240, 160)
    try:
        window.show()
        pump(0.5)
        set_affinity(window, 0)
        pump(0.5)
        assert solid_is_seen(window, box)  # visible first, so the next check means something
        exclude_widget(window)
        pump(0.5)
        assert share_of_solid(grab(*box)) < 0.02
    finally:
        window.close()


def changed_share(before: np.ndarray, after: np.ndarray) -> float:
    return float(np.mean(np.any(before != after, axis=2)))


def settles_to(baseline: np.ndarray, box: tuple[int, int, int, int]) -> float:
    """Capture again until the picture matches ``baseline`` or time runs out; return the
    smallest difference seen. DWM needs a few frames to apply a change of display affinity,
    and other programs repaint the desktop behind the window."""
    best = 1.0
    deadline = time.monotonic() + 2.5
    while time.monotonic() < deadline:
        best = min(best, changed_share(baseline, grab(*box)))
        if best < 0.05:
            break
        pump(0.15)
    return best


def excluded_window_is_absent(window: QWidget, box: tuple[int, int, int, int]) -> str | None:
    """Compare captures with the window excluded, not excluded, and excluded again.

    Return None if the excluded captures match each other and the control differs, or a
    sentence saying what went wrong. Other programs repaint the desktop now and then, so the
    comparison is tried up to three times.
    """
    problem = "never ran"
    for _attempt in range(5):
        window.raise_()
        pump(0.3)
        hidden = grab(*box)
        set_affinity(window, 0)  # control: the same window, no longer excluded
        pump(0.6)
        visible = grab(*box)
        set_affinity(window, WDA_EXCLUDEFROMCAPTURE)
        pump(0.4)
        if changed_share(hidden, visible) <= 0.3:
            problem = "the control did not show the window, so this test proves nothing"
            continue
        if settles_to(hidden, box) >= 0.05:
            problem = "the excluded window showed up in the capture"
            continue
        return None
    return problem


def test_the_panel_does_not_appear_in_a_real_capture(need_capture, tmp_path):
    panel = PanelRenderer(AdapterContext({"state_file": str(tmp_path / "s.ini")}))
    panel.open()
    try:
        panel.window.move(*quiet_origin(240))
        panel.window.resize(320, 240)
        panel.show([item("A line of English", line_id="a")], None)
        panel.set_visible(True)
        pump(0.6)
        frame = panel.window.frameGeometry()
        box = physical_box(frame.x(), frame.y(), frame.width(), frame.height())
        problem = excluded_window_is_absent(panel.window, box)
        assert problem is None, problem
    finally:
        panel.close()
        flush_deletes()


def test_the_overlay_does_not_appear_in_a_real_capture(need_capture):
    overlay = OverlayRenderer(AdapterContext({}))
    overlay.open()
    try:
        window = next(w for w in overlay.windows if (w.physical().x, w.physical().y) == (0, 0))
        geometry = window.physical()
        bbox = Rect(geometry.x + 60, geometry.y + geometry.h - 300, 400, 60)
        overlay.show([item("Overlay text that must not be read", bbox=bbox)], None)
        overlay.set_visible(True)
        pump(0.6)
        problem = excluded_window_is_absent(window, (bbox.x, bbox.y, 400, 60))
        assert problem is None, problem
    finally:
        overlay.close()
        flush_deletes()
