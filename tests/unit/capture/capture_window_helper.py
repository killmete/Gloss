"""A real, solid-colour Windows window for capture tests. Windows only.

It is a Qt widget on the test's main thread, like Gloss's own windows. (A hand-made Win32
message-loop thread was tried first and crashed with an access violation whenever
windows-capture started a session at the same time; Qt on the main thread, with the capture
adapter on another thread, is how the app is laid out and does not.)

Positions are set with Win32 in physical pixels, so they do not depend on Qt's scaling.
``pixel_fraction`` measures how much of an image is that colour.
"""

from __future__ import annotations

import ctypes
import time
import uuid

import numpy as np

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SetWindowPos.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_uint,
]
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]

HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SW_MINIMIZE = 6

MAGENTA = (255, 0, 255)


def _application():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def pump(seconds: float) -> None:
    """Let Qt paint and handle events for a while."""
    app = _application()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


class ColorWindow:
    """Context manager: a window of one colour at a given physical position."""

    def __init__(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        *,
        color: tuple[int, int, int] = MAGENTA,
        title: str | None = None,
        overlapped: bool = False,
    ) -> None:
        self.rect = (x, y, w, h)
        self.color = color
        self.title = title or f"Gloss test window {uuid.uuid4().hex[:8]}"
        self.overlapped = overlapped
        self.hwnd: int | None = None
        self._widget = None

    def __enter__(self) -> ColorWindow:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QPalette
        from PySide6.QtWidgets import QWidget

        _application()
        flags = Qt.WindowType.Window
        if not self.overlapped:
            flags = (
                Qt.WindowType.Tool
                | Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.WindowDoesNotAcceptFocus
            )
        widget = QWidget(None, flags)
        widget.setWindowTitle(self.title)
        palette = widget.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor(*self.color))
        widget.setPalette(palette)
        widget.setAutoFillBackground(True)
        widget.show()
        self._widget = widget
        self.hwnd = int(widget.winId())
        self._place()
        pump(0.6)  # let the desktop compositor draw it
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _place(self) -> None:
        x, y, w, h = self.rect
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, w, h, SWP_NOACTIVATE)

    def move(self, x: int, y: int) -> None:
        _, _, w, h = self.rect
        self.rect = (x, y, w, h)
        self._place()
        pump(0.4)

    def minimize(self) -> None:
        user32.ShowWindow(self.hwnd, SW_MINIMIZE)
        pump(0.4)

    def recolor(self, color: tuple[int, int, int]) -> None:
        from PySide6.QtGui import QColor, QPalette

        self.color = color
        palette = self._widget.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor(*color))
        self._widget.setPalette(palette)
        self._widget.update()
        pump(0.5)

    def close(self) -> None:
        widget, self._widget = self._widget, None
        if widget is not None:
            widget.destroy()
            self.hwnd = None
            pump(0.1)


def pixel_fraction(
    image: np.ndarray, color: tuple[int, int, int] = MAGENTA, tol: int = 24
) -> float:
    """The share of RGB pixels within ``tol`` of ``color``."""
    close = (np.abs(image.astype(np.int16) - np.array(color, dtype=np.int16)) <= tol).all(axis=2)
    return float(close.mean())
