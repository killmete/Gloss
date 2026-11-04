"""The tray icon and its menu: the app's front door.

The icon carries a small status dot (green working, amber limited, red not working), and the
tooltip and the first menu line say why in plain words. Everything here runs on the Qt main
thread. ``GlossShell`` (shell.py) wraps it for callers on other threads.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, QPointF, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from gloss.core.contracts import Health
from gloss.ui.native import ensure_app

log = logging.getLogger(__name__)

DOT_COLOURS = {"ok": "#3ecf6e", "degraded": "#f5a623", "down": "#e5484d"}
STATE_WORDS = {"ok": "Working", "degraded": "Limited", "down": "Not working"}
MODE_LABELS = (("panel", "Side panel"), ("overlay", "Over the text"), ("both", "Both"))

LOW_QUALITY_WARNING = (
    "Low-quality reading is a backup for when the normal reader cannot start. It is quicker "
    "to get going, but it may get words wrong or miss them, so translations can be wrong "
    "too. Gloss shows a warning on every line while it is on.\n\n"
    "Turn it on for this session only?"
)


def status_text(health: Health) -> str:
    word = STATE_WORDS.get(health.state, "Not working")
    detail = health.detail.strip()
    return f"{word}: {detail}" if detail else word


def make_icon(state: str, size: int = 64) -> QIcon:
    """Draw the app icon (a rounded square with a G) with the status dot in the corner."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#2f3a52"))
    painter.drawRoundedRect(2, 2, size - 4, size - 4, size * 0.22, size * 0.22)
    font = QFont(QApplication.font())
    font.setBold(True)
    font.setPixelSize(int(size * 0.62))
    painter.setFont(font)
    painter.setPen(QColor("#f2f2f5"))
    painter.drawText(pixmap.rect().adjusted(0, 0, 0, -2), int(Qt.AlignmentFlag.AlignCenter), "G")
    radius = size * 0.22
    centre = QPointF(size - radius - 1, size - radius - 1)
    painter.setPen(QColor("#101014"))
    painter.setBrush(QColor(DOT_COLOURS.get(state, DOT_COLOURS["down"])))
    painter.drawEllipse(centre, radius, radius)
    painter.end()
    return QIcon(pixmap)


def _default_confirm(title: str, text: str) -> bool:
    box = QMessageBox(QMessageBox.Icon.Warning, title, text)
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    return box.exec() == QMessageBox.StandardButton.Yes


class TrayController(QObject):
    pause_toggled = Signal(bool)  # True = paused
    select_region_requested = Signal()
    mode_selected = Signal(str)
    translations_visible_toggled = Signal(bool)
    low_quality_toggled = Signal(bool)
    settings_requested = Signal()
    api_key_requested = Signal()
    quit_requested = Signal()

    def __init__(
        self,
        *,
        mode: str = "panel",
        confirm: Callable[[str, str], bool] = _default_confirm,
        parent: QObject | None = None,
    ) -> None:
        ensure_app()
        super().__init__(parent)
        self._confirm = confirm
        self._health = Health("ok")
        self._paused = False
        self.tray = QSystemTrayIcon(make_icon("ok"))
        self.menu = QMenu()

        self.status_action = self.menu.addAction(status_text(self._health))
        self.status_action.setEnabled(False)
        self.menu.addSeparator()

        self.pause_action = self.menu.addAction("Pause")
        self.pause_action.triggered.connect(self._on_pause)
        self.region_action = self.menu.addAction("Choose screen area...")
        self.region_action.triggered.connect(self.select_region_requested.emit)
        self.visible_action = self.menu.addAction("Show translations")
        self.visible_action.setCheckable(True)
        self.visible_action.setChecked(True)
        self.visible_action.toggled.connect(self.translations_visible_toggled.emit)

        mode_menu = self.menu.addMenu("Show translations in")
        self._mode_group = QActionGroup(self.menu)
        self._mode_actions: dict[str, QAction] = {}
        for value, label in MODE_LABELS:
            action = mode_menu.addAction(label)
            action.setCheckable(True)
            action.setData(value)
            self._mode_group.addAction(action)
            self._mode_actions[value] = action
            action.triggered.connect(lambda _c=False, v=value: self.mode_selected.emit(v))
        self.set_mode(mode)

        self.low_quality_action = self.menu.addAction("Low-quality reading (this session only)")
        self.low_quality_action.setCheckable(True)
        self.low_quality_action.triggered.connect(self._on_low_quality)
        self.menu.addSeparator()
        self.menu.addAction("Settings...").triggered.connect(self.settings_requested.emit)
        self.menu.addAction("API key...").triggered.connect(self.api_key_requested.emit)
        self.menu.addSeparator()
        self.menu.addAction("Quit Gloss").triggered.connect(self.quit_requested.emit)

        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_activated)
        self.set_health(self._health)

    # -- show / hide
    @staticmethod
    def available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def show(self) -> None:
        self.tray.show()

    def hide(self) -> None:
        self.tray.hide()

    # -- state shown to the user
    def set_health(self, health: Health) -> None:
        self._health = health
        self.tray.setIcon(make_icon(health.state))
        self.tray.setToolTip(f"Gloss - {status_text(health)}")
        self.status_action.setText(status_text(health))
        self.status_action.setIcon(_dot_icon(health.state))

    @property
    def health(self) -> Health:
        return self._health

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self.pause_action.setText("Resume" if paused else "Pause")

    def set_mode(self, mode: str) -> None:
        action = self._mode_actions.get(mode)
        if action is not None:
            action.setChecked(True)

    def set_low_quality(self, on: bool) -> None:
        self.low_quality_action.blockSignals(True)
        self.low_quality_action.setChecked(on)
        self.low_quality_action.blockSignals(False)

    def set_translations_visible(self, visible: bool) -> None:
        self.visible_action.blockSignals(True)
        self.visible_action.setChecked(visible)
        self.visible_action.blockSignals(False)

    def notify(self, title: str, text: str, level: str = "info") -> None:
        icons = {
            "info": QSystemTrayIcon.MessageIcon.Information,
            "warning": QSystemTrayIcon.MessageIcon.Warning,
            "error": QSystemTrayIcon.MessageIcon.Critical,
        }
        if self.tray.isVisible():
            self.tray.showMessage(title, text, icons.get(level, icons["info"]), 8000)

    # -- user actions
    def _on_pause(self) -> None:
        self.set_paused(not self._paused)
        self.pause_toggled.emit(self._paused)

    def _on_low_quality(self, checked: bool) -> None:
        if checked and not self._confirm("Gloss: low-quality reading", LOW_QUALITY_WARNING):
            self.set_low_quality(False)
            return
        self.low_quality_toggled.emit(checked)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.visible_action.toggle()


def _dot_icon(state: str) -> QIcon:
    pixmap = QPixmap(16, 16)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(DOT_COLOURS.get(state, DOT_COLOURS["down"])))
    painter.drawEllipse(2, 2, 12, 12)
    painter.end()
    return QIcon(pixmap)
