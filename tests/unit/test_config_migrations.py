"""core/config_migrations.py and the upgrade path in core/config.py.

The rule (SPEC section 4, ADR-0003): the user's file is never overwritten, only migrated, and
a backup is kept first.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from gloss.core import config as config_module
from gloss.core.config import default_config_path, load_config
from gloss.core.config_migrations import (
    CURRENT_CONFIG_VERSION,
    MIGRATIONS,
    ConfigMigrationError,
    migrate,
)

# ---- migrate() on its own ----------------------------------------------------------------


def test_a_file_with_no_version_counts_as_current():
    result = migrate({"capture": {"fps_max": 2}})
    assert not result.changed
    assert result.data == {"capture": {"fps_max": 2}}
    assert result.from_version == result.to_version == CURRENT_CONFIG_VERSION


def test_the_current_version_is_returned_unchanged():
    raw = {"config_version": CURRENT_CONFIG_VERSION, "ocr": {"stable_reads": 3}}
    assert migrate(raw).data == raw


def test_the_migration_table_has_a_step_for_every_older_version():
    for version in range(1, CURRENT_CONFIG_VERSION):
        assert version in MIGRATIONS, f"no migration from config version {version}"


def test_steps_run_in_order_and_stamp_the_version():
    steps = {
        1: lambda d: {**d, "one": True},
        2: lambda d: {**d, "two": d["one"]},
    }
    result = migrate({"config_version": 1}, current=3, migrations=steps)
    assert result.data == {"config_version": 3, "one": True, "two": True}
    assert (result.from_version, result.to_version, result.changed) == (1, 3, True)


def test_a_partly_old_file_only_runs_the_steps_it_needs():
    ran = []
    steps = {1: lambda d: ran.append(1) or d, 2: lambda d: ran.append(2) or d}
    migrate({"config_version": 2}, current=3, migrations=steps)
    assert ran == [2]


def test_migrate_never_changes_its_input():
    raw = {"config_version": 1, "capture": {"fps_max": 2}}
    migrate(raw, current=2, migrations={1: lambda d: d.update(capture={}) or d})
    assert raw == {"config_version": 1, "capture": {"fps_max": 2}}


def test_a_missing_step_is_a_migration_error():
    with pytest.raises(ConfigMigrationError, match="No migration from config version 1"):
        migrate({"config_version": 1}, current=2, migrations={})


def test_a_file_newer_than_the_app_is_returned_as_it_is():
    result = migrate({"config_version": 7, "future": 1}, current=2, migrations={})
    assert result.data == {"config_version": 7, "future": 1}
    assert result.from_version == 7 and result.to_version == 7 and not result.changed


@pytest.mark.parametrize("bad", [0, -2, "1", 1.0, True, None, [1]])
def test_an_unusable_version_is_a_migration_error(bad):
    with pytest.raises(ConfigMigrationError):
        migrate({"config_version": bad})


# ---- upgrading a real file on disk -------------------------------------------------------


@pytest.fixture
def v2(tmp_path, monkeypatch):
    """Pretend the app is at config version 2, with one migration from version 1."""

    def one_to_two(data):
        data = dict(data)
        capture = dict(data.get("capture", {}))
        if "fps" in capture:
            capture["fps_max"] = capture.pop("fps")
        data["capture"] = capture
        return data

    steps = {1: one_to_two}
    monkeypatch.setattr(config_module, "CURRENT_CONFIG_VERSION", 2)
    monkeypatch.setattr(
        config_module, "migrate", lambda raw: migrate(raw, current=2, migrations=steps)
    )
    text = default_config_path().read_text(encoding="utf-8")
    defaults = tmp_path / "default.toml"
    defaults.write_text(text.replace("config_version = 1", "config_version = 2"), encoding="utf-8")
    user = tmp_path / "Gloss" / "config.toml"
    user.parent.mkdir()
    return defaults, user


OLD = "config_version = 1\n# my notes\n[capture]\nfps = 2\n"


def test_an_old_file_is_upgraded_and_the_values_survive(v2):
    defaults, user = v2
    user.write_bytes(OLD.encode("utf-8"))
    loaded = load_config(defaults_path=defaults, user_path=user)
    assert loaded.config.capture.fps_max == 2
    assert any("was upgraded" in w for w in loaded.warnings)
    assert tomllib.loads(user.read_text(encoding="utf-8")) == {
        "config_version": 2,
        "capture": {"fps_max": 2},
    }


def test_the_upgrade_keeps_a_byte_for_byte_backup_of_the_original(v2):
    defaults, user = v2
    user.write_bytes(OLD.encode("utf-8"))
    loaded = load_config(defaults_path=defaults, user_path=user)
    backup = user.with_name("config.toml.v1.bak")
    assert backup.read_bytes() == OLD.encode("utf-8")
    assert str(backup) in "\n".join(loaded.warnings), "the warning must say where the backup is"


def test_an_existing_backup_is_never_overwritten(v2):
    defaults, user = v2
    earlier = user.with_name("config.toml.v1.bak")
    earlier.write_bytes(b"an older backup the user cares about")
    user.write_bytes(OLD.encode("utf-8"))
    load_config(defaults_path=defaults, user_path=user)
    assert earlier.read_bytes() == b"an older backup the user cares about"
    second = user.with_name("config.toml.v1.2.bak")
    assert second.read_bytes() == OLD.encode("utf-8")


def test_an_upgraded_file_is_not_upgraded_or_backed_up_again(v2):
    defaults, user = v2
    user.write_bytes(OLD.encode("utf-8"))
    load_config(defaults_path=defaults, user_path=user)
    after_first = user.read_bytes()
    again = load_config(defaults_path=defaults, user_path=user)
    assert user.read_bytes() == after_first
    assert len(list(user.parent.glob("*.bak"))) == 1
    assert again.warnings == ()


def test_no_temp_files_are_left_after_an_upgrade(v2):
    defaults, user = v2
    user.write_bytes(OLD.encode("utf-8"))
    load_config(defaults_path=defaults, user_path=user)
    assert not list(user.parent.glob("*.tmp"))


def test_if_the_upgrade_cannot_be_written_the_original_is_left_exactly_as_it_was(v2, monkeypatch):
    defaults, user = v2
    user.write_bytes(OLD.encode("utf-8"))

    def disk_full(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(config_module, "_write_toml_atomic", disk_full)
    loaded = load_config(defaults_path=defaults, user_path=user)
    assert user.read_bytes() == OLD.encode("utf-8")
    assert loaded.config.capture.fps_max == 2, "the migrated values are still used this run"
    assert any("could not be upgraded" in w for w in loaded.warnings)


def test_a_failed_atomic_write_does_not_damage_an_existing_file(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    target.write_bytes(b"keep me")

    def explode(src, dst):
        raise OSError("locked")

    monkeypatch.setattr(config_module.os, "replace", explode)
    with pytest.raises(OSError):
        config_module._write_toml_atomic(target, {"a": 1})
    assert target.read_bytes() == b"keep me"
    assert [p.name for p in tmp_path.iterdir()] == ["config.toml"]


def test_a_file_with_a_missing_migration_step_is_ignored_and_left_alone(v2, monkeypatch):
    defaults, user = v2
    monkeypatch.setattr(
        config_module, "migrate", lambda raw: migrate(raw, current=2, migrations={})
    )
    user.write_bytes(OLD.encode("utf-8"))
    loaded = load_config(defaults_path=defaults, user_path=user)
    assert any("No migration from config version 1" in w for w in loaded.warnings)
    assert user.read_bytes() == OLD.encode("utf-8")
    assert not list(user.parent.glob("*.bak"))


def test_a_migration_that_crashes_must_not_stop_the_app(v2, monkeypatch):
    """A buggy migration step is a Gloss bug, but the user's file must not turn it into a crash."""
    defaults, user = v2

    def buggy(data):
        return data["no_such_key"]

    monkeypatch.setattr(
        config_module, "migrate", lambda raw: migrate(raw, current=2, migrations={1: buggy})
    )
    user.write_bytes(OLD.encode("utf-8"))
    loaded = load_config(defaults_path=defaults, user_path=user)
    assert user.read_bytes() == OLD.encode("utf-8"), "the file must be left untouched"
    assert not list(user.parent.glob("*.bak"))
    assert loaded.warnings, "the user must be told the file was ignored"


def test_a_file_that_would_migrate_into_invalid_values_still_loads_with_defaults(v2):
    defaults, user = v2
    user.write_text("config_version = 1\n[capture]\nfps = -4\n", encoding="utf-8")
    loaded = load_config(defaults_path=defaults, user_path=user)
    defaults_only = load_config(defaults_path=defaults, user_path=Path("missing.toml")).config
    assert loaded.config.capture.fps_max == defaults_only.capture.fps_max
    assert any("capture.fps_max" in w for w in loaded.warnings)
