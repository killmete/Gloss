"""Upgrades a user's config file from an older ``config_version``.

To change the config format: raise ``CURRENT_CONFIG_VERSION``, add one function to
``MIGRATIONS`` keyed by the version it upgrades from, update ``config/default.toml``, and
write an ADR. A migration takes the raw dict and returns the raw dict for the next version.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

RawConfig = dict[str, Any]
Migration = Callable[[RawConfig], RawConfig]

CURRENT_CONFIG_VERSION = 1

MIGRATIONS: dict[int, Migration] = {}


class ConfigMigrationError(Exception):
    """A config file could not be upgraded."""


@dataclass(frozen=True, slots=True)
class MigrationResult:
    data: RawConfig
    from_version: int
    to_version: int

    @property
    def changed(self) -> bool:
        return self.from_version != self.to_version


def migrate(
    raw: Mapping[str, Any],
    *,
    current: int = CURRENT_CONFIG_VERSION,
    migrations: Mapping[int, Migration] | None = None,
) -> MigrationResult:
    """Upgrade ``raw`` to ``current``, leaving the input untouched.

    A file with no ``config_version`` counts as current, since it may hold only a few
    overrides. A file newer than ``current`` is returned as it is: the caller warns.
    Raises ConfigMigrationError for a bad version, a missing step, or a step that fails.
    """
    steps = MIGRATIONS if migrations is None else migrations
    data = copy.deepcopy(dict(raw))
    start = data.get("config_version", current)
    if not isinstance(start, int) or isinstance(start, bool) or start < 1:
        raise ConfigMigrationError("config_version must be a positive whole number")

    version = start
    while version < current:
        step = steps.get(version)
        if step is None:
            raise ConfigMigrationError(f"No migration from config version {version}")
        try:
            data = step(data)
            data["config_version"] = version + 1
        except Exception as exc:  # A buggy step must not stop the app. Callers keep the file.
            raise ConfigMigrationError(
                f"Upgrading config version {version} failed: {type(exc).__name__}"
            ) from exc
        version += 1
    return MigrationResult(data, start, version)
