"""The overlay's Qt code. Imported by ``OverlayRenderer.open()`` and nothing else."""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QEvent, QObject, QRect, QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPen, QScreen
from PySide6.QtWidgets import QApplication, QWidget

from gloss.adapters.render.overlay import (
    CHIP_GAP,
    CHIP_PAD,
    CHIP_RADIUS,
    ScreenGeometry,
    _Line,
    _OverlaySettings,
    physical_to_logical,
    pick_screen,
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


GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10


# ---- native window helpers ----------------------------------------------------------------


def _load_user32() -> Any:
    if sys.platform != "win32":
        return None
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    user32.SetWindowDisplayAffinity.restype = ctypes.c_int
    user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowPos.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    user32.SetWindowPos.restype = ctypes.c_int
    return user32


_USER32 = _load_user32()


def _exclude_hwnd(hwnd: int) -> bool:
    """Hide one native window from every screen capture. The one call site for the helper."""
    return exclude_from_capture(hwnd)


def _click_through_hwnd(hwnd: int) -> bool:
    """Make one native window layered, transparent to the mouse, and unable to take focus."""
    if _USER32 is None or not hwnd:
        return False
    handle = ctypes.c_void_p(hwnd)
    wanted = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    current = _USER32.GetWindowLongPtrW(handle, GWL_EXSTYLE)
    if current & wanted != wanted:
        _USER32.SetWindowLongPtrW(handle, GWL_EXSTYLE, current | wanted)
    return bool(_USER32.GetWindowLongPtrW(handle, GWL_EXSTYLE) & wanted == wanted)


def _raise_hwnd(hwnd: int) -> None:
    if _USER32 is None or not hwnd:
        return
    _USER32.SetWindowPos(
        ctypes.c_void_p(hwnd),
        ctypes.c_void_p(HWND_TOPMOST),
        0,
        0,
        0,
        0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
    )


def _ensure_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        if sys.platform != "win32" and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        ):
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication(["gloss"])
        app.setQuitOnLastWindowClosed(False)
    return app  # type: ignore[return-value]


class _Invoker(QObject):
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
            log_failure(log, "overlay: a queued update failed")

    def post(self, fn: Callable[[], None]) -> None:
        self._call.emit(fn)


def screen_geometry(screen: QScreen) -> ScreenGeometry:
    logical = screen.geometry()
    dpr = float(screen.devicePixelRatio()) or 1.0
    return ScreenGeometry(
        logical.x(),
        logical.y(),
        round(logical.width() * dpr),
        round(logical.height() * dpr),
        dpr,
    )


@dataclass(frozen=True)
class _Chip:
    rect: QRectF
    text: str
    low_quality: bool


class _ScreenWindow(QWidget):
    """The transparent window over one monitor."""

    def __init__(self, screen: QScreen, cfg: _OverlaySettings) -> None:
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        super().__init__(None, flags)
        self._cfg = cfg
        self.qscreen = screen
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setScreen(screen)
        self.setGeometry(screen.geometry())
        font = QFont(QApplication.font())
        font.setPixelSize(cfg.font_size)
        self._font = font
        self._banner_font = QFont(font)
        self._banner_font.setBold(True)
        self.chips: tuple[_Chip, ...] = ()
        self.banner_rect: QRectF | None = None
        self.banner_text = ""
        self.exclusion_ok = True
        self.click_through_ok = True
        self._signature: object = None
        self.installEventFilter(self)
        self.prepare_native()

    # Called when the native window is created, shown, or recreated by Qt.
    def prepare_native(self) -> None:
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
        except RuntimeError:
            return
        self.exclusion_ok = _exclude_hwnd(hwnd)
        self.click_through_ok = _click_through_hwnd(hwnd)

    def raise_topmost(self) -> None:
        if sys.platform != "win32" or not self.isVisible():
            return
        try:
            hwnd = int(self.winId())
        except RuntimeError:
            return
        _raise_hwnd(hwnd)
        # Cheap, and covers a native window that Qt recreated without telling us.
        self.exclusion_ok = _exclude_hwnd(hwnd)
        self.click_through_ok = _click_through_hwnd(hwnd)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched is self and event.type() in (QEvent.Type.WinIdChange, QEvent.Type.Show):
            self.prepare_native()
        return False

    def physical(self) -> ScreenGeometry:
        return screen_geometry(self.qscreen)

    def set_content(self, lines: Iterable[_Line], banner: str, is_primary: bool) -> None:
        """Lay out chips for the lines on this monitor. Repaints only if something changed."""
        geometry = self.physical()
        metrics = QFontMetrics(self._font)
        max_width = max(80.0, geometry.logical_w * self._cfg.max_width_ratio)
        flags = int(Qt.TextFlag.TextWordWrap | Qt.TextFlag.TextWrapAnywhere)
        placed: list[QRectF] = []
        chips: list[_Chip] = []
        for line in sorted(lines, key=lambda ln: (ln.bbox.y, ln.bbox.x)):
            x, y, w, _h = physical_to_logical(line.bbox, geometry)
            natural = metrics.horizontalAdvance(line.text) + 2 * CHIP_PAD + 2
            width = min(max(w, min(natural, max_width)), max_width)
            width = max(width, 2 * CHIP_PAD + 8)
            inner = int(width - 2 * CHIP_PAD)
            text_h = metrics.boundingRect(QRect(0, 0, inner, 100000), flags, line.text).height()
            rect = QRectF(x, y, width, text_h + 2 * CHIP_PAD)
            for other in placed:  # Slide down past any chip already there.
                if rect.intersects(other):
                    rect.moveTop(other.bottom() + CHIP_GAP)
            rect.moveLeft(max(0.0, min(rect.x(), geometry.logical_w - rect.width())))
            rect.moveTop(max(0.0, min(rect.y(), geometry.logical_h - rect.height())))
            placed.append(rect)
            chips.append(_Chip(rect, line.text, line.low_quality))

        banner_rect = None
        banner_text = banner if is_primary else ""
        if banner_text:
            bm = QFontMetrics(self._banner_font)
            bw = min(bm.horizontalAdvance(banner_text) + 24, geometry.logical_w - 20)
            banner_rect = QRectF((geometry.logical_w - bw) / 2, 8, bw, bm.height() + 12)

        signature = (tuple((c.rect.getRect(), c.text, c.low_quality) for c in chips), banner_text)
        if signature == self._signature:
            return
        self._signature = signature
        self.chips = tuple(chips)
        self.banner_rect, self.banner_text = banner_rect, banner_text
        self.update()

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(self._font)
        flags = int(
            Qt.TextFlag.TextWordWrap | Qt.TextFlag.TextWrapAnywhere | Qt.AlignmentFlag.AlignLeft
        )
        for chip in self.chips:
            painter.setBrush(QColor(16, 16, 20, 232))
            if chip.low_quality:
                painter.setPen(QPen(QColor("#f5a623"), 2))
            else:
                painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(chip.rect, CHIP_RADIUS, CHIP_RADIUS)
            painter.setPen(QColor("#f5f5f8"))
            inner = chip.rect.adjusted(CHIP_PAD, CHIP_PAD, -CHIP_PAD, -CHIP_PAD)
            painter.drawText(inner, flags, chip.text)
        if self.banner_rect is not None:
            painter.setFont(self._banner_font)
            painter.setBrush(QColor("#f5a623"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(self.banner_rect, 6, 6)
            painter.setPen(QColor("#1b1300"))
            painter.drawText(self.banner_rect, int(Qt.AlignmentFlag.AlignCenter), self.banner_text)


class OverlayController:
    """Owns the windows, timers and lines. Everything here runs on the Qt main thread, except
    ``post``, which may be called from any thread."""

    def __init__(self, cfg: _OverlaySettings) -> None:
        self._cfg = cfg
        self.invoker: _Invoker | None = None
        self.windows: tuple[_ScreenWindow, ...] = ()
        self.lines: dict[str, _Line] = {}
        self.visible = False
        self.banner = ""
        self.timer: QTimer | None = None
        self._rebuild_timer: QTimer | None = None
        self._screen_signals = False
        self._open = False

    def open(self) -> None:
        try:
            _ensure_app()
            self.invoker = _Invoker()
            self._build_windows()
            self.timer = QTimer()
            self.timer.setInterval(self._cfg.reassert_ms)
            self.timer.timeout.connect(self.tick)
            self.timer.start()
            self._rebuild_timer = QTimer()
            self._rebuild_timer.setSingleShot(True)
            self._rebuild_timer.setInterval(200)
            self._rebuild_timer.timeout.connect(self._rebuild)
            app = QGuiApplication.instance()
            if app is not None:
                app.screenAdded.connect(self._screens_changed)  # type: ignore[attr-defined]
                app.screenRemoved.connect(self._screens_changed)  # type: ignore[attr-defined]
                self._screen_signals = True
        except Exception:
            self.close()
            raise
        self._open = True

    def close(self) -> None:
        self._open = False
        self.visible = False
        app = QGuiApplication.instance()
        if app is not None and self._screen_signals:
            for signal in (app.screenAdded, app.screenRemoved):  # type: ignore[attr-defined]
                with contextlib.suppress(Exception):
                    signal.disconnect(self._screens_changed)
        self._screen_signals = False
        for timer in (self.timer, self._rebuild_timer):
            if timer is not None:
                with contextlib.suppress(RuntimeError):
                    timer.stop()
                    timer.deleteLater()
        self.timer = self._rebuild_timer = None
        self._destroy_windows()
        self.lines.clear()
        invoker, self.invoker = self.invoker, None
        if invoker is not None:
            with contextlib.suppress(RuntimeError):
                invoker.deleteLater()

    def post(self, fn: Callable[[], None]) -> None:
        invoker = self.invoker
        if invoker is None:
            return
        with contextlib.suppress(RuntimeError):  # Deleted by close().
            invoker.post(fn)

    def _build_windows(self) -> None:
        self.windows = tuple(_ScreenWindow(s, self._cfg) for s in QGuiApplication.screens())

    def _destroy_windows(self) -> None:
        windows, self.windows = self.windows, ()
        for window in windows:
            with contextlib.suppress(RuntimeError):
                window.hide()
                window.deleteLater()

    def _screens_changed(self, *_args: Any) -> None:
        if self._rebuild_timer is not None:
            self._rebuild_timer.start()

    def _rebuild(self) -> None:
        if not self._open:
            return
        self._destroy_windows()
        self._build_windows()
        self.refresh()

    def tick(self) -> None:
        """Expire old lines, and put every visible window back on top of the others."""
        if self._cfg.linger_s > 0:
            now = time.monotonic()
            stale = [k for k, v in self.lines.items() if v.expires and v.expires <= now]
            for key in stale:
                del self.lines[key]
            if stale:
                self.refresh()
        for window in self.windows:
            window.raise_topmost()

    def refresh(self) -> None:
        if not self._open:
            return
        geometries = [w.physical() for w in self.windows]
        buckets: list[list[_Line]] = [[] for _ in self.windows]
        for line in self.lines.values():
            index = pick_screen(line.bbox, geometries)
            if index is not None:
                buckets[index].append(line)
        primary = QGuiApplication.primaryScreen()
        for window, bucket in zip(self.windows, buckets, strict=True):
            is_primary = window.qscreen == primary
            window.set_content(bucket, self.banner, is_primary)
            wanted = self.visible and (bool(bucket) or (is_primary and bool(self.banner)))
            if wanted and not window.isVisible():
                window.show()
                window.raise_topmost()
            elif not wanted and window.isVisible():
                window.hide()

    def add(self, lines: Iterable[_Line]) -> None:
        for line in lines:
            self.lines[line.line_id] = line
        self.refresh()

    def remove(self, line_ids: Iterable[str]) -> None:
        for key in line_ids:
            self.lines.pop(key, None)
        self.refresh()

    def clear(self) -> None:
        self.lines.clear()
        self.refresh()

    def set_visible(self, visible: bool) -> None:
        self.visible = visible
        self.refresh()

    def set_banner(self, banner: str) -> None:
        self.banner = banner
        self.refresh()

    def health(self) -> Health:
        windows = self.windows
        if any(not w.exclusion_ok for w in windows):
            return Health("degraded", "The overlay could not be hidden from screen capture.")
        if any(not w.click_through_ok for w in windows):
            return Health("degraded", "The overlay may block mouse clicks.")
        return Health("ok")
