"""Does WDA_EXCLUDEFROMCAPTURE hide a window from each capture backend?
Opens a solid-red topmost Tk window at (300,300) 400x300 on monitor 1 (primary), grabs that
area with each backend with affinity NONE and then EXCLUDEFROMCAPTURE, reports % red pixels.
Throwaway research script. Run: python -I wda_test.py
"""
import ctypes
import json
import time
import tkinter as tk

import numpy as np

ctypes.windll.shcore.SetProcessDpiAwareness(2)
u32 = ctypes.windll.user32
u32.GetParent.restype = ctypes.c_void_p
u32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint]

root = tk.Tk()
root.overrideredirect(True)
root.geometry("400x300+300+300")
root.configure(bg="#ff0000")
root.attributes("-topmost", True)
for _ in range(20):
    root.update()
    time.sleep(0.02)
hwnd = u32.GetParent(root.winfo_id())
print("hwnd", hwnd)

X, Y, W, H = 300, 300, 400, 300


def red_frac(a):  # a: HxWx(3|4), channel order unknown -> test both R and B
    a = np.asarray(a)[..., :3].astype(int)
    c = a[H // 4 : 3 * H // 4, W // 4 : 3 * W // 4].reshape(-1, 3)
    r = ((c[:, 0] > 200) & (c[:, 1] < 60) & (c[:, 2] < 60)).mean()  # R,G,B
    b = ((c[:, 2] > 200) & (c[:, 1] < 60) & (c[:, 0] < 60)).mean()  # B,G,R
    return round(float(max(r, b)), 3)


def pump(n=15):
    for _ in range(n):
        root.update()
        time.sleep(0.02)


def g_mss():
    import mss

    with mss.MSS() as s:
        return s.grab({"left": X, "top": Y, "width": W, "height": H})


def g_dxgi():
    last = None
    from windows_capture import DxgiDuplicationSession

    s = DxgiDuplicationSession(monitor_index=2)
    for _ in range(30):
        f = s.acquire_frame(timeout_ms=200)
        if f is not None:
            a = f.to_numpy()
            print("dxgi frame", a.shape, "mean", a.mean().round(1), file=__import__("sys").stderr)
            last = a[Y : Y + H, X : X + W]
            if red_frac(last) > 0.5:
                return last
    return last  # NOTE: first frames can be stale/black; take any frame that shows the window


def g_dxcam():
    import dxcam

    global _cam
    if "_cam" not in globals():
        _cam = dxcam.create(output_idx=0, backend="dxgi")
    cam = _cam
    for _ in range(30):
        a = cam.grab(region=(X, Y, X + W, Y + H))
        if a is not None:
            return a
        time.sleep(0.05)
    return None


def g_wgc():
    from windows_capture import WindowsCapture

    got = []
    cap = WindowsCapture(cursor_capture=False, draw_border=False, monitor_index=2)

    @cap.event
    def on_frame_arrived(frame, ctl):
        if not got:
            got.append(np.asarray(frame.frame_buffer)[Y : Y + H, X : X + W].copy())

    @cap.event
    def on_closed():
        pass

    ctl = cap.start_free_threaded()
    t = time.time()
    while not got and time.time() - t < 3:
        pump(2)
    ctl.stop()
    return got[0] if got else None


res = {}
for aff_name, aff in (("NONE", 0), ("EXCLUDEFROMCAPTURE", 0x11)):
    ok = u32.SetWindowDisplayAffinity(hwnd, aff)
    pump()
    import sys
    for name, fn in [x for x in (("mss_gdi", g_mss), ("dxgi_duplication", g_dxgi), ("dxcam_dxgi", g_dxcam), ("wgc", g_wgc)) if x[0] == sys.argv[1]]:
        try:
            a = fn()
            res[f"{aff_name}/{name}"] = None if a is None else red_frac(a)
        except Exception as e:  # noqa: BLE001
            res[f"{aff_name}/{name}"] = f"ERR {type(e).__name__}: {e}"
    res[f"{aff_name}/set_ok"] = bool(ok)
root.destroy()
print(json.dumps(res, indent=1))
