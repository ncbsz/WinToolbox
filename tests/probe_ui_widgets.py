# -*- coding: utf-8 -*-
"""验证从 app.py 拆到 ui_widgets.py 的小件在运行时可正常实例化/使用
（py_compile 抓不到「方法体里引用了没导入的名字」）。"""
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["WTB_NO_BROWSER"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication, QLabel, QWidget

qapp = QApplication([])

import app as A
import ui_widgets as W

bad = []


def chk(c, label):
    print(("OK  " if c else "!!! ") + label)
    if not c:
        bad.append(label)


# NumberTween / BarTween
lb = QLabel("0")
nt = W.NumberTween(lb, lambda v: "%.0f" % v)
nt.to(123)
nt.set_now(50)
chk(lb.text() in ("50", "0", "123"), "NumberTween.to/set_now（文本=%r）" % lb.text())
bt = W.BarTween(QWidget())
chk(bt is not None, "BarTween 实例化")

# Sparkline / HueBar（自绘，抓图非空即算能画）
sp = W.Sparkline()
sp.resize(160, 40)
sp.push(1.0); sp.push(2.0)
chk(not sp.grab().isNull(), "Sparkline.push + 可渲染")
hb = W.HueBar()
hb.resize(160, 14)
hb.set_hue(120.0)
chk(abs(hb.hue() - 120.0) < 1e-6, "HueBar.set_hue/hue 往返一致（%s）" % hb.hue())
chk(not hb.grab().isNull(), "HueBar 可渲染")

# restyle / page_active / _cls / _set
w = QWidget()
W.restyle(w, "probe_obj")
chk(w.objectName() == "probe_obj", "restyle(w, name) 设置 objectName=%r" % w.objectName())
chk(W.page_active(w) in (True, False), "page_active 可调用")

# ⚠️ 回归点：_set 内部调 fit_label（上一轮拆分漏搬 → NameError）
val, sub, bar = QLabel("v"), QLabel("s"), QWidget()
W._set((val, sub, bar), "123", "sub text", size="20px")
chk(val.text() == "123" and sub.text() == "sub text", "_set 更新值/副文本（曾 NameError）")
W.fit_label(val)
chk(True, "fit_label 可直接调用")
W._cls(bar, 90)
chk(bar.objectName() == "Err", "_cls 高值 → Err 类（objectName=%r）" % bar.objectName())
W._rich_text_height(val, 120)
chk(True, "_rich_text_height 可调用")

print()
print("结果:", "全过" if not bad else ("失败 " + str(bad)))
os._exit(1 if bad else 0)
