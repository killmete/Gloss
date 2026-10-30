"""The interfaces between the pipeline and the outside world.

The pipeline and services depend only on these Protocols. An adapter depends only on
``gloss.core.contracts``, its own third-party library, and shared helpers in
``gloss.services``. An adapter never imports another adapter.

Declaring an adapter
--------------------
Each adapter is a class with a class attribute ``name``, a class attribute
``REQUIRES_CONTRACT`` such as ``">=1.0,<2.0"``, a constructor taking one ``AdapterContext``,
and the ``@register_adapter`` decorator from ``gloss.core.registry``.

- The constructor must be cheap and must not touch the network, disk models or heavy
  libraries. Import such libraries inside ``open``, because the registry imports every
  adapter module at startup.
- Set the class attribute ``ALLOW_AUTO_FALLBACK = False`` if the adapter must never be
  entered automatically through a fallback list. It defaults to True.
- A ``Translator`` that keeps all text on this PC must set ``LOCAL_ONLY = True`` in its
  own class body. It is not inherited: a subclass of a local translator must declare it
  again. A translator without it counts as sending text off the PC, so it is left out of
  the chain when ``privacy.cloud_translation`` is false. If no translator is left, the
  original text is shown.

Storage ports
-------------
These ports raise only ``GlossError`` subclasses: a backend's own exceptions are wrapped.

``SecretStore``, ``TranslationCache`` and ``SpendLedger`` are not in the registry, because
config does not choose them. A class under ``gloss.services`` joins their contract tests
by setting ``CONTRACT_PORT = "secret" | "cache" | "ledger"`` and offering a classmethod
``for_contract_test(path)``. Two calls with one ``path`` must share a backing store under
that path, so a test can prove data survives a restart. A ``SecretStore`` must use a
throwaway backend there, never the real Windows Credential Manager.

Failure model
-------------
Adapters make one attempt and report what happened. They never retry, sleep or switch
providers themselves, and SDK retries must be turned off. The caller reacts as follows:

=====================  ==================================================================
Raised                 Caller's reaction
=====================  ==================================================================
AdapterUnavailable     Skip to the next adapter in the chain. Try this one again later.
TransientError         Retry with jittered backoff, honouring ``retry_after_s``. After
                       ``reliability.retry_max`` attempts, count one circuit-breaker
                       failure and use the next adapter for this work.
PermanentError         Disable the adapter for this session, show the user why, and use
                       the next adapter.
Any other exception    Treat as TransientError and log it. It must never crash the app.
=====================  ==================================================================

After ``reliability.breaker_failures`` failures in a row the circuit opens: the adapter is
skipped for ``reliability.breaker_cooldown_s``, then tried again. Any fallback in use turns
the status dot amber. An exhausted chain means red for capture and OCR. The OCR fallback
list is empty by default, on purpose: visible failure beats convincing wrong text.

A ``Translator`` handles one problem line by returning it with ``status="failed"``, not by
raising, so one refused line cannot trip the breaker. The TranslationService then splits or
retries the batch, or sends the line down the chain. Exceptions mean the whole call gave
nothing usable. Billed tokens from a call with failed lines are still reported in ``usage``.

Threading and timeouts
----------------------
- Each stage has one worker thread with a bounded queue, and the latest frame wins.
- ``open``, ``close`` and the stage's main call of a capture, OCR or translator adapter all
  run on that stage's worker, so these adapters need no locks for them.
- ``health`` may be called from any thread and must return at once, without I/O.
- The caller applies a timeout to every main call. Adapters must also set the timeouts of
  their own network or library calls, so a call ends rather than leaks a stuck thread.
- Every ``Renderer`` method except ``open`` and ``close`` may be called from any thread and
  must return at once, handing the work to the Qt main thread. ``open`` and ``close`` run on
  the Qt main thread.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from gloss.core.contracts.models import (
    CacheKey,
    CaptureTarget,
    Frame,
    FrameMeta,
    Health,
    MonitorInfo,
    Rect,
    RenderItem,
    TextBlock,
    TokenUsage,
    TranslationRequest,
    TranslationResponse,
)

PortName = Literal["capture", "ocr", "translate", "render"]


@runtime_checkable
class CaptureSource(Protocol):
    """Provides screen images. Registered under port ``capture``."""

    name: str

    def list_monitors(self) -> Sequence[MonitorInfo]:
        """Return connected monitors with stable IDs. Works before ``open``."""
        ...

    def open(self, target: CaptureTarget) -> None:
        """Start capturing. Called again, after ``close``, to change target.

        Raises AdapterUnavailable if the target cannot be captured.
        """
        ...

    def grab(self) -> Frame | None:
        """Return the latest frame, or None if nothing is available right now.

        A push-based adapter keeps its last frame and returns it with ``fresh=False`` when
        nothing has arrived since. Black frames are returned like any other: the check for
        them is shared and sits outside adapters.
        """
        ...

    def close(self) -> None:
        """Release resources. Safe to call twice, or without ``open``."""
        ...

    def health(self) -> Health: ...


@runtime_checkable
class OCREngine(Protocol):
    """Turns a frame into lines of text. Registered under port ``ocr``."""

    name: str
    languages: tuple[str, ...]

    def open(self) -> None:
        """Load models for every language in ``settings["languages"]``.

        Raises AdapterUnavailable if a model or language pack is missing on this PC, and
        PermanentError for a code the engine can never read, such as ``zz``. An engine must
        not skip a requested language, or read it with a recogniser for another one.
        """
        ...

    def recognize(self, frame: Frame, regions: Sequence[Rect] | None) -> Sequence[TextBlock]:
        """Read text, top to bottom then left to right.

        ``regions`` are in frame coordinates: None means the whole frame, and an empty
        sequence returns no blocks. Every returned bbox is in frame coordinates, even for
        a region.
        """
        ...

    def close(self) -> None:
        """Unload models. Safe to call twice, or without ``open``."""
        ...

    def health(self) -> Health: ...


@runtime_checkable
class Translator(Protocol):
    """Turns a batch of lines into translations. Registered under port ``translate``."""

    name: str

    def open(self) -> None:
        """Prepare the provider, within a config timeout.

        Raises AdapterUnavailable if it has no API key, or an optional pack is absent.
        """
        ...

    def translate(self, request: TranslationRequest) -> TranslationResponse:
        """Translate one batch with one provider call.

        Returns exactly one result per request item, in request order. ``source_text``
        echoes the item's text. A line missing from the provider's reply comes back with
        ``status="failed"`` and ``failure="missing"``. An empty request returns no results
        and makes no call.
        """
        ...

    def close(self) -> None:
        """Release resources. Safe to call twice, or without ``open``."""
        ...

    def health(self) -> Health: ...


@runtime_checkable
class Renderer(Protocol):
    """Shows translations to the user. Registered under port ``render``."""

    name: str

    def open(self) -> None:
        """Create windows. Every window must be excluded from screen capture."""
        ...

    def show(self, items: Sequence[RenderItem], frame_meta: FrameMeta | None) -> None:
        """Show lines. ``frame_meta`` is the latest frame's details, if there is one."""
        ...

    def clear(self) -> None:
        """Remove the lines on display. History in a panel may stay."""
        ...

    def set_visible(self, visible: bool) -> None: ...

    def set_status(self, health: Health, banner: str = "") -> None:
        """Show overall status, plus a lasting banner if ``banner`` is not empty."""
        ...

    def close(self) -> None:
        """Destroy windows. Safe to call twice, or without ``open``."""
        ...

    def health(self) -> Health: ...


@runtime_checkable
class SecretStore(Protocol):
    """Holds API keys. Implementations must never log or print a value."""

    def get(self, name: str) -> str | None:
        """Return the secret, or None if there is none. A missing secret is not an error."""
        ...

    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> None:
        """Remove the secret. Deleting one that does not exist does nothing and must not raise."""
        ...


@runtime_checkable
class TranslationCache(Protocol):
    """Stores finished translations locally."""

    def get(self, key: CacheKey) -> str | None: ...

    def put(self, key: CacheKey, text: str) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class SpendLedger(Protocol):
    """Records estimated spend, so the budget guard survives restarts."""

    def record(self, usage: TokenUsage, cost_usd: float, timestamp: float) -> None:
        """Add one entry. ``timestamp`` is ``time.time()`` seconds."""
        ...

    def total_since(self, timestamp: float) -> float:
        """Return US dollars recorded at or after ``timestamp``."""
        ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AdapterContext:
    """What every adapter constructor receives.

    ``settings`` is the adapter's own config table as plain data, and the adapter checks it.
    ``secrets`` is the key store, for adapters that need one.
    """

    settings: Mapping[str, Any] = field(default_factory=dict)
    secrets: SecretStore | None = None


PORTS: dict[str, type] = {
    "capture": CaptureSource,
    "ocr": OCREngine,
    "translate": Translator,
    "render": Renderer,
}
