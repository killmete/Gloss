"""OCR regions are grown so a small change never makes OCR read half a line."""

from __future__ import annotations

from gloss.core.contracts import Rect
from gloss.core.pipeline import _whole_lines

W, H = 1900, 700
LINE = Rect(100, 370, 300, 25)  # a short message with an animated sticker at its right end


def test_a_change_beside_a_line_grows_to_the_whole_line():
    sticker = Rect(330, 380, 100, 20)  # the end of the line and the sticker beside it changed
    assert _whole_lines([sticker], [LINE], W, H) == [Rect(100, 370, 330, 30)]


def test_a_change_touching_no_known_line_stays_as_it_is():
    assert _whole_lines([Rect(500, 100, 30, 30)], [LINE], W, H) == [Rect(500, 100, 30, 30)]


def test_bands_stay_no_wider_than_the_region_and_the_lines_it_touches():
    far = Rect(1500, 370, 300, 25)  # same row, but the change does not reach it
    out = _whole_lines([Rect(330, 380, 40, 10)], [LINE, far], W, H)
    assert out == [Rect(100, 370, 300, 25)]
    assert all(r.w < 960 for r in out)  # read at native size by the OCR detector


def test_a_region_grown_by_one_line_also_takes_in_the_next_line_it_now_touches():
    upper = Rect(100, 340, 200, 34)  # overlaps LINE once grown
    out = _whole_lines([Rect(110, 345, 10, 10)], [upper, LINE], W, H)
    assert out == [Rect(100, 340, 300, 55)]


def test_regions_that_overlap_once_grown_merge_and_separate_ones_stay_apart():
    out = _whole_lines(
        [Rect(120, 375, 5, 5), Rect(380, 380, 5, 5), Rect(0, 600, 5, 5)], [LINE], W, H
    )
    assert sorted(out, key=lambda r: r.y) == [Rect(100, 370, 300, 25), Rect(0, 600, 5, 5)]


def test_known_lines_are_compared_where_they_will_be_after_the_scroll():
    # the content scrolled up by 60: LINE is now at y 310, so a change at 315 touches it
    out = _whole_lines([Rect(150, 315, 10, 5)], [LINE], W, H, scroll_dy=60)
    assert out == [Rect(100, 310, 300, 25)]


def test_regions_are_clamped_to_the_frame():
    out = _whole_lines([Rect(10, 690, 5, 20)], [Rect(0, 680, 50, 30)], W, H)
    assert out == [Rect(0, 680, 50, 20)]
