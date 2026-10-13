# -*- coding: utf-8 -*-
"""截「读取失败」的可见效果 —— 对比原来留「—」的状态。"""
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


CASES = [
    ("gpu", "lb_state", "显卡列表", "WMI 查询超时（0x80041010）", "显卡伪装"),
    ("cputune", "lb_sched", "该进程的调度状态", "权限不足", "CPU调试"),
    ("power", "lb_plan_state", "电源计划列表", "powercfg 返回空", "电源管理"),
]

for dark in (False, True):
    w = A.MainWindow(admin_mode=True, dark=dark, sysinfo=None)
    w.resize(1180, 820)
    w.setAttribute(Qt.WA_DontShowOnScreen, True)
    w.show()
    pump(2.0)
    for pname, attr, what, msg, nav in CASES:
        w.goto(pname)
        pump(1.0)
        pg = w.pages[pname]
        lb = getattr(pg, attr)
        # 先模拟「成功时」的样子，再模拟「读取失败」
        print("  %-6s %-10s 成功时=%-14r -> 失败时=%r"
              % ("深色" if dark else "浅色", pname, lb.text(),
                 "读取失败：%s（%s）" % (what, msg)))
        lb.setStyleSheet("")           # 还原成默认样式再打桩，避免叠加
        pg.load_fail(lb, what, msg)
        pump(0.3)
        w.grab().save(os.path.join(OUT, "读取失败_%s_%s.png"
                                   % (pname, "深色" if dark else "浅色")))
    w.hide()
    w.deleteLater()
    pump(0.3)

# 概览页：详细信息卡下的失败提示
w = A.MainWindow(admin_mode=True, dark=False, sysinfo=None)
w.resize(1180, 820)
w.setAttribute(Qt.WA_DontShowOnScreen, True)
w.show()
pump(2.5)
w.goto("overview")
pump(1.2)
ov = w.pages["overview"]
ov.load_fail(ov.lb_hw_fail, "硬件信息", "WMI 服务未响应")
ov.lb_hw_fail.setVisible(True)
pump(0.4)
print("  概览 lb_hw_fail =", repr(ov.lb_hw_fail.text()))
w.grab().save(os.path.join(OUT, "读取失败_overview_浅色.png"))
w.hide()
w.deleteLater()
pump(0.3)
print("done")
os._exit(0)