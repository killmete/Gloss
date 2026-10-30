"""Contract tests for TranslationCache and SpendLedger. Uses temporary storage only."""

from __future__ import annotations

import pytest
from testkit.storage import (
    BROKEN,
    CACHE_CHECKS,
    FAKE_GOOD,
    LEDGER_CHECKS,
    BrokenStore,
    fake_case,
    real_case,
    real_params,
)


@pytest.mark.parametrize("entry", [*real_params("cache"), *real_params("ledger")])
def test_implementation_conforms(entry, tmp_path):
    case = real_case(entry, tmp_path)
    try:
        entry[2](case)
    finally:
        case.release()


@pytest.mark.parametrize(
    ("kind", "check"),
    [("cache", c) for c in CACHE_CHECKS] + [("ledger", c) for c in LEDGER_CHECKS],
    ids=lambda x: x if isinstance(x, str) else x.__name__.split("_", 2)[2],
)
def test_conforming_fake_passes(kind, check, tmp_path):
    case = fake_case(kind, FAKE_GOOD[kind], tmp_path)
    try:
        check(case)
    finally:
        case.release()


@pytest.mark.parametrize(
    "item", [b for b in BROKEN if b.kind in ("cache", "ledger")], ids=lambda b: b.id
)
def test_broken_fake_is_caught(item: BrokenStore, tmp_path):
    case = fake_case(item.kind, item.broken.adapter, tmp_path)
    try:
        with pytest.raises(AssertionError):
            item.broken.check(case)
    finally:
        case.release()
