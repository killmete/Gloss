"""CLAUDE.md rule 5 and 8: nothing in src/ hard-codes model IDs, API URLs or keys.

Those live in config/default.toml (or in the keyring). Docstrings and comments may mention
them, because documentation is not behaviour.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

# (path relative to src, literal) pairs that are allowed, each with a reason. Keep this empty
# unless the user agrees to an exception.
ALLOWED: dict[tuple[str, str], str] = {}

_PATTERNS = {
    "model ID": re.compile(
        r"\b(?:claude|gpt|gemini|llama|mistral|deepseek|qwen)-[\w.-]*\d"
        r"|\b(?:haiku|sonnet|opus)-\d"
        r"|\bPP-OCRv\d",
        re.IGNORECASE,
    ),
    "URL": re.compile(r"https?://|\bwww\.", re.IGNORECASE),
    "API host": re.compile(
        r"\b[\w-]+\.(?:anthropic\.com|openai\.com|deepl\.com|googleapis\.com|azure\.com)\b",
        re.IGNORECASE,
    ),
    "API key": re.compile(
        r"\bsk-[A-Za-z0-9_-]{8,}|\bAKIA[0-9A-Z]{16}\b|\bgh[pousr]_[A-Za-z0-9]{20,}"
        r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    ),
}
_SECRET_NAME = re.compile(r"(?:^|_)(?:api_?key|secret|token|password|passwd|auth)$", re.IGNORECASE)


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                ids.add(id(first.value))
    return ids


def _target_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _secret_assignments(tree: ast.AST) -> list[tuple[str, str]]:
    """(name, value) where a secret-looking name is given a literal string of real length."""
    found = []

    def check(name: str | None, value: ast.AST | None) -> None:
        if (
            name
            and value is not None
            and _SECRET_NAME.search(name)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and len(value.value) >= 8
        ):
            found.append((name, value.value))

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                check(_target_name(target), node.value)
        elif isinstance(node, ast.AnnAssign):
            check(_target_name(node.target), node.value)
        elif isinstance(node, ast.keyword):
            check(node.arg, node.value)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    check(key.value, value)
    return found


def findings(source: str) -> list[str]:
    """Every hard-coded external in ``source``, as readable strings."""
    tree = ast.parse(source)
    skip = _docstring_ids(tree)
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            for label, pattern in _PATTERNS.items():
                match = pattern.search(node.value)
                if match:
                    problems.append(f"{label}: {match.group(0)!r}")
    problems += [f"key assigned to {name}" for name, _ in _secret_assignments(tree)]
    return problems


def src_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _relative(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


# ---- the real tree -----------------------------------------------------------------------


def test_src_hard_codes_no_model_ids_urls_or_keys():
    problems = {}
    for path in src_files():
        found = [
            f
            for f in findings(path.read_text(encoding="utf-8"))
            if (_relative(path), f.split(": ", 1)[-1].strip("'")) not in ALLOWED
        ]
        if found:
            problems[_relative(path)] = found
    assert not problems, (
        f"move these into config/default.toml (or the keyring for secrets): {problems}"
    )


def test_the_scan_covers_the_whole_package():
    names = {_relative(p) for p in src_files()}
    assert "gloss/core/config.py" in names and "gloss/core/registry.py" in names


def test_the_values_the_scan_guards_really_are_in_default_toml():
    """If these moved out of the config, the scan above would be guarding nothing."""
    data = tomllib.loads((ROOT / "config" / "default.toml").read_text(encoding="utf-8"))
    anthropic = data["translate"]["adapters"]["anthropic"]
    assert _PATTERNS["model ID"].search(anthropic["model"])
    assert data["translate"]["prices"], "prices belong in config"
    assert "api_key" not in anthropic and "key" not in anthropic, "no secrets in config"


def test_the_allow_list_is_empty():
    assert ALLOWED == {}, "an exception to the no-hard-coding rule needs the user's agreement"


# ---- the detector must catch what it claims to -------------------------------------------


@pytest.mark.parametrize(
    ("source", "label"),
    [
        ('MODEL = "claude-haiku-5-5"', "model ID"),
        ('client.create(model="claude-sonnet-5-5")', "model ID"),
        ('name = "gpt-4o"', "model ID"),
        ('name = "gemini-2.5-flash"', "model ID"),
        ('ver = "PP-OCRv6"', "model ID"),
        ('BASE = "https://api.anthropic.com/v1"', "URL"),
        ('BASE = "http://localhost:8080"', "URL"),
        ('host = "api.deepl.com"', "API host"),
        ('f"https://x.test/{path}"', "URL"),
        ('KEY = "sk-ant-api03-abcdefghijkl"', "API key"),
        ('t = "ghp_abcdefghijklmnopqrstuvwxyz"', "API key"),
        ('pem = "-----BEGIN RSA PRIVATE KEY-----"', "API key"),
        ('api_key = "abcdefgh12345678"', "key assigned"),
        ('self.token = "abcdefgh12345678"', "key assigned"),
        ('Client(api_key="hunter2hunter2")', "key assigned"),
        ('{"password": "correct-horse-battery"}', "key assigned"),
        ('class C:\n    API_KEY: str = "abcdefgh12345678"', "key assigned"),
    ],
)
def test_the_detector_flags_hard_coded_externals(source, label):
    assert any(label in f for f in findings(source)), (source, findings(source))


@pytest.mark.parametrize(
    "source",
    [
        '"""Docs may say claude-haiku-5-5 or https://example.com."""\nx = 1',
        'def f():\n    """Calls https://api.anthropic.com/v1 for you."""\n    return 1',
        "class C:\n    '''See sk-ant-api03-abcdefghijkl'''\n    pass",
        "# comment: model claude-haiku-5-5 at https://example.com\nx = 1",
        'secret_name = "anthropic"',
        "api_key = None",
        'api_key = ""',
        'token = "short"',
        'key = secrets.get(settings["secret_name"])',
        'model = settings["model"]',
        'message = "Your claude is not configured"',
        'print("see the settings window")',
    ],
)
def test_the_detector_leaves_ordinary_code_alone(source):
    assert findings(source) == [], (source, findings(source))
