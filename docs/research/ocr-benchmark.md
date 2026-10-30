# OCR benchmark on the user's real screenshots (Phase 1)

Run: 2026-10-30. All accuracy figures are **PROVISIONAL**: the expected text is a draft written by the
test-engineer agent and has not been confirmed by the user. Only aggregate numbers and file names appear here
(the file names are timestamps, no personal names). Raw OCR output, drafts and per-line misses live only in
`tests/golden/expected/_draft/` (gitignored).

## 1. Machine

AMD Ryzen 9 9950X3D, 16 cores, Windows 11 26300, CPython 3.12 (uv), onnxruntime 1.31.1 (CPU), rapidocr 3.10.0.
This is a very fast desktop CPU, **well above the SPEC target of 4-8 cores**. Treat every ms figure below as a
best case; a 4-core laptop may be 2-4x slower. The timing run was not on a quiet machine (a repeat of the same
v6-small run varied by up to about 1.5x on single images, see section 4), so read ms as +/- 30%.

## 2. Method

- Harness: `tests/bench/ocr_bench.py` (run), `tests/bench/ocr_score.py` (score), `tests/bench/winocr_check.py`
  (Windows OCR availability and timing). Run with `uv run --no-project --python 3.12 --with rapidocr==3.10.0
  --with onnxruntime==1.31.1 --with psutil --with pillow python tests/bench/ocr_bench.py <variant>`.
- Adapter settings from `docs/research/ocr.md`: `Det.limit_type=max`, `limit_side_len=960`, 2 intra-op threads,
  1 inter-op thread, `text_score=0.5`. Images are OCR'd at native size (whole screenshot, 100x108 up to
  1919x1036). Variants: PP-OCRv6 small (bundled, primary), PP-OCRv6 medium, PP-OCRv5 server. All three could be
  fetched.
- ms per image = median of 3 timed runs after one warm-up run, model load excluded. Peak RAM = process peak
  working set after all 19 images. Because detection caps the long side at 960, a separate 1280-wide resize gave
  the same times as native size, so no extra 720p column is shown.
- 19 images. Categories (my classification): **chat room 4, profile 10, menu/dialog 4, order card 1.** The
  full-screen home/listing page and the filter/dropdown crops are counted under "menu". The user's categories
  did not include a plain "listing page"; the one listing page full of priced master cards is "order card".
  Images 01, 06, 07, 09, 14 are about 1900x1030 (full screen); 16-18 about 1280x920; the rest are small dialogs
  or crops.
- Drafts: I transcribed each image line by line **before** looking at any OCR output (846 lines). I then
  compared with the OCR output and corrected or extended 61 lines (about 7%). 44 of those are a single image
  where I had skipped the header and top bar; the others are a few mis-reads (for example a wrong character in a
  tab name) and some tiny text inside embedded game screenshots. I added each such line only after seeing it in
  the image, but a draft that is extended using OCR output can favour OCR, and the engines agree with each other
  more than any of them agrees with a human, so the real gap to ground truth is probably larger than shown.
  Text I could not read confidently is marked `[?]` (the marker is ignored when scoring). Small text on busy art
  is my weakest area. My draft also skips decorative and logo text, and OCR lines that are not in the draft are
  not penalised (no precision metric).
- Metric (order-independent because pages are multi-column): for every expected line (at least 2 characters
  after removing punctuation and spaces, NFKC, lower-case) take the best match among all OCR lines and pairs of
  consecutive OCR lines; error = Levenshtein distance divided by the expected length, capped at 1. **Character
  accuracy = 1 - total error / total expected characters.** **Line recall** = share of expected lines with
  error at most 0.2. "Missed per 20" = (1 - recall) x 20. There is no one-to-one matching, so repeated labels
  (for example many "buy" buttons) can be credited twice, which is slightly optimistic.

## 3. Results (PROVISIONAL)

### By category

| Variant | Category (images) | Char accuracy | Line recall | Missed / 20 lines |
|---|---|---|---|---|
| v6 small | chat (4) | 97.9% | 136/140 = 97.1% | 0.6 |
| v6 small | profile (10) | 96.8% | 397/414 = 95.9% | 0.8 |
| v6 small | menu (4) | 94.7% | 184/205 = 89.8% | 2.0 |
| v6 small | order card (1) | 95.2% | 106/118 = 89.8% | 2.0 |
| v6 small | **all (19)** | **96.5%** | 823/877 = 93.8% | 1.2 |
| v6 medium | chat | 97.9% | 97.1% | 0.6 |
| v6 medium | profile | 98.9% | 96.4% | 0.7 |
| v6 medium | menu | 98.4% | 96.6% | 0.7 |
| v6 medium | order card | 94.4% | 89.0% | 2.2 |
| v6 medium | **all** | **98.2%** | 838/877 = 95.6% | 0.9 |
| v5 server | chat | 98.4% | 97.9% | 0.4 |
| v5 server | profile | 98.8% | 97.1% | 0.6 |
| v5 server | menu | 98.8% | 97.1% | 0.6 |
| v5 server | order card | 96.8% | 94.1% | 1.2 |
| v5 server | **all** | **98.5%** | 849/877 = 96.8% | 0.6 |

### Speed and memory (whole screenshot per call, 2 threads)

| Variant | Median ms/image | p95 ms/image | Full-screen (~1900x1030, 5 images) range | Peak RAM | Models on disk |
|---|---|---|---|---|---|
| v6 small | 394 | 1608 | 719 to 1608 ms | 344 MB | 30 MB (bundled) |
| v6 medium | 1293 | 6845 | 3.2 to 6.8 s (at 1900 px) | 498 MB | 133 MB |
| v5 server | 1915 | 5920 | 2.9 to 5.9 s | 700 MB | 165 MB |

Cost scales with the number of text lines found (roughly 7-10 ms per line for v6 small), not with pixels. Lines
found per image for v6 small: 3 to 146; a 20-line chat panel took 280-420 ms, a full-screen page with 85-146
lines took 0.7-1.6 s. Medium is 3-4x slower than small on this CPU and v5 server about 4-5x. The denormal issue
(#743) from the earlier report was **not tested** here; the medium slowdown is roughly in line with its size, so
I do not attribute it to #743.

### Per image (provisional character accuracy; ms is v6 small, second run)

| # | File | Category | Size | small | medium | v5 server | small ms | small lines |
|---|---|---|---|---|---|---|---|---|
| 01 | Screenshot 2026-10-30 220641 | chat | 1911x1028 | 99.0 | 99.2 | 99.6 | 1294 | 103 |
| 02 | ... 220647 | profile | 387x442 | 100 | 100 | 100 | 218 | 16 |
| 03 | ... 220653 | menu | 100x108 | 100 | 100 | 100 | 24 | 3 |
| 04 | ... 220658 | profile | 679x541 | 98.5 | 100 | 100 | 369 | 25 |
| 05 | ... 220702 | profile | 439x317 | 100 | 100 | 100 | 267 | 12 |
| 06 | ... 220714 | menu | 1916x1034 | **93.5** | 98.1 | 98.5 | 1608 | 146 |
| 07 | ... 220722 | profile | 1786x1023 | 100 | 99.3 | 99.3 | 719 | 90 |
| 08 | ... 220728 | menu | 400x590 | 100 | 100 | 100 | 260 | 23 |
| 09 | ... 220741 | profile | 1917x1030 | **87.4** | 98.2 | 97.3 | 1014 | 85 |
| 10 | ... 220749 | profile | 596x592 | 98.9 | 100 | 100 | 305 | 19 |
| 11 | ... 220754 | profile | 410x118 | 100 | 100 | 100 | 198 | 5 |
| 12 | ... 220807 | chat | 572x449 | 100 | 100 | 100 | 338 | 15 |
| 13 | ... 220814 | chat | 581x609 | 98.5 | 98.5 | 99.5 | 394 | 17 |
| 14 | ... 220842 | order card | 1919x1036 | 95.2 | 94.4 | 96.8 | 1025 | 97 |
| 15 | ... 220848 | menu | 1076x198 | 100 | 100 | 100 | 166 | 15 |
| 16 | ... 220904 | profile | 1279x923 | 97.8 | 98.7 | 97.8 | 729 | 53 |
| 17 | ... 220916 | profile | 1288x931 | 99.7 | 99.4 | 99.7 | 836 | 69 |
| 18 | ... 220935 | profile | 1271x912 | 96.4 | 97.0 | 97.6 | 869 | 70 |
| 19 | ... 221006 | chat | 1068x670 | **93.2** | 92.7 | 93.2 | 423 | 22 |

(All other file names are `Screenshot 2026-10-30 <time>.png`.)

### Images that scored badly and the likely reasons (no chat text quoted)

- **09 (profile, v6 small 87.4%; medium and server about 98%)**: small drops the sidebar labels on the dark
  nav and the long grey skill-card description lines, tiny text on dark panels. A model-size effect, not a
  segmentation failure. Reason to keep medium as an optional profile, or to upscale such regions.
- **06 (home/listing page, v6 small 93.5%)**: dense small captions over busy artwork, stylised banner and tag
  text, small rating numbers and member counts. Medium and v5 server read most of them.
- **19 (chat, all engines about 93%)**: shared by every model, so partly my draft. The frame contains an
  embedded screenshot of a social post with tiny text, and a draft I was unsure about (marked `[?]`). The
  actual chat bubbles in it were read correctly. Needs user confirmation before it is used as a regression gate.
- **14 (order-card listing, 94-97%)**: small numbers and tags placed over photos, overlay badges, many repeated
  buttons. Worst category for every engine.
- **18 (profile, 96-98%)**: text inside an embedded game-stats screenshot (rendered at a few pixels tall).

Observed failure types across all three engines: missed tiny labels (under about 10 px), digits on photos,
confusion of similar characters in game-stat text, occasional merged or split lines. Chat bubble text itself
was read almost perfectly on every engine.

## 4. Windows.Media.Ocr (candidate 2)

- Checked without admin. `Get-WindowsCapability -Online -Name "Language.OCR*"` **fails unelevated** ("The
  requested operation requires elevation"), so the capability names are still unconfirmed.
- Python/WinRT probe (`tests/bench/winocr_check.py`): `OcrEngine.available_recognizer_languages` = `['en-US']`
  only. `is_language_supported` is False for zh-Hans-CN, zh-Hans, zh-CN, ja and ko.
- Timing only (en-US engine on the 19 images, meaningless text): median 16 ms per image, max 86 ms. Fast, but
  irrelevant for Chinese.
- **Chinese accuracy: NOT MEASURED.** Nothing was installed. To measure it, the user would run once in an
  **elevated** PowerShell (right-click, Run as administrator):

  ```
  Get-WindowsCapability -Online -Name "Language.OCR*" | Select Name,State
  Add-WindowsCapability -Online -Name "Language.OCR~~~zh-CN~0.0.1.0"
  ```

  Use the exact name shown by the first command if it differs (the `zh-CN` form follows the documented
  pattern but is unverified). Then re-run `tests/bench/winocr_check.py` with a zh-Hans-CN engine.

## 5. Gate check (PROVISIONAL)

| Gate | Result | Verdict |
|---|---|---|
| >= 95% character accuracy on chat lines | chat (4 images): small 97.9%, medium 97.9%, server 98.4%. One chat image (19) is about 93% for every engine and is probably partly a draft problem. | **Met** for all three, on a small sample |
| At most 1 missed line per 20 | chat: 0.4-0.6 per 20 for all variants. Across all 19 images: small 1.2, medium 0.9, server 0.6. Menu/order-card pages with small text: small 2.0 | **Met on chat**; v6 small **not met** on menu and order-card pages |
| 720p frame p95 under 400 ms, 2 threads | v6 small p95 1608 ms, median 394 ms. Chat-sized crops (15-22 lines): 338-423 ms. Full-screen pages: 0.7-1.6 s. Medium and server: seconds | **Not met** on whole full-screen frames, even on this fast CPU. Borderline for chat-panel-sized regions |

Reading of the latency result (my interpretation, not a measured fix): the budget is driven by the number of text
lines per call, so the pipeline must OCR only the changed region (the chat panel) and skip static frames, as
SPEC section 2.1 plans, and the gate should be re-defined per region size rather than per full 720p frame. On a
4-8 core CPU the numbers above would be about 2-4x worse. The 400 ms gate on a full 720p frame of a dense page
is not achievable with v6 small on this evidence.

Idle and active CPU percentages were not measured in this pass.

## 6. Conclusions and open questions

1. v6 small is accurate enough for chat on this sample and is the only one fast enough for any live use. Medium
   adds about 1.7 points overall for 3-4x the time; v5 server is slower and larger than medium for the same
   score, so there is no reason to prefer it. (Note: `SUMMARY.md` corrects the "same score" claim; v5 server scored
   slightly higher than medium here. Also, with only 19 images, "p95" in the tables is effectively the slowest image.)
2. Menu, order-card and dense pages are where small is weakest (89-90% line recall). That matches the
   "optional high-accuracy profile" idea for medium, used on demand rather than per frame.
3. The user must confirm the drafts in `tests/golden/expected/_draft/` (especially image 19 and every `[?]`
   line) before any figure here is treated as real.
4. Not measured: Windows OCR on Chinese (needs admin), idle/active CPU, Korean and Japanese (the screenshots are
   Chinese only), behaviour on a 4-8 core CPU.

## 7. Windows.Media.Ocr with the Chinese pack installed (PROVISIONAL, added later)

The user installed the Windows Chinese OCR pack. Probe (`tests/bench/winocr_check.py`): `available_recognizer_languages`
= `['en-US', 'zh-Hans-CN']`; zh-Hans-CN, zh-Hans and zh-CN supported; ja and ko **not** supported (Chinese only).
Harness: `tests/bench/winocr_bench.py` (zh-Hans-CN engine, whole screenshot per call, median of 3 after a warm-up,
same 19 images, same drafts, same scorer `ocr_score.py`). Still scored against unconfirmed drafts.

Engine limit: `OcrEngine.max_image_dimension` = 10000 px per side. The largest image is 1919 px, so the limit did
not affect any result (no resizing, no skipped images). The API returns no confidence scores and no per-character
boxes in this harness, so text-score filtering is not possible; words are space-separated and it often joins
neighbouring columns into one long line (the scorer's windowed matching tolerates this).

| Engine | Category (images) | Char accuracy | Line recall | Missed / 20 lines |
|---|---|---|---|---|
| Windows OCR zh | chat (4) | 70.0% | 79/140 = 56.4% | 8.7 |
| Windows OCR zh | profile (10) | 81.0% | 262/414 = 63.3% | 7.3 |
| Windows OCR zh | menu (4) | 80.8% | 133/205 = 64.9% | 7.0 |
| Windows OCR zh | order card (1) | 63.9% | 48/118 = 40.7% | 11.9 |
| Windows OCR zh | **all (19)** | **77.0%** | 522/877 = 59.5% | 8.1 |
| RapidOCR v6 small (for reference) | all / chat | 96.5% / 97.9% | 93.8% | 1.2 |

Speed (same machine, no model load): median **51 ms per image**, slowest **171 ms** (a 1916x1034 full-screen
page); chat images 47-125 ms. About 8x faster than v6 small (394 ms median, 1608 ms slowest). Memory was not
measured; the engine runs inside the OS.

Verdict (provisional): speed is excellent but accuracy is far below the gates (>= 95% on chat, <= 1 missed line
per 20). Worst on chat images (one chat image at 51%, two at 61-66%). Not a credible quality fallback to RapidOCR
v6 small. Possible use: a fast, low-quality "something changed" detector or last-resort offline mode, flagged
as low confidence. Not measured: ja/ko (not installed), upscaling before OCR (might help small text; untested).
