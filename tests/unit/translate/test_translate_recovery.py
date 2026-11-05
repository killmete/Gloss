"""A damaged database file is set aside and a fresh one started (never deleted)."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
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


def guard_for(ledger, clock):
    return BudgetGuard(
        ledger,
        CONFIG.translate.price_for,
        monthly_usd=10.0,
        hourly_usd=100.0,
        warn_at=0.8,
        clock=clock,
    )


def test_reset_notice_in_health_survives_restart_until_month_ends(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_store, "_now", lambda: NOW)
    path = tmp_path / "gloss.sqlite3"
    path.write_bytes(b"garbage" * 1000)
    settings = ServiceSettings.from_config(CONFIG)
    ledger = SqliteSpendLedger(path)
    service = TranslationService([], settings=settings, budget=guard_for(ledger, lambda: NOW))
    service.open()  # reads the ledger, which recovers
    health = service.health()
    ledger.close()
    assert health.state == "degraded"
    assert health.detail == (
        "Spending record was reset on 2026-11-15 after damage; "
        "spending before then is not counted this month."
    )
    # A restart: new objects on the same file still show it, with a warn text alongside.
    later = SqliteSpendLedger(path)
    later.record(USAGE, 9.0, NOW)
    restarted = TranslationService([], settings=settings, budget=guard_for(later, lambda: NOW))
    restarted.open()
    detail = restarted.health().detail
    assert detail.startswith("90% of the monthly") and "was reset on 2026-11-15" in detail
    # Over the hard limit, that banner takes priority.
    later.record(USAGE, 5.0, NOW)
    restarted.open()
    assert "spending limit" in restarted.health().detail
    # Next calendar month the notice is gone.
    nxt = NOW + 40 * 86400
    quiet = TranslationService([], settings=settings, budget=guard_for(later, lambda: nxt))
    quiet.open()
    assert quiet.health().state == "ok" and quiet.health().detail == ""
    later.close()


def test_recovery_races_with_another_thread_using_the_second_store(tmp_path):
    path = real_db(tmp_path)
    cache, ledger = SqliteTranslationCache(path), SqliteSpendLedger(path)
    errors: list[BaseException] = []
    stop = threading.Event()
    ledger.total_since(0)

    def hammer():
        while not stop.is_set():
            try:
                ledger.record(USAGE, 0.01, NOW)
                ledger.total_since(0)
            except PermanentError:
                pass
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                return

    thread = threading.Thread(target=hammer)
    thread.start()
    try:
        time.sleep(0.05)
        real = cache._connection
        calls = []

        def flaky():
            if not calls:
                calls.append(1)
                raise sqlite3.DatabaseError("database disk image is malformed")
            return real()

        cache._connection = flaky
        cache.get(KEY)  # recovers once, while the ledger thread keeps writing
        time.sleep(0.1)
    finally:
        stop.set()
        thread.join(10)
    try:
        assert not errors
        cache.put(KEY, "hello")
        assert cache.get(KEY) == "hello"
        ledger.record(USAGE, 0.5, NOW)
        assert ledger.total_since(NOW - 10) >= 0.5
        assert damaged(tmp_path)
    finally:
        cache.close()
        ledger.close()


def test_store_that_never_opened_recovers_when_the_fresh_file_is_damaged_again(tmp_path):
    path = tmp_path / "gloss.sqlite3"
    path.write_bytes(b"garbage" * 1000)
    first, second = SqliteTranslationCache(path), SqliteSpendLedger(path)
    first.get(KEY)  # recovers; the second store has not opened anything yet
    first.close()
    path.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    path.write_bytes(b"garbage again" * 1000)
    try:
        second.record(USAGE, 0.5, NOW)
        assert second.total_since(NOW - 10) == pytest.approx(0.5)
    finally:
        second.close()
    assert len(damaged(tmp_path)) == 2


def test_reset_time_is_saved_even_if_the_app_closes_straight_away(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_store, "_now", lambda: NOW)
    path = tmp_path / "gloss.sqlite3"
    path.write_bytes(b"garbage" * 1000)
    cache = SqliteTranslationCache(path)
    cache.get(KEY)
    cache.close()  # closed right after recovery
    with SqliteSpendLedger(path) as ledger:
        assert ledger.reset_at() == NOW
