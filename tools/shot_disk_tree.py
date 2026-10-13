# -*- coding: utf-8 -*-
"""截「磁盘占用」惰性树：只显示主文件夹 → 展开一层 → 展开两层。"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication            # noqa: E402
from PySide6.QtCore import Qt, QTimer                  # noqa: E402
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


ROOT = "D:\\"
FAKE = [
    (ROOT + "Visual Studio", 18 * 1024 ** 3, 48211, 1),
    (ROOT + "下载", 41 * 1024 ** 3, 3120, 1),
    (ROOT + "文档", 6 * 1024 ** 3, 9120, 1),
    (ROOT + "图片", 12 * 1024 ** 3, 20431, 1),
    (ROOT + "Program Files", 33 * 1024 ** 3, 91022, 1),
    (ROOT + "下载\\电影", 30 * 1024 ** 3, 210, 2),
    (ROOT + "下载\\安装包", 9 * 1024 ** 3, 640, 2),
    (ROOT + "下载\\电影\\2024", 22 * 1024 ** 3, 120, 3),
    (ROOT + "Program Files\\Adobe", 21 * 1024 ** 3, 40000, 2),
    (ROOT + "Program Files\\NVIDIA", 9 * 1024 ** 3, 30000, 2),
]

w = A.MainWindow(admin_mode=True, dark=False, sysinfo=None)
w.resize(1180, 820)
w.setAttribute(Qt.WA_DontShowOnScreen, True)
w.show()
pump(2.0)
w.goto("tools")
pump(1.5)
pg = w.pages["tools"]
pg.tabs.setCurrentIndex(3)      # 磁盘占用扫描
pump(0.8)
pg.cb_drive.blockSignals(True)
pg.cb_drive.clear()
pg.cb_drive.addItem("D:\\  可用 120.0 GB / 共 1.0 TB", ROOT)
pg.cb_drive.setCurrentIndex(0)
pg.cb_drive.blockSignals(False)
pg._dk_caps = [1024 ** 4]
pg._dk_cap = 1024 ** 4
pg._dk_root = ROOT
pg._dk_tree = {}
pg._dk_expanded = set()
for p, b, f, _d in FAKE:
    pg._dk_tree.setdefault(os.path.dirname(p), []).append((p, b, f))
pg.pill_dk.setText("✓ 扫描完成 · 共 10 个文件夹 / 182,884 个文件 / "
                   "合计 198.0 GB / 12.4 秒（点目录展开子级）")
pg._dk_render()
pump(0.4)
w.grab().save(os.path.join(OUT, "磁盘占用_仅主文件夹.png"))
print("① 仅主文件夹：", [pg.tb_dk.item(r, 0).text()
                    for r in range(pg.tb_dk.rowCount())])

# 展开「下载」
paths = [pg.tb_dk.item(r, 0).data(Qt.UserRole)
         for r in range(pg.tb_dk.rowCount())]
pg._dk_toggle(paths.index(ROOT + "下载"), 0)
pump(0.4)
w.grab().save(os.path.join(OUT, "磁盘占用_展开一层.png"))
print("② 展开下载：", [pg.tb_dk.item(r, 0).text()
                  for r in range(pg.tb_dk.rowCount())])

# 再展开「Program Files」
paths = [pg.tb_dk.item(r, 0).data(Qt.UserRole)
         for r in range(pg.tb_dk.rowCount())]
pg._dk_toggle(paths.index(ROOT + "Program Files"), 0)
pump(0.4)
w.grab().save(os.path.join(OUT, "磁盘占用_展开两层.png"))
print("③ 展开两层：", [pg.tb_dk.item(r, 0).text()
                  for r in range(pg.tb_dk.rowCount())])

# 深色
w2 = A.MainWindow(admin_mode=True, dark=True, sysinfo=None)
w2.resize(1180, 820)
w2.setAttribute(Qt.WA_DontShowOnScreen, True)
w2.show()
pump(2.0)
w2.goto("tools")
pump(1.5)
p2 = w2.pages["tools"]
p2.tabs.setCurrentIndex(3)
pump(0.8)
p2.cb_drive.clear()
p2.cb_drive.addItem("D:\\  可用 120.0 GB / 共 1.0 TB", ROOT)
p2._dk_caps = [1024 ** 4]
p2._dk_cap = 1024 ** 4
p2._dk_root = ROOT
p2._dk_tree = {}
p2._dk_expanded = {ROOT + "下载"}
for p, b, f, _d in FAKE:
    p2._dk_tree.setdefault(os.path.dirname(p), []).append((p, b, f))
p2.pill_dk.setText("✓ 扫描完成 · 共 10 个文件夹 / 182,884 个文件 / "
                   "合计 198.0 GB / 12.4 秒（点目录展开子级）")
p2._dk_render()
pump(0.4)
w2.grab().save(os.path.join(OUT, "磁盘占用_深色.png"))
print("saved 深色")
w.hide(); w.deleteLater()
w2.hide(); w2.deleteLater()
pump(0.3)
print("done")
os._exit(0)