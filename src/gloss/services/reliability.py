"""Central retry with jittered backoff, and the circuit breaker.

Adapters make one attempt and report what happened (see ``ports.py``). This module is where
retrying lives. It is pure logic with an injectable clock, sleep and random source, so tests
need no real waiting.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

from gloss.core.contracts import AdapterUnavailable, GlossError, PermanentError, TransientError

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """``retry_max`` is the number of retries after the first attempt."""

    retry_max: int
    base_delay_ms: int
    max_delay_ms: int

    def delay_s(self, retry_number: int, rng: random.Random, retry_after_s: float | None) -> float:
        """Seconds to wait before retry ``retry_number`` (1 for the first retry).

        Exponential growth capped at ``max_delay_ms``, then "equal jitter": half fixed, half
        random, so a burst of failures does not retry in step. A server-supplied
        ``retry_after_s`` is a lower bound.
        """
        ceiling = min(self.max_delay_ms, self.base_delay_ms * (2 ** (retry_number - 1))) / 1000
        delay = ceiling / 2 + rng.uniform(0, ceiling / 2)
        if retry_after_s is not None:
            delay = max(delay, retry_after_s)
        return delay

    def gives_up_on(self, retry_after_s: float | None) -> bool:
        """True if the server asks for a wait longer than we are willing to block for."""
        return retry_after_s is not None and retry_after_s * 1000 > self.max_delay_ms


def as_transient(exc: Exception) -> TransientError:
    """Turn an unexpected exception into a TransientError, per the failure model."""
    if isinstance(exc, TransientError):
        return exc
    # Log the type only: the message of a foreign exception could carry chat text.
    log.warning("Unexpected %s from an adapter; treating it as transient", type(exc).__name__)
    return TransientError(f"unexpected {type(exc).__name__}")


def call_with_retry[T](
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> T:
    """Call ``fn`` until it returns, retrying only on TransientError (or a stray exception).

    AdapterUnavailable and PermanentError pass straight through. After the retries are used
    up, the last TransientError is raised. A ``retry_after_s`` longer than
    ``max_delay_ms`` ends the retrying at once, since waiting that long would stall the
    stage worker.
    """
    generator = rng if rng is not None else random.Random()
    retry = 0
    while True:
        try:
            return fn()
        except (AdapterUnavailable, PermanentError):
            raise
        except Exception as exc:
            if isinstance(exc, GlossError) and not isinstance(exc, TransientError):
                raise
            error = as_transient(exc)
            retry += 1
            if retry > policy.retry_max or policy.gives_up_on(error.retry_after_s):
                raise error from None
            sleep(policy.delay_s(retry, generator, error.retry_after_s))


class CircuitBreaker:
    """Opens after ``failures`` failures in a row, and stays open for ``cooldown_s``.

    After the cooldown one trial call is let through (half-open). If it succeeds the circuit
    closes, and if it fails the circuit opens again for another cooldown.
    """

    def __init__(
        self,
        failures: int,
        cooldown_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failures
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._trial_out = False

    @property
    def state(self) -> str:
        """``closed``, ``open`` or ``half_open``. Does not change anything."""
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self._cooldown_s:
            return "half_open"
        return "open"

    def retry_in_s(self) -> float:
        """Seconds until the next trial, or 0 if calls are allowed."""
        if self._opened_at is None:
            return 0.0
        return max(0.0, self._cooldown_s - (self._clock() - self._opened_at))

    def allow(self) -> bool:
        """Ask before a call. In half-open state only one trial is allowed at a time."""
        state = self.state
        if state == "closed":
            return True
        if state == "half_open" and not self._trial_out:
            self._trial_out = True
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._trial_out = False

    def record_failure(self) -> None:
        self._trial_out = False
        self._failures += 1
        if self._opened_at is not None or self._failures >= self._threshold:
            self._opened_at = self._clock()

    def release_trial(self) -> None:
        """A trial ended without telling us anything (for example, no key). Allow another."""
        self._trial_out = False
