"""Box motion: jitter is ignored, scrolls move boxes at once, capture slows down when idle."""

from __future__ import annotations

import time

import numpy as np
import pytest
from testkit.frames import make_frame
from testkit.pipeline import Rig, block, fast_config, solid, wait_until, with_patch

from gloss.core.contracts import Rect
from gloss.core.pipeline import _Mailbox
from gloss.services.change_detector import ChangeResult, ScrollShift

LINE = "你好吗朋友"
ORIGIN = (100, 50)  # the Rig's default target region origin


@pytest.fixture
def rig():
    made: list[Rig] = []

    def make(**kw) -> Rig:
        r = Rig(fast_config(**kw))
        made.append(r)
        return r

    yield make
    for r in made:
        r.close()


# ---- hysteresis -----------------------------------------------------------------------------


def _read_again(r: Rig, cap, ocr, n: int = [0]) -> None:
    """Change the screen a little and wait for the resulting OCR read to be processed."""
    calls = len(ocr.calls)
    n[0] += 1
    cap.image = with_patch(cap.image, x=10 + n[0] % 100)
    wait_until(lambda: len(ocr.calls) > calls, what="an OCR read")
    time.sleep(0.05)


def _line_items(r: Rig, text: str):
    return [i for i in r.sink.snapshot() if i.result.source_text == text]


def test_two_pixel_ocr_jitter_on_a_static_line_does_not_move_the_box(rig):
    # _read_again pokes one spot repeatedly, which the animated-spot filter would silence.
    r = rig(animated_suppression=False)
    cap = r.capture("cap", solid())
    ocr = r.ocr("ocr", [block(LINE, y=40)])
    r.start()
    wait_until(lambda: _line_items(r, LINE), what="the line")
    shown = len(r.sink.snapshot())
    for k in range(12):
        dy = 2 if k % 2 else -2
        ocr.blocks[:] = [block(LINE, x=5 + dy, y=40 + dy)]
        _read_again(r, cap, ocr)
    assert len(r.sink.snapshot()) == shown


def test_a_real_move_beyond_the_threshold_updates_the_box(rig):
    r = rig()
    cap = r.capture("cap", solid())
    ocr = r.ocr("ocr", [block(LINE, y=40)])
    r.start()
    wait_until(lambda: _line_items(r, LINE), what="the line")
    ocr.blocks[:] = [block(LINE, y=70)]  # 30 px: well beyond max(threshold, h // 4)
    for _ in range(6):
        _read_again(r, cap, ocr)
        if len(_line_items(r, LINE)) > 1:
            break
    items = _line_items(r, LINE)
    assert len(items) >= 2
    assert items[-1].bbox.y == ORIGIN[1] + 70
    assert items[-1].result.text == f"EN:{LINE}"  # the translation is kept, only the box moves


# ---- scroll fast path -----------------------------------------------------------------------

W, H, DY = 400, 600, 60
RNG = np.random.default_rng(1)
TALL = RNG.integers(0, 255, (H + 4 * DY, W, 3), dtype=np.uint8)  # texture the detector can track
TOP = np.ascontiguousarray(TALL[0:H])
SCROLLED = np.ascontiguousarray(TALL[DY : H + DY])
PATCHED = TOP.copy()
PATCHED[300:330, 100:130] = 0  # a small non-scroll change


def _scroll_reader(r_blocks_top, r_blocks_scrolled):
    def read(frame, regions):
        if frame.image is SCROLLED:
            return r_blocks_scrolled
        return r_blocks_top

    return read


def _scroll_rig(rig, **kw):
    r = rig(timeout_ms=5000, **kw)
    cap = r.capture("cap", TOP)
    ocr = r.ocr("ocr")
    a, b, c = (
        block(t, x=20, y=y, w=150, h=20)
        for t, y in (("上面的消息", 20), ("中间的消息", 300), ("下面的消息", 500))
    )
    # after a scroll up by DY every line is DY higher; "上面的消息" has left the area
    shifted = [
        block("中间的消息", x=20, y=300 - DY, w=150, h=20),
        block("下面的消息", x=20, y=500 - DY, w=150, h=20),
    ]
    ocr.reader = _scroll_reader([a, b, c], shifted)
    return r, cap, ocr


def test_a_detected_scroll_moves_shown_boxes_before_ocr_returns_and_drops_those_leaving(rig):
    r, cap, ocr = _scroll_rig(rig)
    r.start()
    wait_until(lambda: len(r.sink.shown()) >= 3, what="three translated lines")
    block_read = r.event()
    ocr.waits.append(block_read)  # the next read hangs: only the fast path can move boxes
    reads = len(ocr.calls)
    cap.image = SCROLLED
    wait_until(lambda: len(ocr.calls) > reads, what="the (slow) read of the scrolled frame")

    def latest(text):
        return [i for i in _line_items(r, text)][-1].bbox

    wait_until(lambda: latest("中间的消息").y == ORIGIN[1] + 300 - DY, what="the box to move")
    assert latest("下面的消息").y == ORIGIN[1] + 500 - DY
    ids = {i.result.source_text: i.result.line_id for i in r.sink.shown()}
    assert ids["上面的消息"] in r.sink.removed  # left through the top
    assert len(ocr.calls) == reads + 1  # and OCR has still not answered
    block_read.set()


def test_a_read_of_a_frame_older_than_a_scroll_does_not_move_boxes_back(rig):
    r, cap, ocr = _scroll_rig(rig)
    r.start()
    wait_until(lambda: len(r.sink.shown()) >= 3, what="three translated lines")
    hold = r.event()
    ocr.waits.append(hold)
    reads = len(ocr.calls)
    cap.image = PATCHED  # a non-scroll change: its read starts and hangs, with old positions
    wait_until(lambda: len(ocr.calls) > reads, what="the read that hangs")
    cap.image = SCROLLED  # the scroll is shown at once; the hung read is now older than it
    moved_y = ORIGIN[1] + 300 - DY
    wait_until(
        lambda: _line_items(r, "中间的消息")[-1].bbox.y == moved_y, what="the scroll fast path"
    )
    hold.set()  # the old read returns, reporting the pre-scroll position
    wait_until(lambda: len(ocr.calls) >= reads + 2, what="the reads that follow")
    time.sleep(0.2)
    ys = [i.bbox.y for i in _line_items(r, "中间的消息")]
    assert moved_y in ys
    assert set(ys[ys.index(moved_y) :]) == {moved_y}  # never back to the pre-scroll position


# ---- mailbox: scroll + scroll ---------------------------------------------------------------

AREA = Rect(0, 0, 400, 600)


def scrolled(dy: int, band: Rect) -> ChangeResult:
    return ChangeResult(True, (band,), (ScrollShift(AREA, dy),), False)


def test_two_scrolls_of_one_area_merge_into_one_with_summed_shift_and_shifted_bands():
    box = _Mailbox()
    frame = make_frame(w=400, h=600)
    box.post(frame, scrolled(60, Rect(0, 532, 400, 68)))
    latest = make_frame(w=400, h=600)
    box.post(latest, scrolled(60, Rect(0, 532, 400, 68)))
    work = box.take(0.0)
    assert work.frame is latest and work.scroll_dy == 120 and work.scroll_area == AREA
    # the older band moved up by the newer shift; the newer band is where it was exposed
    assert work.regions == [Rect(0, 472, 400, 68), Rect(0, 532, 400, 68)]


def test_merged_scroll_bands_that_scroll_off_the_top_are_clipped_or_dropped():
    box = _Mailbox()
    box.post(make_frame(w=400, h=600), scrolled(60, Rect(0, 40, 400, 60)))  # partly leaves
    box.post(make_frame(w=400, h=600), scrolled(60, Rect(0, 10, 400, 30)))  # wholly leaves later
    work = box.take(0.0)
    assert work.scroll_dy == 120
    assert work.regions == [Rect(0, 0, 400, 40), Rect(0, 10, 400, 30)]
    box.post(make_frame(w=400, h=600), scrolled(60, Rect(0, 10, 400, 30)))
    box.post(make_frame(w=400, h=600), scrolled(60, Rect(0, 540, 400, 60)))
    assert box.take(0.0).regions == [Rect(0, 540, 400, 60)]  # the first band is gone


def test_scrolls_of_different_areas_are_not_merged():
    box = _Mailbox()
    box.post(make_frame(w=400, h=600), scrolled(60, Rect(0, 532, 400, 68)))
    other = ChangeResult(True, (Rect(0, 0, 50, 10),), (ScrollShift(Rect(0, 0, 200, 600), 30),))
    box.post(make_frame(w=400, h=600), other)
    assert box.take(0.0).regions is None


# ---- adaptive capture rate ------------------------------------------------------------------


def _grabs_in(cap, seconds: float) -> int:
    start = cap.grabs
    time.sleep(seconds)
    return cap.grabs - start


def test_capture_runs_fast_while_the_screen_changes_and_slows_down_when_it_is_still(rig):
    r = rig(fps=40, idle_fps=5, idle_after_s=0.3)
    cap = r.capture("cap", solid())
    r.ocr("ocr", [block(LINE)])
    r.start()
    wait_until(lambda: r.sink.shown(), what="first translation")
    cap.vary = True
    time.sleep(0.2)
    busy = _grabs_in(cap, 0.6)
    cap.vary = False
    time.sleep(0.6)  # idle_after_s plus one slow frame
    idle = _grabs_in(cap, 0.8)
    assert busy >= 12, busy  # about 40 per second while busy
    assert idle <= 8, idle  # about 5 per second when still
    assert busy >= 3 * idle


def test_capture_speeds_up_again_as_soon_as_the_screen_changes(rig):
    r = rig(fps=40, idle_fps=2, idle_after_s=0.2)
    cap = r.capture("cap", solid())
    r.ocr("ocr", [block(LINE)])
    r.start()
    wait_until(lambda: r.sink.shown(), what="first translation")
    time.sleep(0.8)
    assert _grabs_in(cap, 0.5) <= 3
    cap.vary = True
    # the next (slow, up to 0.5 s) frame notices the change; after that it is fast again
    wait_until(lambda: _grabs_in(cap, 0.3) >= 6, timeout=4, what="the busy rate to return")


# ---- OCR splitting a stable line into pieces -----------------------------------------------

WHOLE = "你好吗朋友今天天气很好"
HALF_A, HALF_B = "你好吗朋友", "今天天气很好"


def test_ocr_alternately_splitting_a_stable_line_creates_no_new_lines_or_box_moves(rig):
    # _read_again pokes one spot repeatedly, which the animated-spot filter would silence.
    r = rig(animated_suppression=False)
    cap = r.capture("cap", solid(400, 120))
    ocr = r.ocr("ocr", [block(WHOLE, x=20, y=40, w=240, h=24)])
    r.start()
    wait_until(lambda: _line_items(r, WHOLE), what="the whole line")
    shown, sent = len(r.sink.snapshot()), list(r.stage.texts)
    for k in range(10):
        if k % 2:
            ocr.blocks[:] = [block(WHOLE, x=20, y=40, w=240, h=24)]
        else:  # the same pixels read as two pieces, each inside the old box
            ocr.blocks[:] = [
                block(HALF_A, x=20, y=40, w=118, h=24),
                block(HALF_B, x=142, y=40, w=118, h=24),
            ]
        _read_again(r, cap, ocr)
    assert r.stage.texts == sent  # nothing new was sent for translation
    assert len(r.sink.snapshot()) == shown  # and no box moved or was added


def test_a_scroll_merged_with_a_full_read_keeps_its_shift_so_lines_can_be_followed():
    box = _Mailbox()
    box.post(make_frame(w=400, h=600), scrolled(100, Rect(0, 500, 400, 100)))
    full = ChangeResult(True, (Rect(0, 0, 400, 600),), (), True)
    box.post(make_frame(w=400, h=600), full)
    box.post(make_frame(w=400, h=600), scrolled(200, Rect(0, 400, 400, 200)))
    work = box.take(0.0)
    assert work.regions is None and work.scroll_dy == 300 and work.scroll_area == AREA


def test_scrolls_of_different_areas_merged_into_a_full_read_keep_no_shift():
    box = _Mailbox()
    box.post(make_frame(w=400, h=600), scrolled(60, Rect(0, 532, 400, 68)))
    other = ChangeResult(True, (Rect(0, 0, 50, 10),), (ScrollShift(Rect(0, 0, 200, 600), 30),))
    box.post(make_frame(w=400, h=600), other)
    work = box.take(0.0)
    assert work.regions is None and work.scroll_dy == 0


# ---- lines that come back ------------------------------------------------------------------

BACK = "大家晚上好呀朋友们"


def test_a_line_that_leaves_and_comes_back_is_shown_again_without_translating_it_again(rig):
    r = rig(animated_suppression=False)
    cap = r.capture("cap", solid(400, 200))
    ocr = r.ocr("ocr", [block(BACK, y=40), block(LINE, y=120)])
    r.start()
    wait_until(lambda: _line_items(r, BACK), what="the line")
    line_id = _line_items(r, BACK)[-1].result.line_id
    sent = list(r.stage.texts)
    ocr.blocks[:] = [block(LINE, y=120)]
    for _ in range(8):
        _read_again(r, cap, ocr)
        if line_id in r.sink.removed:
            break
    assert line_id in r.sink.removed
    shown = len(_line_items(r, BACK))
    ocr.blocks[:] = [block(BACK, y=40), block(LINE, y=120)]
    for _ in range(6):
        _read_again(r, cap, ocr)
        if len(_line_items(r, BACK)) > shown:
            break
    again = _line_items(r, BACK)
    assert len(again) > shown, "the line came back but was never shown"
    assert again[-1].result.line_id == line_id
    assert again[-1].result.text == f"EN:{BACK}"
    assert r.stage.texts == sent  # not translated a second time


def test_a_new_line_read_from_a_frame_older_than_a_scroll_is_shown_where_it_is_now(rig):
    r, cap, ocr = _scroll_rig(rig)
    top = [
        block(t, x=20, y=y, w=150, h=20)
        for t, y in (("上面的消息", 20), ("中间的消息", 300), ("下面的消息", 500))
    ]
    late = block("新来的一条消息", x=20, y=400, w=150, h=20)  # appears just before the scroll
    shifted = [
        block("中间的消息", x=20, y=300 - DY, w=150, h=20),
        block("新来的一条消息", x=20, y=400 - DY, w=150, h=20),
        block("下面的消息", x=20, y=500 - DY, w=150, h=20),
    ]
    ocr.reader = _scroll_reader(top, shifted)
    r.start()
    wait_until(lambda: len(r.sink.shown()) >= 3, what="three translated lines")
    ocr.reader = _scroll_reader([*top, late], shifted)
    reads = len(ocr.calls)
    cap.image = PATCHED  # the new line is seen once...
    wait_until(lambda: len(ocr.calls) > reads, what="the first read of the new line")
    time.sleep(0.05)
    hold = r.event()
    ocr.waits.append(hold)
    reads = len(ocr.calls)
    cap.image = with_patch(PATCHED, x=200)  # ...and again in a read that hangs, pre-scroll
    wait_until(lambda: len(ocr.calls) > reads, what="the read that hangs")
    cap.image = SCROLLED
    wait_until(
        lambda: _line_items(r, "中间的消息")[-1].bbox.y == ORIGIN[1] + 300 - DY,
        what="the scroll fast path",
    )
    before = len(r.sink.snapshot())
    hold.set()
    wait_until(lambda: len(ocr.calls) >= reads + 2, what="the reads that follow")
    time.sleep(0.3)
    after = [
        i.bbox.y for i in r.sink.snapshot()[before:] if i.result.source_text == "新来的一条消息"
    ]
    assert ORIGIN[1] + 400 not in after  # the late read never puts it back where it was
    assert _line_items(r, "新来的一条消息")[-1].bbox.y == ORIGIN[1] + 400 - DY
