"""The TranslationService: everything between "lines to translate" and "finished results".

Pipeline for one batch (``translate``)::

    redact -> in-batch dedupe -> cache lookup -> [budget, breaker] -> primary translator
           -> retry (jittered backoff) -> split / re-queue failed lines -> fallback chain
           -> cache write -> restore redacted values -> results

The caller (the pipeline's translate stage worker) owns batching and timing. Use
``gloss.services.batching.collect_batch`` to build batches of ``settings.batch_max_lines``
lines or ``settings.batch_max_wait_ms``, then call ``translate`` once per batch. All methods
except ``status`` and ``health`` are meant for that one worker thread. ``status`` and
``health`` only read small snapshots, so the UI thread may call them.

Rules it applies (ports.py failure model, ADR-0001 section 5):
- ``TransientError``: retried with jittered backoff, then one circuit-breaker failure and the
  next translator is used. Any other stray exception is treated the same way.
- ``AdapterUnavailable``: the translator is skipped and tried again after the breaker
  cooldown, or at once after ``retry_unavailable``. ``PermanentError``: the translator is
  switched off for the session, with the reason shown in the status.
- Failed lines never touch the breaker. Refused or truncated lines split the batch in halves
  until the offender is alone, then that line alone moves down the chain. Missing or invalid
  lines are re-sent once. A line that fails everywhere comes back ``failed``.
- A paid (non ``LOCAL_ONLY``) translator is skipped while the budget guard says stop.
- Text is redacted before it reaches any translator or the cache, including the context.
- Results with ``status="ok"`` are cached, except lines flagged ``low_quality``. Passthrough
  and failed results are never cached.
"""

from __future__ import annotations

import logging
import random
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from gloss.core.contracts import (
    AdapterUnavailable,
    CacheKey,
    ContextLine,
    FailureReason,
    GlossError,
    Health,
    PermanentError,
    TransientError,
    TranslationCache,
    TranslationItem,
    TranslationRequest,
    TranslationResponse,
    TranslationResult,
    Translator,
)
from gloss.services.budget import BudgetGuard, BudgetStatus
from gloss.services.glossary import GlossaryStore
from gloss.services.redaction import Redactor, restore
from gloss.services.reliability import CircuitBreaker, RetryPolicy, call_with_retry

log = logging.getLogger(__name__)

RETRYABLE_AS_A_GROUP: tuple[FailureReason, ...] = ("refused", "truncated")


@dataclass(frozen=True, slots=True)
class ServiceSettings:
    """The slice of config the service needs. Build it with ``from_config``."""

    target_lang: str
    context_lines: int
    redact_digit_runs: int
    batch_max_lines: int
    batch_max_wait_ms: int
    retry: RetryPolicy
    breaker_failures: int
    breaker_cooldown_s: float

    @classmethod
    def from_config(cls, config: Any) -> ServiceSettings:
        """``config`` is a ``GlossConfig``."""
        t, r = config.translate, config.reliability
        return cls(
            target_lang=t.target_lang,
            context_lines=t.context_lines,
            redact_digit_runs=t.redact_digit_runs,
            batch_max_lines=t.batch_max_lines,
            batch_max_wait_ms=t.batch_max_wait_ms,
            retry=RetryPolicy(r.retry_max, r.retry_base_delay_ms, r.retry_max_delay_ms),
            breaker_failures=r.breaker_failures,
            breaker_cooldown_s=r.breaker_cooldown_s,
        )


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    """``state``: ``ok``, ``open`` (breaker), ``half_open``, ``unavailable``, ``disabled``."""

    name: str
    state: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class TranslationStatus:
    """What the UI status model shows. Dollar figures are local estimates.

    ``banner`` is a lasting plain-English message ("" when all is well). ``provider`` is the
    translator that produced the latest translations, and ``using_fallback`` is True when
    that was not the primary.
    """

    health: Health
    banner: str
    provider: str | None
    using_fallback: bool
    providers: tuple[ProviderStatus, ...]
    budget: BudgetStatus | None
    session_cost_usd: float


@dataclass
class _Provider:
    translator: Translator
    breaker: CircuitBreaker
    cloud: bool
    opened: bool = False
    disabled: str | None = None
    unavailable_until: float = 0.0
    unavailable_reason: str = ""

    @property
    def name(self) -> str:
        return self.translator.name


@dataclass
class _Unit:
    """One distinct redacted text within a batch."""

    uid: str
    text: str
    low_quality: bool
    failure: FailureReason = "other"


@dataclass(frozen=True, slots=True)
class _Outcome:
    text: str
    provider: str
    model: str | None
    status: str
    failure: FailureReason | None = None
    cached: bool = False
    rough: bool = False
    latency_ms: int = 0


@dataclass
class _Run:
    """Bookkeeping for one ``translate`` call."""

    source_lang: str | None
    glossary: Any
    started: float
    calls_left: int
    outcomes: dict[str, _Outcome] = field(default_factory=dict)


class TranslationService:
    def __init__(
        self,
        translators: Sequence[Translator],
        *,
        settings: ServiceSettings,
        cache: TranslationCache | None = None,
        budget: BudgetGuard | None = None,
        glossary: GlossaryStore | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        """``translators`` is the chain from ``Registry.resolve_chain``, primary first."""
        self._s = settings
        self._cache = cache
        self._budget = budget
        self._glossary = glossary
        self._clock = clock
        self._sleep = sleep
        self._rng = rng if rng is not None else random.Random()
        self._redactor = Redactor(settings.redact_digit_runs)
        self._context: deque[ContextLine] = deque(maxlen=settings.context_lines)
        self._providers = [
            _Provider(
                t,
                CircuitBreaker(settings.breaker_failures, settings.breaker_cooldown_s, clock),
                cloud=vars(type(t)).get("LOCAL_ONLY") is not True,
            )
            for t in translators
        ]
        self._budget_status: BudgetStatus | None = None
        self._session_usd = 0.0
        self._last_provider: str | None = None
        self._last_all_failed = False

    # ---- lifecycle ----------------------------------------------------------------------

    def open(self) -> None:
        """Open every translator now, so the status shows a missing key right away."""
        for provider in self._providers:
            self._ensure_open(provider)
        self._refresh_budget()

    def close(self) -> None:
        for provider in self._providers:
            try:
                provider.translator.close()
            except Exception:  # Closing must never crash the app.
                log.warning("Closing translator %s failed", provider.name)
            provider.opened = False
        if self._cache is not None:
            try:
                self._cache.close()
            except GlossError:
                pass

    def retry_unavailable(self) -> None:
        """Try translators that were unavailable or switched off again, on the next batch.

        Call it after the user stores an API key or fixes a setting.
        """
        for provider in self._providers:
            provider.unavailable_until = 0.0
            provider.disabled = None
            provider.opened = False

    # ---- status -------------------------------------------------------------------------

    def status(self) -> TranslationStatus:
        providers = tuple(self._provider_status(p) for p in self._providers)
        budget = self._budget_status
        banner, state = self._banner(providers, budget)
        primary = self._providers[0].name if self._providers else None
        using_fallback = self._last_provider is not None and self._last_provider != primary
        if self._last_all_failed:
            state, banner = "down", banner or "Translation is failing. Showing the original text."
        elif using_fallback and state == "ok":
            state = "degraded"
        health = Health(state, banner)  # type: ignore[arg-type]
        return TranslationStatus(
            health,
            banner,
            self._last_provider,
            using_fallback,
            providers,
            budget,
            self._session_usd,
        )

    def health(self) -> Health:
        return self.status().health

    def _provider_status(self, p: _Provider) -> ProviderStatus:
        if p.disabled:
            return ProviderStatus(p.name, "disabled", p.disabled)
        if p.unavailable_until > self._clock():
            return ProviderStatus(p.name, "unavailable", p.unavailable_reason)
        state = p.breaker.state
        if state == "open":
            return ProviderStatus(p.name, "open", f"retrying in {int(p.breaker.retry_in_s())} s")
        return ProviderStatus(p.name, "half_open" if state == "half_open" else "ok")

    def _banner(
        self, providers: tuple[ProviderStatus, ...], budget: BudgetStatus | None
    ) -> tuple[str, str]:
        if providers and providers[0].state != "ok":
            first = providers[0]
            reason = {
                "disabled": first.detail,
                "unavailable": first.detail,
                "open": "Translation service is not responding, so a fallback is in use "
                f"({first.detail}).",
            }.get(first.state, "")
            return reason, "degraded"
        if budget is not None and not budget.allows_paid_calls:
            which = "monthly" if budget.level == "monthly_limit" else "hourly"
            return (
                f"The {which} spending limit was reached, so paid translation is paused. "
                "Showing the fallback.",
                "degraded",
            )
        if budget is not None and budget.level == "warn":
            share = int(budget.month_share * 100)
            return f"{share}% of the monthly translation budget is used.", "ok"
        return "", "ok"

    # ---- translating --------------------------------------------------------------------

    def translate(
        self, items: Sequence[TranslationItem], source_lang: str | None = None
    ) -> tuple[TranslationResult, ...]:
        """Translate raw (unredacted) lines. Returns one result per item, in order.

        Never raises for provider trouble: a line that could not be translated anywhere
        comes back ``failed``. ``source_text`` is the original text and a ``[#n]`` placeholder
        is replaced by the original number or email in ``text``.
        """
        if not items:
            return ()
        glossary = self._glossary.current() if self._glossary is not None else _empty_glossary()
        redacted = [self._redactor.redact(item.text) for item in items]
        units: dict[str, _Unit] = {}
        for item, red in zip(items, redacted, strict=True):
            unit = units.setdefault(red.text, _Unit(f"u{len(units)}", red.text, False))
            unit.low_quality = unit.low_quality or item.low_quality
        run = _Run(source_lang, glossary, self._clock(), calls_left=4 * len(units) + 8)

        self._refresh_budget()
        self._last_all_failed = False
        pending = [u for u in units.values() if not self._from_cache(u, run)]
        if pending:
            leftover = self._through_chain(pending, run)
            for unit in leftover:
                run.outcomes[unit.text] = _Outcome(
                    "", "", None, "failed", unit.failure, latency_ms=self._elapsed_ms(run)
                )
            self._last_all_failed = len(leftover) == len(pending)
        results = tuple(
            self._result(item, red.values, run.outcomes[red.text])
            for item, red in zip(items, redacted, strict=True)
        )
        self._remember(units, run)
        return results

    # -- cache --

    def _key(self, provider: _Provider, run: _Run, text: str) -> CacheKey:
        model = getattr(provider.translator, "model", None)
        return CacheKey(
            provider.name,
            model if isinstance(model, str) else None,
            self._s.target_lang,
            run.glossary.version,
            text,
        )

    def _from_cache(self, unit: _Unit, run: _Run) -> bool:
        """Fill the outcome from the cache. Look at the primary first, then later providers
        only while the earlier ones cannot be used right now."""
        if self._cache is None:
            return False
        for provider in self._providers:
            try:
                hit = self._cache.get(self._key(provider, run, unit.text))
            except GlossError:
                log.warning("Translation cache read failed; continuing without it")
                return False
            if hit is not None:
                model = getattr(provider.translator, "model", None)
                run.outcomes[unit.text] = _Outcome(
                    hit, provider.name, model if isinstance(model, str) else None, "ok", cached=True
                )
                return True
            if self._usable_now(provider):
                return False
        return False

    def _store(
        self, provider: _Provider, unit: _Unit, result: TranslationResult, run: _Run
    ) -> None:
        if self._cache is None or unit.low_quality or result.status != "ok":
            return
        try:
            self._cache.put(self._key(provider, run, unit.text), result.text)
        except GlossError:
            log.warning("Translation cache write failed; continuing without it")

    # -- providers --

    def _usable_now(self, p: _Provider) -> bool:
        """Could this provider be called? Does not use up a half-open trial."""
        if p.disabled or p.unavailable_until > self._clock() or p.breaker.state == "open":
            return False
        return not (p.cloud and self._budget_blocks())

    def _budget_blocks(self) -> bool:
        return self._budget_status is not None and not self._budget_status.allows_paid_calls

    def _refresh_budget(self) -> None:
        if self._budget is None:
            return
        try:
            self._budget_status = self._budget.status()
        except GlossError:
            log.warning("Could not read the spend ledger; the budget guard is not applied")

    def _ensure_open(self, p: _Provider) -> bool:
        if p.disabled or p.unavailable_until > self._clock():
            return False
        if p.opened:
            return True
        try:
            p.translator.open()
        except AdapterUnavailable as exc:
            self._mark_unavailable(p, str(exc) or f"{p.name} is not available")
        except PermanentError as exc:
            self._disable(p, str(exc))
        except Exception as exc:
            log.warning("Opening translator %s failed (%s)", p.name, type(exc).__name__)
            self._mark_unavailable(p, f"{p.name} could not start")
        else:
            p.opened = True
        return p.opened

    def _mark_unavailable(self, p: _Provider, reason: str) -> None:
        p.unavailable_until = self._clock() + self._s.breaker_cooldown_s
        p.unavailable_reason = reason
        p.opened = False
        p.breaker.release_trial()

    def _disable(self, p: _Provider, reason: str) -> None:
        log.warning("Translator %s switched off for this session: %s", p.name, reason)
        p.disabled = reason or f"{p.name} was switched off"
        p.opened = False
        p.breaker.release_trial()

    def _through_chain(self, pending: list[_Unit], run: _Run) -> list[_Unit]:
        """Send units down the chain. Returns the ones nobody translated."""
        remaining = pending
        for provider in self._providers:
            if not remaining:
                break
            if not self._ensure_open(provider) or not self._claim(provider):
                continue
            remaining = self._attempt(provider, remaining, run, retried=False)
        return remaining

    def _claim(self, p: _Provider) -> bool:
        """Final go/no-go for a call. Takes the half-open trial, so call it last."""
        if p.cloud and self._budget_blocks():
            return False
        return p.breaker.allow()

    # -- one provider --

    def _attempt(
        self, p: _Provider, units: list[_Unit], run: _Run, *, retried: bool
    ) -> list[_Unit]:
        """Translate ``units`` with ``p``. Returns the units still untranslated."""
        if run.calls_left <= 0:
            return units
        run.calls_left -= 1
        response = self._call(p, units, run)
        if response is None:
            return units
        by_id = {r.line_id: r for r in response.results}
        failed: list[_Unit] = []
        for unit in units:
            result = by_id[unit.uid]
            if result.status == "failed":
                unit.failure = result.failure or "other"
                failed.append(unit)
                continue
            run.outcomes[unit.text] = _Outcome(
                result.text,
                result.provider,
                result.model,
                result.status,
                rough=result.rough,
                latency_ms=self._elapsed_ms(run),
            )
            self._store(p, unit, result, run)
        if len(failed) < len(units):
            self._last_provider = p.name
        return self._retry_failed(p, failed, run, retried)

    def _retry_failed(
        self, p: _Provider, failed: list[_Unit], run: _Run, retried: bool
    ) -> list[_Unit]:
        if not failed:
            return []
        grouped = [u for u in failed if u.failure in RETRYABLE_AS_A_GROUP]
        again = [u for u in failed if u.failure not in RETRYABLE_AS_A_GROUP]
        left: list[_Unit] = []
        if len(grouped) > 1:
            middle = len(grouped) // 2
            for half in (grouped[:middle], grouped[middle:]):
                left += self._follow_up(p, half, run, retried)
        else:
            left += grouped
        if again:
            left += again if retried else self._follow_up(p, again, run, True)
        return left

    def _follow_up(self, p: _Provider, units: list[_Unit], run: _Run, retried: bool) -> list[_Unit]:
        if not self._usable_now(p) or not self._claim(p):
            return units
        return self._attempt(p, units, run, retried=retried)

    def _call(self, p: _Provider, units: list[_Unit], run: _Run) -> TranslationResponse | None:
        """One call with retries. Returns None if the provider gave nothing usable."""
        request = TranslationRequest(
            items=tuple(TranslationItem(u.uid, u.text, u.low_quality) for u in units),
            source_lang=run.source_lang,
            target_lang=self._s.target_lang,
            context=tuple(self._context),
            glossary=run.glossary,
        )
        try:
            response = call_with_retry(
                lambda: p.translator.translate(request),
                self._s.retry,
                sleep=self._sleep,
                rng=self._rng,
            )
        except AdapterUnavailable as exc:
            self._mark_unavailable(p, str(exc) or f"{p.name} is not available")
            return None
        except PermanentError as exc:
            self._disable(p, str(exc))
            return None
        except TransientError:
            p.breaker.record_failure()
            log.warning("Translator %s failed after retries (breaker: %s)", p.name, p.breaker.state)
            return None
        if not self._well_formed(request, response):
            log.warning("Translator %s broke the contract (wrong results); ignoring them", p.name)
            p.breaker.record_failure()
            return None
        p.breaker.record_success()
        self._bill(response)
        return response

    @staticmethod
    def _well_formed(request: TranslationRequest, response: Any) -> bool:
        if not isinstance(response, TranslationResponse):
            return False
        ids = [r.line_id for r in response.results]
        return ids == [i.line_id for i in request.items]

    def _bill(self, response: TranslationResponse) -> None:
        if self._budget is None or response.usage is None:
            return
        try:
            self._session_usd += self._budget.record(response.usage)
        except GlossError:
            log.warning("Could not write to the spend ledger")
        self._refresh_budget()

    # -- output --

    def _elapsed_ms(self, run: _Run) -> int:
        return int((self._clock() - run.started) * 1000)

    @staticmethod
    def _result(
        item: TranslationItem, values: tuple[str, ...], outcome: _Outcome
    ) -> TranslationResult:
        common: dict[str, Any] = {
            "line_id": item.line_id,
            "source_text": item.text,
            "provider": outcome.provider,
            "model": outcome.model,
            "cached": outcome.cached,
            "latency_ms": outcome.latency_ms,
            "rough": outcome.rough,
        }
        if outcome.status == "failed":
            return TranslationResult(text="", status="failed", failure=outcome.failure, **common)
        if outcome.status == "passthrough":
            return TranslationResult(text=item.text, status="passthrough", **common)
        return TranslationResult(text=restore(outcome.text, values), **common)

    def _remember(self, units: dict[str, _Unit], run: _Run) -> None:
        """Add translated lines to the context for the next batch (redacted text only)."""
        for unit in units.values():
            outcome = run.outcomes[unit.text]
            if outcome.status == "ok" and not unit.low_quality:
                self._context.append(ContextLine(unit.text, outcome.text))


def _empty_glossary() -> Any:
    from gloss.core.contracts import Glossary

    return Glossary()
