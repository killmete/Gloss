"""Application entry point: ``uv run gloss``.

Order matters. The process becomes per-monitor DPI aware (V2) before Qt or mss load, so
every coordinate is in physical pixels. Then config, the adapter registry, the key store,
the Qt shell and the pipeline start, and the Qt event loop runs until the user quits.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Sequence
from typing import Any

from gloss.services.dpi import ensure_per_monitor_v2

log = logging.getLogger("gloss")


class _Translation:
    """A TranslationService plus the spend ledger it does not close itself."""

    def __init__(self, service: Any, ledger: Any) -> None:
        self._service = service
        self._ledger = ledger

    def open(self) -> None:
        self._service.open()

    def translate(self, items: Sequence[Any], source_lang: str | None = None) -> tuple[Any, ...]:
        return self._service.translate(items, source_lang=source_lang)

    def health(self) -> Any:
        return self._service.health()

    def retry_unavailable(self) -> None:
        self._service.retry_unavailable()

    def close(self) -> None:
        try:
            self._service.close()
        finally:
            self._ledger.close()


def make_translation_factory(config: Any, registry: Any, secrets: Any) -> Any:
    """Return a function that builds a fresh translation stage, called on its worker."""
    from gloss.core.config import user_config_path
    from gloss.core.contracts import AdapterContext
    from gloss.services.budget import BudgetGuard
    from gloss.services.cache import SqliteTranslationCache
    from gloss.services.glossary import GlossaryStore
    from gloss.services.ledger import SqliteSpendLedger
    from gloss.services.translation_service import ServiceSettings, TranslationService

    t = config.translate
    db = user_config_path().parent / config.storage.database

    def build() -> _Translation:
        names = registry.resolve_chain(
            "translate", t.provider, t.fallbacks, local_only=not config.privacy.cloud_translation
        )
        translators = []
        for name in names:
            context = AdapterContext(config.adapter_settings("translate", name), secrets)
            try:
                translators.append(registry.create("translate", name, context))
            except Exception as exc:  # A broken translator is skipped, not fatal.
                log.warning("Translator %s could not be created: %s", name, type(exc).__name__)
        ledger = SqliteSpendLedger(db)
        budget = BudgetGuard(
            ledger,
            t.price_for,
            monthly_usd=t.budget.monthly_usd,
            hourly_usd=t.budget.hourly_usd,
            warn_at=t.budget.warn_at,
        )
        service = TranslationService(
            translators,
            settings=ServiceSettings.from_config(config),
            cache=SqliteTranslationCache(db, max_entries=config.storage.cache_max_entries),
            budget=budget,
            glossary=GlossaryStore.from_config(config.storage.glossary_path),
        )
        return _Translation(service, ledger)

    return build


class _ShellSink:
    """Adapts the shell to the pipeline's ``ResultSink``."""

    def __init__(self, shell: Any) -> None:
        self._shell = shell

    def show_results(self, items: Sequence[Any], frame_meta: Any) -> None:
        self._shell.show_results(items, frame_meta)

    def remove_lines(self, line_ids: Sequence[str]) -> None:
        self._shell.remove_lines(line_ids)

    def set_health(self, health: Any) -> None:
        self._shell.set_health(health)


def _key_test(config: Any, registry: Any, secrets: Any) -> Any:
    """For the API key dialog: open the configured translator once, which checks the stored
    key without sending any text, and return a message or raise one the user can read."""
    from gloss.core.contracts import AdapterContext, GlossError

    def test() -> str:
        provider = config.translate.provider
        settings = config.adapter_settings("translate", provider)
        translator = registry.create("translate", provider, AdapterContext(settings, secrets))
        try:
            translator.open()
        except GlossError as exc:
            raise RuntimeError(str(exc) or "The key could not be checked.") from None
        finally:
            translator.close()
        return "The key was accepted."

    return test


def _setup_logging(debug: bool) -> None:
    # Warnings only by default; adapters log no chat text at that level.
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> int:
    awareness = ensure_per_monitor_v2()  # before Qt or mss load

    from gloss.core.config import load_config
    from gloss.core.contracts import CaptureTarget
    from gloss.core.pipeline import Pipeline
    from gloss.core.registry import default_registry, discover
    from gloss.services.secrets import KeyringSecretStore
    from gloss.ui.native import ensure_app
    from gloss.ui.shell import GlossShell

    loaded = load_config()
    _setup_logging(loaded.config.privacy.debug_logging)
    if awareness != "per_monitor_v2":
        log.warning("DPI awareness is %s; positions may be off on scaled monitors", awareness)
    discover()
    for rejection in default_registry.rejected():
        log.warning("Adapter %s/%s refused: %s", rejection.port, rejection.name, rejection.reason)

    secrets = KeyringSecretStore()
    app = ensure_app()
    app.setQuitOnLastWindowClosed(False)  # the tray keeps running with no window open
    shell = GlossShell(
        loaded, secrets=secrets, key_test=_key_test(loaded.config, default_registry, secrets)
    )
    sink = _ShellSink(shell)
    state: dict[str, Any] = {"pipeline": None, "target": _initial_target(loaded.config)}

    def start_pipeline(config: Any) -> None:
        old = state["pipeline"]
        if old is not None:
            # Off the Qt thread: a worker may take a moment to finish its current call.
            threading.Thread(target=old.stop, name="gloss-stop", daemon=True).start()
        pipeline = Pipeline(
            config,
            registry=default_registry,
            sink=sink,
            make_translation=make_translation_factory(config, default_registry, secrets),
            secrets=secrets,
            target=state["target"],
        )
        pipeline.set_paused(shell.paused)
        pipeline.set_low_quality(shell.low_quality)
        pipeline.start()
        state["pipeline"] = pipeline

    def on_region(rect: Any, monitor_id: str) -> None:
        state["target"] = CaptureTarget("region", monitor_id=monitor_id or None, region=rect)
        _remember_region(rect)
        shell.clear()
        state["pipeline"].set_target(state["target"])

    shell.pause_toggled.connect(lambda paused: state["pipeline"].set_paused(paused))
    shell.low_quality_changed.connect(lambda on: state["pipeline"].set_low_quality(on))
    shell.region_selected.connect(on_region)
    shell.config_changed.connect(lambda new: start_pipeline(new.config))
    shell.api_key_saved.connect(lambda: state["pipeline"].retry_translation())
    shell.quit_requested.connect(app.quit)

    shell.start()
    start_pipeline(loaded.config)
    if shell.needs_api_key() or state["target"] is None:
        shell.run_first_run()
    try:
        return app.exec()
    finally:
        started = time.monotonic()
        state["pipeline"].stop()
        shell.shutdown()
        log.debug("Shut down in %.1f s", time.monotonic() - started)


def _initial_target(config: Any) -> Any:
    """The capture target to start with, or None until the user picks a screen area."""
    from gloss.core.contracts import CaptureTarget

    mode = config.capture.mode
    if mode == "monitor":
        return CaptureTarget("monitor")
    if mode == "region":
        return _saved_region(config)
    return None  # window mode needs a window picker, which is not built yet


def _saved_region(config: Any) -> Any:
    """Restore the last screen area, if it still fits the monitors."""
    saved = config.capture.saved_region
    if saved is None:
        return None
    from gloss.services.monitors import list_monitors
    from gloss.services.region import region_target, restore_region

    try:
        monitors = list_monitors()
        rect = restore_region(saved, monitors)
        return region_target(rect, monitors) if rect is not None else None
    except Exception as exc:
        log.warning("The saved screen area could not be restored: %s", type(exc).__name__)
        return None


def _remember_region(rect: Any) -> None:
    from gloss.core.config import update_user_config

    try:
        update_user_config({"capture": {"region": [rect.x, rect.y, rect.w, rect.h]}})
    except Exception as exc:
        log.warning("The screen area could not be saved: %s", type(exc).__name__)


if __name__ == "__main__":
    sys.exit(main())
