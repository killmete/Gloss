"""The Windows.Media.Ocr adapter: contract checks with the languages installed on this PC,
plus the rules of ADR-0001 (never an automatic fallback, low quality, exact fix for a missing
pack)."""

from __future__ import annotations

import sys

import pytest
from testkit.core import Subject
from testkit.frames import text_frame
from testkit.ocr import (
    CHECKS,
    check_windows_ocr_is_never_an_automatic_fallback_and_is_low_quality,
)

from gloss.adapters.ocr import windows_ocr
from gloss.core.contracts import AdapterContext, AdapterUnavailable, PermanentError, Rect, TextBlock
from gloss.core.registry import default_registry, discover

discover()
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")


def subject(languages) -> Subject:
    def make(context):
        return default_registry.create("ocr", "windows", context)

    return Subject("ocr", "windows", make, {"languages": languages}, real=True)


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__.removeprefix("check_"))
def test_contract_checks_with_chinese_and_english(check):
    s = subject(["zh", "en"])
    try:
        check(s)
    finally:
        s.release()


def test_adr_0001_rule_with_chinese_and_english():
    s = subject(["zh", "en"])
    try:
        check_windows_ocr_is_never_an_automatic_fallback_and_is_low_quality(s)
    finally:
        s.release()


def test_not_an_automatic_fallback_in_the_registry():
    cls = default_registry._adapters["ocr"]["windows"]
    assert cls.ALLOW_AUTO_FALLBACK is False
    assert default_registry.resolve_chain("ocr", "rapidocr", ["windows"]) == ("rapidocr",)
    assert default_registry.resolve_chain("ocr", "windows") == ("windows",)  # explicit choice


def test_output_is_low_quality_with_unknown_confidence():
    s = subject(["zh", "en"])
    try:
        adapter = s.opened()
        blocks = adapter.recognize(text_frame(), None)
        assert blocks
        assert all(b.low_quality and b.confidence is None for b in blocks)
        assert adapter.health().state == "degraded"  # amber while low-quality mode is on
    finally:
        s.release()


def test_missing_language_pack_gives_the_exact_fix(monkeypatch):
    """Simulates a PC without the Japanese pack, whatever is really installed."""
    adapter = default_registry.create(
        "ocr", "windows", AdapterContext(settings={"languages": ["zh", "ja"]})
    )
    monkeypatch.setitem(windows_ocr.WINDOWS_LANGS, "ja", ("qq-ZZ", "Language.OCR~~~ja-JP~0.0.1.0"))
    with pytest.raises(AdapterUnavailable) as caught:
        adapter.open()
    message = str(caught.value)
    assert 'Add-WindowsCapability -Online -Name "Language.OCR~~~ja-JP~0.0.1.0"' in message
    assert "administrator" in message
    assert adapter.health().state == "down"


@pytest.mark.parametrize("code", ["zz", "ko", "th"])
def test_languages_windows_cannot_read_are_permanent_errors(code):
    adapter = default_registry.create(
        "ocr", "windows", AdapterContext(settings={"languages": [code]})
    )
    with pytest.raises(PermanentError):
        adapter.open()


def test_cjk_spacing_is_removed_but_latin_spacing_stays():
    assert windows_ocr._tidy("你 好 世 界") == "你好世界"
    assert windows_ocr._tidy("hello   world 你 好") == "hello world 你好"
    assert windows_ocr._tidy("Lucky 77 好 玩") == "Lucky 77 好玩"


def test_merge_prefers_the_cjk_engine_for_cjk_and_english_engine_for_latin():
    def blk(text, y=0):
        return TextBlock(text, Rect(0, y, 100, 20), None, None, low_quality=True)

    pref = windows_ocr.WindowsOCREngine._preference
    cands = [
        (pref("zh", "你好"), "zh", blk("你好")),
        (pref("en", "ni hao"), "en", blk("ni hao")),
        (pref("zh", "SECOND 凵 N E"), "zh", blk("SECOND 凵 N E", 100)),
        (pref("en", "SECOND LINE"), "en", blk("SECOND LINE", 100)),
    ]
    merged = windows_ocr.WindowsOCREngine._merge(cands)
    assert [b.text for b in merged] == ["你好", "SECOND LINE"]


def test_max_side_len_reaches_the_preprocess_cap():
    def build(**extra):
        settings = {"languages": ["zh"], **extra}
        return default_registry.create("ocr", "windows", AdapterContext(settings=settings))

    assert build()._pre.max_side == 2000
    assert build(max_side_len=1500)._pre.max_side == 1500
