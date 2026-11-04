"""Language detection, region planning, grouping and preprocessing helpers."""

from __future__ import annotations

import numpy as np
import pytest

from gloss.core.contracts import PermanentError, Rect, TextBlock
from gloss.services.ocr_lang import classify_line, detect_lang
from gloss.services.ocr_layout import group_rects
from gloss.services.ocr_preprocess import PreprocessConfig, prepare
from gloss.services.ocr_regions import (
    centre_inside,
    clamp_rect,
    dedupe_blocks,
    iou,
    merge_regions,
    plan_regions,
    reading_order,
)
from gloss.services.ocr_settings import read_languages, read_number


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        ("你好,今天天气真好", "zh"),
        ("Hello there", "en"),
        ("こんにちは世界", "ja"),
        ("カタカナ", "ja"),
        ("今日は晴れです", "ja"),
        ("Gloss 好用", "zh"),
        ("12:30", None),
        ("안녕하세요", None),
        ("", None),
    ],
)
def test_detect_lang(text, lang):
    assert detect_lang(text) == lang


@pytest.mark.parametrize(
    ("text", "needs"),
    [
        ("你好", True),
        ("12:30", False),
        ("99+", False),
        ("¥128.50", False),
        ("...", False),
        ("😀😀", False),
        ("Lucky_77", False),
        ("@alice9", False),
        ("Thanks for the help", True),
        ("OK", True),
        ("안녕하세요", True),
    ],
)
def test_classify_needs_translation(text, needs):
    assert classify_line(text)[1] is needs


def test_target_language_lines_are_skipped():
    assert classify_line("Thanks a lot", ("en",)) == ("en", False)
    assert classify_line("谢谢", ("en",)) == ("zh", True)


# ---- regions ------------------------------------------------------------------------------


def test_clamp_rect():
    assert clamp_rect(Rect(-5, -5, 20, 20), 100, 100) == Rect(0, 0, 15, 15)
    assert clamp_rect(Rect(90, 90, 50, 50), 100, 100) == Rect(90, 90, 10, 10)
    assert clamp_rect(Rect(100, 0, 10, 10), 100, 100) is None
    assert clamp_rect(Rect(10, 10, 0, 5), 100, 100) is None


def test_merge_regions_joins_overlaps_and_keeps_touching_apart():
    assert merge_regions([Rect(0, 0, 10, 10), Rect(5, 5, 10, 10)]) == [Rect(0, 0, 15, 15)]
    assert len(merge_regions([Rect(0, 0, 10, 10), Rect(10, 0, 10, 10)])) == 2
    chain = merge_regions([Rect(0, 0, 10, 10), Rect(20, 0, 10, 10), Rect(8, 0, 14, 10)])
    assert chain == [Rect(0, 0, 30, 10)]


def test_plan_regions_whole_frame_and_margin():
    assert plan_regions(None, 100, 50) == [(Rect(0, 0, 100, 50), Rect(0, 0, 100, 50))]
    assert plan_regions([], 100, 50) == []
    assert plan_regions(None, 0, 0) == []
    ((crop, keep),) = plan_regions([Rect(20, 10, 30, 20)], 100, 50, margin=8)
    assert keep == Rect(20, 10, 30, 20) and crop == Rect(12, 2, 46, 36)
    ((crop, keep),) = plan_regions([Rect(0, 0, 30, 20)], 100, 50, margin=8)
    assert crop == Rect(0, 0, 38, 28)  # the margin never leaves the frame


def test_centre_inside():
    assert centre_inside(Rect(0, 0, 10, 10), Rect(0, 0, 10, 10))
    assert not centre_inside(Rect(8, 0, 10, 10), Rect(0, 0, 10, 10))


def test_reading_order_groups_rows():
    def b(text, x, y):
        return TextBlock(text, Rect(x, y, 50, 20), 0.9, "en")

    ordered = reading_order([b("d", 0, 100), b("c", 100, 52), b("a", 0, 50), b("b", 60, 55)])
    assert [x.text for x in ordered] == ["a", "b", "c", "d"]


def test_dedupe_blocks_drops_the_overlap_twin_but_not_a_repeated_message():
    low = TextBlock("hello world", Rect(0, 0, 100, 20), 0.6, "en")
    high = TextBlock("hello world", Rect(2, 1, 100, 20), 0.9, "en")
    other = TextBlock("hello world", Rect(0, 100, 100, 20), 0.9, "en")
    kept = dedupe_blocks([low, high, other])
    assert len(kept) == 2 and kept[0].confidence == 0.9


def test_iou():
    assert iou(Rect(0, 0, 10, 10), Rect(0, 0, 10, 10)) == 1.0
    assert iou(Rect(0, 0, 10, 10), Rect(10, 0, 10, 10)) == 0.0


# ---- layout -------------------------------------------------------------------------------


def test_group_rects_message_with_header_and_wrapped_lines():
    rects = [
        Rect(10, 100, 60, 16),  # name
        Rect(10, 122, 200, 20),
        Rect(10, 146, 180, 20),
        Rect(10, 300, 200, 20),
    ]
    assert group_rects(rects) == [0, 0, 0, 1]


def test_group_rects_name_and_time_on_one_row():
    assert group_rects([Rect(10, 100, 60, 16), Rect(90, 100, 50, 16)]) == [0, 0]
    assert group_rects([Rect(10, 100, 60, 16), Rect(500, 100, 50, 16)]) == [0, 1]


def test_group_rects_misaligned_or_loose_lines_stay_apart():
    assert group_rects([Rect(10, 100, 100, 20), Rect(150, 124, 100, 20)]) == [0, 1]
    assert group_rects([Rect(10, 100, 100, 20), Rect(10, 150, 100, 20)]) == [0, 1]
    assert group_rects([Rect(10, 100, 100, 20), Rect(10, 124, 100, 50)]) == [0, 1]


def test_group_rects_caps_the_size():
    tall = [Rect(10, 100 + i * 24, 100, 20) for i in range(20)]
    numbers = group_rects(tall, max_lines=5)
    assert max(numbers.count(n) for n in set(numbers)) <= 5


# ---- preprocessing and settings -----------------------------------------------------------


def test_prepare_converts_to_bgr_without_touching_the_input():
    rgb = np.zeros((10, 20, 3), np.uint8)
    rgb[..., 0] = 200
    rgb.flags.writeable = False
    bgr, scale = prepare(rgb, PreprocessConfig())
    assert scale == 1.0 and bgr.shape == (10, 20, 3) and bgr[0, 0, 2] == 200
    assert bgr.flags.c_contiguous and bgr.flags.writeable


def test_prepare_upscales_only_small_crops():
    config = PreprocessConfig(upscale=2.0, upscale_below_px=50)
    small, scale = prepare(np.zeros((30, 40, 3), np.uint8), config)
    assert scale == 2.0 and small.shape[:2] == (60, 80)
    big, scale = prepare(np.zeros((80, 40, 3), np.uint8), config)
    assert scale == 1.0 and big.shape[:2] == (80, 40)


def test_prepare_inverts_only_dark_crops_when_asked():
    dark = np.full((8, 8, 3), 10, np.uint8)
    light = np.full((8, 8, 3), 240, np.uint8)
    assert prepare(dark, PreprocessConfig(invert_dark=True))[0][0, 0, 0] == 245
    assert prepare(light, PreprocessConfig(invert_dark=True))[0][0, 0, 0] == 240
    assert prepare(dark, PreprocessConfig())[0][0, 0, 0] == 10


def test_preprocess_config_validates():
    with pytest.raises(ValueError):
        PreprocessConfig(upscale=9.0)


def test_settings_readers_raise_permanent_error():
    assert read_languages({"languages": "ZH"}) == ("zh",)
    assert read_languages({"languages": ["zh", "EN", "zh"]}) == ("zh", "en")
    for bad in (None, [], [1], ""):
        with pytest.raises(PermanentError):
            read_languages({"languages": bad})
    with pytest.raises(PermanentError):
        read_number({"x": "soon"}, "x", 1, minimum=0, maximum=5)
    with pytest.raises(PermanentError):
        read_number({"x": True}, "x", 1, minimum=0, maximum=5)
    with pytest.raises(PermanentError):
        read_number({"x": 1.5}, "x", 1, minimum=0, maximum=5, integer=True)


def test_group_rects_a_time_stamped_line_opens_a_new_message():
    rects = [Rect(10, 100, 200, 16), Rect(10, 130, 200, 20), Rect(10, 160, 200, 16)]
    assert group_rects(rects) == [0, 0, 0]  # without hints the stacked lines chain together
    assert group_rects(rects, starts=[True, False, True]) == [0, 0, 1]
