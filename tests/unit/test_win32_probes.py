"""The Windows probes behind the contract checks must themselves be right."""

from __future__ import annotations

import pytest
from testkit import win32

pytestmark = pytest.mark.windows_only


def test_os_monitors_lists_a_primary_monitor_at_the_origin():
    monitors = win32.os_monitors()
    assert monitors, "Windows reported no attached monitor"
    assert all(name.startswith("\\\\.\\DISPLAY") for name, _ in monitors)
    assert any((r.x, r.y) == (0, 0) for _, r in monitors)
    assert all(r.w > 0 and r.h > 0 for _, r in monitors)


def test_own_windows_sees_a_window_this_process_makes_and_reads_its_affinity():
    import ctypes
    import tkinter

    root = tkinter.Tk()
    try:
        root.update()
        user32 = ctypes.windll.user32
        hwnd = int(root.frame(), 16)
        before = {h: a for h, v, a in win32.own_windows() if v}
        assert hwnd in before and before[hwnd] == 0
        assert user32.SetWindowDisplayAffinity(hwnd, 0x11)
        after = {h: a for h, v, a in win32.own_windows() if v}
        assert after[hwnd] == 0x11
    finally:
        root.destroy()
