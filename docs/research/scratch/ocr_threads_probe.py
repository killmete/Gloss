"""ms and CPU-seconds per call vs intra-op threads (synthetic 720p, 12 text lines). Approximates weaker CPUs."""
import sys, os, time, statistics; sys.path.insert(0, os.path.dirname(__file__))
import numpy as np
from ocr_synth import cases, composite
from rapidocr import RapidOCR, ModelType as M
img = np.array(composite(cases()))[:, :, ::-1]
for mt in (M.TINY, M.SMALL):
    for th in (1, 2, 4, 8):
        e = RapidOCR(params={"Global.log_level": "warning", "Det.model_type": mt, "Rec.model_type": mt, "Det.limit_type": "max", "Det.limit_side_len": 960,
                             "EngineConfig.onnxruntime.intra_op_num_threads": th, "EngineConfig.onnxruntime.inter_op_num_threads": 1})
        e(img); ts = []; c0 = time.process_time(); w0 = time.perf_counter()
        for _ in range(8):
            t = time.perf_counter(); e(img); ts.append((time.perf_counter() - t) * 1000)
        cpu = (time.process_time() - c0) / 8 * 1000
        print(f"{mt.value:5s} threads={th} median_ms={statistics.median(ts):.0f} cpu_ms_per_call={cpu:.0f}")
