"""Throwaway capture benchmark. Usage: python -I capture_bench.py <backend> <mode>
backend: mss | dxgi_wc | wgc_wc | dxcam_dxgi | dxcam_winrt   (wc = windows-capture)
mode: full (monitor 1, 1920x1080) | region (800x600 at 100,100)
Prints one JSON line: ms per grab (median/p95), process CPU % at 4 fps for 10 s.
Not part of the product. Run from a venv containing mss, windows-capture, dxcam, numpy, psutil.
"""
import json
import statistics
import sys
import time

import numpy as np
import psutil

backend, mode = sys.argv[1], sys.argv[2]
REG = (100, 100, 900, 700)  # l,t,r,b
N = 40
FPS = 4
proc = psutil.Process()


def run_pull(grab):
    # warm up
    for _ in range(3):
        grab()
    lat = []
    proc.cpu_times()
    c0 = sum(proc.cpu_times()[:2])
    t0 = time.perf_counter()
    end = t0 + 10
    nxt = t0
    n = 0
    while time.perf_counter() < end:
        s = time.perf_counter()
        a = grab()
        lat.append((time.perf_counter() - s) * 1000)
        n += 1
        nxt += 1 / FPS
        d = nxt - time.perf_counter()
        if d > 0:
            time.sleep(d)
    cpu = (sum(proc.cpu_times()[:2]) - c0) / (time.perf_counter() - t0) * 100
    lat.sort()
    return {
        "shape": list(a.shape) if a is not None else None,
        "ms_median": round(statistics.median(lat), 2),
        "ms_p95": round(lat[int(len(lat) * 0.95) - 1], 2),
        "cpu_pct_of_one_core_at_4fps": round(cpu, 2),
        "n": n,
    }


if backend == "mss":
    import mss

    sct = mss.mss()
    mon = sct.monitors[1]
    box = (
        mon
        if mode == "full"
        else {"left": mon["left"] + REG[0], "top": mon["top"] + REG[1], "width": 800, "height": 600}
    )

    def grab():
        return np.asarray(sct.grab(box))

    out = run_pull(grab)

elif backend == "dxgi_wc":
    from windows_capture import DxgiDuplicationSession

    s = DxgiDuplicationSession(monitor_index=1)

    def grab():
        f = s.acquire_frame(timeout_ms=1000)
        if f is None:
            return grab.last  # static screen: no new frame (this is the cheap path)
        a = f.to_numpy()
        if mode == "region":
            a = a[REG[1] : REG[3], REG[0] : REG[2]]
        grab.last = a
        return a

    grab.last = None
    out = run_pull(grab)

elif backend.startswith("dxcam"):
    import dxcam

    be = "dxgi" if backend.endswith("dxgi") else "winrt"
    cam = dxcam.create(output_idx=0, backend=be)

    def grab():
        a = cam.grab(region=REG if mode == "region" else None)
        return a if a is not None else grab.last

    grab.last = None
    grab()
    out = run_pull(grab)

elif backend == "wgc_wc":
    from windows_capture import WindowsCapture

    import os
    mui = int(os.environ.get("MUI", "0")) or None
    cap = WindowsCapture(cursor_capture=False, draw_border=False, monitor_index=1, minimum_update_interval=mui)
    frames = []
    t_first = [None]

    @cap.event
    def on_frame_arrived(frame, ctl):
        frames.append(time.perf_counter())
        a = np.asarray(frame.frame_buffer)  # zero-copy view
        if mode == "region":
            a = a[REG[1] : REG[3], REG[0] : REG[2]].copy()
        else:
            a = a.copy()
        if len(frames) >= 10**6:
            ctl.stop()

    @cap.event
    def on_closed():
        pass

    c0 = sum(proc.cpu_times()[:2])
    t0 = time.perf_counter()
    ctl = cap.start_free_threaded()
    time.sleep(10)
    cpu = (sum(proc.cpu_times()[:2]) - c0) / (time.perf_counter() - t0) * 100
    ctl.stop()
    out = {
        "frames_delivered_in_10s": len(frames),
        "cpu_pct_of_one_core_(callback does copy)": round(cpu, 2),
    }
else:
    raise SystemExit("bad backend")

import os
out.update(backend=backend, mode=mode, min_update_interval=os.environ.get('MUI'))
print(json.dumps(out))
