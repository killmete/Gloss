"""SQLite cache and ledger details, the budget guard, secrets, retry, breaker and batching."""

from __future__ import annotations

import queue
import random
import threading
import time
from datetime import UTC, datetime

import pytest
from testkit.discovery import NO_USER_CONFIG
from testkit.storage import SECRET_VALUE, declared_implementations

from gloss.core.config import ModelPrice, load_config
from gloss.core.contracts import (
    AdapterUnavailable,
    CacheKey,
    PermanentError,
    TokenUsage,
    TransientError,
)
from gloss.services.batching import collect_batch
from gloss.services.budget import BudgetGuard, cost_of, start_of_month
from gloss.services.cache import SqliteTranslationCache
from gloss.services.ledger import SqliteSpendLedger
from gloss.services.reliability import CircuitBreaker, RetryPolicy, call_with_retry
from gloss.services.secrets import (
    ApiKeyManager,
    KeyringSecretStore,
    ThrowawayBackend,
    check_key_format,
)

CONFIG = load_config(user_path=NO_USER_CONFIG).config
FIXED_KEY = "k" * 30


# ---- contract discovery ------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["secret", "cache", "ledger"])
def test_each_storage_port_has_a_real_implementation_in_the_contract_suite(kind):
    assert declared_implementations(kind), f"nothing under gloss.services declares {kind!r}"


# ---- cache -------------------------------------------------------------------------------


def key(**changes):
    values = dict(
        provider="p", model="m", target_lang="en", glossary_version="1", normalised_text="你好"
    )
    values.update(changes)
    return CacheKey(**values)


def test_cache_does_not_store_the_source_text(tmp_path):
    cache = SqliteTranslationCache(tmp_path)
    cache.put(key(normalised_text="独特的原文SENTINEL"), "hello")
    cache.close()
    raw = (tmp_path / "gloss.sqlite3").read_bytes()
    wal = tmp_path / "gloss.sqlite3-wal"
    raw += wal.read_bytes() if wal.exists() else b""
    assert "独特的原文SENTINEL".encode() not in raw


def test_cache_prunes_least_recently_used_when_over_the_limit(tmp_path, monkeypatch):
    import gloss.services.cache as module

    monkeypatch.setattr(module, "_PRUNE_EVERY", 10)
    cache = SqliteTranslationCache(tmp_path, max_entries=20)
    for i in range(10):
        cache.put(key(normalised_text=f"old {i}"), "x")
    time.sleep(0.01)
    for i in range(20):
        cache.put(key(normalised_text=f"new {i}"), "y")
    assert cache.get(key(normalised_text="new 19")) == "y"
    assert cache.get(key(normalised_text="old 0")) is None
    cache.close()


def test_cache_works_from_several_threads(tmp_path):
    cache = SqliteTranslationCache(tmp_path)
    errors = []

    def work(n):
        try:
            for i in range(25):
                cache.put(key(normalised_text=f"{n}-{i}"), "v")
                assert cache.get(key(normalised_text=f"{n}-{i}")) == "v"
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    cache.close()
    assert errors == []


def test_cache_and_ledger_share_one_file(tmp_path):
    cache, ledger = SqliteTranslationCache(tmp_path), SqliteSpendLedger(tmp_path)
    cache.put(key(), "hi")
    ledger.record(TokenUsage("p", "m", 1, 1), 0.5, 10.0)
    assert cache.get(key()) == "hi" and ledger.total_since(0) == 0.5
    assert [p.name for p in tmp_path.glob("*.sqlite3")] == ["gloss.sqlite3"]
    cache.close()
    ledger.close()


def test_unusable_database_path_raises_a_gloss_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    cache = SqliteTranslationCache(blocker / "inside" / "x.sqlite3")
    with pytest.raises((PermanentError, OSError)):
        cache.get(key())


def test_use_after_close_reopens_quietly(tmp_path):
    cache = SqliteTranslationCache(tmp_path)
    cache.put(key(), "a")
    cache.close()
    assert cache.get(key()) == "a"
    cache.close()


# ---- budget ------------------------------------------------------------------------------


PRICE = ModelPrice(input=1.0, output=5.0, cache_read=0.1, cache_write=1.25)
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC).timestamp()


def guard(tmp_path, *, monthly=10.0, hourly=0.5, warn=0.8, now=NOW):
    clock = {"t": now}
    ledger = SqliteSpendLedger(tmp_path)
    g = BudgetGuard(
        ledger,
        lambda model: PRICE,
        monthly_usd=monthly,
        hourly_usd=hourly,
        warn_at=warn,
        clock=lambda: clock["t"],
    )
    return g, ledger, clock


def test_cost_formula_uses_all_four_prices_per_million():
    usage = TokenUsage("p", "m", 1_000_000, 1_000_000, 1_000_000, 1_000_000)
    assert cost_of(usage, PRICE) == pytest.approx(1.0 + 5.0 + 0.1 + 1.25)


def test_haiku_hour_estimate_matches_config_prices():
    price = CONFIG.translate.price_for("claude-haiku-5-5")
    usage = TokenUsage("anthropic", "claude-haiku-5-5", 400, 150, 800, 0)
    assert cost_of(usage, price) == pytest.approx((400 * 0.10 + 150 * 0.50 + 800 * 0.01) / 1e6)


def test_unknown_model_is_charged_at_the_highest_configured_price():
    top = CONFIG.translate.price_for("some-future-model")
    assert top == CONFIG.translate.price_for("claude-sonnet-5-5")


def test_status_ok_then_warn_then_hourly_then_monthly(tmp_path):
    g, ledger, _ = guard(tmp_path, monthly=10.0, hourly=0.5)
    assert g.status().level == "ok"
    ledger.record(TokenUsage("p", "m", 1, 1), 0.3, NOW - 7200)
    assert g.status().level == "ok"
    ledger.record(TokenUsage("p", "m", 1, 1), 8.0, NOW - 7200)  # 8.3 of 10 this month
    assert g.status().level == "warn" and g.status().allows_paid_calls
    ledger.record(TokenUsage("p", "m", 1, 1), 0.6, NOW - 60)  # over the hourly limit
    status = g.status()
    assert status.level == "hourly_limit" and not status.allows_paid_calls
    assert status.hour_usd == pytest.approx(0.6)
    ledger.record(TokenUsage("p", "m", 1, 1), 5.0, NOW - 7200)
    assert g.status().level == "monthly_limit"


def test_the_hour_window_slides_and_the_month_resets(tmp_path):
    g, ledger, clock = guard(tmp_path)
    ledger.record(TokenUsage("p", "m", 1, 1), 0.6, NOW)
    assert g.status().level == "hourly_limit"
    clock["t"] = NOW + 3601
    assert g.status().level == "ok"
    ledger.record(TokenUsage("p", "m", 1, 1), 9.9, NOW)
    clock["t"] = datetime(2026, 12, 1, 0, 0, 1, tzinfo=UTC).timestamp()
    assert g.status().month_usd == 0


def test_start_of_month_is_utc_midnight():
    assert start_of_month(NOW) == datetime(2026, 11, 1, tzinfo=UTC).timestamp()


def test_record_writes_the_cost_and_survives_a_restart(tmp_path):
    g, ledger, _ = guard(tmp_path)
    cost = g.record(TokenUsage("p", "m", 1_000_000, 0))
    assert cost == pytest.approx(1.0)
    ledger.close()
    assert SqliteSpendLedger(tmp_path).total_since(0) == pytest.approx(1.0)


# ---- secrets -----------------------------------------------------------------------------


def test_secret_names_map_to_gloss_slash_name_and_api_key():
    backend = ThrowawayBackend()
    KeyringSecretStore(backend).set("anthropic", "abc")
    assert backend.get_password("gloss/anthropic", "api_key") == "abc"


def test_secret_store_never_touches_the_real_keyring_unless_asked(monkeypatch):
    import keyring

    def boom(*a, **k):
        raise AssertionError("the real keyring was used")

    for fn in ("get_password", "set_password", "delete_password"):
        monkeypatch.setattr(keyring, fn, boom)
    store = KeyringSecretStore.for_contract_test(__import__("pathlib").Path("throwaway-x"))
    store.set("n", SECRET_VALUE)
    assert store.get("n") == SECRET_VALUE
    store.delete("n")


def test_real_keyring_path_wraps_backend_errors_without_leaking(caplog):
    class Broken:
        def get_password(self, s, u):
            raise RuntimeError(f"vault locked {SECRET_VALUE}")

        set_password = delete_password = get_password

    store = KeyringSecretStore(Broken())
    for action in (lambda: store.get("n"), lambda: store.set("n", "v"), lambda: store.delete("n")):
        with pytest.raises(PermanentError) as err:
            action()
        assert SECRET_VALUE not in str(err.value) and err.value.__cause__ is None
    assert SECRET_VALUE not in caplog.text


def test_delete_of_a_missing_secret_in_real_keyring_style_is_quiet():
    class PasswordDeleteError(Exception):
        pass

    class Backend(ThrowawayBackend):
        def delete_password(self, s, u):
            raise PasswordDeleteError("not found")

    KeyringSecretStore(Backend()).delete("nothing")


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("", False),
        ("   ", False),
        ("short", False),
        ("has space inside the key value 12345", False),
        ("ключ" + "x" * 30, False),
        (FIXED_KEY, True),
        (f'  "{FIXED_KEY}"  ', True),
    ],
)
def test_key_format_check(text, ok):
    assert (check_key_format(text) is None) is ok


def test_manager_stores_a_cleaned_key_and_reports_presence():
    manager = ApiKeyManager(KeyringSecretStore(ThrowawayBackend()), "anthropic")
    assert not manager.has_api_key()
    manager.set_api_key(f"  {FIXED_KEY}\n")
    assert manager.has_api_key()
    assert manager._store.get("anthropic") == FIXED_KEY
    with pytest.raises(ValueError):
        manager.set_api_key("nope")
    manager.clear_api_key()
    assert not manager.has_api_key()


# ---- retry -------------------------------------------------------------------------------

POLICY = RetryPolicy(retry_max=2, base_delay_ms=250, max_delay_ms=4000)


def test_retry_succeeds_after_transient_failures_and_sleeps_with_jitter():
    sleeps, calls = [], []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise TransientError("blip")
        return "done"

    assert call_with_retry(fn, POLICY, sleep=sleeps.append, rng=random.Random(1)) == "done"
    assert len(calls) == 3 and len(sleeps) == 2
    assert 0.125 <= sleeps[0] <= 0.25 and 0.25 <= sleeps[1] <= 0.5


def test_backoff_grows_is_capped_and_is_not_identical_every_time():
    policy = RetryPolicy(10, 250, 1000)
    rng = random.Random(7)
    delays = [policy.delay_s(n, rng, None) for n in range(1, 8)]
    assert all(d <= 1.0 for d in delays)
    assert delays[3] >= 0.5, "late retries wait near the cap"
    again = [policy.delay_s(1, rng, None) for _ in range(20)]
    assert len(set(again)) > 1


def test_retry_gives_up_after_retry_max_and_raises_the_last_transient_error():
    calls = []

    def fn():
        calls.append(1)
        raise TransientError("still down")

    with pytest.raises(TransientError, match="still down"):
        call_with_retry(fn, POLICY, sleep=lambda s: None)
    assert len(calls) == 3  # first try plus retry_max retries


def test_retry_honours_retry_after_as_a_minimum():
    sleeps = []

    def fn():
        if not sleeps:
            raise TransientError("429", retry_after_s=3.0)
        return 1

    call_with_retry(fn, POLICY, sleep=sleeps.append)
    assert sleeps[0] >= 3.0


def test_retry_after_longer_than_the_cap_stops_retrying_at_once():
    calls = []

    def fn():
        calls.append(1)
        raise TransientError("429", retry_after_s=60.0)

    with pytest.raises(TransientError):
        call_with_retry(fn, POLICY, sleep=lambda s: pytest.fail("must not wait"))
    assert len(calls) == 1


@pytest.mark.parametrize("error", [PermanentError("no"), AdapterUnavailable("gone")])
def test_permanent_and_unavailable_are_never_retried(error):
    calls = []

    def fn():
        calls.append(1)
        raise error

    with pytest.raises(type(error)):
        call_with_retry(fn, POLICY, sleep=lambda s: pytest.fail("must not wait"))
    assert len(calls) == 1


def test_a_stray_exception_is_treated_as_transient_and_retried():
    calls = []

    def fn():
        calls.append(1)
        if len(calls) == 1:
            raise KeyError("oops 你好")
        return "fine"

    assert call_with_retry(fn, POLICY, sleep=lambda s: None) == "fine"
    with pytest.raises(TransientError) as err:
        call_with_retry(
            lambda: (_ for _ in ()).throw(ValueError("你好")), POLICY, sleep=lambda s: None
        )
    assert "你好" not in str(err.value)


# ---- circuit breaker ---------------------------------------------------------------------


def make_breaker(failures=3, cooldown=300.0):
    clock = {"t": 0.0}
    return CircuitBreaker(failures, cooldown, lambda: clock["t"]), clock


def test_breaker_opens_after_n_failures_in_a_row():
    b, _ = make_breaker()
    for _ in range(2):
        assert b.allow()
        b.record_failure()
    assert b.state == "closed"
    b.record_failure()
    assert b.state == "open" and not b.allow()


def test_a_success_resets_the_failure_count():
    b, _ = make_breaker()
    b.record_failure()
    b.record_failure()
    b.record_success()
    b.record_failure()
    b.record_failure()
    assert b.state == "closed"


def test_after_the_cooldown_one_trial_is_allowed_and_success_closes():
    b, clock = make_breaker()
    for _ in range(3):
        b.record_failure()
    clock["t"] = 299
    assert not b.allow() and b.retry_in_s() == pytest.approx(1)
    clock["t"] = 301
    assert b.state == "half_open"
    assert b.allow() and not b.allow(), "only one trial at a time"
    b.record_success()
    assert b.state == "closed" and b.allow()


def test_a_failed_trial_reopens_for_another_full_cooldown():
    b, clock = make_breaker()
    for _ in range(3):
        b.record_failure()
    clock["t"] = 301
    assert b.allow()
    b.record_failure()
    assert b.state == "open"
    clock["t"] = 500
    assert not b.allow()
    clock["t"] = 602
    assert b.allow()


def test_release_trial_allows_another_trial():
    b, clock = make_breaker()
    for _ in range(3):
        b.record_failure()
    clock["t"] = 301
    assert b.allow()
    b.release_trial()
    assert b.allow()


# ---- batching ----------------------------------------------------------------------------


def test_batch_takes_what_is_waiting_up_to_max_lines():
    q: queue.Queue[int] = queue.Queue()
    for i in range(7):
        q.put(i)
    assert collect_batch(q, 5, 0.2) == [0, 1, 2, 3, 4]
    assert collect_batch(q, 5, 0.01) == [5, 6]


def test_batch_waits_up_to_max_wait_for_more_lines():
    q: queue.Queue[int] = queue.Queue()
    q.put(1)
    threading.Timer(0.05, lambda: q.put(2)).start()
    start = time.perf_counter()
    batch = collect_batch(q, 20, 0.3)
    assert batch == [1, 2] and 0.25 < time.perf_counter() - start < 0.8


def test_a_single_line_goes_out_after_the_wait_not_longer():
    q: queue.Queue[int] = queue.Queue()
    q.put(1)
    start = time.perf_counter()
    assert collect_batch(q, 20, 0.05) == [1]
    assert time.perf_counter() - start < 0.5


def test_a_full_batch_returns_at_once_without_waiting():
    q: queue.Queue[int] = queue.Queue()
    for i in range(3):
        q.put(i)
    start = time.perf_counter()
    assert collect_batch(q, 3, 5.0) == [0, 1, 2]
    assert time.perf_counter() - start < 0.5


def test_empty_queue_with_a_timeout_returns_an_empty_batch():
    assert collect_batch(queue.Queue(), 5, 0.05, first_timeout_s=0.05) == []


def test_default_batch_settings_come_from_config():
    assert CONFIG.translate.batch_max_wait_ms == 200 and CONFIG.translate.batch_max_lines == 20
