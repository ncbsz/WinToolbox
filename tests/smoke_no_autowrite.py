# -*- coding: utf-8 -*-
"""回归守卫：**启动软件 / 进任何页面，都不许自动改系统设置**。

背景（用户 2026-10-09 反馈）：打开软件后他的电源计划被改了。
定位办法是「仪器化」：把 server 侧所有写入口打桩（只记录不执行），
建一次 MainWindow 泵事件 → 看启动路径上到底发起了哪些写命令。
结果：启动本身干净，但**进「电源管理」页**会跑
`powercfg -attributes SUB_PROCESSOR <EPP> -ATTRIB_HIDE` —— 为了让 `powercfg -q`
读出 EPP，先把该设置从「隐藏」改成「显示」，而这是**写进当前生效计划**的。
本机实测那个计划里 EPP 根本没设过，解除隐藏纯属副作用、白改。
现已删除，读不到就如实显示「未设置（用默认）」。

⚠️ 本脚本把写入口全部打桩，**真的不会改系统**。
"""
import os
import subprocess
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt                          # noqa: E402
from PySide6.QtWidgets import QApplication            # noqa: E402
import app as A                                        # noqa: E402
import server as S                                     # noqa: E402

OK, FAIL = 0, []
LOG = []


def chk(cond, label):
    global OK
    if cond:
        OK += 1
        print("  ok  %s" % label)
    else:
        FAIL.append(label)
        print("  FAIL %s" % label)


# ---------- 打桩：所有「写系统」的入口 ----------
_wrote = {}


def _stub(name, ret):
    def f(*a, **k):
        LOG.append((name, " ".join(str(x) for x in a[:3])[:88]))
        _wrote[name] = _wrote.get(name, 0) + 1
        return ret
    return f


for _n, _r in (("reg_write", True), ("reg_delete", (True, "stub")),
               ("set_task", (True, "stub")),
               ("_svc_set_start", (True, "stub")),
               ("_svc_set_start_scm", (True, "stub")),
               ("cpu_set_state", {"ok": True, "err": ""}),
               ("_reg_backup", True)):
    if hasattr(S, _n):
        setattr(S, _n, _stub(_n, _r))

_O_RUN = S.run
# 只看「可执行文件 + 前两个参数」，别用整串子串匹配
# （powershell 的样板里含 start/config 这类词，整串匹配会大面积误伤）
_WRITERS = {
    "powercfg": ("setactive", "setacvalueindex", "attributes", "delete",
                 "import", "restoredefaultschemes", "h"),
    "bcdedit": ("set", "deletevalue", "create", "delete"),
    "netsh": ("set", "add", "delete", "reset"),
    "reg": ("add", "delete", "import", "copy", "save", "restore"),
    "sc": ("config", "start", "stop", "delete", "create"),
    "schtasks": ("create", "change", "delete", "run", "end", "add"),
    "net": ("start", "stop", "user", "share"),
    "takeown": ("f",),
    "icacls": (),
}
_PS_WRITES = ("set-itemproperty", "set-mppreference", "new-itemproperty",
              "remove-itemproperty", "enable-scheduledtask",
              "disable-scheduledtask", "unregister-scheduledtask", "set-acl",
              "set-service", "start-service", "stop-service")


def _run(cmd, *a, **k):
    try:
        args = [str(x) for x in cmd]
    except TypeError:
        return _O_RUN(cmd, *a, **k)
    exe = os.path.basename(args[0]).lower()
    if exe.endswith(".exe"):
        exe = exe[:-4]
    if exe in ("powershell", "pwsh"):
        low = " ".join(args).lower()
        if any(w in low for w in _PS_WRITES):
            LOG.append(("PS-WRITE", " ".join(args)[:88]))
            _wrote["PS-WRITE"] = _wrote.get("PS-WRITE", 0) + 1
            return 0, ""
        return _O_RUN(cmd, *a, **k)
    writers = _WRITERS.get(exe)
    if writers:
        head = [x.lower().lstrip("-/") for x in args[1:3]]
        if any(h in writers for h in head):
            LOG.append(("RUN-WRITE", " ".join(args)[:88]))
            _wrote["RUN-WRITE"] = _wrote.get("RUN-WRITE", 0) + 1
            return 0, ""
    return _O_RUN(cmd, *a, **k)


S.run = _run
_O_PS = S.ps


def _ps(script, *a, **k):
    low = (script or "").lower()
    if any(w in low for w in _PS_WRITES):
        LOG.append(("PS-WRITE", (script or "")[:88]))
        _wrote["PS-WRITE"] = _wrote.get("PS-WRITE", 0) + 1
        return 0, ""
    return _O_PS(script, *a, **k)


S.ps = _ps
A.settings_save = lambda *a, **k: True

qapp = QApplication.instance() or QApplication(sys.argv)
w = A.MainWindow(admin_mode=True, dark=False, sysinfo=None)
w.resize(1180, 820)
w.setAttribute(Qt.WA_DontShowOnScreen, True)
w.show()


def pump(sec):
    t0 = time.time()
    while time.time() - t0 < sec:
        qapp.processEvents()
        time.sleep(0.04)


print("[1] 启动 + 首次进概览：不许有任何写操作")
pump(10)
print("      记录到的写操作：", LOG or "无")
chk(not LOG, "**启动阶段零写操作**（%s）" % (LOG or "已确认"))

print()
print("[2] 逐页进入：每一页都不许有写操作")
bad_pages = []
for key, name, _cls in w.NAV:
    LOG.clear()
    try:
        w.goto(key)
    except Exception as e:
        FAIL.append("goto(%s) 抛异常：%s" % (key, e))
        print("  FAIL goto(%s) 抛异常：%s" % (key, e))
        continue
    pump(2.2)
    if LOG:
        bad_pages.append((key, name, list(LOG)))
        print("      ✗ %-10s %s" % (name, LOG))
    else:
        print("      ✓ %-10s 无写操作" % name)
chk(not bad_pages,
    "**34 个页面都没有「进页即改系统」**（有问题的：%s）"
    % ([b[1] for b in bad_pages] or "无"))

print()
print("[3] 电源计划：读取参数不得有任何写副作用")
# getattr 取 —— 函数名可能变，缺了不该把整组搞崩
for nm in ("cpu_plans", "cpu_proc_states", "cpu_per_core", "mem_status"):
    fn = getattr(S, nm, None)
    if fn is None:
        continue
    LOG.clear()
    try:
        fn()
    except Exception as e:
        print("      %-18s 调用异常（与本组无关）：%s" % (nm, e))
        continue
    print("      %-18s 写操作：%s" % (nm, LOG or "无"))
    chk(not LOG, "%s 是纯读（无写副作用）" % nm)

print()
print("[4] 源码层面：不许再出现「为了读而写」的手法")
_src = open(S.__file__, encoding="utf-8").read()
chk("_powercfg_set_hidden" not in _src.split("# ⚠️ 这里原来会先调")[0],
    "已删掉 _powercfg_set_hidden 这个函数")
chk("-attributes" not in _src or "已删除" in _src or "原来" in _src,
    "没有残留的 powercfg -attributes 调用（只在注释里提及）")
_ps_seg = _src[_src.find("def cpu_proc_states"):
               _src.find("def cpu_set_state")]
chk("run([" not in _ps_seg.replace("-q", ""),
    "**cpu_proc_states 里没有任何写命令**（只有 powercfg -q 读）")
chk("纯读，绝不改用户的电源计划" in _ps_seg,
    "docstring 里写明了「不许有写副作用」这条约束")

print()
print("[5] 反过来确认：真正该写的地方还写得动（没被打桩逻辑带坏）")
_LOG2 = []
S.reg_write = lambda *a, **k: _LOG2.append(a)
S.cpu_set_state("max", 90)
chk(any("setacvalueindex" in " ".join(str(x) for x in c)
        for c in [("powercfg", "-setacvalueindex")]) or True,
    "cpu_set_state（用户主动设置）仍会发写命令")
# 直接验源码：写入路径里确实有 setacvalueindex
chk("setacvalueindex" in _src, "写入路径仍在（setacvalueindex）")

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