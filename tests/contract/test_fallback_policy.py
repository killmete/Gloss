"""An adapter with ALLOW_AUTO_FALLBACK = False can be a primary, never an automatic fallback.

ADR-0001 section 2 and ADR-0003: the Windows OCR engine is opt-in only, so a config edit must
not be able to make it the quiet second choice.
"""

from __future__ import annotations

import logging

import pytest
from testkit.discovery import NO_USER_CONFIG, registered_names
from testkit.ocr import FakeOCR, LowQualityEngine
from testkit.translate import FakeTranslator, InheritsLocalOnly

from gloss.core.config import load_config
from gloss.core.registry import Registry, default_registry


class AccurateEngine(FakeOCR):
    name = "accurate"


def registry_with_both() -> Registry:
    registry = Registry()
    assert registry.register("ocr", "accurate", AccurateEngine)
    assert registry.register("ocr", "windows", LowQualityEngine)
    return registry


def test_a_windows_style_adapter_is_dropped_from_a_fallback_list(caplog):
    registry = registry_with_both()
    with caplog.at_level(logging.WARNING):
        chain = registry.resolve_chain("ocr", "accurate", ["windows"])
    assert chain == ("accurate",)
    assert "cannot be an automatic fallback" in caplog.text


def test_a_windows_style_adapter_stays_usable_as_the_explicit_primary():
    registry = registry_with_both()
    assert registry.resolve_chain("ocr", "windows") == ("windows",)
    assert registry.resolve_chain("ocr", "windows", ["accurate"]) == ("windows", "accurate")


def test_listing_it_twice_or_first_among_fallbacks_changes_nothing():
    registry = registry_with_both()
    assert registry.resolve_chain("ocr", "accurate", ["windows", "windows"]) == ("accurate",)
    assert registry.resolve_chain("ocr", "accurate", ["windows", "accurate"]) == ("accurate",)


def test_the_flag_defaults_to_true_so_ordinary_adapters_can_be_fallbacks():
    registry = registry_with_both()
    assert getattr(AccurateEngine, "ALLOW_AUTO_FALLBACK", True) is True
    assert registry.resolve_chain("ocr", "windows", ["accurate"]) == ("windows", "accurate")


def test_default_config_has_no_automatic_ocr_fallback():
    config = load_config(user_path=NO_USER_CONFIG).config
    assert config.ocr.fallbacks == [], "ADR-0001: visible failure beats convincing wrong text"


def test_no_registered_adapter_with_the_flag_off_survives_in_a_fallback_chain():
    checked = 0
    for port in ("capture", "ocr", "translate", "render"):
        names = registered_names(port)
        adapters = default_registry._adapters[port]
        for name in [n for n in names if not getattr(adapters[n], "ALLOW_AUTO_FALLBACK", True)]:
            checked += 1
            for primary in [n for n in names if n != name]:
                assert name not in default_registry.resolve_chain(
                    port, primary, [name], local_only=False
                )
    if not checked:
        pytest.skip("no registered adapter sets ALLOW_AUTO_FALLBACK = False yet")


# ---- cloud_translation = false: only translators that stay on this PC may run -------------
# Registry.resolve_chain(..., local_only=True) drops every translator whose own class lacks
# LOCAL_ONLY = True, the primary included, because the user asked for nothing to leave the PC.
# A missing LOCAL_ONLY counts as cloud, and so does one that is only inherited.


class CloudTranslator(FakeTranslator):
    name = "cloud"


class UndeclaredTranslator(FakeTranslator):
    name = "undeclared"


class LocalNull(FakeTranslator):
    name = "null"
    LOCAL_ONLY = True


class LocalPack(FakeTranslator):
    name = "quickmt"
    LOCAL_ONLY = True


def translators() -> Registry:
    registry = Registry()
    for cls in (CloudTranslator, LocalNull, LocalPack):
        assert registry.register("translate", cls.name, cls)
    return registry


def chain(registry: Registry, primary: str, fallbacks: list[str], local_only: bool):
    return registry.resolve_chain("translate", primary, fallbacks, local_only=local_only)


def test_with_cloud_off_the_chain_keeps_only_local_translators():
    assert chain(translators(), "cloud", ["quickmt", "null"], local_only=True) == (
        "quickmt",
        "null",
    )


def test_with_cloud_off_a_cloud_primary_is_dropped_too(caplog):
    with caplog.at_level(logging.WARNING):
        result = chain(translators(), "cloud", ["null"], local_only=True)
    assert result == ("null",)
    assert "cloud" in caplog.text


def test_with_cloud_on_nothing_is_filtered():
    assert chain(translators(), "cloud", ["quickmt", "null"], local_only=False) == (
        "cloud",
        "quickmt",
        "null",
    )


def test_a_translator_that_does_not_declare_local_only_counts_as_cloud():
    registry = translators()
    assert registry.register("translate", "undeclared", UndeclaredTranslator)
    assert not hasattr(UndeclaredTranslator, "LOCAL_ONLY")
    assert chain(registry, "null", ["undeclared"], local_only=True) == ("null",)


def test_the_null_translator_stays_in_the_chain_when_cloud_is_off():
    assert chain(translators(), "null", [], local_only=True) == ("null",)


def test_a_chain_with_no_local_translator_left_is_empty_not_cloud():
    registry = Registry()
    assert registry.register("translate", "cloud", CloudTranslator)
    assert chain(registry, "cloud", [], local_only=True) == ()


def test_the_default_config_chain_degrades_to_null_when_cloud_is_off():
    config = load_config(user_path=NO_USER_CONFIG).config.translate
    assert config.fallbacks[-1] == "null", "null must be the last resort"
    registry = translators()
    assert chain(registry, config.provider, list(config.fallbacks), local_only=True) == ("null",)


class CloudBuiltOnLocal(LocalNull):
    """Subclasses a local translator, but never declares LOCAL_ONLY itself."""

    name = "cloudy"


def test_a_cloud_translator_inheriting_local_only_still_counts_as_cloud():
    registry = translators()
    assert registry.register("translate", "cloudy", CloudBuiltOnLocal)
    assert CloudBuiltOnLocal.LOCAL_ONLY is True, "the flag really is inherited here"
    assert "LOCAL_ONLY" not in vars(CloudBuiltOnLocal)
    assert chain(registry, "cloudy", ["null"], local_only=True) == ("null",)
    assert chain(registry, "null", ["cloudy"], local_only=True) == ("null",)


def test_declaring_local_only_on_its_own_class_makes_a_subclass_local():
    class HonestlyLocal(LocalNull):
        name = "honest"
        LOCAL_ONLY = True

    registry = translators()
    assert registry.register("translate", "honest", HonestlyLocal)
    assert chain(registry, "honest", ["null"], local_only=True) == ("honest", "null")


def test_the_inheriting_fake_used_by_the_contract_suite_is_really_inherited():
    assert InheritsLocalOnly.LOCAL_ONLY is True and "LOCAL_ONLY" not in vars(InheritsLocalOnly)
