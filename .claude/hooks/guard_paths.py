"""PreToolUse safeguard for Gloss (runs before Read/Edit/Write/MultiEdit/NotebookEdit/Bash).

Exit code 2 blocks the tool call and shows the stderr message to Claude.
Exit code 0 lets the normal permission flow continue.

Rules
  * Secrets (.env*, *.pem, *.key, secrets/): never readable or writable by Claude.
  * Safeguard files (.claude/hooks/, .claude/settings*.json, unlock files, uv.lock):
    never writable by Claude. Use `uv` commands for the lockfile.
  * Frozen areas: writable until the freeze marker exists. After that, writable only while
    the user's unlock file exists.
      src/gloss/core/contracts/  freeze: .claude/CONTRACTS_FROZEN  unlock: .claude/CONTRACT_UNLOCK
      tests/golden/expected/        freeze: .claude/GOLDEN_FROZEN     unlock: .claude/GOLDEN_UNLOCK
      .claude/agents/               always frozen                     unlock: .claude/TEAM_UNLOCK
    Claude may CREATE a freeze marker (that only adds protection) but never delete or edit
    one, and never create an unlock file.

This is a guardrail against accidents, not a security boundary.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

PROJECT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()

SECRET_PATTERNS = [
    re.compile(r"(^|/)\.env(\.[^/]*)?$"),
    re.compile(r"\.(pem|key|pfx|p12)$"),
    re.compile(r"(^|/)secrets/"),
]
ENV_EXAMPLE = re.compile(r"(^|/)\.env\.example$")

# area prefix -> (freeze marker or None for "always frozen", unlock file)
FROZEN_AREAS = {
    "src/gloss/core/contracts/": (".claude/CONTRACTS_FROZEN", ".claude/CONTRACT_UNLOCK"),
    "tests/golden/expected/": (".claude/GOLDEN_FROZEN", ".claude/GOLDEN_UNLOCK"),
    ".claude/agents/": (None, ".claude/TEAM_UNLOCK"),
}
FREEZE_MARKERS = {m for m, _ in FROZEN_AREAS.values() if m}
UNLOCK_FILES = {u for _, u in FROZEN_AREAS.values()}
NEVER_WRITE = [
    ".claude/hooks/",
    ".claude/settings.json",
    ".claude/settings.local.json",
    "uv.lock",
    *sorted(UNLOCK_FILES),
]

WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read"}
# Shell verbs that can modify files (bash + PowerShell + cmd). Heuristic only.
SHELL_WRITE = re.compile(
    r"(>|\btee\b|\brm\b|\bmv\b|\bcp\b|\bsed\s+-i|\btruncate\b|\btouch\b|\bunlink\b|"
    r"Set-Content|Add-Content|Out-File|Remove-Item|Move-Item|Copy-Item|New-Item|Rename-Item|"
    r"\bdel\b|\berase\b|\bren\b|\bcopy\b|\bmove\b|write_text|unlink\(|open\([^)]*['\"][wa])",
    re.IGNORECASE,
)
DELETE_VERB = re.compile(
    r"(\brm\b|\brmdir\b|\brd\b|\bdel\b|\berase\b|Remove-Item|\bunlink\b|rmtree|unlink\()",
    re.IGNORECASE,
)


def norm(path_str: str) -> str:
    """Project-relative POSIX path, case-folded on Windows."""
    p = path_str.replace("\\", "/")
    try:
        r = Path(p).resolve().relative_to(PROJECT).as_posix()
    except (ValueError, OSError):
        r = p
    return r.lower() if os.name == "nt" else r


def exists(rel_path: str) -> bool:
    return (PROJECT / rel_path).exists()


def same(a: str, b: str) -> bool:
    return a.lower() == b.lower() if os.name == "nt" else a == b


def starts(a: str, prefix: str) -> bool:
    return a.lower().startswith(prefix.lower()) if os.name == "nt" else a.startswith(prefix)


def is_secret(r: str) -> bool:
    return not ENV_EXAMPLE.search(r) and any(p.search(r) for p in SECRET_PATTERNS)


def area_locked(freeze: str | None, unlock: str) -> bool:
    frozen = True if freeze is None else exists(freeze)
    return frozen and not exists(unlock)


def block(msg: str) -> None:
    print(f"BLOCKED by Gloss safeguard: {msg}", file=sys.stderr)
    sys.exit(2)


def check_write(r: str, tool: str) -> None:
    if is_secret(r):
        block(f"'{r}' looks like a secrets file. Store secrets with keyring (services/secrets.py).")

    for marker in FREEZE_MARKERS:
        if same(r, marker):
            if tool == "Write" and not exists(marker):
                return  # creating a freeze marker only adds protection
            block(f"'{r}' is a freeze marker. Only the user may remove or change it.")

    for prefix in NEVER_WRITE:
        if same(r, prefix) or starts(r, prefix):
            block(
                f"'{r}' is a protected safeguard file. Only the user may change it. "
                "Explain what you need and ask the user."
            )

    for prefix, (freeze, unlock) in FROZEN_AREAS.items():
        if starts(r, prefix) and area_locked(freeze, unlock):
            block(
                f"'{r}' is frozen. To change it: explain why (write an ADR for contracts), then "
                f"ask the user to create the empty file '{unlock}', and to delete it afterwards."
            )


def check_bash(cmd: str) -> None:
    text = cmd.replace("\\", "/")
    low = text.lower()

    if re.search(r"(^|[\s'\"/=])\.env(\.\w+)?\b", text) and ".env.example" not in low:
        block("shell access to .env files is not allowed (possible secrets).")

    if not SHELL_WRITE.search(text):
        return

    if DELETE_VERB.search(text) and re.search(r"(^|[\s'\"/])\.claude(/|\s|['\"]|$)", text):
        block("deleting anything under .claude/ is not allowed. Ask the user.")

    protected = [p for p in NEVER_WRITE if p != "uv.lock"] + sorted(FREEZE_MARKERS)
    protected += [p for p, (f, u) in FROZEN_AREAS.items() if area_locked(f, u)]
    for p in protected:
        if p.lower().rstrip("/") in low:
            block(
                f"this shell command appears to modify protected path '{p}'. "
                "Use the normal workflow or ask the user."
            )


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # never break Claude Code on malformed input

    tool = data.get("tool_name", "")
    tin = data.get("tool_input") or {}

    if tool in READ_TOOLS:
        path = tin.get("file_path", "")
        if path and is_secret(norm(path)):
            block(f"reading '{norm(path)}' is not allowed (possible secrets).")
    elif tool in WRITE_TOOLS:
        path = tin.get("file_path") or tin.get("notebook_path") or ""
        if path:
            check_write(norm(path), tool)
    elif tool in {"Bash", "PowerShell"}:
        check_bash(str(tin.get("command", "")))

    sys.exit(0)


if __name__ == "__main__":
    main()
