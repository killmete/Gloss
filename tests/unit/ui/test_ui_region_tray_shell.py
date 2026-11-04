"""Region selection maths, the tray, and the shell that joins everything."""

from __future__ import annotations

import threading
import time

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest
from ui_qt_helpers import flush_deletes, get_app, item, pump

from gloss.core.config import load_config
from gloss.core.contracts import Health, MonitorInfo, Rect
from gloss.ui import region_selector as region_module
from gloss.ui.region_selector import (
    MIN_SELECTION_PX,
    MonitorGeometry,
    RegionSelector,
    drag_rect,
    logical_to_physical_rect,
)
from gloss.ui.shell import LOW_QUALITY_BANNER, GlossShell, secret_name_for, worst
from gloss.ui.tray import TrayController, status_text

FAKE_MONITORS = (MonitorInfo(r"\\.\DISPLAY9", Rect(-100000, -100000, 200000, 200000), 1.0, True),)


def test_monitor_id_is_the_device_name_of_the_monitor_holding_the_rect():
    left = MonitorInfo(r"\\.\DISPLAY1", Rect(-1920, 0, 1920, 1080), 1.0)
    right = MonitorInfo(r"\\.\DISPLAY2", Rect(0, 0, 2560, 1440), 1.0, True)
    assert region_module.monitor_id_for(Rect(100, 100, 50, 50), [left, right]) == r"\\.\DISPLAY2"
    assert region_module.monitor_id_for(Rect(-500, 10, 100, 100), [left, right]) == r"\\.\DISPLAY1"
    assert region_module.monitor_id_for(Rect(9000, 0, 10, 10), [left, right]) == ""


# ---- region maths --------------------------------------------------------------------------


def test_100_percent_monitor_maps_one_to_one():
    assert logical_to_physical_rect(10, 20, 300, 200, MonitorGeometry(0, 0, 1.0)) == Rect(
        10, 20, 300, 200
    )


def test_a_monitor_left_of_the_primary_keeps_its_negative_origin():
    rect = logical_to_physical_rect(100, 50, 400, 300, MonitorGeometry(-1920, 0, 1.0))
    assert rect == Rect(-1820, 50, 400, 300)


def test_a_150_percent_monitor_scales_up_and_never_cuts_the_selection():
    rect = logical_to_physical_rect(101, 51, 101, 51, MonitorGeometry(0, 0, 1.5))
    assert rect.x == 151 and rect.y == 76  # floor(151.5), floor(76.5)
    assert rect.right == 303 and rect.bottom == 153  # ceil(303), ceil(153)
    assert rect.right >= 1.5 * (101 + 101) and rect.bottom >= 1.5 * (51 + 51)


def test_a_200_percent_monitor_below_the_primary():
    rect = logical_to_physical_rect(50, 25, 100, 80, MonitorGeometry(0, 1620, 2.0))
    assert rect == Rect(100, 1670, 200, 160)


def test_drag_rect_works_in_any_direction():
    assert drag_rect(QPoint(50, 60), QPoint(10, 20)) == drag_rect(QPoint(10, 20), QPoint(50, 60))


def test_selector_cancel_and_drag(monkeypatch):
    get_app()
    monkeypatch.setattr(region_module, "list_monitors", lambda: FAKE_MONITORS)
    selector = RegionSelector()
    got: list = []
    cancelled: list = []
    selector.selected.connect(lambda r, m: got.append((r, m)))
    selector.cancelled.connect(lambda: cancelled.append(True))
    selector.start()
    try:
        assert len(selector._windows) == len(QGuiApplication.screens())
        window = selector._windows[0]
        monitor = window.monitor()
        QTest.mousePress(
            window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(40, 50)
        )
        QTest.mouseMove(window, QPoint(240, 150))
        QTest.mouseRelease(
            window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(240, 150)
        )
        pump(0.1)
        assert len(got) == 1
        rect, monitor_id = got[0]
        assert rect == logical_to_physical_rect(40, 50, 201, 101, monitor)
        assert monitor_id == FAKE_MONITORS[0].monitor_id
        assert monitor_id != window.qscreen.name()  # Qt gives a model name, not the device id
        assert not selector.active
        assert cancelled == []
    finally:
        selector.cancel()

    selector.start()
    selector.cancel()
    assert cancelled == [True]
    assert not selector.active
    flush_deletes()


def test_a_tiny_drag_is_ignored_and_the_selector_keeps_waiting():
    get_app()
    selector = RegionSelector()
    got: list = []
    selector.selected.connect(lambda r, m: got.append(r))
    selector.start()
    try:
        window = selector._windows[0]
        QTest.mousePress(
            window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(40, 50)
        )
        QTest.mouseRelease(
            window,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            QPoint(40 + MIN_SELECTION_PX - 5, 52),
        )
        pump(0.05)
        assert got == [] and selector.active
    finally:
        selector.cancel()


def test_escape_cancels():
    get_app()
    selector = RegionSelector()
    cancelled: list = []
    selector.cancelled.connect(lambda: cancelled.append(True))
    selector.start()
    QTest.keyClick(selector._windows[0], Qt.Key.Key_Escape)
    assert cancelled == [True]


# ---- tray ----------------------------------------------------------------------------------


@pytest.fixture
def tray():
    get_app()
    controller = TrayController(confirm=lambda title, text: True)
    yield controller
    controller.hide()


def test_status_words():
    assert status_text(Health("ok")) == "Working"
    assert status_text(Health("degraded", "Using the offline translator.")).startswith("Limited:")
    assert status_text(Health("down", "No key.")) == "Not working: No key."


def test_tray_tooltip_and_first_menu_line_say_why(tray):
    tray.set_health(Health("down", "No API key yet."))
    assert "No API key yet." in tray.tray.toolTip()
    assert tray.status_action.text() == "Not working: No API key yet."
    assert not tray.status_action.isEnabled()


def test_icon_changes_with_the_status(tray):
    green = tray.tray.icon().pixmap(32, 32).toImage()
    tray.set_health(Health("down", "x"))
    red = tray.tray.icon().pixmap(32, 32).toImage()
    assert green != red


def test_menu_has_the_required_actions(tray):
    labels = [a.text() for a in tray.menu.actions() if a.text()]
    for wanted in ("Pause", "Choose screen area...", "Settings...", "Quit Gloss"):
        assert wanted in labels
    assert any(
        a.menu() is not None and a.text() == "Show translations in" for a in tray.menu.actions()
    )


def test_pause_and_resume_wording_and_signal(tray):
    seen: list[bool] = []
    tray.pause_toggled.connect(seen.append)
    tray.pause_action.trigger()
    assert tray.pause_action.text() == "Resume"
    tray.pause_action.trigger()
    assert tray.pause_action.text() == "Pause"
    assert seen == [True, False]


def test_mode_choice_is_a_radio_group(tray):
    seen: list[str] = []
    tray.mode_selected.connect(seen.append)
    for action in tray._mode_actions.values():
        if action.data() == "overlay":
            action.trigger()
    assert seen == ["overlay"]
    assert tray._mode_actions["overlay"].isChecked()
    assert not tray._mode_actions["panel"].isChecked()


def test_low_quality_needs_a_yes_and_can_be_declined():
    get_app()
    answers = iter([False, True])
    controller = TrayController(confirm=lambda t, x: next(answers))
    seen: list[bool] = []
    controller.low_quality_toggled.connect(seen.append)
    controller.low_quality_action.trigger()  # declined
    assert seen == [] and not controller.low_quality_action.isChecked()
    controller.low_quality_action.trigger()  # accepted
    assert seen == [True] and controller.low_quality_action.isChecked()
    controller.low_quality_action.trigger()  # switching off never asks
    assert seen == [True, False]


# ---- shell ---------------------------------------------------------------------------------


def test_worst_picks_the_most_serious():
    assert worst(Health("ok"), Health("down", "a"), Health("degraded", "b")).detail == "a"
    assert worst(Health("degraded", "first"), Health("degraded", "second")).detail == "first"


@pytest.fixture
def shell(tmp_path):
    get_app()
    user = tmp_path / "config.toml"
    loaded = load_config(user_path=user)
    instance = GlossShell(loaded, user_path=user, confirm=lambda t, x: True)
    instance.start()
    yield instance
    instance.shutdown()
    flush_deletes()


def panel_of(shell):
    return next(r for r in shell._renderers if r.name == "panel")


def test_shell_starts_the_panel_visible_with_a_tray(shell):
    assert [r.name for r in shell._renderers] == ["panel"]
    assert panel_of(shell).window.isVisible()


def test_results_from_a_worker_thread_reach_the_panel(shell):
    done = threading.Event()

    def worker() -> None:
        shell.show_results([item("From a worker", line_id="w1")], None)
        shell.set_health(Health("degraded", "Using the backup translator."))
        done.set()

    threading.Thread(target=worker, daemon=True).start()
    pump(1, until=lambda: "backup" in shell.tray.tray.toolTip())
    assert done.is_set()
    assert [e.english for e in panel_of(shell).window.model.entries] == ["From a worker"]
    assert shell.tray.health.state == "degraded"
    assert "backup" in panel_of(shell).window.status.text()


def test_low_quality_mode_is_amber_with_a_permanent_banner(shell):
    seen: list[bool] = []
    shell.low_quality_changed.connect(seen.append)
    shell.tray.low_quality_action.trigger()
    window = panel_of(shell).window
    assert seen == [True] and shell.low_quality
    assert window.banner.isVisibleTo(window)
    assert window.banner.text() == LOW_QUALITY_BANNER
    assert shell.tray.health.state == "degraded"
    # A healthy report from the pipeline does not clear the warning.
    shell.set_health(Health("ok"))
    pump(0.1)
    assert shell.tray.health.state == "degraded"
    assert window.banner.isVisibleTo(window)
    shell.tray.low_quality_action.trigger()
    assert not window.banner.isVisibleTo(window)
    assert shell.tray.health.state == "ok"


def test_hotkeys_drive_the_shell(shell):
    seen: list[bool] = []
    shell.pause_toggled.connect(seen.append)
    shell._on_hotkey("pause")
    shell._on_hotkey("pause")
    assert seen == [True, False]
    shell._on_hotkey("toggle")
    assert not panel_of(shell).window.isVisible()
    shell._on_hotkey("toggle")
    assert panel_of(shell).window.isVisible()


def test_changing_the_mode_saves_it_and_swaps_the_renderers(shell, tmp_path):
    seen: list[str] = []
    shell.mode_changed.connect(seen.append)
    shell.set_mode("both")
    assert seen == ["both"]
    assert sorted(r.name for r in shell._renderers) == ["overlay", "panel"]
    assert 'mode = "both"' in (tmp_path / "config.toml").read_text(encoding="utf-8")
    shell.set_mode("overlay")
    assert [r.name for r in shell._renderers] == ["overlay"]


def test_the_ui_thread_never_waits_for_a_slow_pipeline(shell):
    """A worker that takes 300 ms per result must not stall the Qt thread."""
    gaps: list[float] = []
    last = [time.monotonic()]
    from PySide6.QtCore import QTimer

    def beat() -> None:
        now = time.monotonic()
        gaps.append(now - last[0])
        last[0] = now

    ticker = QTimer()
    ticker.setInterval(10)
    ticker.timeout.connect(beat)
    ticker.start()

    def slow_pipeline() -> None:
        for i in range(4):
            time.sleep(0.3)
            shell.show_results([item(f"line {i}", line_id=f"s{i}")], None)
            shell.set_health(Health("ok", f"{i}"))

    thread = threading.Thread(target=slow_pipeline, daemon=True)
    thread.start()
    pump(2, until=lambda: not thread.is_alive())
    pump(0.2)
    ticker.stop()
    assert len(panel_of(shell).window.model.entries) == 4
    assert max(gaps[1:]) < 0.25


class KeyStore:
    def __init__(self, value=None):
        self.value = value

    def get(self, name):
        return self.value

    def set(self, name, value):
        self.value = value

    def delete(self, name):
        self.value = None


def test_first_run_asks_for_a_key_only_when_there_is_none(tmp_path):
    get_app()
    user = tmp_path / "c.toml"
    loaded = load_config(user_path=user)
    assert secret_name_for(loaded.config) == "anthropic"
    empty = GlossShell(loaded, secrets=KeyStore(), user_path=user)
    assert empty.needs_api_key()
    have = GlossShell(loaded, secrets=KeyStore("x" * 30), user_path=user)
    assert not have.needs_api_key()
    none = GlossShell(loaded, secrets=None, user_path=user)
    assert not none.needs_api_key()


def test_first_run_does_not_ask_for_a_key_when_cloud_translation_is_off(tmp_path):
    get_app()
    user = tmp_path / "c.toml"
    user.write_text("[privacy]\ncloud_translation = false\n", encoding="utf-8")
    shell = GlossShell(load_config(user_path=user), secrets=KeyStore(), user_path=user)
    assert not shell.needs_api_key()


def test_first_run_opens_the_key_dialog_then_the_region_selector(tmp_path):
    get_app()
    user = tmp_path / "c.toml"
    shell = GlossShell(load_config(user_path=user), secrets=KeyStore(), user_path=user)
    started: list[bool] = []
    shell.selector.start = lambda: started.append(True)  # type: ignore[method-assign]
    shell.run_first_run()
    assert shell._key_dialog is not None and started == []
    shell._key_dialog.reject()
    pump(0.1, until=lambda: bool(started))
    assert started == [True]


def test_remove_lines_reaches_the_overlay_and_ignores_the_panel(tmp_path):
    get_app()
    user = tmp_path / "c.toml"
    user.write_text('[render]\nmode = "both"\n', encoding="utf-8")
    shell = GlossShell(load_config(user_path=user), user_path=user)
    shell.start()
    try:
        box = Rect(10, 10, 100, 20)
        shell.show_results(
            [item("A", line_id="a", bbox=box), item("B", line_id="b", bbox=Rect(10, 200, 100, 20))],
            None,
        )
        overlay = next(r for r in shell._renderers if r.name == "overlay")
        assert sorted(c.text for w in overlay.windows for c in w.chips) == ["A", "B"]
        done = threading.Event()

        def worker() -> None:
            shell.remove_lines(["a"])
            done.set()

        threading.Thread(target=worker, daemon=True).start()
        pump(1, until=lambda: [c.text for w in overlay.windows for c in w.chips] == ["B"])
        assert done.is_set()
        assert [c.text for w in overlay.windows for c in w.chips] == ["B"]
        assert len(panel_of(shell).window.model.entries) == 2  # history stays
    finally:
        shell.shutdown()
        flush_deletes()


def test_remove_lines_never_raises(shell):
    class Bad:
        name = "bad"

        def remove(self, ids):
            raise RuntimeError("boom")

    shell._renderers = (*shell._renderers, Bad())  # type: ignore[assignment]
    shell.remove_lines(["x"])
    pump(0.1)


def test_api_key_saved_signal_fires_after_the_key_is_stored(tmp_path):
    get_app()
    user = tmp_path / "c.toml"
    store = KeyStore()
    shell = GlossShell(load_config(user_path=user), secrets=store, user_path=user)
    seen: list[bool] = []
    shell.api_key_saved.connect(lambda: seen.append(store.value is not None))
    dialog = shell.open_api_key_dialog()
    dialog.key_edit.setText("k" * 40)
    dialog._on_save()
    assert seen == [True]


def test_first_run_picks_a_region_even_when_a_key_exists(tmp_path):
    get_app()
    user = tmp_path / "c.toml"
    shell = GlossShell(load_config(user_path=user), secrets=KeyStore("x" * 30), user_path=user)
    started: list[bool] = []
    shell.selector.start = lambda: started.append(True)  # type: ignore[method-assign]
    shell.run_first_run()
    pump(0.1, until=lambda: bool(started))
    assert started == [True] and shell._key_dialog is None
