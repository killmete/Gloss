# ADR-0004: Project tooling and initial dependencies

- Status: **Proposed**, pending user approval at Checkpoint 2
- Date: 2026-10-31
- Code: `pyproject.toml`, `.python-version`, `uv.lock`

## Context

Phase 2 creates the Python project. CLAUDE.md rule 9 requires a pinned lockfile, changed only through `uv`, one dependency at a time.

## Options

**Build backend:** `uv_build`, `hatchling`, `setuptools`.

**Version pins:** exact in `pyproject.toml` for everything, or exact only where the ADR requires it, with `uv.lock` pinning the rest.

## Decision

- **Python 3.12 only** (`>=3.12,<3.13`, `.python-version` = 3.12). This PC's default Python is 3.15, so the upper bound stops uv choosing it.
- **`uv_build`**, which ships with uv and needs no extra tool.
- **Exact pins** where ADR-0001 requires them, and for ruff: `rapidocr==3.10.0`, `onnxruntime==1.31.1`, `mss==10.2.0`, `windows-capture==2.0.1`, `ruff==0.17.3`. `pyside6-essentials` is held to 6.12.x. `uv.lock` pins everything else.
- **`offline` extra:** `ctranslate2` and `sentencepiece`, not in the default install.
- **Dev group:** `ruff`, `pytest`, `psutil`, `pillow`.
- **Ruff** excludes `docs/research/scratch` and `.claude/hooks`, and nothing else.
- **`pytest`** by default skips the `golden`, `bench` and `e2e` markers, to stay fast.
- **Entry point:** `gloss = "gloss.app:main"`. For now `app.py` is a placeholder that loads config, and the orchestrator fills it in Phase 3.

## Consequences

- OpenCV arrives twice, both at 5.0.0.93: `opencv-python` from `windows-capture`, and `opencv-python-headless` from `rapidocr`. Both install into the same `cv2` folder, which is 113 MB here, so whichever installs last supplies the files. The behaviour of that overlap is untested.
- **Phase 4 size check:** measure OpenCV's share of install size and RAM against the 400 MB target, and confirm the frozen build carries one working `cv2`.
- `winrt-*` packages for the Windows OCR adapter and PyInstaller are not added yet. Their owners add them with `uv add` when the work starts.
- Ruff 0.17 also formats Python code blocks in Markdown, so `ruff format --check .` covers docs.

## Revisit when

- RapidOCR 3.11 is evaluated under `docs/UPDATING.md`.
- Only one OpenCV package is needed, if the Phase 4 check or a `windows-capture` update allows dropping `opencv-python`.
