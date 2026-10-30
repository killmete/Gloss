"""Machinery shared by every contract suite.

A *check* is a plain function ``check_xxx(subject)`` that uses ``assert``. The same checks run
against registered adapters (real), against a conforming fake (must pass) and against
deliberately broken fakes (must fail with AssertionError). That last run proves the checks
catch what they claim to catch.

Fakes live in ``tests/testkit`` only. They are registered in throw-away ``Registry()``
objects, never in ``gloss.core.registry.default_registry`` and never under ``src/``.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import pytest

from gloss.core.contracts import (
    AdapterContext,
    AdapterUnavailable,
    GlossError,
    Health,
    PermanentError,
)
from gloss.core.registry import Registry, contract_compatible

# Set GLOSS_CONTRACT_STRICT=1 (for example on a build machine) to turn "adapter is unavailable
# on this PC" from a skip into a failure.
STRICT = os.environ.get("GLOSS_CONTRACT_STRICT") == "1"

HEALTH_STATES = ("ok", "degraded", "down")
# health() "must return at once, without I/O". 50 ms is generous for a method that only
# reads a field.
HEALTH_DEADLINE_S = 0.05
# Renderer methods "must return at once" too, because they only hand work to the Qt thread.
RENDER_CALL_DEADLINE_S = 0.25
WDA_EXCLUDEFROMCAPTURE = 0x11


def call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run adapter code the way the app does: only the three Gloss errors may escape.

    Any other exception means the adapter would crash its caller, so it becomes a failure.
    """
    try:
        return fn(*args, **kwargs)
    except GlossError:
        raise
    except Exception as exc:
        name = getattr(fn, "__qualname__", repr(fn))
        raise AssertionError(
            f"{name} raised {type(exc).__name__}: {exc}. An adapter must raise only "
            "AdapterUnavailable, TransientError or PermanentError, never anything else."
        ) from exc


def expect_error(fn: Callable[..., Any], error: type[GlossError], *args: Any) -> GlossError:
    """Call ``fn`` and require it to raise ``error`` (or a subclass), and nothing else."""
    try:
        call(fn, *args)
    except GlossError as exc:
        assert isinstance(exc, error), f"expected {error.__name__}, got {type(exc).__name__}: {exc}"
        return exc
    raise AssertionError(f"expected {error.__name__}, but nothing was raised")


def unavailable(label: str, exc: BaseException) -> None:
    """This adapter cannot run on this PC. Skip, or fail in strict mode."""
    if STRICT:
        raise AssertionError(f"{label} is unavailable and GLOSS_CONTRACT_STRICT=1: {exc}")
    pytest.skip(f"{label} is not available on this machine ({type(exc).__name__}: {exc})")


@dataclass
class Subject:
    """What a check receives: a way to build adapters of one kind.

    ``open_args`` are passed to ``open`` (capture needs a target). ``scripted`` is True for
    fakes that can be told to fail particular lines. ``os_truth`` and ``window_probe`` give
    checks the operating system's own answer to compare against.
    """

    port: str
    name: str
    make: Callable[[AdapterContext], Any]
    base_settings: dict[str, Any]
    real: bool = False
    open_args: tuple[Any, ...] = ()
    scripted: bool = False
    timeout_s: float | None = None
    secrets: Any = None
    os_truth: Callable[[], Sequence[tuple[str, Any]]] | None = None
    window_probe: Callable[[Any], Sequence[tuple[int, bool, int]]] | None = None

    def __post_init__(self) -> None:
        self._created: list[Any] = []
        self._shared: Any = None

    @property
    def label(self) -> str:
        return f"{self.port}/{self.name}"

    def context(self, settings: dict[str, Any] | None = None, secrets: Any = None) -> Any:
        merged = {**self.base_settings, **(settings or {})}
        return AdapterContext(settings=merged, secrets=secrets if secrets else self.secrets)

    def new(self, settings: dict[str, Any] | None = None, secrets: Any = None) -> Any:
        """Build a fresh, unopened adapter. It is closed again when the subject is released."""
        try:
            adapter = self.make(self.context(settings, secrets))
        except AdapterUnavailable as exc:
            unavailable(self.label, exc)
        self._created.append(adapter)
        return adapter

    def open(self, adapter: Any) -> None:
        """Open an adapter. An adapter that is unavailable here skips the test."""
        try:
            call(adapter.open, *self.open_args)
        except AdapterUnavailable as exc:
            unavailable(self.label, exc)

    def opened(self) -> Any:
        """One adapter opened once and shared by the checks that only read from it."""
        if self._shared is None:
            adapter = self.new()
            self.open(adapter)
            self._shared = adapter
        return self._shared

    def release(self) -> None:
        for adapter in [self._shared, *self._created]:
            if adapter is not None:
                try:
                    adapter.close()
                except Exception:  # Teardown must not hide the real failure.
                    pass
        self._shared = None
        self._created.clear()


def registered_subject(
    port: str,
    name: str,
    adapter: type,
    *,
    via_registry: bool = True,
    base_settings: dict[str, Any] | None = None,
    **extra: Any,
) -> Subject:
    """A Subject for a test-only adapter class, built through a private Registry."""
    if via_registry:
        registry = Registry()
        assert registry.register(port, name, adapter), registry.rejected()

        def make(context: Any) -> Any:
            return registry.create(port, name, context)
    else:
        make = adapter
    return Subject(port, name, make, base_settings or {}, **extra)


# ---- checks shared by capture, OCR, translator and renderer ------------------------------


def check_declares_identity(subject: Subject) -> None:
    adapter = subject.new()
    assert isinstance(adapter.name, str) and adapter.name, "name must be a non-empty string"
    assert adapter.name == subject.name, (
        f"name '{adapter.name}' differs from the registered name '{subject.name}'"
    )
    requirement = getattr(type(adapter), "REQUIRES_CONTRACT", None)
    assert isinstance(requirement, str), "REQUIRES_CONTRACT must be a class attribute string"
    assert contract_compatible(requirement), f"REQUIRES_CONTRACT {requirement!r} is not met"
    auto = getattr(type(adapter), "ALLOW_AUTO_FALLBACK", True)
    assert isinstance(auto, bool), "ALLOW_AUTO_FALLBACK must be True or False"


def check_close_is_always_safe(subject: Subject) -> None:
    adapter = subject.new()
    call(adapter.close)  # never opened
    call(adapter.close)  # twice
    subject.open(adapter)
    call(adapter.close)
    call(adapter.close)
    subject.open(adapter)  # reopen after close
    call(adapter.close)


def check_health_is_instant_in_every_state(subject: Subject) -> None:
    adapter = subject.new()
    assert_health(adapter, "before open")
    subject.open(adapter)
    assert_health(adapter, "while open")
    assert_health_from_other_thread(adapter)
    call(adapter.close)
    assert_health(adapter, "after close")


def check_survives_hostile_settings(subject: Subject) -> None:
    """Wrong or unknown settings may be refused, but only with a Gloss error."""
    hostile = {
        "__unknown__": 1,
        "timeout_ms": "soon",
        "fps_max": -5,
        "languages": "zh",
        "profile": 7,
        "model": None,
        "font_size": "big",
    }
    try:
        adapter = subject.make(subject.context(hostile))
    except GlossError:
        return
    except Exception as exc:
        raise AssertionError(
            f"constructor raised {type(exc).__name__} for bad settings; use a Gloss error"
        ) from exc
    try:
        call(adapter.open, *subject.open_args)
    except GlossError:
        pass
    finally:
        try:
            adapter.close()
        except Exception as exc:
            raise AssertionError(f"close() failed after a refused open: {exc}") from exc


def assert_health(adapter: Any, when: str) -> Health:
    start = time.perf_counter()
    result = call(adapter.health)
    took = time.perf_counter() - start
    assert isinstance(result, Health), f"health() {when} returned {type(result).__name__}"
    assert result.state in HEALTH_STATES, f"health() {when}: bad state {result.state!r}"
    assert isinstance(result.detail, str), f"health() {when}: detail must be a string"
    assert took < HEALTH_DEADLINE_S, (
        f"health() {when} took {took * 1000:.0f} ms. It must return at once, with no I/O."
    )
    return result


def assert_health_from_other_thread(adapter: Any) -> None:
    box: list[Any] = []

    def work() -> None:
        try:
            box.append(adapter.health())
        except BaseException as exc:
            box.append(exc)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(2.0)
    assert box, "health() did not return within 2 s when called from another thread"
    assert isinstance(box[0], Health), f"health() from another thread gave {box[0]!r}"


COMMON_CHECKS = (
    check_declares_identity,
    check_close_is_always_safe,
    check_health_is_instant_in_every_state,
    check_survives_hostile_settings,
)


@dataclass(frozen=True)
class Broken:
    """A deliberately wrong fake, and the one check that must notice."""

    adapter: type
    check: Callable[[Subject], None]
    why: str
    via_registry: bool = True
    subject_name: str | None = None

    @property
    def id(self) -> str:
        return f"{self.adapter.__name__}-{self.check.__name__.removeprefix('check_')}"


def apply_subject_name(subject: Subject, case: Broken) -> Subject:
    """Some broken fakes are registered under a name that differs from their own."""
    if case.subject_name:
        subject.name = case.subject_name
    return subject


def _named(base: type, suffix: str, body: dict[str, Any]) -> type:
    cls = type(f"{base.__name__}{suffix}", (base,), body)
    cls.__qualname__ = cls.__name__
    return cls


def identity_breakers(base: type) -> tuple[Broken, ...]:
    """Fakes whose identity is wrong, for check_declares_identity, built from a good fake."""
    cases = (
        ("WrongName", {"name": f"{base.name}_other"}, "name differs from the registered name"),  # type: ignore[attr-defined]
        ("NoContract", {"REQUIRES_CONTRACT": None}, "REQUIRES_CONTRACT missing"),
        ("FutureContract", {"REQUIRES_CONTRACT": ">=2.0,<3.0"}, "contract version not met"),
        ("BadSpecifier", {"REQUIRES_CONTRACT": "newest"}, "REQUIRES_CONTRACT is nonsense"),
        ("NonBoolFallback", {"ALLOW_AUTO_FALLBACK": "no"}, "ALLOW_AUTO_FALLBACK is not a bool"),
    )
    return tuple(
        Broken(
            _named(base, suffix, body),
            check_declares_identity,
            why,
            via_registry=False,
            subject_name=base.name,  # type: ignore[attr-defined]
        )
        for suffix, body, why in cases
    )


def hostile_settings_breakers(base: type) -> tuple[Broken, ...]:
    """Fakes that crash on bad settings, for check_survives_hostile_settings."""

    def bad(context: AdapterContext) -> bool:
        return "__unknown__" in context.settings

    def ctor_init(self: Any, context: AdapterContext) -> None:
        if bad(context):
            raise ValueError("unknown setting")
        base.__init__(self, context)  # type: ignore[misc]

    def flagged_init(self: Any, context: AdapterContext) -> None:
        base.__init__(self, context)  # type: ignore[misc]
        self._bad = bad(context)

    def open_crash(self: Any, *args: Any) -> Any:
        if self._bad:
            raise TypeError("timeout_ms must be a number")
        return base.open(self, *args)  # type: ignore[attr-defined]

    def open_refuses(self: Any, *args: Any) -> Any:
        if self._bad:
            raise PermanentError("bad settings")
        return base.open(self, *args)  # type: ignore[attr-defined]

    def close_crash(self: Any) -> None:
        if self._bad:
            raise RuntimeError("nothing was opened")
        base.close(self)  # type: ignore[attr-defined]

    cases = (
        ("CtorCrash", {"__init__": ctor_init}, "constructor raises ValueError for bad settings"),
        (
            "OpenCrash",
            {"__init__": flagged_init, "open": open_crash},
            "open() raises TypeError for bad settings",
        ),
        (
            "CloseCrashAfterRefusal",
            {"__init__": flagged_init, "open": open_refuses, "close": close_crash},
            "close() raises after a refused open",
        ),
    )
    return tuple(
        Broken(
            _named(base, suffix, body),
            check_survives_hostile_settings,
            why,
            via_registry=False,
            subject_name=base.name,  # type: ignore[attr-defined]
        )
        for suffix, body, why in cases
    )
