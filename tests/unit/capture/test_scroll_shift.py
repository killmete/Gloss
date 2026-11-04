"""Scroll shift maths on its own."""

from __future__ import annotations

import numpy as np

from gloss.core.contracts import Rect
from gloss.services.scroll_shift import (
    dirty_mask_after_shift,
    find_vertical_shift,
    mask_to_rects,
    quantize,
    row_hashes,
)


def striped(height=200, width=64, seed=1):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, size=(height, width), dtype=np.uint8)


def test_row_hashes_are_equal_for_equal_rows_only():
    image = striped()
    image[10] = image[50]
    hashes = row_hashes(image)
    assert hashes[10] == hashes[50] and hashes[11] != hashes[10]


def test_shift_up_and_down_have_opposite_signs():
    page = striped(300)
    top, lower = page[0:200], page[25:225]
    assert find_vertical_shift(top, lower) == 25  # current[i] == previous[i + 25]
    assert find_vertical_shift(lower, top) == -25


def test_unrelated_images_and_blank_images_give_no_shift():
    assert find_vertical_shift(striped(seed=1), striped(seed=2)) is None
    flat = np.full((200, 64), 7, dtype=np.uint8)
    assert find_vertical_shift(flat, flat.copy()) is None
    assert find_vertical_shift(striped(100), striped(120)) is None  # shapes differ


def test_a_mostly_different_image_is_not_a_scroll():
    page = striped(300)
    a, b = page[0:200].copy(), page[25:225].copy()
    b[:150] = striped(150, seed=9)  # three quarters is new content
    assert find_vertical_shift(a, b) is None


def test_dirty_mask_marks_only_the_new_rows():
    page = striped(300)
    a, b = page[0:200], page[25:225]
    mask = dirty_mask_after_shift(a, b, 25)
    assert not mask[:175].any() and mask[175:].any()
    assert mask_to_rects(mask) == [Rect(0, 175, 64, 25)]


def test_mask_to_rects_groups_rows_and_ignores_small_gaps():
    mask = np.zeros((100, 50), dtype=bool)
    mask[10:20, 5:15] = True
    mask[22:30, 8:40] = True
    mask[80:90, 0:3] = True
    assert mask_to_rects(mask, row_gap=3) == [Rect(5, 10, 35, 20), Rect(0, 80, 3, 10)]
    assert len(mask_to_rects(mask, row_gap=1)) == 3
    assert mask_to_rects(np.zeros((5, 5), dtype=bool)) == []


def test_quantize_drops_low_bits():
    assert quantize(np.array([7, 8, 15], dtype=np.uint8), 3).tolist() == [0, 1, 1]
    assert quantize(np.array([7], dtype=np.uint8), 0).tolist() == [7]
