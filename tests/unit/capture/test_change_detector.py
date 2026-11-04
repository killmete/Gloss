"""Change detector: dirty tiles, padding, skipping, and scroll shift."""

from __future__ import annotations

import time

import numpy as np
import pytest

from gloss.core.contracts import Frame, Rect
from gloss.services.change_detector import ChangeDetector, merge_rects, small_gray

W, H = 640, 480


def make_frame(image, *, fresh=True, region=None, source="t"):
    region = region or Rect(0, 0, image.shape[1], image.shape[0])
    return Frame(image, source, region, time.monotonic(), 1.0, fresh)


def blank(w=W, h=H, value=240):
    return np.full((h, w, 3), value, dtype=np.uint8)


def chat_page(lines, w=W, line_h=30, seed=0):
    """Light page of text-like rows: each numbered line has its own random dark blocks."""
    page = blank(w, line_h * len(lines))
    for index, line_id in enumerate(lines):
        rng = np.random.default_rng(1000 + line_id + seed)
        top = index * line_h + 6
        for x in range(20, w - 40, 14):
            if rng.random() < 0.6:
                page[top : top + 18, x : x + 9] = rng.integers(0, 90, size=3, dtype=np.uint8)
    return page


def covers(rects, target):
    return any(
        r.x <= target.x
        and r.y <= target.y
        and r.right >= target.right
        and r.bottom >= target.bottom
        for r in rects
    )


def test_first_frame_is_reported_in_full():
    result = ChangeDetector().detect(make_frame(blank()))
    assert result.changed and result.full and result.dirty == (Rect(0, 0, W, H),)


def test_identical_frame_is_no_change():
    detector = ChangeDetector()
    detector.detect(make_frame(blank()))
    result = detector.detect(make_frame(blank()))
    assert not result.changed and result.dirty == () and not result.full


def test_not_fresh_frame_is_skipped_without_hashing(monkeypatch):
    detector = ChangeDetector()
    detector.detect(make_frame(blank()))

    def boom(*_a, **_k):
        raise AssertionError("pixels were looked at")

    monkeypatch.setattr(detector, "_tile_hashes", boom)
    different = blank(value=10)
    assert not detector.detect(make_frame(different, fresh=False)).changed


def test_small_change_gives_one_padded_rectangle_around_it():
    detector = ChangeDetector(tile_size=64, padding=8)
    base = blank()
    detector.detect(make_frame(base))
    changed = base.copy()
    changed[200:215, 300:340] = 20
    result = detector.detect(make_frame(changed))
    assert result.changed and not result.full
    assert len(result.dirty) == 1
    assert covers(result.dirty, Rect(300, 200, 40, 15))
    assert result.dirty[0].area < 0.1 * W * H
    # padding never leaves the frame
    assert all(r.x >= 0 and r.y >= 0 and r.right <= W and r.bottom <= H for r in result.dirty)


def test_two_far_apart_changes_stay_separate_and_close_ones_merge():
    detector = ChangeDetector(tile_size=64, padding=8)
    base = blank()
    detector.detect(make_frame(base))
    far = base.copy()
    far[10:20, 10:60] = 0
    far[420:430, 560:620] = 0
    assert len(detector.detect(make_frame(far)).dirty) == 2
    detector.reset()
    detector.detect(make_frame(base))
    near = base.copy()
    near[100:110, 100:150] = 0
    near[100:110, 170:200] = 0  # next tile over
    assert len(detector.detect(make_frame(near)).dirty) == 1


def test_faint_noise_is_ignored_but_real_text_is_not():
    detector = ChangeDetector(quantize_bits=3)
    base = blank(value=100)
    detector.detect(make_frame(base))
    noisy = base.copy()
    noisy[::2, ::2] += 1  # one grey level of noise
    assert not detector.detect(make_frame(noisy)).changed
    text = base.copy()
    text[50:60, 50:90] = 0
    assert detector.detect(make_frame(text)).changed


def test_most_of_the_frame_changing_reports_the_whole_frame():
    detector = ChangeDetector()
    detector.detect(make_frame(blank()))
    result = detector.detect(make_frame(blank(value=10)))
    assert result.dirty == (Rect(0, 0, W, H),) and not result.full


def test_changed_size_or_position_resets_history():
    detector = ChangeDetector()
    detector.detect(make_frame(blank()))
    moved = detector.detect(make_frame(blank(), region=Rect(50, 50, W, H)))
    assert moved.full
    resized = detector.detect(make_frame(blank(300, 200), region=Rect(50, 50, 300, 200)))
    assert resized.full
    detector.reset()
    assert detector.detect(make_frame(blank(300, 200), region=Rect(50, 50, 300, 200))).full


def test_odd_frame_sizes_work():
    detector = ChangeDetector(tile_size=64)
    base = blank(333, 217)
    detector.detect(make_frame(base))
    changed = base.copy()
    changed[210:217, 320:333] = 0  # the very corner
    result = detector.detect(make_frame(changed))
    assert result.changed and covers(result.dirty, Rect(320, 210, 13, 7))


@pytest.mark.parametrize("scale", [1, 2, 4])
def test_downscale_options_all_find_the_change(scale):
    detector = ChangeDetector(tile_size=64, downscale=scale)
    base = blank()
    detector.detect(make_frame(base))
    changed = base.copy()
    changed[100:112, 200:300] = 0
    assert covers(detector.detect(make_frame(changed)).dirty, Rect(200, 100, 100, 12))


def test_bad_settings_are_refused():
    for kwargs in ({"downscale": 3}, {"tile_size": 4}, {"padding": -1}, {"quantize_bits": 9}):
        with pytest.raises(ValueError):
            ChangeDetector(**kwargs)


def test_small_gray_is_a_mean_of_the_weighted_channels():
    image = np.zeros((4, 4, 3), dtype=np.uint8)
    image[..., 1] = 200
    assert small_gray(image, 2).tolist() == [[100, 100], [100, 100]]  # (0 + 2*200 + 0) / 4
    white = np.full((8, 8, 3), 255, dtype=np.uint8)
    assert int(small_gray(white, 4).min()) == 255


def test_merge_rects_joins_touching_and_keeps_apart_the_rest():
    merged = merge_rects([Rect(0, 0, 10, 10), Rect(10, 0, 10, 10), Rect(100, 100, 5, 5)])
    assert sorted((r.x, r.w) for r in merged) == [(0, 20), (100, 5)]


# ---- scroll shift ----------------------------------------------------------------------


def test_chat_scroll_reports_only_the_new_band():
    page = chat_page(list(range(40)))
    step = 30 * 2  # two lines scroll in
    first = page[0 : 30 * 14]
    second = page[step : step + 30 * 14]
    detector = ChangeDetector(tile_size=64, padding=8)
    detector.detect(make_frame(first.copy()))
    result = detector.detect(make_frame(second.copy()))
    h = first.shape[0]
    assert result.changed and not result.full
    assert [s.dy for s in result.scrolls] == [step]
    assert covers(result.dirty, Rect(20, h - step + 6, W - 60, 18))
    # only the band (plus padding) is dirty, not the 85 percent that merely moved
    assert all(r.y >= h - step - 8 - 1 for r in result.dirty)
    assert sum(r.area for r in result.dirty) <= (step + 16) * W


def test_scroll_down_is_detected_with_the_opposite_sign():
    page = chat_page(list(range(40)))
    a = page[60 : 60 + 30 * 14]
    b = page[0 : 30 * 14]  # scrolled back down by two lines: new band at the top
    detector = ChangeDetector(tile_size=64, padding=8)
    detector.detect(make_frame(a.copy()))
    result = detector.detect(make_frame(b.copy()))
    assert [s.dy for s in result.scrolls] == [-60]
    assert all(r.bottom <= 60 + 8 + 1 for r in result.dirty)


def test_scroll_with_an_odd_pixel_offset_and_a_changing_clock():
    page = chat_page(list(range(60)))
    h = 30 * 14
    a = page[0:h].copy()
    b = page[37 : 37 + h].copy()
    a[0:20, 500:620] = 0  # a clock in the corner that also changes
    b[0:20, 500:620] = 90
    detector = ChangeDetector(tile_size=64, padding=8)
    detector.detect(make_frame(a))
    result = detector.detect(make_frame(b))
    assert [s.dy for s in result.scrolls] == [37]
    assert covers(result.dirty, Rect(500, 0, 120, 20))  # the clock is still reported
    assert covers(result.dirty, Rect(20, h - 37 + 6, W - 60, 18))


def test_static_header_does_not_hide_the_scroll_or_turn_dirty():
    page = chat_page(list(range(60)))
    h = 30 * 14
    header = chat_page([900, 901], seed=5)  # 60 px of fixed content
    a, b = page[0:h].copy(), page[60 : 60 + h].copy()
    a[0:60], b[0:60] = header, header
    detector = ChangeDetector(tile_size=64, padding=8)
    detector.detect(make_frame(a))
    result = detector.detect(make_frame(b))
    assert [s.dy for s in result.scrolls] == [60]
    assert all(r.y >= 60 - 8 - 1 for r in result.dirty)


def test_unrelated_new_page_is_not_mistaken_for_a_scroll():
    detector = ChangeDetector(tile_size=64)
    detector.detect(make_frame(chat_page(list(range(14)))))
    result = detector.detect(make_frame(chat_page(list(range(100, 114)))))
    assert result.changed and result.scrolls == ()


def test_scroll_detection_can_be_turned_off():
    page = chat_page(list(range(40)))
    h = 30 * 14
    detector = ChangeDetector(tile_size=64, detect_scroll=False)
    detector.detect(make_frame(page[0:h].copy()))
    result = detector.detect(make_frame(page[60 : 60 + h].copy()))
    assert result.scrolls == () and result.changed
