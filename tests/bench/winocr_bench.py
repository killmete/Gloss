"""Windows.Media.Ocr (zh-Hans-CN) on tests/golden/images, same output format as ocr_bench.py.

Run (same pins as winocr_check.py):
  uv run --no-project --python 3.12 --with pillow==13.0.0 --with winrt-runtime==4.0.3 \
    --with winrt-Windows.Media.Ocr==4!10.0.28000.2705 \
    --with winrt-Windows.Globalization==4!10.0.28000.2705 \
    --with winrt-Windows.Graphics.Imaging==4!10.0.28000.2705 \
    --with winrt-Windows.Storage.Streams==4!10.0.28000.2705 \
    --with winrt-Windows.Foundation==4!10.0.28000.2705 \
    --with winrt-Windows.Foundation.Collections==4!10.0.28000.2705 \
    python -I tests/bench/winocr_bench.py
Raw text goes to tests/golden/expected/_draft/_raw_winocr_zh.json (gitignored).
Score with ocr_score.py.
"""

import asyncio
import io
import json
import statistics
import time
from pathlib import Path

from PIL import Image
from winrt.windows.globalization import Language
from winrt.windows.graphics.imaging import BitmapDecoder
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

ROOT = Path(__file__).resolve().parents[2]
IMG = ROOT / "tests/golden/images"
OUT = ROOT / "tests/golden/expected/_draft"
RUNS = 3


async def load(p):
    b = io.BytesIO()
    Image.open(p).convert("RGB").save(b, "PNG")
    s = InMemoryRandomAccessStream()
    w = DataWriter(s)
    w.write_bytes(b.getvalue())
    await w.store_async()
    s.seek(0)
    dec = await BitmapDecoder.create_async(s)
    return await dec.get_software_bitmap_async()


def to_lines(res):
    out = []
    for ln in res.lines:
        rects = [w.bounding_rect for w in ln.words]
        if not rects:
            continue
        x = min(r.x for r in rects)
        y = min(r.y for r in rects)
        out.append(
            {
                "text": ln.text,
                "score": 1.0,  # the API gives no confidence
                "x": x,
                "y": y,
                "w": max(r.x + r.width for r in rects) - x,
                "h": max(r.y + r.height for r in rects) - y,
            }
        )
    return out


async def main():
    eng = OcrEngine.try_create_from_language(Language("zh-Hans-CN"))
    print("engine:", eng.recognizer_language.language_tag)
    print("max image dimension:", OcrEngine.max_image_dimension)
    res_all = {}
    for i, p in enumerate(sorted(IMG.glob("*.png")), 1):
        bmp = await load(p)
        w, h = bmp.pixel_width, bmp.pixel_height
        too_big = max(w, h) > OcrEngine.max_image_dimension
        if too_big:
            res_all[f"img{i:02d}"] = {
                "file": p.name,
                "size": [w, h],
                "ms": 0,
                "lines": [],
                "skipped": "exceeds max_image_dimension",
            }
            print(f"img{i:02d}", w, h, "SKIPPED too large")
            continue
        res = await eng.recognize_async(bmp)  # warm-up
        ts = []
        for _ in range(RUNS):
            t = time.perf_counter()
            res = await eng.recognize_async(bmp)
            ts.append((time.perf_counter() - t) * 1000)
        lines = to_lines(res)
        res_all[f"img{i:02d}"] = {
            "file": p.name,
            "size": [w, h],
            "ms": statistics.median(ts),
            "lines": lines,
        }
        print(f"img{i:02d}", w, h, f"{statistics.median(ts):.0f}ms", len(lines))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "_raw_winocr_zh.json").write_text(
        json.dumps(
            {"variant": "winocr_zh", "peak_rss_mb": 0, "images": res_all},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )


asyncio.run(main())
