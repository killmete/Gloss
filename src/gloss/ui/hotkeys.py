"""Global hotkeys with ``RegisterHotKey`` and a Qt native event filter (ADR-0001 section 7).

Windows delivers ``WM_HOTKEY`` to the thread that registered the key, which is the Qt main
thread, so no extra thread and no keyboard hook is needed. A key that another program already
owns cannot be registered. That is reported with a plain sentence the user can act on, never
silently.

``HotkeyService.set_bindings`` takes ``{"toggle": "ctrl+alt+t", ...}`` straight from config. The
``triggered(name)`` signal fires for a press. ``failed(name, message)`` fires for a binding that
could not be registered, and the same text is returned from ``set_bindings`` and kept in
``problems``.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import re
import sys
from collections.abc import Mapping
from ctypes import wintypes
from dataclasses import dataclass

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal

log = logging.getLogger(__name__)

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY = 0x0312
ERROR_HOTKEY_ALREADY_REGISTERED = 1409
_MODIFIERS = {"ctrl": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}
_KEY = re.compile(r"^(?:[a-z0-9]|f(?:[1-9]|1[0-9]|2[0-4]))$")


class HotkeyError(ValueError):
    """The text is not a hotkey. The message is safe to show to the user."""


@dataclass(frozen=True)
class ParsedHotkey:
    modifiers: int
    vk: int
    text: str


def parse_hotkey(text: str) -> ParsedHotkey:
    """Turn ``ctrl+alt+t`` into Windows modifier flags and a virtual-key code."""
    parts = [p.strip().lower() for p in str(text).split("+")]
    if len(parts) < 2 or not all(parts):
        raise HotkeyError(f"'{text}' is not a hotkey. Use a form like ctrl+alt+t.")
    *mods, key = parts
    flags = 0
    for mod in mods:
        if mod not in _MODIFIERS:
            raise HotkeyError(f"'{mod}' is not a modifier. Use ctrl, alt, shift or win.")
        flags |= _MODIFIERS[mod]
    if not _KEY.fullmatch(key):
        raise HotkeyError(f"'{key}' is not a key Gloss can use. Use a letter, digit or F1-F24.")
    if key.startswith("f") and len(key) > 1:
        vk = 0x70 + int(key[1:]) - 1  # VK_F1 = 0x70
    else:
        vk = ord(key.upper())
    return ParsedHotkey(flags, vk, "+".join(parts))


def conflict_message(name: str, text: str, error: int | None = None) -> str:
    label = name.replace("_", " ")
    if error == ERROR_HOTKEY_ALREADY_REGISTERED or error is None:
        return (
            f"The '{label}' hotkey {text} is already used by another program. "
            "Open Settings and pick a different one."
        )
    return f"The '{label}' hotkey {text} could not be set (Windows error {error})."


def duplicate_bindings(bindings: Mapping[str, str]) -> list[tuple[str, str]]:
    """Pairs of actions that were given the same hotkey, so one would silently never work."""
    seen: dict[str, str] = {}
    pairs: list[tuple[str, str]] = []
    for name, text in bindings.items():
        try:
            key = parse_hotkey(text).text
        except HotkeyError:
            continue
        if key in seen:
            pairs.append((seen[key], name))
        else:
            seen[key] = name
    return pairs


class _Filter(QAbstractNativeEventFilter):
    def __init__(self, service: HotkeyService) -> None:
        super().__init__()
        self._service = service

    def nativeEventFilter(self, event_type: object, message: object) -> tuple[bool, int]:  # noqa: N802
        if sys.platform == "win32" and bytes(event_type) == b"windows_generic_MSG":  # type: ignore[arg-type]
            try:
                msg = wintypes.MSG.from_address(int(message))  # type: ignore[call-overload]
            except Exception:
                return False, 0
            if msg.message == WM_HOTKEY:
                self._service.handle_message(int(msg.wParam))
                return True, 0
        return False, 0


class HotkeyService(QObject):
    triggered = Signal(str)
    failed = Signal(str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._user32 = None
        if sys.platform == "win32":
            self._user32 = ctypes.WinDLL("user32", use_last_error=True)
            self._user32.RegisterHotKey.argtypes = [
                wintypes.HWND,
                ctypes.c_int,
                wintypes.UINT,
                wintypes.UINT,
            ]
            self._user32.RegisterHotKey.restype = wintypes.BOOL
            self._user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            self._user32.UnregisterHotKey.restype = wintypes.BOOL
        self._ids: dict[int, str] = {}
        self._next_id = 1
        self.problems: dict[str, str] = {}
        self._filter: _Filter | None = None

    def _install_filter(self) -> None:
        if self._filter is None:
            from PySide6.QtCore import QCoreApplication

            self._filter = _Filter(self)
            app = QCoreApplication.instance()
            if app is not None:
                app.installNativeEventFilter(self._filter)

    def set_bindings(self, bindings: Mapping[str, str]) -> dict[str, str]:
        """Replace all hotkeys. Return ``{name: message}`` for each that failed."""
        self.clear()
        problems: dict[str, str] = {}
        for first, second in duplicate_bindings(bindings):
            problems[second] = (
                f"The '{second.replace('_', ' ')}' hotkey is the same as the "
                f"'{first.replace('_', ' ')}' hotkey. Give each action its own keys."
            )
        for name, text in bindings.items():
            if name in problems:
                continue
            try:
                parsed = parse_hotkey(text)
            except HotkeyError as exc:
                problems[name] = f"The '{name.replace('_', ' ')}' hotkey: {exc}"
                continue
            message = self._register(name, parsed)
            if message:
                problems[name] = message
        self.problems = problems
        for name, message in problems.items():
            log.warning("hotkey %s: %s", name, message)
            self.failed.emit(name, message)
        return problems

    def _register(self, name: str, parsed: ParsedHotkey) -> str:
        if self._user32 is None:
            return ""  # Not Windows: nothing to register, and nothing to report.
        self._install_filter()
        hotkey_id = self._next_id
        self._next_id += 1
        if self._user32.RegisterHotKey(None, hotkey_id, parsed.modifiers | MOD_NOREPEAT, parsed.vk):
            self._ids[hotkey_id] = name
            return ""
        error = ctypes.get_last_error()
        text = parsed.text
        if parsed.modifiers & MOD_WIN:
            return (
                f"The '{name.replace('_', ' ')}' hotkey {text} uses the Windows key, which "
                "Windows keeps for itself. Pick one with ctrl or alt."
            )
        return conflict_message(name, text, error)

    def handle_message(self, hotkey_id: int) -> None:
        """Called for each WM_HOTKEY. Public so tests can send one without a keyboard."""
        name = self._ids.get(hotkey_id)
        if name is not None:
            self.triggered.emit(name)

    def clear(self) -> None:
        if self._user32 is not None:
            for hotkey_id in list(self._ids):
                with contextlib.suppress(Exception):
                    self._user32.UnregisterHotKey(None, hotkey_id)
        self._ids.clear()

    def close(self) -> None:
        self.clear()
        if self._filter is not None:
            from PySide6.QtCore import QCoreApplication

            app = QCoreApplication.instance()
            if app is not None:
                with contextlib.suppress(Exception):
                    app.removeNativeEventFilter(self._filter)
            self._filter = None
