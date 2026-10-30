"""WGC per-window capture via windows-capture window_hwnd. Throwaway. Run: python -I wgc_window_test.py
Red Tk window; capture it by HWND; then cover it with another topmost window and move it; report frames.
"""
import ctypes
import json
import time
import tkinter as tk

import numpy as np
from windows_capture import WindowsCapture

ctypes.windll.shcore.SetProcessDpiAwareness(2)
u32 = ctypes.windll.user32
u32.GetParent.restype = ctypes.c_void_p

root = tk.Tk()
root.overrideredirect(True)
root.geometry("400x300+300+300")
root.configure(bg="#ff0000")
cover = tk.Toplevel(root)
cover.overrideredirect(True)
cover.geometry("600x500+200+200")
cover.configure(bg="#00ff00")
cover.attributes("-topmost", False)
for _ in range(20):
    root.update()
    time.sleep(0.02)
hwnd = u32.GetParent(root.winfo_id())
chwnd = u32.GetParent(cover.winfo_id())
u32.SetWindowPos(ctypes.c_void_p(chwnd), ctypes.c_void_p(0), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # HWND_TOP, topmost not needed
cover.lift()
for _ in range(20):
    root.update()
    time.sleep(0.02)

log = []
cap = WindowsCapture(cursor_capture=False, draw_border=False, window_hwnd=hwnd)


@cap.event
def on_frame_arrived(frame, ctl):
    a = np.asarray(frame.frame_buffer)
    c = a[a.shape[0] // 4 : 3 * a.shape[0] // 4, a.shape[1] // 4 : 3 * a.shape[1] // 4, :3].reshape(-1, 3)
    red = float(((c[:, 2] > 200) & (c[:, 1] < 60) & (c[:, 0] < 60)).mean())  # BGRA
    log.append((list(a.shape), round(red, 2)))


@cap.event
def on_closed():
    pass


ctl = cap.start_free_threaded()
t = time.time()
while time.time() - t < 1.5:
    root.update()
    time.sleep(0.02)
n_before = len(log)
root.geometry("400x300+900+500")  # follow: window moved
t = time.time()
while time.time() - t < 1.0:
    root.update()
    time.sleep(0.02)
ctl.stop()
print(json.dumps({"hwnd": hwnd, "frames_while_occluded": n_before, "first_frame": log[0] if log else None,
                  "last_frame_after_move": log[-1] if log else None, "total": len(log)}))
