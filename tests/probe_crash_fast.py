# -*- coding: utf-8 -*-
"""快速复现器：把 regress ⑨ 段的动作拆成几组，看哪一组之后连切主题色会崩。

用法：  probe_crash_fast.py N|A|B|C        （N=对照，什么都不做）
        A = 垃圾清理的 20 路 Tasks 并发扫描（打桩成秒级） + 填表
        B = 策略诊断扫描（打桩成 300 条） + 填表
        C = A + B
"""
import io
import os
import sys
import time

os.environ["WTB_NO_BROWSER"] = "1"
os.environ.setdefault("QT_QPA_PLATFORM", "windows")
G = (sys.argv[1] if len(sys.argv) > 1 else "N").upper()

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import faulthandler

faulthandler.enable()

import app as A
import server as S
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

A.settings_save = lambda *a, **k: None
A.confirm = lambda *a, **k: False
A.info = lambda *a, **k: None
A.warn = lambda *a, **k: None
A.safety_confirm = lambda *a, **k: False

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


def until(pred, sec=20.0):
    t0 = time.time()
    while time.time() - t0 < sec:
        qapp.processEvents()
        if pred():
            return True
        time.sleep(0.02)
    return bool(pred())


print(">> 组 %s | 平台 %s" % (G, qapp.platformName()))
sys.stdout.flush()

# 先把 17 个页面都走一遍（和 regress ① 段一样，页面树相同）
for key in ("overview", "optimize", "cputune", "power", "gpu", "software",
            "startup", "performance", "network", "dns", "cleanup", "tools",
            "hub", "regtool", "systool", "policies", "settings"):
    w.goto(key)
    pump(0.12)
print(">> 页面加载完成")
sys.stdout.flush()

cl = w.pages["cleanup"]
po = w.pages["policies"]

def _fake_targets(scale=None):
    return [{"id": "t%d" % i, "name": "T%d" % i, "group": "常规清理"}
            for i in range(20)]


def _fake_item(tid, scale=None):
    time.sleep(0.02)
    return {"id": tid, "name": "T%d" % tid, "level": "ok", "group": "常规清理",
            "what": "假项 %s" % tid, "min_age_hours": 24,
            "bytes": 1024, "files": 3, "size": "1 KB", "size_txt": "1 KB"}


if G in ("A", "C", "L"):
    # 垃圾清理：20 路并发 Tasks + 一次填表（把慢扫描打桩成瞬时）
    _jt, _jis = S.junk_targets, S.junk_item_size
    S.junk_targets, S.junk_item_size = _fake_targets, _fake_item
    rounds = 15 if G == "L" else 1
    for _r in range(rounds):
        w.goto("cleanup")
        cl.scan()
        until(lambda: getattr(cl, "_done_n", 0) >= 20, 25)
        pump(0.5)
        print(">>   第 %2d/%d 轮：表格 %d 行 | Tasks._live=%d（运行中 %d）"
              % (_r + 1, rounds, cl.table.rowCount(), len(A.Tasks._live),
                 len([t for t in list(A.Tasks._live) if t.isRunning()])))
        sys.stdout.flush()
    S.junk_targets, S.junk_item_size = _jt, _jis
    print(">> A/L 组完成：表格 %d 行 | 残留 Tasks._live=%d"
          % (cl.table.rowCount(), len(A.Tasks._live)))
    sys.stdout.flush()

if G == "M":
    # 大量顺序小任务：看是不是「累计任务数」本身出的问题
    import threading
    done = []
    for i in range(400):
        A.Tasks.run(w, lambda k=i: (time.sleep(0.001), k)[1],
                    lambda r: done.append(r))
        if i % 50 == 0:
            qapp.processEvents()
    until(lambda: len(done) >= 400, 60)
    pump(0.5)
    print(">> M 组完成：跑了 400 个任务，回来 %d 个 | Tasks._live=%d"
          % (len(done), len(A.Tasks._live)))
    sys.stdout.flush()

if G == "R":
    # 只做 ⑨ 段的真实动作（前面 ①~⑧ 全不做）—— 若这样就崩，就有一个 90 秒级复现器
    w.goto("cleanup")
    pump(0.5)
    cl.refresh()
    cl.scan()
    until(lambda: cl.table.rowCount() > 0, 90)
    pump(1.0)
    print(">> R: 垃圾扫描完成，表格 %d 行（_done_n=%s）"
          % (cl.table.rowCount(), getattr(cl, "_done_n", "?")))
    sys.stdout.flush()
    cl.update_sum()
    pump(0.4)
    cl.clean()                      # confirm 打了桩 → 直接返回，不真删
    pump(0.4)
    w.goto("policies")
    pump(0.3)
    po.refresh()
    po.scan()
    until(lambda: po.table.rowCount() > 0, 60)
    pump(1.0)
    print(">> R: 策略扫描完成，表格 %d 行" % po.table.rowCount())
    sys.stdout.flush()
    po.fix()
    pump(0.4)
    print(">> R: ⑨ 段的真实动作都做完了 | Tasks._live=%d" % len(A.Tasks._live))
    sys.stdout.flush()

if G == "S":
    # R 的对照：只做垃圾清理（不做策略）
    w.goto("cleanup")
    pump(0.5)
    cl.refresh()
    cl.scan()
    until(lambda: cl.table.rowCount() > 0, 90)
    pump(1.0)
    cl.update_sum()
    pump(0.4)
    cl.clean()
    pump(0.4)
    print(">> S: 只做垃圾清理，表格 %d 行" % cl.table.rowCount())
    sys.stdout.flush()

if G in ("B", "C"):
    # 策略诊断：打桩成 300 条
    _ps = S.policies_scan
    S.policies_scan = lambda *a, **k: [
        {"path": "HKLM\\Software\\Policies\\Fake\\%d" % i,
         "why": "假策略 %d" % i, "count": i + 1} for i in range(300)]
    w.goto("policies")
    po.scan()
    until(lambda: po.table.rowCount() > 0, 20)
    pump(1.0)
    S.policies_scan = _ps
    print(">> B 组完成：表格 %d 行" % po.table.rowCount())
    sys.stdout.flush()

print(">> 开始连切主题色")
sys.stdout.flush()
[w.set_accent(hx) for _n, hx in A.T.ACCENT_PRESETS]
pump(0.6)
print(">> 主题色 10 连切完成（未崩）")
sys.stdout.flush()
w.set_accent("")
pump(0.4)
w.toggle_theme(); pump(0.6); w.toggle_theme(); pump(0.6)
print(">> 组 %s 通过 —— 未复现崩溃" % G)
sys.stdout.flush()
w.hide()
os._exit(0)
