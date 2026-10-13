# -*- coding: utf-8 -*-
"""微实验：confirm_ex 里 box.deleteLater() 之后，processEvents 能否把
未显示的 QMessageBox 从控件树里摘掉。"""
import os
import sys
import time

os.environ["WTB_NO_BROWSER"] = "1"
os.environ.setdefault("QT_QPA_PLATFORM", "windows")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import app as A
from PySide6.QtWidgets import QApplication, QMessageBox, QCheckBox

A.QMessageBox.exec = lambda self, *a, **k: 0
A._center_dialog = lambda *a, **k: None
A.settings_save = lambda *a, **k: None

qapp = QApplication([])
info = A.probe_system_info(qapp)
w = A.MainWindow(settings={}, admin_mode=True, dark=False, sysinfo=info)
w.setAttribute(A.Qt.WA_DontShowOnScreen, True)
w.show()

page = w.pages["cleanup"]
print("初始 QMessageBox 数:", len(w.findChildren(QMessageBox)))

box = A.QMessageBox(page)
box.setWindowTitle("t")
box.setText("x")
box.setIcon(A.QMessageBox.Warning)
box.setStandardButtons(A.QMessageBox.Yes | A.QMessageBox.No)
cb = QCheckBox("b")
box.setCheckBox(cb)
ok = box.exec() == A.QMessageBox.Yes
print("exec 后（未 deleteLater）:", len(w.findChildren(QMessageBox)))
box.deleteLater()
for _ in range(50):
    qapp.processEvents()
    time.sleep(0.02)
print("deleteLater + processEvents 后:", len(w.findChildren(QMessageBox)))

# 真实事件循环（app.exec()）下再验一次：deferred delete 只在事件循环
# 「即将阻塞」时处理 —— 真实应用走这条路，测试 harness 走不了。
from PySide6.QtCore import QTimer
QTimer.singleShot(400, qapp.quit)
qapp.exec()
print("deleteLater + 真实事件循环 400ms 后:", len(w.findChildren(QMessageBox)))
w.hide()
os._exit(0)
