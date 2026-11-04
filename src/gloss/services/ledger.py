"""The spend ledger: estimated dollars per provider call, kept across restarts (SQLite)."""

from __future__ import annotations

from pathlib import Path

from gloss.core.contracts import TokenUsage
from gloss.services.sqlite_store import SqliteStore


class SqliteSpendLedger(SqliteStore):
    """Implements ``SpendLedger``. Shares the database file with the translation cache."""

    CONTRACT_PORT = "ledger"
    SCHEMA = """
        CREATE TABLE IF NOT EXISTS spend (
            ts REAL NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            cache_read_tokens INTEGER NOT NULL,
            cache_write_tokens INTEGER NOT NULL,
            cost_usd REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS spend_ts ON spend (ts);
    """

    @classmethod
    def for_contract_test(cls, path: Path) -> SqliteSpendLedger:
        return cls(path)

    def record(self, usage: TokenUsage, cost_usd: float, timestamp: float) -> None:
        row = (
            timestamp,
            usage.provider,
            usage.model,
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
            cost_usd,
        )
        self._run(lambda c: c.execute("INSERT INTO spend VALUES (?,?,?,?,?,?,?,?)", row))

    def total_since(self, timestamp: float) -> float:
        def work(conn):
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM spend WHERE ts >= ?", (timestamp,)
            ).fetchone()
            return float(row[0])

        return self._run(work)
