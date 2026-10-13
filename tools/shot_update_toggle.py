# -*- coding: utf-8 -*-
"""截「系统优化」页的更新开关卡：验证 pill / 按钮文字在四种状态下都对。

只读截图，不动注册表 —— 直接构造 `_upd_status` 假数据喂给 `_paint_toggles`。
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
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "_preview")
os.makedirs(OUT, exist_ok=True)


def pump(sec):
    t0 = time.time()
    while time.time() - t0 < sec:
        qapp.processEvents()
        time.sleep(0.03)


# 四种状态：正常 / 暂停到2099（本工具的「关闭」）/ 被禁服务 / WSUS 接管
STATES = [
    ("正常", dict(enabled=True, reasons=[], paused=False, paused_until="",
                  paused_long=False, svc_disabled=False)),
    ("暂停到2099", dict(enabled=True,
                        reasons=["更新被暂停到 2099-12-31"],
                        paused=True, paused_until="2099-12-31",
                        paused_long=True, svc_disabled=False)),
    ("服务被禁", dict(enabled=False, reasons=["更新服务 wuauserv 被禁用",
                "策略 NoAutoUpdate=1 关闭了自动更新"],
        paused=False, paused_until="", paused_long=False, svc_disabled=True)),
    ("普通暂停", dict(enabled=True, reasons=["更新被暂停到 2026-11-07"],
                      paused=True, paused_until="2026-11-07",
                      paused_long=False, svc_disabled=False)),
]

for dark in (False, True):
    w = A.MainWindow(settings={"close_action": "ask", "check_update": False,
                               "accent": "#2B8CFF"},
                     admin_mode=True, dark=dark, sysinfo=None)
    w.resize(1180, 780)
    w.setAttribute(Qt.WA_DontShowOnScreen, True)
    w.show()
    pump(2.0)
    w.goto("optimize")
    pump(1.2)
    pg = w.pages["optimize"]
    for name, st in STATES:
        pg._upd_enabled = bool(st["enabled"])
        pg._upd_reasons = list(st["reasons"])
        pg._upd_status = dict(st)
        pg._paint_toggles()
        pump(0.3)
        print("  %-6s %-10s pill=%r 按钮=%r"
              % ("深色" if dark else "浅色", name,
                 pg.upd_pill.text(), pg.btn_upd.text()))
        w.grab().save(os.path.join(OUT, "更新开关_%s_%s.png"
                                   % (name, "深色" if dark else "浅色")))
    # Defender 卡片：实时保护开 / 关两态
    for name, st in (("实时保护_开", dict(enabled=True, realtime=True,
                                    tamper=False, start=2, note="")),
                     ("实时保护_关", dict(enabled=False, realtime=False,
                                    tamper=False, start=2, note="")),
                     ("篡改防护开启", dict(enabled=False, realtime=False,
                                    tamper=True, start=2, note=""))):
        pg._def_enabled = bool(st["enabled"])
        pg._def_status = dict(st)
        tp = st.get("tamper")
        if tp:
            pg.def_hint.setText("⚠ 「篡改防护」正在开启 —— 系统的设置改动可能被自动改回。")
            pg.def_hint.setVisible(True)
        else:
            pg.def_hint.setVisible(False)
        pg._paint_toggles()
        pump(0.3)
        print("  %-6s %-14s pill=%r 按钮=%r"
              % ("深色" if dark else "浅色", name,
                 pg.def_pill.text(), pg.btn_def.text()))
        w.grab().save(os.path.join(OUT, "Defender_%s_%s.png"
                                   % (name, "深色" if dark else "浅色")))
    w.hide()
    w.deleteLater()
    pump(0.3)
print("done")
os._exit(0)