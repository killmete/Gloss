# Prior art: screen-OCR translators

Checked 2026-10-30. Researcher: Phase 1.

## Question and decision it informs

What do existing screen translators (Translumo, LunaTranslator, ScreenTranslator,
PowerToys Text Extractor) do well, where do they hurt their users, and what should Gloss
copy or avoid? Feeds: SPEC section 2 (pipeline, TextTracker, renderers), section 5
(reliability) and the Phase 2 contract design.

## Options compared

| | Translumo | LunaTranslator | ScreenTranslator | PowerToys Text Extractor |
|---|---|---|---|---|
| Purpose | Live game/subtitle translator | Visual-novel/game translator (text hooking first, OCR second) | Hotkey, pick region, OCR, translate popup | One-shot OCR to clipboard (no translation) |
| Repo / stars | ramjke/Translumo, 5.9k | HIllya51/LunaTranslator, 14.0k | OneMoreGres/ScreenTranslator, 1.3k | microsoft/PowerToys, 140k (whole suite) |
| Licence | Apache-2.0 | GPL-3.0 | MIT | MIT |
| Last push | 2026-09-22 | 2026-10-30 (daily) | 2026-09-02 (author says "almost abandoned") | 2026-10-30 |
| Open issues | 76 | 42 | 63 | 7,923 suite-wide; 76 labelled Text Extractor |
| Language / UI | C# / .NET 8, WPF-style overlay | C++ plus Python, Qt | C++ / Qt | C++/C# (Text Grab code base) |
| OCR | Windows OCR (recommended), EasyOCR (Nvidia GPU, optional, installs Python+PyTorch) | Built-in model plus many online/offline engines, Windows OCR | Tesseract + Leptonica (language packs downloaded separately) | Windows.Media.Ocr (needs OCR language packs) |
| Translation | Google (no key), DeepL, Yandex | Almost everything, incl. LLM and offline | Online services, third-party scripts for AI services | None |
| Capture | DirectX capture (`ScreenDXCapturer`, error code seen in logs) | Several | Qt grab of a user-selected region | Full-screen freeze plus drag-select |
| Overlay | Separate translation window over borderless/windowed game | Floating window, Magpie scaling integration | Popup near region | Selection overlay only |
| Install | Zip, unzip and run; EasyOCR needs 5 GB+ and a GPU | Zip/portable; GPL; signed via SignPath | Zip or AppImage, downloads language data in settings | Part of PowerToys installer |

Sources for every cell are in the Evidence section.

## Evidence

### Translumo
- README: Windows OCR is the recommended engine; EasyOCR needs an Nvidia GPU with CUDA 11.8,
  8 GB RAM and 5 GB of disk; several OCR engines can run at once and a model picks the best
  result; fullscreen is not supported, borderless or windowed only; "Failed to capture
  screen" is fixed by making the target window active or restarting; other apps may
  intercept hotkeys; EasyOCR install may fail. Hotkeys: Alt+Q select area, `~` start,
  Alt+T toggle window. Checked 2026-10-30.
  <https://github.com/ramjke/Translumo>
- Repo metadata (GitHub API, 2026-10-30): 5,886 stars, Apache-2.0, last push 2026-09-22,
  76 open issues, language C#.
- Issue #20 and #89 and #240 (open or closed, 2023 to 2025): selecting the capture area
  on the second monitor does not work or captures the main monitor. The maintainer
  called it a known limitation.
  <https://github.com/ramjke/Translumo/issues/20>
- Issues #125, #234 (open), #24: `TextDetectionException` from EasyOCR/Tesseract. Maintainer
  answer in 2025: "EasyOCR is not recommended, use WindowsOCR". The bundled-Python approach
  is the main source of install failures (#90, #18, #62, and "required python module not
  installed" in #125).
  <https://github.com/ramjke/Translumo/issues/234>
- Issue #84 (open): vertical Japanese/Chinese text returns nothing or gibberish.
  <https://github.com/ramjke/Translumo/issues/84>
- Issue #3 (closed): users want a recognition delay because visual-novel text "types in"
  and the tool translates partial strings repeatedly. Maintainer: a 2-3 s delay is
  "extremely uncomfortable" for subtitles, so he offered a config value as a compromise.
  This is the same problem as our "stable for N reads" rule.
  <https://github.com/ramjke/Translumo/issues/3>
- Issue #174 (closed): `Screen capture failed (code: -2005270489) Failed to acquire frame`
  from `ScreenDXCapturer`; the workaround in the thread is "run as administrator".
  <https://github.com/ramjke/Translumo/issues/174>
- Issue #228 (closed): translation window invisible to a screen-share viewer. Cause was a
  setting "do not capture translation window". Shows that excluding the overlay from
  capture is a user-visible switch that needs a clear label.
  <https://github.com/ramjke/Translumo/issues/228>
- Issue #228 also: after 1-2 hours "the translator stops translating". Maintainer: free
  translation services block free use, use a proxy. Lesson: do not rely on a free
  scraped endpoint.
- Issue #39 (closed): Kaspersky flagged the `binaries_extract.bat` helper that downloads
  Python and models via PowerShell. Downloader scripts are antivirus bait.
  <https://github.com/ramjke/Translumo/issues/39>
- Issue #7 (open): Font settings page crashes the app, linked in the title to an ASUS GPU
  Tweak OSD conflict. Overlay/OSD software on gaming PCs can clash with our windows.
  <https://github.com/ramjke/Translumo/issues/7>
- Feature requests that stay open: offline/local translation (#92, #183 Ollama),
  one-shot translation and history (#21), user substitutions/glossary (#93), multi-region
  (#263). Gloss's spec already covers all of these.

### LunaTranslator
- README: text hooking for visual novels and emulator hooking are the core; OCR is
  secondary; supports nearly all translation engines incl. LLM and offline; GPL-3.0; uses
  free code signing from SignPath.io. Checked 2026-10-30.
  <https://github.com/HIllya51/LunaTranslator>
- Repo metadata: 13,962 stars, GPL-3.0, pushed the day we checked, 42 open issues. Almost
  all open issues are per-game support requests (hooking), which is irrelevant to us.
- Issue #1663 (closed): user wants a frozen screenshot while selecting the OCR region, as
  Snipping Tool does. Maintainer: it works on a single screen only, "supporting multiple
  screens is difficult". Same multi-monitor pain as Translumo.
  <https://github.com/HIllya51/LunaTranslator/issues/1663>
- Issue #1131 (closed): "always on top" stickiness. Maintainer: Windows security rules mean
  setting or clearing topmost often fails when the window has no user focus; "cannot be
  fixed". Lesson: z-order handling needs a re-assert strategy.
  <https://github.com/HIllya51/LunaTranslator/issues/1131>
- Issue #533 (closed): clicking the translation box makes the Magpie upscaler exit its
  scaling. Overlays that take focus break game-side tools. Our overlay must be
  non-activating (`WS_EX_NOACTIVATE`) as well as click-through.
  <https://github.com/HIllya51/LunaTranslator/issues/533>
- Issue #1358 (closed, 82 comments): version 7.6.4 hung at launch until the exe was
  started a second time. Large "kitchen-sink" apps are hard to keep stable.
  <https://github.com/HIllya51/LunaTranslator/issues/1358>

### ScreenTranslator
- README: Tesseract + Leptonica, language packs downloaded in settings, tray-only, hotkey
  then pick a region; "almost abandoned"; some dynamic web pages and fullscreen apps cannot
  be captured; missing DLL or TLS errors may need the VC++ redistributable.
  <https://github.com/OneMoreGres/ScreenTranslator>
- Issue #5 (open since 2019): Tesseract returns "gibberish" for stylised fonts. Evidence
  that classic Tesseract is a poor fit for game UI text.
  <https://github.com/OneMoreGres/ScreenTranslator/issues/5>
- Issue #152: capture goes black after some minutes. Root cause was Kaspersky "Protected
  Browser" blocking screen capture. Capture-blocking security software is a real cause of
  "black frame" reports; Gloss needs a black-frame detector with a human-readable hint.
  <https://github.com/OneMoreGres/ScreenTranslator/issues/152>
- Issue #164, #109, #180: translators (DeepL, Google) silently stop working when the
  scraped web endpoint changes. Same lesson as Translumo.
- Issue #157: Japanese vertical text recognition broken. Same as Translumo #84.

### PowerToys Text Extractor
- Microsoft Learn (updated 2026-10-21): uses OCR, default language from Windows keyboard
  settings, only recognises languages whose OCR language pack is installed (listed via
  `Get-WindowsCapability`); activation Win+Shift+T; code based on Text Grab; Microsoft now
  recommends the Snipping Tool for screenshots.
  <https://learn.microsoft.com/en-us/windows/powertoys/text-extractor>
- Issue #21676 (31 comments, open since 2022): works only on the primary monitor for some
  users. Issue #31992: screen contents shift when launched with several monitors. Issue
  #35614: an empty, smaller "desktop copy" overlay appears (user runs DisplayFusion).
  Multi-monitor and mixed-DPI handling for full-screen overlays is the recurring bug class
  even for Microsoft.
  <https://github.com/microsoft/PowerToys/issues/21676>
- Issue #25336 (open): about 700 MB RAM while idle after one use. Keep the resident
  footprint small and release the OCR engine when paused.
  <https://github.com/microsoft/PowerToys/issues/25336>
- Issue #29331 / #28878 / #50075: users want the OCR language to be remembered or follow
  the last used language. Windows OCR is single-language per call, so language handling is
  a UX problem. Gloss should auto-detect or run a configured multi-language list.
- Issue #41208: Windows OCR drops digits and commas strings. Windows OCR is weak on numeric
  strings, which matter for order cards.
  <https://github.com/microsoft/PowerToys/issues/41208>

## Local measurements

None for this report. (The shell probe is in `app-shell.md`.)

## What to copy

1. **Windows OCR as a first-class, zero-install fallback** (Translumo recommends it above its
   own heavy engine; PowerToys uses it). Keep it as `ocr.fallbacks`. Do not make it primary
   for Chinese chat until the Phase 1 OCR accuracy test says so; it needs the zh-Hans
   language pack installed per user.
2. **Translumo's "multiple engines, pick best" idea**, but only as an opt-in later feature.
   It doubles CPU use.
3. **A recognition-stability delay** (Translumo #3). Our `stable_reads = 2` rule is the
   principled version. Make it configurable and show it in settings.
4. **One-shot mode and history** (Translumo #21, Luna's "perform OCR once"). Matches SPEC
   scenario 3 (cards that disappear): the panel history is the answer.
5. **User glossary/substitutions** (Translumo #93). Already SPEC section 6.
6. **Pixel-level screen freeze while selecting a region** (Luna #1663, PowerToys). Do it per
   monitor with one frozen-image window on each monitor so mixed DPI works.
7. **A translate-service-down indicator** and API-key based providers rather than scraped
   free endpoints (Translumo #228, ScreenTranslator #164).
8. **Tray-only quiet start** (ScreenTranslator) with a settings window on demand.
9. **Free signing for open source** (Luna uses SignPath Foundation). See `app-shell.md`.

## What to avoid

1. **Bundling a second runtime on first use** (Translumo downloads Python, PyTorch and
   models via a batch file: antivirus hit #39, install failures #90/#18/#62, 5 GB footprint).
   Gloss ships ONNX models as plain files with a hash check.
2. **Requiring a GPU** (Translumo EasyOCR). Our spec says CPU-only.
3. **Scraped free translation endpoints.** They break silently after an hour or after a
   site change.
4. **Primary-monitor-only assumptions.** Every one of the four tools has multi-monitor
   bugs. Write the DPI/monitor unit tests first (SPEC section 2.1).
5. **A focus-taking overlay.** Make overlay windows `WS_EX_TRANSPARENT | WS_EX_NOACTIVATE`
   and re-assert topmost on a timer (Luna #533, #1131).
6. **Tesseract as the primary engine for stylised UI text** (ScreenTranslator #5).
7. **Idle memory growth** (PowerToys #25336). Add a memory ceiling test to bench.
8. **Silent failure on a black frame.** Detect all-black or unchanged frames when the source
   is changing, mark status "capture blocked", and suggest running borderless or turning
   off capture-blocking software (ScreenTranslator #152, Translumo #174).
9. **Kitchen-sink scope** (Luna: hooking, TTS, Anki, speech). Our non-goals already exclude
   this.

## Recommendation

- Do not adopt any of these as a base. None targets "continuous chat reading with
  dedupe", and the closest (Translumo) has a heavyweight, fragile OCR path. Take the ideas
  listed under "What to copy".
- Keep the Windows OCR adapter in the plan as the zero-install fallback, with RapidOCR
  primary as in the spec (OCR accuracy is decided in the separate OCR research report).
- Add three items to the Phase 2 backlog that the spec does not yet mention:
  (a) black-frame/blocked-capture detection with a user hint,
  (b) topmost re-assertion for the overlay,
  (c) per-monitor frozen selection windows.

## Rejected

- **Forking Translumo**: C#/.NET 8 with its own Python bridge; wrong language for our
  stack; EasyOCR path is the main source of its bugs; no chat-line tracking.
- **Forking LunaTranslator**: GPL-3.0 (would force Gloss to GPL), game-hooking focus, huge
  scope.
- **Forking ScreenTranslator**: self-described "almost abandoned", Tesseract only, C++/Qt
  with Qt WebEngine.
- **Wrapping PowerToys Text Extractor**: no API, no translation, one-shot only.
- **Textractor-style text hooking**: out of scope (SPEC non-goals), and the project's last
  push was 2024-03-15 with 636 open issues.

## Risks and unknowns

- Issue trackers are skewed toward people who have problems; counts of comments are a rough
  signal only. I read titles and the first few comments of about 20 issues, not all.
- Several threads are in Russian or Chinese; I relied on titles and on maintainer replies I
  could read. Treat the Russian-language issue summaries (#3, #228) as my reading, not
  quotes.
- I did not install or run any of these apps, so claims about latency or CPU use are not
  measured. Do not cite them as benchmarks.
- Translumo's capture error `-2005270489` is a DXGI-style HRESULT; the cause is not
  confirmed in the thread. The capture engineer should test a WGC fallback on a machine
  with HDR and with a game in borderless mode.
- Windows OCR digit/comma weakness comes from one PowerToys issue; confirm in the OCR
  report before relying on it.

## Sources

- Translumo: <https://github.com/ramjke/Translumo> (README, issues #3, #7, #20, #39, #84,
  #90, #125, #174, #228, #234)
- LunaTranslator: <https://github.com/HIllya51/LunaTranslator> (issues #533, #1131, #1358,
  #1663)
- ScreenTranslator: <https://github.com/OneMoreGres/ScreenTranslator> (README, issues #5,
  #152, #157, #164)
- PowerToys Text Extractor docs:
  <https://learn.microsoft.com/en-us/windows/powertoys/text-extractor>; issues
  <https://github.com/microsoft/PowerToys/issues/21676>, #25336, #31992, #35614, #41208
- Textractor repo metadata (GitHub API): <https://github.com/Artikash/Textractor>
- Raw repo stats and issue lists: `docs/research/data/github_repo_stats_2026-10-30.json`,
  `docs/research/data/prior_art_issues_2026-10-30.json`; fetch scripts in
  `docs/research/scratch/gh_*.py`.
