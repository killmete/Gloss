"""Contract checks for the secret, cache and ledger ports, plus fakes and broken fakes.

These ports are not in the adapter registry (``PORTS`` has four entries), so implementations
are found differently: a class under ``gloss.services`` opts in by setting
``CONTRACT_PORT = "secret" | "cache" | "ledger"`` and offering a classmethod
``for_contract_test(path)``. Calling it twice with the same ``path`` must give two objects that
share one backing store, which lets the checks prove data survives a restart.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import logging
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from gloss.core.contracts import CacheKey, TokenUsage
from testkit.core import Broken, call

KINDS = ("secret", "cache", "ledger")
SECRET_VALUE = "sk-test-0123456789-DO-NOT-LEAK"


@dataclass
class Case:
    """A way to build objects over one backing store. ``new()`` reopens the same store."""

    kind: str
    name: str
    make: Callable[[Path], Any]
    path: Path
    _made: list[Any] = field(default_factory=list)

    def new(self) -> Any:
        store = self.make(self.path)
        self._made.append(store)
        return store

    def release(self) -> None:
        for store in self._made:
            close = getattr(store, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # Teardown must not hide the real failure.
                    pass
        self._made.clear()


def declared_implementations(kind: str) -> list[tuple[str, Any]]:
    """(name, factory) for every class under gloss.services that opted in for ``kind``."""
    import gloss.services as services

    found: list[tuple[str, Any]] = []
    for info in pkgutil.walk_packages(services.__path__, "gloss.services."):
        try:
            module = importlib.import_module(info.name)
        except Exception:  # A broken service module is reported by its own tests.
            continue
        for obj in vars(module).values():
            if (
                isinstance(obj, type)
                and obj.__module__ == module.__name__
                and getattr(obj, "CONTRACT_PORT", None) == kind
            ):
                found.append((f"{module.__name__.rsplit('.', 1)[-1]}.{obj.__name__}", obj))
    return found


# ---- secret store ------------------------------------------------------------------------


def check_secret_missing_name_is_none(case: Case) -> None:
    store = case.new()
    assert call(store.get, "contract-test-missing") is None, "an unknown name returns None"


def check_secret_round_trips_text_exactly(case: Case) -> None:
    store = case.new()
    for value in (SECRET_VALUE, "密钥-ключ-🔑", " padded ", "x" * 500):
        call(store.set, "contract-test-a", value)
        assert call(store.get, "contract-test-a") == value, f"{value[:12]!r} did not round-trip"
    call(store.delete, "contract-test-a")


def check_secret_set_replaces_the_old_value(case: Case) -> None:
    store = case.new()
    call(store.set, "contract-test-a", "one")
    call(store.set, "contract-test-a", "two")
    assert call(store.get, "contract-test-a") == "two"
    call(store.delete, "contract-test-a")


def check_secret_delete_removes_it_and_missing_delete_is_harmless(case: Case) -> None:
    store = case.new()
    call(store.set, "contract-test-a", "one")
    call(store.delete, "contract-test-a")
    assert call(store.get, "contract-test-a") is None, "a deleted secret must be gone"
    call(store.delete, "contract-test-a")  # already gone: no error
    call(store.delete, "contract-test-never-existed")


def check_secret_names_do_not_collide(case: Case) -> None:
    store = case.new()
    one, two = "contract-test-a-long-shared-prefix-1", "contract-test-a-long-shared-prefix-2"
    call(store.set, one, "A")
    call(store.set, two, "B")
    assert call(store.get, one) == "A"
    assert call(store.get, two) == "B"
    call(store.delete, one)
    assert call(store.get, two) == "B", "deleting one name removed another"
    call(store.delete, two)


def check_secret_survives_a_restart(case: Case) -> None:
    call(case.new().set, "contract-test-a", SECRET_VALUE)
    assert call(case.new().get, "contract-test-a") == SECRET_VALUE, (
        "a secret must still be there for the next run of the app"
    )
    call(case.new().delete, "contract-test-a")


def check_secret_value_never_reaches_output_logs_or_repr(case: Case) -> None:
    records: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    handler = Collect(logging.DEBUG)
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            store = case.new()
            call(store.set, "contract-test-a", SECRET_VALUE)
            call(store.get, "contract-test-a")
            seen = [repr(store), str(store)]  # while the secret is stored
            call(store.delete, "contract-test-a")
            call(store.get, "contract-test-a")
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    everything = "\n".join([out.getvalue(), err.getvalue(), *records, *seen])
    assert SECRET_VALUE not in everything, "a secret value appeared in output, a log or repr()"


SECRET_CHECKS = (
    check_secret_missing_name_is_none,
    check_secret_round_trips_text_exactly,
    check_secret_set_replaces_the_old_value,
    check_secret_delete_removes_it_and_missing_delete_is_harmless,
    check_secret_names_do_not_collide,
    check_secret_survives_a_restart,
    check_secret_value_never_reaches_output_logs_or_repr,
)

# ---- translation cache -------------------------------------------------------------------


def key(**changes: Any) -> CacheKey:
    values: dict[str, Any] = {
        "provider": "p",
        "model": "m1",
        "target_lang": "en",
        "glossary_version": "3",
        "normalised_text": "Hello 你好 世界",
    }
    values.update(changes)
    return CacheKey(**values)


def check_cache_miss_is_none(case: Case) -> None:
    assert call(case.new().get, key()) is None


def check_cache_round_trips_text_exactly(case: Case) -> None:
    cache = case.new()
    for text in ("Hello world", "café 🎮 – “quotes”", "line one\nline two", "x" * 5000):
        call(cache.put, key(), text)
        assert call(cache.get, key()) == text, f"{text[:12]!r} did not round-trip"


def check_cache_put_replaces_the_old_text(case: Case) -> None:
    cache = case.new()
    call(cache.put, key(), "old")
    call(cache.put, key(), "new")
    assert call(cache.get, key()) == "new"


def check_cache_every_key_field_matters(case: Case) -> None:
    cache = case.new()
    call(cache.put, key(), "base")
    for field_name, other in (
        ("provider", "q"),
        ("model", "m2"),
        ("model", None),
        ("target_lang", "th"),
        ("glossary_version", "4"),
        ("normalised_text", "Hello 你好 世界!"),
        ("normalised_text", "hello 你好 世界"),
    ):
        assert call(cache.get, key(**{field_name: other})) is None, (
            f"a different {field_name} ({other!r}) must be a cache miss"
        )
    call(cache.put, key(model=None), "no model")
    assert call(cache.get, key(model=None)) == "no model"
    assert call(cache.get, key(model="None")) is None, "model None is not the text 'None'"
    assert call(cache.get, key()) == "base"


def check_cache_survives_a_restart(case: Case) -> None:
    first = case.new()
    call(first.put, key(), "kept")
    call(first.close)
    assert call(case.new().get, key()) == "kept", "the cache must still be there next run"


def check_cache_close_is_safe_twice(case: Case) -> None:
    cache = case.new()
    call(cache.close)
    call(cache.close)


CACHE_CHECKS = (
    check_cache_miss_is_none,
    check_cache_round_trips_text_exactly,
    check_cache_put_replaces_the_old_text,
    check_cache_every_key_field_matters,
    check_cache_survives_a_restart,
    check_cache_close_is_safe_twice,
)

# ---- spend ledger ------------------------------------------------------------------------


def usage(model: str = "m1") -> TokenUsage:
    return TokenUsage("p", model, 100, 20, 0, 5)


def check_ledger_starts_at_zero(case: Case) -> None:
    total = call(case.new().total_since, 0.0)
    assert isinstance(total, float) and total == 0.0


def check_ledger_adds_up_entries_since_a_time(case: Case) -> None:
    ledger = case.new()
    call(ledger.record, usage(), 0.25, 1000.0)
    call(ledger.record, usage("m2"), 0.5, 2000.0)
    call(ledger.record, usage(), 0.125, 3000.0)
    assert abs(call(ledger.total_since, 0.0) - 0.875) < 1e-9
    assert abs(call(ledger.total_since, 1500.0) - 0.625) < 1e-9
    assert abs(call(ledger.total_since, 3000.5)) < 1e-9, "nothing is recorded after 3000.5"


def check_ledger_boundary_is_inclusive(case: Case) -> None:
    ledger = case.new()
    call(ledger.record, usage(), 0.5, 2000.0)
    assert abs(call(ledger.total_since, 2000.0) - 0.5) < 1e-9, (
        "total_since counts entries at or after the timestamp"
    )
    assert abs(call(ledger.total_since, 2000.0001)) < 1e-9


def check_ledger_keeps_small_costs_exactly(case: Case) -> None:
    ledger = case.new()
    for i in range(100):
        call(ledger.record, usage(), 0.0001, 1000.0 + i)
    assert abs(call(ledger.total_since, 0.0) - 0.01) < 1e-9, "tiny costs must not be rounded away"


def check_ledger_survives_a_restart(case: Case) -> None:
    first = case.new()
    call(first.record, usage(), 0.75, 5000.0)
    call(first.close)
    assert abs(call(case.new().total_since, 0.0) - 0.75) < 1e-9, (
        "the budget guard must survive a restart"
    )


def check_ledger_close_is_safe_twice(case: Case) -> None:
    ledger = case.new()
    call(ledger.close)
    call(ledger.close)


LEDGER_CHECKS = (
    check_ledger_starts_at_zero,
    check_ledger_adds_up_entries_since_a_time,
    check_ledger_boundary_is_inclusive,
    check_ledger_keeps_small_costs_exactly,
    check_ledger_survives_a_restart,
    check_ledger_close_is_safe_twice,
)

CHECKS_BY_KIND = {"secret": SECRET_CHECKS, "cache": CACHE_CHECKS, "ledger": LEDGER_CHECKS}

# ---- fakes -------------------------------------------------------------------------------

_BACKING: dict[tuple[str, str], Any] = {}


def _shared(kind: str, path: Path, factory: Callable[[], Any]) -> Any:
    return _BACKING.setdefault((kind, str(path)), factory())


class FakeSecrets:
    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    @classmethod
    def for_contract_test(cls, path: Path) -> FakeSecrets:
        return cls(_shared("secret", path, dict))

    def get(self, name: str) -> str | None:
        return self._data.get(name)

    def set(self, name: str, value: str) -> None:
        self._data[name] = value

    def delete(self, name: str) -> None:
        self._data.pop(name, None)

    def __repr__(self) -> str:
        return f"FakeSecrets({len(self._data)} secrets)"


class LeakyReprSecrets(FakeSecrets):
    def __repr__(self) -> str:
        return f"LeakyReprSecrets({self._data})"


class LoggingSecrets(FakeSecrets):
    def set(self, name: str, value: str) -> None:
        logging.getLogger("fake.secrets").debug("storing %s=%s", name, value)
        super().set(name, value)


class PrintingSecrets(FakeSecrets):
    def get(self, name: str) -> str | None:
        value = super().get(name)
        print("read secret", name, value)
        return value


class DeleteMissingRaises(FakeSecrets):
    def delete(self, name: str) -> None:
        del self._data[name]


class EmptyStringForMissing(FakeSecrets):
    def get(self, name: str) -> str | None:
        return self._data.get(name, "")


class PrefixCollisionSecrets(FakeSecrets):
    def get(self, name: str) -> str | None:
        return self._data.get(name[:20])

    def set(self, name: str, value: str) -> None:
        self._data[name[:20]] = value

    def delete(self, name: str) -> None:
        self._data.pop(name[:20], None)


class KeepsFirstValueSecrets(FakeSecrets):
    def set(self, name: str, value: str) -> None:
        self._data.setdefault(name, value)


class ForgetfulSecrets(FakeSecrets):
    @classmethod
    def for_contract_test(cls, path: Path) -> ForgetfulSecrets:
        return cls({})  # a fresh dict each time: nothing survives a restart


class StripsSecrets(FakeSecrets):
    def set(self, name: str, value: str) -> None:
        self._data[name] = value.strip()


class RaisesKeyErrorSecrets(FakeSecrets):
    def get(self, name: str) -> str | None:
        return self._data[name]


class FakeCache:
    CONTRACT_PORT = "cache"

    def __init__(self, data: dict[CacheKey, str]) -> None:
        self._data = data

    @classmethod
    def for_contract_test(cls, path: Path) -> FakeCache:
        return cls(_shared("cache", path, dict))

    def _k(self, k: CacheKey) -> Any:
        return k

    def get(self, key: CacheKey) -> str | None:
        return self._data.get(self._k(key))

    def put(self, key: CacheKey, text: str) -> None:
        self._data[self._k(key)] = text

    def close(self) -> None:
        pass


class IgnoresModelCache(FakeCache):
    def _k(self, k):
        return (k.provider, k.target_lang, k.glossary_version, k.normalised_text)


class IgnoresGlossaryVersionCache(FakeCache):
    def _k(self, k):
        return (k.provider, k.model, k.target_lang, k.normalised_text)


class IgnoresTargetLangCache(FakeCache):
    def _k(self, k):
        return (k.provider, k.model, k.glossary_version, k.normalised_text)


class StringifiesModelCache(FakeCache):
    def _k(self, k):
        return (k.provider, str(k.model), k.target_lang, k.glossary_version, k.normalised_text)


class CaseFoldsTextCache(FakeCache):
    def _k(self, k):
        return (
            k.provider,
            k.model,
            k.target_lang,
            k.glossary_version,
            k.normalised_text.lower(),
            1,
        )


class TruncatingCache(FakeCache):
    def put(self, key, text):
        super().put(key, text[:1000])


class KeepsOldTextCache(FakeCache):
    def put(self, key, text):
        self._data.setdefault(self._k(key), text)


class ForgetfulCache(FakeCache):
    @classmethod
    def for_contract_test(cls, path):
        return cls({})


class CloseTwiceCache(FakeCache):
    _closed = False

    def close(self):
        if self._closed:
            raise ValueError("closed")
        self._closed = True


class MissReturnsEmptyCache(FakeCache):
    def get(self, key):
        return super().get(key) or ""


class FakeLedger:
    CONTRACT_PORT = "ledger"

    def __init__(self, rows: list[tuple[float, float]]) -> None:
        self._rows = rows

    @classmethod
    def for_contract_test(cls, path: Path) -> FakeLedger:
        return cls(_shared("ledger", path, list))

    def record(self, usage: TokenUsage, cost_usd: float, timestamp: float) -> None:
        self._rows.append((timestamp, cost_usd))

    def total_since(self, timestamp: float) -> float:
        return float(sum(c for t, c in self._rows if t >= timestamp))

    def close(self) -> None:
        pass


class ExclusiveBoundaryLedger(FakeLedger):
    def total_since(self, timestamp):
        return float(sum(c for t, c in self._rows if t > timestamp))


class IgnoresTimestampLedger(FakeLedger):
    def total_since(self, timestamp):
        return float(sum(c for _, c in self._rows))


class RoundsToCentsLedger(FakeLedger):
    def record(self, usage, cost_usd, timestamp):
        self._rows.append((timestamp, round(cost_usd, 2)))


class ForgetfulLedger(FakeLedger):
    @classmethod
    def for_contract_test(cls, path):
        return cls([])


class ReturnsIntLedger(FakeLedger):
    def total_since(self, timestamp):
        return int(super().total_since(timestamp)) if self._rows else 0


class CloseTwiceLedger(FakeLedger):
    _closed = False

    def close(self):
        if self._closed:
            raise ValueError("closed")
        self._closed = True


class OnlyLastEntryLedger(FakeLedger):
    def record(self, usage, cost_usd, timestamp):
        self._rows[:] = [(timestamp, cost_usd)]


class NonZeroStartLedger(FakeLedger):
    def total_since(self, timestamp):
        return super().total_since(timestamp) + 0.01


class IntZeroLedger(FakeLedger):
    def total_since(self, timestamp):
        total = super().total_since(timestamp)
        return total if total else 0


class RawErrorLedger(FakeLedger):
    def record(self, usage, cost_usd, timestamp):
        raise OSError("disk full")


FAKE_GOOD = {"secret": FakeSecrets, "cache": FakeCache, "ledger": FakeLedger}


def fake_case(kind: str, cls: type, path: Path) -> Case:
    return Case(kind, cls.__name__, cls.for_contract_test, path)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class BrokenStore:
    kind: str
    broken: Broken

    @property
    def id(self) -> str:
        return self.broken.id


def _b(kind: str, cls: type, check: Callable[[Case], None], why: str) -> BrokenStore:
    return BrokenStore(kind, Broken(cls, check, why))  # type: ignore[arg-type]


BROKEN = (
    _b(
        "secret",
        LeakyReprSecrets,
        check_secret_value_never_reaches_output_logs_or_repr,
        "repr shows secrets",
    ),
    _b(
        "secret",
        LoggingSecrets,
        check_secret_value_never_reaches_output_logs_or_repr,
        "logs the value",
    ),
    _b(
        "secret",
        PrintingSecrets,
        check_secret_value_never_reaches_output_logs_or_repr,
        "prints the value",
    ),
    _b(
        "secret",
        DeleteMissingRaises,
        check_secret_delete_removes_it_and_missing_delete_is_harmless,
        "KeyError on missing delete",
    ),
    _b("secret", EmptyStringForMissing, check_secret_missing_name_is_none, "'' instead of None"),
    _b(
        "secret",
        PrefixCollisionSecrets,
        check_secret_names_do_not_collide,
        "names collide on a prefix",
    ),
    _b(
        "secret",
        KeepsFirstValueSecrets,
        check_secret_set_replaces_the_old_value,
        "set() does not replace",
    ),
    _b("secret", ForgetfulSecrets, check_secret_survives_a_restart, "memory only"),
    _b("secret", StripsSecrets, check_secret_round_trips_text_exactly, "strips whitespace"),
    _b(
        "secret",
        RaisesKeyErrorSecrets,
        check_secret_missing_name_is_none,
        "KeyError for a missing name",
    ),
    _b("cache", IgnoresModelCache, check_cache_every_key_field_matters, "model not in key"),
    _b(
        "cache",
        IgnoresGlossaryVersionCache,
        check_cache_every_key_field_matters,
        "glossary_version not in key",
    ),
    _b(
        "cache",
        IgnoresTargetLangCache,
        check_cache_every_key_field_matters,
        "target_lang not in key",
    ),
    _b(
        "cache",
        StringifiesModelCache,
        check_cache_every_key_field_matters,
        "None model equals 'None'",
    ),
    _b(
        "cache",
        CaseFoldsTextCache,
        check_cache_every_key_field_matters,
        "text compared case-insensitively",
    ),
    _b("cache", TruncatingCache, check_cache_round_trips_text_exactly, "long text truncated"),
    _b("cache", KeepsOldTextCache, check_cache_put_replaces_the_old_text, "put() does not replace"),
    _b("cache", ForgetfulCache, check_cache_survives_a_restart, "memory only"),
    _b("cache", CloseTwiceCache, check_cache_close_is_safe_twice, "second close raises"),
    _b("cache", MissReturnsEmptyCache, check_cache_miss_is_none, "'' instead of None on a miss"),
    _b(
        "ledger",
        ExclusiveBoundaryLedger,
        check_ledger_boundary_is_inclusive,
        "boundary is exclusive",
    ),
    _b(
        "ledger",
        IgnoresTimestampLedger,
        check_ledger_adds_up_entries_since_a_time,
        "ignores the timestamp",
    ),
    _b("ledger", RoundsToCentsLedger, check_ledger_keeps_small_costs_exactly, "rounds to cents"),
    _b("ledger", ForgetfulLedger, check_ledger_survives_a_restart, "memory only"),
    _b("ledger", ReturnsIntLedger, check_ledger_adds_up_entries_since_a_time, "returns int"),
    _b("ledger", CloseTwiceLedger, check_ledger_close_is_safe_twice, "second close raises"),
    _b(
        "ledger",
        OnlyLastEntryLedger,
        check_ledger_adds_up_entries_since_a_time,
        "keeps only the last entry",
    ),
    _b(
        "ledger",
        RawErrorLedger,
        check_ledger_adds_up_entries_since_a_time,
        "raw OSError from record()",
    ),
)


def real_params(kind: str) -> list[Any]:
    """pytest params (name, factory, check) for every implementation, or one clear skip."""
    found = declared_implementations(kind)
    if not found:
        reason = (
            f"no class under gloss.services sets CONTRACT_PORT = {kind!r} yet. Services opt in "
            "with that attribute and a for_contract_test(path) classmethod. The fakes still run."
        )
        return [pytest.param(None, id="none-declared", marks=pytest.mark.skip(reason=reason))]
    return [
        pytest.param((name, cls, check), id=f"{name}-{check.__name__.split('_', 2)[2]}")
        for name, cls in found
        for check in CHECKS_BY_KIND[kind]
    ]


def real_case(entry: Any, path: Path) -> Case:
    name, cls, _ = entry
    factory = getattr(cls, "for_contract_test", None)
    assert factory is not None, (
        f"{name} sets CONTRACT_PORT but has no for_contract_test(path) classmethod, "
        "so the contract suite cannot build it"
    )
    return Case(cls.CONTRACT_PORT, name, factory, path)
