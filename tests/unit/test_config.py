"""core/config.py: defaults, user override, unknown keys, bad values, free-form adapter tables."""

from __future__ import annotations

import inspect
import tomllib
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from gloss.core import config as config_module
from gloss.core.config import (
    ConfigError,
    GlossConfig,
    default_config_path,
    load_config,
    update_user_config,
    user_config_path,
)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def user(tmp_path: Path) -> Path:
    return tmp_path / "user" / "config.toml"


def load(user_file: Path, defaults: Path | None = None):
    return load_config(defaults_path=defaults, user_path=user_file)


def assert_defaults_with_cloud_off(loaded) -> None:
    """An unusable user file is ignored, so every setting is the default except the privacy
    switch, which fails safe (CLAUDE.md rule 7)."""
    expected = default_values().model_dump()
    expected["privacy"]["cloud_translation"] = False
    assert loaded.config.model_dump() == expected


def default_values() -> GlossConfig:
    return load_config(user_path=Path("does-not-exist.toml")).config


# ---- defaults ----------------------------------------------------------------------------


def test_default_toml_loads_clean_with_no_user_file(user):
    loaded = load(user)
    assert loaded.warnings == ()
    assert not user.exists(), "loading must never create the user's file"
    assert not user.parent.exists()


def test_every_value_comes_from_default_toml_not_from_the_schema():
    """ADR-0003: the schema states what is valid, and holds no default values."""
    problems = []
    for _, obj in inspect.getmembers(config_module, inspect.isclass):
        if issubclass(obj, BaseModel) and obj.__module__ == config_module.__name__:
            problems += [
                f"{obj.__name__}.{name}"
                for name, info in obj.model_fields.items()
                if not info.is_required()
            ]
    assert not problems, f"fields with a default in the schema: {problems}"


def test_default_toml_covers_every_section_the_schema_requires():
    with default_config_path().open("rb") as handle:
        raw = tomllib.load(handle)
    for section in GlossConfig.model_fields:
        assert section in raw, f"default.toml has no [{section}]"


def test_default_toml_has_a_price_for_the_default_model():
    config = default_values()
    model = config.translate.adapters["anthropic"]["model"]
    assert model in config.translate.prices


def test_missing_default_toml_is_a_build_error(tmp_path):
    with pytest.raises(ConfigError, match="Cannot read default config"):
        load_config(defaults_path=tmp_path / "nope.toml", user_path=tmp_path / "u.toml")


def test_invalid_default_toml_is_a_build_error_not_a_silent_fallback(tmp_path):
    text = default_config_path().read_text(encoding="utf-8").replace("fps_max = 12", "fps_max = -1")
    bad = write(tmp_path / "default.toml", text)
    with pytest.raises(ConfigError, match="invalid"):
        load_config(defaults_path=bad, user_path=tmp_path / "u.toml")


def test_default_toml_with_the_wrong_config_version_is_a_build_error(tmp_path):
    text = (
        default_config_path()
        .read_text(encoding="utf-8")
        .replace("config_version = 1", "config_version = 99")
    )
    bad = write(tmp_path / "default.toml", text)
    with pytest.raises(ConfigError, match="config_version"):
        load_config(defaults_path=bad, user_path=tmp_path / "u.toml")


def test_user_config_path_is_under_appdata(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert user_config_path() == tmp_path / "Gloss" / "config.toml"


# ---- user override -----------------------------------------------------------------------


def test_a_user_value_overrides_one_key_and_leaves_the_rest(user):
    write(user, "[ocr]\nmin_confidence = 0.7\n")
    loaded = load(user)
    baseline = default_values()
    assert loaded.config.ocr.min_confidence == 0.7
    assert loaded.config.ocr.engine == baseline.ocr.engine
    assert loaded.config.ocr.stable_reads == baseline.ocr.stable_reads
    assert loaded.warnings == ()


def test_tables_merge_but_lists_are_replaced(user):
    write(
        user,
        '[translate]\nfallbacks = ["quickmt"]\n'
        '[translate.adapters.anthropic]\nmodel = "my-model"\n',
    )
    config = load(user).config
    anthropic = config.translate.adapters["anthropic"]
    default_anthropic = default_values().translate.adapters["anthropic"]
    assert anthropic["model"] == "my-model"
    assert anthropic["secret_name"] == default_anthropic["secret_name"]
    assert config.translate.fallbacks == ["quickmt"]


def test_a_partial_price_table_for_a_known_model_merges(user):
    write(user, "[translate.prices.claude-haiku-5-5]\ninput = 0.2\n")
    loaded = load(user)
    price = loaded.config.translate.prices["claude-haiku-5-5"]
    assert price.input == 0.2
    assert price.output == default_values().translate.prices["claude-haiku-5-5"].output
    assert loaded.warnings == ()


def test_a_user_can_add_a_new_adapter_table_without_a_schema_change(user):
    write(user, "[ocr.adapters.shiny]\nanything = [1, 2]\nnested = { a = true }\n")
    loaded = load(user)
    settings = loaded.config.adapter_settings("ocr", "shiny")
    assert settings["anything"] == [1, 2]
    assert settings["nested"] == {"a": True}
    assert settings["languages"] == default_values().ocr.languages
    assert loaded.warnings == (), "adapter tables are free-form, so nothing is unknown there"


# ---- unknown keys ------------------------------------------------------------------------


def test_unknown_keys_warn_and_name_the_full_path(user):
    write(user, 'colour = "red"\n[capture]\nfps_mx = 9\n[translate.budget]\nweekly = 1\n')
    loaded = load(user)
    messages = "\n".join(loaded.warnings)
    for path in ("colour", "capture.fps_mx", "translate.budget.weekly"):
        assert f"Unknown setting ignored: {path}" in messages
    assert loaded.config.capture.fps_max == default_values().capture.fps_max


def test_an_unknown_key_never_stops_the_app(user):
    write(user, "[[surprise]]\nx = 1\n[render]\nfont_size = 20\n")
    loaded = load(user)
    assert loaded.config.render.font_size == 20
    assert any("surprise" in w for w in loaded.warnings)


# ---- bad values fall back to the default for that key only -------------------------------


@pytest.mark.parametrize(
    ("table", "key", "bad"),
    [
        ("capture", "fps_max", "-1"),
        ("capture", "fps_max", '"fast"'),
        ("capture", "mode", '"fullscreen"'),
        ("capture", "tile_size", "0"),
        ("capture", "tile_size", "6.5"),
        ("ocr", "min_confidence", "1.5"),
        ("ocr", "profile", '"turbo"'),
        ("ocr", "engine", '"Rapid OCR"'),
        ("ocr", "languages", "[]"),
        ("ocr", "languages", '["zh", "ENGLISH"]'),
        ("ocr", "fallbacks", '["Bad Name"]'),
        ("hotkeys", "toggle", '"ctrl+"'),
        ("hotkeys", "pause", '"t"'),
        ("translate", "target_lang", '"english"'),
        ("translate.budget", "warn_at", "0"),
        ("translate.budget", "monthly_usd", "-5"),
        ("reliability", "retry_max", "-1"),
        ("storage", "database", '""'),
    ],
)
def test_a_bad_value_gets_a_warning_and_that_keys_default(user, table, key, bad):
    write(user, f"[{table}]\n{key} = {bad}\n")
    loaded = load(user)
    path = f"{table}.{key}"
    # A bad list item is reported as 'ocr.languages.1', so match the key as a prefix.
    assert any(f"'{path}" in w for w in loaded.warnings), loaded.warnings
    node, expected = loaded.config, default_values()
    for part in path.split("."):
        node, expected = getattr(node, part), getattr(expected, part)
    assert node == expected
    assert loaded.config == default_values()


def test_a_toml_boolean_is_not_accepted_as_a_number(user):
    """pydantic's default lax mode turns true into 1. A hand-edit slip should be refused."""
    write(user, "[translate]" + chr(10) + "batch_max_lines = true" + chr(10))
    loaded = load(user)
    assert loaded.config.translate.batch_max_lines == default_values().translate.batch_max_lines
    assert loaded.warnings


@pytest.mark.parametrize("flag", ["image_mode", "debug_logging"])
def test_a_text_string_is_not_accepted_as_a_privacy_switch(user, flag):
    """pydantic's default lax mode turns "yes" into True. Privacy switches must be real booleans."""
    write(user, "[privacy]" + chr(10) + f'{flag} = "yes"' + chr(10))
    loaded = load(user)
    assert getattr(loaded.config.privacy, flag) == getattr(default_values().privacy, flag)
    assert loaded.warnings


def test_an_invalid_cloud_translation_value_resolves_to_false_with_a_warning(user):
    """CLAUDE.md rule 7: when unsure, keep text on this PC. So it is false, not the default."""
    write(user, "[privacy]" + chr(10) + 'cloud_translation = "yes"' + chr(10))
    loaded = load(user)
    assert loaded.config.privacy.cloud_translation is False
    assert any("cloud_translation" in w for w in loaded.warnings)


def test_one_bad_key_does_not_cost_the_valid_keys_beside_it(user):
    write(user, '[capture]\nfps_max = -1\ntile_size = 128\nmode = "window"\n')
    loaded = load(user)
    assert loaded.config.capture.fps_max == default_values().capture.fps_max
    assert loaded.config.capture.tile_size == 128
    assert loaded.config.capture.mode == "window"
    assert len(loaded.warnings) == 1


def test_several_bad_keys_each_get_their_own_warning(user):
    write(user, "[capture]\nfps_max = -1\n[ocr]\nmin_confidence = 9\n[render]\nmode = 'x'\n")
    loaded = load(user)
    assert len(loaded.warnings) == 3
    assert loaded.config == default_values()


def test_an_incomplete_new_price_table_is_dropped_with_a_warning(user):
    write(user, "[translate.prices.shiny-model]\ninput = 1.0\n")
    loaded = load(user)
    assert "shiny-model" not in loaded.config.translate.prices
    assert loaded.config.translate.prices.keys() == default_values().translate.prices.keys()
    assert any("shiny-model" in w for w in loaded.warnings)


def test_a_table_where_a_value_belongs_is_a_bad_value_not_a_crash(user):
    write(user, "[capture.fps_max]\nx = 1\n[ocr]\nlanguages = 5\n")
    loaded = load(user)
    assert loaded.config.capture.fps_max == default_values().capture.fps_max
    assert loaded.config.ocr.languages == default_values().ocr.languages
    assert len(loaded.warnings) == 2


# ---- unreadable or odd user files --------------------------------------------------------


def test_a_user_file_that_is_not_toml_is_ignored_with_a_warning_and_left_alone(user):
    write(user, "this is [not toml")
    loaded = load(user)
    assert_defaults_with_cloud_off(loaded)
    assert any("could not be read" in w for w in loaded.warnings)
    assert user.read_text(encoding="utf-8") == "this is [not toml"


def test_a_user_file_with_binary_junk_is_ignored(user):
    user.parent.mkdir(parents=True)
    user.write_bytes(b"\xff\xfe\x00junk")
    loaded = load(user)
    assert_defaults_with_cloud_off(loaded)
    assert loaded.warnings


def test_a_user_config_version_newer_than_the_app_warns_and_is_not_rewritten(user):
    original = "config_version = 99\n[capture]\nfps_max = 2\nnew_in_v99 = true\n"
    write(user, original)
    loaded = load(user)
    assert "newer than this app" in "\n".join(loaded.warnings)
    assert loaded.config.capture.fps_max == 2
    assert user.read_text(encoding="utf-8") == original
    assert not list(user.parent.glob("*.bak"))


@pytest.mark.parametrize("version", ["0", "-1", '"1"', "true", "1.5"])
def test_an_unusable_config_version_in_the_user_file_ignores_the_file(user, version):
    write(user, f"config_version = {version}\n[capture]\nfps_max = 2\n")
    loaded = load(user)
    assert_defaults_with_cloud_off(loaded)
    assert any("Ignored your config file" in w for w in loaded.warnings)


def test_config_objects_are_frozen():
    config = default_values()
    with pytest.raises(ValidationError):
        config.capture.fps_max = 99  # type: ignore[misc]


# ---- helpers on the config ---------------------------------------------------------------


def test_adapter_settings_puts_shared_values_first_so_the_adapter_table_wins(user):
    write(user, "[ocr.adapters.rapidocr]\nprofile = 'custom'\nthreads_extra = 3\n")
    settings = load(user).config.adapter_settings("ocr", "rapidocr")
    assert settings["profile"] == "custom"
    assert settings["threads_extra"] == 3
    assert settings["languages"] == default_values().ocr.languages


@pytest.mark.parametrize("port", ["capture", "ocr", "translate", "render"])
def test_adapter_settings_for_every_port_gives_plain_data(port):
    settings = default_values().adapter_settings(port, "not-listed")
    assert isinstance(settings, dict) and settings


def test_adapter_settings_rejects_an_unknown_port():
    with pytest.raises(ValueError, match="Unknown port"):
        default_values().adapter_settings("storage", "x")


def test_adapter_settings_hands_out_copies():
    config = default_values()
    first = config.adapter_settings("translate", "anthropic")
    first["model"] = "changed"
    assert config.adapter_settings("translate", "anthropic")["model"] != "changed"


def test_price_for_charges_an_unlisted_model_at_the_highest_listed_price():
    translate = default_values().translate
    top = max(translate.prices.values(), key=lambda p: (p.output, p.input))
    assert translate.price_for("some-future-model") == top
    listed = next(iter(translate.prices))
    assert translate.price_for(listed) == translate.prices[listed]


def test_capture_active_follows_the_mode(user):
    write(user, '[capture]\nmode = "window"\n')
    config = load(user).config
    assert config.capture.active is config.capture.sources.window


@pytest.mark.parametrize(
    ("mode", "expected"), [("panel", ("panel",)), ("both", ("panel", "overlay"))]
)
def test_render_renderers_expands_both(user, mode, expected):
    write(user, f'[render]\nmode = "{mode}"\n')
    assert load(user).config.render.renderers == expected


# ---- update_user_config (the settings window) --------------------------------------------


def test_update_creates_the_file_and_it_loads_back(user):
    update_user_config({"render": {"font_size": 18}}, user_path=user)
    assert load(user).config.render.font_size == 18
    assert tomllib.loads(user.read_text(encoding="utf-8"))["config_version"] == 1


def test_update_keeps_every_previous_file_as_a_backup_and_never_overwrites_one(user):
    original = "[render]\nfont_size = 16\n"
    write(user, original)
    update_user_config({"render": {"font_size": 18}}, user_path=user)
    second_original = user.read_text(encoding="utf-8")
    update_user_config({"render": {"font_size": 20}}, user_path=user)
    backups = list(user.parent.glob("config.toml.settings*.bak"))
    assert sorted(b.read_text(encoding="utf-8") for b in backups) == sorted(
        [original, second_original]
    )
    assert load(user).config.render.font_size == 20


def test_update_with_an_invalid_value_raises_and_leaves_the_file_untouched(user):
    original = "[render]\nfont_size = 16\n"
    write(user, original)
    with pytest.raises(ValidationError):
        update_user_config({"render": {"font_size": -3}}, user_path=user)
    assert user.read_text(encoding="utf-8") == original
    assert not list(user.parent.glob("*.bak"))
    assert not list(user.parent.glob("*.tmp"))


def test_update_leaves_no_temp_files_behind(user):
    update_user_config({"hotkeys": {"toggle": "ctrl+alt+x"}}, user_path=user)
    assert [p.name for p in user.parent.iterdir()] == ["config.toml"]


# ---- privacy: when a user file exists but cannot be trusted, cloud translation goes OFF ----
# CLAUDE.md rule 7. A missing file means the user never chose, so the default (true) applies.
# A file that exists but cannot be read the way the user meant it might have said false.

CLOUD_OFF = "Cloud translation is OFF"


def assert_cloud_off(loaded) -> None:
    assert loaded.config.privacy.cloud_translation is False
    assert any(CLOUD_OFF in w for w in loaded.warnings), loaded.warnings


def test_without_a_user_file_cloud_translation_keeps_the_default(user):
    loaded = load(user)
    assert loaded.config.privacy.cloud_translation is True
    assert not any(CLOUD_OFF in w for w in loaded.warnings)


@pytest.mark.parametrize(
    "text",
    [
        "this is [not toml",
        "[capture]\nfps_max = 2\n[privacy\ncloud_translation = false\n",
        "[privacy]\ncloud_translation = false\n[capture]\nfps_max = = 2\n",
        "cloud_translation = false\nstray words\n",
    ],
    ids=["garbage", "error-before-privacy", "error-after-privacy", "error-at-top-level"],
)
def test_a_toml_syntax_error_anywhere_turns_cloud_translation_off(user, text):
    write(user, text)
    assert_cloud_off(load(user))


@pytest.mark.parametrize("version", ["0", "-1", '"1"', "true", "1.5"])
def test_a_bad_config_version_turns_cloud_translation_off(user, version):
    write(user, f"config_version = {version}\n[privacy]\ncloud_translation = false\n")
    assert_cloud_off(load(user))


def test_privacy_set_to_a_string_turns_cloud_translation_off(user):
    write(user, 'privacy = "off"\n')
    assert_cloud_off(load(user))


def test_a_misspelled_privacy_key_turns_cloud_translation_off(user):
    write(user, "[privacy]\ncloud_translaton = false\n")
    loaded = load(user)
    assert_cloud_off(loaded)
    assert any("cloud_translaton" in w for w in loaded.warnings)


def test_a_wrong_case_privacy_section_turns_cloud_translation_off(user):
    write(user, "[Privacy]\ncloud_translation = false\n")
    loaded = load(user)
    assert_cloud_off(loaded)
    assert any("Privacy" in w for w in loaded.warnings)


def test_a_file_that_is_not_utf8_turns_cloud_translation_off(user):
    user.parent.mkdir(parents=True)
    user.write_bytes(b"\xff\xfe[\x00p\x00r\x00i\x00v\x00")  # what Notepad's UTF-16 looks like
    assert_cloud_off(load(user))


def test_a_valid_file_that_says_false_gives_false_without_the_alarm(user):
    write(user, "[privacy]\ncloud_translation = false\n")
    loaded = load(user)
    assert loaded.config.privacy.cloud_translation is False
    assert not any(CLOUD_OFF in w for w in loaded.warnings)


def test_a_valid_file_that_says_true_gives_true(user):
    write(user, "[privacy]\ncloud_translation = true\n")
    assert load(user).config.privacy.cloud_translation is True


def test_an_unrelated_bad_value_does_not_turn_cloud_translation_off(user):
    write(user, "[capture]\nfps_max = -1\n")
    loaded = load(user)
    assert loaded.config.privacy.cloud_translation is True
    assert not any(CLOUD_OFF in w for w in loaded.warnings)


def test_an_unrelated_misspelling_does_not_turn_cloud_translation_off(user):
    write(user, "[capture]\nfps_mx = 3\n")
    assert load(user).config.privacy.cloud_translation is True


def test_an_unknown_key_beside_a_valid_cloud_translation_true_still_turns_it_off(user):
    write(user, "[privacy]\ncloud_translation = true\nshare_with_friends = true\n")
    loaded = load(user)
    assert_cloud_off(loaded)
    assert any("share_with_friends" in w for w in loaded.warnings)


@pytest.mark.parametrize(
    "text",
    [
        "cloud_translation = false\n",
        "[translate]\ncloud_translation = false\n",
        "[privcy]\ncloud_translation = false\n",
        "[privacy_settings]\ncloud_translation = false\n",
        "privcy.cloud_translation = false\n",
        "[privacy.extra]\ncloud_translation = false\n",
    ],
    ids=["top-level", "under-translate", "privcy", "privacy_settings", "dotted-privcy", "nested"],
)
def test_a_cloud_translation_setting_in_the_wrong_place_turns_cloud_off(user, text):
    """The user clearly wanted it off. If it landed where it is ignored, fail safe."""
    write(user, text)
    assert_cloud_off(load(user))


def test_a_user_file_that_cannot_be_checked_for_existence_turns_cloud_off(user, monkeypatch):
    write(user, "[privacy]\ncloud_translation = false\n")
    real_exists = Path.exists

    def locked(self, *args, **kwargs):
        if self == user:
            raise PermissionError("access denied")
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", locked)
    assert_cloud_off(load(user))


def test_a_user_file_that_cannot_be_opened_turns_cloud_off(user, monkeypatch):
    write(user, "[privacy]\ncloud_translation = false\n")
    real_open = Path.open

    def locked(self, *args, **kwargs):
        if self == user:
            raise PermissionError("file is locked")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", locked)
    assert_cloud_off(load(user))


def test_a_normal_file_with_unrelated_keys_keeps_cloud_translation_on(user):
    """False-positive guard: ordinary customisation must not trip the privacy fail-safe."""
    write(
        user,
        "[capture]\nfps_max = 2\n[ocr]\nmin_confidence = 0.7\nlanguages = ['zh', 'en']\n"
        "[translate]\nfallbacks = ['null']\n[translate.adapters.anthropic]\nmodel = 'x'\n"
        "[render]\nfont_size = 18\n[hotkeys]\ntoggle = 'ctrl+alt+x'\n",
    )
    loaded = load(user)
    assert loaded.config.privacy.cloud_translation is True
    assert not any(CLOUD_OFF in w for w in loaded.warnings)
    assert loaded.config.render.font_size == 18


def test_a_normal_file_with_other_privacy_choices_keeps_cloud_translation_on(user):
    write(user, "[privacy]\ndebug_logging = true\n")
    loaded = load(user)
    assert loaded.config.privacy.cloud_translation is True
    assert loaded.config.privacy.debug_logging is True
