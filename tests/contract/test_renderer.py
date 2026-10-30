"""Contract tests for Renderer. See docs/SPEC.md section 3 and ports.py.

Registered renderers need Qt. Qt starts in offscreen mode when no display is available.
"""

from __future__ import annotations

import pytest
from testkit.core import Broken, apply_subject_name
from testkit.discovery import real_subject, registered_names
from testkit.render import BROKEN, CHECKS, PORT, WINDOWS_ONLY, FakeRenderer, fake_subject

from gloss.core.registry import default_registry

__all__ = ["CHECKS", "PORT", "WINDOWS_ONLY"]


def test_real_adapter_conforms(real_case, real_subjects):
    name, check = real_case
    subject = real_subjects.setdefault((PORT, name), real_subject(PORT, name))
    check(subject)


def test_discovery_left_no_render_adapter_refused():
    registered_names(PORT)
    refused = [r for r in default_registry.rejected() if r.port == PORT]
    assert not refused, "render adapters were refused: " + "; ".join(
        f"{r.name}: {r.reason}" for r in refused
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__.removeprefix("check_"))
def test_conforming_fake_passes(check):
    subject = fake_subject(FakeRenderer)
    try:
        check(subject)
    finally:
        subject.release()


@pytest.mark.parametrize("case", BROKEN, ids=lambda c: c.id)
def test_broken_fake_is_caught(case: Broken):
    subject = apply_subject_name(fake_subject(case.adapter, via_registry=case.via_registry), case)
    try:
        with pytest.raises(AssertionError):
            case.check(subject)
    finally:
        subject.release()
