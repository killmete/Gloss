"""Contract tests for SecretStore. Never touches the real Windows Credential Manager."""

from __future__ import annotations

import pytest
from testkit.storage import (
    BROKEN,
    FAKE_GOOD,
    SECRET_CHECKS,
    BrokenStore,
    fake_case,
    real_case,
    real_params,
)

KIND = "secret"


@pytest.mark.parametrize("entry", real_params(KIND))
def test_implementation_conforms(entry, tmp_path):
    case = real_case(entry, tmp_path)
    try:
        entry[2](case)
    finally:
        case.release()


@pytest.mark.parametrize("check", SECRET_CHECKS, ids=lambda c: c.__name__.removeprefix("check_"))
def test_conforming_fake_passes(check, tmp_path):
    case = fake_case(KIND, FAKE_GOOD[KIND], tmp_path)
    try:
        check(case)
    finally:
        case.release()


@pytest.mark.parametrize("item", [b for b in BROKEN if b.kind == KIND], ids=lambda b: b.id)
def test_broken_fake_is_caught(item: BrokenStore, tmp_path):
    case = fake_case(KIND, item.broken.adapter, tmp_path)
    try:
        with pytest.raises(AssertionError):
            item.broken.check(case)
    finally:
        case.release()
