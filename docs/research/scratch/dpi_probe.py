import sys, ctypes, json
from PySide6.QtWidgets import QApplication, QWidget
app = QApplication(sys.argv); w = QWidget(); w.show(); app.processEvents()
u = ctypes.windll.user32
u.GetWindowDpiAwarenessContext.restype = ctypes.c_void_p
u.GetWindowDpiAwarenessContext.argtypes = [ctypes.c_void_p]
ctx = u.GetWindowDpiAwarenessContext(int(w.winId()))
u.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
pm2 = u.AreDpiAwarenessContextsEqual(ctx, ctypes.c_void_p(-4))
print(json.dumps({"per_monitor_v2": bool(pm2), "screens": [(s.name(), s.devicePixelRatio()) for s in app.screens()]}))
