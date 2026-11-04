"""The side panel: an always-on-top, resizable list of translations.

Each line shows the original (small, grey) above the English (larger). The newest line is at
the bottom, and the list follows it unless the user has scrolled up. Click a line to copy
its English. Hover a line to see a glossary note in the footer, when there is one.

Threading: every method except ``open`` and ``close`` may be called from any thread. Each one
hands its work to the Qt main thread through a queued signal and returns at once. Called on
the main thread, the work simply runs in place.

The panel never uses Qt tooltips or pop-ups. They are separate top-level windows, and each
would need its own capture exclusion. Status text and glossary notes live inside the panel.

An adapter may not import another adapter, and shared helpers belong in ``gloss.services``.
The Qt code lives in ``_qt.py`` beside this file and is imported by ``open()`` only, so
discovering and constructing the adapter loads no GUI library. The native-window helpers there
are repeated in the overlay package. When the shared capture-exclusion helper lands in
``gloss.services``, change ``_exclude_hwnd`` in ``_qt.py`` only.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from gloss.core.contracts import (
    AdapterContext,
    AdapterUnavailable,
    FrameMeta,
    Glossary,
    Health,
    RenderItem,
)
from gloss.core.registry import register_adapter

log = logging.getLogger(__name__)

DOT_COLOURS = {"ok": "#3ecf6e", "degraded": "#f5a623", "down": "#e5484d"}
STATE_WORDS = {"ok": "Working", "degraded": "Limited", "down": "Not working"}

# Defaults for optional settings in [render.adapters.panel]. Config may override each one.
DEFAULT_WIDTH = 420
DEFAULT_HEIGHT = 560
DEFAULT_HISTORY_MAX = 500
DEFAULT_MAX_CHARS = 1000
DEFAULT_ORIGINAL_SCALE = 0.75
DEFAULT_FONT_SIZE = 14
COPIED_FLASH_MS = 1500
STATE_SAVE_DELAY_MS = 1000


# ---- settings -----------------------------------------------------------------------------


def _number(
    settings: Mapping[str, Any], key: str, default: float, low: float, high: float
) -> float:
    value = settings.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    if not low <= value <= high:
        return default
    return float(value)


@dataclass(frozen=True)
class _PanelSettings:
    font_size: int
    original_size: int
    width: int
    height: int
    history_max: int
    max_chars: int
    state_file: str

    @classmethod
    def parse(cls, settings: Mapping[str, Any]) -> _PanelSettings:
        font = int(_number(settings, "font_size", DEFAULT_FONT_SIZE, 6, 96))
        scale = _number(settings, "original_scale", DEFAULT_ORIGINAL_SCALE, 0.3, 1.0)
        state = settings.get("state_file")
        if not isinstance(state, str) or not state:
            appdata = os.environ.get("APPDATA")
            base = appdata if appdata else os.path.join(os.path.expanduser("~"), "AppData")
            state = os.path.join(base, "Gloss", "ui_state.ini")
        return cls(
            font_size=font,
            original_size=max(6, round(font * scale)),
            width=int(_number(settings, "width", DEFAULT_WIDTH, 200, 4000)),
            height=int(_number(settings, "height", DEFAULT_HEIGHT, 200, 4000)),
            history_max=int(_number(settings, "history_max", DEFAULT_HISTORY_MAX, 10, 100000)),
            max_chars=int(_number(settings, "max_chars", DEFAULT_MAX_CHARS, 50, 100000)),
            state_file=state,
        )


# ---- one line of the list ----------------------------------------------------------------


@dataclass
class _Entry:
    line_id: str
    original: str
    english: str
    tags: tuple[str, ...]
    failed: bool
    note: str
    flash: bool = False
    # (width, original height, english height, tags height), valid for one viewport width.
    sizes: tuple[int, int, int, int, int] | None = None


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tags_for(item: RenderItem) -> tuple[str, ...]:
    result = item.result
    tags: list[str] = []
    if item.low_quality:
        tags.append("low-quality text")
    if result.rough:
        tags.append("rough translation")
    if result.status == "passthrough":
        tags.append("not translated")
    return tuple(tags)


def _note_for(original: str, glossary: Glossary | None) -> str:
    if glossary is None:
        return ""
    notes = [
        f"{e.term} = {e.translation}: {e.note}"
        for e in glossary.entries
        if e.note and e.term and e.term in original
    ]
    return "   |   ".join(notes[:3])


# ---- the adapter --------------------------------------------------------------------------


@register_adapter("render", "panel")
class PanelRenderer:
    name = "panel"
    REQUIRES_CONTRACT = ">=1.0,<2.0"

    def __init__(self, context: AdapterContext) -> None:
        self._cfg = _PanelSettings.parse(context.settings)
        self._window: Any = None
        self._invoker: Any = None
        self._glossary: Glossary | None = None
        self._visible = False
        self._status = Health("ok")
        self._banner = ""

    # -- lifecycle (Qt main thread)
    def open(self) -> None:
        if self._window is not None:
            return
        try:
            from gloss.adapters.render.panel import _qt

            _qt.ensure_app()
            invoker = _qt.Invoker()
            window = _qt.PanelWindow(self._cfg)
            window.restore_state()
        except Exception as exc:
            raise AdapterUnavailable(f"The panel window could not be created: {exc}") from exc
        self._invoker, self._window = invoker, window
        window.set_status(self._status, self._banner)

    def close(self) -> None:
        window, invoker = self._window, self._invoker
        self._window = self._invoker = None
        self._visible = False
        if window is not None:
            with contextlib.suppress(RuntimeError):
                window.save_state()
                window.hide()
                window.deleteLater()
        if invoker is not None:
            with contextlib.suppress(RuntimeError):
                invoker.deleteLater()

    # -- any thread
    def _post(self, fn: Callable[[Any], None]) -> None:
        invoker = self._invoker
        if invoker is None:
            return

        def run() -> None:
            window = self._window
            if window is not None:
                fn(window)

        with contextlib.suppress(RuntimeError):  # The invoker was deleted by close().
            invoker.post(run)

    def show(self, items: Sequence[RenderItem], frame_meta: FrameMeta | None) -> None:
        if self._invoker is None:
            return
        snapshot = tuple(items)
        glossary = self._glossary
        limit = self._cfg.max_chars
        entries: list[_Entry] = []
        for item in snapshot:
            result = item.result
            original = _clip(result.source_text, limit)
            failed = result.status == "failed"
            english = "Could not translate this line." if failed else _clip(result.text, limit)
            if not original and not english:
                continue
            entries.append(
                _Entry(
                    line_id=result.line_id,
                    original=original,
                    english=english or original,
                    tags=_tags_for(item),
                    failed=failed,
                    note=_note_for(result.source_text, glossary),
                )
            )
        if entries:
            self._post(lambda w: w.add(entries))

    def clear(self) -> None:
        """Does nothing: the panel keeps its history. The Clear button empties it."""

    def clear_history(self) -> None:
        self._post(lambda w: w.model.wipe())

    def set_glossary(self, glossary: Glossary) -> None:
        """Lines shown from now on get hover notes from this glossary."""
        self._glossary = glossary

    def set_visible(self, visible: bool) -> None:
        self._visible = bool(visible)

        def apply(w: Any) -> None:
            if visible:
                w.show()
                w.raise_()
            else:
                w.hide()

        self._post(apply)

    def set_status(self, health: Health, banner: str = "") -> None:
        self._status, self._banner = health, banner or ""
        status, text = health, banner or ""
        self._post(lambda w: w.set_status(status, text))

    def health(self) -> Health:
        window = self._window
        if window is None:
            return Health("down", "The panel is not open.")
        if not window.exclusion_ok:
            return Health("degraded", "The panel could not be hidden from screen capture.")
        return Health("ok")

    # -- helpers for the app and tests
    @property
    def window(self) -> Any:
        return self._window
