# Capture backends: mss vs Windows Graphics Capture vs DXGI Desktop Duplication

Researched 2026-10-30. Machine: Windows 11 build 26300, AMD Ryzen 9 9950X3D, RTX 3060 Ti
(plus an AMD iGPU and a "GameViewer Virtual Display Adapter" listed in WMI), two 1920x1080
monitors, 100% scaling. Python 3.12.16 (uv-managed) in a throwaway venv outside the repo.

## Question and decision it informs

Which capture library should be the default `CaptureSource` adapter, which should be the
fallback, and what must the adapter contract and config guarantee (DPI, window-follow,
self-exclusion)? Feeds SPEC section 2.1 [R] "Capture" and the `[capture]` config block.

## Short answer

- **Primary: `mss` 10.2.0** for monitor and region modes. Pure Python, no native build,
  honours `WDA_EXCLUDEFROMCAPTURE` (measured), 7-13 ms per grab here.
- **Fallback and window-follow: `windows-capture` 2.0.1** (Rust, WGC, abi3 wheel). Captures
  one window by HWND even when it is covered (measured), and also ships a DXGI duplication
  session so one dependency covers both modern backends.
- Do not adopt `dxcam` as a default. It is fast, but maintenance history is thin (see below).

## Options compared

| | mss | windows-capture (WGC) | windows-capture (DXGI session) | dxcam | pywinrt WGC (`winrt-Windows.Graphics.Capture`) |
|---|---|---|---|---|---|
| API underneath | GDI `BitBlt` with `SRCCOPY\|CAPTUREBLT` | Windows.Graphics.Capture | DXGI Desktop Duplication | DXGI (default) or WGC (`dxcam[winrt]`) | WGC, hand-built |
| Latest release (checked 2026-10-30) | 10.2.0, 2026-04-23 | 2.0.1, 2026-08-08 | same package | 0.3.0, 2026-03-12; 0.4.0.dev2, 2026-09-17 | 3.x/10.0.28000.x line, actively published |
| Maintenance signal | Releases 2024-11, 2025-08, 2026-04; 41 open issues | 1.x through 2025, 2.0 in 2026-04; 14 open issues, 268 commits | same | 0.0.5 in 2022-09, then nothing until a burst of 8 releases in March 2026; 12 open issues | Regularly published on PyPI (repo and issue tracker not checked) |
| Licence | MIT | MIT (classifier only; `license` field null) | MIT | MIT | MIT |
| Python 3.12 | Yes (classifiers 3.9-3.14; tested here) | Yes (`cp39-abi3` wheel, 229 KB; tested here) | Yes | Yes (needs >=3.10; tested here) | Yes (cp312 wheels seen on earlier releases; current needs >=3.11) |
| Dependencies | none | numpy | numpy | comtypes + numpy (+ 7 winrt packages for WGC) | many per-namespace packages |
| On-disk size | 0.5 MB | 0.56 MB | same | 2.3 MB + comtypes 2.9 MB | not measured |
| Monitor capture | yes | yes (`monitor_index`) | yes | yes (`output_idx`) | yes, with interop code |
| Region capture | native (crop in BitBlt) | crop in numpy after the fact | crop in numpy | native `region=` | crop yourself |
| Window by HWND | no (use rect polling) | yes (`window_hwnd`, `window_name`) | no | no (open request #87, #30) | yes |
| Cursor in frame | no | optional | n/a | optional (WGC backend) | optional |
| Delivery model | pull, synchronous | push callback on a native thread | pull with timeout, returns `None` if nothing changed | pull; `grab()` returns `None` if nothing changed | push |
| Python ergonomics | best; `numpy.asarray(sct.grab())` gives BGRA | callback style; `frame.frame_buffer` is a zero-copy BGRA view | simple | simple | worst |

## Evidence

### Versions, licences, Python 3.12
- mss 10.2.0 uploaded 2026-04-23, MIT, classifiers 3.9-3.14, no runtime deps. Source: PyPI JSON
  (https://pypi.org/pypi/mss/json), 2026-10-30. In 10.2.0 `mss.mss()` emits a
  `DeprecationWarning` ("use mss.MSS instead"); observed locally. Pin the version and use
  `mss.MSS`.
- windows-capture 2.0.1, uploaded 2026-08-08, wheel `cp39-abi3-win_amd64` 229,121 bytes, Rust
  extension, MIT classifier. Source: PyPI JSON (https://pypi.org/pypi/windows-capture/json),
  2026-10-30. GitHub: 523 stars, 14 open issues, MIT
  (https://github.com/NiiightmareXD/windows-capture). Its README says the Rust crate
  and the Python package are documented separately; the Python docs I could read were thin
  (parameters were found by introspecting the installed package).
- dxcam: PyPI shows 0.0.5 (2022-09-04), then 0.1.0 to 0.3.0 all within 2026-03-08 to
  2026-03-12, then dev builds (0.4.0.dev2 on 2026-09-17). Python >=3.10, MIT, deps
  comtypes + numpy. Source: https://pypi.org/pypi/dxcam/json. Repo
  https://github.com/ra1nty/DXcam: 804 stars, 12 open issues, no GitHub releases.
  Interpretation (mine): the project was dormant for about 3.5 years and was revived in 2026.
  Whether the revival lasts is unknown.
- python-mss repo: 41 open issues, "thread-safe", README says Python 3.10+ while PyPI says
  >=3.9 (minor inconsistency). Source: https://github.com/BoboTiG/python-mss.

### Stability and recurring bugs (issue trackers)
- python-mss Windows issues: wrong sizes on mixed-DPI monitors (#219, open since 2022-12),
  `GetDIBits() failed` (#267, open), black image on Windows Server / after RDP closes (#67,
  open since 2018), black screen from Task Scheduler (#149, closed with a fix). Source:
  https://github.com/BoboTiG/python-mss/issues, 2026-10-30. Theme: **session and DPI context
  problems, not "black windows"**.
- mss sets DPI awareness itself on construction (read in `mss/windows/gdi.py` line ~247:
  `SetProcessDpiAwareness(2)`, which is per-monitor v1, falling back to `SetProcessDPIAware`).
  The README warns that `pyautogui`/`pyscreeze` import-time calls interfere. Consequence for
  Gloss: set Per-Monitor V2 explicitly before importing anything, so mss and Qt do not
  fight over it. I did not test PySide6 here (not installed), so the Qt side is unverified.
- windows-capture open issues (all 14 open): crash on start "Failed to convert item to..."
  (#163), `0xC00D6D60` Windows API error (#145), pink/red tint on a 1920x1080 display (#192),
  title-bar height always 0 (#202, 2026-07-18), "do not create a Python object per frame in
  DDA" (#182, a performance complaint). None mention black frames or HDR. Source:
  https://github.com/NiiightmareXD/windows-capture/issues, 2026-10-30.
- dxcam open issues: access violation in `DXCamera.release()` (#144, 2026-07-22), cannot
  restore output after switching display adapters (#141, 2026-05), `recreate` error (#137),
  `comtypes` has no attribute `IUnknown` (#139), HDR display gives "very bright output"
  (#77, 2023). Source: https://github.com/ra1nty/DXcam/issues, 2026-10-30.
  Theme: **lifecycle/teardown crashes and multi-GPU**; relevant because this PC has an
  NVIDIA dGPU, an AMD iGPU and a virtual display adapter.

### DXGI Desktop Duplication, platform rules (Microsoft Learn)
- Returns frames always as `B8G8R8A8_UNORM`; supplies dirty and move rectangles.
  https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/desktop-dup-api (page dated
  2025-04-15).
- `DuplicateOutput` fails with `E_INVALIDARG` if "the calling application is already
  duplicating this desktop output"; one duplication per output per process, default four
  processes per session; `E_ACCESSDENIED` for the secure desktop; `DXGI_ERROR_UNSUPPORTED`
  for non-DWM modes.
  https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/nf-dxgi1_2-idxgioutput1-duplicateoutput
- `AcquireNextFrame` returns `DXGI_ERROR_ACCESS_LOST` on desktop switch, mode change, or
  "switch from DWM on, DWM off, or other full-screen application"; the duplication object
  must be recreated. Timeouts are normal on a static desktop.
  https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/nf-dxgi1_2-idxgioutputduplication-acquirenextframe
- Local reproduction: creating a second `DxgiDuplicationSession` on the same output while the
  first (or a dxcam camera) was still alive failed with `E_INVALIDARG (0x80070057)`. So one
  live duplication per output per process is real and easy to trip over when swapping backends.

### Windows Graphics Capture (Microsoft Learn)
- `GraphicsCaptureSession` is available from Windows 10 1803. `IsCursorCaptureEnabled` needs
  2004. `IsBorderRequired` needs build 20348 and, to turn the border off, user consent via
  `GraphicsCaptureAccess.RequestAccessAsync(Borderless)` plus the
  `graphicsCaptureWithoutBorder` package capability. Without consent the yellow border
  stays. https://learn.microsoft.com/en-us/uwp/api/windows.graphics.capture.graphicscapturesession
  and `...isborderrequired` (pages updated 2026-07-23), checked 2026-10-30.
  Implication: an unpackaged PyInstaller exe has no package manifest, so assume the border
  may appear on monitor capture. **Not verified visually here.** The border is drawn by the
  system and is not part of the captured pixels (docs describe it as a notification, not
  content).
- HDR: with Windows HD Color enabled the pixel format "might not necessarily be
  B8G8R8A8"; to avoid washed-out capture use `R16G16B16A16_FLOAT` end to end, then tone-map.
  https://learn.microsoft.com/en-us/windows/apps/develop/media-authoring-processing/screen-capture
  (updated 2026-08-27). windows-capture exposes BGRA only (as far as I could see), so HDR
  correctness is **unknown** for it.
- Frames arrive only when content changes; a static window yields one frame. Callers must
  cache the last frame. Minimized windows give no new frames or an unreliable stale frame
  (OBS forum and the cua-driver notes surfaced by search; weak evidence, community sources).
  https://obsproject.com/forum/threads/capture-window-function-problem.117661/

### HDR with GDI and DXGI
- Windows Q&A: screenshots on HDR displays look washed out depending on "SDR content
  brightness" (https://learn.microsoft.com/en-us/answers/questions/4309236/screenshot-with-hdr-enabled-is-washed-out-the-sdr).
  Weak evidence for mss specifically (nobody filed an HDR issue against mss; none of the 15
  mss issue titles matching windows/dpi/black mention HDR).
- Duplication in HDR needs `DuplicateOutput1` with FP16 to avoid OS conversion; and
  Windows 11 may hand back FP16 even in SDR (GStreamer MR 5671; Sunshine PR 654, both
  community sources). Not tested here; I do not know if this PC has HDR enabled.

### SetWindowDisplayAffinity / WDA_EXCLUDEFROMCAPTURE (Microsoft Learn)
- Value 0x11, "displayed only on a monitor; everywhere else the window does not appear at
  all"; Windows 10 2004+; earlier builds behave like `WDA_MONITOR` (black box). Window must be
  top-level and belong to the calling process. Works only when DWM composes. Not a security
  boundary. Page updated 2025-07-01.
  https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowdisplayaffinity
- Build 26300 is far above 19041, so the flag is honoured.

## Local measurements

Scripts: `docs/research/scratch/capture_bench.py`, `wda_test.py`, `wgc_window_test.py`.
Raw data: `docs/research/data/capture_bench.jsonl`, `wda_test.json`, `wgc_window_test.json`.
Venv: Python 3.12.16 with mss 10.2.0, windows-capture 2.0.1, dxcam 0.3.0, numpy 2.5.4.
Desktop was in normal use (not an idle benchmark rig), one run each, so treat as order of
magnitude. Never use these as acceptance numbers.

### Speed (full monitor 1920x1080 / region 800x600, 40 grabs at 4 fps)

| Backend | full median / p95 ms | region median / p95 ms | notes |
|---|---|---|---|
| mss (GDI) | 13.4 / 15.0 | 7.4 / 8.4 | native crop; copies the region only |
| windows-capture DXGI | 6.7 / 10.3 | 5.7 / 8.4 | whole frame copied, then cropped in numpy; returns the last frame when nothing changed |
| dxcam DXGI | 2.4 / 4.6 | 1.2 / 6.3 | `grab()` returns BGR, `None` if unchanged |
| dxcam WGC (`winrt` extra) | 3.9 / 7.9 | 1.7 / 4.2 | needs `pip install "dxcam[winrt]"` |
| windows-capture WGC | push | push | see below |

All are fast enough: the pipeline needs about 4 fps and OCR will cost 100x more than capture.

### CPU
- 4 fps pull loops: mss 0.0% (full), 0.2% (region) of one core; DXGI 3.0% / 0.6%; dxcam
  DXGI 7.0% / 0.3%; dxcam WGC 1.1% / 0.6%. The mss figures are suspiciously low because
  `psutil` timing is coarse and GDI time is partly spent waiting. A saturated mss loop
  (`scratch/sat.py`, no sleep) gave 50.6 fps at 33.7% of one core, i.e. about 6.7 ms CPU per
  full frame, so expect roughly 2-3% of one core at 4 fps. Conclusion: capture CPU is
  negligible next to SPEC's 3% idle target only if change detection runs on small
  downscaled data, and the frame is not converted more than once.
- WGC push mode with no throttle delivered 487 frames in 10 s (about 49 fps, my desktop
  was animating) and cost 22% of one core with a full-frame copy in the callback. With
  `minimum_update_interval=250` it delivered 40 frames and cost 4.1%. **Always set
  `minimum_update_interval` from `fps_max`** and copy only the needed region.

### WDA_EXCLUDEFROMCAPTURE (the 2 x 4 matrix; red window, fraction of red pixels at its centre)

| Backend | affinity NONE | affinity EXCLUDEFROMCAPTURE |
|---|---|---|
| mss (GDI BitBlt + CAPTUREBLT) | 1.0 (visible) | 0.0 (hidden) |
| DXGI duplication (windows-capture) | 1.0 | 0.0 |
| dxcam DXGI | 1.0 | 0.0 |
| WGC monitor capture | 1.0 | 0.0 |

So all four backends honour the flag on this build. Caveats: one run; plain Tk window; I did
not test a layered/click-through Qt overlay, `WA_TranslucentBackground` windows, tooltips or
menus. The acceptance test in SPEC section 8.4 should do exactly this check with real Gloss
windows. Note `CAPTUREBLT` means mss does include layered windows, so without the flag
click-through overlays would be captured.

### Window-follow with WGC
`WindowsCapture(window_hwnd=hwnd)` returned the window's own pixels (400x300, BGRA, red
fraction 1.0) while another window was lifted over it, and the same size after the window was
moved. Only 2 frames in 2.5 s because the content was static (as the docs imply). So WGC
by HWND gives occlusion-proof, move-proof capture with no rect tracking.

### Monitor index mismatch (a real trap)
`windows-capture` `monitor_index=1` was the **second** monitor; the primary was
`monitor_index=2` (the red window on the primary was found only with index 2). mss
`monitors[1]` and dxcam `output_idx=0` were the primary. Each library enumerates differently,
so the contract must identify monitors by a stable id (device name or virtual-desktop rect),
and each adapter must map that to its own index. Do not store a bare integer in config.

### Install size
mss 0.5 MB, windows-capture 0.56 MB, dxcam 2.3 MB + comtypes 2.9 MB; numpy 34 MB is needed anyway.
Not measured: PyInstaller output.

## Per-monitor DPI

- Gloss runs at 100% on both monitors, so scaling bugs will not show up on this machine.
  Design for them anyway because the SPEC acceptance test names mixed-DPI.
- Make the process Per-Monitor V2 at the earliest line of `main` (or via the exe
  manifest) before importing mss or Qt. With V2 all of mss, DXGI, WGC and Win32 rects are
  physical pixels, matching the contract. mss then returns physical coordinates.
- Known mss weakness: issue #219 (mixed DPI wrong widths) is still open, and mss itself
  calls the older per-monitor (v1) API. That is another reason to set V2 first.
- DXGI is inherently physical-pixel, one output at a time; WGC monitor/window capture is
  physical too.

## Recommendation

1. **Primary `mss` for `mode = monitor | region`.** Reasons: smallest and simplest, pure
   Python, region copies only the needed pixels, measured as fast as the pipeline needs,
   honours the exclusion flag, no GPU or COM lifecycle to leak. Pin 10.2.0 and use
   `mss.MSS` (not the deprecated `mss.mss`). Create the `MSS` object on the thread that
   grabs.
2. **`windows-capture` as the second adapter (`wgc`)** and use it as **primary for
   `mode = window`**: capture by `window_hwnd`, so a covered, moved or resized game window
   keeps working and there is no rect polling. Set `cursor_capture=False`,
   `minimum_update_interval = 1000/fps_max`, and keep the last frame because static windows
   emit nothing. Also used as the automatic fallback when mss returns a mostly black frame
   (health check below).
3. Keep the DXGI session inside the same `windows-capture` package as an optional third
   registry entry (`dxgi`) rather than adding dxcam; it gives dirty-rect ready frames and
   needs no `comtypes`.
4. **Self-exclusion**: a single `exclude_from_capture(hwnd)` helper in `ui/`, called on
   every top-level Gloss window after it is created/shown (and after any recreate), plus
   an automated test that grabs the overlay region with every registered capture adapter and
   asserts the overlay colour is absent (the same method as `wda_test.py`).
5. **Config / contract notes for the architect** (recommendations only):
   - `capture.source` may need to be per mode (for example `source.window = "wgc"`) or the
     registry should choose by mode; the draft schema has one global `source`.
   - Monitors and windows identified by stable id, not index (see trap above).
   - `Frame` should say whether it is a fresh capture or the repeated last frame, so the
     change detector can skip hashing. Optional-with-default, minor bump.
   - Add a health signal "captured frame is blank/black for N consecutive frames while the
     screen is changing" that triggers the fallback chain, since GDI failing silently is
     a black image, not an exception.
6. Ask the user one concrete question: is Windows HDR switched on for either monitor
   (Settings > System > Display > HDR)? If yes, test mss and WGC against a real Chinese
   chat screenshot before trusting OCR numbers.

## Rejected

- **dxcam as default**: dormant 2022 to 2026 then a burst of 0.x releases; open
  access-violation on `release()` (#144), adapter-switch (#141) and `comtypes` import
  (#139) bugs; this PC has three display adapters. Also requires `comtypes`. Kept only as a
  possible benchmark comparison; fastest of the set (2.4 ms full frame).
- **DXGI as default**: one duplication per output per process (reproduced), `ACCESS_LOST`
  on any mode change or fullscreen switch, static desktop returns timeouts (needs a cached
  frame), HDR needs `DuplicateOutput1` plus tone mapping; more failure modes than the OCR
  use-case needs.
- **WGC as default for monitor/region**: push model needs a throttle (22% of a core
  unthrottled in my test), possible yellow border for unpackaged apps (unverified),
  HDR format handling unclear in the Python wrapper, thin Python docs, and 14 open issues
  including start crashes (#163, #145).
- **pywinrt WGC bindings built by hand**: many packages (Foundation, Capture, DirectX,
  Direct3D11, Interop...), D3D11 interop and readback code to write and maintain; no
  advantage over windows-capture.
- **PIL `ImageGrab`, pyautogui/pyscreeze**: also GDI, slower, and they call
  `SetProcessDPIAware` at import (mss README warns about this).
- **D3DShot**: reported unmaintained in dxcam's PyPI text; not evaluated.
- **`wgcapture` 0.1.2, `windows-cap` 0.1.2**: tiny, window-only, old; not evaluated beyond
  PyPI listing.
- **Windows.Media.Capture / Screen Capture via ffmpeg gdigrab**: out of scope (extra
  binary, no benefit).

## Open risks

1. **HDR**: unknown whether this PC uses it. GDI/mss can clip, DXGI/WGC need FP16 handling.
   Mitigation: health check plus OCR-confidence drop alarm; verify on the user's actual
   setting; ADR if HDR is on.
2. **Silent black frames** (protected video, some GPU overlays, exclusive fullscreen,
   Task Scheduler/RDP sessions per mss issues #67/#149). I could not reproduce a
   hardware-accelerated black window under mss on this machine because screen-DC GDI
   captures the composited desktop. Black is more likely in exclusive fullscreen; SPEC
   already tells the user to use borderless windowed.
3. **Yellow capture border** on WGC for an unpackaged exe: unverified. If it appears on
   monitor capture it is cosmetic but annoying; the borderless consent path needs a
   package identity, which a plain PyInstaller exe lacks.
4. **windows-capture is a single-maintainer Rust/Python project** with sparse Python
   docs; its Python wrapper converts via OpenCV for `save_as_image` (OpenCV is not
   installed and I did not need it). Pin the version, wrap behind our adapter, and keep mss
   working without it. 2.0 shipped 2026-04, so API drift is plausible; the update steward
   should re-run `scratch/` scripts as a bench after any bump.
5. **mss deprecations**: `mss.mss` is already deprecated in 10.2.0; adapter must use the
   new name and be tested on update.
6. **Process DPI context conflict** between mss's own call and Qt's; set V2 first. Untested
   with PySide6.
7. **WDA coverage**: tested only with a plain Tk window. Qt translucent/layered overlays,
   popups and tooltips not tested; flag must be reapplied if Qt recreates the native
   window (e.g. when toggling `Qt.WindowTransparentForInput`).
8. **Multi-adapter machine**: dGPU, iGPU and a virtual display adapter. Only the dGPU was
   enumerated by dxcam; if the second monitor is attached elsewhere the DXGI path could
   behave differently. mss/WGC are unaffected.
9. **Mixed-DPI** untestable on this hardware (100% on both monitors).
10. **CPU figures are rough** (one run, busy desktop, coarse timer). Phase 4 bench must
    re-measure inside the real pipeline.

## Sources (all checked 2026-10-30 unless noted)

- mss PyPI: https://pypi.org/pypi/mss/json ; repo https://github.com/BoboTiG/python-mss ;
  issues https://github.com/BoboTiG/python-mss/issues
- windows-capture PyPI: https://pypi.org/pypi/windows-capture/json ; repo
  https://github.com/NiiightmareXD/windows-capture ; issues .../issues ;
  Python README .../tree/main/windows-capture-python
- dxcam PyPI: https://pypi.org/pypi/dxcam/json ; repo https://github.com/ra1nty/DXcam ;
  issues https://github.com/ra1nty/DXcam/issues
- pywinrt WGC: https://pypi.org/pypi/winrt-Windows.Graphics.Capture/json
- SetWindowDisplayAffinity:
  https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowdisplayaffinity
- GraphicsCaptureSession:
  https://learn.microsoft.com/en-us/uwp/api/windows.graphics.capture.graphicscapturesession
- IsBorderRequired:
  https://learn.microsoft.com/en-us/uwp/api/windows.graphics.capture.graphicscapturesession.isborderrequired
- Screen capture guide (HDR note):
  https://learn.microsoft.com/en-us/windows/apps/develop/media-authoring-processing/screen-capture
- Desktop Duplication API:
  https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/desktop-dup-api
- DuplicateOutput:
  https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/nf-dxgi1_2-idxgioutput1-duplicateoutput
- AcquireNextFrame:
  https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/nf-dxgi1_2-idxgioutputduplication-acquirenextframe
- HDR washed-out screenshots (Q&A):
  https://learn.microsoft.com/en-us/answers/questions/4309236/screenshot-with-hdr-enabled-is-washed-out-the-sdr
- HDR duplication format (Q&A and community):
  https://learn.microsoft.com/en-us/answers/questions/179015/duplicateoutput-doesnt-convert-format ;
  https://gitlab.freedesktop.org/gstreamer/gstreamer/-/merge_requests/5671 ;
  https://github.com/LizardByte/Sunshine/pull/654
- WGC minimized windows (community): https://obsproject.com/forum/threads/capture-window-function-problem.117661/
- Local scripts and data: `docs/research/scratch/capture_bench.py`, `wda_test.py`,
  `wgc_window_test.py`, `sat.py`; `docs/research/data/capture_bench.jsonl`, `wda_test.json`,
  `wgc_window_test.json`.
