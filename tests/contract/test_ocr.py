"""Contract tests for OCREngine. See docs/SPEC.md section 3 and ports.py."""

from __future__ import annotations

import pytest
from testkit.core import Broken, apply_subject_name
from testkit.discovery import real_subject, registered_names
from testkit.ocr import (
    BROKEN,
    CHECKS,
    PORT,
    WINDOWS_ONLY,
    WINDOWS_RULE_BROKEN,
    FakeOCR,
    LowQualityEngine,
    check_windows_ocr_is_never_an_automatic_fallback_and_is_low_quality,
    fake_subject,
)

from gloss.core.registry import default_registry

__all__ = ["CHECKS", "PORT", "WINDOWS_ONLY"]


def test_real_adapter_conforms(real_case, real_subjects):
    name, check = real_case
    subject = real_subjects.setdefault((PORT, name), real_subject(PORT, name))
    check(subject)


def test_discovery_left_no_ocr_adapter_refused():
    registered_names(PORT)
    refused = [r for r in default_registry.rejected() if r.port == PORT]
    assert not refused, "OCR adapters were refused: " + "; ".join(
        f"{r.name}: {r.reason}" for r in refused
    )


def test_windows_ocr_engine_follows_adr_0001(real_subjects):
    if "windows" not in registered_names(PORT):
        pytest.skip("no OCR adapter named 'windows' is registered")
    subject = real_subjects.setdefault((PORT, "windows"), real_subject(PORT, "windows"))
    check_windows_ocr_is_never_an_automatic_fallback_and_is_low_quality(subject)


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__.removeprefix("check_"))
def test_conforming_fake_passes(check):
    subject = fake_subject(FakeOCR)
    try:
        check(subject)
    finally:
        subject.release()


def test_conforming_low_quality_fake_passes_the_windows_rule():
    subject = fake_subject(LowQualityEngine)
    try:
        check_windows_ocr_is_never_an_automatic_fallback_and_is_low_quality(subject)
    finally:
        subject.release()


@pytest.mark.parametrize("case", [*BROKEN, WINDOWS_RULE_BROKEN], ids=lambda c: c.id)
def test_broken_fake_is_caught(case: Broken):
    subject = apply_subject_name(fake_subject(case.adapter, via_registry=case.via_registry), case)
    try:
        with pytest.raises(AssertionError):
            case.check(subject)
    finally:
        subject.release()
