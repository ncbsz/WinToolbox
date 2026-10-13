# -*- coding: utf-8 -*-
"""给「无效项清理」页截一张图：多选下拉按钮长什么样、有没有把布局撑坏。

不写用户配置；不打桩任何写操作（这个页签只读）。
"""
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


def pump(sec):
    t0 = time.time()
    while time.time() - t0 < sec:
        qapp.processEvents()
        time.sleep(0.03)


OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "_preview")
os.makedirs(OUT, exist_ok=True)

#造一批假结果，让过滤下拉有内容可显示（真扫太慢且会碰系统）
FAKE = []
for cat, cnt in (("com_server", 3), ("filetype", 2), ("service", 1),
                 ("firewall", 1), ("startup", 2)):
    for k in range(cnt):
        FAKE.append({"hive": "HKLM", "path": "\\SOFTWARE\\Test\\%s_%d" % (cat, k),
                     "value": "x.exe", "kind": "invalid_" + cat,
                     "cat": cat, "why": "目标文件不存在",
                     "weak": cat in ("service", "firewall"),
                     "_checked": cat not in ("service", "firewall")})

for dark in (False, True):
    w = A.MainWindow(settings={"close_action": "ask", "check_update": False,
                               "accent": "#2B8CFF"},
                     admin_mode=True, dark=dark, sysinfo=None)
    w.resize(1180, 780)
    w.setAttribute(Qt.WA_DontShowOnScreen, True)
    w.show()
    pump(2.0)
    w.goto("regtool")
    pump(1.0)
    reg = w.pages["regtool"]
    reg.issues = [dict(x) for x in FAKE]
    reg._issue_per = 400
    reg._fill_issue_cat()
    reg._render_issues()
    # 默认态（全部）
    pump(0.4)
    w.grab().save(os.path.join(OUT, "无效项清理_多选全部_%s.png"
                               % ("深色" if dark else "浅色")))
    # 选中 2 类
    reg.mp_issue_cat._sel = {"com_server", "filetype"}
    reg.mp_issue_cat._paint()        # 直接改 _sel 不会自动重绘按钮
    reg._render_issues()
    pump(0.4)
    w.grab().save(os.path.join(OUT, "无效项清理_多选2类_%s.png"
                               % ("深色" if dark else "浅色")))
    print("  %s 按钮文字=%r 显示=%d 项"
          % ("深色" if dark else "浅色",
             reg.mp_issue_cat.btn.text(), reg.tb_issue.rowCount()))
    w.hide()
    w.deleteLater()
    pump(0.3)
print("done")
os._exit(0)