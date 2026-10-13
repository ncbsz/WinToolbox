# -*- coding: utf-8 -*-
"""注册表「无效项清理」的分类多选 + 反选预设：定向冒烟。

验证 MultiPick 接进UI 后的行为，特别是：
  · 多选过滤真的按集合匹配（不是单选）
  · 空集合 = 全部
  · 反选预设会翻转，且「全不选后反选」会回落到推荐组而不是留下空集
  · 勾选框计数会跟着手动勾选实时变
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


def rows():
    return [reg.tb_issue.item(r, 1).text() for r in range(reg.tb_issue.rowCount())]


win = A.MainWindow(admin_mode=True, dark=False, sysinfo=None)
reg = win.pages["regtool"]

print("[1] 单选下拉已换成多选 MultiPick")
chk(hasattr(reg, "mp_issue_cat"), "有 mp_issue_cat")
chk(not hasattr(reg, "cb_issue_cat"),
    "原来的单选 QComboBox（cb_issue_cat）已经移除，不留两处入口")
chk(reg.mp_issue_cat.btn.text() == "全部", "初始按钮文字是「全部」")

print()
print("[2] 灌入假数据，覆盖多个分类")
reg.issues = [
    {"hive": "HKLM", "path": "\\A1", "value": "", "kind": "invalid_com_server",
     "cat": "com_server", "why": "文件丢失", "weak": False, "_checked": True},
    {"hive": "HKLM", "path": "\\A2", "value": "", "kind": "invalid_com_ref",
     "cat": "com_ref", "why": "引用缺失", "weak": False, "_checked": True},
    {"hive": "HKCU", "path": "\\B1", "value": "x", "kind": "invalid_filetype",
     "cat": "filetype", "why": "程序已卸载", "weak": False, "_checked": True},
    {"hive": "HKLM", "path": "\\C1", "value": "", "kind": "invalid_service",
     "cat": "service", "why": "镜像不存在", "weak": True, "_checked": False},
    {"hive": "HKLM", "path": "\\C2", "value": "", "kind": "invalid_firewall",
     "cat": "firewall", "why": "程序不存在", "weak": True, "_checked": False},
]
reg._issue_per = 400
reg._fill_issue_cat()
chk(reg.mp_issue_cat.picked() == set(), "重建后默认不筛（空集 = 全部）")
chk(reg.mp_issue_cat.btn.text() == "全部", "按钮文字 = 全部")
reg._render_issues()
chk(reg.tb_issue.rowCount() == 5, "不筛时显示全部 5 条")

print()
print("[3] 多选过滤（这是本次要解决的核心）")
names = [it[1] for it in reg.mp_issue_cat._items if it[0]]
chk(len(names) == 5, "下拉里有 5 个分类选项（%s）" % names)

reg.mp_issue_cat._sel = {"com_server", "filetype"}
reg._render_issues()
got = rows()
print("      选 com_server + filetype -> %s" % got)
chk(len(got) == 2, "多选后只显示 2 条（旧实现只能选 1 类）")
chk(all("无效的 ActiveX" in g or "文件类型" in g or "关联" in g for g in got),
    "显示的是选中的那两类")

reg.mp_issue_cat._sel = {"service", "firewall"}
reg._render_issues()
got2 = rows()
chk(len(got2) == 2, "换成另外两类也是 2 条")
chk(got2 != got, "两组筛出来的结果不同（不是写死的）")

print()
print("[4] 空集合 = 全部（回退语义）")
reg.mp_issue_cat._sel = set()
reg._render_issues()
chk(reg.tb_issue.rowCount() == 5, "清空选择后回到全部 5 条")
chk("已按分类过滤" not in reg.lb_issue_show.text(),
    "不筛时文案里没有「已按分类过滤」（%r）" % reg.lb_issue_show.text())

print()
print("[5] 按钮文字反映选择状态")
reg.mp_issue_cat._sel = {"com_server"}
reg.mp_issue_cat._paint()
chk(reg.mp_issue_cat.btn.text() == "1 类已选",
    "选 1 类 -> 「1 类已选」（%s）" % reg.mp_issue_cat.btn.text())
reg.mp_issue_cat._sel = set(names)
reg.mp_issue_cat._paint()
chk("全部" in reg.mp_issue_cat.btn.text(),
    "全选 -> 文字含「全部」（%s）" % reg.mp_issue_cat.btn.text())

print()
print("[6] 扫描范围的四个预设")
chk(all(cid in reg.issue_cats for cid, *_ in S.REG_CATEGORIES),
    "%d 个分类都有勾选框" % len(reg.issue_cats))


def n_checked():
    return sum(1 for cb in reg.issue_cats.values() if cb.isChecked())


reg.issue_cat_preset("all")
chk(n_checked() == len(reg.issue_cats), "全选 -> %d/%d" % (n_checked(), len(reg.issue_cats)))
reg.issue_cat_preset("none")
chk(n_checked() == 0, "全不选 -> %d" % n_checked())
reg.issue_cat_preset("recommend")
n_rec = n_checked()
n_def = len([1 for _c, _n, _d, dflt, _r in S.REG_CATEGORIES if dflt])
chk(n_rec == n_def, "只勾推荐 -> %d（= 默认勾选的 %d 类）" % (n_rec, n_def))

print()
print("     反选：")
reg.issue_cat_preset("recommend")
before = {cid: cb.isChecked() for cid, cb in reg.issue_cats.items()}
reg.issue_cat_preset("invert")
after = {cid: cb.isChecked() for cid, cb in reg.issue_cats.items()}
flipped = sum(1 for k in before if before[k] != after[k])
chk(flipped == len(before), "每个分类的勾选都被翻转（%d/%d）"
    % (flipped, len(before)))
chk(n_checked() == len(reg.issue_cats) - n_def,
    "反选后勾选数 = 18 - %d = %d（实际 %d）"
    % (n_def, len(reg.issue_cats) - n_def, n_checked()))

print()
print("     反选的边界一：全选时反选会得空集 -> 回落到推荐组")
reg.issue_cat_preset("all")
reg.issue_cat_preset("invert")
chk(n_checked() == n_def,
    "全选后反选没有留下空集，而是回落到推荐组（%d 类）" % n_checked())

print()
print("     反选的边界二：全不选时反选 = 全选（数学上正确，不是 bug）")
reg.issue_cat_preset("none")
reg.issue_cat_preset("invert")
chk(n_checked() == len(reg.issue_cats),
    "全不选后反选 -> 全部 %d 类（空集的反选就是全集）" % n_checked())

print()
print("[7] 勾选计数实时联动")
reg.issue_cat_preset("none")
chk("一个都没勾" in reg.lb_issue_cat.text(),
    "一个都没勾时给出警告文案（%r）" % reg.lb_issue_cat.text())
first = next(iter(reg.issue_cats.values()))
first.setChecked(True)          # 手动勾，不走预设
chk("已勾选 1 /" in reg.lb_issue_cat.text(),
    "手动勾一个后计数立刻变（%r）" % reg.lb_issue_cat.text())
reg.issue_cat_preset("all")
chk("全部" in reg.lb_issue_cat.text(),
    "全选时文案提示会变慢（%r）" % reg.lb_issue_cat.text())

print()
print("[8] 清理只作用于当前列表勾选项（多选下依然成立）")
reg.issues = [
    {"hive": "HKLM", "path": "\\A1", "value": "", "kind": "invalid_com_server",
     "cat": "com_server", "why": "w", "weak": False, "_checked": True},
    {"hive": "HKLM", "path": "\\B1", "value": "x", "kind": "invalid_filetype",
     "cat": "filetype", "why": "w", "weak": False, "_checked": True},
]
reg._fill_issue_cat()
reg.mp_issue_cat._sel = {"filetype"}
reg._render_issues()
chk(reg.tb_issue.rowCount() == 1, "只剩 filetype 那1 条")
# 只有可见行能被勾选→ 索引必须指向原列表里的正确条目
idx = reg.tb_issue.item(0, 0).data(A.Qt.UserRole)
chk(isinstance(idx, int) and reg.issues[idx].get("cat") == "filetype",
    "UserRole 存的整数下标指向正确的原条目（%r）" % idx)

print()
print("[9] refresh 会清掉筛选状态")
reg._fill_issue_cat()
reg.mp_issue_cat._sel = {"com_server"}
reg.refresh()
chk(reg.mp_issue_cat.picked() == set(), "refresh 后筛选回到「全部」")
chk(reg.tb_issue.rowCount() == 0, "refresh 后表格已清空")

print()
print("[10] 「读没读全」的提示 + 上限「不限」")
_opts = [(reg.cb_issue_per.itemText(i), reg.cb_issue_per.itemData(i))
         for i in range(reg.cb_issue_per.count())]
chk(any(v == 0 for _t, v in _opts), "上限下拉里有「不限」（值 0）")
chk(hasattr(reg, "lb_issue_trunc"), "结果区有截断提示位（lb_issue_trunc）")
reg._issue_auto_wide = False
reg._show_issue_trunc({})
chk(reg.lb_issue_trunc.isHidden(), "没截断时不显示提示（不吓人）")
reg._show_issue_trunc({"per_cat": 400, "capped_cats": {"history": 400},
                       "truncated": True, "total": 1200, "shown": 800,
                       "enum_trunc": [], "unlimited": False})
_t10 = reg.lb_issue_trunc.text()
chk(not reg.lb_issue_trunc.isHidden() and "正好卡在上限" in _t10,
    "分类触顶会写明（不再只藏在 tooltip 里）")
chk("全局上限" in _t10, "总数被全局上限截断也写明")
reg._show_issue_trunc({"per_cat": 400, "capped_cats": {},
                       "truncated": False, "total": 5, "shown": 5,
                       "enum_trunc": [{"where": "HKCR\\CLSID", "cap": 10,
                                       "total": 30}], "unlimited": False})
chk("枚举被读取上限卡住" in reg.lb_issue_trunc.text(),
    "枚举阶段被截断也写明（最隐蔽的一种）")
reg._issue_auto_wide = True
reg._show_issue_trunc({"per_cat": 0, "unlimited": True, "capped_cats": {},
                       "truncated": False, "total": 5, "shown": 5,
                       "enum_trunc": []})
chk("自动放开" in reg.lb_issue_trunc.text(), "只勾一类自动放宽时在界面写明")
reg._issue_auto_wide = False
reg._show_issue_trunc({})
chk(reg.lb_issue_trunc.isHidden(), "复位后提示隐藏")

print()
print("=" * 58)
print("结果：%s（ok %d / fail %d）" % ("全通过" if not FAIL else "有失败",
                                      OK, len(FAIL)))
for f in FAIL:
    print("   -", f)

win.hide()
win.deleteLater()
os._exit(0 if not FAIL else 1)