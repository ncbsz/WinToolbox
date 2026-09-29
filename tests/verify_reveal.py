# -*- coding: utf-8 -*-
"""验证右键「定位文件夹」：各页面表格都注册了解析器，路径能取到且不崩。
explorer 启动被打桩（测试时绝不真的弹资源管理器）。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer, QEventLoop

import app as A
import server as S

FAIL = []


def chk(cond, label):
    print(("  [OK]  " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL.append(label)


launched = []
# 只桩掉「调起资源管理器」这一层：直接桩 subprocess.Popen 会把 server 的
# PowerShell 数据链路一起桩掉（app.subprocess 就是全局 subprocess 模块）
A._explorer_select = lambda p: launched.append(p)

app = QApplication(sys.argv)
win = A.MainWindow(dark=False)
win.show()


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


print("=== 注册情况 ===")
pages = [("software", "table"), ("cputune", "proc_table"), ("performance", "table"),
         ("cleanup", "table"), ("startup", "t1"), ("startup", "t2"), ("startup", "t3")]
for key, attr in pages:
    win.goto(key)
    wait(1200)
    page = win.pages[key]
    tbl = getattr(page, attr, None)
    registered = tbl in A._REVEAL_GETTERS
    n = tbl.rowCount() if tbl is not None else -1
    chk(registered, "%s.%s 已注册定位解析器（%d 行）" % (key, attr, n))

print("\n=== 取路径（不崩 + 有值）===")
for key, attr in pages:
    win.goto(key)
    wait(1200)
    page = win.pages[key]
    if key == "cleanup" and hasattr(page, "scan"):     # 垃圾清理不自动扫描
        page.scan()
    wait(11000)                                        # 进程表走 CIM，需要数秒
    tbl = getattr(page, attr)
    if tbl.rowCount() == 0:
        print("  [skip] %s.%s 无数据" % (key, attr))
        continue
    got = []
    for r in range(min(5, tbl.rowCount())):
        try:
            got.append(A._row_reveal_path(tbl, tbl.visualRect(tbl.model().index(r, 0)).center()))
        except Exception as e:
            got.append("EXC:%s" % e)
    ok_rows = sum(1 for g in got if g and not str(g).startswith("EXC"))
    chk(not any(str(g).startswith("EXC") for g in got),
        "%s.%s 前 5 行解析无异常（%d 行有路径）" % (key, attr, ok_rows))
    if ok_rows:
        print("        示例:", [g for g in got if g][0][:70])

print("\n=== reveal_in_explorer 行为 ===")
tmp = os.path.abspath(".")
chk(A.reveal_in_explorer(tmp) is True, "目录存在 → 调起 explorer")
chk(A.reveal_in_explorer(os.path.join(tmp, "server.py")) is True, "文件存在 → 调起 explorer")
chk(A.reveal_in_explorer("Z:\\nope\\nope.exe") is False, "全不存在的路径 → 返回 False（不弹）")
chk(len(launched) == 2, "explorer 只被调起 2 次（不存在的不启动）：%d" % len(launched))
chk(all(p and os.path.exists(p.rstrip("\\/")) for p in launched),
    "被调起的路径都真实存在：%s" % [p[-28:] for p in launched])

print("\n=== Shell API（SHOpenFolderAndSelectItems）===")
chk(A._shell_select("Z:\\nope\\nope.exe") is False, "不存在的路径 → False（不弹窗、不报错）")
chk(A._shell_select(r"C:\Program Files\绝对不存在的子目录") is False,
    "含空格的不存在路径 → False")
chk(A._shell_select("") is False, "空路径 → False")
# 说明：成功路径（真实存在）会真的打开资源管理器窗口，故不在自动化测试里跑；
# 手工验证结论：正斜杠/反斜杠/含空格三条路径 SHParseDisplayName 均返回 S_OK。

print("\n=== 结果 ===")
print("全部通过" if not FAIL else "失败 %d 项: %s" % (len(FAIL), FAIL))
sys.stdout.flush()
os._exit(0 if not FAIL else 1)
