"""Finds adapters and hands them to the pipeline, by the names used in config.

An adapter registers itself with ``@register_adapter(port, name)``. The registry refuses an
adapter that fails a check, logs why, and keeps the reason so the interface can show it. A
refused or broken adapter never stops the app.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, TypeVar

from packaging.specifiers import InvalidSpecifier, SpecifierSet

from gloss.core.contracts import (
    CONTRACT_VERSION,
    PORTS,
    AdapterContext,
    AdapterUnavailable,
    GlossError,
    PermanentError,
)

log = logging.getLogger(__name__)

T = TypeVar("T", bound=type)

ADAPTERS_PACKAGE = "gloss.adapters"


class UnknownAdapter(PermanentError):
    """Config named an adapter that is not registered."""


@dataclass(frozen=True, slots=True)
class Rejection:
    """An adapter that was refused. ``name`` is a module path for an import failure."""

    port: str
    name: str
    reason: str


def contract_compatible(requirement: str, version: str = CONTRACT_VERSION) -> bool:
    """Return True if ``version`` satisfies a specifier such as ``>=1.0,<2.0``."""
    try:
        return SpecifierSet(requirement).contains(version)
    except InvalidSpecifier:
        return False


def _required_methods(port_type: type) -> list[str]:
    return [
        attr
        for attr, value in vars(port_type).items()
        if inspect.isfunction(value) and not attr.startswith("_")
    ]


class Registry:
    def __init__(self, contract_version: str = CONTRACT_VERSION) -> None:
        self._contract_version = contract_version
        self._adapters: dict[str, dict[str, type]] = {port: {} for port in PORTS}
        self._rejected: list[Rejection] = []

    def register(self, port: str, name: str, adapter: type) -> bool:
        """Add an adapter. Return False, recording why, if it fails a check."""
        reason = self._check(port, name, adapter)
        if reason is not None:
            self._reject(port, name, reason)
            return False
        self._adapters[port][name] = adapter
        return True

    def _check(self, port: str, name: str, adapter: type) -> str | None:
        if port not in PORTS:
            return f"unknown port '{port}'"
        existing = self._adapters[port].get(name)
        if existing is not None and existing is not adapter:
            return f"the name is already used by {existing.__qualname__}"
        if getattr(adapter, "name", None) != name:
            return f"its 'name' attribute must be '{name}'"
        requirement = getattr(adapter, "REQUIRES_CONTRACT", None)
        if not isinstance(requirement, str):
            return "it does not declare REQUIRES_CONTRACT"
        if not contract_compatible(requirement, self._contract_version):
            return (
                f"it requires contract {requirement}, but this build has {self._contract_version}"
            )
        missing = [
            m for m in _required_methods(PORTS[port]) if not callable(getattr(adapter, m, 0))
        ]
        if missing:
            return f"it is missing: {', '.join(missing)}"
        return None

    def _reject(self, port: str, name: str, reason: str) -> None:
        log.warning("Adapter %s/%s refused: %s", port, name, reason)
        self._rejected.append(Rejection(port, name, reason))

    def names(self, port: str) -> tuple[str, ...]:
        return tuple(sorted(self._adapters.get(port, {})))

    def rejected(self) -> tuple[Rejection, ...]:
        return tuple(self._rejected)

    def create(self, port: str, name: str, context: AdapterContext | None = None) -> Any:
        """Build an adapter. Raises UnknownAdapter or AdapterUnavailable."""
        adapter = self._adapters.get(port, {}).get(name)
        if adapter is None:
            reasons = [r.reason for r in self._rejected if r.port == port and r.name == name]
            suffix = f": {reasons[-1]}" if reasons else ""
            raise UnknownAdapter(f"No usable {port} adapter named '{name}'{suffix}")
        try:
            return adapter(context if context is not None else AdapterContext())
        except GlossError:
            raise
        except Exception as exc:
            raise AdapterUnavailable(f"{port} adapter '{name}' failed to start") from exc

    def resolve_chain(
        self,
        port: str,
        primary: str,
        fallbacks: Iterable[str] = (),
        *,
        local_only: bool | None = None,
    ) -> tuple[str, ...]:
        """Return the usable adapter names, in order, with no repeats.

        Unregistered names are dropped with a warning, as is any fallback that sets
        ``ALLOW_AUTO_FALLBACK = False``. The primary is the user's explicit choice, so that
        setting does not apply to it.

        ``local_only`` is required for the translate port and ignored for the others. Pass
        ``not config.privacy.cloud_translation``. Leaving it out raises TypeError, so a
        forgetful caller cannot send text off the PC. When it is True, every translator
        without ``LOCAL_ONLY = True`` on the class itself is dropped, primary included. An
        undeclared adapter counts as sending text off the PC, even if its parent is local.
        """
        if port == "translate" and not isinstance(local_only, bool):
            raise TypeError("resolve_chain needs local_only=True or False for the translate port")
        adapters = self._adapters.get(port, {})
        chain: list[str] = []
        for position, name in enumerate(dict.fromkeys([primary, *fallbacks])):
            adapter = adapters.get(name)
            if adapter is None:
                log.warning("Adapter %s/%s is not available; skipping it", port, name)
            elif port == "translate" and local_only and vars(adapter).get("LOCAL_ONLY") is not True:
                log.warning("Adapter %s/%s skipped: cloud translation is off", port, name)
            elif position > 0 and not getattr(adapter, "ALLOW_AUTO_FALLBACK", True):
                log.warning("Adapter %s/%s cannot be an automatic fallback", port, name)
            else:
                chain.append(name)
        return tuple(chain)

    def record_import_failure(self, port: str, module: str, exc: Exception) -> None:
        self._reject(port, module, f"import failed: {type(exc).__name__}: {exc}")


default_registry = Registry()


def register_adapter(port: str, name: str) -> Callable[[T], T]:
    """Class decorator that adds an adapter to the default registry."""

    def decorate(adapter: T) -> T:
        default_registry.register(port, name, adapter)
        return adapter

    return decorate


def discover(package: str = ADAPTERS_PACKAGE, registry: Registry | None = None) -> None:
    """Import every module under ``gloss.adapters``, so adapters can register themselves.

    A module that fails to import is recorded as a rejection, and the rest still load.
    Safe to call twice.
    """
    target = registry if registry is not None else default_registry
    root = importlib.import_module(package)
    for port_module in pkgutil.iter_modules(root.__path__, f"{package}."):
        if not port_module.ispkg:
            continue
        port = port_module.name.rsplit(".", 1)[-1]
        try:
            port_package = importlib.import_module(port_module.name)
        except Exception as exc:  # Fail soft: the other ports must still load.
            target.record_import_failure(port, port_module.name, exc)
            continue
        for module in pkgutil.iter_modules(port_package.__path__, f"{port_module.name}."):
            try:
                importlib.import_module(module.name)
            except Exception as exc:  # Fail soft: one broken adapter must not stop the app.
                target.record_import_failure(port, module.name, exc)
