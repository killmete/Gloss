"""Hotkey parsing, conflict detection with a clear message, and delivery of WM_HOTKEY."""

from __future__ import annotations

import ctypes
import sys

import pytest
from ui_qt_helpers import get_app, pump

from gloss.ui.hotkeys import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    MOD_WIN,
    WM_HOTKEY,
    HotkeyError,
    HotkeyService,
    duplicate_bindings,
    parse_hotkey,
)

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")
# Rarely used by real programs, so the tests do not fight the user's own shortcuts.
FREE = "ctrl+alt+shift+f23"
FREE2 = "ctrl+alt+shift+f22"


def test_parses_modifiers_and_letters():
    parsed = parse_hotkey("ctrl+alt+t")
    assert parsed.modifiers == MOD_CONTROL | MOD_ALT
    assert parsed.vk == ord("T")


def test_parses_function_keys_digits_and_win():
    assert parse_hotkey("ctrl+f1").vk == 0x70
    assert parse_hotkey("shift+f24").vk == 0x70 + 23
    assert parse_hotkey("alt+5").vk == ord("5")
    assert parse_hotkey("win+shift+x").modifiers == MOD_WIN | MOD_SHIFT


def test_is_case_and_space_tolerant():
    assert parse_hotkey(" Ctrl + Alt + R ").text == "ctrl+alt+r"


@pytest.mark.parametrize("bad", ["", "t", "ctrl+", "ctrl+alt+tab", "super+t", "ctrl+f25", "+t"])
def test_rejects_nonsense_with_a_message(bad):
    with pytest.raises(HotkeyError) as caught:
        parse_hotkey(bad)
    assert str(caught.value)


def test_two_actions_on_the_same_keys_are_reported():
    pairs = duplicate_bindings({"toggle": "ctrl+alt+t", "pause": "Ctrl+Alt+T", "x": "ctrl+alt+r"})
    assert pairs == [("toggle", "pause")]


def test_service_reports_duplicates_and_bad_text_without_registering():
    get_app()
    service = HotkeyService()
    problems = service.set_bindings({"toggle": FREE, "pause": FREE, "select_region": "nonsense"})
    try:
        assert "same as" in problems["pause"]
        assert "select region" in problems["select_region"]
        assert "toggle" not in problems
    finally:
        service.close()


@windows_only
def test_a_key_taken_by_another_program_gives_a_clear_message():
    get_app()
    first, second = HotkeyService(), HotkeyService()
    try:
        assert first.set_bindings({"toggle": FREE}) == {}
        seen: list[tuple[str, str]] = []
        second.failed.connect(lambda n, m: seen.append((n, m)))
        problems = second.set_bindings({"pause": FREE})
        message = problems["pause"]
        assert "already used by another program" in message
        assert FREE in message
        assert "Settings" in message
        assert seen == [("pause", message)]
    finally:
        first.close()
        second.close()


@windows_only
def test_the_win_key_gets_its_own_explanation():
    get_app()
    service = HotkeyService()
    try:
        problems = service.set_bindings({"toggle": "win+l"})
        assert "Windows key" in problems["toggle"]
    finally:
        service.close()


@windows_only
def test_closing_frees_the_keys_again():
    get_app()
    first = HotkeyService()
    assert first.set_bindings({"toggle": FREE}) == {}
    first.close()
    second = HotkeyService()
    try:
        assert second.set_bindings({"toggle": FREE}) == {}
    finally:
        second.close()


@windows_only
def test_a_key_press_reaches_the_service_through_the_qt_event_loop():
    """Post a real WM_HOTKEY to this thread, as Windows does, and let Qt deliver it."""
    get_app()
    service = HotkeyService()
    try:
        assert service.set_bindings({"toggle": FREE, "pause": FREE2}) == {}
        got: list[str] = []
        service.triggered.connect(got.append)
        kernel32 = ctypes.WinDLL("kernel32")
        user32 = ctypes.WinDLL("user32")
        thread_id = kernel32.GetCurrentThreadId()
        hotkey_ids = {name: i for i, name in service._ids.items()}
        assert user32.PostThreadMessageW(thread_id, WM_HOTKEY, hotkey_ids["pause"], 0)
        pump(0.5, until=lambda: bool(got))
        assert got == ["pause"]
    finally:
        service.close()


def test_handle_message_ignores_unknown_ids():
    get_app()
    service = HotkeyService()
    got: list[str] = []
    service.triggered.connect(got.append)
    service.handle_message(9999)
    assert got == []
