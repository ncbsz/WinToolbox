# -*- coding: utf-8 -*-
"""实测：限并发（_SCAN_MAX_PAR=4）之后，垃圾扫描的真实耗时与并发峰值。"""
import os
import sys
import time

os.environ["WTB_NO_BROWSER"] = "1"
os.environ.setdefault("QT_QPA_PLATFORM", "windows")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import app as A
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

A.settings_save = lambda *a, **k: None
A.confirm = lambda *a, **k: False
A.info = lambda *a, **k: None

qapp = QApplication([])
w = A.MainWindow(settings={}, admin_mode=True, dark=False,
                 sysinfo=A.probe_system_info(qapp))
w.setAttribute(Qt.WA_DontShowOnScreen, True)
w.resize(1220, 820)
w.show()
t0 = time.time()
while time.time() - t0 < 1.0:
    qapp.processEvents()
    time.sleep(0.02)

cl = w.pages["cleanup"]
w.goto("cleanup")
cl.refresh()
n = len(A.S.junk_targets(cl._age_scale())) + 1      # +1 = recyclebin

peak = {"n": 0, "max": 0}
_orig = A.S.junk_item_size


def _wrap(tid, sc=None):
    peak["n"] += 1
    peak["max"] = max(peak["max"], peak["n"])
    try:
        return _orig(tid, sc)
    finally:
        peak["n"] -= 1


A.S.junk_item_size = _wrap
t0 = time.time()
cl.scan()
while cl.table.rowCount() == 0 and time.time() - t0 < 120:
    qapp.processEvents()
    time.sleep(0.03)
dt = time.time() - t0
print("垃圾扫描：%d 项，用时 %.1fs，并发峰值 %d，入表 %d 行（_SCAN_MAX_PAR=%d）"
      % (n, dt, peak["max"], cl.table.rowCount(), cl._SCAN_MAX_PAR))
w.hide()
os._exit(0)
