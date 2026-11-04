"""Failure drills (SPEC section 7): every one must end in a visible status, never a crash."""

from __future__ import annotations

import pytest
from testkit.pipeline import Rig, block, fast_config, solid, stays, wait_until, with_patch

from gloss.core.contracts import AdapterUnavailable, Health, PermanentError, TransientError

LINE1 = "你好吗朋友"
LINE2 = "今天天气很好"
NEW = "新来的消息内容"
_FLIPS = [0]


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


def shown(r: Rig, n: int = 2) -> None:
    wait_until(lambda: len(r.sink.shown()) >= n, what=f"{n} translated lines")


def flip_screen(cap) -> None:
    _FLIPS[0] += 1
    cap.image = with_patch(cap.image, x=10 + _FLIPS[0] % 100)


def alive(pipeline) -> bool:
    return all(s.thread.is_alive() for s in pipeline._stages.values())


# ---- OCR hangs and errors -------------------------------------------------------------------


def test_hung_ocr_is_restarted_status_goes_degraded_then_ok_and_the_lost_frame_is_reread(rig):
    r, cap, ocr = basic(rig)
    pipeline = r.start()
    shown(r)
    hang, gate = r.event(), r.event()
    ocr.waits[:] = [hang, gate]
    ocr.blocks.append(block(NEW, y=90))  # what the lost frame would have revealed
    cap.image = with_patch(cap.image)  # a small change: a partial read, which will hang
    wait_until(lambda: len(ocr.calls) >= 2, what="the read that hangs")
    assert ocr.calls[1][1] is not None  # it was a partial read

    wait_until(lambda: pipeline.health().state == "degraded", what="degraded after the hang")
    assert "stopped responding" in pipeline.health().detail
    wait_until(lambda: "degraded" in r.sink.states(), what="the degraded status in the sink")

    # The replacement worker must read the whole frame, because the lost one's change is gone.
    wait_until(lambda: len(ocr.calls) >= 3, what="the replacement worker's read")
    assert ocr.calls[2][1] is None
    gate.set()
    wait_until(lambda: NEW in r.sink.texts(), what="the line the lost frame held")
    wait_until(lambda: pipeline.health().state == "ok", what="status ok again")
    wait_until(lambda: r.sink.last_health() == Health("ok"), what="ok reported to the sink")
    assert r.sink.states().index("degraded") < len(r.sink.states()) - 1


def test_a_stale_ocr_worker_waking_up_does_not_clear_the_restart_warning(rig):
    """The pipeline docstring promises the abandoned thread exits 'without touching shared
    state'. After the OCR read returns in the stale thread it still runs chain.succeeded and
    sets stage.health / stage.note = ok, before checking whether it is still current."""
    r, cap, ocr = basic(rig)
    pipeline = r.start()
    shown(r)
    hang, gate = r.event(), r.event()
    ocr.waits[:] = [hang, gate]
    cap.image = with_patch(cap.image)
    wait_until(lambda: pipeline.health().state == "degraded", what="degraded after the hang")
    wait_until(lambda: len(ocr.calls) >= 3, what="the replacement worker's read")
    hang.set()  # the abandoned read finally returns, while the new worker is still busy
    assert stays(lambda: pipeline.health().state == "degraded", 0.15), (
        "the stale worker reset the status to ok before the new worker finished a read"
    )
    gate.set()


def test_ocr_that_is_not_registered_is_a_visible_down_and_nothing_crashes(rig):
    r = rig(ocr=())
    cap = r.capture("cap", solid())
    pipeline = r.start()
    wait_until(lambda: pipeline.health().state == "down", what="status down")
    assert "ocr" in pipeline.health().detail.lower()
    wait_until(lambda: r.sink.last_health() and r.sink.last_health().state == "down", what="sink")
    before = cap.grabs
    wait_until(lambda: cap.grabs > before + 3, what="capture to keep going")
    assert alive(pipeline)


def test_ocr_engine_missing_its_model_is_down_with_the_reason(rig):
    r, _, ocr = basic(rig)
    ocr.open_error = AdapterUnavailable("the model files are missing")
    pipeline = r.start()
    wait_until(lambda: pipeline.health().state == "down", what="status down")
    assert "model files are missing" in pipeline.health().detail
    assert alive(pipeline)


def test_ocr_permanent_error_disables_the_engine_for_the_session(rig):
    r, cap, ocr = basic(rig)
    ocr.read_error = PermanentError("bad setting")
    pipeline = r.start()
    wait_until(lambda: pipeline.health().state == "down", what="status down")
    assert "bad setting" in pipeline.health().detail
    reads = len(ocr.calls)
    flip_screen(cap)
    assert stays(lambda: len(ocr.calls) == reads, 0.3)


def test_ocr_transient_errors_open_the_breaker_after_breaker_failures(rig):
    r, cap, ocr = basic(rig, breaker_failures=3)
    ocr.read_error = TransientError("blip")
    pipeline = r.start()
    wait_until(lambda: len(ocr.calls) >= 3, what="three failed reads")
    wait_until(lambda: pipeline.health().state == "down", what="status down")
    assert stays(lambda: len(ocr.calls) == 3, 0.3)  # breaker open: no more attempts


def test_a_failed_read_is_retried_in_full_so_no_change_is_lost(rig):
    r, cap, ocr = basic(rig, breaker_failures=50)
    lines = ocr.blocks[:]
    ocr.blocks[:] = []
    ocr.read_error = TransientError("blip")
    r.start()
    wait_until(lambda: len(ocr.calls) >= 2, what="a retry")
    ocr.read_error = None
    ocr.blocks[:] = lines
    wait_until(lambda: len(r.sink.shown()) >= 2, what="lines once the engine recovers")
    assert all(c[1] is None for c in ocr.calls)  # every attempt was a whole-frame read


# ---- translate hangs and crashes ------------------------------------------------------------


def test_hung_translate_shows_the_batch_as_failed_and_replaces_the_worker(rig):
    r, cap, ocr = basic(rig)
    hang = r.event()
    r.stage.waits.append(hang)
    pipeline = r.start()
    wait_until(lambda: r.stage.batches, what="the batch that hangs")
    wait_until(lambda: len(r.sink.shown("failed")) >= 2, what="in-flight lines shown as failed")
    assert sorted(r.sink.texts("failed")) == sorted([LINE1, LINE2])
    assert all(i.result.text == "" for i in r.sink.shown("failed"))
    assert all(i.bbox is not None for i in r.sink.shown("failed"))
    wait_until(lambda: r.stage.stages_made == 2, what="a replacement translate stage")
    # The new worker translates new lines normally and the status recovers.
    ocr.blocks.append(block(NEW, y=90))
    cap.image = with_patch(cap.image)
    wait_until(lambda: NEW in r.sink.texts("ok"), what="a line translated by the new worker")
    wait_until(lambda: pipeline.health().state == "ok", what="status ok again")
    hang.set()
    # the abandoned worker returning must not show the old batch a second time
    assert stays(lambda: LINE1 not in r.sink.texts("ok"), 0.2)


def test_a_translate_worker_crash_restarts_it_and_its_lines_are_not_lost_silently(rig):
    """A bug in the translate step (not a provider error) raises out of the worker loop. The
    worker is restarted, but the batch it held is neither shown nor marked failed, and the
    tracker will not send those lines again, so they vanish from the user's view."""
    r, _, _ = basic(rig)
    r.stage.crash_once = True
    r.start()
    wait_until(lambda: r.stage.stages_made >= 2, what="the translate worker to restart")
    seen = lambda: {i.result.source_text for i in r.sink.snapshot()}  # noqa: E731
    stays(lambda: True, 0.3)
    assert {LINE1, LINE2} <= seen(), (
        f"lines of the crashed batch were never shown: missing {sorted({LINE1, LINE2} - seen())}"
    )


# ---- capture chain --------------------------------------------------------------------------


def test_capture_unavailable_primary_uses_the_fallback_and_status_is_degraded(rig):
    r = rig(capture=("a", "b"))
    a = r.capture("a", solid())
    b = r.capture("b", solid())
    r.ocr("ocr", [block(LINE1)])
    a.open_error = AdapterUnavailable("window is minimised")
    pipeline = r.start()
    wait_until(lambda: LINE1 in r.sink.texts(), what="text through the fallback")
    assert b.grabs > 0
    health = pipeline.health()
    assert health.state == "degraded" and "backup" in health.detail
    assert "window is minimised" in health.detail
    assert stays(lambda: a.opens == 1, 0.2)  # not hammered: it is tried again after a cooldown


def test_capture_primary_that_becomes_unavailable_mid_run_falls_back(rig):
    r = rig(capture=("a", "b"))
    a = r.capture("a", solid())
    b = r.capture("b", solid())
    r.ocr("ocr", [block(LINE1)])
    pipeline = r.start()
    wait_until(lambda: LINE1 in r.sink.texts(), what="text from the primary")
    assert b.grabs == 0
    a.grab_error = AdapterUnavailable("device lost")
    wait_until(lambda: b.grabs > 0, what="the fallback to take over")
    wait_until(lambda: pipeline.health().state == "degraded", what="status degraded")


def test_capture_permanent_error_disables_the_primary_for_the_session(rig):
    r = rig(capture=("a", "b"))
    a = r.capture("a", solid())
    b = r.capture("b", solid())
    r.ocr("ocr", [block(LINE1)])
    a.open_error = PermanentError("invalid capture setting")
    pipeline = r.start()
    wait_until(lambda: b.grabs > 5, what="the fallback to run")
    assert a.opens == 1  # never tried again
    assert "invalid capture setting" in pipeline.health().detail
    assert pipeline.health().state == "degraded"


def test_capture_transient_errors_open_the_breaker_and_the_fallback_takes_over(rig):
    r = rig(capture=("a", "b"), breaker_failures=3)
    a = r.capture("a", solid())
    b = r.capture("b", solid())
    r.ocr("ocr", [block(LINE1)])
    a.grab_error = TransientError("blip")
    pipeline = r.start()
    wait_until(lambda: b.grabs > 3, what="the fallback to run")
    assert a.grabs == 3  # exactly breaker_failures attempts, then skipped
    wait_until(lambda: LINE1 in r.sink.texts(), what="text through the fallback")
    assert pipeline.health().state == "degraded"


def test_all_capture_sources_failing_is_a_visible_down(rig):
    r = rig(capture=("a", "b"))
    a = r.capture("a", solid())
    b = r.capture("b", solid())
    r.ocr("ocr")
    a.open_error = AdapterUnavailable("one")
    b.open_error = AdapterUnavailable("two")
    pipeline = r.start()
    wait_until(lambda: pipeline.health().state == "down", what="status down")
    assert alive(pipeline)


def test_black_frames_trigger_a_trial_grab_and_a_switch_to_the_next_source(rig):
    r = rig(capture=("a", "b"), black_reads=3)
    a = r.capture("a", solid(value=0))
    b = r.capture("b", solid())
    r.ocr("ocr", [block(LINE1)])
    pipeline = r.start()
    wait_until(lambda: b.grabs >= 1, what="the trial grab from the next source")
    wait_until(lambda: pipeline.health().state == "degraded", what="status degraded")
    assert "black" in pipeline.health().detail
    wait_until(lambda: b.grabs > 8, what="the second source to carry on")
    frozen = a.grabs
    assert stays(lambda: a.grabs == frozen, 0.2)  # the black source is no longer used
    assert LINE1 in r.sink.texts()


def test_black_frames_from_every_source_do_not_switch_and_say_so(rig):
    r = rig(capture=("a", "b"), black_reads=3)
    a = r.capture("a", solid(value=0))
    b = r.capture("b", solid(value=0))
    r.ocr("ocr")
    pipeline = r.start()
    wait_until(lambda: b.grabs >= 1, what="the trial grab")
    wait_until(lambda: a.grabs > 30, what="the primary to keep running")
    assert b.grabs <= 2  # one trial, not a permanent switch
    wait_until(lambda: pipeline.health().state == "degraded", what="status degraded")


def test_capture_hang_is_restarted_and_recovers(rig):
    r, cap, _ = basic(rig)
    pipeline = r.start()
    shown(r)
    cap.hang()
    wait_until(lambda: pipeline._stages["capture"].generation >= 2, what="a capture restart")
    wait_until(lambda: pipeline.health().state == "degraded", what="status degraded")
    cap.release()
    grabs = cap.grabs
    wait_until(lambda: cap.grabs > grabs + 5, what="capture to run again")
    wait_until(lambda: pipeline.health().state == "ok", what="status ok again")


def test_a_capture_loop_crash_restarts_the_worker(rig):
    r, cap, _ = basic(rig)
    cap.bad_frame_once = True  # grab returns a non-Frame; the loop raises AttributeError
    pipeline = r.start()
    wait_until(lambda: pipeline._stages["capture"].generation == 2, what="the worker restart")
    shown(r)
    wait_until(lambda: pipeline.health().state == "ok", what="status ok again")


def test_stop_closes_the_adapters_and_the_threads_end(rig):
    r, cap, _ = basic(rig)
    pipeline = r.start()
    shown(r)
    threads = [s.thread for s in pipeline._stages.values()]
    pipeline.stop()
    assert not any(t.is_alive() for t in threads)
    assert cap.closes >= 1
    assert r.stage.closed >= 1


def test_a_sink_that_fails_to_take_the_status_does_not_stop_status_reports(rig):
    r, cap, ocr = basic(rig)
    r.sink.fail_set_health = 2
    pipeline = r.start()
    shown(r)
    ocr.read_error = PermanentError("later trouble")
    flip_screen(cap)
    wait_until(lambda: r.sink.last_health() and r.sink.last_health().state == "down", what="down")
    assert pipeline.health().state == "down"
