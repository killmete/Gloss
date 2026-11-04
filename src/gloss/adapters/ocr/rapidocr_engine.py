"""RapidOCR (PP-OCR models on ONNX Runtime, CPU) as an ``OCREngine``.

Models come from disk, are checked against pinned SHA-256 hashes in ``rapidocr_models.json``
and are never downloaded. ``fast`` and ``accurate`` are config profiles that name a model
version and size, so a different model is a config change (plus a hash in the manifest).

Heavy libraries (rapidocr, onnxruntime, cv2) are imported inside ``open``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from gloss.core.contracts import (
    AdapterContext,
    AdapterUnavailable,
    Frame,
    GlossError,
    Health,
    PermanentError,
    Rect,
    TextBlock,
    TransientError,
)
from gloss.core.registry import register_adapter
from gloss.services.ocr_lang import classify_line
from gloss.services.ocr_preprocess import PreprocessConfig, prepare
from gloss.services.ocr_regions import centre_inside, dedupe_blocks, plan_regions, reading_order
from gloss.services.ocr_settings import read_bool, read_languages, read_number, read_str

log = logging.getLogger(__name__)

MANIFEST_PATH = Path(__file__).with_name("rapidocr_models.json")
REPO_MODELS = Path(__file__).resolve().parents[4] / "models" / "rapidocr"
# Gloss language code -> RapidOCR recogniser language. The v6 models read all of these with
# one shared recogniser, so asking for ja costs nothing next to zh.
RAPID_LANG = {"zh": "ch", "en": "en", "ja": "japan"}
KNOWN_LANGS = frozenset({"zh", "en", "ja", "ko", "th", "ru", "fr", "de", "es"})
TEXT_ROLES = ("det", "rec")


def _load_manifest() -> Mapping[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _search_dirs(configured: str) -> list[Path]:
    """Where to look for model files, most specific first. Nothing here is created."""
    dirs: list[Path] = []
    if configured:
        dirs.append(Path(configured))
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        dirs.append(Path(bundle) / "models" / "rapidocr")
    dirs.append(REPO_MODELS)
    spec = importlib.util.find_spec("rapidocr")  # finds the wheel's own folder without importing
    if spec is not None and spec.submodule_search_locations:
        dirs.extend(Path(p) / "models" for p in spec.submodule_search_locations)
    return dirs


@register_adapter("ocr", "rapidocr")
class RapidOCREngine:
    name = "rapidocr"
    REQUIRES_CONTRACT = ">=1.0,<2.0"
    ALLOW_AUTO_FALLBACK = True

    def __init__(self, context: AdapterContext) -> None:
        s = context.settings
        self.languages: tuple[str, ...] = read_languages(s)
        self._profile = s.get("profile", "fast")
        self._profiles = s.get("profiles")
        self._min_conf = read_number(s, "min_confidence", 0.5, minimum=0.0, maximum=1.0)
        self._limit_type = read_str(s, "det_limit_type", "max")
        self._limit_len = read_number(
            s, "det_limit_side_len", 960, minimum=32, maximum=4096, integer=True
        )
        self._intra = read_number(s, "intra_op_threads", 2, minimum=1, maximum=64, integer=True)
        self._inter = read_number(s, "inter_op_threads", 1, minimum=1, maximum=64, integer=True)
        self._max_side = read_number(
            s, "max_side_len", 2000, minimum=256, maximum=8192, integer=True
        )
        self._use_cls = read_bool(s, "use_cls", False)
        self._margin = read_number(s, "region_margin_px", 16, minimum=0, maximum=256, integer=True)
        self._model_dir = read_str(s, "model_dir", "")
        self._skip_langs = tuple(str(x) for x in s.get("skip_translation_langs", ()))
        self._pre = self._read_preprocess(s, self._max_side)
        self._engine: Any = None
        self._health = Health("down", "OCR engine is not open")
        self.last_ms = 0.0

    @staticmethod
    def _read_preprocess(s: Mapping[str, Any], max_side: int) -> PreprocessConfig:
        try:
            return PreprocessConfig(
                upscale=read_number(s, "upscale", 1.0, minimum=1.0, maximum=4.0),
                upscale_below_px=read_number(
                    s, "upscale_below_px", 0, minimum=0, maximum=4096, integer=True
                ),
                invert_dark=read_bool(s, "invert_dark", False),
                dark_luma=read_number(s, "dark_luma", 90.0, minimum=0.0, maximum=255.0),
                max_side=max_side,
            )
        except ValueError as exc:
            raise PermanentError(f"invalid preprocessing setting: {exc}") from exc

    # ---- lifecycle -----------------------------------------------------------------------

    def open(self) -> None:
        self.close()
        try:
            self._engine = self._build()
        except GlossError as exc:
            self._engine = None
            self._health = Health("down", str(exc))
            raise
        except Exception as exc:  # A model that will not load is unavailable, not a crash.
            self._engine = None
            self._health = Health("down", f"OCR engine failed to start ({type(exc).__name__})")
            raise AdapterUnavailable(
                f"The RapidOCR engine failed to start ({type(exc).__name__}). Run 'uv sync' to "
                "repair the install, then restart Gloss."
            ) from exc
        self._health = Health("ok", f"RapidOCR {self._profile} profile ready")

    def close(self) -> None:
        self._engine = None
        self._health = Health("down", "OCR engine is not open")

    def health(self) -> Health:
        return self._health

    # ---- model loading -------------------------------------------------------------------

    def _profile_choice(self) -> tuple[str, str]:
        profiles = self._profiles
        if not isinstance(self._profile, str) or not isinstance(profiles, Mapping):
            raise PermanentError(
                "ocr.profile must name a table in [ocr.adapters.rapidocr.profiles]"
            )
        choice = profiles.get(self._profile)
        if (
            not isinstance(choice, Mapping)
            or not isinstance(choice.get("ocr_version"), str)
            or not isinstance(choice.get("model_type"), str)
        ):
            known = ", ".join(sorted(map(str, profiles))) or "none"
            raise PermanentError(f"Unknown OCR profile '{self._profile}'. Known profiles: {known}")
        return choice["ocr_version"], choice["model_type"]

    def _model_file(self, filename: str, manifest: Mapping[str, Any]) -> Path:
        pinned = manifest["files"].get(filename)
        if pinned is None:
            raise PermanentError(
                f"Model file {filename} is not in rapidocr_models.json, so it cannot be "
                "trusted. Add its SHA-256 there after checking docs/UPDATING.md."
            )
        dirs = _search_dirs(self._model_dir)
        for folder in dirs:
            path = folder / filename
            if not path.is_file():
                continue
            actual = _sha256(path)
            if actual != pinned:
                raise AdapterUnavailable(
                    f"{path} has the wrong checksum ({actual[:12]}..., expected {pinned[:12]}...)."
                    " Delete it and put the original file back; Gloss never downloads models."
                )
            return path
        url = manifest.get("download", {}).get(filename, "")
        where = dirs[0] if self._model_dir else REPO_MODELS
        hint = f" It can be downloaded from {url}." if url else ""
        raise AdapterUnavailable(
            f"The OCR model file {filename} is missing. Put it in {where} (SHA-256 {pinned})."
            f"{hint} Gloss never downloads models by itself."
        )

    def _build(self) -> Any:
        version, model_type = self._profile_choice()
        for code in self.languages:
            if code not in RAPID_LANG:
                if code in KNOWN_LANGS:
                    raise PermanentError(
                        f"Language '{code}' is not supported by the {self._profile} OCR profile"
                    )
                raise PermanentError(f"'{code}' is not a language code this OCR engine can read")
        manifest = _load_manifest()
        files = {
            role: self._model_file(f"{version}_{role}_{model_type}.onnx", manifest)
            for role in TEXT_ROLES
        }
        cls_file = None
        if self._use_cls:
            cls_name = manifest.get("cls_file", "ch_ppocr_mobile_v2.0_cls_mobile.onnx")
            cls_file = self._model_file(cls_name, manifest)
        try:
            from rapidocr import ModelType, OCRVersion, RapidOCR
        except ImportError as exc:
            raise AdapterUnavailable(
                "RapidOCR or onnxruntime is not installed. Run 'uv sync' in the Gloss folder."
            ) from exc
        try:
            ocr_version, size = OCRVersion(version), ModelType(model_type)
        except ValueError as exc:
            raise PermanentError(
                f"Unknown OCR model version or size: {version} {model_type}"
            ) from exc
        supported = set(RapidOCR.list_supported_langs("rec", ocr_version, size))
        for code in self.languages:
            if RAPID_LANG[code] not in supported:
                raise PermanentError(
                    f"Language '{code}' is not supported by {version} {model_type}"
                )
        params: dict[str, Any] = {
            "Det.ocr_version": ocr_version,
            "Det.model_type": size,
            "Det.model_path": str(files["det"]),
            "Det.limit_type": self._limit_type,
            "Det.limit_side_len": self._limit_len,
            "Rec.ocr_version": ocr_version,
            "Rec.model_type": size,
            "Rec.model_path": str(files["rec"]),
            "Rec.lang_type": RAPID_LANG[self.languages[0]],
            "EngineConfig.onnxruntime.intra_op_num_threads": self._intra,
            "EngineConfig.onnxruntime.inter_op_num_threads": self._inter,
            "Global.text_score": self._min_conf,
            "Global.max_side_len": self._max_side,
            "Global.use_cls": self._use_cls,
            "Global.log_level": "error",
        }
        if cls_file is not None:
            params["Cls.model_path"] = str(cls_file)
        try:
            engine = RapidOCR(params=params)
        except ValueError as exc:
            raise PermanentError(f"RapidOCR refused the OCR settings: {exc}") from exc
        # RapidOCR loads models on first use. Load them now, so open() reports a broken
        # model and the first real frame is not slow.
        loaders = ["_load_det_model", "_load_rec_model"]
        if self._use_cls:
            loaders.append("_load_cls_model")
        for loader in loaders:
            getattr(engine, loader)()
        return engine

    # ---- reading -------------------------------------------------------------------------

    def recognize(self, frame: Frame, regions: Sequence[Rect] | None) -> Sequence[TextBlock]:
        engine = self._engine
        if engine is None:
            raise AdapterUnavailable("The OCR engine is not open. Call open() first.")
        height, width = frame.image.shape[:2]
        started = time.perf_counter()
        blocks: list[TextBlock] = []
        try:
            for crop, keep in plan_regions(regions, width, height, self._margin):
                blocks.extend(self._read_crop(engine, frame.image, crop, keep, width, height))
        except GlossError:
            raise
        except Exception as exc:
            self._health = Health("degraded", f"The last OCR call failed ({type(exc).__name__})")
            raise TransientError(f"OCR call failed ({type(exc).__name__})") from exc
        self.last_ms = (time.perf_counter() - started) * 1000
        self._health = Health("ok", f"RapidOCR {self._profile} profile ready")
        return reading_order(dedupe_blocks(blocks))

    def _read_crop(
        self, engine: Any, image: np.ndarray, crop: Rect, keep: Rect, width: int, height: int
    ) -> list[TextBlock]:
        pixels = image[crop.y : crop.bottom, crop.x : crop.right]
        bgr, scale = prepare(pixels, self._pre)
        result = engine(bgr)
        if result is None or result.txts is None or result.boxes is None:
            return []
        out: list[TextBlock] = []
        for quad, text, score in zip(result.boxes, result.txts, result.scores, strict=False):
            text = " ".join(str(text).split())
            if not text:
                continue
            pts = np.asarray(quad, dtype=float) / scale
            left = max(int(np.floor(pts[:, 0].min())) + crop.x, 0)
            top = max(int(np.floor(pts[:, 1].min())) + crop.y, 0)
            right = min(int(np.ceil(pts[:, 0].max())) + crop.x, width)
            bottom = min(int(np.ceil(pts[:, 1].max())) + crop.y, height)
            if right <= left or bottom <= top:
                continue
            box = Rect(left, top, right - left, bottom - top)
            if not centre_inside(box, keep):
                continue
            lang, needs = classify_line(text, self._skip_langs)
            out.append(
                TextBlock(
                    text=text,
                    bbox=box,
                    confidence=min(max(float(score), 0.0), 1.0),
                    lang=lang,
                    needs_translation=needs,
                )
            )
        return out
