# Gloss — Product & Architecture Spec (v0.2)

Updated 2026-10-31 from accepted ADR-0001 and contract ADRs 0002-0004. Sections marked
**[R]** were hypotheses for Phase 1; ADR-0001 records the outcomes. Any further change must
be recorded as an ADR in `docs/adr/`.

---

## 1. Goal

> Input: whatever is on my monitor. Output: English, in real time, without me doing anything.

The user spends hours in Chinese-language apps (e.g. the "东东电竞" gaming-companion
platform: voice-room chat, order cards, profiles, menus). Today they screenshot each
screen and paste it into an AI chat to get translations. Gloss removes that loop.

### Primary scenarios
1. **Live chat room.** New chat messages scroll in. Each new line appears translated in
   a side panel within ~1–2 s, in order, with the sender name kept.
2. **Static UI / menus / profiles.** User opens a page or dropdown. Translated labels
   appear either in the panel or as an overlay on top of the original text.
3. **Order cards / pop-ups.** A card appears briefly. Its text is translated before it
   disappears, and kept in the panel history so it can be reread.

### Source languages (MVP)
Gloss reads Chinese, English and Japanese (`zh`, `en`, `ja`), detected automatically.
Japanese is unverified on real screenshots. Korean, Thai and Russian are out of scope for
the MVP.

### Non-goals (MVP)
- Audio/voice translation. Text hooking/injection into other processes. Mac/Linux.
- Auto-replying. (A separate "reply composer" is a Phase-6 nice-to-have, see §9.)

---

## 2. Runtime architecture

```
┌──────────────┐  Frame   ┌───────────────┐ changed   ┌────────────┐ TextBlocks ┌──────────────┐
│CaptureSource │────────▶ │ChangeDetector │──regions─▶│ OCREngine  │──────────▶ │ TextTracker  │
│ (mss / WGC)  │          │ (tile hashing)│           │(RapidOCR…) │            │ dedupe+stable│
└──────────────┘          └───────────────┘           └────────────┘            └──────┬───────┘
                                                                                  new/changed lines
┌──────────────┐  Results ┌───────────────────────────────────────────────┐            │
│ Renderer(s)  │◀──────── │ TranslationService                            │◀───────────┘
│ panel/overlay│          │ redact → cache → glossary+context → primary   │
└──────────────┘          │ → fallback chain → cache write                │
                          └───────────────────────────────────────────────┘
```

- Each stage runs on its own worker with a bounded queue; slow stages drop stale work
  rather than building a backlog (latest frame wins).
- The `Pipeline` only knows **ports** (Protocols). Adapters are chosen by config via the
  registry.
- Every stage reports `Health` (ok / degraded / down + reason) to a status model the UI
  shows as a small coloured dot in the tray and panel.

### 2.1 Stage notes (engineering hints, confirm in research)
- **Capture** [R]: `mss` (GDI) is simple and stable. Some hardware-accelerated windows
  capture black with GDI, and HDR can wash out. Windows Graphics Capture or DXGI Desktop
  Duplication may be needed. Support: whole monitor, a user-drawn region, and
  "follow window" (track a window's rect by HWND/title).
- **Overlay must not be captured.** Call `SetWindowDisplayAffinity(hwnd,
  WDA_EXCLUDEFROMCAPTURE)` on every Gloss window (Win10 2004+). Otherwise the app
  OCRs its own translations and loops. Add a test for it.
- **DPI:** the process must be per-monitor-DPI-aware (V2). All bboxes are in *physical*
  pixels in contracts. Convert to logical coordinates only inside renderers.
- **Change detection:** downscale to grayscale and hash fixed tiles (e.g. 64×64). Only
  dirty tiles (merged into rectangles) go to OCR. On a static screen, idle CPU should
  be near zero. A scrolled chat marks every tile dirty, so Phase 3 adds scroll-shift
  detection and sends only the newly exposed band to OCR.
- **OCR** [R]: RapidOCR (PP-OCR models in ONNX) handles Chinese+English by default and
  has Japanese/Korean models. Windows.Media.Ocr is fast and has no install cost, but
  needs Windows language packs. Compare accuracy on stylised game UI text, small fonts
  and coloured chat bubbles.
- **TextTracker:** OCR jitters (the same line reads slightly differently between frames).
  - Normalise text (NFKC, whitespace, punctuation width).
  - Match lines across frames by fuzzy text similarity + bbox IoU.
  - Only translate a line once it has been **stable for 2 consecutive reads**
    (configurable). Unchanged pixels on the next frame count as a read, with no second OCR.
  - Dedupe by normalised-text hash inside a time window, so a scrolled line isn't
    re-translated.
  - Group lines into messages (username + timestamp + bubble) when layout allows. [R]
- **Translation:**
  - Chain: `anthropic`, then the optional offline `quickmt` pack if installed and listed,
    then `null` (shows the original text).
  - Batch new lines (≤ N lines or ≤ T ms) into one request.
  - Send a JSON list of `{id, text}` and require JSON back (structured output / tool
    use), so lines can't get mismatched.
  - Include the last K translated lines as context, and the glossary (§6).
  - Use prompt caching for the static system prompt + glossary. [R: confirm the current
    API feature and pricing at docs.claude.com]
- **Cache:** SQLite keyed by `(provider, model, target_lang, glossary_version,
  normalised_text)`. A cache hit costs 0 API calls.
- **Renderers:**
  - **Panel** (default): an always-on-top, resizable, scrolling list showing original
    (small, grey) + English (large), newest at the bottom, with copy-on-click.
  - **Overlay** (optional): click-through, transparent, draws the English over or just
    below the original bbox. Click-through = `WS_EX_LAYERED | WS_EX_TRANSPARENT`.
    Note that exclusive-fullscreen games can't be overlaid; tell the user to use
    borderless windowed.

---

## 3. Contracts

The code in `src/gloss/core/contracts/` is the source of truth, and ADR-0002 records the
reasoning. This is the short form. All coordinates are physical pixels.

```python
# version.py
CONTRACT_VERSION = "1.0.0"

# models.py — frozen dataclasses; sequences are tuples
Rect(x, y, w, h)
MonitorInfo(monitor_id, bounds, dpi_scale, is_primary=False)
CaptureTarget(mode, monitor_id=None, region=None, hwnd=None, window_title=None)
FrameMeta(source_id, region, timestamp, dpi_scale)
Frame(image, source_id, region, timestamp, dpi_scale, fresh=True)
TextBlock(text, bbox, confidence: float | None, lang, line_id=None, low_quality=False,
          needs_translation=True, group_id=None)
GlossaryEntry(term, translation, note=None); Glossary(version="0", entries=())
ContextLine(source_text, translated_text)
TranslationItem(line_id, text, low_quality=False)
TranslationRequest(items, source_lang, target_lang, context=(), glossary=Glossary())
TranslationResult(line_id, source_text, text, provider, model, cached, latency_ms,
                  status="ok", failure=None, rough=False)
TokenUsage(provider, model, input_tokens, output_tokens, cache_read_tokens=0,
           cache_write_tokens=0)
TranslationResponse(results, usage=None)
RenderItem(result, bbox=None, low_quality=False, group_id=None)
Health(state: "ok" | "degraded" | "down", detail="")
CacheKey(provider, model, target_lang, glossary_version, normalised_text)

# errors.py
AdapterUnavailable  # can't start (missing dll, no key, no language pack) → use fallback
TransientError      # timeout, 429, network blip → retry with backoff, then fallback
PermanentError      # bad config / invalid key → disable adapter, surface to user, use fallback

# ports.py — typing.Protocol
CaptureSource: name; list_monitors(); open(target); grab() -> Frame | None; close(); health()
OCREngine:     name; languages; open(); recognize(frame, regions) -> Sequence[TextBlock];
               close(); health()
Translator:    name; open(); translate(request) -> TranslationResponse; close(); health()
Renderer:      name; open(); show(items, frame_meta); clear(); set_visible(bool);
               set_status(health, banner=""); close(); health()
SecretStore:      get(name); set(name, value); delete(name)
TranslationCache: get(key); put(key, text); close()
SpendLedger:      record(usage, cost_usd, timestamp); total_since(timestamp); close()
AdapterContext(settings={}, secrets: SecretStore | None = None)
```

Rules:
- `TextBlock.bbox` and OCR `regions` use frame coordinates. `Frame.region`, monitor bounds and
  `RenderItem.bbox` use desktop coordinates.
- `confidence=None` means unknown. It is neither 0 nor 1, and it skips `min_confidence`.
- A translator makes one provider call. One bad line comes back with `status="failed"`, and
  does not count toward the circuit breaker. The TranslationService handles retries.
- Adapters declare `REQUIRES_CONTRACT = ">=1.0,<2.0"`. The registry refuses to load
  incompatible adapters and logs why.
- New optional fields must have defaults (minor bump). Breaking changes need a major
  bump + ADR + user approval.
- Contract tests in `tests/contract/test_<port>.py` are parametrised over every
  registered adapter for that port. A new adapter is "done" only when it passes them.

---

## 4. Config (`config/default.toml` + user override in `%APPDATA%/Gloss/config.toml`)

`config/default.toml` is the full list of settings, and ADR-0003 records the reasoning. The
main shape:

```toml
config_version = 1

[capture]
mode = "region"           # monitor | region | window
fps_max = 4

[capture.sources.monitor]
source = "mss"
fallbacks = ["wgc"]

[capture.sources.region]
source = "mss"
fallbacks = ["wgc"]

[capture.sources.window]
source = "wgc"
fallbacks = ["mss"]

[ocr]
engine = "rapidocr"
fallbacks = []            # empty on purpose: no automatic low-quality OCR
profile = "fast"          # fast | accurate
languages = ["zh", "en", "ja"]
min_confidence = 0.5
stable_reads = 2

[translate]
provider = "anthropic"
fallbacks = ["null"]      # add "quickmt" before "null" if the offline pack is installed
target_lang = "en"
batch_max_lines = 20
batch_max_wait_ms = 200
context_lines = 10
redact_digit_runs = 8     # redact sequences of ≥ 8 digits before sending

[translate.budget]        # replaces max_requests_per_hour
monthly_usd = 10.0
hourly_usd = 0.5
warn_at = 0.8

[translate.prices.claude-haiku-5-5]   # US dollars per million tokens
input = 0.10
output = 0.50
cache_read = 0.01
cache_write = 0.125

[translate.adapters.anthropic]
model = "claude-haiku-5-5"   # verify current IDs at runtime; never hard-code in code
thinking = "disabled"
effort = "low"

[render]
mode = "panel"            # panel | overlay | both
font_size = 14

[hotkeys]
toggle = "ctrl+alt+t"
select_region = "ctrl+alt+r"
pause = "ctrl+alt+p"
```

- `adapters` tables are free-form and passed to that adapter, which checks them itself.
- Validate with pydantic. Unknown keys → warning, not crash. An invalid user value → warning
  and the default for that key.
- Privacy exception: if the user's file exists but their privacy choice can't be trusted
  (unreadable, can't be migrated, an invalid `cloud_translation`, a malformed `[privacy]`
  table or an unknown key in it), `privacy.cloud_translation` is forced to false with a
  warning. A missing file keeps the default. When it is false, only translators declaring
  `LOCAL_ONLY = True` stay in the chain (ADR-0002, ADR-0003).
- `config_version` migrations live in `core/config_migrations.py`. The user's file is
  never overwritten, only migrated with a backup.

---

## 5. Reliability & "updates don't break anything"

| Risk | Safeguard |
|---|---|
| Library update changes behaviour | Pinned `uv.lock`; `update-steward` bumps one dep at a time; full + golden + bench suites must match baseline |
| Model deprecated / renamed | Model ID in config; startup check against the models API; clear warning + automatic fallback provider |
| API outage / 429 | Retry with jittered backoff → circuit breaker (3 fails → fallback for 5 min) → status dot turns amber |
| Runaway spend | Dollar budget guard (monthly and hourly) from API `usage`, a local estimate → stop paid calls, move down the chain, amber. The Anthropic console limit is the only hard stop |
| OCR engine fails to load (missing DLL, language pack) | `AdapterUnavailable` → red status, the exact fix, and a button for manual low-quality mode. The automatic list is empty on purpose (ADR-0001 section 2) |
| One stage hangs | Per-call timeouts; watchdog restarts the worker; the app never freezes the UI thread |
| Contract drift | Frozen contracts (hook), contract tests, version checks in registry |
| Silent quality regression | Golden fixtures (real screenshots + expected text) with accuracy thresholds; latency benchmark baseline (`tests/bench/baseline.json`), >20% regression blocks |
| Config change breaks startup | Versioned config + migrations + validation with defaults |
| Bad release | Git tag per release, keep the previous build, documented rollback in `docs/UPDATING.md` |

---

## 6. Glossary & translation style

- `docs/glossary/zh_gaming.yaml` is user-editable and loaded at startup + on change.
  Each entry is `term`, `en`, optional `note`.
- Translation instructions to the model:
  - Natural, casual English matching the tone. Keep usernames, IDs and numbers unchanged.
  - Keep emoji. Expand platform slang using the glossary.
  - Very short output for UI labels.
  - Never add commentary.
- Optional "explain" mode: hovering or clicking a line shows a one-line note on slang or
  puns (e.g. 520 = "I love you").

---

## 7. Performance & resource targets (MVP, typical 4–8-core CPU, no GPU)

| Metric | Target |
|---|---|
| New chat line → English visible | p50 ≤ 1.5 s, p95 ≤ 3 s |
| Idle CPU (static screen) | < 3 % |
| Active CPU (scrolling chat) | < 25 % of one core avg |
| RAM | < 400 MB |
| Install size | as small as research allows; report it |
| API calls on unchanged screen | 0 |

OCR latency gates per region class, v6 small on 2 threads (ADR-0001 section 3). The 4–8 core
column is advisory until run on such a PC, and all gates are re-based in Phase 4.

| Region class | Lines | p95 gate, this PC | p95 gate, 4–8 core |
|---|---|---|---|
| New-lines band | 1–5 | 250 ms | 500 ms |
| Chat panel or dialog | 6–25 | 500 ms | 1,000 ms |
| Whole dense page (not live) | 26–150 | 2,000 ms | 4,000 ms |

The `fast` profile must meet the RAM target. The `accurate` profile, and the `quickmt` pack
while in use, are documented as exceeding it.

---

## 8. Acceptance criteria (MVP = end of Phase 4)

1. Launch → tray icon. Hotkey toggles. Region select works across monitors with different
   DPI scaling.
2. On the user's real screenshots in `tests/golden/images/` (Chinese chat, order cards,
   menus, profile), OCR meets these thresholds on the `fast` profile, and every line OCR
   returned gets an English translation (this needs an Anthropic key in keyring). Lines OCR
   misses are covered by line recall.

   | Scope | Character accuracy | Line recall | Type |
   |---|---|---|---|
   | Chat | ≥ 97.0% | ≥ 95.0% | Acceptance gate |
   | All images | ≥ 95.0% | ≥ 92.0% | Acceptance gate |
   | Profile | ≥ 95.0% | ≥ 93.0% | Regression floor |
   | Menu | ≥ 93.0% | ≥ 88.0% | Regression floor |
   | Order card | ≥ 93.0% | ≥ 88.0% | Regression floor |

   The `accurate` profile adds menu ≥ 97.0% / 95.0%.
3. Live test on a scrolling chat: no duplicate translations, no missed lines, correct order.
4. Gloss's own windows never appear in captures (automated test).
5. With the network off, the app keeps running, shows "offline", and resumes
   automatically.
6. Changing `ocr.engine` or `translate.provider` in config switches engines without code
   edits. Contract tests pass for every adapter.
7. API key stored via keyring. `grep` of logs finds no key and no screenshot data.
8. `uv run pytest` green, `ruff` clean, `safeguard` PASS, Windows build runs on a clean
   user account.

---

## 9. Later phases (not MVP)
- Reply composer: type English → get Chinese in the platform's casual style → copy to
  clipboard (glossary-aware, tone presets: polite / playful / customer-service).
- Thai as a second target language.
- Per-app profiles (regions, glossary, style) auto-selected by window title.
- Searchable translation history with export.
