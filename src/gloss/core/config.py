"""Loads and validates settings.

``config/default.toml`` holds every default, so nothing external or tunable is hard-coded
here. The schema has no default values: it only states what is valid. The user's file at
``%APPDATA%/Gloss/config.toml`` overrides it, key by key.

A bad user file never stops the app. Unknown keys and invalid values produce a warning, and
an invalid value falls back to its default. ``default.toml`` itself must be valid, so a
problem there raises ``ConfigError``.

Privacy exception: if the file exists but the user's privacy choice cannot be trusted,
``privacy.cloud_translation`` is forced to false with a warning. A missing file is a normal
first run and keeps the default. See ``_check_privacy_intent`` and ADR-0003.

``adapters`` tables are free-form on purpose. Adding an adapter must not change this schema,
so each adapter checks its own table and raises ``PermanentError`` if it is wrong. Their keys
are still all listed, with defaults, in ``default.toml`` (ADR-0005). Shared stage settings,
such as ``capture.fps_max``, reach adapters through ``adapter_settings`` and are not repeated
in adapter tables, so each setting has one knob.
"""

from __future__ import annotations

import copy
import os
import re
import shutil
import sys
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Annotated, Any, Literal

import tomli_w
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
)

from gloss.core.config_migrations import (
    CURRENT_CONFIG_VERSION,
    ConfigMigrationError,
    migrate,
)
from gloss.core.contracts import Rect

_HOTKEY = re.compile(r"^(?:(?:ctrl|alt|shift|win)\+)+(?:[a-z0-9]|f(?:[1-9]|1[0-9]|2[0-4]))$")


def _check_hotkey(value: str) -> str:
    if not _HOTKEY.fullmatch(value):
        raise ValueError("hotkey must be one or more modifiers and a key, joined by +")
    return value


_REGION_LIMIT = 1_000_000  # desktop pixels; far beyond any real monitor layout


def _check_region(value: list[int]) -> list[int]:
    """``[]`` (no saved area) or ``[x, y, w, h]`` in desktop pixels. x and y may be negative,
    because a monitor left of or above the primary one has negative coordinates."""
    if not value:
        return value
    if len(value) != 4:
        raise ValueError("region must be [] or [x, y, w, h]")
    if value[2] <= 0 or value[3] <= 0:
        raise ValueError("region width and height must be above 0")
    if any(abs(v) > _REGION_LIMIT for v in value):
        raise ValueError(f"region values must be within {_REGION_LIMIT} pixels")
    return value


def _check_number(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("must be a number")
    return value


AdapterName = Annotated[str, Field(pattern=r"^[a-z0-9_]+$")]
LangCode = Annotated[str, Field(pattern=r"^[a-z]{2,3}$")]
Hotkey = Annotated[str, AfterValidator(_check_hotkey)]

# Strict on purpose: pydantic would otherwise read true as 1, and "yes" as True.
Number = Annotated[float, BeforeValidator(_check_number), Field(allow_inf_nan=False)]
PositiveInt = Annotated[int, Field(gt=0, strict=True)]
NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
PositiveFloat = Annotated[Number, Field(gt=0)]
NonNegativeFloat = Annotated[Number, Field(ge=0)]
SavedRegion = Annotated[list[Annotated[int, Field(strict=True)]], AfterValidator(_check_region)]
AdapterTables = dict[str, dict[str, Any]]


class ConfigError(Exception):
    """``default.toml`` is missing or invalid. This is a build problem, not a user problem."""


class Section(BaseModel):
    # Extras are kept so they can be reported as warnings, rather than crash the app.
    model_config = ConfigDict(extra="allow", frozen=True)


class SourceChoice(Section):
    source: AdapterName
    fallbacks: list[AdapterName]


class CaptureSources(Section):
    monitor: SourceChoice
    region: SourceChoice
    window: SourceChoice


class CaptureConfig(Section):
    mode: Literal["monitor", "region", "window"]
    fps_max: PositiveFloat
    tile_size: PositiveInt
    black_frame_reads: PositiveInt
    black_frame_max_mean: NonNegativeFloat
    region: SavedRegion
    sources: CaptureSources
    adapters: AdapterTables

    @property
    def active(self) -> SourceChoice:
        return getattr(self.sources, self.mode)

    @property
    def saved_region(self) -> Rect | None:
        """The last chosen area, or None. Pass it to ``services.region.restore_region`` first,
        because the monitors may have changed since it was saved."""
        return Rect(*self.region) if self.region else None


class OCRConfig(Section):
    engine: AdapterName
    fallbacks: list[AdapterName]
    low_quality_engine: AdapterName
    profile: Literal["fast", "accurate"]
    languages: list[LangCode] = Field(min_length=1)
    min_confidence: Number = Field(ge=0, le=1)
    stable_reads: PositiveInt
    dedupe_window_s: PositiveFloat
    dedupe_max_lines: PositiveInt
    adapters: AdapterTables


class ModelPrice(Section):
    """US dollars per million tokens."""

    input: NonNegativeFloat
    output: NonNegativeFloat
    cache_read: NonNegativeFloat
    cache_write: NonNegativeFloat


class BudgetConfig(Section):
    monthly_usd: PositiveFloat
    hourly_usd: PositiveFloat
    warn_at: Number = Field(gt=0, le=1)


class TranslateConfig(Section):
    provider: AdapterName
    fallbacks: list[AdapterName]
    target_lang: LangCode
    batch_max_lines: PositiveInt
    batch_max_wait_ms: NonNegativeInt
    context_lines: NonNegativeInt
    redact_digit_runs: PositiveInt
    budget: BudgetConfig
    prices: dict[str, ModelPrice] = Field(min_length=1)
    adapters: AdapterTables

    def price_for(self, model: str) -> ModelPrice:
        """Return a model's price. An unlisted model is charged at the highest listed price."""
        if model in self.prices:
            return self.prices[model]
        return max(self.prices.values(), key=lambda p: (p.output, p.input))


class RenderConfig(Section):
    mode: Literal["panel", "overlay", "both"]
    font_size: PositiveInt
    adapters: AdapterTables

    @property
    def renderers(self) -> tuple[str, ...]:
        return ("panel", "overlay") if self.mode == "both" else (self.mode,)


class HotkeyConfig(Section):
    toggle: Hotkey
    select_region: Hotkey
    pause: Hotkey


class ReliabilityConfig(Section):
    capture_timeout_ms: PositiveInt
    ocr_timeout_ms: PositiveInt
    translate_timeout_ms: PositiveInt
    render_timeout_ms: PositiveInt
    queue_size: PositiveInt
    retry_max: NonNegativeInt
    retry_base_delay_ms: PositiveInt
    retry_max_delay_ms: PositiveInt
    breaker_failures: PositiveInt
    breaker_cooldown_s: PositiveFloat


class PrivacyConfig(Section):
    cloud_translation: StrictBool
    image_mode: StrictBool
    debug_logging: StrictBool


class StorageConfig(Section):
    database: str = Field(min_length=1)
    glossary_path: str = Field(min_length=1)
    cache_max_entries: PositiveInt


class GlossConfig(Section):
    config_version: PositiveInt
    capture: CaptureConfig
    ocr: OCRConfig
    translate: TranslateConfig
    render: RenderConfig
    hotkeys: HotkeyConfig
    reliability: ReliabilityConfig
    privacy: PrivacyConfig
    storage: StorageConfig

    def adapter_settings(self, port: str, name: str) -> dict[str, Any]:
        """Return the plain settings for an adapter's ``AdapterContext``.

        Shared stage settings come first, so an adapter's own table can override them.
        """
        shared: dict[str, Any]
        if port == "capture":
            shared, tables = {"fps_max": self.capture.fps_max}, self.capture.adapters
        elif port == "ocr":
            shared = {"languages": list(self.ocr.languages), "profile": self.ocr.profile}
            shared["min_confidence"] = self.ocr.min_confidence
            tables = self.ocr.adapters
        elif port == "translate":
            shared = {"timeout_ms": self.reliability.translate_timeout_ms}
            tables = self.translate.adapters
        elif port == "render":
            shared, tables = {"font_size": self.render.font_size}, self.render.adapters
        else:
            raise ValueError(f"Unknown port '{port}'")
        return {**shared, **copy.deepcopy(tables.get(name, {}))}


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    config: GlossConfig
    warnings: tuple[str, ...]


def default_config_path() -> Path:
    bundle = getattr(sys, "_MEIPASS", None)  # Set inside a PyInstaller build.
    root = Path(bundle) if bundle else Path(__file__).resolve().parents[3]
    return root / "config" / "default.toml"


def user_config_path() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Gloss" / "config.toml"


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _unknown_keys(model: BaseModel, prefix: str = "") -> list[str]:
    found = [f"{prefix}{key}" for key in (model.model_extra or {})]
    for name in type(model).model_fields:
        value = getattr(model, name)
        if isinstance(value, BaseModel):
            found += _unknown_keys(value, f"{prefix}{name}.")
        elif isinstance(value, dict):  # Tables of models, such as translate.prices.
            for key, item in value.items():
                if isinstance(item, BaseModel):
                    found += _unknown_keys(item, f"{prefix}{name}.{key}.")
    return found


def _cloud_off(user: dict[str, Any], reason: str, warnings: list[str]) -> None:
    """Force cloud translation off in the user's layer. Privacy intent was unclear."""
    if not isinstance(user.get("privacy"), dict):
        user["privacy"] = {}
    user["privacy"]["cloud_translation"] = False
    warnings.append(f"Cloud translation is OFF (privacy.cloud_translation), because {reason}")


def _check_privacy_intent(user: dict[str, Any], warnings: list[str]) -> None:
    """Turn cloud translation off if the file shows privacy settings it cannot be sure of."""
    privacy = user.get("privacy")
    if "privacy" in user and not isinstance(privacy, dict):
        _cloud_off(user, "'privacy' in your config file is not a table.", warnings)
        return
    near_misses = [k for k in user if k != "privacy" and _resembles_privacy(k)]
    if near_misses:
        _cloud_off(user, f"your config file has '{near_misses[0]}', not 'privacy'.", warnings)
        return
    misplaced = _misplaced_cloud_key({k: v for k, v in user.items() if k != "privacy"})
    if misplaced:
        _cloud_off(user, f"'{misplaced}' is outside the 'privacy' table.", warnings)
        return
    unknown = sorted(set(privacy or {}) - set(PrivacyConfig.model_fields))
    if unknown:
        _cloud_off(user, f"'privacy.{unknown[0]}' is not a known setting.", warnings)


def _resembles_privacy(key: str) -> bool:
    name = key.lower().replace("-", "_")
    return "priv" in name or SequenceMatcher(None, name, "privacy").ratio() >= 0.7


def _misplaced_cloud_key(node: Any, path: str = "") -> str | None:
    """Return the dotted path of the first key that reads as ``cloud_translation``."""
    if isinstance(node, list):
        children = [(str(index), item) for index, item in enumerate(node)]
    elif isinstance(node, dict):
        children = list(node.items())
    else:
        return None
    for key, value in children:
        child_path = f"{path}.{key}" if path else key
        if key.lower().replace("-", "_") == "cloud_translation":
            return child_path
        found = _misplaced_cloud_key(value, child_path)
        if found:
            return found
    return None


def _remove_key(data: dict[str, Any], path: tuple[str | int, ...]) -> bool:
    """Delete the user's value that caused an error. Return True if one was removed.

    The error can point at a key missing from a partial user table. That table is then
    removed whole, since only the user's layer is ever touched here.
    """
    node: Any = data
    parent: dict[str, Any] | None = None
    parent_key: Any = None
    for part in path:
        if not isinstance(node, dict) or part not in node:
            break
        parent, parent_key, node = node, part, node[part]
    if parent is None:
        return False
    del parent[parent_key]
    return True


def _write_toml_atomic(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            tomli_w.dump(dict(data), handle)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _backup(path: Path, label: str) -> Path:
    candidate = path.with_name(f"{path.name}.{label}.bak")
    number = 1
    while candidate.exists():
        number += 1
        candidate = path.with_name(f"{path.name}.{label}.{number}.bak")
    shutil.copy2(path, candidate)
    return candidate


def _read_defaults(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            defaults = tomllib.load(handle)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot read default config at {path}: {exc}") from exc
    if defaults.get("config_version") != CURRENT_CONFIG_VERSION:
        raise ConfigError(f"{path} must have config_version = {CURRENT_CONFIG_VERSION}")
    return defaults


def _read_user(path: Path, warnings: list[str]) -> dict[str, Any]:
    """Read the user file and upgrade it, keeping a backup.

    A missing file gives an empty layer. An unusable file is ignored and turns cloud
    translation off, because it may be what held the user's choice.
    """
    user: dict[str, Any] = {}
    try:
        if not path.exists():
            return user
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        warnings.append(f"Ignored your config file, because it could not be read: {exc}")
        _cloud_off(user, "your config file could not be read.", warnings)
        return user

    try:
        result = migrate(raw)
    except ConfigMigrationError as exc:
        warnings.append(f"Ignored your config file: {exc}")
        _cloud_off(user, "your config file could not be upgraded.", warnings)
        return user

    if result.from_version > CURRENT_CONFIG_VERSION:
        warnings.append(
            f"Your config file is version {result.from_version}, newer than this app "
            f"(version {CURRENT_CONFIG_VERSION}). Settings this app does not know are ignored."
        )
    elif result.changed:
        try:
            backup = _backup(path, f"v{result.from_version}")
            _write_toml_atomic(path, result.data)
            warnings.append(f"Your config file was upgraded. Backup: {backup}")
        except OSError as exc:
            warnings.append(f"Your config file could not be upgraded on disk: {exc}")
    result.data.pop("config_version", None)
    return result.data


def _validate(
    defaults: dict[str, Any], user: dict[str, Any], warnings: list[str], defaults_path: Path
) -> GlossConfig:
    while True:
        try:
            return GlossConfig.model_validate(_deep_merge(defaults, user))
        except ValidationError as exc:
            # One at a time: removing a table can also clear its other errors.
            error = exc.errors()[0]
            if error["loc"] == ("privacy", "cloud_translation") and "cloud_translation" in (
                user.get("privacy") or {}
            ):
                reason = f"its value must be true or false: {error['msg']}"
                _cloud_off(user, reason, warnings)
                continue
            if not _remove_key(user, error["loc"]):
                raise ConfigError(f"Default config {defaults_path} is invalid: {exc}") from exc
            key = ".".join(str(part) for part in error["loc"])
            warnings.append(f"Using the default for '{key}': {error['msg']}")


def load_config(
    *, defaults_path: Path | None = None, user_path: Path | None = None
) -> LoadedConfig:
    """Load settings. A problem with the user's file only produces warnings."""
    defaults_file = defaults_path if defaults_path is not None else default_config_path()
    user_file = user_path if user_path is not None else user_config_path()

    warnings: list[str] = []
    defaults = _read_defaults(defaults_file)
    user = _read_user(user_file, warnings)
    _check_privacy_intent(user, warnings)
    config = _validate(defaults, user, warnings, defaults_file)
    warnings += [f"Unknown setting ignored: {key}" for key in _unknown_keys(config)]
    return LoadedConfig(config, tuple(warnings))


def update_user_config(
    changes: Mapping[str, Any],
    *,
    defaults_path: Path | None = None,
    user_path: Path | None = None,
) -> None:
    """Merge ``changes`` into the user's file, for the settings window.

    An older file is upgraded as part of the save. Raises ValidationError if the result would
    be invalid, ConfigMigrationError if the file is newer than this app or cannot be upgraded,
    or the read error if it is unreadable. In all of these the file is left untouched.
    Comments in the user's file are not preserved, so a backup is kept first.
    """
    defaults_file = defaults_path if defaults_path is not None else default_config_path()
    user_file = user_path if user_path is not None else user_config_path()

    current: dict[str, Any] = {}
    if user_file.exists():
        with user_file.open("rb") as handle:
            current = tomllib.load(handle)
    result = migrate(current)
    if result.from_version > CURRENT_CONFIG_VERSION:
        raise ConfigMigrationError(
            f"Your config file is version {result.from_version}, newer than this app "
            f"(version {CURRENT_CONFIG_VERSION}), so it was not changed."
        )
    updated = _deep_merge(result.data, changes)
    updated["config_version"] = CURRENT_CONFIG_VERSION
    GlossConfig.model_validate(_deep_merge(_read_defaults(defaults_file), updated))
    if user_file.exists():
        _backup(user_file, f"v{result.from_version}" if result.changed else "settings")
    _write_toml_atomic(user_file, updated)
