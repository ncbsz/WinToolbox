# -*- coding: utf-8 -*-
"""验证假设：⑨ 段 cl.clean() 创建的「未显示 QMessageBox」留在页面树里，
之后 setStyleSheet 重抛光整棵树 → 访问冲突。

用法： probe_crash_dialog.py          （只造对话框，不扫描）
       probe_crash_dialog.py scan     （先真扫描再造对话框，贴近 ⑨ 段）
"""
import os
import sys
import time

os.environ["WTB_NO_BROWSER"] = "1"
os.environ.setdefault("QT_QPA_PLATFORM", "windows")
DO_SCAN = len(sys.argv) > 1 and sys.argv[1] == "scan"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import faulthandler

faulthandler.enable()

import app as A
import server as S
from PySide6.QtWidgets import QApplication, QCheckBox
from PySide6.QtCore import Qt

# —— 与 regress_all.py 完全相同的打桩 ——
DIALOG = []
A.confirm = lambda *a, **k: (DIALOG.append(("confirm",)), False)[1]
A.info = lambda *a, **k: None
A.warn = lambda *a, **k: None
A.safety_confirm = lambda *a, **k: False
A.QMessageBox.information = staticmethod(lambda *a, **k: None)
A.QMessageBox.warning = staticmethod(lambda *a, **k: None)
A.QMessageBox.critical = staticmethod(lambda *a, **k: None)
A.QMessageBox.question = staticmethod(lambda *a, **k: A.QMessageBox.No)
A.QMessageBox.exec = lambda self, *a, **k: 0
A._center_dialog = lambda *a, **k: None
A.settings_save = lambda *a, **k: None

qapp = QApplication([])
info = A.probe_system_info(qapp)
w = A.MainWindow(settings={}, admin_mode=True, dark=False, sysinfo=info)
w.resize(1220, 820)
w.setAttribute(Qt.WA_DontShowOnScreen, True)
w.show()


def pump(sec=0.35):
    t0 = time.time()
    while time.time() - t0 < sec:
        qapp.processEvents()
        time.sleep(0.02)


def until(pred, sec=90.0):
    t0 = time.time()
    while time.time() - t0 < sec:
        qapp.processEvents()
        if pred():
            return True
        time.sleep(0.03)
    return bool(pred())


print(">> 组：%s | 平台 %s" % ("scan+dialog" if DO_SCAN else "dialog-only",
                             qapp.platformName()))
sys.stdout.flush()

for key in ("overview", "optimize", "cputune", "power", "gpu", "software",
            "startup", "performance", "network", "dns", "cleanup", "tools",
            "hub", "regtool", "systool", "policies", "settings"):
    w.goto(key)
    pump(0.12)
print(">> 17 页加载完成")
sys.stdout.flush()

cl = w.pages["cleanup"]
w.goto("cleanup")
pump(0.3)
cl.refresh()
pump(0.3)

if DO_SCAN:
    cl.scan()
    until(lambda: cl.table.rowCount() > 0, 90)
    pump(1.0)
    cl.update_sum()
    pump(0.4)
    print(">> 真实垃圾扫描完成：%d 行" % cl.table.rowCount())
    sys.stdout.flush()

# —— 关键：不调 cl.clean()（它内部会 confirm_ex → 真对话框），
#    而是**原样复刻** cl.clean() 里那段 QMessageBox 构造，确保对话框
#    以同样方式留在页面树里（exec 已打桩 → 立即返回，从未显示）。
box = A.QMessageBox(cl)
box.setWindowTitle("确认清理")
box.setText("将清理约 12.6 GB 的临时/缓存文件（123456 个文件）。\n\n"
            "「文件年龄」当前为：7 天\n"
            "（比这个时间新的文件会原样保留，不参与清理）\n\n"
            "不勾选备份时：直接删除，速度最快。\n"
            "勾选备份时：先移入回收站（可还原），但预计慢约 92 分钟。")
box.setIcon(A.QMessageBox.Warning)
box.setStandardButtons(A.QMessageBox.Yes | A.QMessageBox.No)
box.button(A.QMessageBox.Yes).setText("确定")
box.button(A.QMessageBox.No).setText("取消")
cb = QCheckBox("删除前移入回收站（可还原，但很慢）")
cb.setChecked(False)
box.setCheckBox(cb)
ok = box.exec() == A.QMessageBox.Yes
print(">> confirm_ex 路径复刻完成（ok=%s，对话框未显示，仍在页面树里）" % ok)
sys.stdout.flush()

pump(0.5)
print(">> 开始连切主题色")
sys.stdout.flush()
[w.set_accent(hx) for _n, hx in A.T.ACCENT_PRESETS]
pump(0.6)
print(">> 主题色 10 连切完成（未崩）")
sys.stdout.flush()
w.set_accent("")
pump(0.4)
w.toggle_theme(); pump(0.6); w.toggle_theme(); pump(0.6)
w.resize(1000, 700); pump(0.4); w.resize(1240, 840); pump(0.4)
print(">> 全部通过 —— 未复现崩溃")
sys.stdout.flush()
w.hide()
os._exit(0)
