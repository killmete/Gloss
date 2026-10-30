"""RapidOCR variant bench on synthetic images. usage: python ocr_bench_rapid.py"""
import sys, time, json, unicodedata, re, os
sys.path.insert(0, os.path.dirname(__file__))
import numpy as np, psutil
from ocr_synth import cases, composite
from rapidocr import RapidOCR

def norm(s): return re.sub(r"[\s\W_]+", "", unicodedata.normalize("NFKC", s))
def lev(a, b):
    p = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        c = [i]
        for j, cb in enumerate(b, 1):
            c.append(min(p[j] + 1, c[j - 1] + 1, p[j - 1] + (ca != cb)))
        p = c
    return p[-1]

from PIL import ImageFilter
import io
from PIL import Image
def degrade(im):
    im = im.filter(ImageFilter.GaussianBlur(0.6)); im = im.resize((int(im.width*0.8), int(im.height*0.8)), Image.BILINEAR)
    b = io.BytesIO(); im.save(b, "JPEG", quality=35); return Image.open(io.BytesIO(b.getvalue())).convert("RGB")
from rapidocr import OCRVersion as V, ModelType as M, LangRec as L, LangDet as LD
LOG = {"Global.log_level": "warning"}
LM = {"Det.limit_type": "max", "Det.limit_side_len": 960}   # see report: default limit_type=min upscales small crops
def v(ver, mt, lang=None, det_lang=None):
    d = {"Det.ocr_version": ver, "Det.model_type": mt, "Rec.ocr_version": ver, "Rec.model_type": mt, **LM}
    if lang: d["Rec.lang_type"] = lang
    return d
VARIANTS = {
  "v6_small_ch_default_limitmin": {},
  "v6_small_ch": dict(LM),
  "v6_tiny_ch": v(V.PPOCRV6, M.TINY),
  "v6_medium_ch": v(V.PPOCRV6, M.MEDIUM),
  "v5_mobile_ch": v(V.PPOCRV5, M.MOBILE),
  "v5_server_ch": v(V.PPOCRV5, M.SERVER),
  "v5_mobile_korean": v(V.PPOCRV5, M.MOBILE, L.KOREAN),
  "v4_mobile_korean": v(V.PPOCRV4, M.MOBILE, L.KOREAN),
}
cs = cases(); comp = np.array(composite(cs))[:, :, ::-1]
only = sys.argv[1:] 
out = {}
for vn, params in VARIANTS.items():
    if only and not any(o in vn for o in only): continue
    try:
        t0 = time.perf_counter(); eng = RapidOCR(params={**LOG, **params}); load = time.perf_counter() - t0
        eng(comp)  # warm-up
        res = {}
        for n, (im, exp) in cs.items():
            e = norm(exp)
            for tag, img in (("", im), ("_deg", degrade(im))):
                r = eng(np.array(img)[:, :, ::-1]); got = "".join(r.txts) if r.txts else ""
                res[n + tag] = round(lev(e, norm(got)) / max(len(e), 1), 3)
        ts = []
        for _ in range(8):
            t = time.perf_counter(); eng(comp); ts.append((time.perf_counter() - t) * 1000)
        ts_s = []
        s = np.array(cs["zh_chat_bubble_16"][0])[:, :, ::-1]
        for _ in range(15):
            t = time.perf_counter(); eng(s); ts_s.append((time.perf_counter() - t) * 1000)
        zh = [v for k, v in res.items() if k.startswith("zh") and not k.endswith("_deg")]
        zhd = [v for k, v in res.items() if k.startswith("zh") and k.endswith("_deg")]
        out[vn] = dict(load_s=round(load, 2), cer=res, cer_zh_clean_mean=round(sum(zh) / len(zh), 3), cer_zh_degraded_mean=round(sum(zhd) / len(zhd), 3),
                       full720p_ms_median=round(float(np.median(ts))), one_line_ms_median=round(float(np.median(ts_s))),
                       rss_mb=round(psutil.Process().memory_info().rss / 2**20))
    except Exception as ex:
        out[vn] = {"error": repr(ex)[:300]}
    print(vn, json.dumps(out[vn], ensure_ascii=False), flush=True)

json.dump(out, open(os.path.join(os.path.dirname(__file__), "..", "data", "ocr_rapid_synth_%s.json" % ("_".join(only) or "all")), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
