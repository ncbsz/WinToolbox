# -*- coding: utf-8 -*-
"""全链路验证：残留扫描 → 对话框显示 → _collect 收集 → residual_clean 安全闸。

关键：删除动作全部打桩（不真删），只校验「是否被允许 + 定位是否正确」。
用法：<venv>/python.exe tests/verify_leftover.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 本脚本在 tests/ 下，模块在上一级
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as S

FAIL = []


def chk(cond, label):
    print(("  [OK]  " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL.append(label)


print("=== 1. _reg_del_ok 单元用例（正例应允许、反例应拒绝）===")
cases = [
    # (entry, 期望允许, 期望是删值)
    ({"hive": "HKLM", "path": r"SOFTWARE\VendorX", "tag": "厂商键"}, True, False),
    ({"hive": "HKCU", "path": r"SOFTWARE\VendorX\ProductY", "tag": "产品键"}, True, False),
    ({"hive": "HKCU", "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Foo",
      "tag": "卸载项"}, True, False),
    ({"hive": "HKLM", "path": r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion"
                             r"\Uninstall\Foo", "tag": "卸载项"}, True, False),
    ({"hive": "HKLM", "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
      "value": "Foo", "tag": "启动项"}, True, True),
    ({"hive": "HKLM", "path": r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion"
                             r"\RunOnce", "value": "Foo", "tag": "启动项"}, True, True),
    ({"hive": "HKLM", "path": r"SYSTEM\CurrentControlSet\Services\FooSvc", "tag": "服务"},
     True, False),
    ({"hive": "HKLM",
      "path": r"SYSTEM\CurrentControlSet\Services\EventLog\Application\FooApp",
      "tag": "日志源"}, True, False),
    # 卸载前已有：按通用规则放行
    ({"hive": "HKCU", "path": r"SOFTWARE\VendorX", "tag": "卸载前已有"}, True, False),
    # ---- 反例 ----
    ({"hive": "HKLM", "path": r"SOFTWARE", "tag": "厂商键"}, False, False),
    ({"hive": "HKLM", "path": r"SOFTWARE\A\B\C", "tag": "产品键"}, False, False),
    ({"hive": "HKLM", "path": r"SYSTEM\CurrentControlSet", "tag": "服务"}, False, False),
    ({"hive": "HKLM", "path": r"SYSTEM\CurrentControlSet\Services", "tag": "服务"}, False, False),
    ({"hive": "HKLM", "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
      "tag": "启动项"}, False, False),          # 启动项没有值名 → 拒绝
    ({"hive": "HKU", "path": r"SOFTWARE\X", "tag": "厂商键"}, False, False),
    ({"hive": "HKLM",
      "path": r"SYSTEM\CurrentControlSet\Services\EventLog\Application",
      "tag": "日志源"}, False, False),
    ({"hive": "HKLM", "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
      "tag": "卸载项"}, False, False),          # Uninstall 键本身不能删
]
for entry, want_ok, want_val in cases:
    got_ok, got_val = S._reg_del_ok(entry)
    chk((got_ok, got_val) == (want_ok, want_val),
        "%s %s -> ok=%s val=%s（期望 %s/%s）"
        % (entry["tag"], entry["path"][:52], got_ok, got_val, want_ok, want_val))

print("\n=== 2. 真实程序扫描 → 对话框 → _collect 往返（Qt 离屏）===")
from PySide6.QtWidgets import QApplication
qapp = QApplication(sys.argv)

inst = S.list_software()
pick = None
for it in inst:
    if (it.get("location") or it.get("uninstall")) and it.get("key"):
        pick = it
        break
print("  测试程序:", pick.get("name"))
res = S.residual_scan(pick.get("name") or "", pick.get("publisher") or "",
                      pick.get("location") or "", pick.get("uninstall") or "",
                      pick.get("key") or "", cap=40)
print("  扫描: files=%d reg=%d" % (len(res["files"]), len(res["reg"])))

import app as A
dlg = A.ResidualDialog(None, pick.get("name"), res)
got_regs = dlg._collect(dlg.tbl_reg, False)
got_files = dlg._collect(dlg.tbl_files, True)
chk(len(got_regs) == len(res["reg"]), "注册表项数量往返一致（%d）" % len(got_regs))
same = all(g.get("hive") == o.get("hive") and g.get("path") == o.get("path")
           and (g.get("value") or "") == (o.get("value") or "")
           for g, o in zip(got_regs, res["reg"]))
chk(same, "hive/path/value 全部原样往返（hive 不再被标签污染）")
print("   回传示例:", got_regs[:2])
chk(all(g["hive"] in ("HKLM", "HKCU") for g in got_regs), "所有 hive 均为 HKLM/HKCU")

print("\n=== 3. residual_clean 打桩全跑（断言零「拒绝」）===")
calls = {"rmtree": [], "remove": [], "reg": [], "delval": []}
S.shutil.rmtree = lambda p, *a, **k: calls["rmtree"].append(p)
S.os.remove = lambda p, *a, **k: calls["remove"].append(p)
S._delete_tree = lambda root, sub: calls["reg"].append(sub)


class _FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeReg:
    HKEY_LOCAL_MACHINE = HKEY_CURRENT_USER = object()
    KEY_SET_VALUE = KEY_WOW64_64KEY = 0

    @staticmethod
    def OpenKey(*a, **k):
        return _FakeKey()

    @staticmethod
    def DeleteValue(k, v):
        calls["delval"].append(v)


_real_reg = S.winreg
S.winreg = _FakeReg
# 造一个含全部 5 类的注册表集合（含启动项带值）
synthetic_regs = [
    {"hive": "HKLM", "path": r"SOFTWARE\VendorX", "tag": "厂商键"},
    {"hive": "HKCU", "path": r"SOFTWARE\VendorX\ProductY", "tag": "产品键"},
    {"hive": "HKCU", "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Foo",
     "tag": "卸载项"},
    {"hive": "HKLM", "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
     "value": "Foo", "tag": "启动项"},
    {"hive": "HKLM", "path": r"SYSTEM\CurrentControlSet\Services\FooSvc", "tag": "服务"},
]
# 文件：真实存在的临时目录（D 盘根直接子项，验证磁盘根白名单）+ 系统路径（应拒）
tmp_d = r"D:\_wtb_leftover_test"
os.makedirs(tmp_d, exist_ok=True)
files = [{"path": tmp_d},
         {"path": r"C:\Windows\System32\drivers\etc\hosts"},
         {"path": r"D:\\"}]
r = S.residual_clean(files, synthetic_regs)
S.winreg = _real_reg
msgs = r["msg"]
print("  消息:")
for m in msgs:
    print("   ", m)
chk(not any("超出安全范围" in m for m in msgs),
    "注册表 5 类全部被允许（无「超出安全范围」）")
chk(any(("已删除：%s" % tmp_d) == m for m in msgs),
    "D 盘直接子目录被允许删除（磁盘根白名单生效）")
chk(any("拒绝" in m and "hosts" in m for m in msgs),
    "系统路径仍被拒绝（C:\\Windows 不在白名单）")
chk(any("拒绝" in m and m.rstrip().rstrip("\\").endswith("D:") for m in msgs),
    "盘根本身仍被拒绝")
chk(sum(1 for m in msgs if "拒绝（不在允许的目录内）" in m) == 2,
    "文件侧恰好 2 项被拒（系统路径 + 盘根），实际 %d"
    % sum(1 for m in msgs if "拒绝（不在允许的目录内）" in m))
chk(len(calls["reg"]) == 4, "整键删除调用 4 次（厂商/产品/卸载项/服务），实际 %d" % len(calls["reg"]))
chk(calls["delval"] == ["Foo"], "启动项走「删值」而非删键：%s" % calls["delval"])
chk(len(calls["rmtree"]) == 1, "临时目录删除调用 1 次")
chk(r["ok"] is False, "整体 ok=False（因有被拒项），符合预期")
try:
    os.rmdir(tmp_d)
except OSError:
    pass

print("\n=== 4. 覆盖面：登录数据 / 计划任务 / 点目录（新增类别）===")
creds = S._scan_credentials({"workbuddy"}, set(), 20)
chk(isinstance(creds, list), "凭据枚举可用（返回 %d 条）" % len(creds))
chk(all(c.get("kind") == "cred" and c.get("hive") == "CRED" for c in creds),
    "凭据项结构正确（kind=cred / hive=CRED）")

tasks = S._scan_tasks({"workbuddy"}, set(), 20)
chk(isinstance(tasks, list), "计划任务枚举可用（返回 %d 条）" % len(tasks))
chk(all(t.get("kind") == "task" and not t["path"].lower().startswith("microsoft\\")
        for t in tasks), "计划任务项结构正确且不含系统任务")

# 点目录：本机存在 C:\Users\<user>\.workbuddy，用它验证新扫描位置真的生效
res_dot = S.residual_scan("WorkBuddy", "", "", "", "", cap=200)
dot_hits = [f for f in res_dot["files"] if f.get("tag") == "用户配置"]
chk(any(os.path.basename(f["path"]).lower() == ".workbuddy" for f in dot_hits),
    "用户主目录点目录（.workbuddy）命中，tag=用户配置：%s"
    % [os.path.basename(f["path"]) for f in dot_hits][:4])

# 快照复查按类型分派
chk(S._item_exists({"hive": "HKCU", "path": "SOFTWARE", "kind": "reg"}) is True,
    "_item_exists 注册表分支可用")
chk(S._item_exists({"hive": "CRED", "path": "绝对不存在的凭据目标",
                    "kind": "cred"}) is False, "_item_exists 凭据分支可用（不存在→False）")
chk(S._item_exists({"hive": "TASK", "path": "绝对不存在的计划任务",
                    "kind": "task"}) is False, "_item_exists 任务分支可用（不存在→False）")

# 清理分派：凭据（不存在的目标 → 走到 CredDeleteW 并如实报失败）；
# 任务（系统任务必须被拒，普通任务走 schtasks 删除）
r2 = S.residual_clean([], [
    {"hive": "CRED", "path": "WtbVerifyNoSuchCred", "kind": "cred", "ctype": 1,
     "tag": "登录凭据"},
    {"hive": "TASK", "path": r"Microsoft\Windows\WtbVerifySystemTask", "kind": "task",
     "tag": "计划任务"},
    {"hive": "TASK", "path": "WtbVerifyNoSuchTask", "kind": "task", "tag": "计划任务"},
])
m2 = r2["msg"]
print("  消息:")
for m in m2:
    print("   ", m)
chk(any("凭据" in m for m in m2), "凭据项走 CredDeleteW 分支")
chk(any("拒绝（系统任务不可删）" in m for m in m2), "系统计划任务被拒绝")
chk(any("任务" in m and "失败" in m for m in m2), "普通计划任务走 schtasks 删除分支")

print("\n=== 5. 全机卸载残留扫描（orphan_scan）===")
r3 = S.orphan_scan()
chk(set(r3.keys()) == {"files", "reg", "tokens"}, "返回结构与 residual_scan 一致")
chk(len(r3["files"]) <= 120 and len(r3["reg"]) <= 120, "结果受 cap 约束")
weak = [f for f in r3["files"] if f.get("weak")]
strong = [f for f in r3["files"] if not f.get("weak")]
print("  命中: files=%d（其中低置信 %d） reg=%d" % (len(r3["files"]), len(weak), len(r3["reg"])))
chk(all(isinstance(f.get("weak"), bool) for f in r3["files"]),
    "每项都显式带 weak 标记")
chk(all(f.get("tag") and f.get("path") for f in r3["files"]), "文件项字段完整")
chk(all("疑似残留目录" not in f["tag"] or f["weak"] for f in r3["files"]),
    "低置信目录一律标 weak（对话框默认不勾选）")
# 关键不变量：扫出来的注册表项必须都能通过删除闸（否则又是"扫得到删不掉"）
bad = [x for x in r3["reg"]
       if (x.get("kind") or "reg") == "reg" and not S._reg_del_ok(x)[0]]
chk(not bad, "孤儿注册表项全部可通过删除闸：%s"
    % [x.get("path") for x in bad][:3])
chk(all((x.get("kind") or "reg") == "reg" for x in r3["reg"]),
    "orphan_scan 不产生凭据/任务类（它们属于整机扫描之外）")
if strong:
    print("  高置信示例:", [(f["tag"], f["path"][-40:]) for f in strong[:3]])
if r3["reg"]:
    print("  注册表示例:", [(x["tag"], x["path"][-40:]) for x in r3["reg"][:3]])

# UI 冒烟：软件管理页有入口按钮，且扫描结果能渲染进对话框
from PySide6.QtWidgets import QPushButton
win = A.MainWindow(dark=False)
win.show()
win.goto("software")
sw = win.pages["software"]
labels = [b.text() for b in sw.findChildren(QPushButton)]
chk("扫描卸载残留" in labels, "软件管理页有「扫描卸载残留」按钮：%s" % labels[:8])
chk(hasattr(sw, "orphan_scan"), "按钮处理函数存在")
dlg2 = A.ResidualDialog(sw, "全机扫描", r3)
chk(dlg2.tbl_files.rowCount() == len(r3["files"]), "对话框渲染了全部文件项")
chk(dlg2.tbl_reg.rowCount() == len(r3["reg"]), "对话框渲染了全部注册表项")
unchecked = sum(1 for i in range(dlg2.tbl_files.rowCount())
                if dlg2.tbl_files.item(i, 0).checkState().value == 0)
chk(unchecked == len(weak), "低置信项默认未勾选（%d 项 / 共 %d）" % (unchecked, len(weak)))
win.close()

print("\n=== 结果 ===")
print("全部通过" if not FAIL else "失败 %d 项: %s" % (len(FAIL), FAIL))
# Qt 在 Windows 上的解释器退出阶段偶发崩溃（0xC0000409），测试脚本直接硬退出，
# 避免把"收尾噪音"当成测试失败
sys.stdout.flush()
os._exit(0 if not FAIL else 1)
