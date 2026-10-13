# -*- coding: utf-8 -*-
"""截「系统优化」页 Defender 卡片：全关闭版本的三态。"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication            # noqa: E402
from PySide6.QtCore import Qt                          # noqa: E402
import app as A                                        # noqa: E402

A.settings_save = lambda *a, **k: None
qapp = QApplication.instance() or QApplication(sys.argv)
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "_preview")
os.makedirs(OUT, exist_ok=True)


def pump(sec):
    t0 = time.time()
    while time.time() - t0 < sec:
        qapp.processEvents()
        time.sleep(0.03)


STATES = [
    ("已开启", dict(enabled=True, realtime=True, tamper=False, start=2,
                    n_disabled=0, n_total=8)),
    ("全关闭", dict(enabled=False, realtime=False, tamper=False, start=4,
                    n_disabled=8, n_total=8)),
    ("部分关闭", dict(enabled=False, realtime=False, tamper=False, start=4,
                      n_disabled=5, n_total=8)),
    ("篡改防护开启", dict(enabled=True, realtime=True, tamper=True, start=2,
                          n_disabled=0, n_total=8)),
]
for dark in (False, True):
    w = A.MainWindow(admin_mode=True, dark=dark, sysinfo=None)
    w.resize(1180, 900)
    w.setAttribute(Qt.WA_DontShowOnScreen, True)
    w.show()
    pump(2.0)
    w.goto("optimize")
    pump(1.2)
    pg = w.pages["optimize"]
    for name, st in STATES:
        pg._def_enabled = bool(st["enabled"])
        pg._def_status = dict(st)
        tp = st.get("tamper")
        if tp:
            pg.def_hint.setText(
                "⚠ 「篡改防护」正在开启 —— 系统的设置改动可能被自动改回。")
            pg.def_hint.setVisible(True)
        else:
            pg.def_hint.setVisible(False)
        pg._paint_toggles()
        pump(0.3)
        print("  %-6s %-14s pill=%r 按钮=%r"
              % ("深色" if dark else "浅色", name,
                 pg.def_pill.text(), pg.btn_def.text()))
        w.grab().save(os.path.join(OUT, "Defender全关_%s_%s.png"
                                   % (name, "深色" if dark else "浅色")))
    w.hide()
    w.deleteLater()
    pump(0.3)
print("done")
os._exit(0)