"""Does ORT's session.set_denormal_as_zero speed up PP-OCRv6 (RapidOCR issue #743)? Synthetic 720p image."""
import sys, os, time, statistics; sys.path.insert(0, os.path.dirname(__file__))
import numpy as np, onnxruntime as ort
from ocr_synth import cases, composite
from rapidocr import RapidOCR, OCRVersion as V, ModelType as M
flag = sys.argv[1] == "on"
if flag:
    _orig = ort.InferenceSession.__init__
    def patched(self, path, sess_options=None, *a, **k):
        sess_options = sess_options or ort.SessionOptions()
        sess_options.add_session_config_entry("session.set_denormal_as_zero", "1")
        _orig(self, path, sess_options, *a, **k)
    ort.InferenceSession.__init__ = patched
img = np.array(composite(cases()))[:, :, ::-1]
for mt in (M.SMALL, M.MEDIUM):
    e = RapidOCR(params={"Global.log_level": "warning", "Det.model_type": mt, "Rec.model_type": mt, "Det.limit_type": "max", "Det.limit_side_len": 960})
    e(img); ts = []
    for _ in range(10):
        t = time.perf_counter(); r = e(img); ts.append((time.perf_counter() - t) * 1000)
    print("denormal_as_zero", flag, mt.value, "median ms", round(statistics.median(ts)), "lines", len(r.txts))
