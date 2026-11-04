"""Black-frame health check: readable Health, and exactly one trial per streak."""

from __future__ import annotations

import time

import numpy as np
import pytest

from gloss.core.contracts import Frame, Rect
from gloss.services.black_frame import BlackFrameCheck


def frame(value: int = 0, size=(60, 80)) -> Frame:
    image = np.full((size[0], size[1], 3), value, dtype=np.uint8)
    return Frame(image, "t", Rect(0, 0, size[1], size[0]), time.monotonic(), 1.0)


def test_normal_frames_never_trip_the_check():
    check = BlackFrameCheck(reads=3, max_mean=4.0)
    assert not any(check.observe(frame(120)) for _ in range(20))
    assert check.health().state == "ok" and check.consecutive_black == 0


def test_the_trial_starts_once_after_n_black_reads_in_a_row():
    check = BlackFrameCheck(reads=3, max_mean=4.0)
    results = [check.observe(frame(0)) for _ in range(8)]
    assert results == [False, False, True, False, False, False, False, False]
    health = check.health()
    assert health.state == "degraded" and "black" in health.detail.lower()


def test_threshold_is_the_mean_brightness():
    check = BlackFrameCheck(reads=1, max_mean=4.0)
    assert check.is_black(frame(4)) and not check.is_black(frame(5))
    mostly_black = frame(0)
    mostly_black.image[:30] = 200  # half bright: a real picture
    assert not check.is_black(mostly_black)


def test_one_good_frame_resets_the_streak():
    check = BlackFrameCheck(reads=3)
    check.observe(frame(0))
    check.observe(frame(0))
    check.observe(frame(100))
    assert check.consecutive_black == 0
    assert not check.observe(frame(0))
    assert [check.observe(frame(0)) for _ in range(2)] == [False, True]


def test_a_trial_that_sees_a_picture_means_switch():
    check = BlackFrameCheck(reads=1)
    assert check.observe(frame(0))
    assert check.record_trial(frame(150)) is True


def test_a_trial_that_is_black_too_means_stay_and_explain():
    check = BlackFrameCheck(reads=1)
    assert check.observe(frame(0))
    assert check.record_trial(frame(0)) is False
    assert check.health().state == "degraded"
    assert not check.observe(frame(0))  # no second trial in the same streak
    assert check.record_trial(None) is False and check.health().state == "degraded"


def test_trial_unavailable_is_reported_and_reset_clears_everything():
    check = BlackFrameCheck(reads=1)
    check.observe(frame(0))
    check.trial_unavailable()
    assert "no other" in check.health().detail
    check.reset()
    assert check.health().state == "ok" and check.observe(frame(0))


def test_bad_settings_are_refused():
    with pytest.raises(ValueError):
        BlackFrameCheck(reads=0)
    with pytest.raises(ValueError):
        BlackFrameCheck(max_mean=300)
