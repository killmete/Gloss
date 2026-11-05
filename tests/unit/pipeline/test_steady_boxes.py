"""Shown boxes stay steady: same-row reads only grow them, late reads catch up with scrolls."""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace

from gloss.core.contracts import Rect
from gloss.core.pipeline import Pipeline, _steady

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


# ---- catching up a late read with the scrolls shown since -----------------------------------

AREA = Rect(500, 180, 1000, 700)


def _catch_up(bbox, since, scrolls):
    owner = SimpleNamespace(_scrolls=deque(scrolls))
    return Pipeline._catch_up(owner, bbox, since)


def test_a_box_from_a_frame_older_than_two_scrolls_moves_by_both():
    box = Rect(520, 600, 200, 20)
    scrolls = [(1.0, AREA, 50), (2.0, AREA, 100), (3.0, AREA, 100)]
    assert _catch_up(box, 1.5, scrolls) == Rect(520, 400, 200, 20)


def test_scrolls_older_than_the_frame_or_elsewhere_are_ignored():
    box = Rect(520, 600, 200, 20)
    other = Rect(0, 0, 400, 700)
    assert _catch_up(box, 2.0, [(1.0, AREA, 100), (3.0, other, 100)]) == box


def test_a_box_that_has_scrolled_out_of_the_area_is_gone():
    assert _catch_up(Rect(520, 200, 200, 20), 0.0, [(1.0, AREA, 100)]) is None
