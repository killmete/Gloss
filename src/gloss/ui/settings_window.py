"""The settings window. It writes only through ``core.config.update_user_config``.

Only the values the user actually changed are written, so every setting they left alone keeps
following the shipped defaults. A rejected value shows a plain message and writes nothing.
"""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from gloss.core.config import GlossConfig, default_config_path, update_user_config
from gloss.core.config_migrations import ConfigMigrationError
from gloss.ui.hotkeys import HotkeyError, duplicate_bindings, parse_hotkey
from gloss.ui.native import ensure_app

log = logging.getLogger(__name__)

MODES = (("panel", "Side panel"), ("overlay", "On the screen, over the text"), ("both", "Both"))
PROFILES = (("fast", "Fast (recommended)"), ("accurate", "Accurate (slower, uses more memory)"))
# Friendly names only. Which languages are offered comes from the shipped config.
LANGUAGE_NAMES = {"zh": "Chinese", "en": "English", "ja": "Japanese"}


def supported_languages(defaults_path: Path | None, config: GlossConfig) -> list[str]:
    """The languages the shipped config reads, plus any the user already has."""
    found: list[str] = []
    try:
        with (defaults_path or default_config_path()).open("rb") as handle:
            found = [str(c) for c in tomllib.load(handle).get("ocr", {}).get("languages", [])]
    except (OSError, ValueError):
        pass
    return list(dict.fromkeys([*found, *config.ocr.languages]))


HOTKEY_LABELS = (
    ("toggle", "Show or hide translations"),
    ("select_region", "Choose the screen area"),
    ("pause", "Pause or resume"),
)

_QT_TO_NAME = {"meta": "win", "control": "ctrl"}


def sequence_to_hotkey(text: str) -> str:
    """Turn Qt's ``Ctrl+Alt+T`` into config's ``ctrl+alt+t``. Empty if nothing was pressed."""
    first = text.split(",")[0].strip()
    if not first:
        return ""
    return "+".join(_QT_TO_NAME.get(p.strip().lower(), p.strip().lower()) for p in first.split("+"))


def hotkey_to_sequence(text: str) -> QKeySequence:
    parts = [("Meta" if p == "win" else p.capitalize()) for p in text.split("+") if p]
    return QKeySequence("+".join(parts))


def friendly_error(exc: BaseException) -> str:
    """Words for a person, for each way saving can fail."""
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        return f"That setting is not valid ({where}): {first['msg']}"
    if isinstance(exc, ConfigMigrationError):
        return str(exc)
    if isinstance(exc, OSError):
        return f"Your settings file could not be written: {exc.strerror or exc}"
    return f"Your settings could not be saved ({type(exc).__name__})."


class SettingsWindow(QDialog):
    """``saved(changes)`` fires after the file is written. With ``auto_close`` the window then
    closes. A shell that wants to check hotkeys first sets it False and calls ``finish``."""

    saved = Signal(object)
    change_key_requested = Signal()

    def __init__(
        self,
        config: GlossConfig,
        *,
        user_path: Path | None = None,
        defaults_path: Path | None = None,
        can_change_key: bool = False,
        auto_close: bool = True,
        writer: Callable[..., None] = update_user_config,
        parent: QWidget | None = None,
    ) -> None:
        ensure_app()
        super().__init__(parent)
        self._initial = config
        self._user_path = user_path
        self._defaults_path = defaults_path
        self._auto_close = auto_close
        self._writer = writer
        self.setWindowTitle("Gloss settings")
        self.setMinimumWidth(480)

        self.mode = QComboBox()
        for value, label in MODES:
            self.mode.addItem(label, value)
        self.font_size = QSpinBox()
        self.font_size.setRange(8, 48)
        self.font_size.setSuffix(" px")

        self.hotkeys: dict[str, QKeySequenceEdit] = {}
        for name, _label in HOTKEY_LABELS:
            edit = QKeySequenceEdit()
            edit.setMaximumSequenceLength(1)
            self.hotkeys[name] = edit

        self.profile = QComboBox()
        for value, label in PROFILES:
            self.profile.addItem(label, value)
        self.languages: dict[str, QCheckBox] = {}
        self._language_codes = supported_languages(defaults_path, config)
        for code in self._language_codes:
            label = LANGUAGE_NAMES.get(code, code)
            self.languages[code] = QCheckBox(label)
        self.target = QLineEdit()
        self.target.setMaxLength(3)
        self.target.setPlaceholderText("en")

        self.cloud = QCheckBox("Send the text Gloss reads to the translation service")
        self.budget = QDoubleSpinBox()
        self.budget.setRange(0.1, 10000.0)
        self.budget.setDecimals(2)
        self.budget.setPrefix("US$ ")

        show = QFormLayout()
        show.addRow("Show translations in", self.mode)
        show.addRow("Text size", self.font_size)
        box_show = QGroupBox("Display")
        box_show.setLayout(show)

        keys = QFormLayout()
        for name, label in HOTKEY_LABELS:
            keys.addRow(label, self.hotkeys[name])
        box_keys = QGroupBox("Keyboard shortcuts (work in any program)")
        box_keys.setLayout(keys)

        reading = QFormLayout()
        reading.addRow("Reading speed", self.profile)
        row = QWidget()
        row_layout = QVBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        for box in self.languages.values():
            row_layout.addWidget(box)
        reading.addRow("Languages to read", row)
        box_reading = QGroupBox("Reading the screen")
        box_reading.setLayout(reading)

        trans = QFormLayout()
        trans.addRow("Translate into (language code)", self.target)
        trans.addRow(self.cloud)
        trans.addRow("Spending limit per month", self.budget)
        note = QLabel(
            "Turn the first box off to keep every word on this PC. Translation then falls back to "
            "an offline pack if you installed one, or shows the original text."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #777;")
        trans.addRow(note)
        box_trans = QGroupBox("Translating")
        box_trans.setLayout(trans)

        self.message = QLabel("")
        self.message.setWordWrap(True)

        self.key_button = QPushButton("Change API key...")
        self.key_button.setVisible(can_change_key)
        self.key_button.clicked.connect(self.change_key_requested.emit)

        self.save_button = QPushButton("Save")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.save)
        buttons = QDialogButtonBox()
        buttons.addButton(self.save_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel).clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        for part in (box_show, box_keys, box_reading, box_trans):
            layout.addWidget(part)
        layout.addWidget(self.key_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.message)
        layout.addWidget(buttons)
        self.load(config)

    # -- values
    def load(self, config: GlossConfig) -> None:
        self._initial = config
        self.mode.setCurrentIndex(max(0, self.mode.findData(config.render.mode)))
        self.font_size.setValue(config.render.font_size)
        for name, _label in HOTKEY_LABELS:
            self.hotkeys[name].setKeySequence(hotkey_to_sequence(getattr(config.hotkeys, name)))
        self.profile.setCurrentIndex(max(0, self.profile.findData(config.ocr.profile)))
        for code, box in self.languages.items():
            box.setChecked(code in config.ocr.languages)
        self.target.setText(config.translate.target_lang)
        self.cloud.setChecked(config.privacy.cloud_translation)
        self.budget.setValue(config.translate.budget.monthly_usd)

    def current_hotkeys(self) -> dict[str, str]:
        return {
            name: sequence_to_hotkey(edit.keySequence().toString())
            for name, edit in self.hotkeys.items()
        }

    def changes(self) -> dict[str, Any]:
        """Only what differs from the settings the window was opened with."""
        cfg = self._initial
        out: dict[str, Any] = {}

        def put(section: str, key: str, value: Any, old: Any) -> None:
            if value != old:
                out.setdefault(section, {})[key] = value

        put("render", "mode", self.mode.currentData(), cfg.render.mode)
        put("render", "font_size", self.font_size.value(), cfg.render.font_size)
        for name, text in self.current_hotkeys().items():
            put("hotkeys", name, text, getattr(cfg.hotkeys, name))
        put("ocr", "profile", self.profile.currentData(), cfg.ocr.profile)
        chosen = [code for code in self._language_codes if self.languages[code].isChecked()]
        extra = [c for c in cfg.ocr.languages if c not in self.languages]  # keep unknown ones
        wanted = chosen + extra
        if sorted(wanted) != sorted(cfg.ocr.languages):
            out.setdefault("ocr", {})["languages"] = wanted
        put(
            "translate",
            "target_lang",
            self.target.text().strip().lower(),
            cfg.translate.target_lang,
        )
        put("privacy", "cloud_translation", self.cloud.isChecked(), cfg.privacy.cloud_translation)
        budget = round(self.budget.value(), 2)
        if abs(budget - cfg.translate.budget.monthly_usd) > 0.004:
            out.setdefault("translate", {}).setdefault("budget", {})["monthly_usd"] = budget
        return out

    # -- messages
    def show_message(self, text: str, error: bool = False) -> None:
        self.message.setStyleSheet(f"color: {'#c0392b' if error else '#2e7d32'};")
        self.message.setText(text)

    def problem_before_saving(self) -> str | None:
        hotkeys = self.current_hotkeys()
        for name, text in hotkeys.items():
            label = dict(HOTKEY_LABELS)[name]
            if not text:
                return f"Press the keys you want for '{label}'."
            try:
                parse_hotkey(text)
            except HotkeyError as exc:
                return f"'{label}': {exc}"
        pairs = duplicate_bindings(hotkeys)
        if pairs:
            a, b = pairs[0]
            labels = dict(HOTKEY_LABELS)
            return f"'{labels[a]}' and '{labels[b]}' use the same keys. Give each its own."
        if not any(box.isChecked() for box in self.languages.values()):
            return "Pick at least one language to read."
        if len(self.target.text().strip()) < 2:
            return "Type the language to translate into, for example en."
        return None

    # -- saving
    def save(self) -> None:
        problem = self.problem_before_saving()
        if problem:
            self.show_message(problem, error=True)
            return
        changes = self.changes()
        if not changes:
            self.show_message("Nothing changed.")
            if self._auto_close:
                self.accept()
            return
        try:
            kwargs: Mapping[str, Any] = {
                k: v
                for k, v in (("user_path", self._user_path), ("defaults_path", self._defaults_path))
                if v is not None
            }
            self._writer(changes, **kwargs)
        except Exception as exc:
            log.warning("settings were not saved: %s", type(exc).__name__)
            self.show_message(friendly_error(exc), error=True)
            return
        self.show_message("Saved.")
        self.saved.emit(changes)
        if self._auto_close:
            self.accept()

    def finish(self, problems: Mapping[str, str]) -> None:
        """Called by the shell after it applied the new settings."""
        if problems:
            self.show_message("Saved, but " + " ".join(problems.values()), error=True)
        else:
            self.accept()
