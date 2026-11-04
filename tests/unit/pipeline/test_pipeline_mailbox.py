"""The capture-to-OCR mailbox: latest frame wins, but no change is ever lost."""

from __future__ import annotations

import threading
import time

from testkit.frames import make_frame

from gloss.core.contracts import Rect
from gloss.core.pipeline import _Mailbox
from gloss.services.change_detector import ChangeResult, ScrollShift

A = Rect(0, 0, 10, 10)
B = Rect(20, 20, 10, 10)
UNCHANGED = ChangeResult(False)


def changed(*dirty: Rect, full: bool = False, scrolls: tuple[ScrollShift, ...] = ()):
    return ChangeResult(True, dirty, scrolls, full)


def test_empty_mailbox_returns_none_after_the_timeout():
    box = _Mailbox()
    started = time.monotonic()
    assert box.take(0.05) is None
    assert time.monotonic() - started >= 0.04


def test_take_empties_the_mailbox():
    box = _Mailbox()
    box.post(make_frame(), changed(A))
    assert box.take(0.0) is not None
    assert box.take(0.0) is None


def test_changed_frame_carries_its_dirty_regions():
    box = _Mailbox()
    frame = make_frame()
    box.post(frame, changed(A, B))
    work = box.take(0.0)
    assert work.frame is frame
    assert work.regions == [A, B]
    assert not work.unchanged


def test_full_change_means_read_everything():
    box = _Mailbox()
    box.post(make_frame(), changed(A, full=True))
    assert box.take(0.0).regions is None


def test_unchanged_frame_is_posted_as_unchanged_work():
    box = _Mailbox()
    box.post(make_frame(), UNCHANGED)
    work = box.take(0.0)
    assert work.unchanged and work.regions == []


def test_unchanged_frame_never_replaces_pending_changes():
    box = _Mailbox()
    first = make_frame()
    box.post(first, changed(A))
    box.post(make_frame(), UNCHANGED)
    work = box.take(0.0)
    assert work.frame is first and work.regions == [A] and not work.unchanged


def test_changed_frame_replaces_pending_unchanged_work():
    box = _Mailbox()
    box.post(make_frame(), UNCHANGED)
    newer = make_frame()
    box.post(newer, changed(B))
    work = box.take(0.0)
    assert work.frame is newer and work.regions == [B] and not work.unchanged


def test_dropped_frame_changes_are_merged_into_the_latest_frame():
    box = _Mailbox()
    box.post(make_frame(), changed(A))
    latest = make_frame()
    box.post(latest, changed(B))
    work = box.take(0.0)
    assert work.frame is latest
    assert work.regions == [A, B]


def test_three_frames_merge_in_order():
    box = _Mailbox()
    c = Rect(40, 40, 5, 5)
    for rect in (A, B, c):
        box.post(make_frame(), changed(rect))
    assert box.take(0.0).regions == [A, B, c]


def test_a_full_read_swallows_a_pending_partial_one_and_vice_versa():
    box = _Mailbox()
    box.post(make_frame(), changed(A))
    box.post(make_frame(), changed(B, full=True))
    assert box.take(0.0).regions is None
    box.post(make_frame(), changed(A, full=True))
    box.post(make_frame(), changed(B))
    assert box.take(0.0).regions is None


def test_a_scroll_cannot_be_merged_so_the_whole_frame_is_read():
    box = _Mailbox()
    shift = ScrollShift(Rect(0, 0, 10, 40), 12)
    box.post(make_frame(), changed(A, scrolls=(shift,)))
    box.post(make_frame(), changed(B))
    assert box.take(0.0).regions is None


def test_a_single_scroll_alone_keeps_its_shift_and_regions():
    box = _Mailbox()
    shift = ScrollShift(Rect(0, 0, 10, 40), 12)
    box.post(make_frame(), changed(A, scrolls=(shift,)))
    work = box.take(0.0)
    assert work.scroll_dy == 12 and work.regions == [A]


def test_two_scrolls_in_one_frame_read_everything():
    box = _Mailbox()
    shifts = (ScrollShift(Rect(0, 0, 5, 40), 3), ScrollShift(Rect(5, 0, 5, 40), 4))
    box.post(make_frame(), changed(A, scrolls=shifts))
    assert box.take(0.0).regions is None


def test_frames_from_different_regions_are_not_merged():
    box = _Mailbox()
    box.post(make_frame(region=Rect(0, 0, 64, 48)), changed(A))
    box.post(make_frame(region=Rect(500, 0, 64, 48)), changed(B))
    assert box.take(0.0).regions is None


def test_clear_discards_pending_work():
    box = _Mailbox()
    box.post(make_frame(), changed(A))
    box.clear()
    assert box.take(0.0) is None


def test_take_wakes_when_work_arrives():
    box = _Mailbox()
    got = []
    thread = threading.Thread(target=lambda: got.append(box.take(5.0)))
    thread.start()
    time.sleep(0.05)
    started = time.monotonic()
    box.post(make_frame(), changed(A))
    thread.join(2.0)
    assert got and got[0] is not None
    assert time.monotonic() - started < 1.0
