"""Check which Windows.Media.Ocr languages exist, time en-US if present. No installs.

Run:
  uv run --no-project --python 3.12 --with pillow==13.0.0 --with winrt-runtime==4.0.3
    --with winrt-Windows.Media.Ocr==4!10.0.28000.2705
    --with winrt-Windows.Globalization==4!10.0.28000.2705
    --with winrt-Windows.Graphics.Imaging==4!10.0.28000.2705
    --with winrt-Windows.Storage.Streams==4!10.0.28000.2705
    --with winrt-Windows.Foundation==4!10.0.28000.2705
    --with winrt-Windows.Foundation.Collections==4!10.0.28000.2705
    python -I tests/bench/winocr_check.py
"""

import asyncio
import io
import statistics
import time
from pathlib import Path

from PIL import Image
from winrt.windows.globalization import Language
from winrt.windows.graphics.imaging import BitmapDecoder
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

IMG = Path(__file__).resolve().parents[2] / "tests/golden/images"


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


async def main():
    print("available:", [lang.language_tag for lang in OcrEngine.available_recognizer_languages])
    for tag in ["zh-Hans-CN", "zh-Hans", "zh-CN", "ja", "ko", "en-US"]:
        print(tag, "supported:", OcrEngine.is_language_supported(Language(tag)))
    eng = OcrEngine.try_create_from_language(Language("en-US"))
    ts = []
    for p in sorted(IMG.glob("*.png")):
        bmp = await load(p)
        await eng.recognize_async(bmp)
        r = []
        for _ in range(3):
            t = time.perf_counter()
            await eng.recognize_async(bmp)
            r.append((time.perf_counter() - t) * 1000)
        ts.append(statistics.median(r))
    print(
        "en-US median ms/img (timing only, accuracy meaningless on zh):",
        statistics.median(ts),
        "max",
        max(ts),
    )


asyncio.run(main())
