"""Scoring for the golden screenshots. The metric is the one used in the Phase 1 benchmark.

Order-independent, because layouts are multi-column. For each expected line ``e`` (at least two
characters once normalised) take the best candidate among every OCR line and every pair of
consecutive OCR lines. If the candidate is longer than ``e``, compare ``e`` with the
best-aligned window of ``len(e)``. ``err = Levenshtein(e, window) / len(e)``, capped at 1.

Character accuracy is ``1 - sum(err * len) / sum(len)``. Line recall is the share of expected
lines with ``err <= 0.2``. There is no one-to-one matching, so a repeated label can be credited
twice, which makes the score slightly optimistic. That is deliberate: it keeps today's numbers
comparable with the Phase 1 research in docs/research/ocr-benchmark.md.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from rapidfuzz.distance import Levenshtein
from rapidfuzz.fuzz import partial_ratio_alignment

ROOT = Path(__file__).resolve().parents[2]
IMAGES = ROOT / "tests" / "golden" / "images"
EXPECTED = ROOT / "tests" / "golden" / "expected"
LINE_OK_ERR = 0.2
MIN_LINE_CHARS = 2

# The category of each screenshot, by file name. Adding a screenshot means adding it here, so
# that it cannot silently fall outside the thresholds.
CATEGORY_BY_STEM = {
    "Screenshot 2026-10-30 220641": "chat",
    "Screenshot 2026-10-30 220647": "profile",
    "Screenshot 2026-10-30 220653": "menu",
    "Screenshot 2026-10-30 220658": "profile",
    "Screenshot 2026-10-30 220702": "profile",
    "Screenshot 2026-10-30 220714": "menu",
    "Screenshot 2026-10-30 220722": "profile",
    "Screenshot 2026-10-30 220728": "menu",
    "Screenshot 2026-10-30 220741": "profile",
    "Screenshot 2026-10-30 220749": "profile",
    "Screenshot 2026-10-30 220754": "profile",
    "Screenshot 2026-10-30 220807": "chat",
    "Screenshot 2026-10-30 220814": "chat",
    "Screenshot 2026-10-30 220842": "order card",
    "Screenshot 2026-10-30 220848": "menu",
    "Screenshot 2026-10-30 220904": "profile",
    "Screenshot 2026-10-30 220916": "profile",
    "Screenshot 2026-10-30 220935": "profile",
    "Screenshot 2026-10-30 221006": "chat",
}

# docs/SPEC.md section 8, criterion 2. (character accuracy %, line recall %).
# tests/unit/test_golden_scoring.py checks these against the SPEC table, so they cannot be
# loosened here without that test failing. "gate" blocks the MVP, "floor" blocks a regression.
THRESHOLDS: dict[str, dict[str, tuple[float, float, str]]] = {
    "fast": {
        "chat": (97.0, 95.0, "gate"),
        "ALL": (95.0, 92.0, "gate"),
        "profile": (95.0, 93.0, "floor"),
        "menu": (93.0, 88.0, "floor"),
        "order card": (93.0, 88.0, "floor"),
    },
    # The accurate profile adds menu >= 97.0 / 95.0.
    "accurate": {
        "chat": (97.0, 95.0, "gate"),
        "ALL": (95.0, 92.0, "gate"),
        "profile": (95.0, 93.0, "floor"),
        "menu": (97.0, 95.0, "gate"),
        "order card": (93.0, 88.0, "floor"),
    },
}


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("[?]", "")
    return "".join(c for c in text.lower() if unicodedata.category(c)[0] not in "PSZC")


def best_err(expected: str, candidates: list[str]) -> float:
    best = 1.0
    for cand in candidates:
        if not cand:
            continue
        if len(cand) > len(expected):
            align = partial_ratio_alignment(expected, cand)
            window = cand[align.dest_start : align.dest_start + len(expected)]
            err = Levenshtein.distance(expected, window) / len(expected)
        else:
            err = min(1.0, Levenshtein.distance(expected, cand) / len(expected))
        best = min(best, err)
    return best


def expected_lines(text: str) -> list[str]:
    lines = [norm(line) for line in text.splitlines()]
    return [line for line in lines if len(line) >= MIN_LINE_CHARS]


@dataclass(frozen=True)
class ImageScore:
    stem: str
    category: str
    chars: int
    error_chars: float
    lines: int
    found: int
    missed: tuple[str, ...]


def score_image(stem: str, category: str, expected_text: str, ocr_lines: list[str]) -> ImageScore:
    expected = expected_lines(expected_text)
    seen = [norm(line) for line in ocr_lines]
    candidates = seen + [a + b for a, b in pairwise(seen)]
    errs = [(e, best_err(e, candidates)) for e in expected]
    return ImageScore(
        stem=stem,
        category=category,
        chars=sum(len(e) for e, _ in errs),
        error_chars=sum(len(e) * err for e, err in errs),
        lines=len(errs),
        found=sum(1 for _, err in errs if err <= LINE_OK_ERR),
        missed=tuple(e for e, err in errs if err > LINE_OK_ERR),
    )


@dataclass(frozen=True)
class GroupScore:
    name: str
    images: int
    accuracy: float  # percent
    recall: float  # percent


def aggregate(scores: list[ImageScore]) -> dict[str, GroupScore]:
    groups: dict[str, list[ImageScore]] = {"ALL": list(scores)}
    for s in scores:
        groups.setdefault(s.category, []).append(s)
    result = {}
    for name, members in groups.items():
        chars = sum(m.chars for m in members)
        lines = sum(m.lines for m in members)
        result[name] = GroupScore(
            name,
            len(members),
            100 * (1 - sum(m.error_chars for m in members) / chars) if chars else 0.0,
            100 * sum(m.found for m in members) / lines if lines else 0.0,
        )
    return result


def failures(groups: dict[str, GroupScore], profile: str) -> list[str]:
    """Every threshold the scores miss. An absent category is a failure, not a pass."""
    problems = []
    for name, (min_accuracy, min_recall, kind) in THRESHOLDS[profile].items():
        group = groups.get(name)
        if group is None:
            problems.append(f"{name}: no images in this category ({kind})")
            continue
        if group.accuracy < min_accuracy:
            problems.append(
                f"{name}: character accuracy {group.accuracy:.2f}% < {min_accuracy}% ({kind})"
            )
        if group.recall < min_recall:
            problems.append(f"{name}: line recall {group.recall:.2f}% < {min_recall}% ({kind})")
    return problems
