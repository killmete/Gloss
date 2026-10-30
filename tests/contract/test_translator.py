"""Contract tests for Translator. See docs/SPEC.md section 3 and ports.py.

Real adapters are opened with an empty secret store and with the network blocked, so a
provider that needs a key reports AdapterUnavailable and its checks skip. Only offline
providers (null, quickmt) run translate() here.
"""

from __future__ import annotations

import pytest
from testkit.core import Broken, apply_subject_name, call, expect_error
from testkit.discovery import NO_USER_CONFIG, real_subject, registered_names
from testkit.translate import (
    BROKEN,
    CHECKS,
    PORT,
    SCRIPTED_BROKEN,
    SCRIPTED_CHECKS,
    WINDOWS_ONLY,
    FakeTranslator,
    LocalFakeTranslator,
    NoSecrets,
    assert_valid_response,
    fake_subject,
    make_request,
)

from gloss.core.config import load_config
from gloss.core.contracts import AdapterContext, AdapterUnavailable, TransientError
from gloss.core.registry import default_registry

__all__ = ["CHECKS", "PORT", "WINDOWS_ONLY"]


def test_real_adapter_conforms(real_case, real_subjects):
    name, check = real_case
    subject = real_subjects.setdefault((PORT, name), real_subject(PORT, name, NoSecrets()))
    check(subject)


def test_discovery_left_no_translate_adapter_refused():
    registered_names(PORT)
    refused = [r for r in default_registry.rejected() if r.port == PORT]
    assert not refused, "translate adapters were refused: " + "; ".join(
        f"{r.name}: {r.reason}" for r in refused
    )


def test_null_translator_passes_text_through(real_subjects):
    if "null" not in registered_names(PORT):
        pytest.skip("no translator named 'null' is registered")
    subject = real_subjects.setdefault((PORT, "null"), real_subject(PORT, "null", NoSecrets()))
    adapter = subject.opened()
    request = make_request()
    response = call(adapter.translate, request)
    assert_valid_response(request, response, "null")
    assert all(r.status == "passthrough" for r in response.results), (
        "null shows the original text, so every line is status='passthrough'"
    )
    assert response.usage is None, "null costs nothing, so it reports no usage"


@pytest.mark.parametrize("name", ["anthropic"])
def test_a_provider_that_needs_a_key_is_unavailable_without_one(name):
    if name not in registered_names(PORT):
        pytest.skip(f"no translator named '{name}' is registered")
    context = AdapterContext(
        settings=load_config(user_path=NO_USER_CONFIG).config.adapter_settings(PORT, name),
        secrets=NoSecrets(),
    )
    adapter = default_registry.create(PORT, name, context)
    try:
        expect_error(adapter.open, AdapterUnavailable)
    finally:
        adapter.close()


def test_the_null_translator_declares_itself_local(real_subjects):
    if "null" not in registered_names(PORT):
        pytest.skip("no translator named 'null' is registered")
    assert vars(default_registry._adapters[PORT]["null"]).get("LOCAL_ONLY") is True


@pytest.mark.parametrize("name", ["anthropic"])
def test_a_cloud_provider_never_declares_itself_local(name):
    if name not in registered_names(PORT):
        pytest.skip(f"no translator named '{name}' is registered")
    assert vars(default_registry._adapters[PORT][name]).get("LOCAL_ONLY", False) is False


def test_a_local_fake_passes_every_check_too():
    subject = fake_subject(LocalFakeTranslator)
    try:
        for check in CHECKS:
            check(subject)
    finally:
        subject.release()


def test_transient_error_carries_the_servers_wait_time():
    error = TransientError("rate limited", retry_after_s=2.5)
    assert error.retry_after_s == 2.5 and TransientError("x").retry_after_s is None


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__.removeprefix("check_"))
def test_conforming_fake_passes(check):
    subject = fake_subject(FakeTranslator)
    try:
        check(subject)
    finally:
        subject.release()


@pytest.mark.parametrize(
    ("check", "settings"), SCRIPTED_CHECKS, ids=lambda x: getattr(x, "__name__", "")[6:] or "s"
)
def test_conforming_fake_passes_scripted_failures(check, settings):
    subject = fake_subject(FakeTranslator, settings=settings)
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


@pytest.mark.parametrize(("case", "settings"), SCRIPTED_BROKEN, ids=lambda x: getattr(x, "id", "s"))
def test_broken_fake_is_caught_when_a_line_fails(case: Broken, settings):
    subject = fake_subject(case.adapter, settings=settings)
    try:
        with pytest.raises(AssertionError):
            case.check(subject)
    finally:
        subject.release()
