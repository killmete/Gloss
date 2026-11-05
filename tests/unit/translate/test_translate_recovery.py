"""A damaged database file is set aside and a fresh one started (never deleted)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from testkit.discovery import NO_USER_CONFIG

from gloss.core.config import load_config
from gloss.core.contracts import CacheKey, PermanentError, TokenUsage
from gloss.services import sqlite_store
from gloss.services.budget import BudgetGuard
from gloss.services.cache import SqliteTranslationCache
from gloss.services.ledger import SqliteSpendLedger
from gloss.services.translation_service import (
    LEDGER_RESET_NOTE,
    ServiceSettings,
    TranslationService,
)

CONFIG = load_config(user_path=NO_USER_CONFIG).config
KEY = CacheKey("cloud", "m", "en", "g1", "你好")
USAGE = TokenUsage("cloud", "claude-haiku-5-5", 10, 10, 0, 0)
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC).timestamp()


def damaged(tmp_path: Path) -> list[Path]:
    return sorted(tmp_path.glob("*.damaged-*"))


def real_db(tmp_path: Path) -> Path:
    path = tmp_path / "gloss.sqlite3"
    with SqliteTranslationCache(path) as c:
        for i in range(300):
            c.put(CacheKey("cloud", "m", "en", "g1", f"secret row {i}"), "x" * 200)
    return path


def exercise(cache, ledger):
    assert cache.get(KEY) is None
    cache.put(KEY, "hello")
    assert cache.get(KEY) == "hello"
    ledger.record(USAGE, 0.5, NOW)
    assert ledger.total_since(NOW - 10) == pytest.approx(0.5)


def test_garbage_file_is_set_aside_and_stores_work(tmp_path, caplog):
    path = tmp_path / "gloss.sqlite3"
    path.write_bytes(b"SECRETROW-garbage" * 500)
    with caplog.at_level(logging.WARNING):
        with SqliteTranslationCache(path) as cache, SqliteSpendLedger(path) as ledger:
            exercise(cache, ledger)
            assert cache.recovered >= 1
    copies = damaged(tmp_path)
    assert len(copies) == 1 and b"SECRETROW" in copies[0].read_bytes()
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "damaged" in text and "SECRETROW" not in text and "你好" not in text


def test_truncated_file_is_handled_the_same(tmp_path):
    path = real_db(tmp_path)
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2 + 100])
    with SqliteTranslationCache(path) as cache, SqliteSpendLedger(path) as ledger:
        exercise(cache, ledger)
    assert len(damaged(tmp_path)) == 1


def test_two_open_stores_both_recover(tmp_path):
    path = real_db(tmp_path)
    ledger = SqliteSpendLedger(path)
    cache = SqliteTranslationCache(path)
    try:
        ledger.total_since(0)  # the ledger's connection stays open on the good file
        ledger._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with path.open("r+b") as f:  # damage it in place
            f.write(bytes([255]) * 4096)
        exercise(cache, ledger)  # the cache hits the damage first; the ledger is still open
        assert cache.recovered >= 1 and ledger.recovered >= 1
    finally:
        cache.close()
        ledger.close()
    assert len(damaged(tmp_path)) == 1


def test_rename_failure_still_raises_permanent_error(tmp_path, monkeypatch):
    path = tmp_path / "gloss.sqlite3"
    path.write_bytes(b"garbage" * 1000)

    def refuse(self, target):
        raise PermissionError("in use")

    monkeypatch.setattr(Path, "rename", refuse)
    with SqliteTranslationCache(path) as cache:
        with pytest.raises(PermanentError):
            cache.get(KEY)
    assert path.exists() and not damaged(tmp_path)


def test_unrelated_errors_do_not_trigger_recovery(tmp_path):
    with SqliteTranslationCache(tmp_path / "gloss.sqlite3") as cache:
        cache.put(KEY, "hello")
        assert cache.recovered == 0
    assert not damaged(tmp_path)
    assert sqlite_store._is_corrupt(Exception("malformed")) is False


def test_service_health_notes_reset_ledger(tmp_path):
    path = tmp_path / "gloss.sqlite3"
    path.write_bytes(b"garbage" * 1000)
    ledger = SqliteSpendLedger(path)
    guard = BudgetGuard(
        ledger,
        CONFIG.translate.price_for,
        monthly_usd=10.0,
        hourly_usd=5.0,
        warn_at=0.8,
        clock=lambda: NOW,
    )
    service = TranslationService([], settings=ServiceSettings.from_config(CONFIG), budget=guard)
    try:
        assert service.health().state == "ok"
        service.open()  # reads the ledger, which recovers
        health = service.health()
        assert health.state == "degraded" and health.detail == LEDGER_RESET_NOTE
    finally:
        ledger.close()
