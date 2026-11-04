# ADR-0005: Adapter settings live in config, one knob per setting

- Status: **Proposed** 2026-11-04 (Phase 3). No contract change; `config_version` stays 1.
- Code: `config/default.toml`, `src/gloss/core/config.py`, `tests/unit/test_config_adapter_defaults.py`

## Context

The Phase 3 adapters read optional settings with in-code defaults. Rule 5 says nothing tunable is hidden in code. The engineers also left three questions: which `fps_max` wins, which `det_limit_side_len` to use, and where the chosen screen area is remembered.

## Options

1. Validate every adapter key in the pydantic schema.
2. Keep `[*.adapters.*]` free-form (ADR-0003), list every key with its default in `default.toml`, and add a test that the TOML values equal the code defaults.

For `det_limit_side_len`: one value for everything; or 960 for changed-area reads and 1280 for whole-frame reads.

## Decision

- **Option 2.** Each adapter still checks its own table and raises `PermanentError`. The panel and overlay instead fall back to their built-in value, so the window always opens. `test_config_adapter_defaults.py` fails if TOML and code disagree.
- **`capture.fps_max` is the only frame-rate knob.** `adapter_settings("capture", ...)` already passes it to every capture adapter, so the pipeline copies nothing. Adapter tables must not repeat shared keys: `fps_max`, `languages`, `profile`, `min_confidence`, `font_size` and `timeout_ms`. A test enforces this.
- **`first_frame_timeout_ms = 1500`** (the code default; the 1000 in the brief was a different number) is set only in `[capture.adapters.wgc]`. mss never waits.
- **`det_limit_side_len = 960`, one value.** RapidOCR sets it once, when the engine is built; there is no per-call argument. With `det_limit_type = "max"`, crops whose longest side is 960 px or less are never resized. So changed-area reads already work at native size, and the limit only matters for large crops and whole frames. Chat is the acceptance gate and the main use: 960 gives 97.6% against the 97.0% gate, while 1280 gives 97.1%, which is too close to it. 1280 is better overall (98.7% against 96.8%) and on menus (99.1%). A user who mostly reads menus or profiles can set 1280 in their own config.
- **Saved area: `capture.region = []`** means none, otherwise `[x, y, w, h]` as whole desktop pixels. x and y may be negative, w and h must be above 0, and every value is limited to 1,000,000. This is the `Rect` that `services.region.restore_region(saved, monitors)` expects. `GlossConfig.capture.saved_region` returns `Rect | None`. The monitor is not stored, because `region_target` works it out again. `update_user_config({"capture": {"region": [...]}})` saves it, and `[]` clears it. A bad value warns and falls back to none.
- **Adaptive frame rate (2026-11-05, after user feedback).** `capture.fps_max = 12` is the rate while the screen is changing. `idle_fps = 4` is the rate after `idle_after_s = 1.0` seconds without change, and it must not be above `fps_max`. If the user lowers only `fps_max`, `idle_fps` silently follows it down, in memory only. An `idle_fps` the user set above `fps_max` is set to `fps_max`, with a warning. `render.move_threshold_px = 4`: the pipeline does not move a box for a shift smaller than max(this, 25% of the line height). The overlay has `min_font_px 9`, `max_font_px 28`, `max_lines 2` and `clip_to_capture true`, and checks them itself.
- **`storage.cache_max_entries = 50000`** (equal to `cache.DEFAULT_MAX_ENTRIES`). The pipeline passes it to `SqliteTranslationCache(max_entries=...)`.
- **`ocr.adapters.*.skip_translation_langs = []`**. The text tracker already skips `translate.target_lang`, so the OCR key stays empty instead of becoming a second knob for the same thing.
- **`model_dir = ""`** searches the gitignored `models/rapidocr/`, then the PyInstaller bundle's `models/rapidocr/`, then the wheel. Nothing is downloaded.

## Consequences

- Every tunable value is visible in one file, with a comment. A new adapter key also needs a TOML line, or the defaults test fails.
- Bad adapter values are caught when the adapter is built (`PermanentError`), not when config loads, so `load_config` does not warn about them.
- **Phase 4 packaging:** `src/gloss/adapters/translate/prompts/system.txt` and `src/gloss/adapters/ocr/rapidocr_models.json` must be PyInstaller data files at the same package-relative paths, together with `config/default.toml` and the model files under `models/rapidocr/`.

## Revisit when

- RapidOCR adds a per-call detection limit. Then use 960 for changed areas and 1280 for whole frames.
- A larger golden set moves chat at 1280 clearly above 97.0%.
- The schema needs to warn about adapter keys at load time (for example, a settings window that edits them).
