# Gloss research summary (Phase 1)

Written 2026-10-30 by the architect. Decisions and reasons: `docs/adr/ADR-0001-stack.md` (status: proposed). Free translation options: `translation-free-options.md`.

## One-screen summary

**Recommended stack.** The planned stack holds, with a cheap translator and no paid backup.

| Part | Choice |
|---|---|
| Capture | `mss` for whole monitor and region. `windows-capture` (WGC) for follow-window mode. |
| OCR | RapidOCR 3.10.0 with PP-OCRv6 **small** by default. PP-OCRv6 **medium** is a config switch for static pages. |
| Translation | Claude `claude-haiku-5-5`, thinking off, effort low. |
| Backups | Optional offline translator pack (rough, large, off by default). If nothing works, Gloss shows the original text. |
| Cost control | $10 a month and $0.50 an hour, with a warning at 80%. |
| App | PySide6-Essentials, PyInstaller one-folder build, Windows `RegisterHotKey` for hotkeys. |

**Benchmark on your 19 screenshots.** Accuracy is provisional until the drafts are confirmed. Speed is from your 16-core CPU, so a 4-8 core PC may be 2-4x slower.

| Engine | All pages | Chat | Menus | Typical time | Peak RAM |
|---|---|---|---|---|---|
| v6 small (default) | 96.5% | 97.9% | 94.7% | 394 ms | 344 MB |
| v6 medium (optional) | 98.2% | 97.9% | 98.4% | 1,293 ms | 498 MB |
| v5 server (not chosen) | 98.5% | 98.4% | 98.8% | 1,915 ms | 700 MB |
| Windows OCR Chinese (manual backup only) | 77.0% | 70.0% | 80.8% | 51 ms | not measured |

Small reads chat as well as medium, and only small fits the 400 MB target. Windows OCR is fast but misses about 8 lines in 20, so Gloss never switches to it on its own.

**Estimated cost:** about **$0.01 per hour** in a quiet room and **$0.04 per hour** in a busy one, or roughly $15-56 a year at 4 hours a day. It is not free, but it is the cheapest paid option compared. This is a calculation, not a measurement; the spikes will check it.

**Biggest risks:**
- The 1.5 second target for a new chat line is not proven (see "Latency" below).
- Without an Anthropic key, Gloss runs but only shows the original text.

**What I need from you.**

| # | Decision | Why it matters |
|---|---|---|
| 1 | Check the expected text for each screenshot, especially image 19 and lines marked `[?]`. **Needed.** | Every accuracy number stays provisional until then. |
| 2 | Do you have an Anthropic API key, or will you create one? Is a **$10 monthly limit** in Gloss OK? Please also set a spend limit in the Anthropic console. | The spikes and real translation need the key. $10 is about 250 busy hours. The console limit is the only hard stop, because the limit in Gloss is a local estimate. |
| 3 | Quick checks, below. | They close known gaps in testing. |

Quick checks:
- Is HDR on for either monitor? (Settings > System > Display > HDR)
- Can you add 2-3 screenshots of real pop-up order cards? The only "order card" image so far is a listing page.
- If you might read Japanese apps, can you add 2-3 Japanese screenshots? Japanese is untested on real screens.

Note: if you turn Smart App Control back on, an unsigned Gloss will probably be blocked.

---

## Benchmark detail (provisional)

Character accuracy and line recall on 19 real screenshots. Harness: `tests/bench/`, full method in `ocr-benchmark.md`.

| Category (images) | Small | Medium | v5 server |
|---|---|---|---|
| Chat (4) | 97.9% / 97.1% | 97.9% / 97.1% | 98.4% / 97.9% |
| Profile (10) | 96.8% / 95.9% | 98.9% / 96.4% | 98.8% / 97.1% |
| Menu (4) | 94.7% / 89.8% | 98.4% / 96.6% | 98.8% / 97.1% |
| Order card (1) | 95.2% / 89.8% | 94.4% / 89.0% | 96.8% / 94.1% |
| All (19) | 96.5% / 93.8% | 98.2% / 95.6% | 98.5% / 96.8% |

Why these scores flatter the engines a little:
- The drafts were corrected after seeing OCR output (61 of 846 lines).
- Extra OCR lines are not penalised.
- Repeated labels can be credited twice.
- The chat category has only 4 images, and order card has 1.
- The kickoff has you check meaning. You do not read Chinese, so individual characters stay agent-checked. Places where the three models disagree are the best extra check.

Medium does not help order cards: it scored slightly below small there. Its gain is on menus and dense profile pages.

Time per OCR call is about **110 ms + 10 ms per text line** on this PC. This is my straight-line fit to the 19 per-image times, so it is approximate. Timings vary by about +/- 30%.

**Proposed accuracy threshold (SPEC section 8.2):** chat at least 97.0% characters and 95.0% line recall; all images at least 95.0% and 92.0%. Lower regression floors per category are in the ADR. These sit 1-2 points below today's scores, so they catch regressions rather than prove quality. If the confirmed text moves any score by more than one point, they come back to you.

## Latency

The old gate (720p frame under 400 ms) measured the wrong thing. Cost follows the number of text lines, not pixels. The proposed gate is per region, for v6 small on 2 threads:

| Region class | Text lines | Measured here | p95 gate, this PC | p95 gate, 4-8 core PC |
|---|---|---|---|---|
| New-lines band | 1-5 | 24-198 ms (2 images) | 250 ms | 500 ms |
| Chat panel or dialog | 6-25 | 166-423 ms (9 images) | 500 ms | 1,000 ms |
| Whole dense page | 26-150 | 719-1,608 ms (8 images) | 2,000 ms | 4,000 ms |

Whole dense pages are not live. They only need to finish, and the bounded queue drops stale work. The 4-8 core column is advisory until someone runs it on such a PC. Small samples mean these gates get re-based in Phase 4.

End-to-end arithmetic for one new line, with a 20-line panel on this PC:

| Step | Whole panel read twice | Whole panel read once | New lines only (estimate) |
|---|---|---|---|
| Wait for next capture at 4 fps | 125 ms | 125 ms | 125 ms |
| OCR | 800 ms | 400 ms | 150 ms |
| Batch wait | 400 ms | 200 ms | 200 ms |
| Show on screen (assumed) | 50 ms | 50 ms | 50 ms |
| **Left for Claude within 1.5 s** | **125 ms** | **725 ms** | **975 ms** |

Three design points follow, and no research report covers them. They are reasoning, not measurements:

1. **Scrolling defeats tile hashing.** A new chat line moves every row, so the whole panel looks changed. Phase 3 needs a simple scroll-shift check so only the newly exposed band goes to OCR.
2. **`stable_reads = 2` conflicts with change detection.** An unchanged screen produces no second OCR. "Read" should be defined as "the pixels were unchanged on the next frame" where possible. That proves stability at zero OCR cost, and it also helps the 25% CPU target.
3. **A lone line waits out the whole batch window.** 400 ms is too much of the budget, so the default drops to 200 ms. Batching saves little money anyway.

## Cost per hour (modelled, not measured)

Assumptions: 3 or 20 new lines per minute, 15 characters per line, 1,500-token cached prompt, 10 context lines, 4 hours a day (1,440 hours a year). Paid models other than Haiku assume no caching and no thinking, so they are minimums. I re-checked this arithmetic.

| Route | Quiet room, per hour | Busy room, per hour | Per year, quiet / busy | Catch |
|---|---|---|---|---|
| **Haiku 5.5, cached, thinking off (chosen)** | **$0.011** | **$0.039** | **$15 / $56** | Needs an Anthropic key |
| Haiku 5.5, default thinking | $0.029 | $0.084 | $41 / $121 | Why thinking stays off |
| Sonnet 5.5, cached | $0.196 | $0.734 | $282 / $1,057 | Too costly for live chat |
| **Offline `quickmt` pack (optional)** | $0 | $0 | $0 | Weak on slang, about 400 MB of disk, above the RAM target while in use |
| **`null` (show original)** | $0 | $0 | $0 | No translation |
| Gemini free tier (not built) | $0 | $0 | $0 | Unpublished limits. Google may review the text. Needs a new adapter |
| Gemini 3.1 Flash-Lite, paid (not built) | $0.066 | $0.20 | $95 / $289 | |
| deepseek-flash direct, peak (not built) | $0.076 | $0.22 | $109 / $317 | |

You prefer free or cheap, so here is the choice in plain terms: the free options are either weak on slang or let another company's staff read other people's chats. Haiku costs a few dollars a month at most on these assumptions. If you ever want a free cloud option, that is a new adapter and a privacy check.

## Windows OCR

**Measured** with the Chinese pack you installed, on the same 19 images and provisional drafts:

| Category | Character accuracy | Line recall | Missed per 20 lines |
|---|---|---|---|
| Chat (4) | 70.0% | 56.4% | 8.7 |
| Profile (10) | 81.0% | 63.3% | 7.3 |
| Menu (4) | 80.8% | 64.9% | 7.0 |
| Order card (1) | 63.9% | 40.7% | 11.9 |
| All (19) | 77.0% | 59.5% | 8.1 |

Speed is 51 ms median and 171 ms at worst, about 8x faster than v6 small. Its RAM was not measured. It reads Chinese only here, with no Japanese or Korean.

**Decision: Gloss keeps it available but never switches to it on its own.** You cannot read Chinese, so garbled text would become confident English that you could not check. It also merges neighbouring columns, which can mix up who said what.

If RapidOCR ever fails, Gloss shows a red status, the fix, and a button for low-quality mode. In that mode the status stays amber, a banner stays on screen, every line is tagged, and results are not saved to the translation cache.

The pack is not wasted: it gives you that manual option, and it let us measure the engine instead of guessing. You do not need to do anything else with it.

## Phase 2 spikes that need your Anthropic key

These wait until you have a key. Development can continue on a fake translator until then.

**Rules:**
- You store the key yourself, once. No agent sees it, and it is never put in a file, printed or logged. The prompt hides what you type:

```
uv run --no-project --with keyring keyring set gloss/anthropic api_key
```

- Test scripts read the key inside the program and report only results.
- Tests use hand-written lines, not real chat, because Anthropic's retention terms are not yet checked.

| Spike | Question | Pass |
|---|---|---|
| 1. Thinking and quality | On 100 Chinese lines, compare Haiku 5.5 with thinking off, effort low and default settings, plus Sonnet 5.5. Record p50/p95 latency, token use, quality, refusals, and any reasoning text in replies. | p50 at most 900 ms and p95 at most 2 s, to fit the 1.5 s target, with no leaked reasoning |
| 2. Real token counts | Run `count_tokens` on 50 lines to replace the 1.3 tokens-per-character guess. | Cost model updated |
| 3. Prompt caching | Is `cache_read_input_tokens > 0` on the second call, and does structured output leave the cache working? | Cache hits confirmed |

The pass limits are my budget-based proposal. If spike 1 fails, thinking changes by config, or you choose between slower translation and another provider.

## Later findings that feed the contracts (Phase 2)

| Finding | Source | Contract or config effect |
|---|---|---|
| Stable monitor IDs | `capture.md`: the libraries number monitors differently | Identify monitors by device name or desktop rectangle, never a bare index |
| Per-mode capture source | `capture.md` | Config picks source and fallbacks per mode, replacing the single `capture.source` |
| Optional `Frame.fresh` flag | `capture.md`: WGC repeats the last frame | Optional field defaulting to true, so the change detector can skip hashing |
| Black-frame health check | `capture.md`, `prior-art.md`: GDI fails as a black image, not an error | Shared check outside adapters. Near-black frames for N reads trigger one trial grab from the next source, and `Health` gives a readable reason |
| Per-line translation failure | `translation.md`: refusals and missing IDs | Results need a per-line failed state. One refused line must not trip the circuit breaker |
| Token usage | Budget guard | The translator must report token usage so spend can be tallied |
| No confidence from Windows OCR | `ocr-benchmark.md` section 7 | `TextBlock.confidence` must allow unknown, and OCR output needs a low-quality marker |

Also from `prior-art.md`, for the Phase 3 backlog: re-assert topmost on the overlay, and use one frozen selection window per monitor.

## Measured and unverified

**Measured on this PC:**
- All OCR numbers above, provisional on the drafts, including Windows OCR on Chinese.
- quickmt 18 ms and Argos 23 ms per line on 16 hand-written lines, with informal quality judgement.
- All four capture backends hide a window flagged `WDA_EXCLUDEFROMCAPTURE` (Tk window). A Qt click-through overlay was also hidden from `mss`.
- `mss` takes 7-13 ms per grab.
- WGC follows a covered, moved window.
- Unthrottled WGC used 22% of a core, and 4.1% when throttled to 4 fps.
- `RegisterHotKey` works with a Qt filter, tested with a synthetic key press.
- The Qt-only PyInstaller probe is 93 MB one-folder and 37.7 MB one-file. 15 of its 58 binaries are unsigned.

**From vendor or official docs, not tested by us:** Claude model IDs, prices, rate limits, caching rules and thinking behaviour; WGC and DXGI platform rules.

**Unverified or unmeasured:**
- Claude latency, refusal rate and slang quality; the cost model inputs.
- Whether the whole app stays under 400 MB. The 344 MB is OCR alone with no Qt, leaving 56 MB of headroom.
- Idle and active CPU; any 4-8 core timing; issue #743 (medium and small may run 4-11x slower on some CPUs).
- Japanese on real screenshots; Korean beyond one test image.
- quickmt RAM.
- HDR; mixed-DPI; exclusive fullscreen.
- Qt overlay exclusion against WGC; hotkeys inside games and in frozen builds.
- WGC yellow border in an unpackaged app.
- Full build size. A rough sum of parts is 340 MB, or 470 MB with medium bundled.
- Nuitka, which never built; antivirus results.
- Anthropic API data-retention terms; DeepL Free terms if it is ever added.

## Contradictions between reports, and how they were resolved

| Contradiction | Resolution |
|---|---|
| `ocr.md` and `translation.md` say uv is missing; `ocr-benchmark.md` used it | uv 0.13.6 and Python 3.12.16 are installed. The quickmt benchmark was skipped for this reason and can run if offline translation is ever wanted. |
| `app-shell.md` treats Smart App Control as something to check | You turned it off on 2026-10-30. It is now a distribution concern only. |
| `ocr.md` gives small 162 MB and medium "2.2x slower, helps a little" | Those were synthetic images. The real benchmark governs: 344 MB, medium 3-4x slower, and clearly better on menus. |
| `ocr.md` gives 196 ms for a 720p frame; the benchmark gives 394 ms median and up to 1.6 s | Both are valid. The synthetic frame had 12 lines and real pages have up to 146. |
| `ocr-benchmark.md` says 7-10 ms per line, but its own table shows 8-40 ms per line | The first figure is the slope. There is also a fixed cost of about 110 ms per call. |
| `ocr-benchmark.md` says v5 server has the "same score" as medium, "no reason to prefer it" | The claim is too strong: server beats medium on chat, order cards and recall, and has a lower p95 (5.9 s against 6.8 s). Medium still wins on RAM, disk and median time, and the gaps are small on this sample. |
| `ocr.md` and `prior-art.md` recommend Windows OCR as a fallback, and `prior-art.md` calls it zero-install | Measured at 70.0% on chat, and the pack needs admin rights. Manual low-quality mode only. |
| ADR-0001 uses the optional `quickmt` pack as second provider; the CLAUDE.md Stack text names DeepL | `quickmt` is free and private. CLAUDE.md is updated after your approval. |
| `translation.md` recommends DeepL Free as first fallback | DeepL stays out of the MVP: it needs an adapter, has a monthly cap, and its retention is unverified. An outage shows original text unless the offline pack is on. |
| SPEC section 5 moves a failed OCR engine to the next in the list, but the OCR fallback list is now empty | ADR-0001 uses red status plus a manual option. The Phase 4 drill must expect that. |
| CLAUDE.md says source language is auto-detected, including ko, th and ru | The MVP reads only zh, en and ja. |
| SPEC default `languages` includes `ko`; `ocr.md` says v6 has no Korean model | One recogniser covers zh, en and ja. Korean is removed from the default. |
| Benchmark used `text_score = 0.5`; SPEC default `min_confidence = 0.6` | Unmeasured difference. Phase 2 uses 0.5 until the golden set shows 0.6 is safe. |
| `translation.md` treats refusals as `TransientError`, yet also says retrying usually refuses again and SPEC retries transient errors | Refusals skip plain retry: split the batch once, send the failed line to the next provider, and do not count it toward the breaker. |
| `translation.md` says "do not hold lines" but keeps a 400 ms wait for a lone line | The wait drops to 200 ms, tuned by spike 1. |
| `translation.md` offers `max_requests_per_hour` raised to 1,500, or a spend guard | A spend guard replaces it. Dollars are what matter, and a request count ignores tokens and model price. |
| SPEC bench rule blocks a >20% slowdown; timings vary by +/- 30% | Phase 4 needs a quieter method, or the rule will misfire. |
| `capture.md` says mss sets older DPI mode and Qt is untested; `app-shell.md` shows Qt alone reaches V2 | Set V2 first in `main` and in the exe manifest. Test mss and Qt together. |
| `app-shell.md` records a pixel of (228,224,223) after hiding; its data file shows (32,24,19) | The file holds a later run with a different background. Both are not red, so the conclusion stands. |
| `app-shell.md` and `ocr.md` place packaging in Phase 5; the kickoff and SPEC put the build in Phase 4 | Phase 4. |
| SPEC 8.2 says "every line gets an English translation", but OCR recall is under 100% | Reworded as every line OCR returned. Missed lines are covered by the recall gate. |
