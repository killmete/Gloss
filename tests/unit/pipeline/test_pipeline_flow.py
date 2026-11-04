"""The running pipeline with scripted fakes: data flow, control calls, privacy gate."""

from __future__ import annotations

import time

import pytest
from testkit.pipeline import (
    Rig,
    block,
    fast_config,
    solid,
    stays,
    wait_until,
    with_patch,
)

from gloss.core.contracts import CaptureTarget, Health, Rect
from gloss.core.registry import Registry

REGION = Rect(100, 50, 200, 120)
LINE1 = "你好吗朋友"
LINE2 = "今天天气很好"


@pytest.fixture
def rig():
    made: list[Rig] = []

    def make(config=None, **kw) -> Rig:
        r = Rig(config or fast_config(**kw))
        made.append(r)
        return r

    yield make
    for r in made:
        r.close()


def basic(rig, **kw):
    r = rig(**kw)
    cap = r.capture("cap", solid())
    ocr = r.ocr("ocr", [block(LINE1, y=10), block(LINE2, y=60)])
    return r, cap, ocr


# ---- data flow ----------------------------------------------------------------------------


def test_text_flows_from_capture_through_ocr_and_translation_to_the_sink(rig):
    r, _, _ = basic(rig)
    r.start()
    wait_until(lambda: len(r.sink.shown()) >= 2, what="two translated lines")
    shown = {i.result.source_text: i for i in r.sink.shown()}
    assert shown[LINE1].result.text == f"EN:{LINE1}"
    assert shown[LINE2].result.text == f"EN:{LINE2}"
    assert r.sink.metas[-1].region == REGION  # frame meta reaches the sink


def test_bboxes_are_desktop_coordinates(rig):
    r, _, _ = basic(rig)
    r.start()
    wait_until(lambda: len(r.sink.shown()) >= 2, what="two translated lines")
    by_text = {i.result.source_text: i for i in r.sink.shown()}
    # block() puts a line at frame (5, y) size 120x20; the region sits at (100, 50).
    assert by_text[LINE1].bbox == Rect(105, 60, 120, 20)
    assert by_text[LINE2].bbox == Rect(105, 110, 120, 20)


def test_each_line_is_translated_once_and_unchanged_frames_cause_no_more_ocr(rig):
    r, cap, ocr = basic(rig)
    r.start()
    wait_until(lambda: len(r.sink.shown()) >= 2, what="translation")
    reads, grabs = len(ocr.calls), cap.grabs
    wait_until(lambda: cap.grabs > grabs + 15, what="more unchanged frames")
    assert len(ocr.calls) == reads
    assert sorted(r.stage.texts) == sorted([LINE1, LINE2])


def test_first_read_is_the_whole_frame(rig):
    r, _, ocr = basic(rig)
    r.start()
    wait_until(lambda: ocr.calls, what="first OCR call")
    assert ocr.calls[0] == (REGION, None)


def test_small_change_reads_only_the_dirty_area(rig):
    r, cap, ocr = basic(rig)
    r.start()
    wait_until(lambda: len(r.sink.shown()) >= 2, what="translation")
    cap.image = with_patch(cap.image)
    wait_until(lambda: len(ocr.calls) >= 2, what="a second OCR call")
    regions = ocr.calls[1][1]
    assert regions is not None and len(regions) >= 1
    assert all(a.w * a.h <= 200 * 120 for a in regions)


def test_a_new_line_is_translated_once_more_not_everything_again(rig):
    r, cap, ocr = basic(rig)
    r.start()
    wait_until(lambda: len(r.sink.shown()) >= 2, what="translation")
    ocr.blocks.append(block("新消息来了", y=90))
    cap.image = with_patch(cap.image)
    wait_until(lambda: "新消息来了" in r.stage.texts, what="the new line")
    assert stays(lambda: sorted(r.stage.texts) == sorted([LINE1, LINE2, "新消息来了"]))


def test_english_and_number_only_lines_are_not_sent(rig):
    r = rig()
    r.capture("cap", solid())
    r.ocr(
        "ocr",
        [
            block(LINE1, y=0),
            block("hello there my friend", y=25, lang="en"),
            block("12345678", y=50, lang=None),
            block("22:05:23", y=75, lang=None),
        ],
    )
    r.start()
    wait_until(lambda: r.stage.texts, what="translation")
    assert stays(lambda: r.stage.texts == [LINE1])


def test_pipeline_reports_ok_when_everything_works(rig):
    r, _, _ = basic(rig)
    r.start()
    wait_until(lambda: len(r.sink.shown()) >= 2, what="translation")
    wait_until(lambda: r.sink.last_health() == Health("ok"), what="status ok")
    assert r.pipeline.health().state == "ok"


def test_a_line_that_leaves_the_screen_is_removed(rig):
    r, cap, ocr = basic(rig)
    r.start()
    wait_until(lambda: len(r.sink.shown()) >= 2, what="translation")
    ocr.blocks[:] = [ocr.blocks[0]]
    flip = [0]

    def change_screen_and_check():
        flip[0] += 1  # the tracker needs a few reads of the area before it lets a line go
        cap.image = with_patch(cap.image, x=10 + flip[0] % 90, y=60)
        return r.sink.removed

    wait_until(change_screen_and_check, what="the line to be removed")


# ---- control calls ------------------------------------------------------------------------


def test_set_target_reopens_capture_and_reads_the_new_area_in_full(rig):
    r, cap, ocr = basic(rig)
    r.start()
    wait_until(lambda: len(r.sink.shown()) >= 2, what="translation")
    new = CaptureTarget("region", region=Rect(400, 300, 200, 120))
    ocr.blocks[:] = [block("全新的区域文字", y=40)]
    r.pipeline.set_target(new)
    wait_until(lambda: len(cap.targets) >= 2, what="capture to reopen")
    assert cap.targets[-1] == new
    wait_until(lambda: "全新的区域文字" in r.sink.texts(), what="text from the new area")
    item = next(i for i in r.sink.shown() if i.result.source_text == "全新的区域文字")
    assert item.bbox == Rect(405, 340, 120, 20)  # new origin added
    full_reads = [c for c in ocr.calls if c[1] is None]
    assert len(full_reads) >= 2  # the new area was read in full, not as a partial change


def test_no_target_means_no_grabbing_and_a_helpful_status(rig):
    r = rig()
    cap = r.capture("cap", solid())
    r.ocr("ocr")
    pipeline = r.start(no_target=True)
    wait_until(lambda: pipeline.health().state == "degraded", what="status")
    assert "screen area" in pipeline.health().detail
    assert cap.grabs == 0


def test_set_paused_stops_grabbing_and_resumes(rig):
    r, cap, _ = basic(rig)
    pipeline = r.start()
    wait_until(lambda: len(r.sink.shown()) >= 2, what="translation")
    pipeline.set_paused(True)
    time.sleep(0.2)  # a grab already under way may finish
    frozen = cap.grabs
    time.sleep(0.4)
    assert cap.grabs == frozen
    assert pipeline.health() == Health("ok", "Paused.")
    pipeline.set_paused(False)
    wait_until(lambda: cap.grabs > frozen + 3, what="grabbing to resume")
    assert pipeline.health().detail != "Paused."


def test_set_low_quality_switches_ocr_to_the_windows_engine_and_back(rig):
    r = rig()
    cap = r.capture("cap", solid())
    main = r.ocr("ocr", [block(LINE1)])
    low = r.ocr("windows", [block(LINE2)])
    pipeline = r.start()
    wait_until(lambda: LINE1 in r.sink.texts(), what="primary engine text")
    assert not low.calls

    pipeline.set_low_quality(True)
    flip = [0]

    def change_and_check():
        flip[0] += 1
        cap.image = with_patch(cap.image, x=10 + flip[0] % 100)
        return low.calls

    wait_until(change_and_check, timeout=6, what="the windows engine to be used")
    # A read already in flight at the switch may still go to the old engine; no more after.
    before = len(main.calls)
    for _ in range(5):
        change_and_check()
        time.sleep(0.05)
    assert len(main.calls) <= before + 1
    wait_until(lambda: LINE2 in r.sink.texts(), what="windows engine text")

    pipeline.set_low_quality(False)
    seen = len(main.calls)

    def back():
        flip[0] += 1
        cap.image = with_patch(cap.image, y=5 + flip[0] % 50)
        return len(main.calls) > seen

    wait_until(back, timeout=6, what="the primary engine to be used again")


def test_set_low_quality_without_a_windows_engine_is_a_visible_down_not_a_crash(rig):
    r, cap, _ = basic(rig)
    pipeline = r.start()
    wait_until(lambda: len(r.sink.shown()) >= 2, what="translation")
    pipeline.set_low_quality(True)
    flip = [0]

    def change_screen_and_check():
        flip[0] += 1  # the status only updates when there is a frame to read
        cap.image = with_patch(cap.image, x=10 + flip[0] % 100)
        return pipeline.health().state == "down"

    wait_until(change_screen_and_check, timeout=3, what="status down")
    assert "ocr" in pipeline.health().detail.lower()


# ---- translate queue ------------------------------------------------------------------------


def test_translate_queue_overflow_drops_the_oldest_lines_and_says_so(rig):
    config = fast_config(queue_size=1, batch_max_lines=2, translate_timeout_ms=5000)
    r = rig(config)
    r.capture("cap", solid())
    names = [f"第{n}条消息内容" for n in "一二三四五六七八"]
    r.ocr("ocr", [block(t, y=i * 14, h=12) for i, t in enumerate(names)])
    gate = r.event()
    r.stage.waits.append(gate)  # the first batch holds the translate worker
    pipeline = r.start()
    wait_until(lambda: r.stage.batches, what="the first batch to start")
    wait_until(lambda: pipeline.health().state == "degraded", what="the overflow status")
    assert "keep up" in pipeline.health().detail
    gate.set()
    wait_until(lambda: pipeline.health().state == "ok", what="recovery once the queue drained")
    sent = set(r.stage.texts)
    # All eight arrive at once and the queue holds two: the six oldest are dropped.
    assert sent == set(names[-2:])
    assert sent == set(r.sink.texts())  # everything that was sent was shown


# ---- privacy gate (built through the real factory) ------------------------------------------


def _translator(name: str, *, local_only: bool | None, log: list[str]):
    body = {
        "name": name,
        "REQUIRES_CONTRACT": ">=1.0,<2.0",
        "__init__": lambda self, ctx: log.append(f"made:{name}"),
        "open": lambda self: None,
        "close": lambda self: None,
        "health": lambda self: Health("ok"),
    }
    if local_only is not None:
        body["LOCAL_ONLY"] = local_only

    def translate(self, request):
        from gloss.core.contracts import TranslationResponse, TranslationResult

        log.append(f"translate:{name}")
        return TranslationResponse(
            tuple(
                TranslationResult(i.line_id, i.text, f"[{name}] {i.text}", name, "m", False, 1)
                for i in request.items
            )
        )

    body["translate"] = translate
    return type(f"T_{name}", (), body)


@pytest.fixture
def user_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("gloss.core.config.user_config_path", lambda: tmp_path / "config.toml")
    return tmp_path


def test_with_cloud_off_no_cloud_translator_is_built_or_called(user_dir):
    from gloss.app import make_translation_factory
    from gloss.core.contracts import TranslationItem

    log: list[str] = []
    registry = Registry()
    registry.register("translate", "cloudy", _translator("cloudy", local_only=None, log=log))
    registry.register("translate", "lying", _translator("lying", local_only=False, log=log))
    registry.register("translate", "local", _translator("local", local_only=True, log=log))
    config = fast_config(provider="cloudy", translate_fallbacks=("lying", "local"), cloud=False)

    stage = make_translation_factory(config, registry, None)()
    try:
        stage.open()
        results = stage.translate([TranslationItem("a", "你好吗朋友")])
    finally:
        stage.close()
    assert results[0].provider == "local" and results[0].status == "ok"
    assert log == ["made:local", "translate:local"]


def test_with_cloud_off_and_no_local_translator_nothing_is_sent_and_nothing_crashes(user_dir):
    from gloss.app import make_translation_factory
    from gloss.core.contracts import TranslationItem

    log: list[str] = []
    registry = Registry()
    registry.register("translate", "cloudy", _translator("cloudy", local_only=None, log=log))
    config = fast_config(provider="cloudy", cloud=False)
    stage = make_translation_factory(config, registry, None)()
    try:
        stage.open()
        results = stage.translate([TranslationItem("a", "你好吗朋友")])
    finally:
        stage.close()
    assert log == []
    assert results[0].status in ("failed", "passthrough")
    assert results[0].provider != "cloudy"


def test_with_cloud_on_the_primary_cloud_translator_is_used(user_dir):
    from gloss.app import make_translation_factory
    from gloss.core.contracts import TranslationItem

    log: list[str] = []
    registry = Registry()
    registry.register("translate", "cloudy", _translator("cloudy", local_only=None, log=log))
    config = fast_config(provider="cloudy", cloud=True)
    stage = make_translation_factory(config, registry, None)()
    try:
        stage.open()
        results = stage.translate([TranslationItem("a", "你好吗朋友")])
    finally:
        stage.close()
    assert results[0].provider == "cloudy"
    assert log == ["made:cloudy", "translate:cloudy"]


def test_a_translator_that_cannot_be_created_is_skipped_not_fatal(user_dir):
    from gloss.app import make_translation_factory
    from gloss.core.contracts import TranslationItem

    log: list[str] = []
    broken = _translator("broken", local_only=None, log=log)

    def boom(self, ctx):
        raise RuntimeError("no")

    broken.__init__ = boom
    registry = Registry()
    registry.register("translate", "broken", broken)
    registry.register("translate", "okay", _translator("okay", local_only=None, log=log))
    config = fast_config(provider="broken", translate_fallbacks=("okay",))
    stage = make_translation_factory(config, registry, None)()
    try:
        stage.open()
        results = stage.translate([TranslationItem("a", "你好吗朋友")])
    finally:
        stage.close()
    assert results[0].provider == "okay"
