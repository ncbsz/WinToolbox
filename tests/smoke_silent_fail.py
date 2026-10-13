# -*- coding: utf-8 -*-
"""「读取失败不能静默」—— 把 6 处一次性读取接上可见出口，8 处刻意静默显式标注。

背景：项目铁律「**失败 ≠ 没有 / 读不到 ≠ 正常**」。后台读取失败时若静默留空，
用户看到的是「—」或一张空表，分不清是「确实没有」还是「读不出来」。
这个坑犯过好几次（备份列表、计划任务、服务状态…）。

本轮把 14 个静默点逐个读源码 + 调用点分类：
  · **刻意静默 8 处**（都加了 `# 刻意静默` 标记）
      - 1 秒轮询 ×6：概览 perf_stats / 概览 net_info / 概览 cpu_per_core /
        概览 cpu_temp / 内存与性能 tick_perf / tick_net
        （弹提示会变成每秒一次骚扰）
      - 预热 ×1：sys_basic 的结果本来就丢弃
      - 检查更新 ×1：docstring 写明「失败零打扰」
  · **真正要修 6 处**：失败时写进各自页面的状态标签
"""
import io
import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication            # noqa: E402
import app as A                                        # noqa: E402

A.settings_save = lambda *a, **k: None
qapp = QApplication.instance() or QApplication(sys.argv)

OK, FAIL = 0, []


def chk(cond, label):
    global OK
    if cond:
        OK += 1
        print("  ok  %s" % label)
    else:
        FAIL.append(label)
        print("  FAIL %s" % label)


_src = io.open(A.__file__, encoding="utf-8").read()
_lines = _src.splitlines()


def _site_lines():
    return [i + 1 for i, l in enumerate(_lines)
            if "on_fail=lambda *_: None" in l]


def _marked_lines():
    return [i + 1 for i, l in enumerate(_lines)
            if "on_fail=lambda *_: None" in l and "# 刻意静默" in l]


print("[1] 分类台账：所有静默点都必须显式声明「刻意」")
sil, mk = _site_lines(), _marked_lines()
for n in sil:
    print("      L%-6d %s" % (n, "# 刻意静默" if n in mk else "**未标记**"))
print("      合计 %d 个静默点，已标记 %d" % (len(sil), len(mk)))
chk(sil == mk,
    "**没有任何「未标记的静默点」**（未标记的：%s）"
    % (sorted(set(sil) - set(mk)) or "无"))
chk(len(sil) <= 8,
    "静默点已从 14 降到 %d（新增的必须写明为什么可以静默）" % len(sil))

print()
print("[2] 6 处一次性读取现在都有可见出口")
# 每处应当是「on_fail 里调 load_fail」—— 按调用点附近 6 行内查
CASES = [
    (3014 - 20, 'Tasks.run_cached(self, "hw"', "硬件信息", "lb_hw_fail"),
    (5429 - 20, "process_sched_state", "调度状态", "lb_sched"),
    (5588 - 20, 'Tasks.run_cached(self, "plans"', "电源计划列表", "lb_plan_state"),
    (5763 - 20, "pstates:", "电源计划的参数", "lb_plan_state"),
    (6048 - 20, 'Tasks.run_cached(self, "gpus"', "显卡列表", "lb_state"),
    (9333 - 20, "context_menu_disabled_list", "已禁用项列表", "lb_cm_off"),
]
for _ln, anchor, what, target in CASES:
    # 全文件搜这个锚点，取其后的调用块
    i = _src.find(anchor)
    seg = _src[i:i + 420] if i > 0 else ""
    has_lf = "self.load_fail(" in seg and target in seg
    print("      %-34s -> load_fail(%s)  %s"
          % (anchor[:32], target, "✓" if has_lf else "**缺**"))
    chk(i > 0, "找得到调用点 %s" % anchor[:34])
    chk(has_lf, "%s：失败时写进 %s" % (what, target))

print()
print("[3] Page.load_fail 的行为")
chk("def load_fail" in _src, "Page 上有 load_fail")
_win = A.MainWindow(admin_mode=True, dark=False, sysinfo=None)
try:
    pg = _win.pages["gpu"]
    before = pg.lb_state.text()
    pg.load_fail(pg.lb_state, "显卡列表", "WMI 超时")
    after = pg.lb_state.text()
    print("      %r -> %r" % (before, after))
    chk("读取失败" in after, "写的是「读取失败：…」而不是留空")
    chk("显卡列表" in after and "WMI 超时" in after,
        "带上了「读什么」和原因")
    chk("color" in pg.lb_state.styleSheet(), "标了警示色（%s）"
        % pg.lb_state.styleSheet()[:28])
    # 不给 label 时不能崩（有些调用点可能没有对应控件）
    pg.load_fail(None, "x")
    chk(True, "label 为 None 时不崩")
    # 失败信息不该把成功文案永久污染：成功路径会重写它
    print()
    print("[4] 目标标签确实存在（不是拼错的属性名）")
    for pname, attr in (("overview", "lb_hw_fail"), ("regtool", "lb_cm_off"),
                        ("cputune", "lb_sched"), ("power", "lb_plan_state"),
                        ("gpu", "lb_state")):
        got = hasattr(_win.pages[pname], attr)
        chk(got, "%s.%s 存在" % (pname, attr))
    ov = _win.pages["overview"]
    chk(ov.lb_hw_fail.isVisible() is False,
        "lb_hw_fail 默认隐藏（成功时不占视觉位置）")
finally:
    _win.hide()
    _win.deleteLater()

print()
print("[5] 刻意静默的 8 处各自有合理的理由（标记文字能看出原因）")
_marks = [l for l in _lines
          if "on_fail=lambda *_: None" in l and "# 刻意静默" in l]
_why = {}
for l in _marks:
    reason = l.split("# 刻意静默", 1)[1].lstrip("：:").strip()
    _why[reason] = _why.get(reason, 0) + 1
for r, n in sorted(_why.items()):
    print("      %-34s ×%d" % (r, n))
chk(any("轮询" in r for r in _why), "轮询类的理由写清楚了")
chk(any("预热" in r for r in _why), "预热类的理由写清楚了")
chk(any("打扰" in r or "零打扰" in r for r in _why),
    "检查更新类的理由写清楚了")
chk(all(len(r) >= 4 for r in _why), "每个标记都写了理由（不是空标记）")

print()
print("[6] 回归：这 6 处失败后**不能**再是原来的空白状态")
# 用假数据把 load_fail 走一遍，确认 6 个目标都能显示
_win2 = A.MainWindow(admin_mode=True, dark=False, sysinfo=None)
try:
    for pname, attr, what in (("overview", "lb_hw_fail", "硬件信息"),
                              ("regtool", "lb_cm_off", "已禁用项列表"),
                              ("cputune", "lb_sched", "该进程的调度状态"),
                              ("power", "lb_plan_state", "电源计划列表"),
                              ("gpu", "lb_state", "显卡列表")):
        pg = _win2.pages[pname]
        lb = getattr(pg, attr)
        pg.load_fail(lb, what, "模拟失败")
        t = lb.text()
        ok = ("读取失败" in t and what in t)
        print("      %-22s %r" % ("%s.%s" % (pname, attr), t))
        chk(ok, "%s.%s 能显示失败" % (pname, attr))
finally:
    _win2.hide()
    _win2.deleteLater()

print()
print("=" * 58)
print("结果：%s（ok %d / fail %d）" % ("全通过" if not FAIL else "有失败",
                                      OK, len(FAIL)))
for f in FAIL:
    print("   -", f)
sys.stdout.flush()
os._exit(0 if not FAIL else 1)