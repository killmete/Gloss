"""A translator that stalls while starting is skipped, never waited on for ever."""

from __future__ import annotations

import dataclasses
import threading
import time

from test_translate_service import SETTINGS, Harness, cloud, local

from gloss.core.contracts import AdapterUnavailable

FAST = dataclasses.replace(SETTINGS, open_timeout_s=0.1)


class Stalling:
    """Makes a scripted translator's open() block until ``release`` is set."""

    def __init__(self, translator):
        self.release = threading.Event()
        self.finished = threading.Event()
        self.stall = True
        original = translator.open

        def open_() -> None:
            original()
            if self.stall:
                self.release.wait(10)
            self.finished.set()

        translator.open = open_


def test_open_that_stalls_uses_the_fallback_and_shows_degraded(tmp_path):
    primary = cloud()
    stall = Stalling(primary)
    try:
        h = Harness(tmp_path, [primary, local()], settings=FAST)
        started = time.monotonic()
        h.service.open()
        assert time.monotonic() - started < 2
        (result,) = h.translate("你好")
        assert result.provider == "null" and result.status == "passthrough"
        status = h.service.status()
        assert status.health.state == "degraded"
        assert status.providers[0].state == "unavailable"
        assert "did not start in time" in status.banner and "cloud" in status.banner
        assert primary.requests == []
    finally:
        stall.release.set()


def test_a_late_finish_is_ignored_and_no_second_open_runs_meanwhile(tmp_path):
    primary = cloud()
    stall = Stalling(primary)
    h = Harness(tmp_path, [primary, local()], settings=FAST)
    h.service.open()
    assert primary.opens == 1
    stall.release.set()  # the abandoned call now finishes
    assert stall.finished.wait(2)
    assert h.service.status().providers[0].state == "unavailable"  # still on cooldown
    (result,) = h.translate("你好")
    assert result.provider == "null" and primary.requests == []
    assert primary.opens == 1


def test_still_running_open_is_not_started_twice_after_the_cooldown(tmp_path):
    primary = cloud()
    stall = Stalling(primary)
    try:
        h = Harness(tmp_path, [primary, local()], settings=FAST)
        h.service.open()
        h.time["mono"] += SETTINGS.breaker_cooldown_s + 1
        (result,) = h.translate("你好")
        assert result.provider == "null" and primary.opens == 1
        assert h.service.status().providers[0].state == "unavailable"
    finally:
        stall.release.set()


def test_provider_recovers_after_the_cooldown(tmp_path):
    primary = cloud()
    stall = Stalling(primary)
    h = Harness(tmp_path, [primary, local()], settings=FAST)
    h.service.open()
    stall.release.set()
    assert stall.finished.wait(2)
    stall.stall = False
    h.time["mono"] += SETTINGS.breaker_cooldown_s + 1
    (later,) = h.translate("别的")
    assert later.provider == "cloud" and later.status == "ok"
    assert h.service.status().health.state == "ok"


def test_errors_from_a_quick_open_keep_their_normal_handling(tmp_path):
    primary = cloud()
    primary.open_error = AdapterUnavailable("No API key. Add one in settings.")
    h = Harness(tmp_path, [primary, local()], settings=FAST)
    h.service.open()
    assert "No API key" in h.service.status().banner


def test_without_a_limit_open_runs_in_the_calling_thread(tmp_path):
    primary = cloud()
    seen = []
    primary.open = lambda: seen.append(threading.current_thread())
    h = Harness(tmp_path, [primary], settings=dataclasses.replace(SETTINGS, open_timeout_s=None))
    h.service.open()
    assert seen == [threading.current_thread()]


def test_the_limit_comes_from_the_translate_timeout_and_stays_below_the_watchdog():
    from test_translate_service import CONFIG

    assert SETTINGS.open_timeout_s == CONFIG.reliability.translate_timeout_ms / 2000
