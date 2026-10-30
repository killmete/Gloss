# ADR-0001: Stack for Gloss MVP

- Status: **Proposed**, pending user approval at Checkpoint 1
- Date: 2026-10-30
- Evidence: `docs/research/SUMMARY.md` and the reports it cites

## Context

Phase 1 tested the default stack in `CLAUDE.md` against SPEC targets: chat line to English p50 at most 1.5 s, RAM under 400 MB, near-zero idle CPU, text-only privacy default. This session established three facts that override parts of the reports:
- uv 0.13.6 and Python 3.12.16 are installed.
- Smart App Control is off on the user's PC, so it is a distribution concern only.
- The repo is public.

Later inputs from the user: translation should be free, or else as cheap and good as possible, and Claude Haiku 5.5 is their chosen translator; the Windows Chinese OCR pack is installed; Korean is not needed; Japanese is a maybe. They have not yet confirmed having an Anthropic key.

All accuracy figures are provisional until the golden drafts are confirmed. All timings come from a 16-core 9950X3D, which is also the user's own PC, and vary by about +/- 30%.

## Decision summary

| Area | Decision |
|---|---|
| Capture | `mss` for monitor and region, `windows-capture` (WGC) for window, source chosen per mode |
| OCR | RapidOCR 3.10.0 on ONNX Runtime CPU, v6 small default, v6 medium as a selectable profile |
| Windows OCR | Registered but not an automatic fallback. Manual low-quality mode with a visible warning |
| Translation | `claude-haiku-5-5`, optional offline `quickmt` pack, then `null` |
| Thinking | Disabled, with effort low, confirmed by spike 1 |
| Spend | Dollar budget guard replaces `max_requests_per_hour` |
| Shell | PySide6-Essentials |
| Packaging | PyInstaller one-folder, no UPX |
| Hotkeys | `RegisterHotKey` through a Qt native event filter |

**Change to `CLAUDE.md`: none.** Anthropic stays the primary. The one departure from its Stack text is that the second provider is the optional `quickmt` pack, not DeepL. CLAUDE.md requires user approval before that text changes.

## 1. Capture

**Options:** `mss`, `windows-capture` WGC, `windows-capture` DXGI, `dxcam`, hand-built WinRT.

**Decision:**
- `monitor` and `region` modes: `mss` 10.2.0 (using `mss.MSS`), falling back to `wgc`.
- `window` mode: `wgc` through `windows-capture` 2.0.1, falling back to `mss` with rectangle polling.
- `capture.source` becomes per-mode in Phase 2.
- DXGI is not built for the MVP.

**Reasons:**
- `mss` is pure Python with no dependencies. Measured at 7-13 ms per grab, and it honours `WDA_EXCLUDEFROMCAPTURE`.
- WGC by HWND survived covering and moving a window, which polling cannot do.
- DXGI allows one duplication per output per process (reproduced) and must be rebuilt after mode changes. `dxcam` has open crash bugs on teardown and adapter switching, and this PC has three display adapters.

**Consequences:**
- The WGC adapter must set `minimum_update_interval` from `fps_max` (22% of a core unthrottled, 4.1% at 4 fps).
- WGC pushes frames, so the adapter keeps the last frame to serve the pull port.
- Monitors need stable IDs because the libraries number them differently.
- Silent black frames need a shared health check.

## 2. OCR

**Options:** v6 small, v6 medium, v5 server, Windows.Media.Ocr. Tesseract, full PaddleOCR, EasyOCR, NPU-only Windows OCR, and LLM vision as default were rejected in `ocr.md`.

| Model | All / chat / menu accuracy | Median | p95 | Peak RAM | Models |
|---|---|---|---|---|---|
| v6 small | 96.5 / 97.9 / 94.7 | 394 ms | 1,608 ms | 344 MB | 30 MB, in the wheel |
| v6 medium | 98.2 / 97.9 / 98.4 | 1,293 ms | 6,845 ms | 498 MB | 133 MB |
| v5 server | 98.5 / 98.4 / 98.8 | 1,915 ms | 5,920 ms | 700 MB | 165 MB |

**Decision: v6 small is the default.**
- Gloss exists mainly for live chat. On chat, small ties medium and sits 0.5 points below server, a gap this sample cannot resolve.
- Only small is near the 400 MB target. Medium and server exceed it before Qt is even loaded.
- Only small can fit the latency budget, and that is on a CPU far above the 4-8 core target.
- Issue #743 reports 4-11x slowdowns on some CPUs for medium's recogniser and small's detector. It did not reproduce on Zen 5 and is untested on Intel.

**Decision: a selectable higher-accuracy profile is justified.**
- `ocr.profile = "fast" | "accurate"`, with `fast` as default. `accurate` is v6 medium.
- The benefit is real and concentrated where speed matters least. Menu accuracy rises from 94.7% to 98.4%, recall from 89.8% to 96.6%, and the worst profile page from 87.4% to 98.2%.
- Scenario 2 (static pages) can wait a few seconds. Live chat gains nothing.
- The RAM target applies to `fast`. `accurate` is documented as exceeding it. Profiles swap, so the two models are never loaded together.
- It is not a fix for order cards, where medium scored below small (94.4% against 95.2%).
- Both model sets are bundled, adding 133 MB of disk. Nothing downloads at runtime, because `prior-art.md` shows downloaders cause antivirus and install failures.
- Model fields stay in config, so v5 server is reachable without code but is neither tested nor supported. It stays out because of its 700 MB RAM.
- A one-shot "reread this page accurately" hotkey is deferred until after Checkpoint 4.

**Check before relying on this trade-off (hypothesis, not measured).** `limit_side_len = 960` halves a 1,900-wide frame before detection. The two images where small trails medium most (09 and 06) are both full-screen, while crops under 960 px wide scored 98.5-100% on small. Region OCR may recover much of the lost text with small alone. Phase 3 should re-run images 06 and 09 as native-size crops and with a higher limit.

**Decision: Windows OCR stays registered but is not an automatic fallback.**

With the Chinese pack installed it was measured on the same 19 images and drafts (provisional): 77.0% all, 70.0% chat, 59.5% line recall, 8.1 lines missed per 20, 51 ms median. That is 27 points under the chat gate, too wide a gap for draft errors to explain. It has no Japanese or Korean recogniser here.

- **Why it is not in `ocr.fallbacks`.** Bad OCR is worse than no OCR here. The user cannot read Chinese, so garbled text sent to translation returns confident English they cannot check. It also joins neighbouring columns, which can merge different speakers into one line, and it wastes API spend. Jittery text would also weaken dedupe.
- **Why it stays registered.** The adapter is small, it proves `ocr.engine` swaps by config, and it gives the user a manual option if RapidOCR cannot load.
- **Default failure behaviour.** The automatic OCR fallback list is empty. If RapidOCR fails, status goes red with the exact fix and a button offering low-quality mode.
- **Low-quality mode is never silent.** The user must turn it on, per session. While on, the status stays amber, the panel shows a permanent "Low-quality OCR: text may be wrong or missing" banner, and every line is tagged. Results are not written to the translation cache.
- **Adapter rules.** It raises `AdapterUnavailable` if the required language is missing and never runs en-US over Chinese. It removes the spaces it puts between Chinese characters. It returns no confidence scores, so `TextBlock.confidence` needs to allow "unknown" in Phase 2.
- **Rejected uses.** Not a fast path, because it fails the thresholds. Not a change detector, because tile hashing is cheaper.

**Languages:** the default is `zh, en, ja`. One recogniser covers all three, so `ja` costs nothing. Japanese is **unverified on real screenshots** (synthetic lines only) and has no fallback, since Windows OCR here has no Japanese. The user does not need Korean, so it is out.

**Adapter requirements carried forward:**
- Pin `rapidocr==3.10.0` and `onnxruntime==1.31.1`. RapidOCR 3.11.0 is due 2026-10-31 and may change model files, so upgrade only through `docs/UPDATING.md` with the golden suite.
- Use `Det.limit_type=max`, because the default `min` setting broke small crops. Use 2 intra-op threads and keep all of these in config.
- `onnxruntime` is not a declared RapidOCR dependency, so add it explicitly.
- `min_confidence` starts at 0.5, the benchmarked value, until 0.6 is proven safe.

## 3. Latency gate (replaces "720p frame p95 under 400 ms")

OCR cost is about 110 ms plus 10 ms per line, so the gate is per region class, for v6 small on 2 threads.

| Region class | Lines | Measured here | p95 gate, this PC | p95 gate, 4-8 core |
|---|---|---|---|---|
| New-lines band | 1-5 | 24-198 ms | 250 ms | 500 ms |
| Chat panel or dialog | 6-25 | 166-423 ms | 500 ms | 1,000 ms |
| Whole dense page | 26-150 | 719-1,608 ms | 2,000 ms, not live | 4,000 ms |

The 4-8 core column is advisory until run on such a PC. All gates are re-based in Phase 4 from a quieter method, because +/- 30% noise would also misfire SPEC's 20% regression rule. The `accurate` profile has no latency gate, only a "finishes" check.

SPEC's end-to-end target stays p50 at most 1.5 s, p95 at most 3 s, and is **unproven**. Two full 20-line reads plus a 400 ms batch wait leave 125 ms for the API. So:
- Phase 3 must add scroll-shift detection, because a scrolled panel marks every tile dirty and changed-region OCR alone does not help.
- A stability "read" may be satisfied by unchanged pixels on the next frame, with no second OCR.
- `batch_max_wait_ms` defaults to 200.

If these do not meet the target on measurement, raise it with the user rather than quietly weakening it.

## 4. Accuracy threshold for SPEC section 8.2 (proposed)

Scored with `tests/bench/ocr_score.py`, fast profile, on confirmed text.

| Scope | Character accuracy | Line recall | Provisional v6 small | Type |
|---|---|---|---|---|
| Chat | >= 97.0% | >= 95.0% (1 miss per 20) | 97.9% / 97.1% | Acceptance gate |
| All images | >= 95.0% | >= 92.0% | 96.5% / 93.8% | Acceptance gate |
| Profile | >= 95.0% | >= 93.0% | 96.8% / 95.9% | Regression floor |
| Menu | >= 93.0% | >= 88.0% | 94.7% / 89.8% | Regression floor |
| Order card | >= 93.0% | >= 88.0% | 95.2% / 89.8% | Regression floor |

The `accurate` profile adds menu >= 97.0% / 95.0%.

These sit 1-2 points below today's scores (character accuracy), so they guard against regression. They do not prove quality independently. If the confirmed text moves any score by more than one point, the thresholds are re-proposed to the user, never adjusted silently. The order-card row needs real pop-up cards, because the one image is a listing page. Section 8.2 is read as every line OCR returned gets a translation; lines OCR misses fall under recall.

## 5. Translation

**Decision by the user:** Claude Haiku 5.5 is the translator. The user prefers free or cheap, and has not yet confirmed having an Anthropic key. Evidence: `translation.md` and `translation-free-options.md`.

**Options:** Haiku 5.5, Sonnet 5.5, Gemini free tier, DeepL Free, quickmt, LibreTranslate/Argos, Google, `null`.

| Position | Provider | When it is active |
|---|---|---|
| Primary | `anthropic`, model `claude-haiku-5-5` | When a key is in keyring |
| Fallback | `quickmt` | Only if the optional offline pack is installed and switched on |
| Last resort | `null` | Always. Shows the original text with amber status |

**Shipped default: `provider = "anthropic"`, `fallbacks = ["null"]`.** This is SPEC's existing config shape, so it stays.
- The default matches what the user chose, so no personal config is needed.
- It is safe in a public repo. The key lives in keyring, and a user with no key gets `AdapterUnavailable`, so Gloss falls to `null`, shows the original text, and explains how to add one.
- Nothing leaves the PC until someone adds a key, so adding it is the opt-in. A "cloud translation off" switch remains.
- `quickmt` is not listed by default, because it is an opt-in pack. Users who install it add it to `fallbacks`.

**Why Haiku 5.5.**
- Modelled cost is $0.011-0.039 per hour, about $15-56 a year at 4 hours a day. This is the cheapest paid route compared, because of its $0.10 input price and prompt caching.
- It takes glossary, context and style instructions, which the free local engines cannot.
- Rejected as live default: Sonnet 5.5 (19x the cost, and thinking cannot be disabled). It stays a config option for later low-volume features.

**Free options, on their merits:**
- Gemini free tier: $0, but its terms allow human reviewers to read input and output and allow use for product improvement. That conflicts with the privacy rule for other people's chats. Anthropic's own retention terms are unresearched, so this weighs a known problem against an unchecked one.
- DeepL Free: stays out of the MVP. It needs its own adapter, is capped at 500,000 characters a month, handles slang through glossary entries only, and its retention terms are unverified. It adds little beside `quickmt`.
- LibreTranslate: 429 MB install, a server process, the weaker engine in the smoke test, and no documented glossary. Public instances send text to strangers.
- Stock `argostranslate`: pulls in PyTorch through `stanza`.
- `quickmt`: free and private, kept as the optional pack below.

**`quickmt` is optional, because it cannot fit the RAM target.**
- The model is 407 MB on disk at about 200M parameters. Loaded as int8, weights alone are about 200 MB by arithmetic. RAM is unmeasured, but the app has only 56 MB of headroom over OCR, so the verdict does not depend on the exact number. Estimated total is 550 MB or more.
- It is an installable offline pack, off by default, not in the default build. `ctranslate2` and `sentencepiece` are an optional dependency extra. The user installs the pack by a deliberate step, with a pinned hash. The app itself still downloads nothing at runtime.
- It loads only when the chain reaches it and unloads after idle, so the 400 MB target applies whenever it is not in use. Above-target RAM during an outage is disclosed, as with the `accurate` profile. Phase 4 measures it.
- If the pack is absent, the adapter raises `AdapterUnavailable` and the chain moves to `null`.
- Output is labelled as rough. It ignores glossary and context, and about 5 of 16 hand-written lines were wrong, mostly slang (a smoke test, not a benchmark).

**MVP adapters: `anthropic` and `null`, plus `quickmt` as the optional pack.** No generic adapter for other providers is built. A second cloud provider would be a new adapter, added when actually needed. Engine swaps remain config changes among registered adapters.

**Request shape:** structured output through `output_config.format`, a cached system prompt and glossary, context after the cache breakpoint. A shared validator requires each id exactly once, otherwise the line is re-queued.

**Thinking: `thinking = "disabled"` with `effort = "low"`, confirmed by spike 1.**
- Thinking is on by default, billed as output, and adds latency in a budget with almost no slack. Default settings cost about 2.2x in the model.
- Translating short lines against a glossary should not need reasoning, though this is unproven.
- The known risk is reasoning-like text leaking into replies. Structured output limits this but does not eliminate it, so spike 1 checks for it.
- If spike 1 finds worse quality or leaked reasoning, the default becomes adaptive thinking with low effort, as a config change only.
- Both values are config and stay constant at runtime, because changing effort invalidates the message cache. The adapter checks model capability, because Sonnet 5.5 rejects `disabled`.

**Spikes before Haiku is relied on.** All three use hand-written lines, not real chat, and need the user's key in keyring:
1. Thinking and quality: 100 lines at thinking off, effort low and default settings, compared with Sonnet 5.5. Record latency, tokens, quality and refusals.
2. `count_tokens` on 50 lines, to replace the 1.3 tokens-per-character guess.
3. Caching: `cache_read_input_tokens > 0` on the second call, with structured output on.

**Budget guard replaces `max_requests_per_hour`.**

A request count blocks a legitimate busy room (1,200 requests per hour at batch size 1, against a limit of 600), and it ignores tokens, thinking and model price.

| Proposed setting | Default | Meaning |
|---|---|---|
| `monthly_usd` | 10.00 | About 250 busy hours |
| `hourly_usd` | 0.50 | About 13x a busy room; catches runaway loops |
| `warn_at` | 0.8 | Tray warning at 80% of monthly |
| Prices | in config | Per model, never in code |

- Spend is calculated from API `usage` fields and kept in local SQLite, so it survives restarts.
- At a limit, Gloss stops calling the paid provider, moves down the chain, and shows amber.
- It is a local estimate. The Anthropic console limit is the only hard stop.
- A model with no price entry is charged at the highest configured price, never treated as free.

**Failure handling:**
- `stop_reason: refusal` is not retried as-is. The batch splits once, the failed line goes to the next provider, and it does not count toward the circuit breaker.
- `max_tokens` truncation: retry with a split batch.
- 429 with `enforced_spend_limit_reached`, and the user-set limit's HTTP 400: `PermanentError` with a clear message, no retry.

## 6. App shell and packaging

**Options:** PySide6, Tauri, .NET WPF, Tkinter; PyInstaller, Nuitka.

**Decision:**
- PySide6-Essentials 6.12 (not the `PySide6` meta-package, which adds 40 MB of Addons).
- PyInstaller one-folder, zipped or in an installer, no UPX, in Phase 4 as the kickoff states.

**Reasons:**
- One language and one process keep contract tests simple.
- A working probe showed tray, click-through, per-monitor V2, capture exclusion and hotkey together.
- Tauri would still need a Python sidecar. WPF would rewrite the pipeline. Tkinter has no per-pixel transparency.
- One-folder avoids extraction to `%TEMP%` at each launch and makes file-by-file auditing simple.
- Nuitka has no measurement here because the build failed, and its licence is unchecked. It is only a backup if antivirus problems appear.

**Consequences:**
- Set Per-Monitor V2 first in `main` and in the exe manifest, then test with `mss` and Qt together.
- Exclusion and click-through must be reapplied if Qt recreates a native window.
- Overlay windows need `WS_EX_NOACTIVATE` and periodic topmost re-assertion.
- Unsigned builds work on the user's PC. Smart App Control would likely block them elsewhere, or on this PC if turned back on. Signing is deferred until the user wants to share Gloss. Microsoft Store or SignPath are the candidates, and SignPath needs an open-source licence, which the repo lacks. Azure Artifact Signing's individual tier is limited to the US and Canada.

## 7. Hotkeys

**Options:** `RegisterHotKey`, `pynput`, `keyboard`.

**Decision:** Win32 `RegisterHotKey` with `MOD_NOREPEAT` through a Qt native event filter, behind a small `HotkeyService` in `ui/`. Bindings stay in config: `ctrl+alt+t`, `ctrl+alt+r`, `ctrl+alt+p`.

**Reasons:**
- It was measured working with no hook thread.
- `keyboard` has been unmaintained since 2020.
- Low-level hooks are heavier and, by general knowledge rather than a tested source, the kind of behaviour anti-cheat software dislikes.

**Consequences:**
- The return value must be checked. A taken combination shows "already in use" and offers a rebind, and must never fail silently.
- Bindings are released on exit.
- Delivery inside real games and from a frozen build is still unverified, so both go on the Phase 4 test list.

## SPEC changes implied (to apply after approval)

- Section 4: per-mode capture source, `ocr.profile`, empty `ocr.fallbacks`, `languages` without `ko`, `min_confidence` 0.5, `batch_max_wait_ms` 200, `translate.fallbacks` of `null` only by default (DeepL removed), thinking and effort keys, budget section instead of `max_requests_per_hour`.
- Section 5: OCR load failure now means red status and a manual option, not an automatic next engine.
- Section 7: add the per-region table.
- Section 8.2: add the thresholds. Its translation requirement needs an Anthropic key.
- Section 3 (Phase 2 contract work): stable monitor ID, optional `Frame.fresh`, per-line translation failure, usage reporting, `TextBlock.confidence` allowing "unknown", a low-quality marker on OCR output.

## Consequences overall

- The stack stays small and the MVP keeps a single process.
- Two things can still fail: end-to-end latency (it depends on new-lines-only OCR and unmeasured Haiku latency) and total RAM (only 56 MB of headroom for Qt and the app).
- Without an Anthropic key, Gloss runs but shows original text. Phase 3 can proceed on a fake translator.
- There is no automatic OCR backup. This is deliberate: a visible failure is better than convincing wrong text.

## Revisit when

- Confirmed drafts move any score by more than one point, or new order-card images fail the floors.
- Native-size region tests show small recovers the lost text. The `accurate` profile may then be dropped, or medium's menu gain shrinks.
- Windows OCR with upscaling reaches the thresholds on Chinese.
- RapidOCR 3.11 changes models or fixes #743, or the app runs on an Intel CPU.
- Spike 1 shows Haiku latency, quality or refusal rate is unacceptable.
- The user wants a free cloud option or a second cloud provider, which needs a new adapter and a privacy check.
- quickmt RAM is measured, or a smaller offline model proves good enough.
- The user adds Japanese screenshots or asks for Korean.
- Total RAM exceeds 400 MB in the Phase 4 benchmark.
- The user wants to distribute Gloss, or HDR turns out to be on.
- `mss` shows repeated black frames, or `windows-capture` becomes unmaintained.
