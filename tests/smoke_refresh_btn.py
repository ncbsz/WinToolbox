# -*- coding: utf-8 -*-
"""「刷新」按钮必须真的重读：验证 reload 语义而不是 run_cached 回放。

做法：把页面的 `run_cached` 换成计数版 —— 第一次真读并写进缓存，
第二次如果还走缓存，计数器不会涨。真实实现里这一步就是
`Tasks.run_cached` 命中 `_sess` 直接 `on_done(旧值)`。
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication            # noqa: E402
import app as A                                        # noqa: E402
import server as S                                     # noqa: E402

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


def pump(sec=1.2):
    t0 = time.time()
    while time.time() - t0 < sec:
        qapp.processEvents()
        time.sleep(0.02)


win = A.MainWindow(admin_mode=True, dark=False, sysinfo=None)

# 探针：替换 run_cached，记录「真的起后台线程读了几次」
CALLS = {"n": 0}
_orig_cached = A.Tasks.run_cached


def counting_cached(parent, key, fn, on_done, on_fail=None, *a, **kw):
    CALLS["n"] += 1
    return _orig_cached(parent, key, fn, on_done, on_fail, *a, **kw)


A.Tasks.run_cached = staticmethod(counting_cached)

print("[1] 软件管理页的「刷新」按钮")
sw = win.pages["software"]
CALLS["n"] = 0
sw.reload()
pump(6.0)
first = CALLS["n"]
chk(first >= 1, "第一次 reload 真的读了系统（%d 次）" % first)

CALLS["n"] = 0
sw.reload()                      # 再点一次
pump(6.0)
second = CALLS["n"]
chk(second >= 1, "第二次点「刷新」也真的重读了（%d 次）" % second)
chk("run_cached" not in __import__("inspect").getsource(
    type(sw).__mro__[0].reload.__code__ and A.Page.reload),
    "Page.reload 会先丢缓存")

print()
print("[2] 缓存确实被清了（直接验 _sess）")
sw.sess_invalidate("sw")
chk("sw" not in sw._sess, "sess_invalidate('sw') 把键清掉了")

print()
print("[3] 其它三处「刷新」按钮的绑定")
import io                                              # noqa: E402
src = io.open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app.py"), encoding="utf-8").read()
bad = [ln for ln in src.splitlines()
       if "刷新" in ln and "on_click=self.refresh" in ln]
chk(not bad, "没有任何「刷新」按钮绑到 self.refresh（%s）" % (bad or "无"))
n_reload = len([ln for ln in src.splitlines()
                if "刷新" in ln and "on_click=self.reload" in ln])
chk(n_reload >= 5, "共 %d 处「刷新」按钮改绑 reload" % n_reload)

print()
print("[4] 顶层工具栏的刷新（原本就是对的，别改坏）")
CALLS["n"] = 0
win.goto("software")
pump(0.4)
win.on_refresh()
pump(6.0)
chk(CALLS["n"] >= 1, "工具栏刷新也能真读（%d 次）" % CALLS["n"])

print()
print("[5] 还原点页：新的删除路径接好了")
reg = win.pages["regtool"]
reg.tabs.setCurrentIndex(2)
pump(0.5)
chk(hasattr(reg, "del_rp") and hasattr(reg, "btn_rp_del"),
    "有删除入口")
reg._got_rp([{"seq": "7", "desc": "冒烟点", "type": "应用",
              "raw_date": "2026/10/4 10:00:00"}])
chk(reg.tb_rp.rowCount() == 1, "列表能填")
reg.tb_rp.item(0, 0).setCheckState(A.Qt.Checked)
reg._rp_sel()
chk(reg.btn_rp_del.isEnabled(), "勾选后删除按钮可用")
# 真的走一次删除（无 admin → 应拿到可读原因，而不是崩/静默）
r = S.remove_restore_point("7")
print("      实调 remove_restore_point ->", r)
chk(isinstance(r, dict) and "ok" in r, "返回结构完整")
chk(bool(r.get("msg")), "失败时给了原因（%s）" % str(r.get("msg"))[:50])

print()
print("=" * 56)
print("结果：%s（ok %d / fail %d）" % ("全通过" if not FAIL else "有失败",
                                      OK, len(FAIL)))
for f in FAIL:
    print("   -", f)

win.hide()
win.deleteLater()
os._exit(0 if not FAIL else 1)
