# -*- coding: utf-8 -*-
"""右键菜单「纯原生动作」的验证。

**全程打桩**：不弹属性框、不真删、不真建快捷方式、不真开终端。
临时文件都建在 tempfile 目录里，跑完删掉。
"""
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication, QMenu, QLineEdit    # noqa: E402
import app as A                                                  # noqa: E402
import server as S                                               # noqa: E402

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


tmp = tempfile.mkdtemp(prefix="wt_menu_")
f1 = os.path.join(tmp, "报告 2026.txt")
f2 = os.path.join(tmp, "已存在.txt")
d1 = os.path.join(tmp, "子目录")
os.makedirs(d1)
open(f1, "w", encoding="utf-8").write("x")
open(f2, "w", encoding="utf-8").write("x")

w = A.MainWindow(admin_mode=True, dark=False, sysinfo=None)

# ---- 打桩：所有会真的动系统 / 弹窗的出口 ----
seen = {"verb": [], "ps": [], "popen": [], "info": [], "warn": [], "ask": None}
_orig = {"_shell_exec": A._shell_exec, "ps": S.ps, "popen": A.subprocess.Popen,
         "info": A.info, "warn": A.warn, "confirm": A.confirm,
         "getText": A.QInputDialog.getText, "known": S.known_folder}
A._shell_exec = lambda verb, path: (seen["verb"].append((verb, path)), True)[1]
def fake_ps(script, timeout=45):
    """模拟 PowerShell：把脚本里 CreateShortcut(...) 指到的 .lnk 真建出来。

    不这样的话「成功」永远判 False（create_shortcut_on_desktop 会回读文件在不在）。
    """
    import re
    seen["ps"].append(script)
    m = re.search(r"CreateShortcut\('([^']+)'\)", script)
    if m:
        try:
            open(m.group(1).replace("''", "'"), "w").close()
        except OSError:
            pass
    return 0, ""


S.ps = fake_ps
A.subprocess.Popen = lambda cmd, *a, **k: seen["popen"].append((cmd, k))
A.info = lambda parent, a, b=None, **k: seen["info"].append((a, b))
A.warn = lambda parent, a, b=None, **k: seen["warn"].append((a, b))
A.confirm = lambda parent, t, x, danger=False: True
S.known_folder = lambda name: tmp          # 绝不碰用户真的桌面

try:
    print("[1] shell_verb 用的谓词要对（copy/cut/delete/properties）")
    for verb in ("copy", "cut", "delete", "properties"):
        seen["verb"].clear()
        ok = A.shell_verb(f1, verb)
        chk(ok and seen["verb"] == [(verb, f1)],
            "%s → 下发 (%s, 路径)" % (verb, verb))
    seen["verb"].clear()
    bad = A.shell_verb(os.path.join(tmp, "不存在.txt"), "delete")
    chk(bad is False and not seen["verb"],
        "路径不存在时**直接返回 False，不下发谓词**（不能把「删了个不存在的」当成功）")

    print()
    print("[2] 打开终端：不用 CREATE_NO_WINDOW（否则等于什么都没发生）")
    seen["popen"].clear()
    A.open_terminal(d1, "cmd")
    A.open_terminal(d1, "ps")
    cmds = [c for c, _kw in seen["popen"]]
    flags = [kw.get("creationflags", 0) for _c, kw in seen["popen"]]
    print("      下发：", cmds)
    chk(len(cmds) == 2, "起了两个终端进程")
    chk(cmds[0][:3] == ["cmd", "/k", "cd"] and cmds[0][-1] == d1,
        "cmd 是 `cmd /k cd /d <目录>`：%s" % cmds[0])
    chk(cmds[1][0] == "powershell" and "Set-Location" in " ".join(cmds[1]),
        "PowerShell 用 Set-Location")
    chk(all(fl == 0 for fl in flags),
        "**没有传 CREATE_NO_WINDOW**（传了会起一个不可见的终端）")
    # 传文件时应该用它的父目录
    seen["popen"].clear()
    A.open_terminal(f1, "cmd")
    chk(seen["popen"][0][0][-1] == tmp, "给的是文件时，在它的父目录打开")

    print()
    print("[3] 重命名：同名已存在 / 非法字符 / 未改动 都要挡下来")
    def ask_text(value):
        A.QInputDialog.getText = lambda *a, **k: (value, True)
    ask_text("新名字.txt")
    seen["warn"].clear()
    A.rename_path(w, f1)
    new_f1 = os.path.join(tmp, "新名字.txt")
    chk(os.path.exists(new_f1) and not os.path.exists(f1), "正常改名成功")
    chk(seen["info"], "改完有提示：%r" % (seen["info"][-1],))
    ask_text("已存在.txt")
    seen["warn"].clear()
    A.rename_path(w, new_f1)
    chk(os.path.exists(new_f1), "**目标已存在 → 不改**")
    chk("同名已存在" in (seen["warn"][-1][0] or ""),
        "并说明为什么：%r" % (seen["warn"][-1][0],))
    ask_text("非法/名字.txt")
    seen["warn"].clear()
    A.rename_path(w, new_f1)
    chk(os.path.exists(new_f1) and "非法字符" in (seen["warn"][-1][0] or ""),
        "非法字符被拒：%r" % (seen["warn"][-1][0],))
    ask_text("新名字.txt")          # 和当前同名
    seen["warn"].clear()
    A.rename_path(w, new_f1)
    chk(os.path.exists(new_f1) and not seen["warn"], "名字没变 → 安静地什么都不做")
    ask_text("随便什么")
    A.QInputDialog.getText = lambda *a, **k: ("随便什么", False)   # 点取消
    seen["warn"].clear()
    A.rename_path(w, new_f1)
    chk(os.path.exists(new_f1) and not seen["warn"], "弹窗点「取消」→ 不动")

    print()
    print("[4] 创建快捷方式：目标不该已存在，脚本要指向桌面")
    seen["ps"].clear()
    ok, note = A.create_shortcut_on_desktop(d1)
    chk(ok and seen["ps"], "成功创建（打桩）")
    script = seen["ps"][0] if seen["ps"] else ""
    chk("WScript.Shell" in script and "CreateShortcut" in script,
        "用的是系统自带的 WScript.Shell")
    chk(tmp.replace("'", "''") in script, "快捷方式落在桌面目录（测试里是临时目录）")
    lnk = os.path.join(tmp, os.path.basename(d1) + " - 快捷方式.lnk")
    open(lnk, "w").close()          # 造一个「已存在」
    seen["ps"].clear()
    ok2, note2 = A.create_shortcut_on_desktop(d1)
    chk(ok2 is False and "已有同名" in note2 and not seen["ps"],
        "同名快捷方式已存在 → 不改写、如实说：%r" % note2)
    ok3, note3 = A.create_shortcut_on_desktop(os.path.join(tmp, "没这东西"))
    chk(ok3 is False and not seen["ps"], "路径不存在 → 不调 PowerShell")

    print()
    print("[5] 删除：先自己的确认框，再交给系统的回收站流程")
    seen["verb"].clear()
    A.confirm = lambda *a, **k: False
    A._delete_ui(w, f2, "已存在.txt", False)
    chk(not seen["verb"], "确认框点「取消」→ **不下发删除**")
    A.confirm = lambda *a, **k: True
    A._delete_ui(w, f2, "已存在.txt", False)
    chk(seen["verb"] == [("delete", f2)],
        "确认后才调 delete 谓词（系统随后还会自己再确认一次）")

    print()
    print("[6] 复制 / 剪切失败要喊出来（系统谓词失败时没有返回值）")
    A._shell_exec = lambda verb, path: False
    seen["warn"].clear()
    A._shell_guard(w, A.shell_verb(f2, "copy"), "复制")
    chk(seen["warn"] and "复制" in seen["warn"][-1][0],
        "失败会提示：%r" % (seen["warn"][-1][0],))
    A._shell_exec = lambda verb, path: (seen["verb"].append((verb, path)), True)[1]

    print()
    print("[7] 菜单项集合：全是原生能力，且按对象类型置灰")
    m = QMenu()
    A.native_file_menu(m, f2, w)
    labels = [a.text() for a in m.actions() if not a.isSeparator()]
    print("      文件菜单：", labels)
    acts = {a.text(): a for a in m.actions() if not a.isSeparator()}
    chk(acts["在此打开命令提示符"].isEnabled() is False,
        "对**文件**：终端项置灰（目录才有意义）")
    chk(acts["打开"].isEnabled() and acts["属性"].isEnabled(),
        "对文件：打开 / 属性可用")
    m2 = QMenu()
    A.native_file_menu(m2, d1, w)
    en = [a.text() for a in m2.actions() if not a.isSeparator() and a.isEnabled()]
    chk("在此打开命令提示符" in en, "对**目录**：终端项可用")
    chk("在资源管理器中定位" in en and "属性" in en, "定位 / 属性都在")
    BAN = ("网盘", "云盘", "迅雷", "7-Zip", "ToDesk", "百度", "上传到", "365")
    chk(not [x for x in labels + en if any(b in x for b in BAN)],
        "两类菜单都不含第三方字样")

finally:
    A._shell_exec = _orig["_shell_exec"]
    S.ps = _orig["ps"]
    A.subprocess.Popen = _orig["popen"]
    A.info, A.warn, A.confirm = _orig["info"], _orig["warn"], _orig["confirm"]
    A.QInputDialog.getText = _orig["getText"]
    S.known_folder = _orig["known"]
    shutil.rmtree(tmp, ignore_errors=True)
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
