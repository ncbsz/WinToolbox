# -*- coding: utf-8 -*-
"""二分定位 regress_all 的崩溃：跑到指定段**之前**，再复现 ⑩ 的「连切主题色」。

用法（参数 = 要停在它**之前**的那一段）：
    probe_crash_bisect.py ⑩     → 跑 ①~⑨ 后立刻做主题色压力（最接近真实崩点）
    probe_crash_bisect.py ⑤     → 跑 ①~④b 后压力
    probe_crash_bisect.py ③     → 跑 ①~② 后压力
    probe_crash_bisect.py ①     → 不跑任何段，直接压力
    probe_crash_bisect.py 'MARK:po = w.pages["policies"]'
                              → 任意源码标记切点（切在它之前，用来切进某段内部）

压力块跑 3 轮：第一轮不崩的话后两轮继续 —— 一次运行就能区分
「必然崩」（第一轮就挂）与「偶发崩」（要到第 2/3 轮或看运气）。

原理：把 `regress_all.py` 的**真实源码**截断再 exec —— 跑的就是原版代码，
不存在「我抄了一遍、抄错了」的可能。
"""
import io
import os
import sys

STOP_AT = sys.argv[1] if len(sys.argv) > 1 else "⑩"

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
SRC = os.path.join(HERE, "regress_all.py")
sys.path.insert(0, PROJ)

src = io.open(SRC, encoding="utf-8").read()
if STOP_AT.startswith("MARK:"):
    # 任意标记切点：直接给一段源码子串，切在它**之前**（用来切进某一段内部）
    marker = STOP_AT[5:]
    cut = src.find(marker)
    assert cut > 0, "找不到标记 %r" % marker
else:
    marker = 'print("%s ' % STOP_AT
    cut = src.find(marker)
    assert cut > 0, "找不到段标题 %r" % marker
body = src[:cut]

# BISECT_NO_DIALOG=1：把 confirm_ex 也打桩（返回「取消」）——⑨ 段 clean()
# 就走不到创建真实 QMessageBox 那条路，用来反证「对话框是不是崩点必需条件」。
if os.environ.get("BISECT_NO_DIALOG") == "1":
    _anchor = "A.ResidualDialog = _FakeResidual"
    assert _anchor in body, "找不到打桩锚点"
    body = body.replace(
        _anchor,
        _anchor + "\nA.confirm_ex = lambda *a, **k: (False, False)", 1)
    print(">> [bisect] 已注入 confirm_ex 打桩（不再创建真实对话框）")
    sys.stdout.flush()

GC = os.environ.get("BISECT_GC") == "1"
ROUNDS = int(os.environ.get("BISECT_ROUNDS", "3"))
# 标记里可能带引号（切点写的是源码片段）→ 生成代码前先消毒
LABEL = STOP_AT.replace(chr(34), chr(39))

_body = (
    '\n'
    '# ---------------- 复现 ⑩ 的崩点 ----------------\n'
    'import sys as _s, gc, faulthandler\n'
    'faulthandler.enable()\n'
    + 'print(">> [bisect] 停在 ' + LABEL + ' 之前；BISECT_GC=' + str(GC) + '")\n'
    'print(">> [bisect] Tasks._live =", len(A.Tasks._live), "| 运行中 =",\n'
    '      len([t for t in list(A.Tasks._live) if t.isRunning()]))\n'
    '_s.stdout.flush()\n'
    # 压力前状态：每页控件数 / 标签总数 / 图标缓存 / QSS 长度
    '_nw = 0\n'
    '_nl = 0\n'
    'from PySide6.QtWidgets import QLabel as _QL\n'
    'from PySide6.QtWidgets import QWidget as _QW\n'
    'for _pg in w.pages.values():\n'
    '    _nw += len(_pg.findChildren(_QW))\n'
    '    _nl += len(_pg.findChildren(_QL))\n'
    '_nw += len(w.findChildren(_QW)); _nl += len(w.findChildren(_QL))\n'
    'import theme as _T\n'
    'from PySide6.QtWidgets import QMessageBox as _QM\n'
    '_cl = w.pages["cleanup"]\n'
    '_it = getattr(_cl, "items", []) or []\n'
    '_sz = [x for x in _it if (x.get("size") or 0) > 0]\n'
    '_nmb = len(w.findChildren(_QM))\n'
    'print(">> [bisect] 状态：控件 %d | 标签 %d | 图标缓存 %d | QSS %d 字符"\n'
    '      % (_nw, _nl, len(_T._ICON_CACHE), len(_T.build_qss(w.dark, w.scale, w.font_base))))\n'
    'print(">> [bisect] cleanup：items=%d（size>0 %d 个）| 树里 QMessageBox=%d"\n'
    '      % (len(_it), len(_sz), _nmb))\n'
    '_s.stdout.flush()\n')
if GC:
    # 若崩在 collect 这一行 → 就是「QThread 对象被 GC 回收」的时点问题
    _body += ('print(">> [bisect] 先强制 gc.collect()（崩在这里 = QThread 回收时点问题）")\n'
              '_s.stdout.flush()\n'
              'n_ = gc.collect()\n'
              'print(">> [bisect] gc 回收了", n_, "个对象，没崩")\n'
              '_s.stdout.flush()\n')
_body += (
    'for _rd in range(1, ' + str(ROUNDS) + ' + 1):\n'
    '    print(">> [bisect] ---- 压力第 %d/%d 轮 ----" % (_rd, ' + str(ROUNDS) + '))\n'
    '    _s.stdout.flush()\n'
    '    [w.set_accent(hx) for _n, hx in A.T.ACCENT_PRESETS]\n'
    '    pump(0.6)\n'
    '    print(">> [bisect] 主题色 10 连切完成（未复现）")\n'
    '    _s.stdout.flush()\n'
    '    w.set_accent("")\n'
    '    pump(0.4)\n'
    '    w.toggle_theme(); pump(0.6); w.toggle_theme(); pump(0.6)\n'
    '    print(">> [bisect] 主题来回切换完成（未复现）")\n'
    '    _s.stdout.flush()\n'
    '    w.resize(1000, 700); pump(0.4); w.resize(1240, 840); pump(0.4)\n'
    '    print(">> [bisect] 第 %d 轮全部通过" % _rd)\n'
    '    _s.stdout.flush()\n'
    'print(">> [bisect] ' + str(ROUNDS) + ' 轮压力全过 —— 本次未复现崩溃")\n'
    '_s.stdout.flush()\n'
    'os._exit(0)\n')
body += _body

print(">> [bisect] 平台 = %s | 截断到 %s 之前，共 %d 字符"
      % (os.environ.get("QT_QPA_PLATFORM", "(脚本内定)"), STOP_AT, len(body)))
sys.stdout.flush()
exec(compile(body, "regress_all[trunc@%s]" % STOP_AT, "exec"),
     {"__name__": "__main__", "__file__": SRC})   # 原脚本用 __file__ 定位工程根
