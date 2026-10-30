# App shell: UI toolkit, packaging, hotkeys, signing

Checked 2026-10-30. Researcher: Phase 1. Machine: Windows 11 Pro 10.0.26300, two monitors
(both 100% scale), Python 3.12.16 (uv-managed), PySide6-Essentials 6.12.0.

## Question and decision it informs

Which toolkit should host Gloss's tray icon, click-through overlay and side panel
(PySide6 vs Tauri vs .NET/WPF vs Tkinter)? How should it be packaged (PyInstaller vs
Nuitka), how should global hotkeys work, and what happens to an unsigned build on a PC
with Smart App Control (SAC)? Feeds: CLAUDE.md "Stack" (PySide6 default), the ADR for
packaging, and the Phase 5 release plan.

## Options compared (UI toolkit)

| | PySide6 (Qt 6.12) | Tauri v2 | .NET WPF | Tkinter |
|---|---|---|---|---|
| Maturity | Official Qt for Python, release 6.12.0 on 2026-10-08 | Mature, Rust core + WebView2 | Mature, Microsoft | Ships with Python, stagnant |
| Licence | LGPL-3.0 / GPL (we use LGPL, dynamic linking) | MIT/Apache-2.0 | MIT | PSF |
| Fits our Python pipeline | Yes, same process, same language, pytest can drive it | No: UI in Rust+web, pipeline in Python needs a sidecar process and IPC | No: pipeline would be rewritten in C# | Yes |
| Per-pixel transparent overlay | Yes (`WA_TranslucentBackground`) | Yes (transparent webview) | Yes (`AllowsTransparency`) | No: only colour-key `-transparentcolor` plus whole-window `-alpha` |
| Click-through | Yes, measured (below) | `set_ignore_cursor_events`; open issues about hover and focus | Yes (`WS_EX_TRANSPARENT` via P/Invoke) | Needs Win32 calls; fragile after packaging (anecdotes) |
| Tray icon | `QSystemTrayIcon`, measured working | Built in | Needs WinForms `NotifyIcon` or a library | Needs third-party `pystray` |
| Side panel (rich scrolling list) | Good (QListView/QML) | Good (HTML) | Good | Poor |
| Windows OCR / WGC capture | Via `winrt` Python packages or ctypes | Via Rust crates | Native | Via Python packages |
| Packaged size (probe only, see below) | 93 MB folder or 37.7 MB one-file (PyInstaller) | Small binary; WebView2 is preinstalled on Win10/11, so near zero added | Not measured | Not measured (small) |

## Evidence

### Toolkit
- Measured locally: a PySide6 overlay with `Qt.FramelessWindowHint | WindowStaysOnTopHint |
  Tool | WindowTransparentForInput | WindowDoesNotAcceptFocus` plus
  `WA_TranslucentBackground` and `WA_ShowWithoutActivating` gets extended styles
  `WS_EX_LAYERED`, `WS_EX_TRANSPARENT` and `WS_EX_NOACTIVATE` on Windows 11. A hit test
  with `WindowFromPoint` at a point inside the red overlay does not return the overlay,
  so clicks fall through. Script and numbers in "Local measurements".
- Forum reports say `WA_TranslucentBackground` on Windows implies mouse transparency and
  that ordering of flags matters; the safe pattern is to set the attributes and
  `WindowTransparentForInput` explicitly before `show()`. Weak evidence (forum threads):
  <https://forum.qt.io/post/817594>, <https://qtcentre.org/showthread.php?p=310715>
- Qt 6.12 starts the process per-monitor-DPI-aware V2: measured with
  `GetWindowDpiAwarenessContext` equal to `DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2`
  (`docs/research/data/dpi_probe_2026-10-30.json`). Both of my monitors are at 100%, so
  mixed-DPI behaviour is still untested.
- `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE = 0x11)`: Microsoft Learn says the window
  "does not appear at all" in captures, introduced in Windows 10 version 2004, behaves like
  `WDA_MONITOR` (black box) on older builds, and works only while DWM is composing.
  <https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowdisplayaffinity>
  (page dated 2025-07-01). Measured: an `mss` (GDI) grab of a pure red overlay returned
  (255, 0, 0) before and (228, 224, 223) after the call, i.e. the overlay vanished.
  My probe did not test WGC or DXGI, but the capture report
  (`docs/research/capture.md`, data in `docs/research/data/wda_test.json`) measured the
  same call against mss, DXGI duplication, dxcam and WGC: red-pixel fraction fell from 1.0
  to 0.0 in all four. HDR and exclusive fullscreen remain untested.
- Tauri: docs say Tauri "provides very small binaries" but give no number; on Windows 10
  (2018 update) and 11 the WebView2 runtime "is distributed as part of the operating
  system"; an offline installer would add about 127 MB.
  <https://v2.tauri.app/concept/size/>,
  <https://v2.tauri.app/distribute/windows-installer/>
- Tauri click-through: GitHub issues report that after `set_ignore_cursor_events(true)` the
  app cannot detect hover, JS-based detection only works while focused, and a child webview
  still received clicks on Windows 10 in one report.
  <https://github.com/tauri-apps/tauri/issues/9250>,
  <https://github.com/tauri-apps/tauri/issues/11461>. A 2026 write-up of a Tauri overlay
  reports panel clicks stealing focus from the game and needing platform window-flag fixes:
  <https://dev.to/manasightgg/why-i-chose-tauri-v2-for-a-desktop-overlay-in-2026-597h>
  (blog, weak evidence).
- Tkinter: only colour-key transparency and whole-window alpha; Stack Overflow answers
  (2019 to 2021, not authoritative) describe making the whole window click-through, needing
  Win32 styles on a canvas handle, and breakage after PyInstaller.
  <https://kiwix.lema.org/stackoverflow.com_en_all_2023-11/questions/58846611/tkinter-partially-click-through-able-windows-only>
- .NET/WPF: strong Windows integration, and Translumo is the existence proof (C#, 5.9k
  stars) but it still has multi-monitor, capture and OCR-install bugs (see
  `prior-art.md`). I did not measure WPF here; nothing in this table about WPF size is
  tested.
- Wheel sizes (PyPI, win_amd64, 2026-10-30; `docs/research/data/wheel_sizes_2026-10-30.txt`):
  PySide6_Essentials 82.2 MB, PySide6_Addons 40.5 MB, shiboken6 1.3 MB, PyInstaller 6.22.3
  1.5 MB. The `PySide6` meta-package pulls Addons too, so depend on `PySide6-Essentials`.
  Installed `PySide6` folder from Essentials alone: 215 MB (not what ships).

### Packaging: PyInstaller vs Nuitka
- Measured (PyInstaller 6.22.3, probe app importing only QtCore/QtGui/QtWidgets, windowed,
  no UPX): one-folder 93 MB in 158 files; one-file 37.7 MB. Startup of the full probe
  (which sleeps about 2.0 s on purpose) was 2.3 s from source, about 3.05 s one-folder, and
  4.1 s on the first one-file runs then 3.04 s. So one-folder adds roughly 0.7 s over
  source, one-file adds 0.7 to 1.8 s. Four runs each, noisy; data in
  `docs/research/data/pyinstaller_startup_2026-10-30.txt`. The real app adds
  onnxruntime, models, numpy, OpenCV or similar; those sizes are not measured here.
- Signature audit of the one-folder build (`docs/research/data/signature_audit_2026-10-30.txt`):
  58 binaries (exe/dll/pyd), 43 validly signed (35 by The Qt Company Oy, 8 by Microsoft),
  15 unsigned, including the app exe itself, `python312.dll`, `python3.dll`, OpenSSL
  DLLs, `libffi` and several `.pyd` files. The python here is uv's managed build; the
  python.org build may differ (not checked). onnxruntime and numpy DLLs not yet audited.
  This matters for SAC (next section).
- PyInstaller antivirus: GitHub issues show recurring detections of the bootloader
  (`Wacatac.B!ml`-style heuristics); rebuilding the bootloader reduced engine hits in one
  report (12 to 5), and the practical fallback is reporting each build to vendors.
  <https://github.com/pyinstaller/pyinstaller/issues/5579>,
  <https://github.com/pyinstaller/pyinstaller/issues/5854>
- Nuitka vs PyInstaller (anecdotal): a PySide6 developer reported 3 VirusTotal hits with
  Nuitka against 11 with PyInstaller, and a smaller exe (39 MB vs 77 MB). Other users
  reported Nuitka standalone folders larger than PyInstaller's, and build times about ten
  times longer. Sources are Reddit and vendor blogs (one sells a Nuitka-built product), so
  treat as weak. <https://reddit.fsky.io/r/Python/comments/1je94cf/pyside6_nuitka_is_very_impressive_some_numbers>
  and <https://github.com/Erriez/pyside6-nuitka-test>, which itself says both tools show
  false positives and that "this is a known issue".
- Nuitka current: 4.3.3 on PyPI, released 2026-10-29. PyPI metadata lists the licence as
  AGPL-3.0; whether that affects the compiled output was not verified here (Nuitka's own
  FAQ should be read before committing).
- Nuitka local test: not completed. Nuitka tried to download a MinGW toolchain and the
  build exited with code 1 in this sandbox before compiling (no C compiler on the
  machine). So there is no Nuitka size/startup number in this report. Do not infer one.
- Qt's own deploy tool `pyside6-deploy` wraps Nuitka; I could not read the page body
  (the fetch returned navigation only), so no claim is made about it.

### Global hotkeys
- `RegisterHotKey` (Win32, Vista+): system-wide hot key; with `hWnd = NULL` the
  `WM_HOTKEY` message goes to the calling thread's queue; fails if another app already
  registered the same combination; `MOD_NOREPEAT` suppresses auto-repeat; Win-key
  combinations "are reserved for use by the operating system"; F12 is reserved for
  debuggers; some OS defaults (PrintScreen) can be overridden when one of your windows is
  foreground. <https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerhotkey>
  (page dated 2025-07-01).
- Measured: registering Ctrl+Alt+T with `MOD_NOREPEAT` via ctypes succeeded, and a Qt
  `QAbstractNativeEventFilter` received exactly one `WM_HOTKEY` after a synthetic key press
  (`keybd_event`). No hook thread needed. I did not verify that the frozen PyInstaller
  builds produce the same results (they are windowed and print nothing); only that they
  start and exit cleanly.
- Python libraries: `keyboard` 0.13.5 was last released 2020-03-23 and its README says
  "This project is currently unmaintained"; it also warns that some games install hooks
  that swallow key events. <https://github.com/boppreh/keyboard>. `pynput` 1.8.2 was
  released 2026-05-12 and is maintained, but both use low-level keyboard hooks, which are
  heavier, can lag if the callback is slow, and are the kind of behaviour anti-cheat
  software dislikes (general knowledge, not verified from a source here).
- Behaviour to design for: registration can fail, so the settings UI must show "hotkey
  taken by another app" and let the user rebind; check the return value of
  `RegisterHotKey`; release hotkeys on exit; keep Ctrl+Alt+R (select region) as a window
  independent of overlay focus.

### Smart App Control (SAC) and unsigned builds
- What SAC does: Microsoft Learn (page dated 2026-09-30): SAC "selectively allows apps and
  binaries to run only if they're likely to be safe". If the cloud app-intelligence service
  cannot predict, SAC still allows the app if it is signed by a CA in the Trusted Root
  Program. "Malware, PUA, and unknown, unsigned code are blocked by default." In
  evaluation mode SAC watches and may turn itself off for developers; in enforcement mode
  "apps cannot be run unless they are recognized by Microsoft's app intelligence services,
  or they are signed with a trusted certificate". Check state in Settings > Windows
  Security > App & browser control. <https://learn.microsoft.com/en-us/windows/apps/develop/smart-app-control/overview>
- It checks every loaded module, not just the exe: Eric Lawrence's April 2026 post says SAC
  "evaluates trust/signatures of all code (DLLs, scripts, etc)", requires SHA-256 and a
  chain to a Microsoft Trusted Root (user-installed or self-signed roots do not count),
  and says a signed stub installer that drops unsigned code is not sufficient. It also
  says SAC disables SmartScreen app reputation and that a file with Mark of the Web from
  an untrusted origin may be blocked outright with no override.
  <https://textslashplain.com/2026/04/28/smart-app-control/> (expert blog, secondary).
- Re-enabling: older reporting says that once SAC is turned off it could only be turned on
  again by resetting or reinstalling Windows. The same blog says this limit was lifted in
  Windows 11 25H2 (Windows Security app v1000.29554+) in April 2026. Single-source;
  forum posts in 2026 still describe the reset requirement on older builds.
  <https://windowslatest.com/2025/12/16/microsoft-confirms-you-can-soon-disable-smart-app-control-without-reinstalling-windows-11>
- Measured: on this development PC `VerifiedAndReputablePolicyState` is 0 (SAC off), so
  nothing here proves how a Gloss build behaves under SAC. The audit above is the evidence
  for the risk: an unsigned PyInstaller one-folder build has an unsigned exe plus unsigned
  Python/OpenSSL DLLs. Under enforcement mode, the cloud reputation of a brand-new,
  low-prevalence hash is "unknown", so expect a block with no "Run anyway". One-file
  mode extracts the unsigned DLLs to `%TEMP%` at launch, so it fails the same way.
  This is inference from the sources above, not a tested result.
- Without SAC (the common case on upgraded PCs), an unsigned download shows SmartScreen
  "Windows protected your PC / Unknown publisher" with More info > Run anyway. Browsers
  and zip extractors add Mark of the Web. (General knowledge; the Microsoft SAC page above
  is the only primary source I fetched on this.)

### Cost of getting signed (checked 2026-10-30)
- **Azure Artifact Signing** (renamed from Trusted Signing; GA reported January 2026):
  Basic $9.99/month for 5,000 signatures, Premium $99.99/month for 100,000, $0.005 per
  extra signature, per secondary sources (DevClass 2026-01-14 and codenote 2026-06). The
  official pricing page did not show amounts when fetched ("$-"), so confirm in the
  Azure calculator. Billing starts when you create the account and is not pro-rated.
  Certificates are OV-style, "no plan to issue EV".
  <https://azure.microsoft.com/en-us/pricing/details/artifact-signing/>,
  <https://devclass.com/2026/01/14/code-signing-windows-apps-may-be-easier-and-more-secure-with-new-azure-artifact-service/>
- **Eligibility is the catch**: Microsoft Learn quickstart (updated 2026-10-08): public-trust
  certificates are for organisations in the US, Canada, EU, UK, Australia, NZ, Japan, South
  Korea, Singapore, Switzerland, Norway and Israel; "Individual developers must be located
  in the United States or Canada"; identity check uses a government ID via Authenticator
  and takes 1 to 20 business days. The user is described as native Thai, so (location to be
  confirmed) an individual in Thailand is not eligible; Thailand is not in the
  organisation list either.
  <https://learn.microsoft.com/azure/artifact-signing/quickstart>
- **Traditional OV certificate**: since June 2023 the private key must live on certified
  hardware (USB token, cloud HSM) so a downloadable `.pfx` is no longer issued. Reseller
  and blog prices (secondary, 2026): SSL.com OV about $129/year, Sectigo OV from about
  $220/year via resellers, GlobalSign OV about $434, DigiCert OV $696 to $840/year; EV
  $349 to $1,116. Major CAs often do not issue to individuals without a registered
  business; SSL.com sells Individual Validation. Certum cloud OV was quoted at about $348
  for three years by a reseller (unverified). <https://codenote.net/en/posts/ov-ev-code-signing-certificate-pricing-japan-2026/>,
  <https://gabrielmoraru.com/obtaining-cheap-ev-ov-certificates-singing-your-application/>
  A new OV certificate does not start with SmartScreen reputation, but SAC accepts any
  validly chained signature, so signing is enough for SAC.
- **SignPath Foundation (free for open source)**: certificate issued to the Foundation (the
  Foundation appears as publisher); requires an OSI-approved licence, a public
  repository, no proprietary components, MFA, and signing from CI with origin
  verification. LunaTranslator uses it. Policy page requirements may have changed; check
  before applying. <https://signpath.org/terms>, <https://signpath.org/>
- **Microsoft Store (MSIX)**: Windows blog 2025-09-10: individual registration is free, no
  credit card, government ID plus selfie, "nearly 200 markets"; packaging as MSIX gives
  free hosting, free code signing and automatic updates; Win32 apps are supported.
  Some Microsoft pages still show the older $19 fee, so confirm in Partner Center.
  <https://blogs.windows.com/windowsdeveloper/2025/09/10/free-developer-registration-for-individual-developers-on-microsoft-store/>
  Store-signed packages should satisfy SAC (inference: Store apps are signed to a trusted
  root; no source fetched explicitly states SAC allows them). Costs: Store certification
  review per update; a packaged app has a restricted environment for some Win32 features
  (not verified for Gloss: screen capture, `RegisterHotKey`, `SetWindowDisplayAffinity`
  are expected to work in a full-trust packaged desktop app, but test).

## Local measurements

Scripts and raw output (machine: see header; all throwaway):
- `docs/research/scratch/shell_probe.py` and result
  `docs/research/data/shell_probe_2026-10-30.json`:
  `qt_import_ms` 166; hotkey registered true; hotkey messages received 1; tray visible
  true; overlay styles layered, transparent, noactivate all true; `WindowFromPoint` is
  overlay false (click-through works); `mss` pixel 255,0,0 then 228,224,223 after
  `WDA_EXCLUDEFROMCAPTURE`; affinity call returned true.
- `docs/research/scratch/dpi_probe.py`: per-monitor-v2 true; both screens ratio 1.0.
- `docs/research/scratch/wheel_sizes.py`: PyPI wheel sizes.
- PyInstaller builds in `docs/research/scratch/build_pi/` (not meant to be committed;
  add to `.gitignore` or delete). Sizes 93 MB folder / 37.7 MB one-file; signature audit as
  above.
- Not measured: Nuitka (toolchain download failed), WPF, Tauri, Tkinter, SAC enforcement,
  mixed DPI, WGC/DXGI vs affinity, idle CPU of a Qt event loop with a live overlay, and
  VirusTotal/Defender detection of the PyInstaller exe (no upload was done on purpose,
  to avoid publishing a binary).

## Recommendation

**Primary: PySide6 (depend on `PySide6-Essentials`) with PyInstaller one-folder, zipped or
wrapped in an installer, and `RegisterHotKey` through a Qt native event filter.**
- It keeps one language and one process, so contract tests and the pipeline stay simple.
- Everything the spec needs for the shell (tray, click-through overlay,
  `WDA_EXCLUDEFROMCAPTURE`, global hotkey, per-monitor-DPI-v2) was demonstrated in a
  working probe.
- Prefer one-folder over one-file: no per-launch extraction into `%TEMP%`, the first two
  one-file runs were about 1 s slower (later runs matched one-folder), and signing and
  auditing individual files is simpler.
- Hotkeys: wrap `RegisterHotKey` behind a small `HotkeyService` in `ui/` with a clear
  "already in use" error path. Do not use `keyboard`. Use `pynput` only if a feature
  later needs key-up or Win-key combinations.

**Packaging fallback: Nuitka standalone** (evaluate in Phase 5, once, in a slice by
itself). Reason to try it: possibly fewer antivirus detections and smaller output, per
anecdotes only. Reasons for caution: long builds, a toolchain to install, AGPL licence
on the tool itself (check), and I could not build it in this environment.

**Signing plan (decision for the user, in plain words):**
1. For personal use on the user's own PC, no signing is needed unless SAC is "On". Check
   Windows Security > App & browser control > Smart App Control. If it is Off or in
   Evaluation, an unsigned build works (Evaluation mode may turn itself off).
2. If the user later wants to share Gloss: first choice is the **Microsoft Store MSIX
   route** (free for individuals, Microsoft signs, works with SAC) or **SignPath
   Foundation** (free, needs an open-source licence and public repo; note this conflicts
   with the privacy/gitignore rules only if golden images were ever committed, which they
   must not be). Azure Artifact Signing is the cheapest paid route (about $10/month) but
   is not open to individuals in Thailand as of the docs dated 2026-10-08.
3. Until then, document the SAC block as a known limitation in the README, and ship a
   `tools/audit_signatures.ps1` style check in Phase 5 so unsigned binaries are listed
   before every release.

## Rejected

- **Tauri**: small binary, but our code is Python: it would need a Python sidecar
  (PyInstaller anyway) plus IPC and a Rust/JS toolchain, so total size and moving parts
  go up, not down. Click-through and focus behaviour on Windows has open issues.
- **.NET/WPF**: best Windows API access, but would discard the Python ecosystem
  (RapidOCR, pytest contract suite, uv, the agent definitions). Reconsider only if the OCR
  research concludes Windows.Media.Ocr alone is good enough.
- **Tkinter**: no per-pixel transparency, no tray, weak rich-text panel, packaging
  quirks reported for click-through.
- **`keyboard` library**: unmaintained since 2020.
- **PyInstaller one-file as the default**: extraction at every start, slower first start,
  more antivirus surface.
- **UPX compression**: increases antivirus false positives (reported in PyInstaller and
  community threads); do not enable.
- **Paid EV certificate**: $349+ per year and hardware-bound; no SAC benefit beyond a
  normal valid signature, and the user is one person.

## Risks and unknowns

1. **SAC enforcement is untested** (my PC has it off). Mitigation: test in a clean Windows
   11 VM or spare PC with SAC on before any release claim; run the signature audit; have
   the Store/SignPath route ready.
2. **Unsigned third-party DLLs** (python312.dll, OpenSSL, `.pyd` files, onnxruntime): under
   SAC a signed exe alone is not enough. Mitigation: audit every release; prefer the
   python.org build (check its signature) and wheels that ship signed DLLs; the Store
   route signs the whole package.
3. **Run-from-source on a SAC machine**: `uv`'s managed Python was unsigned in my audit, so
   `uv run gloss` may be blocked under enforcement. Needs a test.
4. **Capture exclusion** is verified for mss, DXGI, dxcam and WGC (see `capture.md`), but
   not for HDR or exclusive-fullscreen games. The spec's automated test should run against
   every capture adapter.
5. **Mixed-DPI** was not testable here (both monitors 100%). Add a unit test with synthetic
   rects and a manual test on a 100% + 150% setup.
6. **Hotkey collisions** with game overlays or other tools; failure is silent unless we
   check the return code. Mitigation as above.
7. **Antivirus false positives** on PyInstaller output: treat as likely; plan to submit the
   exact release build to Microsoft's false-positive form and avoid UPX. No VirusTotal result
   gathered.
8. **Qt LGPL obligations**: shipping PySide6 dynamically linked in a one-folder build is the
   simple, compliant layout; include licence texts. Not legal advice.
9. **Source reliability**: signing prices come from reseller blogs and one news article;
   the official Azure page showed no amounts. Re-check before buying anything.

## Sources

- Smart App Control overview (Microsoft Learn, 2026-09-30):
  <https://learn.microsoft.com/en-us/windows/apps/develop/smart-app-control/overview>
- Smart App Control (Text/Slash/Plain, 2026-04-28): <https://textslashplain.com/2026/04/28/smart-app-control/>
- Windows Latest on disabling SAC (2025-12-16):
  <https://windowslatest.com/2025/12/16/microsoft-confirms-you-can-soon-disable-smart-app-control-without-reinstalling-windows-11>
- Artifact Signing quickstart (Microsoft Learn, updated 2026-10-08):
  <https://learn.microsoft.com/azure/artifact-signing/quickstart>
- Artifact Signing pricing page: <https://azure.microsoft.com/en-us/pricing/details/artifact-signing/>
- DevClass, Artifact Signing GA (2026-01-14):
  <https://devclass.com/2026/01/14/code-signing-windows-apps-may-be-easier-and-more-secure-with-new-azure-artifact-service/>
- OV/EV price table and Azure price (codenote, 2026):
  <https://codenote.net/en/posts/ov-ev-code-signing-certificate-pricing-japan-2026/>
- Cheap OV/EV notes: <https://gabrielmoraru.com/obtaining-cheap-ev-ov-certificates-singing-your-application/>
- SignPath Foundation: <https://signpath.org/>, <https://signpath.org/terms>
- Microsoft Store free individual registration (Windows blog, 2025-09-10):
  <https://blogs.windows.com/windowsdeveloper/2025/09/10/free-developer-registration-for-individual-developers-on-microsoft-store/>
- RegisterHotKey: <https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerhotkey>
- SetWindowDisplayAffinity:
  <https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowdisplayaffinity>
- keyboard library: <https://github.com/boppreh/keyboard>
- PyInstaller antivirus issues: <https://github.com/pyinstaller/pyinstaller/issues/5579>,
  <https://github.com/pyinstaller/pyinstaller/issues/5854>
- Nuitka PySide6 anecdotes: <https://github.com/Erriez/pyside6-nuitka-test>,
  <https://reddit.fsky.io/r/Python/comments/1je94cf/pyside6_nuitka_is_very_impressive_some_numbers>
- Tauri: <https://v2.tauri.app/concept/size/>, <https://v2.tauri.app/distribute/windows-installer/>,
  <https://github.com/tauri-apps/tauri/issues/9250>, <https://github.com/tauri-apps/tauri/issues/11461>
- Qt click-through forum threads: <https://forum.qt.io/post/817594>, <https://qtcentre.org/showthread.php?p=310715>
- PyPI JSON API for PySide6 6.12.0, PyInstaller 6.22.3, Nuitka 4.3.3, keyboard 0.13.5,
  pynput 1.8.2 (queried 2026-10-30).
- Local data: `docs/research/data/*_2026-10-30.*`; scripts in `docs/research/scratch/`.
