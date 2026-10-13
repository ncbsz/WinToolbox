# -*- coding: utf-8 -*-
"""截图：「高级启动」卡片、磁盘占用的搜索栏与原生右键菜单。

不真扫盘、不真重启 —— 用合成数据喂进 _dk_* 状态。
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication, QFrame, QMenu    # noqa: E402
from PySide6.QtCore import Qt, QPoint                        # noqa: E402
from PySide6.QtGui import QFont                              # noqa: E402
import app as A                                              # noqa: E402

A.settings_save = lambda *a, **k: None
qapp = QApplication.instance() or QApplication(sys.argv)
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "_preview")
os.makedirs(OUT, exist_ok=True)


def pump(secs):
    t0 = time.time()
    while time.time() - t0 < secs:
        qapp.processEvents()
        time.sleep(0.04)


ROOT = "C:\\"
FAKE = [
    (ROOT + "Windows", 41 * 1024 ** 3, 182344, 1),
    (ROOT + "Users", 96 * 1024 ** 3, 421233, 1),
    (ROOT + "Program Files", 19 * 1024 ** 3, 56334, 1),
    (ROOT + "Windows\\WinSxS", 12 * 1024 ** 3, 98221, 2),
    (ROOT + "Users\\29566", 71 * 1024 ** 3, 288912, 2),
    (ROOT + "Users\\29566\\AppData", 33 * 1024 ** 3, 154002, 3),
]

for dark in (False, True):
    tag = "深色" if dark else "浅色"
    w = A.MainWindow(admin_mode=True, dark=dark, sysinfo=None)
    w.resize(1180, 820)
    w.setAttribute(Qt.WA_DontShowOnScreen, True)
    w.show()
    pump(2.2)

    # ---- 「高级启动」卡片（只截那一个卡片的框）----
    w.goto("settings")
    pump(1.2)
    pg = w.pages["settings"]
    card = pg.btn_reboot_cancel
    while card is not None and not (isinstance(card, QFrame)
                                   and card.objectName() == "Card"):
        card = card.parent()
    if card is not None:
        card.grab().save(os.path.join(OUT, "高级启动卡片_%s.png" % tag))
        print("%s  「高级启动」卡片已截（含 %d 个按钮）"
              % (tag, len([b for b in card.findChildren(type(pg.btn_reboot_cancel))])))
    # 安排一次重启（打桩，不真重启），截倒计时状态
    A.S.reboot_to = lambda delay=None: {"ok": True,
                                 "name": "「高级启动」", "delay": 27, "err": ""}
    A.confirm = lambda *a, **k: True
    pg._reboot_ui()
    pump(0.6)
    if card is not None:
        card.grab().save(os.path.join(OUT, "高级启动卡片_已安排重启_%s.png" % tag))
        print("      倒计时态：%r" % pg.lb_reboot.text())
    pg.timer_reboot.stop()
    pg.btn_reboot_cancel.setEnabled(False)
    pg.lb_reboot.setVisible(False)

    # ---- 磁盘占用：搜索栏 + 树 ----
    w.goto("tools")
    pump(1.0)
    tp = w.pages["tools"]
    tp.tabs.setCurrentIndex(3)          # 磁盘占用扫描
    pump(0.8)
    tp._dk_root = ROOT
    tp._dk_tree = {}
    tp._dk_flat = {}
    tp._dk_files = [(ROOT + "Users\\29566\\Downloads\\巨无霸.zip", 4 * 1024 ** 3),
                    (ROOT + "Users\\29566\\Videos\\录屏.mkv", 2 * 1024 ** 3),
                    (ROOT + "Users\\29566\\x.iso", 900 * 1024 ** 2)]
    tp._dk_expanded = {ROOT + "Users"}
    tp._dk_inbox = list(FAKE)
    tp._dk_live = {"files": 976233, "dirs": 240659,
                   "bytes": 260 * 1024 ** 3, "t0": time.time() - 46}
    tp._dk_drain(final=True)
    tp._dk_cap_update()
    tp.pill_dk.setText("扫描中…")
    tp._dk_stat("扫描中… 已发现 %d 个文件夹 / %d 个文件 / 已扫到 %s / 46 秒"
                % (tp._dk_n, 976233, A.fmt_bytes(260 * 1024 ** 3)))
    # 搜索一下，截搜索结果
    tp.ed_dk.setText("s")
    tp.dk_search()
    pump(0.5)
    w.grab().save(os.path.join(OUT, "磁盘占用_搜索_%s.png" % tag))
    print("%s  搜索结果：%s" % (tag, tp.lb_dk_search.text()))
    tp.dk_search_clear()
    pump(0.4)
    w.grab().save(os.path.join(OUT, "磁盘占用_树_%s.png" % tag))

    # ---- 原生右键菜单 ----
    from PySide6.QtWidgets import QMenu as _M
    m = _M(tp)
    A.style_menu(m, dark)
    row = 0
    it = tp.tb_dk.item(row, 0)
    A.native_file_menu(m, it.data(Qt.UserRole) if it else ROOT, tp)
    m.setFont(QFont(m.font().family(), 10))
    m.popup(w.mapToGlobal(QPoint(60, 140)))
    pump(0.6)
    pix = m.grab()
    pix.save(os.path.join(OUT, "右键菜单_原生_%s.png" % tag))
    print("%s  菜单大小 %dx%d，%d 项"
          % (tag, pix.width(), pix.height(), len(m.actions())))
    m.close()
    pump(0.2)

    w.hide()
    w.deleteLater()
    pump(0.3)

print("done")
sys.stdout.flush()
os._exit(0)
