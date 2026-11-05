"""Shown boxes on the same row only grow: partial reads never shrink or nudge them."""

from __future__ import annotations

from gloss.core.contracts import Rect
from gloss.core.pipeline import _steady

OLD = Rect(600, 250, 180, 24)  # limit = max(4, 24 // 4) = 6
THRESHOLD = 4


def test_a_read_missing_the_first_characters_on_the_same_row_keeps_the_box():
    partial = Rect(640, 252, 140, 22)  # an animated avatar hid the start of the line
    assert _steady(OLD, partial, THRESHOLD) is None


def test_a_few_pixels_of_jitter_on_the_same_row_keeps_the_box():
    assert _steady(OLD, Rect(603, 248, 176, 25), THRESHOLD) is None


def test_a_wider_read_on_the_same_row_grows_the_box_and_keeps_its_row():
    wider = Rect(570, 253, 230, 21)
    grown = _steady(OLD, wider, THRESHOLD)
    assert grown == Rect(570, OLD.y, 230, OLD.h)


def test_a_read_on_another_row_takes_the_new_position():
    moved = Rect(600, 290, 180, 24)  # no vertical overlap with the old box
    assert _steady(OLD, moved, THRESHOLD) == moved


def test_a_read_sharing_too_little_height_counts_as_another_row():
    low = Rect(600, 265, 180, 24)  # shares 9 of 24 px: below the 0.6 share
    assert _steady(OLD, low, THRESHOLD) == low


def test_a_read_that_does_not_overlap_sideways_takes_the_new_position():
    beside = Rect(820, 250, 100, 24)
    assert _steady(OLD, beside, THRESHOLD) == beside
