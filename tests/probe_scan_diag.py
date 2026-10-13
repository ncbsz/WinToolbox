# -*- coding: utf-8 -*-
"""诊断：限并发扫描为什么只跑了第一波（计数 _start_one / _one_done / _one_step）。"""
import os
import sys
import time

os.environ["WTB_NO_BROWSER"] = "1"
os.environ.setdefault("QT_QPA_PLATFORM", "windows")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import app as A
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

A.settings_save = lambda *a, **k: None
A.confirm = lambda *a, **k: False
A.info = lambda *a, **k: None

qapp = QApplication([])
w = A.MainWindow(settings={}, admin_mode=True, dark=False,
                 sysinfo=A.probe_system_info(qapp))
w.setAttribute(Qt.WA_DontShowOnScreen, True)
w.resize(1220, 820)
w.show()
t0 = time.time()
while time.time() - t0 < 1.2:
    qapp.processEvents()
    time.sleep(0.02)

cl = w.pages["cleanup"]
w.goto("cleanup")
cl.refresh()

cnt = {"start": 0, "done": 0, "step": 0, "finish": 0, "tokens": []}
_S, _D, _T, _F = cl._start_one, cl._one_done, cl._one_step, cl._finish


def start_one(token):
    cnt["start"] += 1
    cnt["tokens"].append(getattr(cl, "_scan_token", None))
    return _S(token)


def one_done(token, r, total):
    cnt["done"] += 1
    return _D(token, r, total)


def one_step(token, total):
    cnt["step"] += 1
    return _T(token, total)


def finish(token):
    cnt["finish"] += 1
    return _F(token)


cl._start_one, cl._one_done, cl._one_step, cl._finish = \
    start_one, one_done, one_step, finish

# 计时的假扫描器（0.05s/项），排除真实磁盘影响
_fake = {"n": 0}


def _fast(tid, sc=None):
    time.sleep(0.05)
    _fake["n"] += 1
    return None


_orig = A.S.junk_item_size
A.S.junk_item_size = _fast

cl.scan()
t0 = time.time()
while time.time() - t0 < 15:
    qapp.processEvents()
    time.sleep(0.02)
A.S.junk_item_size = _orig

print("计数器：start=%d done=%d step=%d finish=%d | 真正跑过的扫描=%d"
      % (cnt["start"], cnt["done"], cnt["step"], cnt["finish"], _fake["n"]))
print("扫描时状态：_done_n=%s | len(_pend)=%s | _scan_token=%s | _scan_total=%s"
      % (getattr(cl, "_done_n", "?"), len(getattr(cl, "_pend", []) or []),
         getattr(cl, "_scan_token", "?"), getattr(cl, "_scan_total", "?")))
print("start_one 收到的 token 序列:", cnt["tokens"])
print("Tasks._live=%d" % len(A.Tasks._live))
print("进度条文案:", cl.prog_lb.text())
w.hide()
os._exit(0)
