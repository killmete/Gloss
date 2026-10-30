# Gloss — Product & Architecture Spec (v0.1, pre-research)

Sections marked **[R]** are hypotheses the `researcher` must confirm or overturn in
Phase 1. Anything changed after research must be recorded as an ADR in `docs/adr/`.

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
  be near zero.
- **OCR** [R]: RapidOCR (PP-OCR models in ONNX) handles Chinese+English by default and
  has Japanese/Korean models. Windows.Media.Ocr is fast and has no install cost, but
  needs Windows language packs. Compare accuracy on stylised game UI text, small fonts
  and coloured chat bubbles.
- **TextTracker:** OCR jitters (the same line reads slightly differently between frames).
  - Normalise text (NFKC, whitespace, punctuation width).
  - Match lines across frames by fuzzy text similarity + bbox IoU.
  - Only translate a line once it has been **stable for 2 consecutive reads**
    (configurable).
  - Dedupe by normalised-text hash inside a time window, so a scrolled line isn't
    re-translated.
  - Group lines into messages (username + timestamp + bubble) when layout allows. [R]
- **Translation:**
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

## 3. Contracts (draft for the architect to finalise in Phase 2)

```python
# src/gloss/core/contracts/version.py
CONTRACT_VERSION = "1.0.0"

# models.py — frozen dataclasses; all coordinates in physical pixels
Rect(x:int, y:int, w:int, h:int)
Frame(image: np.ndarray[H,W,3] uint8 RGB, source_id: str, region: Rect,
      timestamp: float, dpi_scale: float)
TextBlock(text: str, bbox: Rect, confidence: float, lang: str | None,
          line_id: str | None = None)
TranslationRequest(items: list[tuple[str, str]],      # (line_id, text)
                   source_lang: str | None, target_lang: str,
                   context: list[str], glossary: Glossary)
TranslationResult(line_id: str, source_text: str, text: str, provider: str,
                  model: str | None, cached: bool, latency_ms: int)
Health(state: Literal["ok","degraded","down"], detail: str = "")

# errors.py
AdapterUnavailable  # can't start (missing dll, no key, no language pack) → use fallback
TransientError      # timeout, 429, network blip → retry with backoff, then fallback
PermanentError      # bad config / unsupported → disable adapter, surface to user

# ports.py — typing.Protocol
CaptureSource: name; open(cfg); grab() -> Frame | None; close(); health()
OCREngine:     name; languages; recognize(frame, regions: list[Rect] | None) -> list[TextBlock]; health()
Translator:    name; translate(req) -> list[TranslationResult]; health()
Renderer:      name; show(results, frame_meta); clear(); set_visible(bool); health()
```

Rules:
- Adapters declare `REQUIRES_CONTRACT = ">=1.0,<2.0"`. The registry refuses to load
  incompatible adapters and logs why.
- New optional fields must have defaults (minor bump). Breaking changes need a major
  bump + ADR + user approval.
- Contract tests in `tests/contract/test_<port>.py` are parametrised over every
  registered adapter for that port. A new adapter is "done" only when it passes them.

---

## 4. Config (`config/default.toml` + user override in `%APPDATA%/Gloss/config.toml`)

```toml
config_version = 1

[capture]
source = "mss"            # registry name
fallbacks = ["wgc"]
mode = "region"           # monitor | region | window
fps_max = 4

[ocr]
engine = "rapidocr"
fallbacks = ["windows"]
languages = ["zh", "en", "ja", "ko"]
min_confidence = 0.6
stable_reads = 2

[translate]
provider = "anthropic"
fallbacks = ["deepl", "null"]
target_lang = "en"
model = "claude-haiku-5-5"   # verify current IDs at runtime; never hard-code in code
batch_max_lines = 20
batch_max_wait_ms = 400
context_lines = 10
max_requests_per_hour = 600
redact_digit_runs = 8         # redact sequences of ≥ 8 digits before sending

[render]
mode = "panel"            # panel | overlay | both
font_size = 14

[hotkeys]
toggle = "ctrl+alt+t"
select_region = "ctrl+alt+r"
pause = "ctrl+alt+p"
```

- Validate with pydantic. Unknown keys → warning, not crash.
- `config_version` migrations live in `core/config_migrations.py`. The user's file is
  never overwritten, only migrated with a backup.

---

## 5. Reliability & "updates don't break anything"

| Risk | Safeguard |
|---|---|
| Library update changes behaviour | Pinned `uv.lock`; `update-steward` bumps one dep at a time; full + golden + bench suites must match baseline |
| Model deprecated / renamed | Model ID in config; startup check against the models API; clear warning + automatic fallback provider |
| API outage / 429 | Retry with jittered backoff → circuit breaker (3 fails → fallback for 5 min) → status dot turns amber |
| OCR engine fails to load (missing DLL, language pack) | `AdapterUnavailable` → next engine in fallback list; message in settings |
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

---

## 8. Acceptance criteria (MVP = end of Phase 4)

1. Launch → tray icon. Hotkey toggles. Region select works across monitors with different
   DPI scaling.
2. On the user's real screenshots in `tests/golden/images/` (Chinese chat, order cards,
   menus, profile), OCR text accuracy ≥ the threshold agreed in Phase 1, and every line
   gets an English translation.
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
