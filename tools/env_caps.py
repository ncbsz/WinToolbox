# -*- coding: utf-8 -*-
"""环境能力自检：逐条探测「本机/本沙箱能不能跑这个功能依赖的外部命令 + 有没有权限」。

目的：给用户一份**诚实的边界清单** —— 哪些功能是「实测过能用」，
哪些是「我这里根本验不到」（缺管理员 / 命令被策略拦）。
⚠️ 全部是**只读**命令，不改任何系统状态。
"""
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as S                                              # noqa: E402


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def probe(label, cmd, timeout=25):
    """跑一条只读命令，返回 (rc, 输出摘要)。"""
    try:
        rc, out = S.run(cmd, timeout=timeout)
    except Exception as e:
        return None, "异常：%s" % str(e)[:50]
    txt = (out or "").strip().replace("\r", " ").replace("\n", " | ")
    return rc, txt[:90]


print("=" * 78)
print("环境能力自检（只读，不改任何系统状态）")
print("=" * 78)
print("  管理员权限：%s" % ("是" if is_admin() else "**否**"))
print("  Python：%s" % sys.version.split()[0])
print("  程序目录：%s" % S.APP_DIR)
print()

GROUPS = [
    ("电源计划", [
        ("powercfg /list", ["powercfg", "/list"]),
    ]),
    ("BCD / 启动配置", [
        ("bcdedit /enum", ["bcdedit", "/enum", "{current}"]),
    ]),
    ("显卡设备", [
        ("pnputil /?", ["pnputil", "/?"]),
    ]),
    ("服务管理", [
        ("sc.exe query", ["sc", "query", "wuauserv"]),
    ]),
    ("计划任务", [
        ("schtasks /query", ["schtasks", "/query", "/tn",
                            r"\Microsoft\Windows\Windows Defender\Windows Defender Cleanup"]),
    ]),
    ("注册表命令", [
        ("reg.exe query", ["reg", "query", r"HKCU\Software"]),
    ]),
    ("网络", [
        ("netsh 帮助", ["netsh", "interface", "show", "interface"]),
        ("ipconfig", ["ipconfig", "/all"]),
    ]),
    ("磁盘", [
        ("fsutil（卷信息）", ["fsutil", "fsinfo", "drives"]),
    ]),
    ("Defender 读取", [
        ("Get-MpComputerStatus", ["powershell", "-NoProfile", "-Command",
                                  "$s=Get-MpComputerStatus;"
                                  "'{0}|{1}|{2}' -f $s.RealTimeProtectionEnabled,"
                                  "$s.AntivirusEnabled,$s.IsTamperProtected"]),
    ]),
    ("PowerShell 通道", [
        ("简单计算", ["powershell", "-NoProfile", "-Command", "1+1"]),
    ]),
]

for gname, items in GROUPS:
    print("【%s】" % gname)
    for label, cmd in items:
        rc, txt = probe(label, cmd)
        if rc is None:
            flag = "⚠️ 异常"
        elif rc == 0 and txt:
            flag = "✅ 可用"
        elif rc == 0 and not txt:
            flag = "⚠️ rc=0 但**无输出**"
        else:
            flag = "❌ 不可用（rc=%s）" % rc
        print("   %-22s %s" % (label, flag))
        if txt:
            print("        %s" % txt)
    print()

# 软件自己的关键只读接口（不依赖外部命令的，应该都能用）
print("=" * 78)
print("软件自身的只读接口（走 winreg / ctypes，不经外部命令）")
print("=" * 78)
SELF = [
    ("cpu_plans（读电源计划）", lambda: S.cpu_plans()),
    ("defender_status（读 Defender）", lambda: S.defender_status()),
    ("update_status（读更新状态）", lambda: S.update_status()),
    ("gpu_adapters（读显卡）", lambda: S.gpu_adapters()),
    ("list_processes（读进程）", lambda: S.list_processes(limit=5)),
    ("own_state_export（自有注册表状态）", lambda: S.own_state_export()),
    ("power_active_snapshot（当前电源计划）", lambda: S.power_active_snapshot()),
]
for label, fn in SELF:
    try:
        r = fn()
        n = len(r) if isinstance(r, (list, dict)) else "—"
        print("   %-38s ✅ 可用（%s 项）" % (label, n))
    except Exception as e:
        print("   %-38s ❌ 异常：%s" % (label, str(e)[:60]))
print()
sys.stdout.flush()
os._exit(0)
