"""Shared helpers for the UI tests. Not a test module.

The whole test run shares one QApplication. Contract tests need real Windows windows, so these
tests never force the offscreen platform.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from gloss.core.contracts import Rect, RenderItem, TranslationResult


def get_app() -> QApplication:
    from gloss.ui.native import ensure_app

    return ensure_app()


def pump(seconds: float = 0.05, until: Callable[[], bool] | None = None) -> bool:
    """Run the Qt event loop for a while, or until ``until()`` is true."""
    app = get_app()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.005)
    app.processEvents()
    return until() if until is not None else True


def flush_deletes() -> None:
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def item(
    text: str,
    *,
    line_id: str = "L1",
    original: str = "原文",
    bbox: Rect | None = None,
    status: str = "ok",
    low_quality: bool = False,
    rough: bool = False,
) -> RenderItem:
    if status == "failed":
        result = TranslationResult(line_id, original, "", "p", "m", False, 5, "failed", "refused")
    else:
        shown = original if status == "passthrough" else text
        result = TranslationResult(
            line_id,
            original,
            shown,
            "p",
            "m",
            False,
            5,
            status,
            None,
            rough,  # type: ignore[arg-type]
        )
    return RenderItem(result, bbox, low_quality)
