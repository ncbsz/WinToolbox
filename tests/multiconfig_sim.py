# -*- coding: utf-8 -*-
"""多配置模拟：不同逻辑尺寸 × 深浅主题，逐页加载 + 布局/胶囊条/自动刷新检查。

手工触发（与 regress_all / audit_features 一样，不自动跑）：
    python tests/multiconfig_sim.py

关键约束（项目铁律）：
- **每档配置都新建窗口** —— fit_labels 只抬 minimumHeight、从不回落，
  同一窗口里先小后大地 resize 会污染高度测量。
- **绝不 close() 窗口** —— close_action=ask 会弹模态对话框卡死脚本，
  close_action=exit 会触发 settings_save 把用户配置覆盖成测试值。
  收尾用 hide + deleteLater，并把 settings_save 打桩成空操作双保险。
- 结果同时落文件（stdout 在部分环境会被吞）。
"""
import os
import sys
import time

os.environ["WTB_NO_BROWSER"] = "1"
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import app as A
from PySide6.QtWidgets import QApplication, QLabel, QProgressBar

A.settings_save = lambda *a, **k: None          # 铁律：测试脚本绝不写用户配置

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = open(os.path.join(HERE, "_multiconfig_result.txt"), "w", encoding="utf-8")
RESULTS = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    OUT.write(s + "\n")
    OUT.flush()


qapp = QApplication([])
info = A.probe_system_info(qapp)
PAGES = [k for k, _, _ in A.MainWindow.NAV]

CONFIGS = [
    ("1366x728 (100%)", 1366, 728),
    ("1600x860 (125%)", 1600, 860),
    ("1920x1000(100%)", 1920, 1000),
    ("1280x690 (150%)", 1280, 690),
    ("2560x1300(150%)", 2560, 1300),
]


def pump(sec=0.0, n=30):
    qapp.processEvents()
    if sec:
        t0 = time.time()
        while time.time() - t0 < sec:
            qapp.processEvents()
            time.sleep(0.03)
    for _ in range(n):
        qapp.processEvents()


def check(tag, fn):
    try:
        fn()
        RESULTS.append((tag, "PASS", ""))
        log("  [PASS] %s" % tag)
        return True
    except Exception as e:
        RESULTS.append((tag, "FAIL", "%s: %s" % (type(e).__name__, e)))
        log("  [FAIL] %s -> %s: %s" % (tag, type(e).__name__, e))
        return False


def need(cond, msg):
    if not cond:
        raise AssertionError(msg)


def clipped_labels(page):
    """文字有内容、但高度明确矮于一行 → 判为裁切。"""
    bad = []
    for lb in page.findChildren(QLabel):
        t = lb.text()
        if not t or t == "—" or lb.parent() is None:
            continue
        h = lb.height()
        if h and h < lb.fontMetrics().height():
            bad.append((t[:16], h, lb.fontMetrics().height()))
    return bad


for label, W, H in CONFIGS:
    for dark in (False, True):
        theme = "深色" if dark else "浅色"
        tag0 = "%s %s" % (label, theme)
        log("=" * 60)
        log("配置：%s" % tag0)
        w = A.MainWindow(settings={"close_action": "ask", "check_update": False,
                                   "update_checked_on": time.strftime("%Y-%m-%d")},
                         admin_mode=True, dark=dark, sysinfo=info)
        w.resize(W, H)
        w.show()
        pump(0.6)

        for key in PAGES:
            def one(k=key):
                w.goto(k)
                pump(0.35)
                p = w.pages.get(k)
                need(p is not None, "页面 %s 不存在" % k)
                p.refresh()
                pump(0.25)
            check("%s · 页 %s" % (tag0, key), one)

        def ov_fit():
            w.goto("overview")
            pump(0.6)
            mx = w.scrolls["overview"].verticalScrollBar().maximum()
            need(mx == 0, "概览页出现滚动条 max=%d" % mx)
        check("%s · 概览一屏(max=0)" % tag0, ov_fit)

        def bars():
            w.goto("overview")
            pump(0.4)
            bs = w.pages["overview"].findChildren(QProgressBar)
            need(len(bs) > 0, "概览页没有进度条")
            bad = [b.height() for b in bs if b.height() != 8]
            need(not bad, "存在非 8px 进度条高度：%s" % bad)
        check("%s · 进度条胶囊(8px)" % tag0, bars)

        def clip():
            worst = []
            for k in PAGES[:6]:
                w.goto(k)
                pump(0.3)
                bad = clipped_labels(w.pages[k])
                if bad:
                    worst += ["%s:%s" % (k, b) for b in bad[:2]]
            need(not worst, "标签裁切：%s" % worst[:4])
        check("%s · 标签零裁切" % tag0, clip)

        def auto_proc():
            w.goto("performance")
            pump(2.6)
            need(w.pages["performance"].table.rowCount() > 0, "进程表未自动刷新（0 行）")
        check("%s · 进程表自动刷新" % tag0, auto_proc)

        # 不 close()：见文件头铁律
        w.hide()
        w.deleteLater()
        pump(0.2)

log("")
npass = sum(1 for _, s, _ in RESULTS if s == "PASS")
nfail = sum(1 for _, s, _ in RESULTS if s == "FAIL")
log("合计：%d/%d 通过，%d 失败" % (npass, len(RESULTS), nfail))
if nfail:
    log("失败项：")
    for t, s, m in RESULTS:
        if s == "FAIL":
            log("   - %s -> %s" % (t, m))
OUT.close()
os._exit(1 if nfail else 0)
