# Kickoff prompt — paste everything below the line into Claude Code

---

You are the **orchestrator** for building Gloss, a lightweight Windows app that watches my
screen, OCRs foreign-language text (mostly Chinese) and shows English translations in near real
time. Read `CLAUDE.md` and `docs/SPEC.md` first. They are the source of truth. The agent team is
defined in `.claude/agents/`. Delegate to those subagents by name instead of doing specialist work
yourself, and keep your own context for planning, integration, and talking to me.

Work in the phases below. At each **⛳ checkpoint**, stop and give me a short plain-English summary
(what was decided, what I need to do, what's next) and wait for my "go". Between checkpoints, work
autonomously. Keep a task list so I can see progress.

## Phase 0 — Environment check (you)
1. Check the Windows version and build (`WDA_EXCLUDEFROMCAPTURE` needs Windows 10 2004+), whether
   `git`, `uv` and Python 3.12 are available, and the monitor count and DPI scaling.
2. If something is missing, give me exact install commands to run and wait.
3. **GitHub repo: https://github.com/killmete/Gloss** (default branch `main`). This folder is the
   working copy for that repo.
   - If this folder isn't a git repo yet, run `git init -b main` and
     `git remote add origin https://github.com/killmete/Gloss.git`.
   - If the remote already has commits (README, licence…), merge them:
     `git fetch origin` then `git pull origin main --allow-unrelated-histories`. Keep both sides;
     if the kit's files conflict with the remote, ask me before choosing.
   - Check that I can push. Use `gh auth status` if the GitHub CLI is installed; otherwise do a
     test push in step 5. If authentication fails, give me the exact steps to sign in (Git
     Credential Manager or `gh auth login`) and wait.
   - Check whether the repo is public or private (`gh repo view killmete/Gloss --json visibility`,
     or ask me). **If it's public**, add `tests/golden/images/` and `tests/golden/expected/` to
     `.gitignore`, because my screenshots contain other people's names and chats. Tell me those
     tests will only run locally.
4. Create the repo skeleton from CLAUDE.md and add a `.gitignore` (venv, caches, builds, `.env`,
   `*.log`, and the unlock files `.claude/*_UNLOCK`). Commit the freeze markers
   (`.claude/*_FROZEN`) when they appear; never ignore them.
5. Verify the safeguard hooks fire on this machine:
   - Try to Edit `.claude/settings.json` (add a harmless trailing newline) → must be BLOCKED.
   - Try to Read `.env` (it doesn't need to exist) → must be BLOCKED.
   - Try to Write `docs/_probe.md` → must be ALLOWED (then delete it).
   - If a hook doesn't run (for example `python` isn't on PATH but `py` is), you can't edit
     `.claude/settings.json` yourself. Tell me the exact one-line change to make and wait.
6. Make the first commit and push it to `origin main`.

⛳ Checkpoint 0: report the environment, the GitHub connection (repo, visibility, push OK) and the
hook test results.

## Phase 1 — Research & literature review (researcher, run in parallel)
Spawn the `researcher` subagent **four times in parallel**, one per topic, each writing to
`docs/research/<topic>.md`:
- **capture**: mss vs Windows Graphics Capture vs DXGI Desktop Duplication in Python. Cover
  stability, CPU cost, black-window/HDR issues, per-monitor DPI, window-follow mode, and capture
  exclusion of our own windows.
- **ocr**: RapidOCR (and model variants), Windows.Media.Ocr / Snipping-Tool OCR wrappers,
  Tesseract, PaddleOCR, and an LLM-vision fallback. Cover accuracy on stylised Chinese game UI and
  chat bubbles, speed on CPU, install size, and Japanese/Korean support.
- **translation**: Anthropic API (current model IDs, pricing, latency, structured output, prompt
  caching, rate limits — verify on docs.claude.com) vs DeepL vs Google vs offline options. Cover
  batching, context and glossary strategy, and cost per hour of active chat.
- **app-shell**: PySide6 vs alternatives (Tauri, .NET/WPF, Tkinter) for a click-through overlay +
  tray + panel. Cover packaging size and stability (PyInstaller vs Nuitka) and global hotkeys.
  Also do a prior-art review of existing screen translators (e.g. Translumo, LunaTranslator,
  ScreenTranslator, PowerToys Text Extractor): what they do well, their known pain points, and
  what to copy or avoid.

Each report must end with: a recommendation, rejected options with reasons, and open risks.

Then have `test-engineer` build a tiny benchmark harness and run the top 2 OCR candidates on the
images in `tests/golden/images/`. I will drop real screenshots there (Chinese chat rooms, order
cards, menus, profile pages). Report character accuracy and ms per image.

Finally, `architect` writes `docs/research/SUMMARY.md` and ADR-0001 (stack decision). It may only
change the default stack in CLAUDE.md if the evidence clearly justifies it.

⛳ Checkpoint 1: give me a one-screen summary of the recommended stack, the benchmark numbers, the
estimated API cost per hour, and anything I must decide. Show me the proposed expected text for
each golden image so I can confirm or correct it (I'll check meaning, you check characters). Once I
confirm, save it to `tests/golden/expected/` and create `.claude/GOLDEN_FROZEN`.

## Phase 2 — Architecture & contracts (architect → safeguard)
1. `architect` finalises `src/gloss/core/contracts/` (models, ports, errors, version), the
   registry, the config schema + `config/default.toml`, `docs/UPDATING.md`, and ADRs.
2. `test-engineer` writes the contract test suites (`tests/contract/`) **before** any adapter
   exists. They should fail or skip cleanly with no adapters.
3. `safeguard` reviews. Fix every BLOCK item.

⛳ Checkpoint 2: show me the contracts in plain English (what each module promises). After my
"go", create `.claude/CONTRACTS_FROZEN`. From then on the hook blocks contract edits, and I'll only
create `.claude/CONTRACT_UNLOCK` if a change is truly needed.

## Phase 3 — Build the MVP in parallel slices
Run these engineers in parallel. Each owns only its folders (see CLAUDE.md), works test-first
against the contract suites, and finishes with `safeguard` PASS:
- `capture-engineer`: mss adapter (+ WGC/DXGI if research says so), change detector, region
  selector backend, DPI handling, capture-exclusion helper.
- `ocr-engineer`: primary + fallback OCR adapters, preprocessing, line grouping, TextTracker
  (jitter-tolerant dedupe + stability).
- `translation-engineer`: anthropic + fallback + null translators, redaction, glossary loader
  (`docs/glossary/zh_gaming.yaml`), SQLite cache, batching, circuit breaker, keyring secrets with a
  first-run "enter API key" flow.
- `ui-engineer`: tray, side panel (default), click-through overlay, hotkeys, settings window,
  status dot.

Then you (orchestrator) wire up `core/pipeline.py` and `app.py`. `test-engineer` adds an
end-to-end test that replays a recorded sequence of chat screenshots through the whole pipeline
with a fake translator.

⛳ Checkpoint 3: tell me how to run it (`uv run gloss`), what to try, and what's not done yet.

## Phase 4 — Harden, measure, package
- Golden tests + latency/CPU benchmark against the targets in SPEC §7. Save the baseline to
  `tests/bench/baseline.json`.
- Failure drills: kill the network, give an invalid API key, remove an OCR model file, hang a
  stage. The app must degrade, show status, and recover (SPEC §5).
- Windows build (per ADR) + a `README.md` for me with install/run/uninstall steps and screenshots.
- `safeguard` full review of the whole repo → PASS.

⛳ Checkpoint 4: I run it on my real chat room. Collect my feedback as issues in `docs/FEEDBACK.md`.

## Phase 5 — Update-proofing dry run
`update-steward` performs one real dependency update (pick the safest outdated package) following
`docs/UPDATING.md` end to end, including baseline comparison and rollback notes. Then it
documents how to swap the translation model and how to add a new OCR engine as a plugin.

⛳ Checkpoint 5: done. Give me a short "how to maintain this" note.

## Ground rules for this whole project
- Keep it lightweight: no heavy frameworks (no full PyTorch) unless research proves it's needed
  and I approve.
- Never hard-code model IDs, keys, languages or hotkeys. Use config.
- Never weaken, skip or delete a test, hook, or safeguard to make something pass. Explain and ask.
- If a subagent gets stuck twice on the same problem, stop and tell me instead of looping.
- Commit at the end of each phase with a clear message, and push to `origin main` only after
  `safeguard` returns PASS. Tag releases (`v0.1.0`, …) at Checkpoints 4 and 5.
- `update-steward` works on `update/...` branches. Push the branch, and merge into `main` only
  after PASS.
- Never force-push, rewrite pushed history, or push secrets. If a push is rejected, pull and
  rebase/merge, then tell me what happened.
- Explain things to me in plain English. I'm not a professional developer.
