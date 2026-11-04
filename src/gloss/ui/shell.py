"""``GlossShell``: everything the user sees, behind one object the app wires up.

Create it on the Qt main thread, call ``start()``, and run the Qt event loop. After that:

* Worker threads call ``show_results``, ``set_health``, ``notify`` and ``clear``. They return at
  once and are safe from any thread.
* The shell tells the app what the user did through Qt signals (``pause_toggled``,
  ``region_selected``, ``mode_changed``, ``low_quality_changed``, ``config_changed``,
  ``quit_requested``). Connect them to slots that hand the work to the pipeline's own threads.
  The shell never waits on capture, OCR or the network.

The shell talks to renderers only through the ``Renderer`` port and the registry, and to the key
store only through the ``SecretStore`` port.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QMessageBox

from gloss.core.config import GlossConfig, LoadedConfig, load_config, update_user_config
from gloss.core.contracts import (
    AdapterContext,
    FrameMeta,
    GlossError,
    Health,
    Renderer,
    RenderItem,
    SecretStore,
)
from gloss.core.registry import Registry, default_registry, discover
from gloss.ui.api_key_dialog import ApiKeyDialog
from gloss.ui.hotkeys import HotkeyService
from gloss.ui.native import MainThread, ensure_app, log_failure
from gloss.ui.region_selector import RegionSelector
from gloss.ui.settings_window import SettingsWindow
from gloss.ui.tray import TrayController

log = logging.getLogger(__name__)

LOW_QUALITY_BANNER = "Low-quality OCR: text may be wrong or missing"
LOW_QUALITY_DETAIL = "Low-quality reading is on for this session."
_RANK = {"ok": 0, "degraded": 1, "down": 2}


def worst(*healths: Health) -> Health:
    """The most serious state, with the first reason given for it."""
    pick = healths[0]
    for health in healths[1:]:
        if _RANK[health.state] > _RANK[pick.state]:
            pick = health
    return pick


def secret_name_for(config: GlossConfig) -> str | None:
    """The key-store name the chosen translator uses, or None if it needs no key."""
    settings = config.adapter_settings("translate", config.translate.provider)
    name = settings.get("secret_name")
    return name if isinstance(name, str) and name else None


class GlossShell(QObject):
    pause_toggled = Signal(bool)  # True = paused
    region_selected = Signal(object, str)  # Rect in physical desktop pixels, monitor id
    mode_changed = Signal(str)
    low_quality_changed = Signal(bool)
    visibility_changed = Signal(bool)
    config_changed = Signal(object)  # the new LoadedConfig
    quit_requested = Signal()
    api_key_saved = Signal()  # after the key dialog stored a key (there is no clear action)

    def __init__(
        self,
        loaded: LoadedConfig,
        *,
        registry: Registry = default_registry,
        secrets: SecretStore | None = None,
        key_test: Callable[[], str] | None = None,
        user_path: Path | None = None,
        defaults_path: Path | None = None,
        confirm: Callable[[str, str], bool] | None = None,
    ) -> None:
        ensure_app()
        super().__init__()
        self._loaded = loaded
        self._registry = registry
        self._secrets = secrets
        self._key_test = key_test
        self._user_path = user_path
        self._defaults_path = defaults_path
        self._main = MainThread(self)
        self._renderers: tuple[Renderer, ...] = ()
        self._base_health = Health("ok")
        self._low_quality = False
        self._paused = False
        self._visible = True
        self._settings: SettingsWindow | None = None
        self._key_dialog: ApiKeyDialog | None = None

        tray_args: dict[str, object] = {"mode": loaded.config.render.mode}
        if confirm is not None:
            tray_args["confirm"] = confirm
        self.tray = TrayController(**tray_args)  # type: ignore[arg-type]
        self.hotkeys = HotkeyService(self)
        self.selector = RegionSelector(self)

        self.tray.pause_toggled.connect(self._on_pause)
        self.tray.select_region_requested.connect(self.select_region)
        self.tray.mode_selected.connect(self.set_mode)
        self.tray.translations_visible_toggled.connect(self.set_translations_visible)
        self.tray.low_quality_toggled.connect(self._on_low_quality)
        self.tray.settings_requested.connect(self.open_settings)
        self.tray.api_key_requested.connect(self.open_api_key_dialog)
        self.tray.quit_requested.connect(self.quit_requested.emit)
        self.hotkeys.triggered.connect(self._on_hotkey)
        self.hotkeys.failed.connect(self._on_hotkey_failed)
        self.selector.selected.connect(self.region_selected.emit)

    # ---- start and stop (Qt main thread)
    @property
    def config(self) -> GlossConfig:
        return self._loaded.config

    def start(self) -> None:
        """Show the tray, open the renderers and register the hotkeys."""
        for warning in self._loaded.warnings:
            log.warning("config: %s", warning)
        self.tray.show()
        self._open_renderers()
        self.hotkeys.set_bindings(self._hotkey_bindings())
        self._push_status()

    def shutdown(self) -> None:
        self.hotkeys.close()
        self.selector.cancel()
        self._close_renderers()
        self.tray.hide()

    # ---- renderers
    def _open_renderers(self) -> None:
        discover()
        opened: list[Renderer] = []
        for name in self.config.render.renderers:
            try:
                context = AdapterContext(
                    settings=self.config.adapter_settings("render", name), secrets=self._secrets
                )
                renderer = self._registry.create("render", name, context)
                renderer.open()
                renderer.set_visible(self._visible)
                opened.append(renderer)
            except GlossError as exc:
                log_failure(log, "renderer %s did not start", name)
                self.tray.notify("Gloss", f"The '{name}' display could not start: {exc}", "error")
            except Exception:
                log_failure(log, "renderer %s crashed while starting", name)
                self.tray.notify("Gloss", f"The '{name}' display could not start.", "error")
        self._renderers = tuple(opened)
        self.tray.set_mode(self.config.render.mode)

    def _close_renderers(self) -> None:
        renderers, self._renderers = self._renderers, ()
        for renderer in renderers:
            try:
                renderer.close()
            except Exception:
                log_failure(
                    log, "renderer %s did not close cleanly", getattr(renderer, "name", "?")
                )

    # ---- status
    def _effective_health(self) -> Health:
        parts = [self._base_health]
        if self._low_quality:
            parts.append(Health("degraded", LOW_QUALITY_DETAIL))
        for renderer in self._renderers:
            try:
                state = renderer.health()
            except Exception:
                continue
            if state.state != "ok":
                parts.append(state)
        return worst(*parts)

    def _push_status(self) -> None:
        health = self._effective_health()
        banner = LOW_QUALITY_BANNER if self._low_quality else ""
        self.tray.set_health(health)
        for renderer in self._renderers:
            try:
                renderer.set_status(health, banner)
            except Exception:
                log_failure(log, "renderer %s rejected a status", getattr(renderer, "name", "?"))

    # ---- any thread
    def show_results(self, items: Sequence[RenderItem], frame_meta: FrameMeta | None) -> None:
        """Hand new translated lines to the display. Returns at once, from any thread."""
        for renderer in self._renderers:
            try:
                renderer.show(items, frame_meta)
            except Exception:
                log_failure(log, "renderer %s could not show lines", getattr(renderer, "name", "?"))

    def remove_lines(self, line_ids: Sequence[str]) -> None:
        """Take lines that left the screen off the display. Any thread, returns at once."""
        ids = tuple(line_ids)
        for renderer in self._renderers:
            remove = getattr(renderer, "remove", None)
            if remove is None:
                continue
            try:
                remove(ids)
            except Exception:
                log_failure(
                    log, "renderer %s could not remove lines", getattr(renderer, "name", "?")
                )

    def clear(self) -> None:
        for renderer in self._renderers:
            try:
                renderer.clear()
            except Exception:
                log_failure(log, "renderer %s could not clear", getattr(renderer, "name", "?"))

    def set_health(self, health: Health) -> None:
        """Report the pipeline's overall state. Safe from any thread."""
        self._base_health = health
        self._main.post(self._push_status)

    def notify(self, title: str, text: str, level: str = "info") -> None:
        self._main.post(lambda: self.tray.notify(title, text, level))

    # ---- user actions (Qt main thread)
    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def low_quality(self) -> bool:
        return self._low_quality

    def _on_pause(self, paused: bool) -> None:
        self._paused = paused
        self.pause_toggled.emit(paused)

    def toggle_pause(self) -> None:
        self.tray.set_paused(not self._paused)
        self._on_pause(not self._paused)

    def _on_low_quality(self, on: bool) -> None:
        self._low_quality = on
        self.tray.set_low_quality(on)
        self._push_status()
        self.low_quality_changed.emit(on)

    def set_translations_visible(self, visible: bool) -> None:
        self._visible = visible
        self.tray.set_translations_visible(visible)
        for renderer in self._renderers:
            renderer.set_visible(visible)
        self.visibility_changed.emit(visible)

    def select_region(self) -> None:
        self.selector.start()

    def set_mode(self, mode: str) -> None:
        """Switch between panel, overlay and both, and remember the choice."""
        if mode == self.config.render.mode:
            return
        try:
            update_user_config(
                {"render": {"mode": mode}},
                **self._paths(),
            )
        except Exception as exc:
            log.warning("could not save the display mode: %s", type(exc).__name__)
            self.tray.notify("Gloss", "The display choice could not be saved.", "warning")
        self._reload()
        self.mode_changed.emit(mode)

    def _paths(self) -> dict[str, Path]:
        out: dict[str, Path] = {}
        if self._user_path is not None:
            out["user_path"] = self._user_path
        if self._defaults_path is not None:
            out["defaults_path"] = self._defaults_path
        return out

    def _reload(self) -> None:
        self._loaded = load_config(**self._paths())
        self._close_renderers()
        self._open_renderers()
        self.hotkeys.set_bindings(self._hotkey_bindings())
        self._push_status()
        self.config_changed.emit(self._loaded)

    def _hotkey_bindings(self) -> dict[str, str]:
        hk = self.config.hotkeys
        return {"toggle": hk.toggle, "select_region": hk.select_region, "pause": hk.pause}

    def _on_hotkey(self, name: str) -> None:
        if name == "toggle":
            self.set_translations_visible(not self._visible)
        elif name == "pause":
            self.toggle_pause()
        elif name == "select_region":
            self.select_region()

    def _on_hotkey_failed(self, name: str, message: str) -> None:
        self.tray.notify("Gloss: keyboard shortcut", message, "warning")

    # ---- windows
    def open_settings(self) -> None:
        if self._settings is not None:
            self._settings.raise_()
            self._settings.activateWindow()
            return
        window = SettingsWindow(
            self.config,
            can_change_key=self._secrets is not None and secret_name_for(self.config) is not None,
            auto_close=False,
            **self._paths(),
        )
        window.change_key_requested.connect(self.open_api_key_dialog)
        window.saved.connect(lambda _changes, w=window: self._after_settings_saved(w))
        window.finished.connect(lambda _r: setattr(self, "_settings", None))
        self._settings = window
        window.open()

    def _after_settings_saved(self, window: SettingsWindow) -> None:
        self._reload()
        window.finish(self.hotkeys.problems)
        window.load(self.config)

    def needs_api_key(self) -> bool:
        name = secret_name_for(self.config)
        if self._secrets is None or name is None or not self.config.privacy.cloud_translation:
            return False
        try:
            return self._secrets.get(name) is None
        except GlossError:
            log_failure(log, "could not look for the saved key")
            return False

    def open_api_key_dialog(self) -> ApiKeyDialog | None:
        name = secret_name_for(self.config)
        if self._secrets is None or name is None:
            QMessageBox.information(
                None, "Gloss", "The chosen translation service does not need an API key."
            )
            return None
        if self._key_dialog is not None:
            self._key_dialog.raise_()
            return self._key_dialog
        dialog = ApiKeyDialog(self._secrets, name, self._key_test)
        dialog.saved.connect(self.api_key_saved.emit)
        dialog.finished.connect(lambda _r: setattr(self, "_key_dialog", None))
        self._key_dialog = dialog
        dialog.open()
        return dialog

    def run_first_run(self) -> None:
        """Ask for the API key if there is none, then let the user pick the screen area.

        Neither step blocks the pipeline: both are ordinary windows on the main thread.
        """
        dialog = self.open_api_key_dialog() if self.needs_api_key() else None
        if dialog is None:
            QTimer.singleShot(0, self.select_region)
        else:
            dialog.finished.connect(lambda _r: QTimer.singleShot(0, self.select_region))
