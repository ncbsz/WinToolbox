# -*- coding: utf-8 -*-
"""只读探测：Defender 各组件的 Start / LaunchProtected / 服务键可写性。

用来解释「为什么 WaaSMedicSvc 没被禁掉」。
"""
import ctypes
import os
import sys
import winreg
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as S                                              # noqa: E402

START = {0: "Boot", 1: "System", 2: "自动", 3: "手动", 4: "**已禁用**"}
LP = {0: "（无保护）", 1: "LL", 2: "PP", 3: "PPL", None: "（未设置）"}

print("=" * 74)
print("Defender 各组件：Start / LaunchProtected / 服务键能否以写权限打开")
print("=" * 74)
adv = ctypes.windll.advapi32
KEY_SET_VALUE = 0x0002
for name, kind, _dflt, desc in S._DEF_COMPONENTS:
    p = "SYSTEM" + chr(92) + "CurrentControlSet" + chr(92) + "Services" + chr(92) + name
    row = {"start": None, "lp": None}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, p) as k:
            for v, key in (("Start", "start"), ("LaunchProtected", "lp")):
                try:
                    row[key] = winreg.QueryValueEx(k, v)[0]
                except OSError:
                    pass
    except OSError as e:
        print("   %-24s 打不开：%s" % (name, e))
        continue
    # ⚠️ 别用 advapi32.RegOpenKeyExW 裸调 —— HKEY 是 64 位句柄，
    #    不声明 argtypes 会 OverflowError（本项目踩过这个坑）。
    #    直接用 winreg 以写权限打开，被拒会抛 PermissionError。
    try:
        h = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, p, 0, winreg.KEY_SET_VALUE)
        winreg.CloseKey(h)
        writable = "可写"
    except PermissionError:
        writable = "**被拒（Access Denied）**"
    except OSError as e:
        writable = "**被拒（%s）**" % str(e)[:28]
    print("   %-22s Start=%-3s %-11s 保护=%-10s 写权限=%s"
          % (name, row["start"], START.get(row["start"], "?"),
             LP.get(row["lp"], row["lp"]), writable))

print()
print("=== 当前状态汇总（defender_status）===")
d = S.defender_status()
print("   realtime=%s tamper=%s enabled=%s" % (d.get("realtime"), d.get("tamper"),
                                               d.get("enabled")))
print("   已禁 %s/%s，还差：%s" % (d.get("n_disabled"), d.get("n_total"),
                                  d.get("remaining")))
print()
print("=== set_task 的替代实现（不依赖 schtasks）===")
print("   set_task 是否用 PowerShell ScheduledTasks：",
      "ScheduledTasks" in open(S.__file__, encoding="utf-8").read())
sys.stdout.flush()
os._exit(0)
