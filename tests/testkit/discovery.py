"""Finds registered adapters and builds Subjects for them."""

from __future__ import annotations

import functools
import logging
import sys
from pathlib import Path
from typing import Any

from gloss.core.config import load_config
from gloss.core.registry import default_registry, discover
from testkit.core import Subject

log = logging.getLogger(__name__)
NO_USER_CONFIG = Path(__file__).parent / "no-such-user-config.toml"


@functools.cache
def discovered() -> None:
    try:
        discover()
    except Exception:  # Collection must not error. test_*_discovery_is_clean reports it.
        log.exception("adapter discovery failed")


def registered_names(port: str) -> tuple[str, ...]:
    discovered()
    return default_registry.names(port)


@functools.cache
def _config() -> Any:
    return load_config(user_path=NO_USER_CONFIG).config


def real_subject(port: str, name: str, secrets: Any = None) -> Subject:
    """A Subject for a registered adapter, built with the default config settings."""
    settings = _config().adapter_settings(port, name)

    def make(context: Any) -> Any:
        return default_registry.create(port, name, context)

    extra: dict[str, Any] = {}
    if port == "capture":
        from gloss.core.contracts import CaptureTarget

        extra["open_args"] = (CaptureTarget("monitor"),)
    if sys.platform == "win32":
        from testkit import win32

        extra["os_truth"] = win32.os_monitors
        extra["window_probe"] = win32.own_windows
    return Subject(port, name, make, settings, real=True, secrets=secrets, **extra)
