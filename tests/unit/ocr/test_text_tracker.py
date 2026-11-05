"""TextTracker: jitter, scroll, duplicates, partial occlusion, order and groups."""

from __future__ import annotations

import pytest

from gloss.core.contracts import Rect, TextBlock
from gloss.services.text_tracker import (
    TextTracker,
    TrackerConfig,
    normalize_text,
    text_key,
)

LINE_H = 20
STEP = 40  # distance between two chat lines


def blk(text, x=20, y=100, w=260, h=LINE_H, conf=0.9, **kw):
    return TextBlock(text, Rect(x, y, w, h), conf, kw.pop("lang", "zh"), **kw)


def tracker(**kw):
    return TextTracker(TrackerConfig(**kw))


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self, step=0.25):
        self.t += step
        return self.t


@pytest.fixture
def now():
    return Clock()


# ---- normalisation ----------------------------------------------------------------------


def test_normalize_text_uses_nfkc_and_collapses_space():
    assert normalize_text("ＡＢＣ，１２３！") == "ABC,123!"
    assert normalize_text("  hello \t\n world  ") == "hello world"
    assert normalize_text("你 好 世 界") == "你好世界"  # Windows OCR puts spaces between CJK
    assert normalize_text("hi ​there") == "hi there"  # zero-width characters go
    assert normalize_text("Hello 世界") == "Hello 世界"  # Latin and CJK keep their space


def test_text_key_ignores_case_space_and_punctuation():
    assert text_key(normalize_text("Hello, World!")) == text_key("hello world")
    assert text_key("！？。") == ""


# ---- the stable-reads rule ----------------------------------------------------------------


def test_a_line_is_emitted_only_after_stable_reads(now):
    t = tracker(stable_reads=2)
    first = t.update([blk("今天天气真好啊")], now=now())
    assert first.new == () and first.visible == ()
    second = t.update([blk("今天天气真好啊")], now=now())
    assert [b.text for b in second.new] == ["今天天气真好啊"]
    assert second.new[0].line_id and second.visible == second.new
    assert t.update([blk("今天天气真好啊")], now=now()).new == ()


def test_stable_reads_one_emits_at_once(now):
    t = tracker(stable_reads=1)
    assert len(t.update([blk("你好啊朋友")], now=now()).new) == 1


def test_unchanged_pixels_count_as_a_read_without_a_second_ocr(now):
    t = tracker(stable_reads=2)
    t.update([blk("今天天气真好啊")], now=now())
    result = t.unchanged(now=now())
    assert [b.text for b in result.new] == ["今天天气真好啊"]


def test_a_line_outside_the_scanned_region_earns_a_read(now):
    t = tracker(stable_reads=2)
    t.update([blk("上面的一行字啊", y=50)], now=now())
    other = blk("下面新来的一行", y=300)
    result = t.update([other], now=now(), scanned=[Rect(0, 250, 400, 100)])
    assert [b.text for b in result.new] == ["上面的一行字啊"]  # unchanged pixels, no re-OCR


def test_a_line_in_the_scanned_region_that_is_gone_is_not_credited(now):
    t = tracker(stable_reads=2)
    t.update([blk("一闪而过的字幕")], now=now())
    result = t.update([], now=now(), scanned=[Rect(0, 0, 400, 400)])
    assert result.new == ()
    t.update([], now=now(), scanned=None)  # second miss: the pending line is forgotten
    assert t.unchanged(now=now()).new == ()


def test_stable_reads_three_needs_three(now):
    t = tracker(stable_reads=3)
    assert t.update([blk("需要三次读取才行")], now=now()).new == ()
    assert t.update([blk("需要三次读取才行")], now=now()).new == ()
    assert len(t.update([blk("需要三次读取才行")], now=now()).new) == 1


def test_invalid_config_is_refused():
    with pytest.raises(ValueError):
        TrackerConfig(stable_reads=0)
    with pytest.raises(ValueError):
        TrackerConfig(text_match=1.5)


# ---- jitter -------------------------------------------------------------------------------


def test_ocr_jitter_in_text_and_box_is_one_line(now):
    t = tracker(stable_reads=2)
    reads = [
        blk("你好吗朋友今天过得怎么样", x=20, y=100),
        blk("你好马朋友今天过得怎么样", x=22, y=101),  # one character wrong, box moved 2 px
        blk("你好吗朋友今天过得怎么样", x=21, y=99),
        blk("你好吗朋友今天过得怎么样.", x=20, y=100),  # a stray full stop
    ]
    emitted = []
    for read in reads:
        emitted += t.update([read], now=now()).new
    assert len(emitted) == 1
    assert emitted[0].text == "你好吗朋友今天过得怎么样"  # the majority wins over the one bad frame
    assert len(t.visible) == 1


def test_a_line_that_flickers_forever_is_emitted_eventually(now):
    t = tracker(stable_reads=2)
    variants = [
        "苹果手机特价出售啦",
        "苹果手机特价出隹啦",
        "苹果手机特价出蕾啦",
        "苹果手机持价出售啦",
    ]
    emitted = []
    for i in range(9):
        emitted += t.update([blk(variants[i % 4])], now=now()).new
    assert len(emitted) == 1


def test_different_lines_in_the_same_place_are_not_merged(now):
    t = tracker(stable_reads=1)
    first = t.update([blk("欢迎来到直播间大家好")], now=now()).new
    second = t.update([blk("请大家点点关注谢谢啦")], now=now()).new
    assert first and second and first[0].line_id != second[0].line_id


# ---- scrolling ----------------------------------------------------------------------------

CHAT = [
    "欢迎大家来到我的直播间",
    "今天晚上有人想一起玩吗",
    "我可以带你们上王者荣耀",
    "谢谢老板送的小礼物哦",
    "有没有人会唱这首歌呀",
    "明天早上我要去上班了",
]


def frame_of(lines, offset=0):
    return [blk(text, y=100 + i * STEP + offset) for i, text in enumerate(lines)]


def test_scrolling_does_not_reemit_lines_text_match(now):
    """The tracker finds moved lines by their text, even with no scroll hint."""
    t = tracker(stable_reads=2)
    emitted = []
    for shift in range(3):  # each step scrolls up by one line and shows one new line
        for _ in range(2):  # two frames per scroll position
            emitted += t.update(frame_of(CHAT[shift : shift + 4], 0), now=now()).new
    assert [b.text for b in emitted] == CHAT[:6]


def test_scrolling_with_a_shift_hint_keeps_box_matching(now):
    t = tracker(stable_reads=2)
    feed = CHAT[:5]
    emitted = []
    for _ in range(2):
        emitted += t.update(frame_of(feed[:4], 0), now=now()).new
    t.scroll(-STEP)
    for _ in range(2):
        emitted += t.update(frame_of(feed[1:5], 0), now=now()).new
    assert [b.text for b in emitted] == feed
    assert len({b.line_id for b in emitted}) == 5


def test_scroll_with_a_changed_pixel_position_and_jitter_does_not_duplicate(now):
    t = tracker(stable_reads=2)
    emitted = []
    for _ in range(2):
        emitted += t.update(frame_of(CHAT[:3], 0), now=now()).new
    # now scrolled 55 px with one character misread in two lines and no hint at all
    moved = [
        blk("欢迎大家来到我的直播问", y=100 - 55),
        blk(CHAT[1], y=140 - 55),
        blk("我可以带你们上王者荣躍", y=180 - 55),
    ]
    for _ in range(2):
        emitted += t.update(moved, now=now()).new
    assert len(emitted) == 3


def test_scrolled_off_line_is_dropped_and_not_retranslated_when_back(now):
    t = tracker(stable_reads=2, dedupe_window_s=30)
    first = []
    for _ in range(2):
        first += t.update([blk("这一行会被滚出屏幕去")], now=now()).new
    gone = None
    for _ in range(3):
        gone = t.update([], now=now(), scanned=None)
    assert first[0].line_id in gone.dropped or first[0].line_id in " ".join(gone.dropped)
    assert t.visible == ()
    back = []
    for _ in range(2):
        back.append(t.update([blk("这一行会被滚出屏幕去", y=300)], now=now()))
    assert all(r.new == () for r in back)  # known text inside the window
    assert back[-1].visible[0].line_id == first[0].line_id  # same id: reuse the translation


# ---- duplicates ---------------------------------------------------------------------------


def test_dedupe_window_expires(now):
    t = tracker(stable_reads=1, dedupe_window_s=10)
    first = t.update([blk("这是一条重复的消息啊")], now=now()).new
    for _ in range(4):
        t.update([], now=now(), scanned=None)
    later = t.update([blk("这是一条重复的消息啊")], now=now(step=20)).new
    assert first and later and first[0].line_id != later[0].line_id


def test_two_identical_messages_on_screen_are_both_emitted(now):
    t = tracker(stable_reads=1)
    both = [blk("哈哈哈哈哈哈哈哈", y=100), blk("哈哈哈哈哈哈哈哈", y=200)]
    result = t.update(both, now=now())
    assert len(result.new) == 2 and result.new[0].line_id != result.new[1].line_id


def test_a_repeat_after_the_first_message_is_still_visible_is_emitted(now):
    t = tracker(stable_reads=2)
    for _ in range(2):
        t.update([blk("今天晚上有人一起玩吗")], now=now())
    emitted = []
    for _ in range(2):
        emitted += t.update(
            [blk("今天晚上有人一起玩吗", y=100), blk("今天晚上有人一起玩吗", y=140)], now=now()
        ).new
    assert len(emitted) == 1  # the second copy is genuinely new


def test_short_texts_are_never_deduped(now):
    t = tracker(stable_reads=1, dedupe_min_chars=4)
    first = t.update([blk("666")], now=now()).new
    t.update([], now=now(), scanned=None)
    t.update([], now=now(), scanned=None)
    t.update([], now=now(), scanned=None)
    second = t.update([blk("666", y=300)], now=now()).new
    assert first and second and first[0].line_id != second[0].line_id


def test_dedupe_tolerates_jitter(now):
    t = tracker(stable_reads=1)
    first = t.update([blk("请问有没有人能带我上分啊")], now=now()).new
    for _ in range(3):
        t.update([], now=now(), scanned=None)
    again = t.update([blk("请问有没有人能带我上分阿", y=300)], now=now()).new
    assert first and again == ()


# ---- partial occlusion --------------------------------------------------------------------


def test_clipped_read_of_an_emitted_line_changes_nothing(now):
    t = tracker(stable_reads=2)
    emitted = []
    for _ in range(2):
        emitted += t.update([blk("我今天晚上想要打一把排位赛")], now=now()).new
    for clipped in ("我今天晚上想要", "我今天晚上想要打一把", "晚上想要打一把排位赛"):
        for _ in range(3):  # a popup covers part of the line for a while
            result = t.update([blk(clipped, w=120)], now=now())
            assert result.new == ()
            assert [b.line_id for b in result.visible] == [emitted[0].line_id]
            assert result.visible[0].text == "我今天晚上想要打一把排位赛"


def test_line_fully_hidden_leaves_then_returns_without_a_new_translation(now):
    t = tracker(stable_reads=2)
    emitted = []
    for _ in range(2):
        emitted += t.update([blk("我今天晚上想要打一把排位赛")], now=now()).new
    hidden = [t.update([], now=now(), scanned=None) for _ in range(3)]
    assert emitted[0].line_id in hidden[-1].dropped
    back = [t.update([blk("我今天晚上想要打一把排位赛")], now=now()) for _ in range(2)]
    assert all(r.new == () for r in back)
    assert back[-1].visible[0].line_id == emitted[0].line_id


def test_a_briefly_missed_detection_does_not_drop_the_line(now):
    t = tracker(stable_reads=2)
    for _ in range(2):
        t.update([blk("这一行偶尔会漏检一次")], now=now())
    missed = t.update([], now=now(), scanned=None)
    assert missed.dropped == () and len(missed.visible) == 1
    assert len(t.update([blk("这一行偶尔会漏检一次")], now=now()).visible) == 1


def test_pending_line_read_whole_then_clipped_is_emitted_whole(now):
    t = tracker(stable_reads=2)
    t.update([blk("我今天晚上想要打一把排位赛")], now=now())
    result = t.update([blk("我今天晚上想要打一把", w=170)], now=now())
    assert [b.text for b in result.new] == ["我今天晚上想要打一把排位赛"]


def test_a_line_that_truly_changes_is_emitted_again(now):
    t = tracker(stable_reads=2)
    first = []
    for _ in range(2):
        first += t.update([blk("房间公告今晚八点开始")], now=now()).new
    second = []
    dropped = ()
    for _ in range(2):
        r = t.update([blk("全体禁言已经开启请注意")], now=now())
        second += r.new
        dropped += r.dropped
    assert len(second) == 1 and second[0].line_id != first[0].line_id
    assert first[0].line_id in dropped


# ---- order, groups and translation flags --------------------------------------------------


def test_output_is_in_reading_order(now):
    t = tracker(stable_reads=1, group_lines=False)
    unordered = [
        blk("第三行字在最下面啊", y=300),
        blk("右边的字在同一行", x=400, y=100),
        blk("第一行字在最上面啊", y=100),
        blk("第二行字在中间位置", y=200),
    ]
    result = t.update(unordered, now=now())
    assert [b.text for b in result.new] == [
        "第一行字在最上面啊",
        "右边的字在同一行",
        "第二行字在中间位置",
        "第三行字在最下面啊",
    ]
    assert result.visible == result.new


def test_stacked_message_lines_share_a_group_and_far_lines_do_not(now):
    t = tracker(stable_reads=1)
    lines = [
        blk("小明", y=100, w=60, h=16),  # name
        blk("今天有人想要一起打游戏吗", y=122, h=20),  # message, line 1
        blk("我可以带你们上分哦哦哦", y=146, h=20),  # message, line 2
        blk("另一个人说的另一句话啊", y=300, h=20),  # far below
    ]
    visible = t.update(lines, now=now()).visible
    groups = [b.group_id for b in visible]
    assert groups[0] == groups[1] == groups[2] and groups[0] is not None
    assert groups[3] is None


def test_group_ids_stay_when_a_line_is_added(now):
    t = tracker(stable_reads=1)
    first = t.update(
        [blk("小明", y=100, w=60, h=16), blk("今天有人想要一起打游戏吗", y=122)], now=now()
    )
    gid = first.visible[0].group_id
    assert gid and first.visible[1].group_id == gid
    again = t.update(
        [
            blk("小明", y=100, w=60, h=16),
            blk("今天有人想要一起打游戏吗", y=122),
            blk("我可以带你们上分哦哦哦", y=146),
        ],
        now=now(),
    )
    assert {b.group_id for b in again.visible} == {gid}


def test_evenly_spaced_menu_rows_are_not_grouped(now):
    t = tracker(stable_reads=1)
    rows = [
        blk(label, y=100 + i * 60, w=120)
        for i, label in enumerate(["我的订单", "我的钱包", "设置中心"])
    ]
    assert all(b.group_id is None for b in t.update(rows, now=now()).visible)


def test_lines_with_nothing_to_translate_are_marked(now):
    t = tracker(stable_reads=1, skip_langs=("en",))
    lines = [
        blk("12:30", y=100, lang=None, needs_translation=False),
        blk("Lucky_77", y=140, lang="en"),
        blk("Thanks for the help", y=180, lang="en"),
        blk("谢谢你的帮助", y=220),
    ]
    flags = {b.text: b.needs_translation for b in t.update(lines, now=now()).new}
    assert flags == {
        "12:30": False,
        "Lucky_77": False,
        "Thanks for the help": False,  # already the target language
        "谢谢你的帮助": True,
    }


def test_low_quality_and_confidence_pass_through(now):
    t = tracker(stable_reads=1)
    out = t.update([blk("低质量引擎读出的字", conf=None, low_quality=True)], now=now()).new[0]
    assert out.low_quality is True and out.confidence is None


def test_tracker_never_changes_the_blocks_it_is_given(now):
    t = tracker(stable_reads=1)
    block = blk("原样不动的输入行")
    t.update([block], now=now())
    assert block.line_id is None and block.group_id is None


def test_reset_forgets_lines_and_dedupe_history(now):
    t = tracker(stable_reads=1)
    first = t.update([blk("这是一条会被重置的行")], now=now()).new
    t.reset()
    assert t.visible == ()
    again = t.update([blk("这是一条会被重置的行")], now=now()).new
    assert first and again


def test_empty_and_symbol_only_reads_are_ignored(now):
    t = tracker(stable_reads=1)
    assert t.update([blk("   "), blk("。。。"), blk("​")], now=now()).new == ()


def test_many_lines_stay_fast(now):
    import time

    t = tracker(stable_reads=2)
    page = [blk(f"第{i}行是一条比较长的聊天消息内容", y=10 + i * 22, h=18) for i in range(150)]
    t.update(page, now=now())
    start = time.perf_counter()
    result = t.update(page, now=now())
    assert len(result.new) == 150
    assert time.perf_counter() - start < 1.0


def test_time_stamped_header_lines_split_back_to_back_messages(now):
    t = tracker(stable_reads=1)
    lines = [
        blk("小明 22:05:23", y=100, w=140, h=16),
        blk("今天有人想要一起打游戏吗", y=126),
        blk("小红 22:05:40", y=156, w=140, h=16),
        blk("我可以带你们上分哦哦哦", y=182),
    ]
    groups = [b.group_id for b in t.update(lines, now=now()).visible]
    assert groups[0] == groups[1] and groups[2] == groups[3] and groups[0] != groups[2]


def test_identical_text_that_moved_beats_similar_text_in_the_old_place(now):
    """Two headers differ only by their time. After a scroll the second sits where the first
    was, and must not be mistaken for it."""
    t = tracker(stable_reads=2)
    first, second = "茉川-钻石-00 22:05:37", "茉川-钻石-00 22:05:54"
    emitted = []
    for _ in range(2):
        emitted += t.update([blk(first, y=490), blk(second, y=561)], now=now()).new
    assert len(emitted) == 2
    for _ in range(2):  # scrolled up by 71 px, no hint given
        emitted += t.update([blk(first, y=419), blk(second, y=490)], now=now()).new
    assert len(emitted) == 2


def test_a_single_stray_letter_is_not_sent_for_translation(now):
    t = tracker(stable_reads=1)
    flags = {
        b.text: b.needs_translation
        for b in t.update([blk("C", y=10, lang="en"), blk("好", y=60)], now=now()).new
    }
    assert flags == {"C": False, "好": True}


def test_scroll_with_a_frame_removes_lines_that_left_it_and_reports_them_next(now):
    t = tracker(stable_reads=2)
    emitted = []
    for _ in range(2):
        emitted += t.update(frame_of(CHAT[:4], 0), now=now()).new
    ids = [b.line_id for b in emitted]
    frame = Rect(0, 0, 400, 300)
    t.scroll(-170, frame=frame)  # lines at y=100,140 now sit above the frame; 180,220 inside
    assert [b.text for b in t.visible] == CHAT[2:4]
    result = t.unchanged(now=now())
    assert set(result.dropped) == set(ids[:2])
    assert t.unchanged(now=now()).dropped == ()  # reported once


def test_scroll_without_a_frame_keeps_todays_behaviour(now):
    t = tracker(stable_reads=2)
    for _ in range(2):
        t.update(frame_of(CHAT[:2], 0), now=now())
    t.scroll(-5000)
    assert len(t.visible) == 2


def test_a_line_removed_by_scroll_is_not_retranslated_when_it_returns(now):
    t = tracker(stable_reads=2)
    first = []
    for _ in range(2):
        first += t.update([blk(CHAT[0], y=100)], now=now()).new
    t.scroll(-500, frame=Rect(0, 0, 400, 300))
    assert t.visible == ()
    back = []
    for _ in range(2):
        back.append(t.update([blk(CHAT[0], y=200)], now=now()))
    assert all(r.new == () for r in back)
    assert back[-1].visible[0].line_id == first[0].line_id


def test_dedupe_history_is_capped_by_dedupe_max_lines(now):
    t = tracker(stable_reads=1, dedupe_max_lines=3)
    for i in range(6):
        t.update([blk(f"这是第{chr(65 + i) * 6}号独特消息")], now=now())
    assert len(t._seen) == 3


def test_dedupe_max_lines_is_validated():
    assert TrackerConfig().dedupe_max_lines == 2000
    with pytest.raises(ValueError):
        TrackerConfig(dedupe_max_lines=0)


# ---- a line OCR sometimes reads as pieces (animated sticker beside the text) ---------------

WHOLE = "大哥你上班一天有接单两小时多吗"


def emitted_whole(t, now):
    out = []
    for _ in range(2):
        out += t.update([blk(WHOLE, x=23, y=100, w=119)], now=now()).new
    assert len(out) == 1
    return out[0]


def test_pieces_of_a_known_line_are_not_new_lines_and_change_nothing(now):
    t = tracker(stable_reads=2)
    first = emitted_whole(t, now)
    pieces = [blk("大哥你上班一天", x=24, y=100, w=56), blk("有接单两小时多吗", x=80, y=100, w=62)]
    for _ in range(4):
        result = t.update(pieces, now=now())
        assert result.new == () and result.dropped == ()
        assert [b.line_id for b in result.visible] == [first.line_id]
        assert result.visible[0].text == first.text and result.visible[0].bbox == first.bbox
    back = t.update([blk(WHOLE, x=23, y=100, w=119)], now=now())
    assert back.new == () and back.visible[0].bbox == first.bbox


def test_alternating_whole_and_pieces_never_emits_again(now):
    t = tracker(stable_reads=2)
    emitted_whole(t, now)
    pieces = [blk("大哥你上班一天", x=24, y=100, w=56), blk("有接单两小时多吗", x=80, y=100, w=62)]
    total = []
    for i in range(10):
        reads = pieces if i % 2 else [blk(WHOLE, x=23, y=100, w=119)]
        total += t.update(reads, now=now()).new
    assert total == []


def test_pieces_keep_the_line_alive_without_a_whole_read(now):
    t = tracker(stable_reads=2)
    emitted_whole(t, now)
    for _ in range(6):  # more scans than emitted_max_misses
        t.update([blk("大哥你上班一天", x=24, y=100, w=56)], now=now())
    assert len(t.visible) == 1


def test_a_new_short_line_on_another_row_or_with_other_text_still_appears(now):
    t = tracker(stable_reads=2)
    emitted_whole(t, now)
    below = blk("谢谢老板", x=24, y=140, w=56)  # another row
    beside = blk("完全不同的文字", x=24, y=100, w=56)  # same place, text not contained
    emitted = []
    for _ in range(2):
        emitted += t.update([blk(WHOLE, x=23, y=100, w=119), below], now=now()).new
    assert [b.text for b in emitted] == ["谢谢老板"]
    for _ in range(2):
        emitted += t.update([beside], now=now()).new
    assert "完全不同的文字" in [b.text for b in emitted]


def test_piece_thresholds_are_configurable():
    cfg = TrackerConfig(piece_inside=0.9, piece_min_chars=3)
    assert (cfg.piece_inside, cfg.piece_min_chars) == (0.9, 3)
    with pytest.raises(ValueError):
        TrackerConfig(piece_inside=1.5)


# ---- a piece and its whole in the same update ---------------------------------------------


def test_whole_and_piece_in_one_update_is_one_new_line(now):
    t = tracker(stable_reads=2)
    whole, piece = "今天晚上一起打排位赛吧", "今天晚上一起"
    result = None
    for _ in range(2):
        result = t.update(
            [blk(piece, x=576, y=774, w=77), blk(whole, x=576, y=773, w=159)], now=now()
        )
    assert [b.text for b in result.new] == [whole]
    assert len(result.visible) == 1


def test_piece_one_frame_before_the_whole_becomes_the_whole(now):
    t = tracker(stable_reads=2)
    whole = "今天晚上一起打排位赛吧"
    t.update([blk("今天晚上", x=607, y=811, w=60)], now=now())
    new = []
    for _ in range(2):
        new += t.update([blk(whole, x=606, y=810, w=175)], now=now()).new
    assert [b.text for b in new] == [whole]
    assert len(t.update([blk(whole, x=606, y=810, w=175)], now=now()).visible) == 1


def test_two_different_words_side_by_side_stay_separate(now):
    t = tracker(stable_reads=1)
    result = t.update([blk("你好吗朋友", x=20, w=100), blk("再见了朋友", x=140, w=100)], now=now())
    assert len(result.new) == 2


# ---- look-alike lines after a scroll ------------------------------------------------------


def test_look_alike_name_lines_with_other_times_keep_their_ids_after_a_scroll(now):
    t = tracker(stable_reads=2)
    names = ["茉川-钻石-遥遥 20:53:31", "茉川-钻石-遥遥 20:53:42", "茉川-钻石-遥遥 20:54:07"]
    ids = {}
    for _ in range(2):
        for b in t.update(
            [blk(n, y=100 + i * 100) for i, n in enumerate(names)], now=now()
        ).visible:
            ids[b.text] = b.line_id
    t.scroll(-100)
    shown = [blk(n, y=100 + i * 100 - 100) for i, n in enumerate(names)]
    shown.append(blk("茉川-钻石-遥遥 20:55:19", y=300))
    for _ in range(2):
        result = t.update(shown[1:], now=now())
    by_text = {b.text: b.line_id for b in result.visible}
    assert by_text[names[1]] == ids[names[1]]
    assert by_text[names[2]] == ids[names[2]]
    assert by_text["茉川-钻石-遥遥 20:55:19"] not in ids.values()


def test_a_text_only_match_needs_the_same_numbers(now):
    t = tracker(stable_reads=2)
    for _ in range(2):
        first = t.update([blk("茉川-钻石-遥遥 20:53:31", y=500)], now=now())
    for _ in range(2):
        result = t.update([blk("茉川-钻石-遥遥 20:53:42", y=100)], now=now())
    assert result.visible[0].line_id != first.visible[0].line_id


def test_repeated_short_message_twice_keeps_both_ids_across_a_scroll(now):
    t = tracker(stable_reads=2)
    for _ in range(2):
        first = t.update([blk("好", y=100, w=20), blk("好", y=200, w=20)], now=now())
    ids = [b.line_id for b in first.visible]
    assert len(set(ids)) == 2
    t.scroll(-60)
    result = t.update([blk("好", y=40, w=20), blk("好", y=140, w=20)], now=now())
    assert result.new == ()
    assert [b.line_id for b in result.visible] == ids


# ---- TrackerUpdate.read -------------------------------------------------------------------


def test_read_holds_only_lines_matched_in_this_update(now):
    t = tracker(stable_reads=2)
    a, b = "这是第一条测试消息啊", "这是第二条测试消息啊"
    lines = [blk(a, y=100), blk(b, y=300)]
    for _ in range(2):
        first = t.update(lines, now=now())
    ids = {x.text: x.line_id for x in first.visible}
    assert first.read == {ids[a], ids[b]}
    carried = t.update([blk(a, y=100)], now=now(), scanned=[Rect(0, 80, 400, 60)])
    assert carried.read == {ids[a]}  # b lies outside the scanned region
    assert {x.line_id for x in carried.visible} == {ids[a], ids[b]}
    assert t.unchanged(now=now()).read == frozenset()


def test_a_returning_line_is_in_read_when_read_again(now):
    t = tracker(stable_reads=2)
    text = "这一行会离开然后回来"
    for _ in range(2):
        first = t.update([blk(text)], now=now())
    for _ in range(3):
        t.update([], now=now(), scanned=None)
    results = [t.update([blk(text)], now=now()) for _ in range(2)]
    line_id = first.visible[0].line_id
    assert results[-1].new == () and line_id in results[-1].read
