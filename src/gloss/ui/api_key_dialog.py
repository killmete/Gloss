"""The "enter your API key" dialog, used on first run and from Settings.

It talks to the key store only through the ``SecretStore`` port, so any implementation works
(the real Windows Credential Manager one, or a fake in tests). The key is never shown after
it is typed, never logged and never kept anywhere but the store.

The optional ``test`` callable makes one small translation and returns the English text, or
raises. It runs on a background thread, so the window stays responsive while the network call
is in flight.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gloss.core.contracts import GlossError, SecretStore
from gloss.ui.native import MainThread, ensure_app

log = logging.getLogger(__name__)

INTRO = (
    "Gloss reads the text on your screen on this PC, then sends only that text (never a picture "
    "of your screen) to Anthropic's Claude to translate it.\n\n"
    "Paste your Anthropic API key below. It is kept in Windows Credential Manager, not in a file."
)


def looks_like_a_key(text: str) -> str | None:
    """Return a plain-English problem with the pasted text, or None if it looks fine."""
    if not text:
        return "Paste your key first."
    if any(ch.isspace() for ch in text):
        return "A key has no spaces or line breaks. Copy it again, without anything around it."
    if len(text) < 20:
        return "That is too short to be a key. Copy the whole key."
    return None


class ApiKeyDialog(QDialog):
    saved = Signal()
    _test_done = Signal(bool, str)

    def __init__(
        self,
        secrets: SecretStore,
        secret_name: str,
        test: Callable[[], str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        ensure_app()
        super().__init__(parent)
        self._secrets = secrets
        self._name = secret_name
        self._test = test
        self._busy = False
        self._main = MainThread(self)
        self.setWindowTitle("Gloss: connect to the translation service")
        self.setMinimumWidth(460)

        intro = QLabel(INTRO)
        intro.setWordWrap(True)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("Paste your API key here")
        self.key_edit.textChanged.connect(lambda _t: self._set_message(""))
        self.show_box = QCheckBox("Show the key while I check it")
        self.show_box.toggled.connect(
            lambda on: self.key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )
        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        label = "Save and test" if test is not None else "Save"
        self.save_button = QPushButton(label)
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self._on_save)
        buttons = QDialogButtonBox()
        buttons.addButton(self.save_button, QDialogButtonBox.ButtonRole.AcceptRole)
        cancel = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel.clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.key_edit)
        layout.addWidget(self.show_box)
        layout.addWidget(self.message)
        layout.addWidget(buttons)
        self._test_done.connect(self._on_test_done)

    def _set_message(self, text: str, error: bool = False) -> None:
        colour = "#c0392b" if error else "#2e7d32"
        self.message.setStyleSheet(f"color: {colour};")
        self.message.setText(text)

    def _on_save(self) -> None:
        if self._busy:
            return
        pasted = self.key_edit.text().strip()
        problem = looks_like_a_key(pasted)
        if problem:
            self._set_message(problem, error=True)
            return
        try:
            self._secrets.set(self._name, pasted)
        except GlossError as exc:
            self._set_message(f"Windows could not store the key: {exc}", error=True)
            return
        except Exception as exc:
            log.warning("storing the key failed: %s", type(exc).__name__)
            self._set_message(f"Windows could not store the key ({type(exc).__name__}).", True)
            return
        self.key_edit.clear()  # The key now lives only in the store.
        self.saved.emit()
        if self._test is None:
            self.accept()
            return
        self._busy = True
        self.save_button.setEnabled(False)
        self._set_message("Key saved. Checking it with the provider (no text is sent)...")
        test = self._test

        def work() -> None:
            try:
                english = test()
            except Exception as exc:
                reason = _plain_error(exc)  # Python clears `exc` when this block ends.
                self._main.post(lambda: self._test_done.emit(False, reason))
                return
            self._main.post(lambda: self._test_done.emit(True, english))

        threading.Thread(target=work, name="gloss-key-test", daemon=True).start()

    def _on_test_done(self, ok: bool, text: str) -> None:
        self._busy = False
        self.save_button.setEnabled(True)
        if ok:
            self._set_message(f"It works. A test line came back as: {text}")
            self.accept()
        else:
            self._set_message(
                f"The key is saved, but the test failed: {text} Check the key and try again.",
                error=True,
            )


def _plain_error(exc: Exception) -> str:
    text = str(exc).strip()
    return text if text else type(exc).__name__
