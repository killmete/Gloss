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


def test_capture_fps_default_matches_the_code():
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


# ---- adaptive frame rate and box stability -----------------------------------------------


def test_frame_rate_and_box_stability_defaults():
    c = CONFIG
    assert (c.capture.fps_max, c.capture.idle_fps, c.capture.idle_after_s) == (12, 4, 1.0)
    assert c.render.move_threshold_px == 4


def test_idle_fps_may_equal_fps_max(tmp_path):
    user = _write(tmp_path / "config.toml", "[capture]\nfps_max = 6\nidle_fps = 6\n")
    loaded = load_config(user_path=user)
    assert loaded.warnings == ()
    assert loaded.config.capture.idle_fps == 6


def test_idle_fps_above_fps_max_warns_and_uses_fps_max(tmp_path):
    user = _write(tmp_path / "config.toml", "[capture]\nfps_max = 6\nidle_fps = 10\n")
    loaded = load_config(user_path=user)
    assert (loaded.config.capture.fps_max, loaded.config.capture.idle_fps) == (6, 6)
    assert any("capture.idle_fps" in w for w in loaded.warnings), loaded.warnings


def test_lowering_fps_max_below_the_default_idle_fps_keeps_the_users_fps_max(tmp_path):
    """A lower fps_max alone is a sensible choice, so it must not throw away the user's
    other capture settings. The idle rate follows it down."""
    user = _write(tmp_path / "config.toml", "[capture]\nfps_max = 2\ntile_size = 32\n")
    loaded = load_config(user_path=user)
    capture = loaded.config.capture
    assert (capture.fps_max, capture.idle_fps, capture.tile_size) == (2, 2, 32)
    assert loaded.warnings == (), "a lower fps_max on its own is not a mistake"
    assert "idle_fps" not in user.read_text(encoding="utf-8"), "the file is not rewritten"


def test_the_settings_window_can_save_a_low_fps_max_alone(tmp_path):
    user = tmp_path / "config.toml"
    update_user_config({"capture": {"fps_max": 2}}, user_path=user)
    assert "idle_fps" not in user.read_text(encoding="utf-8")
    capture = load_config(user_path=user).config.capture
    assert (capture.fps_max, capture.idle_fps) == (2, 2)


def test_the_settings_window_cannot_save_idle_fps_above_fps_max(tmp_path):
    from pydantic import ValidationError

    user = tmp_path / "config.toml"
    with pytest.raises(ValidationError):
        update_user_config({"capture": {"fps_max": 5, "idle_fps": 8}}, user_path=user)
    assert not user.exists()


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("capture", "idle_fps", "0"),
        ("capture", "idle_fps", "true"),
        ("capture", "idle_after_s", "0"),
        ("capture", "idle_after_s", "-1.0"),
        ("render", "move_threshold_px", "-1"),
        ("render", "move_threshold_px", "2.5"),
        ("render", "move_threshold_px", "true"),
    ],
)
def test_bad_frame_rate_or_threshold_values_warn_and_use_the_default(tmp_path, section, key, value):
    user = _write(tmp_path / "config.toml", f"[{section}]\n{key} = {value}\n")
    loaded = load_config(user_path=user)
    assert getattr(getattr(loaded.config, section), key) == getattr(getattr(CONFIG, section), key)
    assert any(f"{section}.{key}" in w for w in loaded.warnings), loaded.warnings


def test_move_threshold_zero_is_allowed(tmp_path):
    user = _write(tmp_path / "config.toml", "[render]\nmove_threshold_px = 0\n")
    loaded = load_config(user_path=user)
    assert loaded.warnings == () and loaded.config.render.move_threshold_px == 0


def test_overlay_fit_keys_are_listed_with_sane_defaults():
    """The overlay validates these itself (free-form table); the defaults test above checks
    they equal the code once the ui-engineer adds them."""
    o = CONFIG.render.adapters["overlay"]
    assert (o["min_font_px"], o["max_font_px"], o["max_lines"]) == (9, 28, 2)
    assert 0 < o["min_font_px"] <= o["max_font_px"]
    assert o["clip_to_capture"] is True


# ---- change detection and text-tracker pieces --------------------------------------------

_DETECTOR_KEYS = (
    "scroll_min_agreement",
    "animated_window",
    "animated_min_changes",
    "animated_recheck_s",
    "animated_max_share",
    "animated_unmark_cells",
)
_PIECE_KEYS = ("piece_inside", "piece_min_chars")


def _code_default(func, name):
    import inspect

    return inspect.signature(func).parameters[name].default


@pytest.mark.parametrize("key", _DETECTOR_KEYS)
def test_change_detector_defaults_match_the_code(key):
    from gloss.services.change_detector import ChangeDetector

    assert getattr(CONFIG.capture, key) == _code_default(ChangeDetector.__init__, key)


@pytest.mark.parametrize("key", _PIECE_KEYS)
def test_tracker_piece_defaults_match_the_code(key):
    from gloss.services.text_tracker import TrackerConfig

    assert getattr(CONFIG.ocr, key) == getattr(TrackerConfig(), key)


def test_config_values_build_a_valid_detector_and_tracker():
    from gloss.services.change_detector import ChangeDetector
    from gloss.services.text_tracker import TrackerConfig

    ChangeDetector(**{k: getattr(CONFIG.capture, k) for k in _DETECTOR_KEYS})
    TrackerConfig(**{k: getattr(CONFIG.ocr, k) for k in _PIECE_KEYS})


def test_a_user_config_without_the_new_keys_loads_quietly(tmp_path):
    user = _write(tmp_path / "config.toml", "[capture]\ntile_size = 32\n[ocr]\nstable_reads = 2\n")
    loaded = load_config(user_path=user)
    assert loaded.warnings == ()
    assert loaded.config.capture.animated_window == CONFIG.capture.animated_window
    assert loaded.config.ocr.piece_min_chars == CONFIG.ocr.piece_min_chars


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("capture", "scroll_min_agreement", "0"),
        ("capture", "scroll_min_agreement", "1.5"),
        ("capture", "scroll_min_agreement", "true"),
        ("capture", "animated_window", "0"),
        ("capture", "animated_window", "2.5"),
        ("capture", "animated_min_changes", "0"),
        ("capture", "animated_recheck_s", "0"),
        ("capture", "animated_recheck_s", "-1.0"),
        ("capture", "animated_max_share", "0"),
        ("capture", "animated_max_share", "1.1"),
        ("capture", "animated_unmark_cells", "-1"),
        ("capture", "animated_unmark_cells", "true"),
        ("ocr", "piece_inside", "-0.1"),
        ("ocr", "piece_inside", "1.1"),
        ("ocr", "piece_inside", '"high"'),
        ("ocr", "piece_min_chars", "0"),
        ("ocr", "piece_min_chars", "1.5"),
    ],
)
def test_bad_detector_or_piece_values_warn_and_use_the_default(tmp_path, section, key, value):
    user = _write(tmp_path / "config.toml", f"[{section}]\n{key} = {value}\n")
    loaded = load_config(user_path=user)
    assert getattr(getattr(loaded.config, section), key) == getattr(getattr(CONFIG, section), key)
    assert any(f"{section}.{key}" in w for w in loaded.warnings), loaded.warnings


@pytest.mark.parametrize(("key", "value"), [("scroll_min_agreement", 1), ("piece_inside", 0)])
def test_ratio_edges_are_allowed(tmp_path, key, value):
    section = "capture" if key.startswith("scroll") else "ocr"
    user = _write(tmp_path / "config.toml", f"[{section}]\n{key} = {value}\n")
    loaded = load_config(user_path=user)
    assert loaded.warnings == ()
    assert getattr(getattr(loaded.config, section), key) == value


def test_min_changes_may_equal_the_window(tmp_path):
    user = _write(
        tmp_path / "config.toml", "[capture]\nanimated_window = 4\nanimated_min_changes = 4\n"
    )
    loaded = load_config(user_path=user)
    assert loaded.warnings == ()
    assert (loaded.config.capture.animated_window, loaded.config.capture.animated_min_changes) == (
        4,
        4,
    )


def test_min_changes_above_the_window_warns_and_uses_the_window(tmp_path):
    user = _write(
        tmp_path / "config.toml", "[capture]\nanimated_window = 4\nanimated_min_changes = 6\n"
    )
    loaded = load_config(user_path=user)
    assert (loaded.config.capture.animated_window, loaded.config.capture.animated_min_changes) == (
        4,
        4,
    )
    assert any("capture.animated_min_changes" in w for w in loaded.warnings), loaded.warnings


def test_a_small_window_alone_pulls_min_changes_down_quietly(tmp_path):
    """Lowering only animated_window below the default threshold is a sensible choice."""
    user = _write(tmp_path / "config.toml", "[capture]\nanimated_window = 2\ntile_size = 32\n")
    loaded = load_config(user_path=user)
    capture = loaded.config.capture
    assert (capture.animated_window, capture.animated_min_changes, capture.tile_size) == (2, 2, 32)
    assert loaded.warnings == ()


def test_the_settings_window_can_save_a_small_window_alone(tmp_path):
    user = tmp_path / "config.toml"
    update_user_config({"capture": {"animated_window": 1}}, user_path=user)
    assert "animated_min_changes" not in user.read_text(encoding="utf-8")
    assert load_config(user_path=user).config.capture.animated_min_changes == 1


def test_the_settings_window_cannot_save_min_changes_above_the_window(tmp_path):
    from pydantic import ValidationError

    user = tmp_path / "config.toml"
    with pytest.raises(ValidationError):
        update_user_config(
            {"capture": {"animated_window": 3, "animated_min_changes": 5}}, user_path=user
        )
    assert not user.exists()
