"""Korean/Japanese probe: how det limit_type and padding affect small crops (synthetic)."""
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
import numpy as np
from PIL import ImageOps
from ocr_synth import cases
from rapidocr import RapidOCR, OCRVersion as V, ModelType as M, LangRec as L
cs = cases()
KO = {"Rec.ocr_version": V.PPOCRV5, "Rec.model_type": M.MOBILE, "Rec.lang_type": L.KOREAN}
for name, p in {"v5mob_ko": KO, "v4mob_ko": {**KO, "Rec.ocr_version": V.PPOCRV4}}.items():
    for lim, pad in (("min", 0), ("max", 0), ("max", 40), ("min", 40)):
        e = RapidOCR(params={"Global.log_level": "warning", "Det.limit_type": lim, "Det.limit_side_len": 960 if lim == "max" else 736, **p})
        im = cs["ko_chat_16"][0]
        if pad: im = ImageOps.expand(im, border=pad, fill=im.getpixel((0, 0)))
        r = e(np.array(im)[:, :, ::-1]); print(name, lim, "pad", pad, r.txts, flush=True)
