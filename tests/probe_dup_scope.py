# -*- coding: utf-8 -*-
"""重复文件查找「查找范围」的定向验证：
  ① list_drives() 返回的盘根串能被 _scan_roots 接受（不许被静默丢掉）
  ② UI：三档范围 + 控件显隐（选目录 / 选盘）
  ③ scan_dup 把 roots 传对（打桩捕获）
  ④ 端到端：临时目录里放两个内容相同的文件 → 真扫出 1 组
"""
import os
import shutil
import sys
import tempfile
import time

os.environ["WTB_NO_BROWSER"] = "1"
os.environ.setdefault("QT_QPA_PLATFORM", "windows")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import app as A
import server as S
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

OK, FAIL = 0, []


def chk(cond, label):
    global OK
    if cond:
        OK += 1
        print("  ok  %s" % label)
    else:
        FAIL.append(label)
        print("  FAIL %s" % label)


A.settings_save = lambda *a, **k: None
A.confirm = lambda *a, **k: False
A.info = lambda *a, **k: None
A.warn = lambda *a, **k: None

# ---------- ① 盘根解析 ----------
print("[①] 盘根串解析")
drv = S.list_drives()
print("      list_drives ->", [d["root"] for d in drv])
if drv:
    r0 = drv[0]["root"]
    got = S._scan_roots([r0])
    chk(got == [r0], "整盘根 %r 解析后仍是 %r（没被当环境变量名丢掉）" % (r0, got))
chk(S._scan_roots(["C:"]) == ["C:\\"], "短写 'C:' 归一成 'C:\\\\'")
chk(len(S._scan_roots(["C:\\Windows"])) == 1, "普通目录不受影响")

# ---------- UI ----------
qapp = QApplication([])
w = A.MainWindow(settings={}, admin_mode=True, dark=False,
                 sysinfo=A.probe_system_info(qapp))
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


page = w.pages["tools"]
w.goto("tools")
pump(0.6)
# 重复查找在「磁盘工具」页的第 2 个页签
tabs = getattr(page, "tabs", None)
if tabs is not None:
    tabs.setCurrentIndex(1)
    pump(0.4)

print("[②] UI：三档范围 + 控件显隐")
sc = page.cb_dup_scope
chk(sc.count() == 3, "范围下拉 3 档（实际 %d）" % sc.count())
chk([sc.itemData(i) for i in range(sc.count())] == ["default", "folder", "drive"],
    "档位顺序 = 常用位置 / 指定文件夹 / 整个磁盘")
sc.setCurrentIndex(0)
pump(0.2)
chk(not page.lb_dup_dir.isHidden() or True, "默认档不报错")
chk(page.cb_dup_drive.isHidden(), "默认档：选盘控件隐藏")
sc.setCurrentIndex(1)
pump(0.2)
chk(not page.lb_dup_dir.isHidden() and not page.btn_dup_dir.isHidden(),
    "文件夹档：目录标签 + 「选择…」可见")
chk(page.cb_dup_drive.isHidden(), "文件夹档：选盘控件隐藏")
sc.setCurrentIndex(2)
pump(0.3)
chk(not page.cb_dup_drive.isHidden(), "磁盘档：选盘控件可见")
chk(page.cb_dup_drive.count() > 0, "磁盘下拉已填充（%d 项）" % page.cb_dup_drive.count())

print("[③] scan_dup 传参（打桩捕获 roots）")
captured = []
_real = S.duplicate_files_scan
S.duplicate_files_scan = lambda **kw: (captured.append(kw.get("roots")), [])[1]
sc.setCurrentIndex(0)
page.scan_dup()
until(lambda: bool(captured), 5)
chk(captured and captured[-1] is None, "常用位置 → roots=None（走 server 默认根）")
sc.setCurrentIndex(1)
page._dup_dir = ""
page.scan_dup()
pump(0.2)
chk(len(captured) == 1, "文件夹档未选目录 → 直接提示、不发任务")
page._dup_dir = "C:\\Windows"
page.scan_dup()
until(lambda: len(captured) >= 2, 5)
chk(captured[-1] == ["C:\\Windows"], "文件夹档 → roots=[所选目录]（实际 %r）" % (captured[-1],))
sc.setCurrentIndex(2)
page.scan_dup()
until(lambda: len(captured) >= 3, 5)
chk(captured[-1] == [page.cb_dup_drive.currentData()],
    "磁盘档 → roots=[所选盘]（实际 %r）" % (captured[-1],))
S.duplicate_files_scan = _real

print("[④] 端到端：临时目录两个相同文件 → 真扫出 1 组")
td = tempfile.mkdtemp(prefix="wtb_dup_")
try:
    blob = os.urandom(200 * 1024)          # 200KB（> 100KB 档）
    for n in ("a.bin", "b.bin"):
        with open(os.path.join(td, n), "wb") as f:
            f.write(blob)
    sc.setCurrentIndex(1)
    page._dup_dir = td
    page.cb_dup_size.setCurrentIndex(0)    # 100 KB
    page.ck_dup_hash.setChecked(True)
    page.scan_dup()
    ok = until(lambda: page.tb_dup.rowCount() > 0, 30)
    chk(ok and page.tb_dup.rowCount() == 2,
        "扫出 1 组 2 个文件（表格 %d 行）" % page.tb_dup.rowCount())
    chk("1 组" in page.pill_dup.text(), "状态药丸：%s" % page.pill_dup.text())
finally:
    shutil.rmtree(td, ignore_errors=True)

print()
print("结果：%s（ok %d / fail %d）" % ("全通过" if not FAIL else "有失败", OK, len(FAIL)))
for f in FAIL:
    print("   -", f)
sys.stdout.flush()
w.hide()
os._exit(0 if not FAIL else 1)
