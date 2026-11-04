"""ADR-0005: every adapter setting is visible in default.toml, with the same value as the code.

Each test builds the adapter's settings twice: once from default.toml, once with only the
shared stage values (so the adapter falls back to its in-code defaults). The two must agree,
so default.toml never says one thing while the code does another.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from testkit.discovery import NO_USER_CONFIG

from gloss.adapters.ocr.rapidocr_engine import RapidOCREngine
from gloss.adapters.ocr.windows_ocr import WindowsOCREngine
from gloss.adapters.render.overlay import _OverlaySettings
from gloss.adapters.render.panel import _PanelSettings
from gloss.core.config import default_config_path, load_config, update_user_config
from gloss.core.contracts import AdapterContext, Rect
from gloss.services import cache, capture_common

CONFIG = load_config(user_path=NO_USER_CONFIG).config

# Values a port hands to every adapter. They belong to the stage table, never to an adapter's.
SHARED = {
    "capture": {"fps_max"},
    "ocr": {"languages", "profile", "min_confidence"},
    "translate": {"timeout_ms"},
    "render": {"font_size"},
}


def shared_only(port: str, name: str, keep: tuple[str, ...] = ()) -> dict:
    settings = CONFIG.adapter_settings(port, name)
    return {k: v for k, v in settings.items() if k in SHARED[port] or k in keep}


def _raw_defaults() -> dict:
    with default_config_path().open("rb") as handle:
        return tomllib.load(handle)


@pytest.mark.parametrize("port", sorted(SHARED))
def test_default_adapter_tables_do_not_repeat_shared_stage_settings(port):
    """One knob per setting: capture.fps_max is not also set in [capture.adapters.*]."""
    tables = _raw_defaults()[port]["adapters"]
    for name, table in tables.items():
        doubled = SHARED[port] & set(table)
        assert not doubled, f"[{port}.adapters.{name}] repeats shared keys {sorted(doubled)}"


@pytest.mark.parametrize("name", ["mss", "wgc"])
def test_capture_defaults_match_the_code(name):
    from_toml = capture_common.parse_settings(CONFIG.adapter_settings("capture", name))
    from_code = capture_common.parse_settings(shared_only("capture", name))
    assert from_toml == from_code
    assert CONFIG.capture.fps_max == capture_common.DEFAULT_FPS_MAX


_OCR_ATTRS = (
    "_limit_type",
    "_limit_len",
    "_intra",
    "_inter",
    "_max_side",
    "_use_cls",
    "_margin",
    "_model_dir",
    "_skip_langs",
    "_pre",
)


def test_rapidocr_defaults_match_the_code():
    full = RapidOCREngine(AdapterContext(settings=CONFIG.adapter_settings("ocr", "rapidocr")))
    bare = RapidOCREngine(AdapterContext(settings=shared_only("ocr", "rapidocr", ("profiles",))))
    for attr in _OCR_ATTRS:
        assert getattr(full, attr) == getattr(bare, attr), attr


def test_windows_ocr_defaults_match_the_code():
    full = WindowsOCREngine(AdapterContext(settings=CONFIG.adapter_settings("ocr", "windows")))
    bare = WindowsOCREngine(AdapterContext(settings=shared_only("ocr", "windows")))
    for attr in ("_margin", "_skip_langs", "_pre"):
        assert getattr(full, attr) == getattr(bare, attr), attr


def test_render_defaults_match_the_code():
    for name, parser in (("panel", _PanelSettings), ("overlay", _OverlaySettings)):
        full = parser.parse(CONFIG.adapter_settings("render", name))
        assert full == parser.parse(shared_only("render", name)), name


def test_cache_size_default_matches_the_code():
    assert CONFIG.storage.cache_max_entries == cache.DEFAULT_MAX_ENTRIES


# ---- saved capture region ----------------------------------------------------------------


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_no_region_is_saved_by_default():
    assert CONFIG.capture.region == []
    assert CONFIG.capture.saved_region is None


def test_a_saved_region_may_sit_on_a_monitor_left_of_the_primary(tmp_path):
    user = _write(tmp_path / "config.toml", "[capture]\nregion = [-1920, 40, 800, 600]\n")
    loaded = load_config(user_path=user)
    assert loaded.warnings == ()
    assert loaded.config.capture.saved_region == Rect(-1920, 40, 800, 600)


@pytest.mark.parametrize(
    "value",
    [
        "[1, 2, 3]",
        "[1, 2, 3, 4, 5]",
        "[0, 0, 0, 100]",
        "[0, 0, 100, -5]",
        "[0, 0, 1.5, 100]",
        "[0, 0, true, 100]",
        '"0,0,100,100"',
        "[0, 0, 100, 2000000]",
    ],
)
def test_a_bad_saved_region_warns_and_falls_back_to_none(tmp_path, value):
    user = _write(tmp_path / "config.toml", f"[capture]\nregion = {value}\n")
    loaded = load_config(user_path=user)
    assert loaded.config.capture.saved_region is None
    assert any("capture.region" in w for w in loaded.warnings), loaded.warnings


def test_the_settings_window_can_save_and_clear_the_region(tmp_path):
    user = tmp_path / "config.toml"
    update_user_config({"capture": {"region": [10, 20, 300, 200]}}, user_path=user)
    assert load_config(user_path=user).config.capture.saved_region == Rect(10, 20, 300, 200)
    update_user_config({"capture": {"region": []}}, user_path=user)
    assert load_config(user_path=user).config.capture.saved_region is None


def test_the_settings_window_cannot_save_a_bad_region(tmp_path):
    from pydantic import ValidationError

    user = tmp_path / "config.toml"
    with pytest.raises(ValidationError):
        update_user_config({"capture": {"region": [0, 0, 0, 0]}}, user_path=user)
    assert not user.exists()


# ---- OCR stage settings ------------------------------------------------------------------


def test_low_quality_engine_is_registered_and_never_an_automatic_fallback():
    from gloss.core.registry import default_registry, discover

    discover()
    name = CONFIG.ocr.low_quality_engine
    assert name == "windows"
    assert name in default_registry.names("ocr")
    assert name not in CONFIG.ocr.fallbacks


@pytest.mark.parametrize("value", ['"Bad Name"', "3", '""'])
def test_a_bad_low_quality_engine_name_warns_and_uses_the_default(tmp_path, value):
    user = _write(tmp_path / "config.toml", f"[ocr]\nlow_quality_engine = {value}\n")
    loaded = load_config(user_path=user)
    assert loaded.config.ocr.low_quality_engine == "windows"
    assert any("ocr.low_quality_engine" in w for w in loaded.warnings), loaded.warnings


def test_dedupe_memory_limit_matches_the_tracker_code():
    from gloss.services import text_tracker

    assert CONFIG.ocr.dedupe_max_lines == text_tracker._MAX_SEEN


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "true"])
def test_a_bad_dedupe_memory_limit_warns_and_uses_the_default(tmp_path, value):
    user = _write(tmp_path / "config.toml", f"[ocr]\ndedupe_max_lines = {value}\n")
    loaded = load_config(user_path=user)
    assert loaded.config.ocr.dedupe_max_lines == 2000
    assert any("ocr.dedupe_max_lines" in w for w in loaded.warnings), loaded.warnings
