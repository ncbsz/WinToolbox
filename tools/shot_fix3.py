# -*- coding: utf-8 -*-
"""截图：Defender 结论式状态 + 注册表清理「不限」。用法： shot_fix3.py [dark]"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6.QtWidgets import QApplication, QFrame
from PySide6.QtCore import Qt

import app as A

A.settings_save = lambda *a, **k: None
DARK = len(sys.argv) > 1 and sys.argv[1] in ("dark", "1", "true")
TAG = "深色" if DARK else "浅色"

q = QApplication.instance() or QApplication(sys.argv)


def pump(secs):
    t0 = time.time()
    while time.time() - t0 < secs:
        q.processEvents()
        time.sleep(0.03)


def until(pred, secs=60.0):
    t0 = time.time()
    while time.time() - t0 < secs:
        q.processEvents()
        if pred():
            return True
        time.sleep(0.03)
    return bool(pred())


A.info = lambda *a, **k: None          # 别弹模态框卡住截图
A.warn = lambda *a, **k: None
OUT = os.path.join(ROOT, "_preview")
os.makedirs(OUT, exist_ok=True)

w = A.MainWindow(admin_mode=True, dark=DARK, sysinfo=None)
w.resize(1240, 920)
w.setAttribute(Qt.WA_DontShowOnScreen, True)
w.show()
pump(1.8)

# ---- ① 系统优化页的 Defender 卡片 ----
w.goto("optimize")
pump(2.4)
op = w.pages["optimize"]
if not until(lambda: op._def_enabled is not None, 60):
    op._toggles_done((A.S.update_status(), A.S.defender_status()))
    pump(0.4)
p = op.btn_def
while p is not None and not (isinstance(p, QFrame) and p.objectName() == "Card"):
    p = p.parentWidget()
if p is not None:
    p.grab().save(os.path.join(OUT, "Defender卡_结论式状态_%s.png" % TAG))
    print("Defender 卡片 %dx%d" % (p.width(), p.height()))
print("  pill :", op.def_pill.text())
print("  明细 :", op.def_prog.text())

# ---- ② 注册表清理：单类扫描（自动放宽） ----
w.goto("regtool")
pump(2.0)
rt = w.pages["regtool"]
rt.tabs.setCurrentIndex(0)
pump(0.8)
for cid, cb in rt.issue_cats.items():
    cb.setChecked(cid == "history")          # 只勾一类 → 自动放开上限
pump(0.4)
rt.scan_issues()
until(lambda: rt.issues or not rt.lb_issue_trunc.isHidden(), 40)
pump(1.2)
print("  per_cat =", rt._issue_per, "| auto_wide =", rt._issue_auto_wide)
print("  提示 :", rt.lb_issue_trunc.text()[:120].replace("<br>", " | "))
card = rt.tabs.widget(0)
card.grab().save(os.path.join(OUT, "注册表清理_不限_%s.png" % TAG))
print("注册表清理页 %dx%d" % (card.width(), card.height()))

w.hide()
w.deleteLater()
sys.stdout.flush()
os._exit(0)
