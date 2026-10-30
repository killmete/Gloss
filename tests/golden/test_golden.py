"""Golden accuracy: real screenshots through the real OCR engine, scored against the
human-checked text in tests/golden/expected/ (docs/SPEC.md section 8, criterion 2).

Run with: uv run pytest -m golden

The images and the expected text are local and gitignored, because they hold other people's
chats. Where they are absent these tests skip, and say so. They are never edited to make a
score pass: fix the code, or raise it with the user.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image
from testkit.core import STRICT, call
from testkit.discovery import NO_USER_CONFIG, registered_names
from testkit.golden import (
    CATEGORY_BY_STEM,
    EXPECTED,
    IMAGES,
    THRESHOLDS,
    aggregate,
    failures,
    score_image,
)

from gloss.core.config import load_config
from gloss.core.contracts import AdapterContext, AdapterUnavailable, Frame, Rect
from gloss.core.registry import default_registry

pytestmark = pytest.mark.golden


def local_data() -> tuple[list, list]:
    images = sorted(IMAGES.glob("*.png")) if IMAGES.is_dir() else []
    texts = sorted(EXPECTED.glob("*.txt")) if EXPECTED.is_dir() else []
    return images, texts


@pytest.fixture(scope="module")
def golden_files():
    images, texts = local_data()
    if not images or not texts:
        pytest.skip(
            "golden data is not on this machine: tests/golden/images/ and tests/golden/expected/ "
            "are gitignored, because they hold other people's chats"
        )
    return images, texts


def test_every_image_has_expected_text_and_a_category(golden_files):
    images, texts = golden_files
    image_stems = {p.stem for p in images}
    text_stems = {p.stem for p in texts}
    assert image_stems == text_stems, (
        f"images without expected text: {sorted(image_stems - text_stems)}; "
        f"expected text without an image: {sorted(text_stems - image_stems)}"
    )
    uncategorised = sorted(image_stems - set(CATEGORY_BY_STEM))
    assert not uncategorised, (
        f"add these to CATEGORY_BY_STEM in tests/testkit/golden.py so they are scored: "
        f"{uncategorised}"
    )


def test_expected_text_comes_from_expected_not_from_a_draft(golden_files):
    _, texts = golden_files
    assert all(p.parent == EXPECTED for p in texts), "only frozen text in expected/ counts"
    assert not any(p.name.startswith("_") for p in texts)


@pytest.fixture(scope="module", params=sorted(THRESHOLDS))
def scores(request, golden_files):
    """Run every screenshot through the configured OCR engine once per profile."""
    profile = request.param
    config = load_config(user_path=NO_USER_CONFIG).config
    engine = config.ocr.engine
    if engine not in registered_names("ocr"):
        pytest.skip(f"the configured OCR engine '{engine}' is not registered yet (Phase 3)")
    settings = {**config.adapter_settings("ocr", engine), "profile": profile}
    adapter = default_registry.create("ocr", engine, AdapterContext(settings=settings))
    try:
        try:
            call(adapter.open)
        except AdapterUnavailable as exc:
            if STRICT:
                raise
            pytest.skip(f"OCR engine '{engine}' ({profile}) is unavailable here: {exc}")
        images, _ = golden_files
        results = []
        for path in images:
            with Image.open(path) as picture:
                pixels = np.array(picture.convert("RGB"), dtype=np.uint8)
            height, width = pixels.shape[:2]
            frame = Frame(pixels, "golden", Rect(0, 0, width, height), 0.0, 1.0)
            blocks = call(adapter.recognize, frame, None)
            kept = [
                b.text
                for b in blocks
                if b.confidence is None or b.confidence >= config.ocr.min_confidence
            ]
            expected = (EXPECTED / f"{path.stem}.txt").read_text(encoding="utf-8")
            results.append(score_image(path.stem, CATEGORY_BY_STEM[path.stem], expected, kept))
        return profile, aggregate(results)
    finally:
        adapter.close()


@pytest.mark.parametrize("scope", ["chat", "profile", "menu", "order card", "ALL"])
def test_accuracy_and_recall_meet_the_thresholds(scores, scope):
    profile, groups = scores
    problems = [p for p in failures(groups, profile) if p.startswith(f"{scope}:")]
    assert not problems, f"profile '{profile}': " + "; ".join(problems)
