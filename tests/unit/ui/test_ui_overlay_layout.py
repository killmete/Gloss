"""Overlay chip placement: pure maths, no Qt. Each chip is anchored to its own line."""

from __future__ import annotations

from dataclasses import replace

from gloss.adapters.render.overlay import (
    LineBox,
    ScreenGeometry,
    _OverlaySettings,
    layout_chips,
    wrap_text,
)
from gloss.core.contracts import Rect

SCREEN = ScreenGeometry(0, 0, 1920, 1080, 1.0)
CFG = _OverlaySettings.parse({})


def measure(text: str, font_px: int) -> float:
    """A predictable fake font: every character is half as wide as the font is tall."""
    return 0.5 * font_px * len(text)


def box(text: str, x: int, y: int, w: int, h: int, key: str = "a") -> LineBox:
    return LineBox(key, text, Rect(x, y, w, h), False)


def lay(lines, region=None, screen=SCREEN, cfg=CFG):
    return layout_chips(lines, screen, cfg, measure, region)


def test_defaults_match_the_architects_config():
    assert (CFG.min_font_px, CFG.max_font_px, CFG.max_lines, CFG.clip_to_capture) == (
        9,
        28,
        2,
        True,
    )


def test_a_chip_sits_exactly_on_its_line():
    (chip,) = lay([box("Hello", 100, 200, 300, 24)])
    assert (chip.x, chip.y, chip.h) == (100, 200, 24)
    assert chip.w >= 300  # it covers the original
    assert chip.lines == ("Hello",)


def test_font_follows_line_height_within_limits():
    assert lay([box("Hi", 0, 0, 100, 20)])[0].font_px == 14
    assert lay([box("Hi", 0, 0, 100, 80)])[0].font_px == CFG.max_font_px
    assert lay([box("Hi", 0, 0, 100, 8)])[0].font_px == CFG.min_font_px


def test_text_shrinks_then_widens_to_the_right_to_fit_one_line():
    text = "x" * 60  # at 28px this needs 840 wide; allowed 960
    (chip,) = lay([box(text, 100, 0, 200, 60)])
    assert chip.font_px == 28 and chip.w > 200 and len(chip.lines) == 1
    long = "x" * 120  # needs far more than 960 at 28px, so the font must shrink
    (small,) = lay([box(long, 100, 0, 200, 60)])
    assert small.font_px < 28
    assert small.x + small.w <= 100 + 960 + 0.001
    assert len(small.lines) == 1


def test_chip_never_passes_the_capture_regions_right_edge():
    region = Rect(0, 0, 500, 1080)
    (chip,) = lay([box("y" * 80, 100, 10, 200, 24)], region)
    assert chip.x + chip.w <= 500 + 0.001


def test_wraps_down_into_free_space_at_the_minimum_font():
    text = "word " * 40  # too long for one line even at 9px within 400 wide
    (chip,) = lay([box(text, 100, 100, 400, 20)], Rect(0, 0, 500, 1000))
    assert chip.font_px == CFG.min_font_px
    assert len(chip.lines) == CFG.max_lines
    assert chip.h > 20 and chip.y == 100  # grew downward only


def test_growth_stops_before_the_next_line():
    text = "word " * 40
    upper = box(text, 100, 100, 400, 20, "a")
    lower = box("next line", 100, 124, 400, 20, "b")
    chips = {c.key: c for c in lay([upper, lower], Rect(0, 0, 500, 1000))}
    assert chips["a"].y + chips["a"].h <= 124 + 0.001
    assert chips["a"].lines[-1].endswith("…")
    assert (chips["b"].y, chips["b"].h) == (124, 20)


def test_text_that_cannot_fit_is_cut_with_an_ellipsis():
    (chip,) = lay([box("z" * 600, 0, 0, 100, 20)], Rect(0, 0, 400, 400))
    assert len(chip.lines) <= CFG.max_lines
    assert chip.lines[-1].endswith("…")
    assert chip.w <= 400


def test_lines_outside_the_capture_region_are_not_drawn():
    region = Rect(0, 0, 800, 600)
    chips = lay([box("in", 10, 10, 100, 20, "in"), box("out", 10, 700, 100, 20, "out")], region)
    assert [c.key for c in chips] == ["in"]


def test_clipping_can_be_turned_off():
    cfg = _OverlaySettings.parse({"clip_to_capture": False})
    chips = lay([box("out", 10, 700, 100, 20)], Rect(0, 0, 800, 600), cfg=cfg)
    assert len(chips) == 1


def test_overlapping_lines_are_not_pushed_apart():
    chips = {
        c.key: c for c in lay([box("one", 10, 10, 100, 20, "a"), box("two", 10, 12, 100, 20, "b")])
    }
    assert chips["a"].y == 10 and chips["b"].y == 12


def test_a_small_change_to_one_line_moves_no_other_chip():
    lines = [box(f"line {i}", 50, 40 * i, 300, 24, str(i)) for i in range(6)]
    before = {c.key: c for c in lay(lines, Rect(0, 0, 1000, 1000))}
    moved = list(lines)
    moved[2] = replace(moved[2], bbox=Rect(53, 83, 301, 24))
    after = {c.key: c for c in lay(moved, Rect(0, 0, 1000, 1000))}
    for key in before:
        if key != "2":
            assert after[key] == before[key]
    assert after["2"] != before["2"]


def test_same_input_gives_identical_output():
    lines = [box("same", 5, 5, 100, 20)]
    assert lay(lines) == lay(lines)


def test_positions_are_converted_with_the_monitors_own_scale():
    screen = ScreenGeometry(0, 0, 2880, 1620, 1.5)
    (chip,) = lay([box("Hi", 300, 150, 300, 60)], screen=screen)
    assert (chip.x, chip.y, chip.h) == (200, 100, 40)


def test_wrap_prefers_spaces_and_respects_the_line_limit():
    lines = wrap_text("alpha beta gamma delta", 12, 14 * 0.5 * 11, measure, 2)
    assert lines == ("alpha beta", "gamma delta")
    cut = wrap_text("alpha beta gamma delta epsilon", 12, 14 * 0.5 * 11, measure, 2)
    assert len(cut) == 2 and cut[-1].endswith("…")
