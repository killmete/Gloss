# ADR-0003: Config layering and adapter registry

- Status: **Proposed**, pending user approval at Checkpoint 2
- Date: 2026-10-31
- Code: `src/gloss/core/config.py`, `config_migrations.py`, `registry.py`, `config/default.toml`

## Context

CLAUDE.md rules 4 and 5 require engine swaps by config and no hard-coded limits or IDs. Two questions were open: where defaults live, and how adapter-specific settings stay validated without tying the schema to each adapter.

## Options

**Defaults:** in pydantic field defaults, in `default.toml` alone, or in both.

**Adapter settings:** one typed schema per adapter inside `config.py`, or free-form tables that each adapter checks.

**Finding adapters:** a module-name convention from config names, an explicit list, or importing everything under `gloss.adapters`.

## Decision

- **`default.toml` is the only source of defaults.** The schema has required fields and rules, with no default values, so a value cannot exist in two places.
- **Layers:** the user's file overrides `default.toml` key by key. Tables merge, and lists replace.
- **Fail soft.** An unknown key gives a warning. An invalid user value gives a warning and that key's default. An unreadable user file is ignored with a warning. Only a broken `default.toml` raises, because that is a build fault.
- **One exception, for privacy (CLAUDE.md rule 7):** `privacy.cloud_translation` is forced to false, not its default of true, with a "Cloud translation is OFF" warning, whenever the user's file exists but their choice cannot be trusted. An unclear answer must not send text off the PC. This covers:
  - an invalid value for the key;
  - a file that cannot be read, including a permission error, a TOML syntax error or non-UTF-8 content;
  - a file that cannot be migrated, including a bad `config_version`;
  - `privacy` not being a table;
  - a top-level key resembling `privacy`, such as `[Privacy]` or `[privcy]` (difflib ratio at least 0.7, or containing `priv`);
  - a key reading as `cloud_translation` anywhere outside `[privacy]`, at any depth, ignoring case and treating `-` as `_`;
  - an unknown key under `[privacy]`, such as a misspelling. This wins even over a valid `cloud_translation = true` beside it.

  A missing file is a normal first run, so it keeps the default of true. `image_mode` and `debug_logging` already default to the safe side, so they need no exception.
- **Adapter tables are free-form:** `[translate.adapters.anthropic]` and similar. `adapter_settings()` passes them as plain data, with shared stage settings first. Each adapter checks its own table in `open()` and raises `PermanentError` if it is wrong. This moves `translate.model` from SPEC's draft into the `anthropic` table.
- **Migrations:** numbered steps in `config_migrations.py`. An upgrade keeps a `.bak` copy and writes atomically. `tomli-w` writes TOML, and it does not preserve comments. A file newer than the app is read with a warning and is not rewritten. The settings window refuses to save over one, and upgrades an older file as part of the save.
- **Registry:** adapters self-register with a decorator. `discover()` imports every module under `gloss.adapters`, and a module that fails to import is recorded, not fatal. Agent briefs already name files such as `rapidocr_engine.py`, so no name-to-file convention is imposed.
- **Refusal checks:** `name` matches, `REQUIRES_CONTRACT` is declared and satisfied, the port's methods exist, and the name is not taken.
- **`ALLOW_AUTO_FALLBACK = False`** on an adapter makes `resolve_chain()` drop it from any fallback list. Windows OCR sets this, so a config edit cannot make it an automatic fallback, which enforces ADR-0001. It stays usable as an explicit primary.
- `[reliability]` and `[privacy]` sections hold timeouts, retry and breaker limits, the cloud switch, image mode and debug logging. The new numbers are starting values, not measurements.

## Consequences

- A typo inside an adapter table is caught when that adapter opens, not at load.
- The settings window loses comments in the user's file, so `update_user_config` keeps a backup first.
- A wheel install has no `config/default.toml`. It works from the repo and needs adding to the PyInstaller data files in Phase 4, with `storage.glossary_path` handled the same way.
- `pkgutil` discovery needs hidden imports in a frozen build. Phase 4 must list every adapter module and test it.
- Setting `ocr.engine = "windows"` in the file is persistent, while ADR-0001 asks for per-session low-quality mode. The banner still shows because every block is marked `low_quality`.

## Revisit when

- Adapter-table errors cause real confusion. Adapters could then publish a schema.
- A second target language needs per-language prices or glossaries.
