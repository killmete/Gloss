# OCR engine research (Phase 1)

Checked: 2026-10-30. Machine for local numbers: AMD Ryzen 9 9950X3D (16 cores), 15 GB RAM shown by
WMI, Windows 11 26300, CPython 3.12.16 (uv-managed), onnxruntime CPU only. This is a FAST desktop CPU.
SPEC targets a 4-8 core CPU, so treat absolute ms as a best case.

## 1. Question and decision it informs

Which OCR engine(s) should the `OCREngine` port use as primary and fallback for Gloss (CPU-only,
offline, lightweight, no full PyTorch), reading stylised Chinese game UI and chat bubbles, with
Japanese and Korean as secondary languages? Which two candidates go into the local benchmark on the
user's real screenshots? Feeds: ADR on `[ocr] engine/fallbacks`, packaging size, privacy mode.

IMPORTANT LIMIT OF THIS REPORT: I was told not to open `tests/golden/images/`, so **no accuracy number
below comes from real game screenshots**. The accuracy numbers are from small synthetic images I
rendered myself (clean text and a degraded copy). They only show relative behaviour and failure
modes. They are NOT evidence of real-world accuracy. Section 5 gives the plan for the real benchmark.

## 2. Options compared

| Option | Maturity / maintenance | Licence | Install size (Win, py3.12) | CPU speed | CJK accuracy (evidence level) | Windows quirks | Python ergonomics |
|---|---|---|---|---|---|---|---|
| **RapidOCR 3.10.0** (PP-OCR models on ONNX Runtime) | Very active: PyPI 3.10.0 on 2026-10-08, pushed 2026-10-30, 8.4k stars, 18 open issues | Apache-2.0 (models too, see MODEL_LICENSES.md) | ~280 MB site-packages for the whole env (opencv 113, onnxruntime 48, numpy 55, Pillow 16, rapidocr 32); default PP-OCRv6-small models (30 MB) are bundled in the wheel | 12 text lines at 1280x720: 130-250 ms (tiny/small), 440 ms (medium/server) on 16-core Zen 5 | Best available local option; published PaddleOCR numbers, plus my synthetic tests (see 4) | None found: pure wheels, no DLL hell seen. Other models auto-download from ModelScope on first use (must bundle for offline) | Excellent: `RapidOCR()(ndarray)` returns boxes, texts, scores |
| **Windows.Media.Ocr** via `winrt-Windows.Media.Ocr` (or `winocr` wrapper) | OS API since Win10 10240; Microsoft marks it "legacy" next to the NPU API; `winocr` 0.0.15 last released 2024-10-25 (stale), `winrt-*` 4!10.0.28000.2705 current | MIT wrappers; OS component | ~31 MB of Python wheels (venv w/ Pillow); zero model size, but language packs are OS features | 19 ms for 15 English lines at 1280x720 (en-US only, measured) | Unknown for zh/ja/ko: language packs not installed on my PC, so I could NOT test. No evidence found online either | `zh`, `ja`, `ko` are NOT available by default (measured: only `en-US` here; `is_language_supported` false for zh-Hans-CN, ja, ko). Needs admin to add packs (`Add-WindowsCapability`). One language per engine instance, no auto-detect. | Good: async WinRT, needs a SoftwareBitmap built from bytes |
| **Tesseract 5.5.x** (`pytesseract` 0.3.13) | Alive: 5.5.4 shown as latest on GitHub releases (28 Oct, year not shown on page); pytesseract last release 2024-08-16 | Apache-2.0 | Needs external `tesseract.exe` install (not pip); `tessdata_fast` chi_sim 2.4 MB, jpn 2.5 MB, kor 1.7 MB | ~0.16-0.45 s per page in generic English tests; not measured by me | Weakest on Chinese in the only numbers I found (76.5% vs 92.7% PaddleOCR, but that blog gives no test set; treat as anecdote). Known for inserting spaces between CJK characters | Separate binary + PATH, subprocess per call | Poor fit: external process, no good small-text handling |
| **PaddleOCR 3.7.0** (full) | Very active (3.7.0 on 2026-06-11); PP-OCRv6 is released from here | Apache-2.0 | `paddleocr` pulls `paddlex[ocr-core]`; also needs `paddlepaddle` (cp312 win wheel was 97 MB for 3.0.0, latest not verified) | Own table, Xeon 8350C: v6 small 0.59-0.79 s/img, tiny 0.20-0.32 s, medium 1.4-3.3 s | Same models as RapidOCR, so same accuracy | Heavy dependency tree, historic Windows/oneDNN issues (not re-verified) | Heavier than RapidOCR for identical models |
| **EasyOCR** | Not re-verified today | Apache-2.0 | Requires PyTorch (not re-checked today, but well known) | Slower than RapidOCR in a March 2026 test (0.66 s vs 0.21 s, other hardware) | Lower than PaddleOCR family in the sources I saw | Torch DLLs | Rejected by the "no full PyTorch" rule |
| **Windows AI TextRecognizer** (Windows App SDK, "Snipping Tool-class" OCR) | New API, doc updated 2026-10-01 | OS | Model delivered by Windows | Not measured | Microsoft says better than OcrEngine | **Runs exclusively on devices with an NPU** (Copilot+ PCs). No NPU on the user's PC to my knowledge; C#/C++ projection, no Python story found | Not usable as a general fallback |
| **Snipping Tool "Text Actions" OCR** | Shipped 2023 (v11.2308.33.0) | App | n/a | n/a | n/a | GUI-only. Searches found no CLI, API or scripting hook. | Cannot be wrapped. Only the platform OCR underneath is reachable, and that is Windows.Media.Ocr |
| **LLM vision (Claude Haiku-class)** | GA; `claude-haiku-5-5` listed as vision-capable | Commercial API | None | Network round trip, seconds | Likely strongest on stylised text and context, but NOT measured by me | Sends screenshots off the PC, which breaks the privacy default | Easy, but cost and privacy trade-offs (section 3) |

## 3. Evidence

### RapidOCR and the model family
- PyPI `rapidocr` **3.10.0**, released 2026-10-08, requires Python `<4,>=3.8`, licence Apache-2.0.
  Deps: pyclipper, opencv-python-headless, numpy, six, Shapely, PyYAML, Pillow, tqdm, omegaconf,
  requests, colorlog. **`onnxruntime` is NOT a declared dependency**, you must add it.
  https://pypi.org/pypi/rapidocr/json (checked 2026-10-30)
- Repo: pushed 2026-10-30, 8,391 stars, 18 open issues, Apache-2.0.
  https://api.github.com/repos/RapidAI/RapidOCR (2026-10-30). Engines: ONNX Runtime, OpenVINO, MNN,
  Paddle, TensorRT, PyTorch. https://github.com/RapidAI/RapidOCR
- Model families in `default_models.yaml`: PP-OCRv4, v5 and **PP-OCRv6 (tiny, small, medium)**, hosted on
  ModelScope under release v3.10.0. v6 small/medium cover about 52 language codes including
  `ch`, `chinese_cht`, `en`, `japan`; v6 tiny excludes Japan; **Korean is not in v6** (my run:
  `ValueError: Unsupported rec.lang_type='korean' for PP-OCRv6 small`). Korean exists as v4 and v5 mobile
  recognisers. https://raw.githubusercontent.com/RapidAI/RapidOCR/main/python/rapidocr/default_models.yaml
- Model list page: Japanese = v4 mobile, v5 ch/ja/en mix (mobile+server), v6 small/medium; Korean = v4
  and v5 mobile only. https://rapidai.github.io/RapidOCRDocs/main/model_list/
- **The 3.10.0 wheel ships PP-OCRv6 small det+rec and the v2.0 cls model inside the package**
  (`rapidocr/models/*.onnx`, I saw them after install) and default config is `ocr_version: PP-OCRv6`,
  `model_type: small`. So the default install works offline. Any other model is downloaded on first use
  (my runs fetched v6 tiny/medium, v5 mobile/server, v4/v5 Korean automatically).
- PP-OCRv6 released with PaddleOCR 3.7.0 on 2026-06-11: tiny 1.5M params, small 7.7M, medium 34.5M.
  Medium reports +4.6 det Hmean and +5.1 rec accuracy over PP-OCRv5_server on Paddle's own benchmark
  (vendor numbers, not independent). Official guidance: Small for CPU document systems, Tiny for embedded.
  https://github.com/PaddlePaddle/PaddleOCR/releases/tag/v3.7.0 ,
  https://www.paddleocr.ai/latest/en/version3.x/algorithm/PP-OCRv6/PP-OCRv6.html ,
  https://arxiv.org/html/2606.13108
- Paddle's published ONNX Runtime CPU latency per image on Xeon 8350C: medium 3.31 s, small 0.61 s,
  tiny 0.22 s. Game UI is not among the scenarios they list (they list digital displays, artistic
  fonts, rotated text...). Same PaddleOCR page.
- Independent write-up of v6-small in RapidOCR 3.9.1: 12/14 strings exact on one synthetic JP/EN image,
  ~0.84 s on an M4 Max, and an example of a confident mis-read (取扱注意 read as 取极注意, score 0.883).
  Single image, not a benchmark. https://dev.to/kiarina/testing-japanese-and-english-ocr-with-pp-ocrv6-small-and-rapidocr-39pd
- Open RapidOCR issue #743 (2026-09-30, milestone v3.11.0 due 2026-10-31): PP-OCRv6 `rec_medium` and
  `det_small` ONNX files hold ~200k float32 denormal weights; the reporter says this makes CPU inference
  4-11x slower on their machine. https://github.com/RapidAI/RapidOCR/issues/743 . **My probe did not
  reproduce it** (ORT `session.set_denormal_as_zero=1` changed small 171->185 ms, medium 446->407 ms on
  Zen 5). It may be CPU-specific (likely Intel); re-check on the user's actual CPU.
- Other open-issue themes: tiny/single-character crops fail or get rotated, upscaling small text helps
  (issue of 2024-11-04), no max/min input-size controls, DirectML crashes with multi-threading (2024-12), OOM on
  large images. https://github.com/RapidAI/RapidOCR/issues . These match what I saw (below).

### Windows.Media.Ocr
- `OcrEngine` API: `AvailableRecognizerLanguages`, `MaxImageDimension`, `IsLanguageSupported`,
  `TryCreateFromLanguage`, `TryCreateFromUserProfileLanguages`; results are lines then words with
  bounding boxes. Introduced Win10 10240. https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine
  (page updated 2025-11-21). On my PC `MaxImageDimension` = 10000.
- Language packs are Windows capabilities installed as admin, e.g.
  `Add-WindowsCapability -Online -Name "Language.OCR~~~ja-JP~0.0.1.0"`; list with
  `Get-WindowsCapability -Online -Name "Language.OCR*"`. https://github.com/GitHub30/winocr (README).
  I could not list capabilities here (needs elevation). The exact zh name is probably
  `Language.OCR~~~zh-CN~0.0.1.0` by pattern, **unverified**.
- Python wrappers: `winocr` 0.0.15 (2024-10-25, MIT per classifier) sits on `winrt-windows-*`;
  `winrt-Windows.Media.Ocr` version `4!10.0.28000.2705` requires Python >=3.11, MIT.
  https://pypi.org/pypi/winocr/json , https://pypi.org/pypi/winrt-Windows.Media.Ocr/json
- Microsoft says the new Windows App SDK text recognition runs **exclusively on devices with an NPU** and
  is "faster and more accurate than the legacy Windows.Media.Ocr.OcrEngine".
  https://learn.microsoft.com/en-us/windows/ai/apis/text-recognition (updated 2026-10-01)
- Snipping Tool Text Actions: only GUI sources found (Win+Shift+S then Text Actions button);
  no API found. https://www.ghacks.net/2023/09/15/windows-11s-snipping-tool-is-getting-a-text-recognition-feature-called-text-actions/

### Tesseract / PaddleOCR / others
- Tesseract GitHub releases page: latest 5.5.4 (28 Oct), 5.5.3 (24 Jul), 5.5.2 (26 Dec); years not
  shown. https://github.com/tesseract-ocr/tesseract/releases . `tessdata_fast` is Apache-2.0, LSTM-only,
  has chi_sim (2,469,156 B), chi_tra, jpn (2,471,260 B), kor (1,677,415 B).
  https://api.github.com/repos/tesseract-ocr/tessdata_fast/contents/
- `pytesseract` 0.3.13 (2024-08-16) is only a wrapper: external `tesseract` binary must be on PATH.
  https://pypi.org/pypi/pytesseract/json
- Tesseract inserts spaces between CJK characters (tesseract issue #991, OCRmyPDF issue #1041), which
  needs post-processing. https://github.com/tesseract-ocr/tesseract/issues/991
- Codesota March 2026 invoice test: RapidOCR ~0.21 s, Tesseract ~0.16 s, EasyOCR ~0.66 s,
  PaddleOCR ~4.85 s (some figures older); accuracy: PaddleOCR best, Tesseract second. Note the site's
  own warning that these comparisons are not apples-to-apples. Weak evidence.
  https://codesota.com/ocr/best-for-python
- `paddleocr` 3.7.0 on PyPI (2026-06-11), Apache-2.0, depends on `paddlex[ocr-core]`.
  https://pypi.org/pypi/paddleocr/json . `paddlepaddle` cp312 win_amd64 wheel: 97.1 MB (3.0.0),
  81.1 MB (2.6.2); latest 3.3.1 size not visible. https://pypi.org/pypi/paddlepaddle/json

### LLM vision fallback
- Anthropic vision docs: cost is ceil(w/28) x ceil(h/28) tokens per image; 1920x1080 on the
  standard tier is downsized to 1456x819 = 1,560 tokens; on Claude 4.7+ high-res tier 2,691 tokens.
  Caution: "may hallucinate or make mistakes with low-quality, rotated or very small images";
  heavy JPEG compression makes text hard to read.
  https://platform.claude.com/docs/en/build-with-claude/vision (2026-10-30)
- Pricing page: Claude Haiku 5.5 $0.10 / MTok input, $0.50 / MTok output (prompts up to 100k tokens);
  Haiku 4.5 $1 / $5. https://platform.claude.com/docs/en/about-claude/pricing (2026-10-30).
  Arithmetic (mine): a 1,560-token frame costs about $0.00016 input on Haiku 5.5, so cost is small per
  call, but a 4 fps loop of frames is not. Latency and the privacy rule are the real blockers, not price.
- Privacy: CLAUDE.md rule 7 says only text leaves the PC unless the user opts in to an image mode. An
  LLM-vision OCR path sends screenshots (including other people's chats and possibly personal data), so
  it must be an explicit, per-session opt-in, never a silent fallback.

## 4. Local measurements

Scripts: `docs/research/scratch/ocr_synth.py` (generator), `ocr_bench_rapid.py`, `ocr_bench_winocr.py`,
`ocr_denormal_probe.py`, `ocr_threads_probe.py`, `ocr_dbg_ko.py`. Raw: `docs/research/data/ocr_rapid_synth_all.json`.
Venv created with `python -m venv` inside the session scratchpad because `uv` is not on this PC's PATH
(nothing installed globally). Install used `rapidocr==3.10.0`, `onnxruntime 1.31.1`,
`opencv-python-headless 5.0.0.93`, `numpy 2.5.4`.

Method: 9 synthetic snippets rendered with Microsoft YaHei, SimSun, Yu Gothic and Malgun Gothic: green and
blue chat bubbles, dark order card, yellow outlined text on a gradient banner, 12 px grey UI text,
mixed Latin/CJK name, menu row, one Japanese and one Korean line. "Degraded" = Gaussian blur 0.6, 0.8x
downscale, JPEG q35. Metric = character error rate (CER, NFKC, punctuation/space removed). 1280x720 fake
screen with 12 lines for timing, median of 8 runs after warm-up. All engines on onnxruntime CPU,
default threads. **Synthetic, easy, and font-rendered, so clean-text CER 0 is expected and says little.**

| Variant (all det limit_type=max, 960) | Clean zh CER (7 imgs) | Degraded zh CER | Japanese clean / degraded | 720p, 12 lines | One short line | RSS |
|---|---|---|---|---|---|---|
| v6 **tiny** (det 1.7 + rec 4.3 MB) | 0.277 | 0.376 | 0.76 / 0.81 | **127 ms** | 28 ms | 145 MB |
| v6 **small** (9.5 + 20.3 MB) | 0.000 | 0.061 | 0.0 / 0.0 | **196 ms** | 54 ms | 162 MB |
| v6 **medium** (59 + 73 MB) | 0.000 | **0.034** | 0.0 / 0.0 | 437 ms | 81 ms | 275 MB |
| v5 mobile (4.6 + 15.9 MB) | 0.095 | 0.149 | 0.0 / 0.43 | 230 ms | 64 ms | 165 MB |
| v5 server (84 + 81 MB) | 0.000 | 0.083 | 0.05 / 0.05 | 458 ms | 92 ms | 313 MB |
| v6 small, **default config** (limit_type=min, 736) | 0.000 | **0.571** | 0.0 / 0.0 | 246 ms | **290 ms** | 158 MB |

Findings:
1. **Det limit_type matters a lot for small crops.** RapidOCR's default is `limit_type: min,
   limit_side_len: 736`, which upscales the short side of any small crop to 736 px. A one-line crop took
   290 ms (vs 54 ms with `max`/960) and on blurred/downscaled bubbles the detector returned nothing
   ("text detection result is empty", CER 1.0 on 4 of 7). With `limit_type=max` those cases read.
   Consequence: the OCR adapter must set det limits deliberately and OCR whole dirty regions, not tiny
   crops. This echoes upstream issues about tiny crops and missing size limits.
2. **v6 tiny is not good enough** (misses grey 12 px text and the menu row entirely). Do not use as primary.
3. **v6 small is the sweet spot on this synthetic set**; medium helped only a little (degraded CER 0.034 vs
   0.061) at 2.2x the time and 1.7x the RAM; v5 mobile was clearly worse than v6 small on mixed name and
   blurred Japanese. Direction is plausible but needs real data to confirm.
4. **Japanese:** the default Chinese-label v6/v5 recogniser read my Japanese line without a Japanese
   model (CER 0 for v6 small/medium). Setting `Rec.lang_type=japan` gave the same result. Needs real data.
5. **Korean is the weak spot:** v6 has no Korean model; with the `ch` recogniser the line is garbage (CER 1.0).
   The v5 mobile Korean recogniser reads Hangul well (confidence 0.999) but under the default det limits
   it only found 3 of the 4 words as separate "lines", and with `limit_type=max` the detector returned
   nothing on that 273x39 crop (probe `ocr_dbg_ko.py`). One synthetic image is not proof, but treat Korean
   as "needs a dedicated recogniser and detector tuning".
6. **Threads:** (v6 small, 720p) 1 thread 328 ms, 2 threads 217 ms, 4 threads 217 ms, 8 threads 179 ms. CPU
   time per call balloons with more threads (small: 494 ms at 1 thread, 1,662 ms at 8) because ORT spins.
   Use 2 intra-op threads (`EngineConfig.onnxruntime.intra_op_num_threads: 2`) to protect the 25% CPU
   budget. See `ocr_threads_probe.py`. (process_time includes spin-wait; indicative only.)
7. **Windows.Media.Ocr** (en-US only): 19 ms for 15 lines on 1280x720, 30 lines returned. Chinese,
   Japanese and Korean engines could not be created on this PC because no pack is installed:
   `is_language_supported` false for zh-Hans-CN, ja, ko. **Its CJK accuracy is therefore unmeasured.**
8. **Install size** (RapidOCR env, before extra models): opencv-python-headless 113 MB (the largest piece,
   unusually big for version 5.0.0.93, a pinned 4.x might be smaller, not tested), onnxruntime 48 MB,
   numpy 34 + 21 MB libs, Pillow 16 MB, rapidocr 32 MB incl. bundled v6-small models. About 280 MB unpacked
   for everything. Windows OCR path: about 31 MB of Python wheels, no models.
9. Denormal issue #743: not reproduced on Zen 5 (see Evidence).

## 5. Top 2 candidates for the real-screenshot benchmark

Both are free of PyTorch, CPU-only, and run entirely offline. Run against the user's screenshots in
`tests/golden/images/` by the test-engineer (I did not open them). Use `uv` (user's machine has it
through the project, `uv run --with` never touches the global Python).

### Candidate A: RapidOCR (PP-OCRv6 small, with medium and v5 server as comparison variants)
PyPI: `rapidocr==3.10.0`, `onnxruntime==1.31.1` (CPU wheel; do NOT also install `onnxruntime-gpu`),
`pillow`. Opencv-headless, numpy, shapely etc. come in as dependencies.

```
uv run --python 3.12 --with rapidocr==3.10.0 --with onnxruntime==1.31.1 --with pillow --with psutil python docs/research/scratch/ocr_bench_rapid.py v6_small
```
(`ocr_bench_rapid.py` argument filters variant names; it runs the synthetic set. For real screenshots
swap `cases()` for image paths and a hand-typed expected-text file.) Minimal use:
```python
import numpy as np
from rapidocr import RapidOCR, ModelType as M

eng = RapidOCR(
    params={
        "Det.limit_type": "max",
        "Det.limit_side_len": 960,
        "EngineConfig.onnxruntime.intra_op_num_threads": 2,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        "Global.text_score": 0.5,
        "Global.log_level": "warning",
    }
)
r = eng(np_bgr_image)  # BGR ndarray (cv2 order)
# r.boxes (N,4,2), r.txts, r.scores ; r is None-like when empty (txts is None)
```
Variants to compare (add `Det.ocr_version`, `Det.model_type`, `Rec.ocr_version`, `Rec.model_type`,
`Rec.lang_type` using the enums `OCRVersion`, `ModelType`, `LangRec` from `rapidocr`, plain strings are
rejected with "must be Enum Type"): v6 small (bundled), v6 medium, v5 server, and for Hangul
`Rec.ocr_version=PP-OCRv5, Rec.model_type=mobile, Rec.lang_type=korean`.

### Candidate B: Windows.Media.Ocr (local OS engine, zero model download)
PyPI: `winocr==0.0.15` (pulls `winrt-Windows.Media.Ocr==4!10.0.28000.2705` and friends), `pillow`.
Or depend on the `winrt-*` packages directly and skip `winocr` (it is stale since 2024-10).

One-time, **in an elevated PowerShell** (needed on this PC, only en-US present). Confirm the exact
names first, then install the pack(s) for Chinese, Japanese, Korean:
```
Get-WindowsCapability -Online -Name "Language.OCR*" | Select Name,State
Add-WindowsCapability -Online -Name "Language.OCR~~~zh-CN~0.0.1.0"
Add-WindowsCapability -Online -Name "Language.OCR~~~ja-JP~0.0.1.0"
Add-WindowsCapability -Online -Name "Language.OCR~~~ko-KR~0.0.1.0"
```
(`ja-JP` form is from the winocr README; the zh/ko names follow the pattern but are unverified.)
Run:
```
uv run --python 3.12 --with winocr==0.0.15 --with pillow python docs/research/scratch/ocr_bench_winocr.py
```
then extend the script with `Language("zh-Hans-CN")` etc. Windows picks one language per engine, so for
mixed Chinese/English chat try zh first; engines can be created per language and results merged.

Why these two: A is the only mature, maintained, accurate-enough, offline, no-Torch option and the
planned primary. B is the only option that differs fundamentally (OS engine), costs no size, and is
about 10x faster, so it is the only plausible fast-path or fallback. PaddleOCR proper uses the same models
as A with a heavier stack. Tesseract is a possible third control but is likely weaker on stylised CJK.

## 6. Recommendation

**Primary: RapidOCR 3.10.0 + onnxruntime CPU, default PP-OCRv6 *small* models bundled in the build**
(about 30 MB of models). Reasons: wheel already ships the models so offline works with no download;
0.2 s for a busy 720p frame on a fast CPU, on target; Apache-2.0; most actively maintained option;
Chinese+Japanese covered by one recogniser. Specifics for the adapter:
- Set `Det.limit_type=max`, `limit_side_len` about 960, two ORT intra-op threads.
- OCR dirty *regions* of useful size (pad crops; avoid sub-40 px tall crops) rather than tiny strips.
- Put `ocr_version`, `model_type`, `lang_type`, limits and `min_confidence` in config, not in code.
- Models must be bundled or pre-downloaded into the app folder (`model_path`/`model_dir`); never rely on
  the ModelScope auto-download at runtime. Hash-pin them in the update process.
- Add a Korean path only if the user needs it: a second `RapidOCR` instance with the v5 mobile Korean
  recogniser (12.9 MB), selected by script detection or user setting. Verify on real Korean first.
- Keep `v6 medium` as an optional "high accuracy" profile if the real benchmark shows it matters.
  Cost: 133 MB models, 2.2x slower, 275 MB RSS. Check issue #743 and the 3.11.0 release before enabling.

**Fallback: Windows.Media.Ocr** through `winrt-Windows.Media.Ocr`, registered as the `windows` adapter
(matches the SPEC config `fallbacks = ["windows"]`). It is the cheap, fast, no-model fallback when
RapidOCR fails to load, AND potentially a quick-first-pass engine. It must raise `AdapterUnavailable`
when the needed language pack is missing, and the settings screen should tell the user the exact
PowerShell command. Do not promote it until the real-screenshot benchmark proves CJK accuracy.

**LLM vision:** not an OCR engine in the default pipeline. Offer only as an opt-in "image mode" for cases
that local OCR fails (e.g. a user-drawn region on a stylised banner), with a visible per-use indicator,
a downscale to the model's limit, and PNG not JPEG. The text-only default stays.

Decision gate for the real benchmark (suggested thresholds for the architect/user, not set by me):
character accuracy on chat lines >= 95%, no more than 1 missed line per 20 on scrolling chat, 720p
frame p95 < 400 ms on the user's CPU with 2 threads.

## 7. Rejected

- **Tesseract / pytesseract**: needs an external binary, weaker on CJK in the little evidence found,
  space-between-characters artefacts, no gain over RapidOCR on any axis except 20-30 ms speed.
- **Full PaddleOCR + paddlepaddle**: same models as RapidOCR with a much heavier install; no benefit for
  an app that wants small and stable.
- **EasyOCR**: requires PyTorch (project rule), slower than RapidOCR in the test I saw.
- **Windows AI `TextRecognizer` (Windows App SDK)**: NPU-only per Microsoft; no Python projection found;
  cannot be a general fallback.
- **Snipping Tool OCR**: no programmatic interface; the only thing reachable is the engine underneath it,
  which is candidate B.
- **PP-OCRv6 tiny as primary**: fastest and smallest (6 MB), but it missed small UI text and menu rows in
  my synthetic test and has no Japanese.
- **LLM vision as the default OCR**: breaks the "only text leaves the PC" default, adds network latency
  to every frame, may hallucinate on small text per Anthropic docs, and bills per frame.
- **`winocr` 0.0.15 as a hard dependency**: stale (2024-10), thin wrapper; prefer direct `winrt-*`.

## 8. Risks and unknowns

- **No real-screenshot accuracy yet.** All accuracy claims are synthetic. Mitigation: benchmark in section
  5, then freeze thresholds in golden tests. Real game UI may include text on busy art backgrounds that
  my synthetic banner did not capture.
- **Small-crop fragility** (empty detection, upscale blowup) is real and config-dependent. Mitigation:
  region padding, limit settings in config, and a golden test of "single chat line in a small region".
- **Korean support is the weakest link** (no v6 model; v5 detector flakiness on my one probe).
- **Windows OCR on CJK is unmeasured**, requires admin to install packs, and the user's Windows has only
  en-US. A PC-wide language pack install cannot be done silently by the app.
- **Model/ORT supply chain:** the ORT wheel is 48 MB and RapidOCR changes models between minor releases
  (v6 default arrived recently). Pin `rapidocr`, `onnxruntime` and model hashes; run goldens before bumping
  (docs/UPDATING.md). Open issue #743 may change the v6 model files in 3.11.0.
- **onnxruntime is not a declared dependency of rapidocr**: add it explicitly in `pyproject.toml` and
  make sure no GPU variant gets pulled in.
- **Packaging size:** opencv (113 MB) dominates; test PyInstaller/Nuitka output size in Phase 5, and try
  an older `opencv-python-headless` pin. Not tested.
- **CPU load:** ORT thread spinning measured above; keep 2 threads and rely on tile-change detection
  to skip static frames (SPEC section 2.1).
- **Time-sensitive:** all versions above were verified 2026-10-30 and RapidOCR 3.11.0 is due 2026-10-31.
- **Machine bias:** my CPU is far faster than the SPEC target; the 130-250 ms figures could be 2-4x
  slower on a 4-core laptop.

## 9. Sources

- https://pypi.org/pypi/rapidocr/json
- https://api.github.com/repos/RapidAI/RapidOCR , https://github.com/RapidAI/RapidOCR
- https://github.com/RapidAI/RapidOCR/issues , https://github.com/RapidAI/RapidOCR/issues/743
- https://rapidai.github.io/RapidOCRDocs/main/model_list/
- https://raw.githubusercontent.com/RapidAI/RapidOCR/main/python/rapidocr/default_models.yaml
- https://github.com/PaddlePaddle/PaddleOCR/releases/tag/v3.7.0
- https://www.paddleocr.ai/latest/en/version3.x/algorithm/PP-OCRv6/PP-OCRv6.html
- https://arxiv.org/html/2606.13108
- https://dev.to/kiarina/testing-japanese-and-english-ocr-with-pp-ocrv6-small-and-rapidocr-39pd
- https://pypi.org/pypi/paddleocr/json , https://pypi.org/pypi/paddlepaddle/json , https://pypi.org/pypi/onnxruntime/json
- https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine
- https://learn.microsoft.com/en-us/windows/ai/apis/text-recognition
- https://pypi.org/pypi/winocr/json , https://pypi.org/pypi/winrt-Windows.Media.Ocr/json , https://github.com/GitHub30/winocr
- https://www.ghacks.net/2023/09/15/windows-11s-snipping-tool-is-getting-a-text-recognition-feature-called-text-actions/
- https://github.com/tesseract-ocr/tesseract/releases , https://github.com/tesseract-ocr/tesseract/issues/991
- https://github.com/tesseract-ocr/tessdata_fast , https://pypi.org/pypi/pytesseract/json
- https://codesota.com/ocr/best-for-python
- https://platform.claude.com/docs/en/build-with-claude/vision
- https://platform.claude.com/docs/en/about-claude/pricing
- Local: `docs/research/scratch/ocr_*.py`, `docs/research/data/ocr_rapid_synth_all.json`
