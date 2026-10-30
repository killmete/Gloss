"""Windows.Media.Ocr probe via winrt projection: lists languages, times an English line (only en-US installed on this PC)."""
import asyncio, time, io, statistics
from PIL import Image, ImageDraw, ImageFont
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.globalization import Language
from winrt.windows.graphics.imaging import BitmapDecoder
from winrt.windows.storage.streams import InMemoryRandomAccessStream, DataWriter

print("languages:", [l.language_tag for l in OcrEngine.available_recognizer_languages])
for tag in ("zh-Hans-CN", "ja", "ko", "en-US"):
    print(tag, "supported:", OcrEngine.is_language_supported(Language(tag)))

async def sb(img):
    b = io.BytesIO(); img.save(b, "PNG"); st = InMemoryRandomAccessStream(); w = DataWriter(st); w.write_bytes(b.getvalue()); await w.store_async(); await w.flush_async(); st.seek(0)
    return await (await BitmapDecoder.create_async(st)).get_software_bitmap_async()

async def main():
    eng = OcrEngine.try_create_from_language(Language("en-US"))
    img = Image.new("RGB", (1280, 720), (30, 30, 40)); d = ImageDraw.Draw(img)
    f = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 18)
    for i in range(15): d.text((40, 30 + i * 40), f"Lucky_77: welcome to the live room number {i} please follow", font=f, fill=(240, 240, 240))
    bmp = await sb(img); await eng.recognize_async(bmp)
    ts = []
    for _ in range(10):
        t = time.perf_counter(); r = await eng.recognize_async(bmp); ts.append((time.perf_counter() - t) * 1000)
    print("en-US 1280x720, 15 lines: median ms", round(statistics.median(ts)), "lines", len(r.lines))
asyncio.run(main())
