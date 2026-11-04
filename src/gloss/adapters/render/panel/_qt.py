"""The panel's Qt code. Imported by ``PanelRenderer.open()`` and nothing else."""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from collections.abc import Callable, Sequence
from typing import Any

from PySide6.QtCore import (
    QAbstractListModel,
    QEvent,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QRect,
    QSettings,
    QSize,
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from gloss.adapters.render.panel import (
    COPIED_FLASH_MS,
    DOT_COLOURS,
    STATE_SAVE_DELAY_MS,
    STATE_WORDS,
    _Entry,
    _PanelSettings,
)
from gloss.core.contracts import Health
from gloss.services.capture_exclusion import exclude_from_capture

log = logging.getLogger(__name__)


def log_failure(logger: logging.Logger, message: str, *args: object) -> None:
    """Log the kind of the exception being handled, never its text (it could hold chat text).
    The full traceback is added only when debug logging is on."""
    kind = sys.exc_info()[0]
    logger.warning(
        message + " (%s)",
        *args,
        kind.__name__ if kind else "error",
        exc_info=logger.isEnabledFor(logging.DEBUG),
    )


# ---- native window helpers -----------------------------------------------------------------


def _exclude_hwnd(hwnd: int) -> bool:
    """Hide one native window from every screen capture. The one call site for the helper."""
    return exclude_from_capture(hwnd)


class _NativeGuard(QObject):
    """Excludes a window from capture when it is created, shown, or given a new native handle.

    Qt recreates the native window when some flags change or when it moves between screens, and
    a new handle starts with no exclusion, so the call is repeated on every such event.
    """

    def __init__(self, widget: QWidget, on_result: Callable[[bool], None]) -> None:
        super().__init__(widget)
        self._widget = widget
        self._on_result = on_result
        widget.installEventFilter(self)

    def apply(self) -> bool:
        try:
            ok = _exclude_hwnd(int(self._widget.winId())) if sys.platform == "win32" else True
        except RuntimeError:  # The C++ window is already gone.
            return False
        self._on_result(ok)
        return ok

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched is self._widget and event.type() in (QEvent.Type.WinIdChange, QEvent.Type.Show):
            self.apply()
        return False


def ensure_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        if sys.platform != "win32" and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        ):
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication(["gloss"])
        app.setQuitOnLastWindowClosed(False)
    return app  # type: ignore[return-value]


class Invoker(QObject):
    """Runs callables on the thread this object lives in (the Qt main thread)."""

    _call = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._call.connect(self._run)

    @Slot(object)
    def _run(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            log_failure(log, "panel: a queued update failed")

    def post(self, fn: Callable[[], None]) -> None:
        self._call.emit(fn)


class _EntryModel(QAbstractListModel):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.entries: list[_Entry] = []
        self._by_id: dict[str, _Entry] = {}

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008, N802
        return 0 if parent.isValid() else len(self.entries)

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = 0) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.entries):
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self.entries[index.row()].english
        if role == Qt.ItemDataRole.UserRole:
            return self.entries[index.row()]
        return None

    def upsert(self, entries: Sequence[_Entry], history_max: int) -> None:
        fresh: list[_Entry] = []
        for entry in entries:
            old = self._by_id.get(entry.line_id)
            if old is None:
                fresh.append(entry)
                self._by_id[entry.line_id] = entry
                continue
            row = self.entries.index(old)
            self.entries[row] = entry
            self._by_id[entry.line_id] = entry
            self.dataChanged.emit(self.index(row), self.index(row))
        if fresh:
            start = len(self.entries)
            self.beginInsertRows(QModelIndex(), start, start + len(fresh) - 1)
            self.entries.extend(fresh)
            self.endInsertRows()
        extra = len(self.entries) - history_max
        if extra > 0:
            self.beginRemoveRows(QModelIndex(), 0, extra - 1)
            for gone in self.entries[:extra]:
                if self._by_id.get(gone.line_id) is gone:
                    del self._by_id[gone.line_id]
            del self.entries[:extra]
            self.endRemoveRows()

    def wipe(self) -> None:
        self.beginResetModel()
        self.entries.clear()
        self._by_id.clear()
        self.endResetModel()

    def touch(self, entry: _Entry) -> None:
        try:
            row = self.entries.index(entry)
        except ValueError:
            return
        self.dataChanged.emit(self.index(row), self.index(row))


class _Delegate(QStyledItemDelegate):
    PAD = 8
    GAP = 3

    def __init__(self, view: QListView, english: QFont, original: QFont, small: QFont) -> None:
        super().__init__(view)
        self._view = view
        self.english_font = english
        self.original_font = original
        self.small_font = small

    def _measure(self, entry: _Entry, width: int) -> tuple[int, int, int, int, int]:
        if entry.sizes is not None and entry.sizes[0] == width:
            return entry.sizes
        inner = max(40, width - 2 * self.PAD)
        flags = int(Qt.TextFlag.TextWordWrap | Qt.TextFlag.TextWrapAnywhere)

        def height(font: QFont, text: str) -> int:
            if not text:
                return 0
            return QFontMetrics(font).boundingRect(QRect(0, 0, inner, 100000), flags, text).height()

        sizes = (
            width,
            height(self.original_font, entry.original),
            height(self.english_font, entry.english),
            height(self.small_font, " | ".join(entry.tags)),
            0,
        )
        entry.sizes = sizes
        return sizes

    def sizeHint(  # noqa: N802
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        entry = index.data(Qt.ItemDataRole.UserRole)
        width = self._view.viewport().width()
        if not isinstance(entry, _Entry) or width <= 0:
            return QSize(max(width, 0), 24)
        _, h_orig, h_eng, h_tags, _ = self._measure(entry, width)
        parts = [h for h in (h_orig, h_eng, h_tags) if h]
        total = sum(parts) + self.GAP * max(0, len(parts) - 1) + 2 * self.PAD
        return QSize(width, max(total, 24))

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        entry = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(entry, _Entry):
            return
        rect = option.rect
        _, h_orig, h_eng, h_tags, _ = self._measure(entry, rect.width())
        painter.save()
        hovered = bool(option.state & option.state.State_MouseOver)
        background = "#3a4a63" if entry.flash else ("#2a2a33" if hovered else "#202026")
        painter.fillRect(rect, QColor(background))
        painter.setPen(QPen(QColor("#33333c")))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        flags = int(
            Qt.TextFlag.TextWordWrap | Qt.TextFlag.TextWrapAnywhere | Qt.AlignmentFlag.AlignLeft
        )
        x, inner = rect.x() + self.PAD, rect.width() - 2 * self.PAD
        y = rect.y() + self.PAD

        def draw(font: QFont, colour: str, text: str, h: int) -> None:
            nonlocal y
            if not h:
                return
            painter.setFont(font)
            painter.setPen(QColor(colour))
            painter.drawText(QRect(x, y, inner, h), flags, text)
            y += h + self.GAP

        draw(self.original_font, "#8a8a95", entry.original, h_orig)
        if entry.failed:
            draw(self.english_font, "#e5484d", entry.english, h_eng)
        else:
            draw(self.english_font, "#f2f2f5", entry.english, h_eng)
        draw(self.small_font, "#f5a623", " | ".join(entry.tags), h_tags)
        painter.restore()


class _Dot(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._colour = QColor(DOT_COLOURS["ok"])
        self.setFixedSize(14, 14)

    def set_state(self, state: str) -> None:
        self._colour = QColor(DOT_COLOURS.get(state, DOT_COLOURS["down"]))
        self.update()

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(self._colour)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(1, 1, 12, 12)


class PanelWindow(QWidget):
    HINT = "Click a line to copy the English."

    def __init__(self, settings: _PanelSettings) -> None:
        flags = (
            Qt.WindowType.Window
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowMinMaxButtonsHint
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        super().__init__(None, flags)
        self._cfg = settings
        self.setWindowTitle("Gloss")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setStyleSheet("QWidget#panel { background: #17171c; }")
        self.setObjectName("panel")
        self.resize(settings.width, settings.height)
        self.setMinimumSize(220, 160)

        self.exclusion_ok = True
        self.guard = _NativeGuard(self, self._exclusion_result)

        base = QFont(QApplication.font())
        english = QFont(base)
        english.setPixelSize(settings.font_size)
        original = QFont(base)
        original.setPixelSize(settings.original_size)
        small = QFont(base)
        small.setPixelSize(max(6, settings.original_size - 1))
        self._status_font = QFont(base)
        self._status_font.setPixelSize(max(10, settings.original_size))

        self.model = _EntryModel(self)
        self.list = QListView(self)
        self.list.setModel(self.model)
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.list.setUniformItemSizes(False)
        self.list.setResizeMode(QListView.ResizeMode.Adjust)
        self.list.setMouseTracking(True)
        self.list.setStyleSheet("QListView { background: #17171c; }")
        self._delegate = _Delegate(self.list, english, original, small)
        self.list.setItemDelegate(self._delegate)
        self.list.clicked.connect(self._copy)
        self.list.entered.connect(self._hover)
        self.list.viewport().installEventFilter(self)

        self.dot = _Dot()
        self.status = QLabel("Starting")
        self.status.setFont(self._status_font)
        self.status.setStyleSheet("color: #d0d0d8;")
        self.status.setWordWrap(True)
        top = QHBoxLayout()
        top.setContentsMargins(8, 6, 8, 6)
        top.addWidget(self.dot)
        top.addWidget(self.status, 1)

        self.banner = QLabel("")
        self.banner.setFont(self._status_font)
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet(
            "background: #f5a623; color: #1b1300; padding: 6px 8px; font-weight: bold;"
        )
        self.banner.setVisible(False)

        self.footer = QLabel(self.HINT)
        self.footer.setFont(self._status_font)
        self.footer.setWordWrap(True)
        self.footer.setStyleSheet("color: #8a8a95;")
        clear = QPushButton("Clear")
        clear.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        clear.setFont(self._status_font)
        clear.clicked.connect(self.model.wipe)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(8, 4, 8, 6)
        bottom.addWidget(self.footer, 1)
        bottom.addWidget(clear)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(top)
        layout.addWidget(self.banner)
        layout.addWidget(self.list, 1)
        layout.addLayout(bottom)

        # Follow the newest line unless the user scrolled up.
        self._stick = True
        self._programmatic = False
        bar = self.list.verticalScrollBar()
        bar.valueChanged.connect(self._on_scrolled)
        bar.rangeChanged.connect(self._on_range)

        self._footer_timer = QTimer(self)
        self._footer_timer.setSingleShot(True)
        self._footer_timer.timeout.connect(self._reset_footer)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self.save_state)
        self._baseline: bytes | None = None
        self._placed = False
        self._settings_store: QSettings | None = None

        # Native handle now, so exclusion is in place before the window is ever shown.
        self.guard.apply()

    # -- capture exclusion
    def _exclusion_result(self, ok: bool) -> None:
        self.exclusion_ok = ok

    # -- scrolling
    def _on_scrolled(self, value: int) -> None:
        if self._programmatic:
            return
        bar = self.list.verticalScrollBar()
        self._stick = value >= bar.maximum() - 4

    def _on_range(self, _low: int, high: int) -> None:
        if self._stick:
            self._programmatic = True
            try:
                self.list.verticalScrollBar().setValue(high)
            finally:
                self._programmatic = False

    # -- content
    def add(self, entries: Sequence[_Entry]) -> None:
        self.model.upsert(entries, self._cfg.history_max)
        if self._stick:
            self._on_range(0, self.list.verticalScrollBar().maximum())

    def _copy(self, index: QModelIndex) -> None:
        entry = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(entry, _Entry):
            return
        text = entry.english or entry.original
        QGuiApplication.clipboard().setText(text)
        entry.flash = True
        self.model.touch(entry)
        self.footer.setText("Copied to the clipboard.")
        self._footer_timer.start(COPIED_FLASH_MS)
        QTimer.singleShot(COPIED_FLASH_MS, lambda e=entry: self._unflash(e))

    def _unflash(self, entry: _Entry) -> None:
        entry.flash = False
        with contextlib.suppress(RuntimeError):
            self.model.touch(entry)

    def _hover(self, index: QModelIndex) -> None:
        entry = index.data(Qt.ItemDataRole.UserRole)
        if isinstance(entry, _Entry) and entry.note and not self._footer_timer.isActive():
            self.footer.setText(entry.note)
        elif not self._footer_timer.isActive():
            self.footer.setText(self.HINT)

    def _reset_footer(self) -> None:
        self.footer.setText(self.HINT)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched is self.list.viewport() and event.type() == QEvent.Type.Leave:
            if not self._footer_timer.isActive():
                self.footer.setText(self.HINT)
        return False

    def set_status(self, health: Health, banner: str) -> None:
        self.dot.set_state(health.state)
        word = STATE_WORDS.get(health.state, "Not working")
        detail = health.detail.strip()
        self.status.setText(f"{word}: {detail}" if detail else word)
        self.banner.setText(banner)
        self.banner.setVisible(bool(banner.strip()))

    # -- remembered position
    def _store(self) -> QSettings:
        if self._settings_store is None:
            path = self._cfg.state_file
            with contextlib.suppress(OSError):
                os.makedirs(os.path.dirname(path), exist_ok=True)
            self._settings_store = QSettings(path, QSettings.Format.IniFormat)
        return self._settings_store

    def restore_state(self) -> None:
        """Place the window where the user left it, or at the bottom right of the screen."""
        restored = False
        try:
            data = self._store().value("panel/geometry")
            if data:
                restored = bool(self.restoreGeometry(data))
        except Exception:
            log_failure(log, "panel: could not restore the saved position")
        if not restored:
            screen = QGuiApplication.primaryScreen()
            if screen is not None:
                area = screen.availableGeometry()
                x = area.right() - self.width() - 24
                y = area.bottom() - self.height() - 24
                self.move(max(area.left(), x), max(area.top(), y))
        self._placed = True

    def showEvent(self, event: Any) -> None:  # noqa: N802
        super().showEvent(event)
        if self._baseline is None:
            QTimer.singleShot(STATE_SAVE_DELAY_MS, self._set_baseline)

    def _set_baseline(self) -> None:
        with contextlib.suppress(RuntimeError):
            self._baseline = bytes(self.saveGeometry())

    def moveEvent(self, event: Any) -> None:  # noqa: N802
        super().moveEvent(event)
        self._schedule_save()

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._schedule_save()
        self.list.doItemsLayout()

    def _schedule_save(self) -> None:
        if self._placed and self._baseline is not None and self.isVisible():
            self._save_timer.start(STATE_SAVE_DELAY_MS)

    def save_state(self) -> None:
        """Write the position, only if the user changed it. Never raises."""
        try:
            current = bytes(self.saveGeometry())
            if self._baseline is None or current == self._baseline:
                return
            store = self._store()
            store.setValue("panel/geometry", self.saveGeometry())
            store.sync()
            self._baseline = current
        except Exception:
            log_failure(log, "panel: could not save the position")

    def closeEvent(self, event: Any) -> None:  # noqa: N802
        # The X button hides the panel. The tray menu brings it back.
        event.ignore()
        self.save_state()
        self.hide()
