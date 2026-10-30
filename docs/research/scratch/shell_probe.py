"""Throwaway probe: click-through translucent overlay + tray + RegisterHotKey +
WDA_EXCLUDEFROMCAPTURE. Prints JSON. Not product code."""
import sys, time, json, ctypes
from ctypes import wintypes
t0 = time.perf_counter()
from PySide6.QtCore import Qt, QTimer, QAbstractNativeEventFilter, QRect
from PySide6.QtGui import QColor, QPainter, QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QWidget, QSystemTrayIcon, QMenu
import_ms = (time.perf_counter() - t0) * 1000
u32 = ctypes.windll.user32
WM_HOTKEY = 0x0312; MOD_ALT, MOD_CTRL, MOD_NOREPEAT = 1, 2, 0x4000
class HK(QAbstractNativeEventFilter):
    got = 0
    def nativeEventFilter(self, et, msg):
        if et == b"windows_generic_MSG":
            m = wintypes.MSG.from_address(int(msg))
            if m.message == WM_HOTKEY: HK.got += 1
        return False, 0
class Overlay(QWidget):
    def __init__(s):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        s.setAttribute(Qt.WA_TranslucentBackground); s.setAttribute(Qt.WA_ShowWithoutActivating)
        s.setGeometry(200, 200, 300, 200)
    def paintEvent(s, e):
        p = QPainter(s); p.fillRect(s.rect(), QColor(255, 0, 0, 255))
def grab_px(x, y):
    import mss
    with mss.mss() as sct:
        im = sct.grab({"left": x, "top": y, "width": 1, "height": 1})
        return tuple(im.rgb[:3])
app = QApplication(sys.argv)
flt = HK(); app.installNativeEventFilter(flt)
reg = bool(u32.RegisterHotKey(None, 1, MOD_CTRL | MOD_ALT | MOD_NOREPEAT, 0x54))
pm = QPixmap(16, 16); pm.fill(QColor("blue"))
tray = QSystemTrayIcon(QIcon(pm)); tray.setContextMenu(QMenu()); tray.show()
ov = Overlay(); ov.show(); app.processEvents(); time.sleep(0.5); app.processEvents()
hwnd = int(ov.winId())
ex = u32.GetWindowLongW(hwnd, -20)
res = {"qt_import_ms": round(import_ms), "hotkey_registered": reg, "tray_visible": tray.isVisible(),
       "exstyle_layered": bool(ex & 0x80000), "exstyle_transparent": bool(ex & 0x20),
       "exstyle_noactivate": bool(ex & 0x08000000)}
# click-through: which window is at a point inside the overlay?
pt = wintypes.POINT(300, 300); res["WindowFromPoint_is_overlay"] = (u32.WindowFromPoint(pt) == hwnd)
res["px_without_affinity"] = grab_px(300, 300)
res["affinity_ok"] = bool(u32.SetWindowDisplayAffinity(hwnd, 0x11)); app.processEvents(); time.sleep(0.5)
res["px_with_affinity"] = grab_px(300, 300)
# simulate hotkey press
u32.keybd_event(0x11,0,0,0); u32.keybd_event(0x12,0,0,0); u32.keybd_event(0x54,0,0,0)
u32.keybd_event(0x54,0,2,0); u32.keybd_event(0x12,0,2,0); u32.keybd_event(0x11,0,2,0)
end = time.time()+1; 
while time.time()<end: app.processEvents(); time.sleep(0.02)
res["hotkey_msgs_received"] = HK.got
u32.UnregisterHotKey(None, 1)
res["total_startup_ms"] = round((time.perf_counter() - t0) * 1000)
print("RESULT " + json.dumps(res))
