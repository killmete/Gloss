"""RapidOCR adapter details beyond the shared contract: pinned models, profiles, languages."""

from __future__ import annotations

import json
import shutil
import sys

import pytest
from testkit.discovery import NO_USER_CONFIG, real_subject
from testkit.frames import text_frame

from gloss.adapters.ocr import rapidocr_engine
from gloss.core.config import load_config
from gloss.core.contracts import AdapterContext, AdapterUnavailable, PermanentError, Rect
from gloss.core.registry import default_registry, discover

discover()


@pytest.fixture(scope="module")
def settings():
    return load_config(user_path=NO_USER_CONFIG).config.adapter_settings("ocr", "rapidocr")


def make(settings, **override):
    return default_registry.create(
        "ocr", "rapidocr", AdapterContext(settings={**settings, **override})
    )


def test_registered_with_the_contract_declaration():
    assert "rapidocr" in default_registry.names("ocr")
    cls = rapidocr_engine.RapidOCREngine
    assert cls.ALLOW_AUTO_FALLBACK is True and cls.REQUIRES_CONTRACT == ">=1.0,<2.0"


def test_construction_does_not_import_the_heavy_libraries(settings):
    for heavy in ("rapidocr", "onnxruntime"):
        sys.modules.pop(heavy, None)
    make(settings)
    assert "rapidocr" not in sys.modules


def test_defaults_come_from_config(settings):
    engine = make(settings)
    assert engine._limit_type == "max" and engine._limit_len == 960
    assert (engine._intra, engine._inter) == (2, 1)
    assert engine.languages == ("zh", "en", "ja")


def test_every_model_in_the_manifest_has_a_sha256():
    manifest = json.loads(rapidocr_engine.MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["files"]
    for name, digest in manifest["files"].items():
        assert name.endswith(".onnx") and len(digest) == 64 and int(digest, 16) >= 0


def test_the_small_model_on_disk_matches_its_pinned_hash(settings):
    engine = make(settings)
    manifest = rapidocr_engine._load_manifest()
    for role in ("det", "rec"):
        name = f"PP-OCRv6_{role}_small.onnx"
        path = engine._model_file(name, manifest)
        assert rapidocr_engine._sha256(path) == manifest["files"][name]


def test_a_model_with_the_wrong_hash_is_refused(settings, tmp_path):
    (tmp_path / "PP-OCRv6_det_small.onnx").write_bytes(b"not a model")
    engine = make(settings, model_dir=str(tmp_path))
    with pytest.raises(AdapterUnavailable, match="wrong checksum"):
        engine.open()
    assert engine.health().state == "down"


def test_a_bad_file_in_the_configured_folder_does_not_fall_through(settings, tmp_path):
    (tmp_path / "PP-OCRv6_rec_small.onnx").write_bytes(b"x" * 10)
    with pytest.raises(AdapterUnavailable):
        make(settings, model_dir=str(tmp_path)).open()


def test_good_models_in_the_configured_folder_are_used(settings, tmp_path):
    manifest = rapidocr_engine._load_manifest()
    source = make(settings)
    for role in ("det", "rec"):
        name = f"PP-OCRv6_{role}_small.onnx"
        shutil.copy(source._model_file(name, manifest), tmp_path / name)
    engine = make(settings, model_dir=str(tmp_path))
    engine.open()
    try:
        assert engine.health().state == "ok"
        assert engine.recognize(text_frame(), [Rect(0, 0, 800, 400)])
    finally:
        engine.close()


def test_missing_accurate_model_names_the_file_folder_and_hash(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(rapidocr_engine, "REPO_MODELS", tmp_path / "models")
    monkeypatch.setattr(rapidocr_engine, "_search_dirs", lambda configured: [tmp_path / "models"])
    engine = make(settings, profile="accurate")
    with pytest.raises(AdapterUnavailable) as caught:
        engine.open()
    message = str(caught.value)
    assert "PP-OCRv6_det_medium.onnx" in message and str(tmp_path / "models") in message
    assert "92078b73" in message and "never downloads" in message


def test_unknown_profile_is_a_permanent_error(settings):
    with pytest.raises(PermanentError, match="Unknown OCR profile"):
        make(settings, profile="turbo").open()
    with pytest.raises(PermanentError):
        make(settings, profile=7).open()


@pytest.mark.parametrize("code", ["zz", "xx", "ko"])
def test_unreadable_languages_are_permanent_errors(settings, code):
    with pytest.raises(PermanentError):
        make(settings, languages=[code]).open()


def test_one_unsupported_language_refuses_the_whole_request(settings):
    with pytest.raises(PermanentError):
        make(settings, languages=["zh", "ko"]).open()


def test_malformed_numbers_are_permanent_errors(settings):
    for key, value in (("det_limit_side_len", "big"), ("intra_op_threads", 0), ("upscale", 9)):
        with pytest.raises(PermanentError):
            make(settings, **{key: value})


def test_recognize_before_open_is_unavailable(settings):
    with pytest.raises(AdapterUnavailable):
        make(settings).recognize(text_frame(), None)


def test_regions_outside_the_frame_and_overlapping_regions():
    subject = real_subject("ocr", "rapidocr")
    try:
        engine = subject.opened()
        frame = text_frame()
        assert engine.recognize(frame, [Rect(5000, 5000, 100, 100)]) == []
        twice = engine.recognize(frame, [Rect(0, 0, 800, 220), Rect(0, 100, 800, 300)])
        assert len([b for b in twice if "HELLO" in b.text.upper()]) == 1  # overlap read once
    finally:
        subject.release()


def test_blocks_carry_language_and_confidence(settings):
    engine = make(settings)
    engine.open()
    try:
        blocks = engine.recognize(text_frame(), None)
        assert blocks and all(b.lang == "en" for b in blocks)
        assert all(b.confidence is not None and 0 <= b.confidence <= 1 for b in blocks)
        assert all(not b.low_quality for b in blocks)
    finally:
        engine.close()


def test_open_close_open_cycles_release_the_engine(settings):
    engine = make(settings)
    for _ in range(2):
        engine.open()
        assert engine.health().state == "ok"
        engine.close()
        assert engine.health().state == "down" and engine._engine is None


def test_max_side_len_reaches_the_preprocess_cap(settings):
    assert make(settings)._pre.max_side == 2000
    assert make(settings, max_side_len=1500)._pre.max_side == 1500
