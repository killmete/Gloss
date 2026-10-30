"""PostToolUse hook: auto-format and lint-fix the Python file Claude just edited.

Silent no-op if ruff isn't available yet (e.g. before Phase 0 finishes). Always exits 0,
so it can never block work. Lint problems that can't be auto-fixed are reported to Claude
on stderr as information.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


def ruff_cmd() -> list[str] | None:
    if shutil.which("ruff"):
        return ["ruff"]
    if shutil.which("uv") and Path(".venv").exists():
        return ["uv", "run", "--no-sync", "ruff"]
    return None


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    path = (data.get("tool_input") or {}).get("file_path", "")
    if not path.endswith(".py") or not Path(path).exists():
        sys.exit(0)
    ruff = ruff_cmd()
    if not ruff:
        sys.exit(0)
    try:
        subprocess.run([*ruff, "format", "--quiet", path], timeout=20, check=False)
        res = subprocess.run(
            [*ruff, "check", "--fix", "--quiet", path],
            timeout=20,
            check=False,
            capture_output=True,
            text=True,
        )
        if res.returncode != 0 and res.stdout.strip():
            print(f"ruff issues remaining in {path}:\n{res.stdout.strip()}", file=sys.stderr)
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
