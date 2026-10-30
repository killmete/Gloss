# Updating Gloss safely

Goal: updates never change what the app does unexpectedly. The `update-steward` agent follows this file. If you run a step yourself, every command runs from the project folder.

## If `uv` is not found

Close and reopen the terminal first, because a new install only reaches new windows. If it still fails, check that it is installed:

```
winget list astral-sh.uv
```

If it is listed, run it by its full path. In PowerShell:

```
& "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe\uv.exe" --version
```

If it is not listed, install it, then reopen the terminal:

```
winget install astral-sh.uv
```

## Not in place yet

- `tests/bench/baseline.json` does not exist. It is created in Phase 4, so until then there is no benchmark baseline to compare with.
- `uv run pytest -m e2e` and `uv run pytest -m bench` collect no tests today, and pytest reports that as exit code 5. That is not a pass. Do not treat either as a safety check until these suites exist.
- If an update must happen before Phase 4, say in its record that these two checks were not available.

## Rules

- One change at a time: one package, one model ID, or one runtime.
- Work on a branch named `update/<thing>-<version>`. Merge after `safeguard` returns PASS.
- Never hand-edit `uv.lock`. Change it only through `uv`.
- Never change a test's expected result, or a threshold, to get a pass.
- If an update would need a contract change, stop and hand it to the architect.

## Pinned on purpose

| Package | Pin | Why |
|---|---|---|
| `rapidocr` | `3.10.0` | Benchmarked version. A new release may change model files |
| `onnxruntime` | `1.31.1` | Benchmarked version |
| `mss` | `10.2.0` | Tested version |
| `windows-capture` | `2.0.1` | Tested version |
| `pyside6-essentials` | `6.12.x` | Windowing behaviour was probed on this version |
| `ruff` | `0.17.3` | New versions can change formatting |

OpenCV arrives through two packages, `opencv-python` and `opencv-python-headless`, which share one `cv2` folder. Keep them on the same version when updating `rapidocr` or `windows-capture` (ADR-0004).

## Updating a package

1. Create the branch.

   ```
   git switch -c update/<package>-<version>
   ```

2. Read the release notes between the two versions. List breaking changes, deprecations and licence changes.
3. Confirm the baselines are current on `main`: `tests/bench/baseline.json` and the golden results.
4. Apply the change, using the first form for a package pinned with `==` and the second for the rest.

   ```
   uv add "<package>==<version>"
   uv lock --upgrade-package <package>
   uv sync
   ```

5. Run the full check.

   ```
   uv run ruff check .
   uv run ruff format --check .
   uv run pytest
   uv run pytest -m golden
   uv run pytest -m e2e
   uv run pytest -m bench
   ```

6. Compare with the baseline. A golden score below its threshold or a benchmark regression over the agreed limit means stop. Benchmark timings vary by about +/- 30%, so the 20% rule needs the quieter Phase 4 method before it can be trusted.
7. If something breaks, fix it inside that library's adapter only.
8. Record the change, plus an ADR if behaviour or defaults changed, including the previous version and rollback steps.
9. Run `safeguard`. Merge after PASS.

## Updating RapidOCR or OCR models

Treat this as a behaviour change, even for a small version bump. Run the golden suite on both the `fast` and `accurate` profiles, and re-check RAM against the 400 MB target. Model files are bundled, and nothing downloads at runtime.

## Changing the translation model

1. Check the provider's docs for the new model ID, prices, retirement dates and request format.
2. Change `model` under `[translate.adapters.anthropic]` in `config/default.toml`.
3. Add its entry under `[translate.prices]`. A model with no entry is charged at the highest listed price, so the estimate stays safe but too high.
4. Check that its `thinking` and `effort` settings are allowed. Some models reject `thinking = "disabled"`.
5. Re-run the translation quality and cache spikes in `tests/bench/spikes/`, which use the user's key from keyring.
6. Record the change in an ADR. A model change alters translation behaviour, even with no code change.

## Changing prices

Edit `[translate.prices]` in `config/default.toml`. Prices are in US dollars per million tokens. Check them against the provider's pricing page.

## Changing the config format

Raise `CURRENT_CONFIG_VERSION` in `core/config_migrations.py`, add a migration step, update `config_version` in `config/default.toml`, and write an ADR. The user's file is upgraded with a backup beside it.

## Changing a contract

Follow CLAUDE.md rule 2. It needs an ADR, the user's approval and `.claude/CONTRACT_UNLOCK`, a version bump, and a migration of every adapter.

## Updating Python

Change `requires-python` and `.python-version` together, on their own branch, then run the full check. Check first that every dependency has wheels for the new version.

## Rolling back

Before merge, delete the branch. After merge, revert the merge commit, which keeps history intact.

```
git revert -m 1 <merge-commit>
uv sync
```

For a released build, reinstall the previous build, and check out its git tag if you need the matching code. A user's `config.toml.v<N>.bak` file restores their previous settings.
