# -*- coding: utf-8 -*-
"""健壮性改动的定向验证：
  ⑤ Task 收尾（wait + deleteLater）：任务跑完后 _live 清空、无线程销毁告警
  ⑥ 垃圾扫描限并发：20+1 项都扫完入表，且并发峰值 ≤ 4
  ⑦ 字体校正防抖：10 连切主题色只触发有限次 fit_labels
"""
import os
import sys
import time

os.environ["WTB_NO_BROWSER"] = "1"
os.environ.setdefault("QT_QPA_PLATFORM", "windows")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import app as A
import server as S
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

A.settings_save = lambda *a, **k: None
A.confirm = lambda *a, **k: False
A.info = lambda *a, **k: None
A.warn = lambda *a, **k: None

OK, FAIL = 0, []


def chk(cond, label):
    global OK
    if cond:
        OK += 1
        print("  ok  %s" % label)
    else:
        FAIL.append(label)
        print("  FAIL %s" % label)


qapp = QApplication([])
info = A.probe_system_info(qapp)
w = A.MainWindow(settings={}, admin_mode=True, dark=False, sysinfo=info)
w.setAttribute(Qt.WA_DontShowOnScreen, True)
w.resize(1220, 820)
w.show()


def pump(sec=0.3):
    t0 = time.time()
    while time.time() - t0 < sec:
        qapp.processEvents()
        time.sleep(0.02)


def until(pred, sec=40.0):
    t0 = time.time()
    while time.time() - t0 < sec:
        qapp.processEvents()
        if pred():
            return True
        time.sleep(0.02)
    return bool(pred())


def _no_stale():
    """`_live` 里没有「已结束但还没收尾」的任务。

    ⚠️ 必须按条件轮询，不能点采样：任务线程刚结束、它的 `done` 事件还没派发时
    点一下就会误判（实测踩过一次）。
    """
    return all(t.isRunning() for t in A.Tasks._live)


# ---------------- ⑤ Task 收尾 ----------------
print("[⑤] Task 收尾（wait + deleteLater）")
done = []
mine = []
for i in range(24):
    mine.append(A.Tasks.run(w, lambda k=i: (time.sleep(0.02), k)[1],
                            lambda r: done.append(r)))
until(lambda: len(done) >= 24, 30)
pump(0.4)
chk(len(done) == 24, "24 个任务回调都回来了（实际 %d）" % len(done))
_still = [t for t in mine if t in A.Tasks._live]
chk(not _still,
    "自己的 24 个任务全部退出 _live（残留 %d；_live 里其余的是应用自身任务）"
    % len(_still))
chk(until(_no_stale, 5),
    "非运行态都已被收尾（_live=%d，运行中=%d）"
    % (len(A.Tasks._live),
       len([t for t in A.Tasks._live if t.isRunning()])))

# ---------------- ⑥ 垃圾扫描限并发 ----------------
print("[⑥] 垃圾扫描限并发（上限 4）")
live = {"n": 0, "max": 0}
_old_t, _old_i = S.junk_targets, S.junk_item_size


def _fake_targets(scale=None):
    return [{"id": "t%d" % i, "name": "T%d" % i, "group": "常规清理"}
            for i in range(20)]


def _fake_item(tid, scale=None):
    live["n"] += 1
    live["max"] = max(live["max"], live["n"])
    time.sleep(0.05)
    live["n"] -= 1
    return {"id": tid, "name": "T%s" % tid, "level": "ok", "group": "常规清理",
            "what": "x", "min_age_hours": 24, "bytes": 1024, "files": 3,
            "size": 1024, "size_txt": "1 KB",
            "path": "C:\\fake\\%s" % tid}


S.junk_targets, S.junk_item_size = _fake_targets, _fake_item
cl = w.pages["cleanup"]
w.goto("cleanup")
cl.refresh()
pump(0.3)
cl.scan()
ok = until(lambda: cl.table.rowCount() >= 20, 40)
pump(0.6)
S.junk_targets, S.junk_item_size = _old_t, _old_i
chk(ok, "扫描在 40s 内扫完（表格 %d 行，含 recyclebin 共 21 项）" % cl.table.rowCount())
chk(live["max"] <= 4, "并发峰值 ≤ 4（实际 %d）" % live["max"])
chk(until(_no_stale, 5),
    "扫描后无「已结束但未收尾」的任务（_live=%d，运行中=%d）"
    % (len(A.Tasks._live),
       len([t for t in A.Tasks._live if t.isRunning()])))

# ---------------- ⑦ 字体校正防抖 ----------------
print("[⑦] 字体校正防抖（10 连切主题色）")
# ⚠️ 数的是「整树 pass 次数」：一次 `_refit_fonts` = 窗口 1 次 + 17 个页面各 1 次
#    `fit_labels`。所以只统计 root 是窗口的那些调用。
calls = {"n": 0}
_old_fit = A.fit_labels


def _counting_fit(root):
    if root is w:
        calls["n"] += 1
    return _old_fit(root)


A.fit_labels = _counting_fit
[w.set_accent(hx) for _n, hx in A.T.ACCENT_PRESETS]
pump(1.0)
A.fit_labels = _old_fit
chk(calls["n"] <= 4,
    "10 连切只跑 ≤4 轮整树字体校正（实际 %d；防抖前每次调用排两轮 → 约 20 轮）"
    % calls["n"])
chk(until(_no_stale, 5),
    "主题色连切后无「已结束但未收尾」的任务（_live=%d，运行中=%d）"
    % (len(A.Tasks._live),
       len([t for t in A.Tasks._live if t.isRunning()])))

print()
print("结果：%s（ok %d / fail %d）"
      % ("全通过" if not FAIL else "有失败", OK, len(FAIL)))
for f in FAIL:
    print("   -", f)
sys.stdout.flush()
w.hide()
os._exit(0 if not FAIL else 1)
