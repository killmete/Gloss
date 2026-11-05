"""Failure drills (SPEC sections 5 and 8): no network, an invalid key, a missing model file and
a hung stage.

Each drill runs the real Pipeline and the real translation service (cache, breaker, fallback
chain, real Anthropic adapter with a fake SDK client, real null translator). Fakes sit only at
the edges: the network, the screen, the OCR engine and the key store.

For each: the app does not crash, the status shows the right state in plain English, the
fallback is what config says (``null`` shows the original text), and when the problem goes
away the app recovers.
"""

from __future__ import annotations

import importlib.util
import shutil
import threading
import time
from pathlib import Path

import pytest
from testkit.drill import LINE1, LINE2, NEW, NEWER, SECRET, Drill
from testkit.frames import text_frame
from testkit.pipeline import wait_until

from gloss.adapters.ocr import rapidocr_engine
from gloss.adapters.ocr.rapidocr_engine import RapidOCREngine
from gloss.core.contracts import AdapterContext

pytestmark = pytest.mark.drill

TECHNICAL = ("Traceback", "Exception", "Error", "errno", "0x", "None")


@pytest.fixture
def make_drill(tmp_path, monkeypatch):
    made: list[Drill] = []
    monkeypatch.setattr("gloss.core.pipeline._IDLE_WAIT_S", 0.05)  # quick teardown

    def make(**kw) -> Drill:
        d = Drill(tmp_path, monkeypatch, **kw)
        made.append(d)
        return d

    yield make
    for d in made:
        d.close()


def plain(detail: str) -> None:
    """A message for the user: words, not a stack trace, a key or a bare type name."""
    assert detail.strip(), "the status gave no text"
    assert not any(word in detail for word in TECHNICAL), detail
    assert SECRET not in detail


def originals_shown(d: Drill, timeout: float = 8.0) -> None:
    for text in (LINE1, LINE2):
        item = d.wait_shown("passthrough", text, timeout)
        assert item.result.text == text, "the null fallback must show the original text"


# ---- 1. no network --------------------------------------------------------------------------


def test_drill_no_network_shows_originals_says_so_and_recovers(make_drill):
    d = make_drill()
    d.net.go("offline")
    d.start()
    originals_shown(d)
    detail = d.wait_state("degraded")
    plain(detail)
    assert "reach" in detail and "internet" in detail.lower()
    assert d.alive()
    wait_until(lambda: d.dot() == "degraded", what="the status dot")

    d.net.go("ok")  # the connection is back; the 0.5 s retry pause ends by itself
    time.sleep(0.7)
    d.new_line(NEW, y=90)
    assert d.wait_shown("ok", NEW).result.text == f"EN:{NEW}"
    d.wait_state("ok")
    assert d.alive()


# ---- 2. invalid key -------------------------------------------------------------------------


def test_drill_invalid_key_asks_for_a_new_key_and_recovers_when_it_is_fixed(make_drill):
    d = make_drill()
    d.net.go("bad_key")
    d.start()
    originals_shown(d)
    detail = d.wait_state("degraded")
    plain(detail)
    assert "key" in detail.lower() and "rejected" in detail.lower()
    assert d.alive()
    calls = d.net.calls
    d.new_line(NEW, y=90)
    d.wait_shown("passthrough", NEW)
    assert d.net.calls == calls, "a rejected key must not be hammered with more calls"

    d.net.go("ok")  # the user enters a good key; the settings window asks for a retry
    d.keys.set("anthropic_api_key", "sk-good-key-0123456789")
    d.pipeline.retry_translation()
    wait_until(lambda: not d.pipeline._retry_translation, what="the translate worker to see it")
    d.new_line(NEWER, y=120)
    assert d.wait_shown("ok", NEWER).result.text == f"EN:{NEWER}"
    d.wait_state("ok")


def test_drill_invalid_key_the_first_line_after_the_fix_is_translated(make_drill, monkeypatch):
    monkeypatch.setattr("gloss.core.pipeline._IDLE_WAIT_S", 0.8)  # make the race certain
    d = make_drill()
    d.net.go("bad_key")
    d.start()
    originals_shown(d)
    d.wait_state("degraded")
    d.net.go("ok")
    d.pipeline.retry_translation()
    d.new_line(NEW, y=90)
    assert d.wait_shown("ok", NEW, timeout=1.5).result.text == f"EN:{NEW}"


# ---- 3. missing model file ------------------------------------------------------------------


class EmptyFolderOCR(RapidOCREngine):
    """The real RapidOCR adapter, pointed at a folder that holds no model files."""

    folder: Path | None = None

    def __init__(self, context: AdapterContext) -> None:
        settings = {**context.settings, "model_dir": str(type(self).folder)}
        super().__init__(AdapterContext(settings, context.secrets))


def _real_model(name: str) -> Path:
    dirs = [rapidocr_engine.REPO_MODELS]
    spec = importlib.util.find_spec("rapidocr")
    if spec is not None and spec.submodule_search_locations:
        dirs.extend(Path(p) / "models" for p in spec.submodule_search_locations)
    found = next((p / name for p in dirs if (p / name).is_file()), None)
    if found is None:
        pytest.skip("the real OCR model files are not on this machine")
    return found


def test_drill_missing_model_file_goes_red_with_the_fix_and_recovers(
    make_drill, tmp_path, monkeypatch
):
    sources = [_real_model(f"PP-OCRv6_{role}_small.onnx") for role in ("det", "rec")]
    folder = tmp_path / "models_here"
    folder.mkdir()
    # Only the configured folder is searched, so the repo's own models cannot hide the fault.
    monkeypatch.setattr(rapidocr_engine, "_search_dirs", lambda configured: [folder])
    EmptyFolderOCR.folder = folder
    d = make_drill(ocr_class=EmptyFolderOCR, ocr_name="rapidocr", image=text_frame().image)
    d.start()
    detail = d.wait_state("down")
    plain(detail)
    assert "model file" in detail and "missing" in detail and str(folder) in detail
    assert "never downloads" in detail  # the exact fix, in words
    assert d.alive()
    wait_until(lambda: d.dot() == "down", what="the red status dot")
    assert not d.sink.snapshot(), "nothing can be shown without OCR, and nothing crashed"

    for source in sources:  # the user puts the model files back
        shutil.copy(source, folder / source.name)
    d.wait_state("ok", timeout=20)
    assert d.alive()


# ---- 4. a hung stage ------------------------------------------------------------------------


def test_drill_hung_ocr_is_restarted_and_the_app_keeps_going(make_drill):
    d = make_drill()
    hang, gate = threading.Event(), threading.Event()
    d.ocr.waits[:] = [hang, gate]  # the first read never returns; the retry waits for the gate
    d.start()
    detail = d.wait_state("degraded")
    plain(detail)
    assert "ocr" in detail.lower() and "restarted" in detail.lower()
    assert d.alive()
    gate.set()  # the restarted worker gets on with it and re-reads the lost frame
    assert d.wait_shown("ok", LINE1).result.text == f"EN:{LINE1}"
    d.wait_state("ok")
    assert d.alive()
    hang.set()


def test_drill_hung_translation_is_restarted_and_the_app_recovers(make_drill):
    d = make_drill()
    d.net.go("hang")
    d.start()
    detail = d.wait_state("degraded")
    plain(detail)
    assert "translat" in detail.lower() and "restarted" in detail.lower()
    d.wait_shown("failed", LINE1)  # the lost batch is marked, not silently dropped
    assert d.alive()
    d.net.go("ok")
    d.net.gate.set()
    d.new_line(NEW, y=90)
    assert d.wait_shown("ok", NEW).result.text == f"EN:{NEW}"
    d.wait_state("ok")
    assert d.alive()


def test_drill_translator_that_stalls_while_starting_still_shows_the_original_text(make_drill):
    d = make_drill()
    d.net.go("hang_open")
    d.start()
    d.wait_state("degraded")
    originals_shown(d, timeout=1.5)
