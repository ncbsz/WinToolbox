# -*- coding: utf-8 -*-
"""逐项计时垃圾扫描：拿到每项耗时后，可推算任意并发上限下的总耗时。

总耗时 ≈ max(单项最长, 各项之和 / 并发数)
"""
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
while time.time() - t0 < 1.2:
    qapp.processEvents()
    time.sleep(0.02)

cl = w.pages["cleanup"]
w.goto("cleanup")
cl.refresh()
# ⚠️ 必须给「延迟刷新」留一拍：`refresh()` 会递增 _scan_token 作废进行中的扫描，
#    goto 触发的延迟 refresh 若落在 scan() 之后就会把这次扫描作废（表永不填）。
#    regress 里 refresh() 后也有 pump(0.3)。
_t = time.time()
while time.time() - _t < 0.6:
    qapp.processEvents()
    time.sleep(0.02)

dur = {}
_orig = A.S.junk_item_size


def _timed(tid, sc=None):
    t = time.perf_counter()
    try:
        return _orig(tid, sc)
    finally:
        dur[tid] = time.perf_counter() - t


A.S.junk_item_size = _timed
names = {t["id"]: t.get("name") or t["id"] for t in A.S.junk_targets(cl._age_scale())}
names["recyclebin"] = "回收站"

t0 = time.perf_counter()
cl.scan()
while cl.table.rowCount() == 0 and time.perf_counter() - t0 < 300:
    qapp.processEvents()
    time.sleep(0.03)
wall = time.perf_counter() - t0
A.S.junk_item_size = _orig

items = sorted(dur.items(), key=lambda kv: -kv[1])
tot = sum(dur.values())
print("并发上限 =", cl._SCAN_MAX_PAR, "| 墙钟 %.1fs | 入表 %d 行" % (wall, cl.table.rowCount()))
print("各项耗时合计 %.1fs；最长单项 %.1fs" % (tot, items[0][1] if items else 0))
print("最慢 8 项：")
for tid, d in items[:8]:
    print("   %6.1fs  %s（%s）" % (d, names.get(tid, tid), tid))
print()
print("推算总耗时 ≈ max(最长单项, 合计/并发)：")
for c in (20, 10, 8, 6, 4):
    print("   并发 %2d → 约 %.0fs" % (c, max(items[0][1] if items else 0, tot / c)))
w.hide()
os._exit(0)
