"""core/registry.py: refusals with reasons, name lookup, chains, and cheap discovery."""

from __future__ import annotations

import logging
import subprocess
import sys
import textwrap

import pytest

from gloss.core import registry as registry_module
from gloss.core.contracts import (
    CONTRACT_VERSION,
    AdapterContext,
    AdapterUnavailable,
    Health,
    PermanentError,
    TransientError,
)
from gloss.core.registry import (
    Registry,
    UnknownAdapter,
    contract_compatible,
    default_registry,
    discover,
)


def make_adapter(name="thing", requires=">=1.0,<2.0", **attrs):
    """A minimal translator-shaped adapter class."""

    def init(self, context):
        self.context = context

    body = {
        "name": name,
        "__init__": init,
        "open": lambda self: None,
        "translate": lambda self, request: None,
        "close": lambda self: None,
        "health": lambda self: Health("ok"),
        **attrs,
    }
    if requires is not None:
        body["REQUIRES_CONTRACT"] = requires
    return type("Thing", (), body)


@pytest.fixture
def registry():
    return Registry()


# ---- contract_compatible -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("requirement", "version", "ok"),
    [
        (">=1.0,<2.0", "1.0.0", True),
        (">=1.0,<2.0", "1.7.3", True),
        (">=1.0,<2.0", "2.0.0", False),
        (">=1.1,<2.0", "1.0.0", False),
        (">=2.0", "1.9.9", False),
        ("not a specifier", "1.0.0", False),
        ("", "1.0.0", True),
    ],
)
def test_contract_compatible(requirement, version, ok):
    assert contract_compatible(requirement, version) is ok


def test_the_default_check_uses_this_builds_contract_version():
    assert contract_compatible(f"=={CONTRACT_VERSION}")
    assert not contract_compatible(">=99.0")


# ---- registering and refusing ------------------------------------------------------------


def test_a_valid_adapter_registers_and_is_found_by_name(registry):
    adapter = make_adapter()
    assert registry.register("translate", "thing", adapter) is True
    assert registry.names("translate") == ("thing",)
    assert registry.rejected() == ()
    assert isinstance(registry.create("translate", "thing"), adapter)


def test_names_are_sorted_and_kept_per_port(registry):
    for name in ("zeta", "alpha", "mid"):
        registry.register("translate", name, make_adapter(name))
    assert registry.names("translate") == ("alpha", "mid", "zeta")
    assert registry.names("ocr") == ()
    assert registry.names("no-such-port") == ()


def test_an_incompatible_adapter_is_refused_with_the_reason_kept(registry, caplog):
    adapter = make_adapter(requires=">=2.0,<3.0")
    with caplog.at_level(logging.WARNING):
        assert registry.register("translate", "thing", adapter) is False
    assert registry.names("translate") == ()
    (rejection,) = registry.rejected()
    assert (rejection.port, rejection.name) == ("translate", "thing")
    assert ">=2.0,<3.0" in rejection.reason and CONTRACT_VERSION in rejection.reason
    assert "refused" in caplog.text and rejection.reason in caplog.text


def test_a_registry_for_another_contract_version_judges_by_that_version():
    new = Registry(contract_version="2.1.0")
    assert not new.register("translate", "thing", make_adapter(requires=">=1.0,<2.0"))
    assert new.register("translate", "newer", make_adapter("newer", requires=">=2.0,<3.0"))


def test_create_explains_why_a_refused_adapter_is_missing(registry):
    registry.register("translate", "thing", make_adapter(requires=">=2.0"))
    with pytest.raises(UnknownAdapter) as caught:
        registry.create("translate", "thing")
    assert "requires contract >=2.0" in str(caught.value)
    assert isinstance(caught.value, PermanentError)


@pytest.mark.parametrize(
    ("kwargs", "words"),
    [
        ({"requires": None}, "does not declare REQUIRES_CONTRACT"),
        ({"requires": 1.0}, "does not declare REQUIRES_CONTRACT"),
        ({"requires": "garbage"}, "requires contract garbage"),
    ],
)
def test_other_refusal_reasons(registry, kwargs, words):
    registry.register("translate", "thing", make_adapter(**kwargs))
    assert words in registry.rejected()[0].reason


def test_a_name_that_differs_from_the_registered_name_is_refused(registry):
    registry.register("translate", "wanted", make_adapter("actual"))
    assert "'name' attribute must be 'wanted'" in registry.rejected()[0].reason


def test_a_missing_method_is_named_in_the_reason(registry):
    adapter = make_adapter(translate=None, health=None)
    registry.register("translate", "thing", adapter)
    reason = registry.rejected()[0].reason
    assert "missing" in reason and "translate" in reason and "health" in reason


def test_each_port_checks_its_own_methods(registry):
    translator = make_adapter()
    assert not registry.register("ocr", "thing", translator)
    assert "recognize" in registry.rejected()[0].reason


def test_an_unknown_port_is_refused(registry):
    assert not registry.register("audio", "thing", make_adapter())
    assert "unknown port 'audio'" in registry.rejected()[0].reason


def test_a_second_class_cannot_take_a_used_name(registry):
    first, second = make_adapter(), make_adapter()
    assert registry.register("translate", "thing", first)
    assert not registry.register("translate", "thing", second)
    assert "already used" in registry.rejected()[0].reason
    assert type(registry.create("translate", "thing")) is first


def test_registering_the_same_class_twice_is_harmless(registry):
    adapter = make_adapter()
    assert registry.register("translate", "thing", adapter)
    assert registry.register("translate", "thing", adapter)
    assert registry.rejected() == ()


def test_the_same_name_can_exist_on_different_ports(registry):
    registry.register("translate", "null", make_adapter("null"))
    ocr = type(
        "O",
        (),
        {
            "name": "null",
            "REQUIRES_CONTRACT": ">=1.0,<2.0",
            "languages": (),
            "open": lambda s: None,
            "recognize": lambda s, f, r: [],
            "close": lambda s: None,
            "health": lambda s: Health("ok"),
        },
    )
    assert registry.register("ocr", "null", ocr)


# ---- create ------------------------------------------------------------------------------


def test_create_passes_the_context_through(registry):
    registry.register("translate", "thing", make_adapter())
    context = AdapterContext(settings={"a": 1})
    assert registry.create("translate", "thing", context).context is context


def test_create_supplies_an_empty_context_when_none_is_given(registry):
    registry.register("translate", "thing", make_adapter())
    context = registry.create("translate", "thing").context
    assert isinstance(context, AdapterContext) and dict(context.settings) == {}


def test_create_for_a_name_never_registered_is_an_unknown_adapter(registry):
    with pytest.raises(UnknownAdapter, match="No usable translate adapter named 'ghost'"):
        registry.create("translate", "ghost")


def test_a_constructor_crash_becomes_adapter_unavailable_with_the_cause_kept(registry):
    def init(self, context):
        raise ImportError("DLL load failed")

    registry.register("translate", "thing", make_adapter(__init__=init))
    with pytest.raises(AdapterUnavailable) as caught:
        registry.create("translate", "thing")
    assert isinstance(caught.value.__cause__, ImportError)
    assert "failed to start" in str(caught.value)


@pytest.mark.parametrize(
    "error", [AdapterUnavailable("no key"), PermanentError("bad"), TransientError("x")]
)
def test_a_constructor_that_raises_a_gloss_error_is_not_rewrapped(registry, error):
    def init(self, context):
        raise error

    registry.register("translate", "thing", make_adapter(__init__=init))
    with pytest.raises(type(error)) as caught:
        registry.create("translate", "thing")
    assert caught.value is error


# ---- fallback chains ---------------------------------------------------------------------


def test_resolve_chain_keeps_order_and_drops_repeats(registry):
    for name in ("a", "b", "c"):
        registry.register("translate", name, make_adapter(name))
    assert registry.resolve_chain("translate", "b", ["a", "b", "c", "a"], local_only=False) == (
        "b",
        "a",
        "c",
    )


def test_resolve_chain_drops_unregistered_names_with_a_warning(registry, caplog):
    registry.register("translate", "a", make_adapter("a"))
    with caplog.at_level(logging.WARNING):
        assert registry.resolve_chain("translate", "a", ["ghost"], local_only=False) == ("a",)
    assert "ghost" in caplog.text


def test_resolve_chain_with_an_unavailable_primary_still_returns_the_fallbacks(registry):
    registry.register("translate", "b", make_adapter("b"))
    assert registry.resolve_chain("translate", "missing", ["b"], local_only=False) == ("b",)


def test_a_flag_off_adapter_is_dropped_as_a_fallback_but_allowed_as_primary(registry):
    registry.register("ocr", "good", _ocr("good"))
    registry.register("ocr", "windows", _ocr("windows", ALLOW_AUTO_FALLBACK=False))
    assert registry.resolve_chain("ocr", "good", ["windows"]) == ("good",)
    assert registry.resolve_chain("ocr", "windows", ["good"]) == ("windows", "good")


def _ocr(name, **attrs):
    return type(
        "O",
        (),
        {
            "name": name,
            "REQUIRES_CONTRACT": ">=1.0,<2.0",
            "languages": (),
            "open": lambda s: None,
            "recognize": lambda s, f, r: [],
            "close": lambda s: None,
            "health": lambda s: Health("ok"),
            **attrs,
        },
    )


# ---- discovery ---------------------------------------------------------------------------


@pytest.fixture
def isolated_default(monkeypatch):
    """Let discovery register into a scratch copy of the default registry."""
    monkeypatch.setattr(default_registry, "_adapters", {p: {} for p in default_registry._adapters})
    monkeypatch.setattr(default_registry, "_rejected", [])
    return default_registry


@pytest.fixture
def adapter_package(tmp_path, monkeypatch):
    """A throw-away package shaped like gloss.adapters, on sys.path for this test only."""
    root = tmp_path / "fake_adapters"
    (root / "translate").mkdir(parents=True)
    (root / "__init__.py").write_text("")
    (root / "translate" / "__init__.py").write_text("")
    (root / "toplevel_module.py").write_text("raise RuntimeError('must not be imported')")
    good = """
        from gloss.core.registry import register_adapter
        from gloss.core.contracts import Health

        @register_adapter("translate", "tmp_good")
        class Good:
            name = "tmp_good"
            REQUIRES_CONTRACT = ">=1.0,<2.0"
            def __init__(self, context): pass
            def open(self): pass
            def translate(self, request): pass
            def close(self): pass
            def health(self): return Health("ok")
    """
    (root / "translate" / "good.py").write_text(textwrap.dedent(good))
    (root / "translate" / "broken.py").write_text("import a_library_that_is_not_installed\n")
    (root / "translate" / "boom.py").write_text("raise ValueError('bad module')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield "fake_adapters"
    for name in [m for m in sys.modules if m == "fake_adapters" or m.startswith("fake_adapters.")]:
        del sys.modules[name]


def test_discover_registers_good_adapters_and_records_broken_modules(
    adapter_package, isolated_default
):
    discover(adapter_package)
    assert isolated_default.names("translate") == ("tmp_good",)
    reasons = {r.name: r.reason for r in isolated_default.rejected()}
    assert "fake_adapters.translate.broken" in reasons
    assert "import failed: ModuleNotFoundError" in reasons["fake_adapters.translate.broken"]
    assert "ValueError: bad module" in reasons["fake_adapters.translate.boom"]


def test_discover_ignores_modules_that_are_not_inside_a_port_package(
    adapter_package, isolated_default
):
    discover(adapter_package)  # toplevel_module raises on import, so this would blow up
    assert not any("toplevel" in r.name for r in isolated_default.rejected())


def test_discover_can_run_twice_without_duplicating_or_refusing(adapter_package, isolated_default):
    discover(adapter_package)
    discover(adapter_package)
    assert isolated_default.names("translate") == ("tmp_good",)
    assert len([r for r in isolated_default.rejected() if "good" in r.name]) == 0


def test_discover_into_a_given_registry_records_failures_there(adapter_package, isolated_default):
    mine = Registry()
    discover(adapter_package, registry=mine)
    assert {r.name for r in mine.rejected()} == {
        "fake_adapters.translate.broken",
        "fake_adapters.translate.boom",
    }


def test_the_real_adapter_tree_imports_without_refusals():
    discover()
    refused = [f"{r.port}/{r.name}: {r.reason}" for r in default_registry.rejected()]
    assert not refused, refused


def test_the_real_adapter_tree_registers_only_compatible_adapters():
    discover()
    for port in ("capture", "ocr", "translate", "render"):
        for name in default_registry.names(port):
            adapter = default_registry._adapters[port][name]
            assert contract_compatible(adapter.REQUIRES_CONTRACT)


HEAVY = (
    "rapidocr",
    "onnxruntime",
    "cv2",
    "PySide6",
    "anthropic",
    "ctranslate2",
    "sentencepiece",
    "torch",
    "windows_capture",
    "winrt",
)


def probe(setup: str, heavy: tuple[str, ...]) -> str:
    """Run ``setup`` in a clean interpreter and list which of ``heavy`` got imported."""
    script = textwrap.dedent(setup) + textwrap.dedent(
        f"""
        import sys
        heavy = {heavy!r}
        print("HEAVY:" + ",".join(sorted(m for m in sys.modules if m.split(".")[0] in heavy)))
        """
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr
    line = next(x for x in done.stdout.splitlines() if x.startswith("HEAVY:"))
    return line.removeprefix("HEAVY:")


def test_discovering_and_building_every_adapter_imports_no_heavy_library():
    """ports.py: the constructor is cheap and heavy imports wait for open()."""
    setup = """
        from gloss.core.registry import default_registry, discover
        from gloss.core.contracts import PORTS
        discover()
        for port in PORTS:
            for name in default_registry.names(port):
                try:
                    default_registry.create(port, name)
                except Exception:
                    pass
    """
    assert probe(setup, HEAVY) == "", "importing or constructing adapters pulled in heavy libraries"


def test_the_heavy_import_probe_can_actually_see_an_import():
    assert "json.tool" in probe("import json.tool", ("json",)).split(",")
    assert probe("import os", ("json",)) == ""


def test_the_adapter_package_name_is_the_one_the_tests_assume():
    assert registry_module.ADAPTERS_PACKAGE == "gloss.adapters"


def test_a_translate_chain_cannot_be_resolved_without_saying_whether_cloud_is_allowed(registry):
    """Privacy: a forgotten local_only must be a loud error, not a quiet cloud chain."""
    registry.register("translate", "a", make_adapter("a"))
    with pytest.raises(TypeError, match="local_only"):
        registry.resolve_chain("translate", "a")
    with pytest.raises(TypeError, match="local_only"):
        registry.resolve_chain("translate", "a", ["a"])
    assert registry.resolve_chain("translate", "a", local_only=False) == ("a",)


def test_other_ports_do_not_need_local_only(registry):
    registry.register("ocr", "good", _ocr("good"))
    assert registry.resolve_chain("ocr", "good") == ("good",)
