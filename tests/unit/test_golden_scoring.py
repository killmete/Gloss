"""The golden scorer and its thresholds. These run without any screenshots.

If a threshold here is ever loosened, the SPEC cross-check fails. Thresholds are an agreement
with the user (docs/SPEC.md section 8), not a tuning knob.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from testkit.golden import (
    CATEGORY_BY_STEM,
    EXPECTED,
    IMAGES,
    THRESHOLDS,
    aggregate,
    expected_lines,
    failures,
    norm,
    score_image,
)

SPEC = Path(__file__).resolve().parents[2] / "docs" / "SPEC.md"


def spec_table() -> dict[str, tuple[float, float, str]]:
    rows = {}
    for line in SPEC.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 4 and cells[1].startswith("≥") and cells[2].startswith("≥"):
            name = cells[0].lower().replace("all images", "ALL")
            number = lambda s: float(re.search(r"[\d.]+", s).group(0))  # noqa: E731
            kind = "gate" if "gate" in cells[3].lower() else "floor"
            rows[name] = (number(cells[1]), number(cells[2]), kind)
    return rows


def test_the_fast_profile_thresholds_equal_the_spec_table():
    assert spec_table() == THRESHOLDS["fast"]


def test_the_accurate_profile_is_the_spec_table_plus_the_stricter_menu_rule():
    expected = dict(THRESHOLDS["fast"])
    expected["menu"] = (97.0, 95.0, "gate")
    assert THRESHOLDS["accurate"] == expected
    assert re.search(r"accurate.*adds menu ≥ 97\.0% / 95\.0%", SPEC.read_text(encoding="utf-8"))


def test_every_category_has_a_threshold():
    assert set(CATEGORY_BY_STEM.values()) <= set(THRESHOLDS["fast"])


def test_golden_scoring_reads_the_frozen_folder_not_the_draft():
    assert EXPECTED.name == "expected" and EXPECTED.parent.name == "golden"
    assert IMAGES.name == "images"
    assert "_draft" not in str(EXPECTED)


# ---- the metric --------------------------------------------------------------------------

TEXT = "你好世界\n欢迎来到房间\nOrder accepted\n"


def scored(ocr: list[str], category: str = "chat"):
    return score_image("x", category, TEXT, ocr)


def test_a_perfect_read_scores_100():
    s = scored(["你好世界", "欢迎来到房间", "Order accepted"])
    assert (s.found, s.lines, s.error_chars) == (3, 3, 0)
    assert aggregate([s])["chat"].accuracy == 100.0


def test_one_wrong_character_costs_accuracy_but_not_recall():
    s = scored(["你好世界", "欢迎来到房问", "Order accepted"])
    assert s.found == 3 and s.error_chars == pytest.approx(1.0)
    assert 95 < aggregate([s])["ALL"].accuracy < 100


def test_a_missing_line_costs_recall_and_accuracy():
    s = scored(["你好世界", "Order accepted"])
    assert s.found == 2 and s.lines == 3
    assert s.missed == ("欢迎来到房间",)
    group = aggregate([s])["ALL"]
    assert group.recall == pytest.approx(200 / 3) and group.accuracy < 100


def test_empty_ocr_scores_zero_recall():
    s = scored([])
    assert s.found == 0 and aggregate([s])["ALL"].recall == 0.0


def test_order_and_line_splitting_do_not_matter():
    split = scored(["Order", "accepted", "欢迎来到房间", "你好世界"])
    assert split.found == 3


def test_merged_lines_still_match_the_expected_lines_inside_them():
    assert scored(["你好世界欢迎来到房间 Order accepted"]).found == 3


def test_case_width_and_punctuation_are_ignored():
    assert norm("Ｏrder, ACCEPTED!") == norm("order accepted")
    assert scored(["你好，世界！", "欢迎来到房间。", "ORDER ACCEPTED"]).found == 3


def test_expected_lines_under_two_characters_are_dropped():
    assert expected_lines("a\n你好\n\n  \n!!\n") == ["你好"]


def test_unreadable_marker_in_expected_text_is_ignored():
    assert norm("你[?]好") == norm("你好")


def test_aggregate_groups_by_category_and_adds_all():
    groups = aggregate(
        [scored([], "chat"), scored(["你好世界", "欢迎来到房间", "Order accepted"], "menu")]
    )
    assert set(groups) == {"ALL", "chat", "menu"}
    assert groups["chat"].recall == 0.0 and groups["menu"].recall == 100.0
    assert groups["ALL"].recall == 50.0 and groups["ALL"].images == 2


def test_failures_report_each_missed_threshold_and_missing_categories():
    perfect = aggregate([scored(["你好世界", "欢迎来到房间", "Order accepted"], "chat")])
    problems = failures(perfect, "fast")
    assert not any(p.startswith(("chat", "ALL")) for p in problems)
    assert any(p.startswith("menu: no images") for p in problems), "an absent category fails"
    bad = aggregate([scored([], "chat")])
    messages = "\n".join(failures(bad, "fast"))
    assert "chat: character accuracy 0.00% < 97.0% (gate)" in messages
    assert "chat: line recall 0.00% < 95.0% (gate)" in messages


def test_the_thresholds_sit_exactly_on_the_boundary_inclusive():
    from testkit.golden import GroupScore

    at = {n: GroupScore(n, 1, a, r) for n, (a, r, _) in THRESHOLDS["fast"].items()}
    assert failures(at, "fast") == []
    low_accuracy = {n: GroupScore(n, 1, a - 0.01, r) for n, (a, r, _) in THRESHOLDS["fast"].items()}
    assert len(failures(low_accuracy, "fast")) == len(THRESHOLDS["fast"])
    low_both = {
        n: GroupScore(n, 1, a - 0.01, r - 0.01) for n, (a, r, _) in THRESHOLDS["fast"].items()
    }
    assert len(failures(low_both, "fast")) == 2 * len(THRESHOLDS["fast"])
