# Gloss — real-time screen OCR translator

Windows desktop app that watches the user's monitor (or a region of it), OCRs any
foreign-language text that appears, and shows an English translation in near real time.
Primary use: Chinese gaming/companion platforms (chat rooms, order cards, profiles),
but source language is auto-detected (zh, ja, ko, th, ru, ...).

The full product spec, architecture and acceptance criteria are in `docs/SPEC.md`.
Read it before doing any design or implementation work.

## The user
- Native Thai, near-native English, a little Japanese. Does not read Chinese.
- Windows PC, plays games, wants something lightweight that "just keeps working".
- Not a professional developer: explain decisions in plain English, keep questions few
  and concrete, and give copy-pasteable commands for anything they must run themselves.

## Non-negotiable engineering rules
1. **Ports & adapters.** Everything external (screen capture, OCR, translation APIs, UI
   rendering, storage) sits behind an interface in `src/gloss/core/contracts/`.
   Modules talk only through those contracts. No adapter imports another adapter.
2. **Contracts are frozen** once the user approves Checkpoint 2. You then create the empty
   marker `.claude/CONTRACTS_FROZEN`, and from then on a hook blocks edits to
   `src/gloss/core/contracts/` unless the user creates `.claude/CONTRACT_UNLOCK`.
   Changing a contract needs: an ADR in `docs/adr/`, a version bump in
   `contracts/version.py`, migration of every adapter, and the user's OK.
   Additive, optional-with-default fields = minor bump. Rename/remove = major bump.
3. **Golden expectations are protected** the same way. After the user confirms them, you
   create `.claude/GOLDEN_FROZEN`. `tests/golden/expected/` then only changes while the user's
   `.claude/GOLDEN_UNLOCK` exists. Never "fix" a failing test by editing its expected output;
   fix the code or raise it with the user.
   Hooks, settings, agent definitions and unlock files are never editable by Claude. If one
   needs changing, explain why and ask the user (agents: user creates `.claude/TEAM_UNLOCK`).
4. **Swapping an engine is a config change, never a code change.** Adapters register
   themselves in the registry; `config/default.toml` picks primary + fallbacks.
5. **Nothing external is hard-coded:** model IDs, API base URLs, languages, hotkeys,
   intervals and limits all live in config with validated defaults.
6. **Fail soft.** An adapter exception must never crash the app: timeouts, circuit
   breaker, fallback chain, and a visible status indicator.
7. **Privacy by default.** OCR runs locally. Only extracted text (not screenshots) is
   sent to a translation API unless the user turns on an image mode. Long digit runs
   (card/phone numbers) are redacted before sending. Never log screenshots, API keys,
   or full translated chat logs unless debug logging is explicitly on.
8. **Secrets** live in Windows Credential Manager via `keyring`. Never read, write or
   print `.env` files or keys. Never commit secrets.
9. **Dependencies are pinned** (`uv.lock`). Change them only with `uv add/remove/lock`,
   one dependency per change, following `docs/UPDATING.md`.
10. **Tests first for contracts.** Every adapter must pass the contract test suite for
    its port (`tests/contract/`). A phase is not done until `uv run pytest` is green and
    the `safeguard` agent returns PASS.

## Stack (default — may be revised only via ADR after Phase 1 research + user approval)
- Python 3.12, `uv` for env/lockfile, `ruff` for lint/format, `pytest`.
- PySide6 for tray, settings window, side panel and click-through overlay.
- Capture: `mss` baseline; Windows Graphics Capture / DXGI adapter if research shows a need.
- OCR: RapidOCR (ONNX Runtime, CPU) primary; Windows.Media.Ocr adapter as alternative.
- Translation: Anthropic API (fast Haiku-class model by default) with glossary + chat
  context; a second provider (e.g. DeepL) as fallback; `null` translator for offline.
- Cache: SQLite (text hash → translation). Config: TOML validated with pydantic.
- Packaging: one-folder or one-file Windows build (PyInstaller or Nuitka, per research).

## Repository layout
```
src/gloss/core/contracts/   FROZEN interfaces + data models + version
src/gloss/core/             pipeline, registry, config, events, health
src/gloss/adapters/{capture,ocr,translate,render}/
src/gloss/services/         change detection, text tracker, cache, glossary, redaction, secrets
src/gloss/ui/               tray, settings, hotkeys
tests/{contract,unit,golden,bench}/
docs/{SPEC.md,adr/,research/,glossary/,UPDATING.md}
config/default.toml
```

## Agent team (definitions in `.claude/agents/`)
| Agent | Owns |
|---|---|
| researcher | Prior art, library comparisons, benchmarks → `docs/research/` |
| architect | Contracts, ADRs, module boundaries, config schema |
| capture-engineer | `adapters/capture/`, change detection, DPI, multi-monitor |
| ocr-engineer | `adapters/ocr/`, preprocessing, line grouping, language detection |
| translation-engineer | `adapters/translate/`, prompts, glossary, cache, fallback, secrets |
| ui-engineer | `adapters/render/`, `ui/`, overlay, panel, tray, hotkeys |
| test-engineer | `tests/`, golden fixtures, latency benchmarks |
| safeguard | Read-only gatekeeper. Reviews every phase/slice → PASS or BLOCK |
| update-steward | Dependency/model/API updates without behaviour changes |

The main session is the **orchestrator**: plan, delegate, integrate, and talk to the user.
Each engineer edits only the files it owns. Cross-module changes go through the architect.
Run `safeguard` before declaring any phase or slice done.

## Git & GitHub
- Repo: **https://github.com/killmete/Gloss**. Remote `origin`, default branch `main`.
- Commit at the end of each phase. Push to `main` only after `safeguard` PASS.
- Update work goes on `update/<thing>-<version>` branches and merges after PASS.
- Never force-push or rewrite pushed history. Never commit secrets, `.env`, logs or debug frames.
- If the repo is public, `tests/golden/images/` and `tests/golden/expected/` stay gitignored,
  because they contain other people's chats.

## Commands
- Setup: `uv sync`. If `.venv` doesn't exist, first run `uv python install 3.12`, then
  `& (uv python find 3.12) -m venv .venv --without-pip`. Smart App Control blocks the launcher
  `uv venv` creates, so never create or recreate `.venv` with uv itself.
- Run: `uv run gloss`
- Tests: `uv run pytest` (fast) · `uv run pytest -m golden` · `uv run pytest -m bench`
- Lint: `uv run ruff check . && uv run ruff format --check .`
