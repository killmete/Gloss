"""The side panel: content, scrolling, copy, status, position, and responsiveness."""

from __future__ import annotations

import threading
import time

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from ui_qt_helpers import flush_deletes, get_app, item, pump

from gloss.adapters.render.panel import PanelRenderer
from gloss.core.contracts import AdapterContext, Glossary, GlossaryEntry, Health


@pytest.fixture
def panel(tmp_path):
    get_app()
    renderer = PanelRenderer(AdapterContext({"state_file": str(tmp_path / "state.ini")}))
    renderer.open()
    yield renderer
    renderer.close()
    flush_deletes()


def texts(renderer):
    return [(e.original, e.english) for e in renderer.window.model.entries]


def test_newest_line_is_at_the_bottom(panel):
    panel.show([item("one", line_id="a"), item("two", line_id="b")], None)
    panel.show([item("three", line_id="c")], None)
    assert [e for _o, e in texts(panel)] == ["one", "two", "three"]


def test_original_and_english_are_both_kept(panel):
    panel.show([item("Hello", original="你好")], None)
    assert texts(panel) == [("你好", "Hello")]


def test_a_line_sent_again_updates_in_place(panel):
    panel.show([item("rough", line_id="a")], None)
    panel.show([item("better", line_id="a")], None)
    assert [e for _o, e in texts(panel)] == ["better"]


def test_failed_line_says_so_and_keeps_the_original(panel):
    panel.show([item("", status="failed", original="坏")], None)
    ((original, english),) = texts(panel)
    assert original == "坏"
    assert "Could not translate" in english
    assert panel.window.model.entries[0].failed


def test_tags_for_low_quality_rough_and_untranslated(panel):
    panel.show([item("x", low_quality=True, rough=True, line_id="a")], None)
    panel.show([item("", status="passthrough", line_id="b")], None)
    first, second = panel.window.model.entries
    assert set(first.tags) == {"low-quality text", "rough translation"}
    assert second.tags == ("not translated",)


def test_empty_lines_are_not_shown(panel):
    panel.show([item("   ", original="  ")], None)
    assert texts(panel) == []


def test_a_huge_line_is_cut_down(panel):
    panel.show([item("长 " * 5000)], None)
    assert len(panel.window.model.entries[0].english) <= 1000


def test_history_is_capped(tmp_path):
    get_app()
    renderer = PanelRenderer(
        AdapterContext({"state_file": str(tmp_path / "s.ini"), "history_max": 10})
    )
    renderer.open()
    try:
        renderer.show([item(f"t{i}", line_id=f"i{i}") for i in range(25)], None)
        english = [e for _o, e in texts(renderer)]
        assert english == [f"t{i}" for i in range(15, 25)]
    finally:
        renderer.close()


def test_status_dot_text_and_permanent_banner(panel):
    window = panel.window
    panel.set_status(Health("degraded", "slow"), "Low-quality OCR: text may be wrong or missing")
    assert window.banner.isVisibleTo(window)
    assert "Low-quality OCR" in window.banner.text()
    assert "slow" in window.status.text()
    panel.set_status(Health("ok"), "")
    assert not window.banner.isVisibleTo(window)


def test_click_copies_the_english(panel):
    panel.show([item("Copy me", line_id="a")], None)
    window = panel.window
    window._copy(window.model.index(0))
    assert QGuiApplication.clipboard().text() == "Copy me"
    assert "Copied" in window.footer.text()


def test_hover_note_comes_from_the_glossary(panel):
    glossary = Glossary("1", (GlossaryEntry("520", "I love you", "number pun"),))
    panel.set_glossary(glossary)
    panel.show([item("520!", original="520", line_id="a")], None)
    window = panel.window
    window._hover(window.model.index(0))
    assert "number pun" in window.footer.text()


def test_clear_keeps_history_and_the_button_empties_it(panel):
    panel.show([item("keep", line_id="a")], None)
    panel.clear()
    assert len(texts(panel)) == 1
    panel.clear_history()
    assert texts(panel) == []


def test_follows_new_lines_until_the_user_scrolls_up(panel):
    panel.set_visible(True)
    window = panel.window
    window.resize(300, 300)
    panel.show([item(f"line {i} " + "word " * 12, line_id=f"a{i}") for i in range(40)], None)
    pump(0.3)
    bar = window.list.verticalScrollBar()
    assert bar.maximum() > 0
    assert bar.value() == bar.maximum()

    bar.setValue(0)  # the user scrolls up to read
    pump(0.1)
    panel.show([item("new", line_id="n1")], None)
    pump(0.2)
    assert bar.value() == 0

    bar.setValue(bar.maximum())  # back at the bottom: following again
    pump(0.1)
    panel.show([item("newer", line_id="n2")], None)
    pump(0.2)
    assert bar.value() == bar.maximum()


def test_close_button_hides_instead_of_quitting(panel):
    panel.set_visible(True)
    panel.window.close()
    assert not panel.window.isVisible()
    assert panel.health().state == "ok"


def test_position_is_remembered(tmp_path):
    get_app()
    state = str(tmp_path / "panel.ini")
    first = PanelRenderer(AdapterContext({"state_file": state}))
    first.open()
    first.set_visible(True)
    pump(1.2)  # the first remembered position is the one the window opened at
    first.window.resize(333, 444)
    first.window.move(120, 90)
    pump(0.3)
    first.close()
    flush_deletes()

    second = PanelRenderer(AdapterContext({"state_file": state}))
    second.open()
    try:
        assert (second.window.width(), second.window.height()) == (333, 444)
        assert (second.window.x(), second.window.y()) == (120, 90)
    finally:
        second.close()


def test_an_untouched_panel_writes_no_state_file(tmp_path):
    get_app()
    state = tmp_path / "never.ini"
    renderer = PanelRenderer(AdapterContext({"state_file": str(state)}))
    renderer.open()
    renderer.set_visible(True)
    pump(0.2)
    renderer.close()
    assert not state.exists()


def test_bad_settings_fall_back_to_defaults():
    renderer = PanelRenderer(AdapterContext({"font_size": "big", "width": -4, "history_max": None}))
    assert renderer._cfg.font_size == 14
    assert renderer._cfg.width == 420


def test_the_panel_stays_responsive_while_a_slow_translator_works(panel):
    """A worker that takes 300 ms per batch must not stall the Qt thread."""
    panel.set_visible(True)
    gaps: list[float] = []
    last = [time.monotonic()]

    def beat() -> None:
        now = time.monotonic()
        gaps.append(now - last[0])
        last[0] = now

    ticker = QTimer()
    ticker.setInterval(10)
    ticker.timeout.connect(beat)
    ticker.start()
    call_times: list[float] = []

    def slow_worker() -> None:
        for batch in range(5):
            time.sleep(0.3)  # the "translation"
            start = time.perf_counter()
            panel.show([item(f"batch {batch}", line_id=f"b{batch}")], None)
            panel.set_status(Health("ok", f"batch {batch}"), "")
            call_times.append(time.perf_counter() - start)

    thread = threading.Thread(target=slow_worker, daemon=True)
    thread.start()
    pump(2.2, until=lambda: not thread.is_alive())
    thread.join(1)
    pump(0.2)
    ticker.stop()
    assert len(texts(panel)) == 5
    assert max(call_times) < 0.05, "show() from a worker must return at once"
    assert max(gaps[1:]) < 0.25, f"the UI thread stalled for {max(gaps[1:]):.2f} s"
