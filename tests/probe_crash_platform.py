# -*- coding: utf-8 -*-
"""判定实验：⑩ 那段（逐页加载 + 连切主题色 + 主题/尺寸切换）在哪个 Qt 平台下崩。

用法：  probe_crash_platform.py offscreen | windows
"""
import os
import sys
import time

MODE = (sys.argv[1] if len(sys.argv) > 1 else "offscreen").lower()
os.environ["WTB_NO_BROWSER"] = "1"
os.environ["QT_QPA_PLATFORM"] = MODE

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, PROJ)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import app as A
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

A.confirm = lambda *a, **k: False
A.info = lambda *a, **k: None
A.warn = lambda *a, **k: None
A.safety_confirm = lambda *a, **k: False

PAGES = ["overview", "optimize", "cputune", "power", "gpu", "software",
         "startup", "performance", "network", "dns", "cleanup", "tools",
         "hub", "regtool", "systool", "policies", "settings"]

qapp = QApplication([])
info = A.probe_system_info(qapp)
w = A.MainWindow(settings={}, admin_mode=True, dark=False, sysinfo=info)
w.resize(1220, 820)
if MODE != "offscreen":
    # 真实平台 + 不上屏（既不闪窗，也不走 offscreen 的 backing store）
    w.setAttribute(Qt.WA_DontShowOnScreen, True)
w.show()
print("platform =", MODE, "| 实际上报 =", qapp.platformName())


def pump(sec=0.0):
    qapp.processEvents()
    if sec:
        t0 = time.time()
        while time.time() - t0 < sec:
            qapp.processEvents()
            time.sleep(0.03)


def mark(msg):
    print("  >> %s" % msg)
    sys.stdout.flush()


pump(0.6)
mark("逐页 goto + refresh")
for key in PAGES:
    w.goto(key)
    pump(0.25)
    p = w.pages.get(key)
    if p is not None:
        try:
            p.refresh()
        except Exception as e:
            print("     refresh %s 异常: %s" % (key, e))
    pump(0.5)
mark("逐页完成")

w.goto("settings")
pump(0.6)

mark("连切主题色（regress ⑩ 的崩点）")
for i, (_n, hx) in enumerate(A.T.ACCENT_PRESETS):
    w.set_accent(hx)
    print("     [%d] %s ok" % (i, hx))
    sys.stdout.flush()
pump(0.6)
mark("主题色切完，活下来了")

mark("跟随系统强调色 + 暗色来回切 + 缩放")
w.set_accent("")
pump(0.4)
w.toggle_theme()
pump(0.6)
w.toggle_theme()
pump(0.6)
w.resize(1000, 700)
pump(0.4)
w.resize(1240, 840)
pump(0.4)
mark("全部通过")

w.hide()
sys.stdout.flush()
os._exit(0)
