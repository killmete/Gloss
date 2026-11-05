"""The dollar budget guard.

Spend is worked out from the provider's own ``usage`` numbers and the per-model prices in
``[translate.prices]``, then written to the ledger, so it survives restarts. It is a local
estimate: the Anthropic console limit is the only hard stop.

- ``hourly_usd`` limits the spend over the last 3600 seconds.
- ``monthly_usd`` limits the spend since the start of the current calendar month (UTC, which
  is how the provider's console counts).
- At ``warn_at`` of the monthly budget the state becomes ``warn``: calls continue, and the UI
  shows a tray warning.
- At either limit the state is ``hourly_limit`` or ``monthly_limit`` and ``allows_paid_calls``
  is False. The TranslationService then skips paid providers and moves down the chain.
- A model with no price entry is charged at the highest configured price, never as free.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from gloss.core.config import ModelPrice
from gloss.core.contracts import SpendLedger, TokenUsage

BudgetLevel = Literal["ok", "warn", "hourly_limit", "monthly_limit"]
HOUR_S = 3600.0


def cost_of(usage: TokenUsage, price: ModelPrice) -> float:
    """US dollars for one call. Prices are per million tokens."""
    return (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_read_tokens * price.cache_read
        + usage.cache_write_tokens * price.cache_write
    ) / 1_000_000


def start_of_month(timestamp: float) -> float:
    moment = datetime.fromtimestamp(timestamp, UTC)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    """A snapshot for the status model. Dollars are estimates."""

    level: BudgetLevel
    hour_usd: float
    month_usd: float
    hourly_limit_usd: float
    monthly_limit_usd: float

    @property
    def allows_paid_calls(self) -> bool:
        return self.level in ("ok", "warn")

    @property
    def month_share(self) -> float:
        return self.month_usd / self.monthly_limit_usd


class BudgetGuard:
    def __init__(
        self,
        ledger: SpendLedger,
        price_for: Callable[[str], ModelPrice],
        *,
        monthly_usd: float,
        hourly_usd: float,
        warn_at: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ledger = ledger
        self._price_for = price_for
        self._monthly = monthly_usd
        self._hourly = hourly_usd
        self._warn_at = warn_at
        self._clock = clock

    @property
    def ledger_recovered(self) -> bool:
        """True once the ledger file was damaged and restarted (this month's total began at 0)."""
        return bool(getattr(self._ledger, "recovered", 0))

    def record(self, usage: TokenUsage) -> float:
        """Price one call, write it to the ledger, and return its cost in dollars."""
        cost = cost_of(usage, self._price_for(usage.model))
        self._ledger.record(usage, cost, self._clock())
        return cost

    def status(self) -> BudgetStatus:
        now = self._clock()
        hour = self._ledger.total_since(now - HOUR_S)
        month = self._ledger.total_since(start_of_month(now))
        if month >= self._monthly:
            level: BudgetLevel = "monthly_limit"
        elif hour >= self._hourly:
            level = "hourly_limit"
        elif month >= self._monthly * self._warn_at:
            level = "warn"
        else:
            level = "ok"
        return BudgetStatus(level, hour, month, self._hourly, self._monthly)
