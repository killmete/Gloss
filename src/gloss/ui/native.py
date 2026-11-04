"""Small Windows and Qt helpers shared by everything in ``gloss.ui``.

* ``exclude_hwnd`` is the one place the UI excludes a window. It calls the shared helper in
  ``gloss.services.capture_exclusion``.
* ``install_capture_guard`` makes every top-level Qt window of the app (dialogs, menus, the
  region selector, message boxes) excluded from capture the moment it appears, and again if Qt
  recreates its native window.
* ``ensure_app`` creates the QApplication once.
* ``MainThread`` lets any thread run a function on the Qt main thread.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, Signal, Slot
from PySide6.QtWidgets import QApplication, QWidget

from gloss.services.capture_exclusion import (  # noqa: F401  (re-exported for tests)
    WDA_EXCLUDEFROMCAPTURE,
    display_affinity,
    exclude_from_capture,
)

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


def exclude_hwnd(hwnd: int) -> bool:
    """Hide one native window from every screen capture. The one call site for the helper."""
    return exclude_from_capture(hwnd)


def exclude_widget(widget: QWidget) -> bool:
    """Exclude a Qt window. Forces its native window into existence first."""
    if sys.platform != "win32":
        return True
    try:
        return exclude_hwnd(int(widget.winId()))
    except RuntimeError:  # The C++ window was already deleted.
        return False


class _CaptureGuard(QObject):
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        kind = event.type()
        if kind in (QEvent.Type.Show, QEvent.Type.WinIdChange):
            if isinstance(watched, QWidget) and watched.isWindow():
                exclude_widget(watched)
        return False


_guard: _CaptureGuard | None = None


def install_capture_guard(app: QApplication) -> None:
    """Exclude every top-level window this app ever shows. Safe to call twice."""
    global _guard
    if _guard is None:
        _guard = _CaptureGuard(app)
        app.installEventFilter(_guard)


def ensure_app() -> QApplication:
    """Return the QApplication, creating it (and the capture guard) on first use."""
    app = QApplication.instance()
    if app is None:
        if sys.platform != "win32" and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        ):
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication(["gloss"])
        app.setQuitOnLastWindowClosed(False)
    install_capture_guard(app)  # type: ignore[arg-type]
    return app  # type: ignore[return-value]


class MainThread(QObject):
    """Runs functions on the thread this object was created in. Create it on the Qt main thread.

    ``post`` may be called from any thread and returns at once. Called on the main thread, the
    function runs right away.
    """

    _call = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._call.connect(self._run)

    @Slot(object)
    def _run(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            log_failure(log, "a queued UI update failed")

    def post(self, fn: Callable[[], None]) -> None:
        with contextlib.suppress(RuntimeError):  # Deleted during shutdown.
            self._call.emit(fn)
