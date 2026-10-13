# -*- coding: utf-8 -*-
"""「清理与诊断」大类的验证：
  · 侧栏只剩 13 项（4 个页收进大类，不再单独占位）
  · 4 个页仍然存在、仍然可达（stack 下标对齐）
  · 点格子能进对应页，且侧栏高亮停在大类上（不出现"整列没高亮"）
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication, QFrame            # noqa: E402
import app as A                                                # noqa: E402

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


w = A.MainWindow(admin_mode=True, dark=False, sysinfo=None)
NAV = w.NAV
KEYS = [k for k, _, _ in NAV]

print("[1] NAV 结构")
print("      NAV 共 %d 项：%s" % (len(NAV), KEYS))
chk("hub" in KEYS, "NAV 里有 hub（清理与诊断）")
for k in ("cleanup", "regtool", "systool", "policies"):
    chk(k in KEYS, "%s 仍留在 NAV 里（stack 下标要对齐）" % k)
chk(KEYS.index("hub") < KEYS.index("regtool"),
    "hub 排在隐藏页之前（顺序合理）")

print()
print("[2] 侧栏只剩可见项")
n_btn = len(w.group.buttons())
chk(n_btn == len(NAV) - len(w.HIDDEN_NAV),
    "侧栏按钮 %d 个 = NAV %d - 隐藏 %d"
    % (n_btn, len(NAV), len(w.HIDDEN_NAV)))
texts = [b.text() for b in w.group.buttons()]
print("      侧栏：", texts)
for nm in ("垃圾清理", "注册表清理", "系统组件", "策略诊断"):
    chk(nm not in texts, "侧栏不再单独出现「%s」" % nm)
chk("清理与诊断" in texts, "侧栏出现「清理与诊断」")

print()
print("[3] stack 与 NAV 下标一一对应")
chk(w.stack.count() == len(NAV),
    "stack %d 页 = NAV %d 项" % (w.stack.count(), len(NAV)))
ok_all = True
for i, (k, _n, _c) in enumerate(NAV):
    if w.stack.widget(i) is None or w.pages.get(k) is None:
        ok_all = False
        print("      ✗ 第 %d 项 %s 没有对应页" % (i, k))
chk(ok_all, "每个 NAV 项都有对应的 page 与 stack 页")

print()
print("[4] 4 个页仍然可达（goto 能切过去）")
for k, nm in (("cleanup", "垃圾清理"), ("regtool", "注册表清理"),
              ("systool", "系统组件"), ("policies", "策略诊断")):
    w.goto(k)
    qapp.processEvents()
    idx = KEYS.index(k)
    same = w.stack.currentIndex() == idx
    title = w.title_lb.text()
    print("      goto(%-9s) -> stack=%d(期望 %d) 标题=%r" % (k, w.stack.currentIndex(), idx, title))
    chk(same, "%s 切到了对应页" % nm)
    chk(title == nm, "%s 顶部标题显示自己的名字（%r）" % (nm, title))
    # 侧栏高亮应停在 hub
    hub_i = KEYS.index("hub")
    chk(w.group.button(hub_i).isChecked(),
        "%s：侧栏高亮停在「清理与诊断」" % nm)

print()
print("[5] 回到大类")
w.goto("hub")
qapp.processEvents()
chk(w.stack.currentIndex() == KEYS.index("hub"), "回到 hub 页")
hub = w.pages["hub"]
cards = hub.findChildren(QFrame, "HubCard")
print("      格子数：", len(cards))
chk(len(cards) == 4, "有 4 个格子（实际 %d）" % len(cards))

print()
print("[6] 点格子真的会跳页")
targets = [t[0] for t in hub.TOOLS]
chk(targets == ["cleanup", "regtool", "systool", "policies"],
    "格子顺序与 TOOLS 一致：%s" % targets)
for i, key in enumerate(targets):
    w.goto("hub")
    qapp.processEvents()
    # 直接调卡片上挂的处理函数（模拟点击）
    cards = hub.findChildren(QFrame, "HubCard")
    cards[i].mousePressEvent(None)
    qapp.processEvents()
    chk(w.stack.currentIndex() == KEYS.index(key),
        "点第 %d 个格子 -> 进入 %s" % (i + 1, key))

print()
print("[7] 每个格子都有标题与说明（不是空框）")
w.goto("hub")
qapp.processEvents()
from PySide6.QtWidgets import QLabel                            # noqa: E402
titles = [lb.text() for lb in hub.findChildren(QLabel, "HubTitle")]
descs = [lb.text() for lb in hub.findChildren(QLabel, "HubDesc")]
print("      标题：", titles)
chk(len(titles) == 4 and all(titles), "4 个格子都有标题")
chk(len(descs) == 4 and all(len(d) > 10 for d in descs), "4 个格子都有说明文字")
chk(set(titles) == {"垃圾清理", "注册表清理", "系统组件", "策略诊断"},
    "标题就是那 4 个工具名")

print()
print("[8] goto_index（侧栏点击）id 映射没被跳过逻辑打乱")
for i, (k, _n, _c) in enumerate(NAV):
    if k in w.HIDDEN_NAV:
        continue
    w.goto_index(i)
    qapp.processEvents()
    ok = w.stack.currentIndex() == i
    if not ok:
        print("      ✗ id=%d (%s) -> stack=%d" % (i, k, w.stack.currentIndex()))
    chk(ok, "侧栏 id=%d -> %s" % (i, k))

print()
print("[9] 二级页都要有「返回」入口（用户要求）")
# ⚠️ 用 isHidden() 而不是 isVisible()：isVisible() 要求整条祖先链都在屏上，
#    WA_DontShowOnScreen 下恒 False（会把「已显示」误判成「没显示」）。
chk(hasattr(w, "btn_back_page"), "顶部栏有 btn_back_page")
chk(hasattr(w, "go_back"), "有 go_back 方法")
# 一级页不该显示返回
w.goto("overview")
qapp.processEvents()
chk(w.btn_back_page.isHidden(), "一级页（概览）不显示返回按钮")
w.goto("hub")
qapp.processEvents()
chk(w.btn_back_page.isHidden(), "大类页自己不显示返回按钮")
# 二级页要显示，且文案写明回哪
for key, nm in (("cleanup", "垃圾清理"), ("regtool", "注册表清理"),
                ("systool", "系统组件"), ("policies", "策略诊断")):
    w.goto(key)
    qapp.processEvents()
    vis = not w.btn_back_page.isHidden()
    txt = w.btn_back_page.text()
    print("      %-10s 返回按钮：可见=%s 文案=%r" % (nm, vis, txt))
    chk(vis is True, "%s 显示返回按钮" % nm)
    chk("返回" in txt and "清理与诊断" in txt,
        "%s 的按钮写明回到「清理与诊断」（%r）" % (nm, txt))

print()
print("[10] 点返回真的回到大类")
for key, nm in (("cleanup", "垃圾清理"), ("regtool", "注册表清理"),
                ("systool", "系统组件"), ("policies", "策略诊断")):
    w.goto(key)
    qapp.processEvents()
    w.go_back()
    qapp.processEvents()
    ok = w.stack.currentIndex() == KEYS.index("hub")
    chk(ok, "%s 点返回 -> 回到「清理与诊断」" % nm)

print()
print("[11] 返回按钮在二级页/一级页之间切换正确（不会残留在别的页）")
w.goto("systool")
qapp.processEvents()
chk(not w.btn_back_page.isHidden(), "在系统组件页：显示")
w.goto("settings")
qapp.processEvents()
chk(w.btn_back_page.isHidden(), "切到设置页：隐藏（不残留）")
w.goto("systool")
qapp.processEvents()
chk(not w.btn_back_page.isHidden(), "再回系统组件：又显示")

print()
print("[12] 系统优化的大类：一行三个横排（用户要求，与 hub 卡片同款）")


def _pump(secs):
    t0 = time.time()
    while time.time() - t0 < secs:
        qapp.processEvents()
        time.sleep(0.03)


w.goto("optimize")
_pump(3.0)
pg = w.pages["optimize"]
g = pg.cat_grid
n = g.count()
chk(n >= 3, "大类卡片已构建（%d 个）" % n)

_pos = []
for k in range(n):
    it = g.itemAt(k)
    if it.widget() is None:
        continue
    r, c, _rs, _cs = g.getItemPosition(k)
    _pos.append((r, c, it.widget()))

chk(sorted(set(c for _r, c, _w in _pos)) == [0, 1, 2],
    "用满 3 列（实际列：%s）" % sorted(set(c for _r, c, _w in _pos)))
chk([c for r, c, _w in _pos if r == 0] == [0, 1, 2],
    "第一行正好三个（实际 %s）" % [c for r, c, _w in _pos if r == 0])
chk(all(r == i // 3 and c == i % 3 for i, (r, c, _w) in enumerate(_pos)),
    "位置一律 i//3 行、i%3 列（没有错位）")
chk(len(set(r for r, _c, _w in _pos)) == (len(_pos) + 2) // 3,
    "行数 = ceil(卡片数 / 3)（%d 个卡片 -> %d 行）"
    % (len(_pos), (len(_pos) + 2) // 3))

_ws = [wt.width() for _r, _c, wt in _pos]
_hs = [wt.height() for _r, _c, wt in _pos]
chk(max(_ws) - min(_ws) <= 2, "三列等宽（宽度 %s..%s）" % (min(_ws), max(_ws)))
chk(max(_hs) - min(_hs) <= 2, "同一行等高（高度 %s..%s）" % (min(_hs), max(_hs)))
chk(all(wt.objectName() == "CatCard" for _r, _c, wt in _pos),
    "卡片都用 CatCard 样式（沿用既有 QSS，没新造样式）")

# 竖排卡片：顶行有 图标 / 名称 / ›，下面有「N 项 · 已生效 M」
from PySide6.QtWidgets import QLabel                             # noqa: E402

_c0 = _pos[0][2]
_names = [x.objectName() for x in _c0.findChildren(QLabel)]
chk("CatIcon" in _names and "CatName" in _names and "CatArrow" in _names,
    "卡片含 图标 / 名称 / 箭头（%s）" % _names)
chk(any("项 · 已生效" in x.text() for x in _c0.findChildren(QLabel)),
    "卡片显示「N 项 · 已生效 M」")

# 点击仍然进得去、回得来
_pos[0][2].mousePressEvent(None)
_pump(1.2)
chk(pg.current_cat is not None and not pg.item_scroll.isHidden(),
    "点第一个卡片能进入大类（%s）" % pg.current_cat)
pg.go_root()
_pump(0.8)
chk(pg.current_cat is None and not pg.cat_scroll.isHidden(),
    "返回后回到大类网格")
chk(g.count() == n, "返回后卡片数不变（没重复添加）")

w.hide()
w.deleteLater()

print()
print("=" * 58)
print("结果：%s（ok %d / fail %d）" % ("全通过" if not FAIL else "有失败",
                                      OK, len(FAIL)))
for f in FAIL:
    print("   -", f)
sys.stdout.flush()
os._exit(0 if not FAIL else 1)