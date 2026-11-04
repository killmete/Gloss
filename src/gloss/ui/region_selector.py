"""Pick the part of the screen to translate.

One frozen window covers each monitor, showing that monitor as it looked a moment ago, dimmed.
The user drags a rectangle on any of them. The result is a ``Rect`` in physical desktop pixels
(what the capture adapters expect) and the monitor's Windows device name, such as
``\\\\.\\DISPLAY1``, which is the ``monitor_id`` the capture adapters use.
The id comes from ``services.monitors.list_monitors()`` matched to the selected rectangle.
Qt's ``QScreen.name()`` is the monitor's model name on Windows (for example ``GS24F14``), so it
is never used.

A selection stays inside one monitor. Qt places each monitor's logical rectangle at the same
origin as its physical one, but scales its size by the monitor's scale (``devicePixelRatio``),
so ``logical_to_physical_rect`` converts a drag inside a monitor window with that scale. The
capture-engineer may provide a shared version of this maths later. If so, replace the body of
``logical_to_physical_rect`` only.
"""

from __future__ import annotations

import contextlib
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QObject, QPoint, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QPainter, QPen, QPixmap, QScreen
from PySide6.QtWidgets import QApplication, QWidget

from gloss.core.contracts import MonitorInfo, Rect
from gloss.services.monitors import list_monitors, monitor_with_most_overlap
from gloss.ui.native import ensure_app, exclude_widget

log = logging.getLogger(__name__)

MIN_SELECTION_PX = 12  # logical pixels; smaller drags are ignored as accidental clicks
HELP_TEXT = "Drag to choose the area to translate.  Esc or right-click to cancel."


@dataclass(frozen=True)
class MonitorGeometry:
    """One monitor: physical origin (x, y) shared with Qt's logical origin, and its scale."""

    x: int
    y: int
    dpr: float


def logical_to_physical_rect(
    left: float, top: float, width: float, height: float, monitor: MonitorGeometry
) -> Rect:
    """Convert a rectangle inside a monitor window (logical pixels, relative to the monitor's
    top-left) to physical desktop pixels. Rounds outwards, so the area picked is never cut."""
    x0 = monitor.x + math.floor(left * monitor.dpr)
    y0 = monitor.y + math.floor(top * monitor.dpr)
    x1 = monitor.x + math.ceil((left + width) * monitor.dpr)
    y1 = monitor.y + math.ceil((top + height) * monitor.dpr)
    return Rect(x0, y0, max(0, x1 - x0), max(0, y1 - y0))


def monitor_id_for(rect: Rect, monitors: Sequence[MonitorInfo] | None = None) -> str:
    """The capture adapters' id for the monitor holding most of ``rect``, or "" if none."""
    try:
        found = monitor_with_most_overlap(list_monitors() if monitors is None else monitors, rect)
    except Exception:
        return ""
    return found.monitor_id if found is not None else ""


def drag_rect(a: QPoint, b: QPoint) -> QRect:
    return QRect(
        min(a.x(), b.x()), min(a.y(), b.y()), abs(a.x() - b.x()) + 1, abs(a.y() - b.y()) + 1
    )


class _SelectionWindow(QWidget):
    chosen = Signal(object, str)  # Rect (physical), monitor id
    cancelled = Signal()

    def __init__(self, screen: QScreen, frozen: QPixmap) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.qscreen = screen
        self._frozen = frozen
        self._start: QPoint | None = None
        self._now: QPoint | None = None
        self.setScreen(screen)
        self.setGeometry(screen.geometry())
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        exclude_widget(self)

    def monitor(self) -> MonitorGeometry:
        g = self.qscreen.geometry()
        return MonitorGeometry(g.x(), g.y(), float(self.qscreen.devicePixelRatio()) or 1.0)

    def selection(self) -> QRect | None:
        if self._start is None or self._now is None:
            return None
        return drag_rect(self._start, self._now)

    def paintEvent(self, event: object) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.drawPixmap(self.rect(), self._frozen)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 110))
        selection = self.selection()
        if selection is not None:
            painter.drawPixmap(selection, self._frozen, self._source_rect(selection))
            painter.setPen(QPen(QColor("#3ecf6e"), 2))
            painter.drawRect(selection)
        font = QFont(QApplication.font())
        font.setPixelSize(18)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(
            QRectF(0, 20, self.width(), 40), int(Qt.AlignmentFlag.AlignCenter), HELP_TEXT
        )

    def _source_rect(self, selection: QRect) -> QRect:
        scale = self._frozen.width() / max(1, self.width())  # device pixels per logical
        return QRect(
            round(selection.x() * scale),
            round(selection.y() * scale),
            round(selection.width() * scale),
            round(selection.height() * scale),
        )

    def mousePressEvent(self, event: object) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:  # type: ignore[attr-defined]
            self.cancelled.emit()
            return
        self._start = self._now = event.position().toPoint()  # type: ignore[attr-defined]
        self.update()

    def mouseMoveEvent(self, event: object) -> None:  # noqa: N802
        if self._start is not None:
            self._now = event.position().toPoint()  # type: ignore[attr-defined]
            self.update()

    def mouseReleaseEvent(self, event: object) -> None:  # noqa: N802
        selection = self.selection()
        self._start = self._now = None
        self.update()
        if selection is None:
            return
        if selection.width() < MIN_SELECTION_PX or selection.height() < MIN_SELECTION_PX:
            return  # An accidental click: keep waiting.
        rect = logical_to_physical_rect(
            selection.x(), selection.y(), selection.width(), selection.height(), self.monitor()
        )
        self.chosen.emit(rect, monitor_id_for(rect))

    def keyPressEvent(self, event: object) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:  # type: ignore[attr-defined]
            self.cancelled.emit()


class RegionSelector(QObject):
    """Shows a frozen selection window on every monitor.

    ``selected(Rect, monitor_id)`` or ``cancelled()`` fires once per ``start()``. Must be
    created and used on the Qt main thread.
    """

    selected = Signal(object, str)
    cancelled = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._windows: list[_SelectionWindow] = []

    @property
    def active(self) -> bool:
        return bool(self._windows)

    def start(self) -> None:
        if self._windows:
            return
        ensure_app()
        screens = QGuiApplication.screens()
        # Grab every monitor first, so no selection window ends up in another's picture.
        frozen = [(s, s.grabWindow(0)) for s in screens]
        for screen, pixmap in frozen:
            window = _SelectionWindow(screen, pixmap)
            window.chosen.connect(self._on_chosen)
            window.cancelled.connect(self._on_cancelled)
            self._windows.append(window)
        for window in self._windows:
            window.show()
        under_cursor = QGuiApplication.screenAt(QCursor.pos())
        for window in self._windows:
            if window.qscreen == under_cursor:
                window.raise_()
                window.activateWindow()
                window.setFocus()
                break

    def _finish(self) -> None:
        windows, self._windows = self._windows, []
        for window in windows:
            with contextlib.suppress(RuntimeError):
                window.close()

    def _on_chosen(self, rect: Rect, monitor_id: str) -> None:
        self._finish()
        self.selected.emit(rect, monitor_id)

    def _on_cancelled(self) -> None:
        self._finish()
        self.cancelled.emit()

    def cancel(self) -> None:
        if self._windows:
            self._on_cancelled()
