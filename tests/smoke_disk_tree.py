# -*- coding: utf-8 -*-
"""磁盘占用「惰性树」的验证：只显示主文件夹、点击展开子级、同级按大小降序。

用**合成数据**喂进 `_dk_inbox`，不真扫盘（真扫要几十秒且依赖本机磁盘）。
另外覆盖后加的三件事：实时大小（同目录重复回报只留一行）、搜索、右键菜单只给原生项。
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication, QMenu    # noqa: E402
from PySide6.QtCore import Qt                          # noqa: E402
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


w = A.MainWindow(admin_mode=True, dark=False, sysinfo=None)
pg = w.pages["tools"]
tb = pg.tb_dk

# ---- 合成一棵树（字节数故意乱序，验证「同级按大小降序」）----
# D:\  ->  A(300)  B(900)  C(100)
#         B ->  B1(50)  B2(700)
#               B2 -> B2a(30)
ROOT = "D:\\"
FAKE = [
    (ROOT + "A", 300, 3, 1),
    (ROOT + "B", 900, 40, 1),
    (ROOT + "C", 100, 1, 1),
    (ROOT + "B\\B1", 50, 1, 2),
    (ROOT + "B\\B2", 700, 30, 2),
    (ROOT + "B\\B2\\B2a", 30, 1, 3),
]


def feed(items=None):
    pg._dk_root = ROOT
    pg._dk_tree = {}
    pg._dk_flat = {}
    pg._dk_files = []
    pg._dk_expanded = set()
    pg._dk_query = ""
    pg._dk_hits = []
    pg._dk_inbox = list(FAKE if items is None else items)
    pg._dk_n = 0
    pg._dk_live = {"files": 0, "dirs": 0, "bytes": 0, "t0": time.time()}
    pg._dk_drain(final=True)


def texts():
    return [tb.item(r, 0).text() for r in range(tb.rowCount())]


def paths():
    return [tb.item(r, 0).data(Qt.UserRole) for r in range(tb.rowCount())]


print("[1] 初次只显示「主文件夹」（根的直接子级），不展开任何子级")
feed()
print("      可见行：", texts())
chk(tb.rowCount() == 3, "只显示 3 个主文件夹（实际 %d）" % tb.rowCount())
chk(all("B\\" not in p for p in paths()), "没有出现二级目录")
chk(paths() == [ROOT + "B", ROOT + "A", ROOT + "C"],
    "**同级按占用降序**（B 900 > A 300 > C 100）：%s" % paths())

print()
print("[2] 有子目录的才给箭头，没有的不给")
row_B = paths().index(ROOT + "B")
row_A = paths().index(ROOT + "A")
chk("\u25b8" in tb.item(row_B, 0).text(), "B 有子级 → 显示未展开箭头 ▸")
chk("\u25b8" not in tb.item(row_A, 0).text(),
    "A 没有子级 → 不显示箭头（%r）" % tb.item(row_A, 0).text())

print()
print("[3] 点击展开 B —— 只多出 B 的直接子级")
pg._dk_toggle(row_B, 0)
print("      可见行：", texts())
chk(tb.rowCount() == 5, "展开后多 2 行（B1/B2），共 5 行（实际 %d）" % tb.rowCount())
chk(ROOT + "B\\B2" in paths() and ROOT + "B\\B1" in paths(), "B1 / B2 出现了")
chk(ROOT + "B\\B2\\B2a" not in paths(),
    "**B2a 还没出现**（B2 未展开，惰性生效）")
ex = [i for i, p in enumerate(paths()) if p == ROOT + "B"][0]
chk("\u25be" in tb.item(ex, 0).text(), "展开后箭头变成 ▾")
sub = [i for i, p in enumerate(paths())
       if p in (ROOT + "B\\B1", ROOT + "B\\B2")]
chk(paths()[sub[0]] == ROOT + "B\\B2",
    "子级也是按大小降序（B2 700 > B1 50）")

print()
print("[4] 再展开 B2 → 出现 B2a；缩进应递增")
pg._dk_toggle(sub[0], 0)
print("      可见行：", texts())
chk(ROOT + "B\\B2\\B2a" in paths(), "B2a 出现了（第三层）")
i2a = paths().index(ROOT + "B\\B2\\B2a")
i2 = paths().index(ROOT + "B\\B2")
i1 = paths().index(ROOT + "B")
chk(len(tb.item(i2a, 0).text()) > len(tb.item(i2, 0).text())
    > len(tb.item(i1, 0).text()),
    "缩进随层级递增（每层多一段全角空格）")

print()
print("[5] 收起 B2 → B2a 跟着消失（不留孤儿行）")
pg._dk_toggle(i2, 0)
chk(ROOT + "B\\B2\\B2a" not in paths(), "收起父级后子级行被移除")
chk(ROOT + "B\\B2" in paths(), "B2 自己还在")

print()
print("[6] 收起 B → 只剩三个主文件夹")
i1 = paths().index(ROOT + "B")
pg._dk_toggle(i1, 0)
chk(tb.rowCount() == 3, "回到 3 行（实际 %d）" % tb.rowCount())

print()
print("[7] 点没有子目录的行不会崩、也不改变视图")
before = tb.rowCount()
pg._dk_toggle(paths().index(ROOT + "A"), 0)
chk(tb.rowCount() == before, "点 A（无子级）无变化")

print()
print("[8] 路径存在 UserRole 里（col0 文字带缩进/箭头，不能当路径用）")
p0 = tb.item(0, 0)
chk(p0.data(Qt.UserRole) == ROOT + "B",
    "UserRole 是真实路径（%r），而文字是 %r" % (p0.data(Qt.UserRole),
                                                p0.text()))
chk(p0.text() != p0.data(Qt.UserRole),
    "文字 ≠ 路径（所以「定位到资源管理器」必须读 UserRole）")

print()
print("[9] 表格不做全局排序（树形不能全局排）")
chk(tb.isSortingEnabled() is False, "表头排序已关闭")
chk(not hasattr(pg, "_dk_row_sort"), "没有残留的全局排序调用")

print()
print("[10] 空树 / 只有根时不崩")
pg._dk_tree = {}
pg._dk_expanded = set()
pg._dk_render()
chk(tb.rowCount() == 0, "空树渲染 0 行且不抛异常")

print()
print("[11] 实时大小：同一目录先报 live、再报 final —— 只能有一行，且值取最终值")
feed([
    (ROOT + "A", 500, 5, 1),          # 第一遍（模拟 on_live：还在长）
    (ROOT + "A", 3000, 9, 1),         # 第二遍（模拟 on_dir：最终值）
])
names = [p for p in paths()]
chk(names.count(ROOT + "A") == 1,
    "同一目录只出现一行（实际 %d 行）" % names.count(ROOT + "A"))
chk(tb.item(0, 1).text() == A.fmt_bytes(3000),
    "显示的是**最终值**（%s），不是先到的 500 B（实际 %s）"
    % (A.fmt_bytes(3000), tb.item(0, 1).text()))
chk(pg._dk_flat[ROOT + "A"] == (3000, 9), "_dk_flat 也被刷成最终值")
chk(pg._dk_tree[ROOT][ROOT + "A"] == (3000, 9), "_dk_tree 里也是最终值")

print()
print("[12] 搜索：文件夹名 + 大文件名，平表、按占用降序、点行不展开")
feed()                                     # 回到完整合成树（上一步只喂了 A）
pg._dk_files = [(ROOT + "big\\aaa.iso", 5000)]      # 大文件名也含 a
pg.ed_dk.setText("a")
pg.dk_search()
print("      可见行：", texts())
chk("A" in [os.path.basename(p) for p in paths()], "命中文件夹 A")
chk(ROOT + "big\\aaa.iso" in paths(), "命中大文件 aaa.iso")
chk(tb.item(0, 0).data(Qt.UserRole) == ROOT + "big\\aaa.iso",
    "按占用降序：5000 的大文件排第一")
chk(all("\u25b8" not in t and "\u25be" not in t for t in texts()),
    "搜索结果不带展开箭头（平表）")
chk(pg.lb_dk_search.text().startswith("搜索「a」"),
    "状态栏写了搜索摘要：%r" % pg.lb_dk_search.text())
rows_before = tb.rowCount()
pg._dk_toggle(0, 0)                    # 搜索态点行不应展开/收起
chk(tb.rowCount() == rows_before, "搜索态点行不改变视图")
chk(ROOT + "B\\B2" not in paths(), "搜索态不显示树里的其它行（B2 不在）")

print()
print("[13] 清掉搜索 → 回到树视图")
pg.dk_search_clear()
chk(pg._dk_query == "", "_dk_query 清空")
chk(pg.ed_dk.text() == "", "输入框清空")
chk(pg.lb_dk_search.text() == "", "状态栏清空")
chk(ROOT + "A" in paths() and ROOT + "B" in paths(), "树视图回来了")

print()
print("[14] 搜不到时不崩、状态栏如实说 0")
pg.ed_dk.setText("这个肯定没有zzz")
pg.dk_search()
chk(tb.rowCount() == 0, "0 行")
chk("0 个" in pg.lb_dk_search.text(), "状态栏写 0：%r" % pg.lb_dk_search.text())
pg.dk_search_clear()

print()
print("[15] 右键菜单：**只给 Windows 原生项**，不拉第三方塞进来的")
feed()
m = QMenu()
pg._dk_menu(m, tb, 0)
labels = [a.text() for a in m.actions() if not a.isSeparator()]
print("      菜单项：", labels)
for must in ("打开", "在资源管理器中定位", "复制完整路径", "复制名称",
             "复制", "剪切", "创建快捷方式到桌面", "重命名…", "属性"):
    chk(must in labels, "有原生项「%s」" % must)
BAN = ("网盘", "云盘", "迅雷", "7-Zip", "ToDesk", "百度", "阿里", "坚果云",
       "上传到", "QQ", "微信", "WPS", "360", "无痕", "添加到")
bad = [x for x in labels if any(b in x for b in BAN)]
chk(not bad, "没有任何第三方字样：%s" % (bad or "无"))
chk(not any("打开方式" in x for x in labels),
    "不含「打开方式」（会跳到 Win11 设置页，反而绕）")
# 目录：终端项可用；文件：终端项置灰
a_cmd = [a for a in m.actions() if a.text() == "在此打开命令提示符"]
chk(bool(a_cmd), "有「在此打开命令提示符」")
if a_cmd:
    chk(a_cmd[0].isEnabled() is bool(os.path.isdir(ROOT + "B")),
        "它对目录可用（合成数据里的 D:\\B 不存在 → 应置灰）")

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
