# -*- coding: utf-8 -*-
"""测 ⑧：一次整树字体校正（`_refit_fonts`）到底多贵 —— 决定要不要改成「只做可见页」。"""
import os
import sys
import time

os.environ["WTB_NO_BROWSER"] = "1"
os.environ.setdefault("QT_QPA_PLATFORM", "windows")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import app as A
from PySide6.QtWidgets import QApplication, QLabel
from PySide6.QtCore import Qt

A.settings_save = lambda *a, **k: None
qapp = QApplication([])
info = A.probe_system_info(qapp)
w = A.MainWindow(settings={}, admin_mode=True, dark=False, sysinfo=info)
w.setAttribute(Qt.WA_DontShowOnScreen, True)
w.resize(1220, 820)
w.show()


def pump(sec=0.4):
    t0 = time.time()
    while time.time() - t0 < sec:
        qapp.processEvents()
        time.sleep(0.02)


pump(1.0)
n_pages = len(w.pages)
n_lb = sum(len(p.findChildren(QLabel)) for p in w.pages.values())
print("页面 %d 个 | 标签 %d 个" % (n_pages, n_lb))
ts = []
for _ in range(6):
    t0 = time.perf_counter()
    w._refit_fonts()
    ts.append((time.perf_counter() - t0) * 1000)
ts.sort()
print("整树 _refit_fonts: 最快 %.1fms / 中位 %.1fms / 最慢 %.1fms（%d 次）"
      % (ts[0], ts[len(ts) // 2], ts[-1], len(ts)))
# 对照：只做当前页
cur = w.pages.get("settings")
ts2 = []
for _ in range(6):
    t0 = time.perf_counter()
    A.fit_labels(cur)
    ts2.append((time.perf_counter() - t0) * 1000)
ts2.sort()
print("单页 fit_labels: 最快 %.1fms / 中位 %.1fms" % (ts2[0], ts2[len(ts2) // 2]))
w.hide()
os._exit(0)
