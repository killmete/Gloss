"""CLAUDE.md rule 1: no adapter imports another adapter.

ports.py goes further: an adapter depends only on gloss.core.contracts, its own third-party
library, and shared helpers in gloss.services. It also needs the registry decorator, so
gloss.core.registry is allowed too.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
ADAPTER_ROOT = SRC / "gloss" / "adapters"
ALLOWED_GLOSS = ("gloss.core.contracts", "gloss.core.registry", "gloss.services")
_ADAPTER_REF = re.compile(r"^gloss\.adapters\.\w+\.\w+")
_PORT_PACKAGE = re.compile(r"^gloss\.adapters\.\w+$")


def adapter_id(module: str) -> str:
    """'gloss.adapters.ocr.rapid.helpers' -> 'gloss.adapters.ocr.rapid'."""
    return ".".join(module.split(".")[:4])


def resolve(node: ast.ImportFrom, module: str, is_package: bool) -> list[str]:
    """Every module an ``import`` statement could be loading, as absolute dotted names."""
    if node.level:
        parts = module.split(".")
        base = parts if is_package else parts[:-1]
        base = base[: len(base) - (node.level - 1)]
        prefix = ".".join(base + ([node.module] if node.module else []))
    else:
        prefix = node.module or ""
    # "from a.b import c" may import the module a.b.c, so check both.
    return [prefix, *(f"{prefix}.{alias.name}" for alias in node.names)]


def imports_of(source: str, module: str, is_package: bool = False) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            found += resolve(node, module, is_package)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # importlib.import_module("gloss.adapters.x.y"), __import__, config strings
            if _ADAPTER_REF.match(node.value):
                found.append(node.value)
    return found


def isolation_violations(source: str, module: str, is_package: bool = False) -> list[str]:
    own = adapter_id(module)
    problems = []
    for target in imports_of(source, module, is_package):
        if target.startswith("gloss.adapters.") and target.count(".") >= 3:
            if adapter_id(target) != own:
                problems.append(f"imports another adapter: {target}")
    return problems


def dependency_violations(source: str, module: str, is_package: bool = False) -> list[str]:
    own = adapter_id(module)
    problems = []
    for target in imports_of(source, module, is_package):
        if target != "gloss" and not target.startswith("gloss."):
            continue
        if target.startswith(own) or target.startswith(ALLOWED_GLOSS):
            continue
        if target in ("gloss", "gloss.core", "gloss.adapters") or _PORT_PACKAGE.match(target):
            continue  # a bare package, such as the left side of "from gloss.core import x"
        problems.append(f"depends on {target}")
    return problems


def adapter_files() -> list[tuple[Path, str, bool]]:
    files = []
    for path in sorted(ADAPTER_ROOT.rglob("*.py")):
        relative = path.relative_to(SRC).with_suffix("")
        parts = list(relative.parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts.pop()
        if len(parts) >= 4:  # gloss.adapters.<port>.<adapter>[...]
            files.append((path, ".".join(parts), is_package))
    return files


# ---- the real tree -----------------------------------------------------------------------


def test_no_adapter_imports_another_adapter():
    problems = {}
    for path, module, is_package in adapter_files():
        found = isolation_violations(path.read_text(encoding="utf-8"), module, is_package)
        if found:
            problems[str(path.relative_to(SRC))] = found
    assert not problems, f"adapters must not import each other: {problems}"


def test_adapters_depend_only_on_contracts_the_registry_and_services():
    problems = {}
    for path, module, is_package in adapter_files():
        found = dependency_violations(path.read_text(encoding="utf-8"), module, is_package)
        if found:
            problems[str(path.relative_to(SRC))] = found
    assert not problems, f"adapters may import only {ALLOWED_GLOSS}: {problems}"


def test_the_adapter_scan_looks_at_the_directories_adapters_will_live_in():
    assert ADAPTER_ROOT.is_dir()
    for port in ("capture", "ocr", "translate", "render"):
        assert (ADAPTER_ROOT / port).is_dir(), f"missing adapters/{port}"


# ---- the detector itself must catch what it claims to ------------------------------------

OWN = "gloss.adapters.ocr.rapid"


@pytest.mark.parametrize(
    "source",
    [
        "import gloss.adapters.ocr.windows",
        "from gloss.adapters.ocr import windows",
        "from gloss.adapters.ocr.windows import Engine",
        "from gloss.adapters.translate.anthropic import Client",
        "from . import windows",
        "from .windows import Engine",
        "from ..translate import anthropic",
        "from ..translate.anthropic import Client",
        "import importlib\nimportlib.import_module('gloss.adapters.ocr.windows')",
        "__import__('gloss.adapters.render.panel')",
        "def late():\n    from gloss.adapters.ocr.windows import Engine",
    ],
)
def test_the_detector_flags_every_way_of_importing_a_sibling(source):
    assert isolation_violations(source, OWN), source


@pytest.mark.parametrize(
    "source",
    [
        "import numpy as np",
        "from gloss.core.contracts import Frame, TextBlock",
        "from gloss.core.registry import register_adapter",
        "from gloss.services.redaction import redact",
        "from gloss.adapters.ocr.rapid import helper",
        "from gloss.adapters.ocr.rapid.sub import helper",
        "import os",
        "x = 'gloss.adapters.ocr.rapid.models'",
        "x = 'text that merely mentions gloss.adapters.ocr.windows in a sentence'",
    ],
)
def test_the_detector_leaves_legitimate_imports_alone(source):
    assert isolation_violations(source, OWN) == []


def test_a_package_style_adapter_may_import_its_own_submodules():
    own_package = "gloss.adapters.ocr.rapid"
    assert isolation_violations("from . import models", own_package, True) == []
    assert isolation_violations("from .models import Loader", own_package, True) == []
    assert (
        isolation_violations("from . import windows", own_package, True) == []
    )  # its own submodule
    assert isolation_violations("from ..windows import Engine", own_package, True)


@pytest.mark.parametrize(
    "source",
    [
        "from gloss.core.config import load_config",
        "from gloss.ui.tray import Tray",
        "import gloss.app",
        "from gloss.core.pipeline import Pipeline",
        "from gloss import app",
    ],
)
def test_the_dependency_detector_flags_forbidden_imports(source):
    assert dependency_violations(source, OWN), source


@pytest.mark.parametrize(
    "source",
    [
        "from gloss.core.contracts import Frame",
        "from gloss.core.contracts.models import Frame",
        "from gloss.core.registry import register_adapter",
        "from gloss.services.secrets import SecretStore",
        "import cv2, numpy",
        "from pathlib import Path",
    ],
)
def test_the_dependency_detector_allows_the_documented_dependencies(source):
    assert dependency_violations(source, OWN) == []
