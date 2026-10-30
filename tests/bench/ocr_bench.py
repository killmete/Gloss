"""Tiny OCR benchmark on tests/golden/images. Phase 1 research harness.

Run (no project file yet):
  uv run --no-project --python 3.12 --with rapidocr==3.10.0 --with onnxruntime==1.31.1 \
     --with psutil --with pillow python tests/bench/ocr_bench.py <variant>
variants: v6_small v6_medium v5_server
Raw text output goes to tests/golden/expected/_draft/ (gitignored, contains real chat text).
"""

import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import psutil
from PIL import Image
from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR

ROOT = Path(__file__).resolve().parents[2]
IMG = ROOT / "tests/golden/images"
OUT = ROOT / "tests/golden/expected/_draft"
RUNS = 3

VARIANTS = {
    "v6_small": (OCRVersion.PPOCRV6, ModelType.SMALL),
    "v6_medium": (OCRVersion.PPOCRV6, ModelType.MEDIUM),
    "v5_server": (OCRVersion.PPOCRV5, ModelType.SERVER),
}


def make(variant):
    v, m = VARIANTS[variant]
    return RapidOCR(
        params={
            "Det.ocr_version": v,
            "Det.model_type": m,
            "Rec.ocr_version": v,
            "Rec.model_type": m,
            "Rec.lang_type": LangRec.CH,
            "Det.limit_type": "max",
            "Det.limit_side_len": 960,
            "EngineConfig.onnxruntime.intra_op_num_threads": 2,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            "Global.text_score": 0.5,
            "Global.log_level": "warning",
        }
    )


def run(eng, arr):
    r = eng(arr)
    if r is None or r.txts is None:
        return []
    out = []
    for box, t, s in zip(r.boxes, r.txts, r.scores, strict=False):
        b = np.asarray(box)
        out.append(
            {
                "text": t,
                "score": float(s),
                "x": float(b[:, 0].min()),
                "y": float(b[:, 1].min()),
                "w": float(np.ptp(b[:, 0])),
                "h": float(np.ptp(b[:, 1])),
            }
        )
    return out


def timed(eng, arr):
    run(eng, arr)  # warm-up, excluded
    ts = []
    for _ in range(RUNS):
        t = time.perf_counter()
        res = run(eng, arr)
        ts.append((time.perf_counter() - t) * 1000)
    return res, statistics.median(ts)


def main(variant):
    proc = psutil.Process()
    eng = make(variant)
    run(eng, np.zeros((64, 64, 3), np.uint8))  # load models
    res_all = {}
    for i, p in enumerate(sorted(IMG.glob("*.png")), 1):
        im = Image.open(p).convert("RGB")
        w, h = im.size
        arr = np.asarray(im)[:, :, ::-1].copy()  # BGR
        lines, ms = timed(eng, arr)
        # 720p-equivalent: scale so long side = 1280
        s = min(1.0, 1280 / max(w, h))
        im7 = im.resize((round(w * s), round(h * s)), Image.LANCZOS)
        _, ms720 = timed(eng, np.asarray(im7)[:, :, ::-1].copy())
        res_all[f"img{i:02d}"] = {
            "file": p.name,
            "size": [w, h],
            "ms": ms,
            "ms_1280": ms720,
            "lines": lines,
        }
        print(
            variant,
            f"img{i:02d}",
            w,
            h,
            f"{ms:.0f}ms",
            f"{ms720:.0f}ms@1280",
            len(lines),
            flush=True,
        )
    peak = proc.memory_info().peak_wset / 2**20
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"_raw_{variant}.json").write_text(
        json.dumps(
            {"variant": variant, "peak_rss_mb": peak, "images": res_all},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print("peak MB", peak)


if __name__ == "__main__":
    main(sys.argv[1])
