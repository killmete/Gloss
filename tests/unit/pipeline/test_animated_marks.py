"""Very short reads on animated spots (a pulsing "new" badge) are ignored; real text is kept."""

from __future__ import annotations

from types import SimpleNamespace

from testkit.pipeline import block, fast_config

from gloss.core.contracts import Rect
from gloss.core.pipeline import Pipeline

ANIMATED = Rect(600, 0, 100, 1000)  # a column of animated cells


class FakeDetector:
    def animated_share(self, rect: Rect) -> float:
        left, right = max(rect.x, ANIMATED.x), min(rect.right, ANIMATED.right)
        return max(0, right - left) / rect.w if rect.w else 0.0


DETECTOR = FakeDetector()


def _filter(blocks, detector=DETECTOR, **ocr):
    cfg = fast_config()
    if ocr:
        cfg = cfg.model_copy(update={"ocr": cfg.ocr.model_copy(update=ocr)})
    owner = SimpleNamespace(_config=cfg, _detector=detector)
    return [b.text for b in Pipeline._without_animated_marks(owner, blocks)]


def test_a_one_character_badge_on_an_animated_spot_is_dropped():
    assert _filter([block("新", x=610, w=20)]) == []


def test_a_one_character_message_on_a_still_bubble_is_kept():
    assert _filter([block("又", x=100, w=20)]) == ["又"]


def test_longer_text_on_an_animated_spot_is_kept():
    assert _filter([block("小林-钻石-阿月", x=610, w=80)]) == ["小林-钻石-阿月"]


def test_a_short_read_only_partly_on_an_animated_spot_is_kept():
    assert _filter([block("好", x=580, w=30)]) == ["好"]  # 10 of 30 px animated


def test_the_rule_can_be_turned_off_and_does_nothing_before_capture_starts():
    badge = [block("新", x=610, w=20)]
    assert _filter(badge, animated_text_max_chars=0) == ["新"]
    assert _filter(badge, detector=None) == ["新"]
