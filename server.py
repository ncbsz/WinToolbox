#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WinToolbox backend  —  pure stdlib, no pip installs required.

Serves the Fluent (blue/white) web UI from ./web and exposes a JSON API that
performs REAL Windows operations: registry tweaks, service control, process /
memory management, network repair, uninstall listing, junk cleanup and policy
diagnosis.

Run:  python server.py            (browser opens automatically)
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

try:
    import winreg
except ImportError:
    winreg = None

if getattr(sys, "frozen", False):
    # PyInstaller onefile: bundled data lands in _MEIPASS, but writable state
    # must live next to the .exe so it survives restarts.
    ROOT = sys._MEIPASS
    APP_DIR = os.path.dirname(sys.executable)
else:
    ROOT = os.path.dirname(os.path.abspath(__file__))
    APP_DIR = ROOT

WEB = os.path.join(ROOT, "web")
DATA = os.path.join(APP_DIR, "data")
BACKUP = os.path.join(APP_DIR, "backup")
RULES_FILE = os.path.join(ROOT, "rules.json")
JOURNAL = os.path.join(DATA, "journal.json")

for d in (DATA, BACKUP):
    os.makedirs(d, exist_ok=True)
if not os.path.isdir(WEB):
    os.makedirs(WEB, exist_ok=True)

HOST, PORT = "127.0.0.1", 8760
HIVE = {"HKLM": 0x80000002, "HKCU": 0x80000001, "HKCR": 0x80000000, "HKU": 0x80000003,
        "HKCC": 0x80000005}

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# 子进程并发闸门：老机器上 PowerShell 起一次要 0.3~1.5s CPU，启动瞬间实测有 10 个
# 并发（系统信息 + 性能采样 + 网卡枚举 + PDH 建连），双核机直接被顶满。这里限制同时
# 最多 3 个，其余排队 —— 总工作量不变，但 CPU 峰值被削平。
_CHILD_SEM = threading.Semaphore(3)


def run(cmd, timeout=25, shell=False):
    """Run a command, return (rc, stdout+stderr text).

    受 `_CHILD_SEM` 限流；排队超过 30 秒就直接跑（宁可超限也不要卡死调用方）。
    """
    _got = _CHILD_SEM.acquire(timeout=30)
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, shell=shell,
                           creationflags=0x08000000)   # CREATE_NO_WINDOW
        out = (p.stdout or b"") + (p.stderr or b"")
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                return p.returncode, out.decode(enc)
            except Exception:
                continue
        return p.returncode, out.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"
    except Exception as e:
        return -1, str(e)
    finally:
        # ⚠️ 只有真的 acquire 到了才能 release：早先无条件 release，
        # 排队超时的那些线程会把信号量计数越加越大 —— 限流在最需要的时候
        # （并发长任务）反而失效。
        if _got:
            _CHILD_SEM.release()


_PS_PREFIX = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
              "$OutputEncoding=[System.Text.Encoding]::UTF8;")


def ps(script, timeout=45):
    """Run PowerShell with UTF-8 stdout so Chinese text survives."""
    return run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-Command", _PS_PREFIX + script], timeout=timeout)


def split_hive(path):
    parts = path.split("\\", 1)
    return parts[0].upper(), (parts[1] if len(parts) > 1 else "")


# 注册表根**句柄**映射：读、写、删共用一张表 —— 早先 reg_read 少了 HKU，
# 传 HKU 路径会被当成 HKCU 去读，读到的是另一份配置（静默读错）。
# ⚠️ 注意别和下面 `_HIVE_NAMES`（存的是给 regedit 用的名字**字符串**）搞混：
# 两张表名字太像会互相覆盖（改名前就踩过：reg_read 拿到字符串，整条规则库全崩）。
def _hive_keys():
    if winreg is None:
        return {}
    return {"HKCR": winreg.HKEY_CLASSES_ROOT,
            "HKLM": winreg.HKEY_LOCAL_MACHINE,
            "HKCU": winreg.HKEY_CURRENT_USER,
            "HKU": winreg.HKEY_USERS,
            "HKCC": winreg.HKEY_CURRENT_CONFIG}


_HIVE_KEYS = _hive_keys()


def reg_read(path, value):
    """Return (exists, type, data)."""
    if winreg is None:
        return False, None, None
    hive, sub = split_hive(path)
    try:
        with winreg.OpenKey(_HIVE_KEYS.get(hive) or winreg.HKEY_CURRENT_USER,
                            sub, 0, winreg.KEY_READ) as k:
            data, typ = winreg.QueryValueEx(k, value)
            return True, typ, data
    except FileNotFoundError:
        return False, None, None
    except OSError:
        return False, None, None


def reg_write(path, value, typ, data):
    hive, sub = split_hive(path)
    root = _HIVE_KEYS.get(hive)
    if root is None:
        raise ValueError("不支持的注册表根：%s" % hive)
    with winreg.CreateKeyEx(root, sub, 0, winreg.KEY_SET_VALUE) as k:
        t = {"REG_DWORD": winreg.REG_DWORD, "REG_SZ": winreg.REG_SZ,
             "REG_EXPAND_SZ": winreg.REG_EXPAND_SZ}.get(typ, winreg.REG_SZ)
        d = int(data) if t == winreg.REG_DWORD else str(data)
        winreg.SetValueEx(k, value, 0, t, d)


def _delete_tree(root, sub):
    """递归删除注册表键（含所有子键）。

    DeleteKeyEx 无法删除仍有子键的键，而很多规则（如经典右键菜单）写入的是
    ...\\CLSID\\{GUID}\\InprocServer32 这种深层路径，撤销时要删掉最外层键，
    必须先把子键清干净，否则撤销会静默失败 —— 表现就是"改回去了但没生效"。

    注意：DeleteKeyEx 传入 KEY_WOW64_64KEY 会返回 WinError 87（参数错误），
    因此这里统一用 DeleteKey（无视图参数），对 HKCU/HKLM 的 Classes 均有效。
    """
    try:
        with winreg.OpenKey(root, sub, 0, winreg.KEY_READ) as k:
            children = []
            i = 0
            while True:
                try:
                    children.append(winreg.EnumKey(k, i))
                    i += 1
                except OSError:
                    break
    except FileNotFoundError:
        return
    except OSError:
        children = []
    for c in children:
        _delete_tree(root, sub + "\\" + c)
    try:
        winreg.DeleteKey(root, sub)
    except FileNotFoundError:
        pass
    except OSError:
        # 个别键受 ACL 保护，退回 reg.exe
        hive = ("HKCU" if root == winreg.HKEY_CURRENT_USER else
                "HKLM" if root == winreg.HKEY_LOCAL_MACHINE else
                "HKCR" if root == winreg.HKEY_CLASSES_ROOT else "HKU")
        run(["reg", "delete", hive + "\\" + sub, "/f"])


def reg_delete(path, value):
    hive, sub = split_hive(path)
    root = _HIVE_KEYS.get(hive)
    if root is None:
        raise ValueError("不支持的注册表根：%s" % hive)
    try:
        if value is None or value == "":
            # 键是否真的存在（避免对不存在的键做无意义操作）
            try:
                with winreg.OpenKey(root, sub, 0, winreg.KEY_READ):
                    pass
            except FileNotFoundError:
                return True          # 已经没有这个键了，视作删除成功
            _delete_tree(root, sub)
            # 复核
            try:
                with winreg.OpenKey(root, sub, 0, winreg.KEY_READ):
                    return False     # 仍然存在 -> 删除失败
            except FileNotFoundError:
                return True
        with winreg.OpenKey(root, sub, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, value)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def hex_md5(b):
    import hashlib
    return hashlib.md5(b).hexdigest()


# --------------------------------------------------------------------------
# system info / metrics
# --------------------------------------------------------------------------
class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wt.DWORD), ("dwHighDateTime", wt.DWORD)]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---- 进程句柄 / 工作集 API：必须显式声明签名（64 位下的两个经典坑）----
# 1) 不声明 argtypes 时 ctypes 按 C int 转换参数 —— SetProcessWorkingSetSize 的
#    SIZE_T(-1) 超出 int 范围，直接抛 "OverflowError: int too long to convert"
#    （argument 2），内存整理整条链路全废；
# 2) 不声明 restype 时 OpenProcess 返回的 64 位句柄被按「有符号 32 位」解析，
#    高位句柄会被截断成负数（CloseHandle/SetProcessWorkingSetSize 随即失败）。
_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k32.OpenProcess.restype = ctypes.c_void_p
_k32.GetCurrentProcess.restype = ctypes.c_void_p
_k32.SetProcessWorkingSetSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                          ctypes.c_size_t]
_k32.SetProcessWorkingSetSize.restype = wt.BOOL
_k32.CloseHandle.argtypes = [ctypes.c_void_p]
_k32.CloseHandle.restype = wt.BOOL

# ---- 其余「返回/接收句柄」的 kernel32 调用：同样必须显式声明 ----
# 这一类漏声明的后果都是静默的：句柄被截断 → 调用失败 → 只看返回值的代码
# 以为「这台机器不支持」，界面上表现为"功能莫名其妙不生效"。
_k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
_k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
_k32.OpenThread.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k32.OpenThread.restype = ctypes.c_void_p
_k32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
_k32.SetProcessAffinityMask.restype = wt.BOOL
_k32.SetThreadAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
_k32.SetThreadAffinityMask.restype = ctypes.c_size_t
_k32.GetProcessAffinityMask.argtypes = [ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_size_t),
                                        ctypes.POINTER(ctypes.c_size_t)]
_k32.GetProcessAffinityMask.restype = wt.BOOL
_k32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, wt.DWORD, wt.LPWSTR,
                                            ctypes.POINTER(wt.DWORD)]
_k32.QueryFullProcessImageNameW.restype = wt.BOOL

# INVALID_HANDLE_VALUE：restype 声明成 c_void_p 后，失败返回的是
# 0xFFFFFFFFFFFFFFFF 而不是 -1 —— 老式 `snap == -1` 判断会漏掉，
# 于是拿着无效句柄继续枚举（结果是空列表，白跑一轮）。
_INVALID_HANDLE = 0xFFFFFFFFFFFFFFFF


def _bad_handle(h):
    return not h or h in (-1, _INVALID_HANDLE)

# ---- 带缓存的重查询一律加锁（单飞）----
# 这些函数都跑在后台线程里，而 UI 侧有 1 秒心跳 + 两个常驻采样线程会并发调用它们。
# 没锁时会出现「缓存击穿」：两个线程同时看到缓存为空 → 同一条 PowerShell / nvidia-smi
# 被同时发出去两份（实测启动瞬间 sys_basic 跑 2 遍、nvidia-smi 同一秒跑 2 遍）。
_SYS_BASIC_LOCK = threading.Lock()
_NV_LOCK = threading.Lock()
_CPU_TEMP_LOCK = threading.Lock()
_GPU_AGG_LOCK = threading.Lock()
_NET_ADAPTERS_FAIL_TTL = 5.0     # 网卡信息查询失败后的退避时间


def mem_status():
    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    _k32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m


_cpu_prev = {"idle": 0, "kernel": 0, "user": 0}


def cpu_percent():
    i, k, u = FILETIME(), FILETIME(), FILETIME()
    if not _k32.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u)):
        return 0.0

    def val(f):
        return (f.dwHighDateTime << 32) | f.dwLowDateTime
    ni, nk, nu = val(i), val(k), val(u)
    di, dk, du = ni - _cpu_prev["idle"], nk - _cpu_prev["kernel"], nu - _cpu_prev["user"]
    _cpu_prev.update(idle=ni, kernel=nk, user=nu)
    total = dk + du
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, (total - di) * 100.0 / total))


_SYS_BASIC = {"v": None}

# Windows 功能更新代号：build number → 发布名（注册表读不到 DisplayVersion 时兜底）
_WIN_RELEASE_BY_BUILD = {
    10240: "1507", 10586: "1511", 14393: "1607", 15063: "1703",
    16299: "1709", 17134: "1803", 17763: "1809", 18362: "1903",
    18363: "1909", 19041: "2004", 19042: "20H2", 19043: "21H1",
    19044: "21H2", 19045: "22H2",
    22000: "21H2", 22621: "22H2", 22631: "23H2",
    26100: "24H2", 26200: "25H2",
}


def os_release():
    """Windows 功能更新代号 + 完整内部版本号，如 ("25H2", "26200.9168")。

    优先读注册表 DisplayVersion（微软维护的权威值，Win10 1903 起才有）；
    读不到时用 build number 查表兜底。两者都没有就返回 ("", "")。
    """
    rel, build, ubr = "", 0, 0
    if winreg is not None:
        try:
            k = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
                0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
            try:
                def q(n):
                    try:
                        return winreg.QueryValueEx(k, n)[0]
                    except OSError:
                        return None
                rel = str(q("DisplayVersion") or "").strip()
                build = int(q("CurrentBuildNumber") or q("CurrentBuild") or 0)
                ubr = int(q("UBR") or 0)
            finally:
                winreg.CloseKey(k)
        except Exception:
            pass
    if not rel and build:
        rel = _WIN_RELEASE_BY_BUILD.get(build, "")
    return rel, (("%d.%d" % (build, ubr)) if build else "")


def cpu_base_mhz():
    """CPU 标称基准频率(MHz)：CallNtPowerInformation 的 MaxMhz（任务管理器「基准速度」同源）。

    注册表 ~MHz 在 Win11 上会跟随当前频率浮动，不可作基准值来源；API 失败时才退回它。
    """
    try:
        class _PPI(ctypes.Structure):
            _fields_ = [("Number", wt.ULONG), ("MaxMhz", wt.ULONG),
                        ("CurrentMhz", wt.ULONG), ("MhzLimit", wt.ULONG),
                        ("MaxIdleState", wt.ULONG), ("CurrentIdleState", wt.ULONG)]
        arr = (_PPI * (os.cpu_count() or 1))()
        rc = ctypes.windll.powrprof.CallNtPowerInformation(11, None, 0, arr, ctypes.sizeof(arr))
        if rc == 0 and arr[0].MaxMhz:
            return int(arr[0].MaxMhz)
    except Exception:
        pass
    if winreg is not None:
        try:
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                               r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            v = int(winreg.QueryValueEx(k, "~MHz")[0])
            winreg.CloseKey(k)
            return v
        except OSError:
            pass
    return 0


def sys_basic():
    """One PowerShell round-trip instead of three (keeps /api/sysinfo fast).

    首次约 1.8s，之后进程内缓存直接返回（OS 版本 / CPU / 核数开机后不会变）。
    """
    with _SYS_BASIC_LOCK:
        if _SYS_BASIC["v"] is not None:
            return _SYS_BASIC["v"]
        rc, out = ps("$o=Get-CimInstance Win32_OperatingSystem; "
                     "$p=Get-CimInstance Win32_Processor | Select-Object -First 1; "
                     "'{0}|||{1}|||{2}|||{3}|||{4}|||{5}' -f $o.Caption,$o.Version,$o.OSArchitecture,"
                     "$p.Name,$p.NumberOfCores,$p.NumberOfLogicalProcessors")
        f = out.strip().split("|||")
        if len(f) < 6:
            f = (f + [""] * 6)[:6]
        try:
            cores, logical = int(f[4]), int(f[5])
        except Exception:
            cores = logical = os.cpu_count() or 1
        res = {"caption": f[0].strip(), "version": f[1].strip(), "arch": f[2].strip(),
               "cpu": f[3].strip() or "Unknown CPU", "cores": cores, "logical": logical}
        # CPU 标称基准频率（任务管理器「基准速度」同源：CallNtPowerInformation 的 MaxMhz；
        # 注册表 ~MHz 在 Win11 上会跟随当前频率浮动，仅作兜底）
        res["base_mhz"] = cpu_base_mhz()
        # 功能更新代号（24H2 / 25H2…）+ 完整内部版本号（26200.9168）
        rel, full = os_release()
        res["release"], res["build_full"] = rel, full
        # 只有真读到东西才写永久缓存：WMI/PowerShell 偶发失败（冷启动、杀软拦截、
        # 执行策略受限）时若把空结果缓存下来，这台机器整个会话都显示空白且不再自愈。
        if res["cpu"] and res["caption"]:
            _SYS_BASIC["v"] = res
        return res

def disks():
    res = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        d = letter + ":\\"
        if os.path.exists(d):
            try:
                t, u, f = shutil.disk_usage(d)
                res.append({"drive": d, "total": t, "used": t - f, "free": f,
                            "percent": round((t - f) * 100.0 / t, 1)})
            except Exception:
                pass
    return res


# GetTickCount64 返回 ULONGLONG，ctypes 默认按「有符号 32 位」解析 ——
# 连续开机超过 24.8 天就会变成负数，界面上的运行时长直接读不出来。
_k32.GetTickCount64.restype = ctypes.c_ulonglong

_UPTIME_CACHE = {"t": 0.0, "v": None}


def uptime_seconds():
    """开机时长（秒）。

    首选 GetTickCount64（已修正 restype）；异常或数值不合理时回退 WMI
    （`(Get-Date) - LastBootUpTime`，结果缓存 60s，避免反复起进程）；都失败返回 None。
    """
    try:
        v = int(_k32.GetTickCount64() / 1000)
        if v > 0:
            return v
    except Exception:
        pass
    now = time.time()
    if _UPTIME_CACHE["v"] is not None and now - _UPTIME_CACHE["t"] < 60:
        return _UPTIME_CACHE["v"]
    rc, out = ps("[int]((Get-Date) - "
                 "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime).TotalSeconds",
                 timeout=25)
    m = re.findall(r"-?\d+", out or "")
    if rc == 0 and m:
        v = int(m[-1])
        if v > 0:
            _UPTIME_CACHE["t"], _UPTIME_CACHE["v"] = now, v
            return v
    return None


def boot_time():
    """开机时刻（本地时间，如 2026-09-24 12:03）；取不到返回 ""。"""
    u = uptime_seconds()
    if not u:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - u))
    except Exception:
        return ""


# --------------------------------------------------------------------------
# 硬件标识：主板 / 显示器（EDID）/ 内存条 —— 都是一次查询 + 进程内缓存，
# 不进 1 秒心跳；UI 侧只在「系统信息」刷新时取一次。
# --------------------------------------------------------------------------
_VENDOR_CN = [
    ("asustek", "华硕"), ("asus", "华硕"), ("micro-star", "微星"), ("msi", "微星"),
    ("gigabyte", "技嘉"), ("asrock", "华擎"), ("biostar", "映泰"), ("colorful", "七彩虹"),
    ("maxsun", "铭瑄"), ("onda", "昂达"), ("soyo", "梅捷"), ("ecs ", "精英"),
    ("foxconn", "富士康"), ("pegatron", "和硕"), ("compal", "仁宝"), ("quanta", "广达"),
    ("clevo", "蓝天"), ("tongfang", "同方"), ("hasee", "神舟"), ("lenovo", "联想"),
    ("hewlett", "惠普"), ("hp ", "惠普"), ("dell", "戴尔"), ("acer", "宏碁"),
    ("samsung", "三星"), ("intel", "英特尔"), ("microsoft", "微软"), ("apple", "苹果"),
    ("huawei", "华为"), ("supermicro", "超微"), ("razer", "雷蛇"), ("lg ", "LG"),
    ("crucial", "英睿达"), ("micron", "镁光"), ("kingston", "金士顿"),
    ("hynix", "海力士"), ("ramaxel", "记忆科技"), ("adata", "威刚"),
    ("corsair", "海盗船"), ("g.skill", "芝奇"), ("teamgroup", "十铨"),
    ("kimtigo", "金泰克"), ("gloway", "光威"), ("asgard", "阿斯加特"),
    ("netac", "朗科"), ("a-data", "威刚"), ("transcend", "创见"), ("pny", "必恩威"),
]
# 长键优先，避免 "hp" 抢了 "hpe"、"asus" 抢了 "asustek"
_VENDOR_KEYS = sorted(_VENDOR_CN, key=lambda kv: -len(kv[0]))


def vendor_cn(name):
    """常见厂商英文名 → 中文简称；没命中就原样返回，绝不猜。"""
    low = (name or "").strip().lower()
    if not low:
        return ""
    for key, cn in _VENDOR_KEYS:
        if low.startswith(key.strip()):
            return cn
    return (name or "").strip()


_BASEBOARD = {"v": None}


def baseboard():
    r"""主板厂商 + 型号。

    优先注册表 `HKLM\HARDWARE\DESCRIPTION\System\BIOS`（0 开销），
    读不到再回退 WMI Win32_BaseBoard（一次查询并缓存）。
    """
    if _BASEBOARD["v"] is not None:
        return _BASEBOARD["v"]
    man = prod = ""
    if winreg is not None:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\BIOS", 0,
                                winreg.KEY_READ) as k:
                def _gv(nm):
                    try:
                        return str(winreg.QueryValueEx(k, nm)[0]).strip()
                    except OSError:
                        return ""
                man, prod = _gv("BaseBoardManufacturer"), _gv("BaseBoardProduct")
                if not prod:                      # 部分 OEM 只填 System* 组
                    man = man or _gv("SystemManufacturer")
                    prod = _gv("SystemProductName")
        except OSError:
            pass
    if not prod:
        rc, out = ps("(Get-CimInstance Win32_BaseBoard).Manufacturer + '|||' + "
                     "(Get-CimInstance Win32_BaseBoard).Product", timeout=30)
        f = ((out or "").strip().split("|||") + ["", ""])[:2]
        man = man or f[0].strip()
        prod = prod or f[1].strip()
    label = ("%s %s" % (vendor_cn(man), prod)).strip() if man else prod.strip()
    res = {"manufacturer": man.strip(), "product": prod.strip(), "label": label}
    if man or prod:                 # 读不到就下次重试，别把空结果永久缓存
        _BASEBOARD["v"] = res
    return res


# EDID 里的三位厂商码 → 面板/整机厂中文名
_EDID_VENDOR = {
    "AUO": "友达", "LGD": "LG Display", "LPL": "LG Display", "LTM": "LG Display",
    "BOE": "京东方", "SEC": "三星", "SDC": "三星", "SAM": "三星",
    "CSO": "华星光电", "CMN": "奇美", "CMO": "奇美", "CPT": "中华映管",
    "IVM": "冠捷", "AOC": "冠捷", "DEL": "戴尔", "ACI": "华硕", "AUS": "华硕",
    "HWP": "惠普", "LEN": "联想", "VSC": "优派", "MSI": "微星", "BNQ": "明基",
    "ACR": "宏碁", "PHL": "飞利浦", "SNY": "索尼", "NEC": "NEC", "PAN": "松下",
    "SHP": "夏普", "HEC": "现代", "HIT": "日立", "TOS": "东芝", "TCL": "TCL",
    "INL": "群创", "HKC": "惠科", "SKG": "天马",
}

_MONITORS = {"v": None}


def _edid_parse(b):
    """解析 128 字节 EDID 头：型号名 / 厂商码 / 商品码 / 对角尺寸。"""
    if len(b) < 128 or bytes(b[0:8]) != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return None
    v = (b[8] << 8) | b[9]
    pnp = "".join(chr(((v >> sh) & 0x1F) + 64) for sh in (10, 5, 0))
    pnp = "".join(ch for ch in pnp if ch.isalpha())
    code = "%04X" % ((b[11] << 8) | b[10])          # 商品码（小端）
    w_cm, h_cm = b[21], b[22]
    try:
        inch = round(((w_cm ** 2 + h_cm ** 2) ** 0.5) / 2.54, 1) if (w_cm and h_cm) else None
    except Exception:
        inch = None
    name = ""
    for off in (54, 72, 90, 108):                   # 四个描述符块找 0xFC = 名称
        blk = bytes(b[off:off + 18])
        if len(blk) == 18 and blk[0:3] == b"\x00\x00\x00" and blk[3] == 0xFC:
            name = blk[5:18].decode("ascii", "ignore").replace("\x00", " ").strip()
            if name:
                break
    return {"name": name, "pnp": (pnp + code) if pnp else code,
            "vendor": _EDID_VENDOR.get(pnp, pnp), "inch": inch}


_FAKE_MON_NAMES = ("hdmi", "display", "generic", "default", "vga", "dvi", "dp")


def _monitor_score(m):
    """给一块屏打分，用来把虚拟显示器的伪 EDID 排到后面。"""
    score = 0
    name = (m.get("name") or "").strip()
    low = name.lower()
    if name and not any(low.startswith(x) for x in _FAKE_MON_NAMES) and len(name) >= 4:
        score += 1                              # 型号名像真的（B156HAN15.H / DELL U2419H）
    if m.get("pnp", "")[:3] in _EDID_VENDOR:
        score += 1                              # 厂商码可识别
    inch = m.get("inch") or 0
    if 8 <= inch <= 34:
        score += 1                              # 笔记本内置屏 / 常见桌面显示器
    return score


def monitors():
    """显示器：解析注册表里每块屏的 EDID（毫秒级，且不依赖虚拟显示器配合）。

    虚拟显示器一般没有 EDID，自然被过滤掉；结果按屏幕尺寸从大到小排序。
    """
    if _MONITORS["v"] is not None:
        return _MONITORS["v"]
    out, seen = [], set()
    root = r"SYSTEM\CurrentControlSet\Enum\DISPLAY"
    if winreg is not None:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root, 0,
                                winreg.KEY_READ) as rk:
                subs = [winreg.EnumKey(rk, i) for i in range(winreg.QueryInfoKey(rk)[0])]
        except OSError:
            subs = []
        for sub in subs:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root + "\\" + sub, 0,
                                    winreg.KEY_READ) as sk:
                    insts = [winreg.EnumKey(sk, i)
                             for i in range(winreg.QueryInfoKey(sk)[0])]
            except OSError:
                continue
            for inst in insts:
                try:
                    with winreg.OpenKey(
                            winreg.HKEY_LOCAL_MACHINE,
                            root + "\\" + sub + "\\" + inst + "\\Device Parameters",
                            0, winreg.KEY_READ) as dp:
                        raw = winreg.QueryValueEx(dp, "EDID")[0]
                except OSError:
                    continue
                info = _edid_parse(bytes(raw))
                if not info:
                    continue
                # 去重键用「型号键 + 实例」而不是型号名：
                # 两台同型号显示器（双屏很常见）型号名完全一样，按名字去重会丢掉一台。
                key = (sub, inst)
                if key in seen:
                    continue
                seen.add(key)
                info["_score"] = _monitor_score(info)
                out.append(info)

    # 排序：先"像真屏"的（型号名正常 / 厂商可识别 / 尺寸在面板常见区间），
    # 再按尺寸从大到小 —— 虚拟显示器驱动常伪造 EDID（型号写成 HDMI2.0、尺寸离谱），
    # 这样真正的内置屏 / 外接屏会排在前面，虚拟屏退到提示里。
    out.sort(key=lambda x: (-x.pop("_score", 0), -(x["inch"] or 0)))
    if out:                     # 读不到 EDID 就别缓存空结果（外接屏可能稍后才枚举到）
        _MONITORS["v"] = out
    return out


_MEM_MODULES = {"v": None}
_MEM_TYPES = {20: "DDR", 21: "DDR2", 24: "DDR3", 26: "DDR4", 27: "LPDDR",
              28: "LPDDR2", 29: "LPDDR3", 30: "LPDDR4", 34: "DDR5", 35: "LPDDR5"}


def memory_modules():
    """内存条明细：厂商 / 容量字节 / 频率 / 颗粒类型（一次 WMI 查询并缓存）。"""
    if _MEM_MODULES["v"] is not None:
        return _MEM_MODULES["v"]
    # 用 -join 拼字段，避免在 PowerShell 里再套一层带引号的格式串（引号转义最容易翻车）
    rc, out = ps("Get-CimInstance Win32_PhysicalMemory | ForEach-Object { "
                 "@($_.Manufacturer,$_.Capacity,$_.Speed,$_.ConfiguredClockSpeed,"
                 "$_.PartNumber,$_.SMBIOSMemoryType) -join '|||' }", timeout=30)
    res = []
    for line in (out or "").splitlines():
        f = [x.strip().strip('"') for x in line.split("|||")]
        if len(f) < 6:
            continue

        def _num(x):
            try:
                return int(float(x or 0))
            except ValueError:
                return 0

        res.append({"manufacturer": f[0], "capacity": _num(f[1]),
                    "speed": _num(f[3]) or _num(f[2]), "part": f[4],
                    "type": _MEM_TYPES.get(_num(f[5]), "")})
    if res:                         # 查不到就当没查到，下次重试（否则内存页永久空白）
        _MEM_MODULES["v"] = res
    return res


_HW_EXTRA = {"v": None}


def hw_extra(force=False):
    """物理磁盘（型号 / 容量 / 接口）与声卡列表：一次 PowerShell 查完，进程内只查一次。"""
    if _HW_EXTRA["v"] is not None and not force:
        return _HW_EXTRA["v"]
    rc, out = ps(
        "Get-CimInstance Win32_DiskDrive | ForEach-Object { "
        "'DD|' + $_.Model + '|' + $_.Size + '|' + $_.InterfaceType }; "
        "Get-CimInstance Win32_SoundDevice | ForEach-Object { "
        "if ($_.Name) { 'SD|' + $_.Name } }; "
        "Get-CimInstance Win32_VideoController | ForEach-Object { "
        "if ($_.Name) { 'VC|' + $_.Name + '|' + $_.AdapterRAM } }", timeout=30)
    disks, sounds, vcs = [], [], []
    for line in (out or "").splitlines():
        line = line.strip()
        if line.startswith("DD|"):
            p = line.split("|")
            if len(p) >= 4:
                try:
                    size = int(float(p[2] or 0))
                except ValueError:
                    size = 0
                disks.append({"model": p[1].strip(), "size": size,
                              "iface": p[3].strip()})
        elif line.startswith("SD|"):
            nm = line.split("|", 1)[1].strip()
            if nm:
                sounds.append({"name": nm})
        elif line.startswith("VC|"):
            p = line.split("|")
            if len(p) >= 3:
                try:
                    ram = int(float(p[2] or 0))
                except ValueError:
                    ram = 0
                vcs.append({"name": p[1].strip(), "ram": ram})
    res = {"disks": disks, "sounds": sounds, "vcs": vcs}
    if disks or sounds or vcs:   # 成功才缓存（失败下次再试）
        _HW_EXTRA["v"] = res
    return res


_GPU_VRAM = {"v": None}


def gpu_vram():
    """每块显卡的显存字节数：读注册表 HardwareInformation.qwMemorySize。

    WMI 的 AdapterRAM 是 32 位、超过 4GB 会截断成 4294967295，所以走注册表 64 位值。
    """
    if _GPU_VRAM["v"] is not None:
        return _GPU_VRAM["v"]
    out = {}
    if winreg is not None:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _GPU_CLASS) as k:
                n = winreg.QueryInfoKey(k)[0]
                for i in range(n):
                    sub = winreg.EnumKey(k, i)
                    if not re.match(r"^\d{4}$", sub):
                        continue
                    try:
                        with winreg.OpenKey(k, sub) as sk:
                            def q(v, _sk=sk):
                                try:
                                    return winreg.QueryValueEx(_sk, v)[0]
                                except OSError:
                                    return None
                            name = q("DriverDesc") or ""
                            mem = q("HardwareInformation.qwMemorySize")
                            if isinstance(mem, bytes):
                                mem = int.from_bytes(mem[:8], "little")
                            try:
                                mem = int(mem or 0)
                            except (TypeError, ValueError):
                                mem = 0
                            if name:
                                out[name] = mem
                    except OSError:
                        continue
        except FileNotFoundError:
            pass
    if out:                     # 读不到就别永久缓存（驱动装完/重插显卡后应能自愈）
        _GPU_VRAM["v"] = out
    return out


# --------------------------------------------------------------------------
# optimize rules engine
# --------------------------------------------------------------------------
def load_rules():
    with open(RULES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)["rules"]


def load_rules_with_state():
    """Return rules with a real `state` field = whether the system already
    matches the optimized value (used by the UI to pre-read current settings)."""
    rules = load_rules()
    for r in rules:
        r["state"] = rule_state(r)
    return rules


def optimize_applied_list():
    """本机**当前已生效**的优化项（`rule_state()` 实时读系统判定，不靠记录）。

    为什么不做「用户点过哪些」的流水记录：那个记录会与系统真实状态脱节
    （用户可能在软件外改回去、或换了台机器）。以系统实际状态为准最诚实，
    也正是配置档案要带走的东西。
    """
    out = []
    for r in load_rules():
        try:
            if rule_state(r) is True:
                out.append({"id": r["id"], "name": r.get("name", r["id"]),
                            "category": r.get("category", ""),
                            "risk": r.get("risk", "safe")})
        except Exception:
            continue          # 单条规则读失败不影响其余（也不谎报它已生效）
    return out


def optimize_profile_ids():
    """配置档案里只存 id 列表（体积小，且规则库随程序走）。"""
    return [x["id"] for x in optimize_applied_list()]


def optimize_missing(ids):
    """档案里带了的优化项里，本机**还没生效**的那些（返回完整规则项）。

    找不到对应规则（档案来自更新/更旧的规则库）的一律忽略 —— 不猜、不硬套。
    """
    want = {str(i) for i in (ids or [])}
    if not want:
        return []
    out = []
    for r in load_rules():
        if r["id"] not in want:
            continue
        try:
            st = rule_state(r)
        except Exception:
            st = None
        if st is not True:
            out.append(r)
    return out


def _val_match(data, want, mode="eq"):
    """规则值比较：mode="contains" 用于复合字符串，其余走 int 宽容比较。"""
    if mode == "contains":
        return str(want) in str(data or "")
    try:
        return int(data) == int(want)
    except (TypeError, ValueError):
        return str(data).strip() == str(want).strip()


def rule_state(rule):
    """Return True if the rule currently reads as 'already optimized'.

    支持两个可选字段（数据驱动，写在 rules.json 的规则里）：
      match: "contains" —— 复合字符串（如 DirectXUserGlobalSettings 的
              "GpuPreference=2;AutoHDR...;"）按「包含」判定。整串相等是错的：
              用户在设置里调过 AutoHDR / 窗口化优化之后，串里多了别的参数，
              整串比较会**永远显示未生效**，而点应用还会把已有参数整串抹掉。
      altChecks: [{key, value, data}, ...] —— 等效手段。同一个效果可以通过
              不同路径达成（禁用更新可以写策略键，也可以直接禁 wuauserv 服务；
              关快速启动可以用 powercfg /h off）。主键没命中但任一 altCheck
              命中，同样算已生效 —— 否则就会「我明明设置了，它还说没设置」。
    """
    ck = rule.get("checkKey")
    if not ck:
        return None
    mode = rule.get("match") or "eq"
    want = rule.get("optimizedValue")

    def hit(key, value, data_want):
        if not key:
            return False
        ex, _t, d = reg_read(key, value)
        if data_want is None:
            return not ex                     # 期望「值不存在」
        if not ex:
            return False
        return _val_match(d, data_want, mode)

    if hit(ck, rule.get("checkValue", ""), want):
        return True
    for alt in rule.get("altChecks") or []:
        try:
            if hit(alt.get("key"), alt.get("value", ""), alt.get("data")):
                return True
        except Exception:
            continue
    return False


def _merge_kv_string(cur, piece):
    """把 "GpuPreference=2;" 这类段合并进 "A=1;B=2;" 复合串。

    规则：同键段**替换**、其余原样保留、无则追加。整串覆盖会抹掉用户在
    设置 App 里调好的 AutoHDR / 窗口化优化等参数（实测踩过）。
    """
    piece = str(piece).strip().rstrip(";")
    key = piece.split("=", 1)[0].strip()
    parts = [p.strip() for p in str(cur or "").split(";") if p.strip()]
    out, replaced = [], False
    for p in parts:
        if p.split("=", 1)[0].strip().lower() == key.lower():
            out.append(piece)
            replaced = True
        else:
            out.append(p)
    if not replaced:
        out.append(piece)
    return "".join(s + ";" for s in out if s)


def apply_op(op, reverse=False):
    """执行一条写入操作，返回 (是否成功, 错误信息)。

    关键：必须真实反映结果。早期版本无论成败都返回 True，
    导致 UI 显示"已应用"但系统其实没改 —— 即"保存了没生效"。
    """
    kind = op.get("op")
    try:
        if kind == "reg_set":
            path, value, typ = op["key"], op["value"], op.get("type", "REG_DWORD")
            if reverse:
                if op.get("restoreType") == "delete":
                    ok = reg_delete(path, value)
                    return ok, "" if ok else "删除失败: %s\\%s" % (path, value)
                reg_write(path, value, op.get("restoreType", typ), op.get("restoreData", 0))
            else:
                data = op["data"]
                if op.get("merge"):
                    # 复合字符串合并写入：只替换/追加本段，保留用户已有的其它参数
                    _ex, _t, cur = reg_read(path, value)
                    data = _merge_kv_string(cur, data)
                reg_write(path, value, typ, data)
            return True, ""
        if kind == "reg_del":
            if reverse:
                reg_write(op["key"], op["value"], op.get("type", "REG_DWORD"), op.get("data", 0))
                return True, ""
            ok = reg_delete(op["key"], op["value"])
            return ok, "" if ok else "删除失败: %s\\%s" % (op["key"], op.get("value", ""))
        if kind == "svc":
            name = op["name"]
            start = op.get("restoreStart", 3) if reverse else op.get("start", 4)
            key = "HKLM\\SYSTEM\\CurrentControlSet\\Services\\" + name
            # 先确认服务真实存在（reg add 会凭空造出假键，让用户以为改成功了）
            ex, _t, _d = reg_read(key, "ImagePath")
            if not ex:
                return False, "服务 %s 不存在，已跳过" % name
            rc, out = run(["reg", "add", key, "/v", "Start",
                           "/t", "REG_DWORD", "/d", str(start), "/f"])
            if rc != 0:
                return False, (out or "").strip()[:120] or "服务 %s 设置失败" % name
            # 读回确认
            ex2, _t2, d2 = reg_read(key, "Start")
            if not (ex2 and int(d2) == int(start)):
                return False, "服务 %s Start 未生效（当前 %r，目标 %r）" % (name, d2, start)
            # 目标为禁用(4)：若服务正在运行，主动停止让改动立即生效
            if int(start) == 4 and not reverse:
                run(["sc", "stop", name], timeout=15)
            return True, ""
        return False, "unknown op"
    except PermissionError:
        return False, "权限不足（请以管理员身份运行）: %s" % op.get("key", op.get("name", ""))
    except FileNotFoundError:
        return False, "路径不存在: %s" % op.get("key", "")
    except OSError as e:
        return False, "系统错误: %s" % str(e)[:100]
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, str(e)[:100])


def journal_load():
    try:
        with open(JOURNAL, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def journal_save(items):
    with open(JOURNAL, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)


def snapshot_and_apply(rules, ids):
    """Backup the affected registry values, then apply the selected rules."""
    chosen = [r for r in rules if r["id"] in ids]
    journal = journal_load()
    entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "items": []}
    for r in chosen:
        for op in r.get("optimize", []):
            if op.get("op", "").startswith("reg") and op.get("key"):
                ex, typ, data = reg_read(op["key"], op.get("value", ""))
                entry["items"].append({"key": op["key"], "value": op.get("value", ""),
                                       "existed": ex,
                                       "type": {1: "REG_SZ", 2: "REG_EXPAND_SZ", 3: "REG_BINARY",
                                                4: "REG_DWORD", 7: "REG_MULTI_SZ"}.get(typ, "REG_SZ"),
                                       "data": data if not isinstance(data, bytes) else None})
    journal.insert(0, entry)
    journal_save(journal[:50])

    results = []
    for r in chosen:
        ok = True
        msg = ""
        for op in r.get("optimize", []):
            good, m = apply_op(op, reverse=False)
            ok = ok and good
            if m:
                msg = m.strip().splitlines()[0] if m.strip() else ""
        # 应用后复核：从系统重新读取，确认真的达到了 optimizedValue。
        # 这一步专门用来兜住"保存成功但系统没生效"的情况。
        verified = rule_state(r) if ok else None
        if ok and verified is False:
            ok = False
            msg = "已写入，但系统读回与目标不一致"
        results.append({"id": r["id"], "name": r["name"], "ok": ok, "msg": msg,
                        "verified": verified})
    return results


def restore_rules(rules, ids):
    """The `restore` array describes the desired end state explicitly, so it is
    executed with reverse=False (e.g. {"op":"reg_del"} deletes a value)."""
    chosen = [r for r in rules if r["id"] in ids]
    results = []
    for r in chosen:
        ok = True
        msg = ""
        for op in r.get("restore", []):
            good, m = apply_op(op, reverse=False)
            ok = ok and good
            if m:
                msg = m.strip().splitlines()[0] if m.strip() else ""
        results.append({"id": r["id"], "name": r["name"], "ok": ok, "msg": msg})
    return results


# --------------------------------------------------------------------------
# software / startup / services
# --------------------------------------------------------------------------
UNINSTALL_ROOTS = [
    ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_64KEY if winreg else 0),
    ("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", 0),
    ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0),
]


# 名称关键词：命中即视为系统组件（运行库 / SDK / 更新补丁 等）
_SYS_NAME_HINTS = ("visual c++", "redistributable", ".net ", "windows sdk",
                   "windows software development kit", "运行库", "runtime",
                   "update for", "hotfix", "driver")


def _app_kind(name, publisher, sys_comp, parent=""):
    """区分「系统自带 / 商店」与「自己安装」的软件。

    系统类判定（任一命中）：SystemComponent=1（驱动/运行库/系统组件）、有 ParentKeyName、
    发行商是 Microsoft / Windows、名称里带运行库 / SDK / 更新之类关键词。
    """
    pub = (publisher or "").strip().lower()
    low = (name or "").strip().lower()
    if sys_comp in (1, "1") or parent:
        return "system"
    if "microsoft" in pub or pub in ("windows", "microsoft windows"):
        return "system"
    for kw in _SYS_NAME_HINTS:
        if kw in low:
            return "system"
    return "user"


_SOFTWARE_CACHE = {"data": None}

# 更新补丁（Windows Updates 视图）的识别：KB 编号 / "Update for ..." / 安全更新
_KB_RE = re.compile(r"(KB\d{5,7}|\bUpdate for\b|Security Update|Hotfix|"
                    r"更新程序|更新包|安全更新)", re.I)


def list_software(force=False):
    """枚举已安装程序（带内存缓存：二次调用秒返回；force=True 强制重读）。

    字段比常见的更全：除名称/版本/发布者/大小/日期外，还包括
    `uninstall`/`quiet`（卸载命令）、`location`（安装目录）、`icon`（图标）、
    `modify`（修改/修复命令）、`url`（官网）、`bit`（32/64 位）、`kb`（是否系统更新补丁）。
    缓存让「切换分类 / 排序 / 重新搜索」不再反复读注册表。
    """
    if not force and _SOFTWARE_CACHE["data"] is not None:
        return _SOFTWARE_CACHE["data"]
    out = []
    if winreg is None:
        return out
    for hive, sub, flag in UNINSTALL_ROOTS:
        root = winreg.HKEY_LOCAL_MACHINE if hive == "HKLM" else winreg.HKEY_CURRENT_USER
        try:
            with winreg.OpenKey(root, sub, 0, winreg.KEY_READ | flag) as k:
                n = winreg.QueryInfoKey(k)[0]
                for i in range(n):
                    try:
                        name = winreg.EnumKey(k, i)
                        with winreg.OpenKey(k, name) as sk:
                            def q(v, default=""):
                                try:
                                    return winreg.QueryValueEx(sk, v)[0]
                                except OSError:
                                    return default
                            dn = q("DisplayName")
                            if not dn or dn.startswith("${{") or dn.startswith("${"):
                                continue      # NVIDIA 等未解析的 ARP 变量名，不是真名字
                            pub = q("Publisher") or ""
                            # 系统组件不再跳过：标记成 system，交给 UI 分类显示
                            out.append({
                                "key": hive + "\\" + sub + "\\" + name,
                                "name": dn,
                                "version": q("DisplayVersion"),
                                "publisher": pub,
                                "size": q("EstimatedSize", 0),
                                "date": q("InstallDate"),
                                "uninstall": q("UninstallString"),
                                "quiet": q("QuietUninstallString"),
                                "location": q("InstallLocation"),
                                "icon": q("DisplayIcon"),
                                "modify": q("ModifyPath") or q("ModifyString"),
                                "url": (q("URLInfoAbout") or q("URLUpdateInfo")
                                        or q("HelpLink") or ""),
                                # 32/64 位：Wow6432Node 视图 = 32 位；HKLM 主视图 = 64 位；
                                # HKCU 的 ARP 项来源不确定，留空不标
                                "bit": ("32" if "WOW6432Node" in sub
                                        else ("64" if hive == "HKLM" else "")),
                                "kb": bool(_KB_RE.search(dn)),
                                "kind": _app_kind(dn, pub, q("SystemComponent", 0),
                                                  q("ParentKeyName")),
                            })
                    except OSError:
                        continue
        except FileNotFoundError:
            continue
    seen = set()
    uniq = []
    for it in out:
        sig = (it["name"], it["version"], it["publisher"])
        if sig in seen:
            continue
        seen.add(sig)
        uniq.append(it)
    uniq.sort(key=lambda x: x["name"].lower())
    _SOFTWARE_CACHE["data"] = uniq          # 缓存：分类/排序/搜索不再重读注册表
    return uniq


# --------------------------------------------------------------------------
# 已安装的 Windows 更新补丁
#
# ⚠️ 为什么不能靠 ARP 名单：Win10 累积更新早已搬离「应用和功能」，
#    本机 350 条 ARP 里带 KB 号的**一条都没有**，靠名字正则永远筛不出东西
#    （表现为「更新补丁」分类永远空白，但权限其实是好的）。
#
# 权威来源是 CIM 的 Win32_QuickFixEngineering（PowerShell 侧是 Get-HotFix），
# 纯注册表拿不到：CBS\Packages 下RollupFix 包的 Source 值里也没有 KB 号。
# --------------------------------------------------------------------------
_HOTFIX_CACHE = {"t": 0.0, "data": None}
_HOTFIX_TTL = 300          # 补丁列表很少变，缓存 5 分钟
_HF_DESC = {"Security Update": "安全更新", "Update": "更新",
            "Hotfix": "修补程序", "Service Pack": "服务包"}


def list_hotfix(force=False):
    """已安装的更新补丁：[{kb, name, desc, date, by}]，date 为 YYYYMMDD。

    读不到（PowerShell 被策略拦 / CIM 服务停）时返回 []，不抛异常。
    """
    if not force and _HOTFIX_CACHE["data"] is not None and \
            time.time() - _HOTFIX_CACHE["t"] < _HOTFIX_TTL:
        return _HOTFIX_CACHE["data"]
    rows = []
    rc, out = ps("Get-HotFix | Select-Object HotFixID,Description,InstalledOn,InstalledBy "
                 "| Sort-Object InstalledOn -Descending | ConvertTo-Json -Compress",
                 timeout=90)
    if rc == 0 and out and out.strip():
        try:
            import json as _json
            data = _json.loads(out)
        except Exception:
            data = None
        if isinstance(data, dict):
            data = [data]                # 只有一条时 ConvertTo-Json 返回对象
        for d in (data or []):
            kb = str(d.get("HotFixID") or "").strip()
            if not kb:
                continue
            desc = str(d.get("Description") or "").strip()
            # ConvertTo-Json 的 DateTime 是 {"DateTime":"2026年10月2日 0:00:00"}
            dt = d.get("InstalledOn")
            if isinstance(dt, dict):
                dt = dt.get("DateTime") or ""
            rows.append({"kb": kb, "desc": desc,
                         "name": "%s %s" % (kb, _HF_DESC.get(desc, desc)),
                         "date": _cn_date_to_yyyymmdd(str(dt or "")),
                         "by": str(d.get("InstalledBy") or "")})
    _HOTFIX_CACHE["t"] = time.time()
    _HOTFIX_CACHE["data"] = rows
    return rows


def _cn_date_to_yyyymmdd(s):
    """「2026年10月2日 0:00:00」/「2026-10-02」/「2026/10/02」→ 20261002。"""
    s = (s or "").strip()
    m = re.search(r"(\d{4})\s*[年/-]\s*(\d{1,2})\s*[月/-]\s*(\d{1,2})", s)
    if m:
        try:
            return "%04d%02d%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except (ValueError, TypeError):
            return ""
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)      # 美式 M/D/YYYY
    if m:
        try:
            return "%04d%02d%02d" % (int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except (ValueError, TypeError):
            return ""
    return ""


# --------------------------------------------------------------------------
# 软件条目右键动作
#
# 这些动作会**真的操作系统**（打开资源管理器 / 浏览器 / 注册表、删注册表键），
# 因此统一写成可打桩的普通函数；设环境变量 WTB_NO_BROWSER=1 可让它们在测试中
# 只返回成功、不弹任何窗口。
# --------------------------------------------------------------------------
_HIVE_NAMES = {
    "HKLM": "HKEY_LOCAL_MACHINE",
    "HKCU": "HKEY_CURRENT_USER",
    "HKCR": "HKEY_CLASSES_ROOT",
    "HKU": "HKEY_USERS",
}


def _no_gui():
    return bool(os.environ.get("WTB_NO_BROWSER"))


def open_dir(path):
    """在资源管理器里打开目录（给的是文件则打开其所在目录）。返回 True/False。"""
    p = os.path.expandvars((path or "").strip().strip('"'))
    if not p:
        return False
    target = p if os.path.isdir(p) else os.path.dirname(p)
    if not (target and os.path.isdir(target)):
        return False
    try:
        if not _no_gui():
            os.startfile(target)                    # noqa: S606  Windows 专有
        return True
    except Exception:
        return False


def open_url(url):
    """用默认浏览器打开链接（也支持 ms-windows-store: 之类的自定义协议）。"""
    u = (url or "").strip()
    if not u:
        return False
    if not re.match(r"^[a-z][a-z0-9+.\-]*:", u, re.I):     # 无协议头 → 补 https
        u = "https://" + u
    try:
        if not _no_gui():
            webbrowser.open(u)
        return True
    except Exception:
        return False


def google_search(text):
    from urllib.parse import quote
    return open_url("https://www.google.com/search?q=" + quote(text or ""))


def open_store(name):
    """在 Microsoft Store 里搜索该程序。"""
    from urllib.parse import quote
    return open_url("ms-windows-store://search/?query=" + quote(name or ""))


def _user_reg_root_label():
    """regedit 的 LastKey 前缀：简中是「计算机」、繁体是「電腦」、其它是「Computer」。

    原先用 `(lang & 0x3ff) == 0x04` 判「简体中文」—— 0x0404(zh-TW) / 0x0C04(zh-HK)
    的低 10 位也是 0x04，繁体系统会被判成简体，写出「计算机」这个不存在的根节点名，
    regedit 定位就失效了。
    """
    try:
        lang = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        if lang in (0x0804, 0x1004):        # zh-CN / zh-SG
            return "计算机"
        if (lang & 0x3ff) == 0x04:          # zh-TW / zh-HK / zh-MO
            return "電腦"
    except Exception:
        pass
    return "Computer"


def open_regedit(key_path):
    """打开注册表编辑器并定位到指定键。

    regedit 官方支持的定位方式：把完整键路径写进
    `HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Applets\\Regedit\\LastKey`，
    再以 `regedit -m`（打开上次位置）启动。
    """
    if winreg is None or not key_path:
        return False
    hive_s, _, rest = key_path.partition("\\")
    root_name = _HIVE_NAMES.get(hive_s)
    if not rest or not root_name:
        return False
    if _no_gui():                       # 测试模式：不写注册表、不启动 regedit
        return True
    full = "%s\\%s\\%s" % (_user_reg_root_label(), root_name, rest)
    try:
        with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Applets\Regedit",
                0, winreg.KEY_WRITE) as k:
            winreg.SetValueEx(k, "LastKey", 0, winreg.REG_SZ, full)
    except OSError:
        pass
    try:
        subprocess.Popen(["regedit.exe", "-m"])
        return True
    except Exception:
        return False


def run_detached(cmd):
    """启动程序但不等待（用于「修改 / 修复安装」）。返回 (ok, msg)。"""
    exe, args = parse_uninstall_cmd(cmd or "")
    if not exe:
        return False, "该程序没有提供「修改 / 修复」命令。"
    if not os.path.exists(exe):
        return False, "找不到程序：%s" % exe
    try:
        if not _no_gui():
            subprocess.Popen('"%s" %s' % (exe, args))
        return True, "已启动：%s" % os.path.basename(exe)
    except Exception as e:
        return False, "启动失败：%s" % e


def _reg_delete_tree(hive_root, sub):
    """递归删除注册表键（含所有子键与值）—— 等价于 Win32 `RegDeleteTree`。"""
    try:
        import ctypes
        adv = ctypes.windll.advapi32
        return adv.RegDeleteTreeW(ctypes.c_void_p(int(hive_root)),
                                  ctypes.c_wchar_p(sub)) == 0
    except Exception:
        return False


def force_remove_entry(key_path):
    """强制移除程序的 Uninstall 条目。

    只删注册表条目、**不动程序文件** —— 用于卸载程序已损坏、无法正常卸载的情形。
    返回 {ok, msg}。带安全阀：仅允许删 Uninstall 列表下的键。
    """
    if winreg is None:
        return {"ok": False, "msg": "当前环境无法访问注册表。"}
    hive_s, _, rest = (key_path or "").partition("\\")
    if not rest:
        return {"ok": False, "msg": "无效的注册表路径。"}
    if "\\uninstall\\" not in (rest.lower() + "\\"):
        return {"ok": False, "msg": "出于安全考虑，只能移除 Uninstall 列表下的条目。"}
    root = {"HKLM": winreg.HKEY_LOCAL_MACHINE,
            "HKCU": winreg.HKEY_CURRENT_USER}.get(hive_s)
    if root is None:
        return {"ok": False, "msg": "不支持的注册表根：%s" % hive_s}
    if _reg_delete_tree(root, rest):
        _SOFTWARE_CACHE["data"] = None              # 列表已变，下次 refresh 重读
        return {"ok": True, "msg": "已移除条目：%s" % key_path}
    return {"ok": False, "msg": "删除失败（可能需要管理员权限，或该条目已被移除）。"}


STARTUP_LOCATIONS = [
    ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
    ("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"),
    ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
    ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce"),
    ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce"),
]


# --------------------------------------------------------------------------
# 软件卸载执行
#
# 旧实现把整条 UninstallString 丢给 `cmd /c start ""`：无引号且含空格的路径
# 会被空格截断（C:\Program Files\Foo\unins000.exe 直接打不开）；而且命令一发出
# 就立刻返回 —— 卸载还没跑完就弹残留扫描，自然什么都扫不到。
# 现在：解析命令行 → CreateProcess 直接启动（exe 统一加引号）→ 等卸载真正结束
# （顶层进程退出 + 该程序的 Uninstall 注册表键消失，双判据）。
# --------------------------------------------------------------------------
_MSI_GUID_RE = re.compile(r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
                          r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}")


def parse_uninstall_cmd(cmd):
    """解析 UninstallString → (exe, args_str)：展开环境变量、剥离外层引号，
    无引号时按 `.exe/.com/.bat/.cmd` 边界切分（路径本身可能含空格）。"""
    s = os.path.expandvars((cmd or "").strip())
    if not s:
        return "", ""
    if s.startswith('"'):
        end = s.find('"', 1)
        if end > 0:
            return s[1:end], s[end + 1:].strip()
        return s[1:].strip(), ""
    m = re.search(r"\.(?:exe|com|bat|cmd)(?=\s|$)", s, re.I)
    if m:
        return s[:m.end()], s[m.end():].strip()
    return s, ""


def _msi_guid(cmd):
    m = _MSI_GUID_RE.search(cmd or "")
    return m.group(0).upper() if m else ""


def _arp_key_exists(key_path):
    """该程序的 Uninstall 注册表键是否还在（64/32 位视图都试）。"""
    if not key_path or winreg is None:
        return True                       # 判断不了 → 当作还在，不误报"已完成"
    hive_s, _, rest = (key_path or "").partition("\\")
    root = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER,
            "HKCR": winreg.HKEY_CLASSES_ROOT, "HKU": winreg.HKEY_USERS}.get(hive_s)
    if root is None or not rest:
        return True
    for flag in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(root, rest, 0, winreg.KEY_READ | flag):
                return True
        except OSError:
            continue
    return False


def _uninst_procs_alive(exe="", install_dirs=(), extra_names=(), proc=None,
                        match_name=True):
    """卸载程序是否还在运行 —— 判定「用户关掉自带卸载程序了没有」。

    为什么不能只看 Popen 的那个进程：卸载器普遍是**先自我复制到 %TEMP% 再重启**
    （NSIS 的 Unins000.exe / Au_.exe 就是这个套路），于是父进程瞬间退出、
    真正干活的在别处跑。只看父进程会立刻误判"已结束"，在用户还没操作完时就
    去扫残留 —— 扫到的是一堆"还没被删"的正常文件。

    信号（任一命中即认为还在跑）：
      1) 传进来的 `proc` 句柄仍存活；
      2) 进程名与卸载程序 exe 同名（覆盖「复制到 %TEMP% 再启动」的副本）；
      3) 进程 exe 路径落在该程序的安装目录里（覆盖换名字的子进程）；
      4) 显式补充的候选文件名（extra_names）。

    `match_name=False` 用于 MSI：`msiexec.exe` 是全局公共宿主，按名字匹配会把
    别的安装/卸载也算进来，只能靠 proc 句柄 + 安装目录。
    """
    if proc is not None:
        try:
            if proc.poll() is None:
                return True
        except Exception:
            pass
    want_exe = os.path.basename(exe or "").lower() if match_name else ""
    dirs = [os.path.normcase(os.path.abspath(d)) for d in (install_dirs or ()) if d]
    names = {str(n).lower() for n in extra_names if n} if match_name else set()
    if not want_exe and not dirs and not names:
        return False                       # 没有可匹配的线索 → 不阻塞
    try:
        procs = procs_fast(limit=300)
    except Exception:
        return False                       # 拿不到进程表 → 不阻塞（宁可早扫）
    for pr in procs:
        nm = (pr.get("name") or "").lower()
        px = os.path.normcase(os.path.abspath(pr.get("exe") or "")) if pr.get("exe") else ""
        if want_exe and nm == want_exe:
            return True
        if nm in names:
            return True
        if px and dirs:
            for d in dirs:
                if px == d or px.startswith(d + os.sep):
                    return True
    return False


def uninstall_software(key_path, cmd, timeout=1800, install_dirs=(), on_wait=None,
                       quiet=False):
    """执行卸载并**等它真正结束**（后台线程调用，可能运行几十分钟）。

    - MSI（含 {GUID}）：直接用 `msiexec /x {GUID}`，`quiet=True` 用 `/qn`（全静默）、
      否则 `/qb`（带基础进度条，能看见在装）；
    - 普通 exe：解析出 exe 与参数，exe 加引号后交 CreateProcess（不走 cmd，避免
      空格/引号解析坑）；卸载程序文件不存在时直接报"疑似残留注册表项"。

    **`quiet` 由调用方决定用哪条命令**（勾选「卸载字符串」时走静默）：
    默认走 `UninstallString`（弹自带界面），`quiet=True` 时走 `QuietUninstallString`
    （7-Zip 的 `Uninstall.exe /S` 那种，不弹界面）—— 批量卸载时省事。

    **完成判据**：以「卸载程序进程全部关闭」为主 —— 用户要的就是
    「关掉自带卸载程序之后才去扫残留」。早先只看 Popen 那个进程 + 注册表键，
    两个都不准：
      · 卸载器自我复制到 %TEMP% 重启 → 父进程秒退，误判"已结束"；
      · 注册表键先被删、进程还在清文件 → 此时去扫会扫到"还没删完"的正常文件。
    关闭之后再看 Uninstall 注册表键，据此区分：
      completed（键已消失 = 真卸载完了）/ cancelled（键还在 = 用户取消了）/
      timeout（一直没关，可能弹了 UAC 或界面被最小化）。

    返回新增 `mode` 字段，供 UI 决定"是否自动进入残留扫描"。
    """
    cmd = (cmd or "").strip()
    if not cmd:
        return {"ok": False, "msg": "该程序没有卸载命令", "mode": "error"}
    guid = _msi_guid(cmd)
    exe = ""
    is_msi = bool(guid and re.search(r"msiexec", cmd, re.I))
    if is_msi:
        exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                           "System32", "msiexec.exe")
        line = '"%s" /x %s %s /norestart' % (exe, guid,
                                             "/qn" if quiet else "/qb")
    else:
        exe, args = parse_uninstall_cmd(cmd)
        if not exe:
            return {"ok": False, "msg": "无法解析卸载命令", "mode": "error"}
        if exe.lower().endswith((".bat", ".cmd")):
            line = '"%s" %s' % (os.environ.get("ComSpec", "cmd.exe"),
                                subprocess.list2cmdline([exe] + (args.split() if args else [])))
        else:
            probe = exe if os.path.isabs(exe) else (shutil.which(exe) or exe)
            if os.path.isabs(probe) and not os.path.exists(probe):
                return {"ok": False, "exe": exe, "left": True, "mode": "error",
                        "msg": "卸载程序文件不存在：%s\n该注册表项可能是卸载残留，"
                               "可直接用「扫描卸载残留」清理。" % exe}
            line = '"%s" %s' % (probe, args)
    try:
        proc = subprocess.Popen(line, shell=False)
    except Exception as e:
        return {"ok": False, "exe": exe, "mode": "error",
                "msg": "启动卸载程序失败：%s" % str(e)[:160]}
    t0 = time.time()
    deadline = t0 + max(60, int(timeout))
    # 卸载器自我复制到 %TEMP% 重启时，副本名字可能变（Au_.exe 之类），
    # 用「卸载程序同目录里的可执行文件」补一份候选名，提高识别率。
    extra = set()
    if not is_msi:
        try:
            ud = os.path.dirname(exe)
            if ud and os.path.isdir(ud):
                for nm in os.listdir(ud)[:80]:
                    if nm.lower().endswith((".exe", ".bat", ".cmd")):
                        extra.add(nm.lower())
        except OSError:
            pass
    dirs = list(install_dirs or ())
    left = True
    mode = "timeout"
    seen_gone_at = None
    started = False
    while time.time() < deadline:
        alive = _uninst_procs_alive(exe, dirs, extra, proc=proc,
                                    match_name=not is_msi)
        if alive:
            started = True
        gone = not _arp_key_exists(key_path)
        # 主判据：卸载程序进程已全部关闭 → 用户已经关掉自带卸载程序
        if started and not alive:
            # 关掉后再给 8 秒确认注册表（NSIS 会先删键、再慢慢清文件）
            for _ in range(8):
                if not _arp_key_exists(key_path):
                    break
                time.sleep(1.0)
            left = _arp_key_exists(key_path)
            mode = "completed" if not left else "cancelled"
            break
        # 次级：注册表键消失但进程还在（正在清理文件）→ 最多再等 120 秒
        if gone:
            if seen_gone_at is None:
                seen_gone_at = time.time()
            elif time.time() - seen_gone_at > 120:
                left = False
                mode = "completed"
                break
        if on_wait is not None:
            try:
                on_wait(int(time.time() - t0), alive)
            except Exception:
                pass
        time.sleep(1.2)
    else:
        # 一直没关掉：可能弹了 UAC / 界面被最小化 / 装了个常驻更新器
        left = _arp_key_exists(key_path)
        mode = "completed" if not left else "timeout"
    if mode == "completed":
        msg = ""
    elif mode == "cancelled":
        msg = ("卸载程序已关闭，但程序仍在「已安装」列表中 —— "
               "看起来是取消了卸载（没有真正卸载）。")
    else:
        msg = ("卸载程序还没关闭（已等待 %d 秒）。可能是弹了 UAC 授权框、"
               "卸载窗口被最小化，或该卸载程序会常驻后台。" % int(time.time() - t0))
    return {"ok": not left, "exe": exe, "left": left, "mode": mode,
            "secs": int(time.time() - t0), "msg": msg}


# ---- 残留扫描（常见检查位置）----
# 旧实现只扫 5 个根目录的**第一层**，漏掉最常见的第二层：
#   %LOCALAPPDATA%\Programs\<名>（VSCode / Electron 类全在这）、
#   C:\Program Files\<厂商>\<名>、%APPDATA%\<厂商>\<名>；
# 注册表同样只扫第一层，漏掉 HKCU\SOFTWARE\<厂商>\<产品>、Uninstall 残留键、启动项与服务。
_STOP_WORDS = {"inc", "ltd", "llc", "corp", "co", "gmbh", "limited", "company",
               "software", "technologies", "technology", "studio", "team", "the",
               "app", "desktop", "client", "setup", "update", "official", "tool",
               # 品类通用词：命中它们会把无关程序也扫进来（如 downloader 命中
               # Xbox Accessories、video 命中 MPC-BE），一律不作为匹配 token
               "video", "downloader", "download", "player", "manager", "editor",
               "viewer", "converter", "browser", "music", "photo", "code", "drive",
               "notes", "note", "reader", "writer", "mail", "chat", "suite", "office",
               "tools", "utility", "utilities", "system", "service", "driver",
               "runtime", "library", "home", "pro", "plus", "lite", "free", "portable",
               "media", "audio", "image", "graphics", "design", "master", "cleaner",
               "assistant", "center", "panel", "control", "windows", "microsoft",
               "premium", "edition", "ultimate", "professional", "standard", "basic"}
_SKIP_ENTRIES = {"microsoft", "windows", "windowsapps", "common files", "internet explorer",
                 "windows nt", "windows defender", "windows mail", "windows media player",
                 "windows portable devices", "windows security", "temp", "temporary",
                 "packages", "crashdumps", "d3dscache", "connecteddevicesplatform",
                 "nvidia", "nvidia corporation", "intel", "amd", "realtek", "google",
                 "mozilla", "apple", "adobe", "public", "default", "all users",
                 "system volume information", "$recycle.bin", "classes", "policies",
                 "clients", "wow6432node", "microsoft corporation"}


def _res_tokens(name, publisher, extra=()):
    """匹配 token：规范化全名（**先剥掉版本号/位数**，否则 "7-Zip 24.09 (x64)"
    会变成 "7zip2409x64" 而匹配不上 "7-Zip"）+ 去停用词后的独立词（≥5 字符）。"""
    full, words = set(), set()
    for src in [name or "", publisher or ""] + list(extra or ()):
        s = re.sub(r"[^\w\u4e00-\u9fff]+", " ", src or "").strip().lower()
        if not s:
            continue
        full.add(s.replace(" ", ""))
        # 只剥"多位数/点位版本号"与位数标记；单个数字保留（"7-Zip" 的 7 是名字一部分）
        s2 = re.sub(r"\b(?:v?\d{2,}(?:[.\d]+)?|x64|x86|amd64|i386|bit|beta|alpha|rc|final|edition)\b",
                    " ", s, flags=re.I)
        s2 = re.sub(r"\s+", " ", s2).strip()
        if s2:
            full.add(s2.replace(" ", ""))
            parts = s2.split()
            # 前两词拼接与首词：补齐 "7-Zip → 7zip"、"WinRAR 7 → winrar" 这类
            if len(parts) >= 2:
                full.add(parts[0] + parts[1])
            if len(parts[0]) >= 5 and parts[0] not in _STOP_WORDS:
                full.add(parts[0])
            for w in parts:
                if len(w) >= 5 and w not in _STOP_WORDS:
                    words.add(w)
    return full, (words - full)


def _lev_close(a, b, cap=2):
    """编辑距离是否 ≤ cap（快速带长度/首字符预筛）。同款思路容错
    目录名与程序名的拼写差异；首字符必须一致 —— 否则 "video" 会误配 "4kvideo"。"""
    if a == b:
        return True
    if abs(len(a) - len(b)) > cap or min(len(a), len(b)) < 5:
        return False
    if not a or not b or a[0] != b[0]:
        return False
    la, lb = len(a), len(b)
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        best = cur[0]
        ca = a[i - 1]
        for j in range(1, lb + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1,
                         prev[j - 1] + (0 if ca == b[j - 1] else 1))
            if cur[j] < best:
                best = cur[j]
        if best > cap:
            return False
        prev = cur
    return prev[lb] <= cap


def _res_score(entry, full, words, fuzzy=True):
    """命中强度：2 = 全名精确/近似匹配（可靠）；1 = 仅独立词匹配（可能与同名
    品类混淆，低置信 → UI 默认不勾选）；0 = 未命中。"""
    el = re.sub(r"[^\w\u4e00-\u9fff]+", " ", (entry or "")).strip().lower()
    if not el:
        return 0
    eln = el.replace(" ", "")
    if eln in full:
        return 2
    if fuzzy:
        for f in full:
            if len(f) >= 6 and _lev_close(eln, f, 2):
                return 2
    if set(el.split()) & words:
        return 1
    return 0


def _res_hit(entry, full, words, fuzzy=True):
    """是否命中（含低置信）。"""
    return _res_score(entry, full, words, fuzzy) > 0


def _walk2(base, depth=2):
    """产出 base 下最多 depth 层的条目 (path, level)，跳过系统目录。"""
    stack = [(base, 0)]
    while stack:
        cur, lv = stack.pop()
        try:
            names = os.listdir(cur)
        except OSError:
            continue
        for nm in names:
            if nm.lower() in _SKIP_ENTRIES:
                continue
            fullp = os.path.join(cur, nm)
            yield fullp, lv + 1
            if lv + 1 < depth and os.path.isdir(fullp) and not os.path.islink(fullp):
                stack.append((fullp, lv + 1))


def _scan_reg_products(full, words, cap):
    """HKLM / HKCU 的 SOFTWARE（含 WOW6432Node）两层内的厂商/产品键。"""
    out = []
    targets = [(winreg.HKEY_LOCAL_MACHINE, "HKLM", r"SOFTWARE"),
               (winreg.HKEY_LOCAL_MACHINE, "HKLM", r"SOFTWARE\WOW6432Node"),
               (winreg.HKEY_CURRENT_USER, "HKCU", r"SOFTWARE")]
    for root, hive, sub in targets:
        try:
            k = winreg.OpenKey(root, sub, 0, winreg.KEY_READ)
        except OSError:
            continue
        try:
            for i in range(winreg.QueryInfoKey(k)[0]):
                try:
                    lv1 = winreg.EnumKey(k, i)
                except OSError:
                    continue
                if lv1.lower() in _SKIP_ENTRIES:
                    continue
                p1 = sub + "\\" + lv1
                if _res_hit(lv1, full, words):
                    out.append({"hive": hive, "path": p1, "tag": "厂商键"})
                    if len(out) >= cap:
                        return out
                    continue                  # 命中一层就不再下探
                try:
                    with winreg.OpenKey(k, lv1) as sk:
                        for j in range(winreg.QueryInfoKey(sk)[0]):
                            try:
                                lv2 = winreg.EnumKey(sk, j)
                            except OSError:
                                continue
                            if _res_hit(lv2, full, words):
                                out.append({"hive": hive, "path": p1 + "\\" + lv2,
                                            "tag": "产品键"})
                                if len(out) >= cap:
                                    return out
                except OSError:
                    continue
        finally:
            winreg.CloseKey(k)
    return out


def _scan_reg_arp(full, words, cap):
    """四处「已安装程序」列表里的残留项（按 DisplayName 匹配）。"""
    out = []
    for hive, sub, flag in UNINSTALL_ROOTS:
        root = winreg.HKEY_LOCAL_MACHINE if hive == "HKLM" else winreg.HKEY_CURRENT_USER
        try:
            with winreg.OpenKey(root, sub, 0, winreg.KEY_READ | flag) as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    try:
                        key = winreg.EnumKey(k, i)
                        with winreg.OpenKey(k, key) as sk:
                            try:
                                dn = str(winreg.QueryValueEx(sk, "DisplayName")[0])
                            except OSError:
                                dn = ""
                    except OSError:
                        continue
                    if dn and _res_hit(dn, full, words):
                        out.append({"hive": hive, "path": sub + "\\" + key, "tag": "卸载项"})
                        if len(out) >= cap:
                            return out
        except OSError:
            continue
    return out


def _scan_reg_startup(full, words, cap):
    """Run / RunOnce 启动项里的残留（值名或可执行名匹配）。

    结构里 path=键路径、value=值名（**不拼箭头** —— UI 曾用「sub  →  值名」拼成
    字符串，回传时既解析不出 hive 也定位不到值，导致勾选后删不掉）。
    """
    out = []
    for hive, sub in STARTUP_LOCATIONS:
        root = winreg.HKEY_LOCAL_MACHINE if hive == "HKLM" else winreg.HKEY_CURRENT_USER
        try:
            with winreg.OpenKey(root, sub, 0, winreg.KEY_READ) as k:
                for i in range(winreg.QueryInfoKey(k)[1]):
                    try:
                        vn, vd, _t = winreg.EnumValue(k, i)
                    except OSError:
                        continue
                    exe_name = os.path.basename(str(vd).strip('"').split()[0].strip('"')) \
                        if str(vd).strip() else ""
                    if _res_hit(vn, full, words) or (exe_name and
                                                     _res_hit(os.path.splitext(exe_name)[0],
                                                              full, words)):
                        out.append({"hive": hive, "path": sub, "value": vn,
                                    "tag": "启动项"})
                        if len(out) >= cap:
                            return out
        except OSError:
            continue
    return out


def _scan_reg_services(full, words, cap):
    """服务键残留（键名匹配）。勾选后可按 _REG_DEL_RULES 精确删除。"""
    out = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Services", 0, winreg.KEY_READ) as k:
            for i in range(winreg.QueryInfoKey(k)[0]):
                try:
                    sn = winreg.EnumKey(k, i)
                except OSError:
                    continue
                if _res_hit(sn, full, words):
                    out.append({"hive": "HKLM",
                                "path": r"SYSTEM\CurrentControlSet\Services" + "\\" + sn,
                                "tag": "服务"})
                    if len(out) >= cap:
                        break
    except OSError:
        pass
    return out


class _CREDENTIAL(ctypes.Structure):
    """Windows 凭据管理器条目（CredEnumerateW 返回结构）。"""
    _fields_ = [
        ("Flags", wt.DWORD),
        ("Type", wt.DWORD),
        ("TargetName", wt.LPWSTR),
        ("Comment", wt.LPWSTR),
        ("LastWritten", wt.FILETIME),
        ("CredentialBlobSize", wt.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", wt.DWORD),
        ("AttributeCount", wt.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wt.LPWSTR),
        ("UserName", wt.LPWSTR),
    ]


def _scan_reg_eventlog(full, words, cap):
    """事件日志源残留：安装程序常往 Application 日志注册源，卸载后常留下。"""
    out = []
    base = r"SYSTEM\CurrentControlSet\Services\EventLog\Application"
    if winreg is None:
        return out
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base, 0, winreg.KEY_READ) as k:
            for i in range(winreg.QueryInfoKey(k)[0]):
                try:
                    nm = winreg.EnumKey(k, i)
                except OSError:
                    continue
                if _res_hit(nm, full, words):
                    out.append({"hive": "HKLM", "path": base + "\\" + nm,
                                "tag": "日志源"})
                    if len(out) >= cap:
                        break
    except OSError:
        pass
    return out


def _scan_credentials(full, words, cap):
    """Windows 凭据管理器里的**登录凭据**（登录数据的典型残留位置）。

    列出所有 TargetName（如 `git:https://github.com`、
    `LegacyGeneric:target=XXX`），按程序 token 匹配；删除走 CredDeleteW。
    """
    out = []
    try:
        adv = ctypes.windll.advapi32
        cnt = wt.DWORD(0)
        arr = ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))()
        if not adv.CredEnumerateW(None, 0, ctypes.byref(cnt), ctypes.byref(arr)):
            return out
        try:
            for i in range(cnt.value):
                try:
                    c = arr[i].contents
                except (ValueError, OSError):
                    continue
                target = c.TargetName or ""
                short = re.sub(r"^LegacyGeneric:target=", "", target, flags=re.I)
                if _res_hit(short, full, words) or _res_hit(target, full, words):
                    out.append({"hive": "CRED", "path": target, "kind": "cred",
                                "ctype": int(c.Type), "value": c.UserName or "",
                                "tag": "登录凭据"})
                    if len(out) >= cap:
                        break
        finally:
            adv.CredFree(arr)
    except Exception:
        pass
    return out


def _task_list_raw():
    """全量计划任务的 `(路径, 名称)` 列表。

    ⚠️ **不要用 `schtasks /query`** —— 它和 `reg.exe` / `sc.exe` 一样在
    「程序黑名单」里，会被安全策略直接拦掉（实测本机`run(["schtasks",...])`
    返回 rc=2、一个字节输出都没有）。**返回空列表还看不出是「没有任务」还是
    「被拦了」**，那是最坏的一种失败方式。

    改走 PowerShell 的 `Get-ScheduledTask`（实测可读到209 条）。
    """
    rc, out = ps("Get-ScheduledTask | ForEach-Object { "
                 "'{0}|||{1}' -f $_.TaskPath, $_.TaskName }", timeout=120)
    rows = []
    for line in (out or "").splitlines():
        p = line.strip().split("|||")
        if len(p) >= 2 and p[1]:
            rows.append((p[0] or "\\", p[1].strip()))
    return rows


def _task_split(full_name):
    """`\\Microsoft\\Windows\\Defrag\\ScheduledDefrag` → (TaskPath, TaskName)。

    末段是任务名，前面拼回路径（含结尾反斜杠）。传进来的也可能不带前导反斜杠。
    """
    s = str(full_name or "").strip().replace("/", "\\")
    if "\\" not in s:
        return "\\", s
    path, _sep, name = s.rpartition("\\")
    return (path + "\\") if path else "\\", name


def _scan_tasks(full, words, cap):
    """计划任务残留：卸载程序常在计划任务里留下自动更新 / 守护任务。
    `\\Microsoft\\...` 开头的系统任务一律跳过。"""
    out = []
    for path, name in _task_list_raw():
        # TaskPath + TaskName 拼回 schtasks 那种全名（`name` 本身可能不含路径）
        full_name = path + name
        if not name or name.lower().startswith("microsoft\\") \
                or full_name.lower().startswith("\\microsoft\\"):
            continue
        if _res_hit(name, full, words):
            out.append({"hive": "TASK", "path": full_name, "kind": "task",
                        "tag": "计划任务"})
            if len(out) >= cap:
                break
    return out


def _cred_exists(target, ctype=1):
    """该凭据是否仍在（快照复查用）。"""
    if not target:
        return False
    try:
        adv = ctypes.windll.advapi32
        p = ctypes.POINTER(_CREDENTIAL)()
        if adv.CredReadW(ctypes.c_wchar_p(target), int(ctype or 1), 0,
                         ctypes.byref(p)):
            adv.CredFree(p)
            return True
    except Exception:
        pass
    return False


def _task_exists(name):
    """该计划任务是否仍在。

    ⚠️ 这里原来是 `run(["schtasks", "/query", "/tn", name])` + `rc == 0`
    —— **`schtasks.exe` 被安全策略拦掉后 rc 恒为 2，于是这个函数恒返回
    False**。假阴性比抛异常危险得多：快照复查会把「**还在**的计划任务」
    判成「已经删干净」，于是残留扫描报告「已处理干净」，用户以为清完了。

    改成 `Get-ScheduledTask -TaskName/-TaskPath`，顺带把 `rc == 0` 这种
    弱判定换成「哨兵互斥 + 逐行精确匹配」（同 `_rp_run` 的教训：
    PowerShell 出错时退出码仍然是 0）。
    """
    path, tname = _task_split(name)
    if not tname:
        return False
    st, _why = _rp_run(
        "$ErrorActionPreference='Stop';"
        "try {"
        "  $t = @(Get-ScheduledTask -TaskName '%s' -TaskPath '%s'"
        "         -ErrorAction SilentlyContinue);"
        "  if (-not $t) { '%s' } else { '%s' }"
        "} catch { '%s' + $_.Exception.Message }"
        % (tname.replace("'", "''"), path.replace("'", "''"),
           _SRP_NOTFOUND, _SRP_OK, _SRP_ERR))
    return st == "ok"


def _item_exists(it):
    """快照项是否仍然存在（按 kind 分派到各自的检查方式）。"""
    kind = (it or {}).get("kind") or "reg"
    if kind == "cred":
        return _cred_exists(it.get("path"), it.get("ctype") or 1)
    if kind == "task":
        return _task_exists(it.get("path"))
    return _reg_key_exists(it.get("hive"), it.get("path"), it.get("value") or "")


def install_dir_candidates(key_path="", location="", uninstall_cmd=""):
    """多来源推断安装目录：
    InstallLocation → 注册表 Inno Setup: App Path / App Path（Inno / InstallShield
    常写这两个值）→ 卸载程序所在目录。只返回当前仍存在的目录。"""
    dirs = []
    if location:
        dirs.append(location)
    if key_path and winreg is not None:
        hive_s, _, rest = key_path.partition("\\")
        root = {"HKLM": winreg.HKEY_LOCAL_MACHINE,
                "HKCU": winreg.HKEY_CURRENT_USER}.get(hive_s)
        if root and rest:
            for flag in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(root, rest, 0, winreg.KEY_READ | flag) as k:
                        for vn in ("Inno Setup: App Path", "InstallLocation", "App Path"):
                            try:
                                dirs.append(str(winreg.QueryValueEx(k, vn)[0]))
                            except OSError:
                                pass
                    break
                except OSError:
                    continue
    try:
        exe, _a = parse_uninstall_cmd(uninstall_cmd or "")
        sysroot = os.environ.get("SystemRoot", r"C:\Windows").lower()
        if exe and os.path.isabs(exe) and not exe.lower().startswith(sysroot):
            dirs.append(os.path.dirname(exe))
    except Exception:
        pass
    out, seen = [], set()
    for d in dirs:
        d = os.path.expandvars((d or "").strip().strip('"')).rstrip("\\/")
        if d and d.lower() not in seen and os.path.isdir(d):
            seen.add(d.lower())
            out.append(d)
    return out


def _scan_msi_folders(full, words, cap):
    """MSI 安装目录索引：值名即目录路径，
    命中程序名且目录仍在 → MSI 卸载残留。返回目录路径列表（并入文件项）。"""
    out = []
    sub = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Installer\Folders"
    for flag in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub, 0,
                                winreg.KEY_READ | flag) as k:
                for i in range(winreg.QueryInfoKey(k)[1]):
                    try:
                        vn, _vd, _t = winreg.EnumValue(k, i)
                    except OSError:
                        continue
                    d = (vn or "").rstrip("\\")
                    base = os.path.basename(d)
                    if base and _res_hit(base, full, words) and os.path.isdir(d):
                        out.append(d)
                        if len(out) >= cap:
                            return out
        except OSError:
            continue
    return out


def _reg_key_exists(hive_s, sub, value=""):
    """注册表项是否还存在（64/32 视图都试）。

    value 非空时判断的是「该值名是否存在于 sub 键下」（启动项是「键 → 值名」，
    键永远在、只有值会被清掉）。
    """
    if winreg is None or not sub:
        return False
    root = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}.get(hive_s)
    if root is None:
        return False
    for flag in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(root, sub, 0, winreg.KEY_READ | flag) as k:
                if not value:
                    return True
                try:
                    winreg.QueryValueEx(k, value)
                    return True
                except OSError:
                    continue
        except OSError:
            continue
    return False


# 注册表残留的可删范围：按 tag 精确校验，绝不按「路径层数」一刀切 ——
# 旧实现只放行 SOFTWARE\<一层>，导致「产品键」(两层)、「卸载项」(4 层)、
# 「启动项」(带值名)、「服务」全被拒绝：扫得到、删不掉，用户点删除只会看到
# 「拒绝（超出安全范围）」。现在逐类给出各自的精确模式。
_REG_DEL_RULES = (
    # (tag, 路径正则, 是否删值)
    ("厂商键", r"^SOFTWARE(?:\\WOW6432Node)?\\[^\\]+$", False),
    ("产品键", r"^SOFTWARE(?:\\WOW6432Node)?\\[^\\]+\\[^\\]+$", False),
    ("卸载项", r"^SOFTWARE\\(?:WOW6432Node\\)?Microsoft\\Windows\\"
              r"CurrentVersion\\Uninstall\\[^\\]+$", False),
    ("启动项", r"^SOFTWARE\\(?:WOW6432Node\\)?Microsoft\\Windows\\"
              r"CurrentVersion\\Run(?:Once)?$", True),
    ("服务", r"^SYSTEM\\CurrentControlSet\\Services\\[^\\]+$", False),
    ("日志源", r"^SYSTEM\\CurrentControlSet\\Services\\EventLog\\Application\\[^\\]+$",
     False),
)


def _reg_del_ok(entry):
    """该项是否在可删除范围内（返回 (ok, 是否删值)）。"""
    if entry.get("hive") not in ("HKLM", "HKCU"):
        return False, False
    path = (entry.get("path") or "").strip()
    tag = entry.get("tag") or ""
    # 卸载前已有：按通用规则试（这类项本来是扫描产物，形态与上面一致）
    rules = [r for r in _REG_DEL_RULES if r[0] == tag] or list(_REG_DEL_RULES)
    for _t, pat, is_value in rules:
        if re.match(pat, path, re.I):
            if is_value and not (entry.get("value") or "").strip():
                continue          # 启动项没有值名 → 不知道删什么，拒绝
            return True, is_value
    return False, False


_SOFT_SNAP = {}


def snapshot_before(key_path, name, publisher="", location="", uninstall_cmd=""):
    """卸载前基线（首次快照的思路）：记录当前与程序相关的
    目录与注册表项。卸载后复查其中**仍存在**的项 —— 语义上就是确定的残留。

    **两层记录**（本次补上第二层）：
      ① 名字匹配（`residual_scan`）—— 覆盖面广，但路径必须"名字像"才进候选；
      ② **安装目录的精确快照** —— 该程序自己的安装目录里，卸载后**还剩什么**。
    第二层是「该删而没删」的硬证据，比 ① 的名字猜测确定得多。

    为什么只对**安装目录**做快照、不做全量：实测 Program Files 全量拍一次
    1.8 秒（11 万文件）还行，但加上整个 %LOCALAPPDATA% / %APPDATA% 要
    **20.3 秒**（47 万文件）—— 用户要干等 20 秒才能开始卸载，不可接受。
    而且 Program Files 里本来就有别人的文件，"仍在"根本不能当残留；
    只有**该程序自己的安装目录**里"仍在"才等于"该删没删"。
    """
    r = residual_scan(name, publisher, location, uninstall_cmd, key_path, cap=80)
    # 安装目录快照（精确层）。卸载后重扫对比 → 「该删没删」的确定残留。
    dirs = []
    try:
        dirs = install_dir_candidates(key_path, location, uninstall_cmd)
    except Exception:
        dirs = []
    fs = {}
    if dirs:
        try:
            fs, _d = _im_snapshot(dirs)
        except Exception:
            fs = {}
    _SOFT_SNAP[key_path or name] = {
        "files": [f["path"] for f in r.get("files") or []],
        "reg": r.get("reg") or [],
        "t": time.time(),
        "fs_dirs": dirs,
        "fs": fs,
    }
    return {"files": len(r.get("files") or []), "reg": len(r.get("reg") or []),
            "install_dirs": len(dirs), "install_files": len(fs)}


def snapshot_get(key_path, name=""):
    return _SOFT_SNAP.get(key_path or name)


def residual_scan(name, publisher="", location="", uninstall_cmd="",
                  key_path="", base=None, cap=80):
    """卸载后的残留扫描：只定位、不删除。返回 {"files", "reg", "tokens"}。

    覆盖范围（常见检查位置）：
      目录：安装目录多来源（InstallLocation / Inno Setup: App Path / 卸载程序目录）、
            ProgramFiles(+x86) / ProgramData / LOCALAPPDATA / LOCALAPPDATA\\Programs /
            APPDATA / LocalLow（两层）、各盘根目录、桌面与开始菜单快捷方式、
            **启动文件夹**、Recent 使用痕迹（一层）、**用户主目录点目录**（.vscode 等）、
            WER 崩溃报告（两层）；
      注册表：SOFTWARE（含 WOW6432Node）两层、四处已安装列表、MSI Installer\\Folders
              索引、Run/RunOnce 启动项、服务键、**事件日志源**；
      登录数据 / 守护：**Windows 凭据管理器**（CredEnumerateW，按程序名匹配 target）、
              **计划任务**（Get-ScheduledTask，系统任务除外）；
      base： 卸载前快照 —— 其中**仍存在**的项标记为「卸载前已有」（确定残留）。
    """
    dir_dirs = install_dir_candidates(key_path, location, uninstall_cmd)
    extra = [os.path.basename(d.rstrip("\\/")) for d in dir_dirs if d]
    full, words = _res_tokens(name, publisher, extra)

    files, seen = [], set()

    def add(path, tag, weak=False):
        key = os.path.abspath(path).lower()
        if key in seen:
            return
        seen.add(key)
        try:
            sz = dir_size(path, cap=8000)[0] if os.path.isdir(path) else os.path.getsize(path)
        except OSError:
            sz = 0
        files.append({"path": path, "kind": "dir" if os.path.isdir(path) else "file",
                      "bytes": sz, "tag": tag, "weak": weak})

    # 0) 卸载前快照 —— 两层，语义上最确定的残留放最前
    if base:
        # ① 名字匹配到的候选里，仍存在的
        for p in base.get("files") or []:
            if os.path.exists(p):
                add(p, "卸载前已有")
        # ② 安装目录精确 diff：卸载**确实删掉了一部分**、但还剩这些 → 硬证据
        _old_fs = base.get("fs") or {}
        _fs_dirs = base.get("fs_dirs") or []
        if _old_fs and _fs_dirs:
            try:
                _new_fs, _nd = _im_snapshot(_fs_dirs)
            except Exception:
                _new_fs = {}
            _gone_n = sum(1 for k in _old_fs if k not in _new_fs)
            _kept = [(k, v[1]) for k, v in _new_fs.items() if k in _old_fs]
            # 只在「删了一部分、又剩一部分」时才逐条列 ——
            #   · 一个都没删（_gone_n==0）→ 卸载没生效，目录级提示就够（见下）；
            #   · 全删干净（_kept 空）→ 没有残留，别报噪音。
            if _gone_n and _kept:
                def _sz(_p):
                    try:
                        return os.path.getsize(_p)
                    except OSError:
                        return 0
                _kept.sort(key=lambda kv: -_sz(kv[1]))
                for _k, _p in _kept[:40]:
                    add(_p, "该删没删")
    
    # 1) 安装目录 / 卸载程序目录仍在 → 最典型的残留
    for d in dir_dirs:
        add(d, "安装目录")

    # 2) 常见位置扫描
    roots = []
    for kk in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData",
               "LOCALAPPDATA", "APPDATA"):
        p = os.environ.get(kk)
        if p and os.path.isdir(p):
            roots.append((p, "程序目录" if kk.startswith("ProgramFiles") else "用户数据"))
    local = os.environ.get("LOCALAPPDATA") or ""
    if local and os.path.isdir(os.path.join(local, "Programs")):
        roots.append((os.path.join(local, "Programs"), "用户程序目录"))
    for drive in ("C", "D", "E", "F"):
        r = drive + ":\\"
        if os.path.isdir(r):
            roots.append((r, "磁盘根目录"))

    # 快捷方式 / 使用痕迹 / 崩溃报告
    up = os.environ.get("USERPROFILE") or ""
    dat = os.environ.get("ProgramData") or ""
    shallow = [
        (os.path.join(up, "Desktop"), "桌面快捷方式", 1),
        (os.path.join(os.environ.get("PUBLIC") or r"C:\Users\Public", "Desktop"),
         "桌面快捷方式", 1),
        (os.path.join(up, r"AppData\Roaming\Microsoft\Windows\Start Menu\Programs"),
         "开始菜单", 1),
        # 启动文件夹（开机自启的快捷方式，卸载后常残留）
        (os.path.join(up, r"AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup"),
         "启动文件夹", 1),
        (os.path.join(dat, r"Microsoft\Windows\Start Menu\Programs\Startup"),
         "启动文件夹", 1),
        (os.path.join(up, r"AppData\Roaming\Microsoft\Windows\Recent"), "使用痕迹", 1),
        (os.path.join(up, r"AppData\LocalLow"), "用户数据", 2),
        (os.path.join(dat, r"Microsoft\Windows\WER"), "崩溃报告", 2),
        (os.path.join(local, r"Microsoft\Windows\WER"), "崩溃报告", 2),
    ]

    for bdir, tag in roots:
        depth = 1 if tag == "磁盘根目录" else 2
        for path, _lv in _walk2(bdir, depth):
            if len(files) >= cap:
                break
            sc = _res_score(os.path.basename(path), full, words)
            if sc:
                add(path, tag, weak=(sc == 1))
        if len(files) >= cap:
            break
    for bdir, tag, depth in shallow:
        if len(files) >= cap or not bdir or not os.path.isdir(bdir):
            continue
        for path, _lv in _walk2(bdir, depth):
            nm = os.path.splitext(os.path.basename(path))[0]   # 快捷方式去 .lnk
            sc = _res_score(nm, full, words)
            if sc:
                add(path, tag, weak=(sc == 1))

    # 2.5) 用户主目录下的点目录（.vscode / .android / .gradle / .config …）——
    # 工具类程序把配置与登录态放这里，卸载后常整目录留下。
    if up and os.path.isdir(up):
        try:
            for nm in os.listdir(up):
                if len(files) >= cap:
                    break
                if not nm.startswith(".") or len(nm) < 3:
                    continue
                sc = _res_score(nm, full, words)
                if sc:
                    add(os.path.join(up, nm), "用户配置", weak=(sc == 1))
        except OSError:
            pass

    # 3) 注册表 + MSI 目录索引 + 登录凭据 + 计划任务
    regs = []
    if winreg is not None and (full or words):
        regs += _scan_reg_products(full, words, cap)
        regs += _scan_reg_arp(full, words, cap)
        for p in _scan_msi_folders(full, words, cap):     # 目录 → 并入文件项
            add(p, "MSI 目录")
        regs += _scan_reg_startup(full, words, cap)
        regs += _scan_reg_services(full, words, cap)
        regs += _scan_reg_eventlog(full, words, cap)
        regs += _scan_credentials(full, words, cap)       # 登录数据
        regs += _scan_tasks(full, words, cap)
        if base:
            for rg in base.get("reg") or []:
                if _item_exists(rg):
                    item = {k: rg.get(k) for k in
                            ("hive", "path", "value", "kind", "ctype")
                            if rg.get(k) is not None}
                    item["tag"] = "卸载前已有"
                    regs.append(item)
    uniq, seen_reg = [], set()
    for rg in [r for r in regs if r.get("tag") == "卸载前已有"] + regs:
        k = (rg.get("hive"), rg.get("path"))
        if k in seen_reg:
            continue
        seen_reg.add(k)
        uniq.append(rg)
    return {"files": files, "reg": uniq[:cap], "tokens": sorted(full | words)}


def residual_clean(files, regs):
    """删除所选残留。文件**优先移入回收站**（可还原），注册表整键删除前先
    导出 .reg 备份（删除一律先留退路）。

    返回 {"ok": bool, "msg": [...], "backups": [...], "backup_dir": str}。
    """
    msgs = []
    ok = True
    allowed_roots = [os.path.abspath(os.environ.get(k, "")).lower() for k in
                     ("ProgramFiles", "ProgramFiles(x86)", "ProgramData",
                      "LOCALAPPDATA", "APPDATA")]
    # 新增扫描位置对应的可删范围：LocalLow、用户/公共桌面、开始菜单快捷方式
    up = os.environ.get("USERPROFILE") or ""
    for extra in (os.path.join(up, "AppData", "LocalLow"),
                  os.path.join(up, "Desktop"),
                  os.path.join(os.environ.get("PUBLIC") or r"C:\Users\Public", "Desktop")):
        if extra and os.path.isdir(extra):
            allowed_roots.append(os.path.abspath(extra).lower())
    allowed_roots = [r for r in allowed_roots if r]
    # 磁盘根：扫描覆盖了 C/D/E/F 根下的直接子项（如 D:\SomeApp），删除范围
    # 必须与之对齐 —— 否则又是「扫得到、删不掉」。只放行**直接子项**，
    # 盘根本身永远不可删。
    # 注意统一小写：pl 已 lowercase，盘符若留大写会永远匹配不上
    drive_roots = [(d + ":\\").lower() for d in ("C", "D", "E", "F", "G")
                   if os.path.isdir(d + ":\\")]
    # 危险动作的最后一跳不依赖上游过滤：先过 _is_protected_dir（任意层级的
    # 系统 / 容器目录硬闸），再看是否落在允许的根目录内，最后**优先送回收站**
    # （可还原）—— 删除一律先留退路。
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backups = []
    for it in files or []:
        p = (it.get("path") or "").strip()
        if _is_protected_dir(p):
            msgs.append("拒绝（系统/容器目录，不允许删除）：%s" % p)
            ok = False
            continue
        pl = os.path.abspath(p).lower()
        okp = any(pl.startswith(r + os.sep) for r in allowed_roots)
        if not okp:
            par = os.path.dirname(pl)
            okp = (pl.rstrip("\\") != par.rstrip("\\") and
                   any(par.rstrip("\\") == r.rstrip("\\") for r in drive_roots))
        if not okp:
            msgs.append("拒绝（不在允许的目录内）：%s" % p)
            ok = False
            continue
        if not os.path.exists(p):
            msgs.append("跳过（已不存在）：%s" % p)
            continue
        recycled, note = _recycle_one(p)
        if recycled == "recycled":
            msgs.append("已移入回收站（可还原）：%s" % p)
            continue
        if recycled == "gone":               # 已不在原处，别再删一次
            msgs.append("已删除（%s）：%s" % (note, p))
            continue
        try:
            if os.path.isdir(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
            msgs.append("已删除%s：%s"
                        % ("（%s）" % note if note else "", p))
        except Exception as e:
            ok = False
            msgs.append("失败：%s（%s）" % (p, str(e)[:80]))
    for it in regs or []:
        kind = it.get("kind") or "reg"
        if kind == "cred":
            target = (it.get("path") or "").strip()
            if it.get("hive") != "CRED" or not target:
                msgs.append("拒绝（凭据项无效）：%s" % target)
                ok = False
                continue
            try:
                rc = ctypes.windll.advapi32.CredDeleteW(
                    ctypes.c_wchar_p(target), int(it.get("ctype") or 1), 0)
                if rc:
                    msgs.append("已删除登录凭据：%s" % target)
                else:
                    ok = False
                    msgs.append("删除凭据失败（可能被系统保护）：%s" % target)
            except Exception as e:
                ok = False
                msgs.append("失败：%s（%s）" % (target, str(e)[:60]))
            continue
        if kind == "task":
            name = (it.get("path") or "").strip()
            if it.get("hive") != "TASK" or not name or \
                    name.lower().startswith("microsoft\\"):
                msgs.append("拒绝（系统任务不可删）：%s" % name)
                ok = False
                continue
            # ⚠️ 原来走 `schtasks /delete`—— 同样被安全策略拦（rc 恒 2），
            # 会报「删除任务失败」但其实一次都没试。改走
            # `Unregister-ScheduledTask`，**删完再回读确认它真的没了**。
            _tp, _tn = _task_split(name)
            st, why = _rp_run(
                "$ErrorActionPreference='Stop';"
                "try {"
                "  Unregister-ScheduledTask -TaskName '%s' -TaskPath '%s' "
                "    -Confirm:$false -ErrorAction Stop; '%s'"
                "} catch { '%s' + $_.Exception.Message }"
                % (_tn.replace("'", "''"), _tp.replace("'", "''"),
                   _SRP_OK, _SRP_ERR))
            if st == "ok" and not _task_exists(name):
                msgs.append("已删除计划任务：%s" % name)
            else:
                ok = False
                msgs.append("删除任务失败：%s（%s）"
                            % (name, why or "回读发现任务仍在"))
            continue
        hive = it.get("hive")
        path = (it.get("path") or "").strip()
        value = (it.get("value") or "").strip()
        allowed, is_value = _reg_del_ok(it)
        if not allowed:
            msgs.append("拒绝（超出安全范围）：%s\\%s" % (hive, path))
            ok = False
            continue
        root = winreg.HKEY_LOCAL_MACHINE if hive == "HKLM" else winreg.HKEY_CURRENT_USER
        try:
            if is_value:
                # 启动项：键是系统的，只能删自己那个值
                with winreg.OpenKey(root, path, 0,
                                    winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
                    winreg.DeleteValue(k, value)
                msgs.append("已删除启动项：HK%s\\%s → %s"
                            % ("LM" if hive == "HKLM" else "CU", path, value))
            else:
                bk = _reg_backup(hive, path, stamp)
                if not bk:
                    # 备份失败就不删 —— 删除动作一律先留退路
                    ok = False
                    msgs.append("跳过（备份失败，未删除）：%s\\%s" % (hive, path))
                    continue
                backups.append(bk)
                _delete_tree(root, path)
                msgs.append("已删除注册表：HK%s\\%s"
                            % ("LM" if hive == "HKLM" else "CU", path))
        except Exception as e:
            ok = False
            msgs.append("失败：%s\\%s（%s）" % (hive, path, str(e)[:80]))
    if backups:
        msgs.append("注册表已备份 %d 个 .reg 文件 → %s（双击可还原）"
                    % (len(backups), os.path.join(BACKUP, "uninstall")))
    return {"ok": ok, "msg": msgs, "backups": backups,
            "backup_dir": os.path.join(BACKUP, "uninstall") if backups else ""}


# ==========================================================================
# 安装监视
#
# 用法：装软件前先开启监视 —— 先记录当前状态、再运行
# 安装包、最后列出这次安装**动了什么**并可一键清掉。这是「事前记录」，
# 比事后靠名字猜残留准得多。
#
# 刻意**不存两份全量快照**：实测 HKLM\SOFTWARE 全递归要 29.9 秒 / 61 万个键。
#   · 文件系统 —— 存基线（normcase 路径 → mtime，10 万条约 4 秒），停止时重扫
#     对比，能**准确区分**新增 / 修改 / 删除；
#   · 注册表 —— 只存「浅层基线」（HKLM/HKCU 下 SOFTWARE 的直接子键名，几千个
#     字符串），停止时用 QueryInfoKey 的 LastWriteTime **剪枝**（某键写入时间早于
#     起点就整棵跳过）。实测 29.9 秒 → 0.0~0.4 秒。
#
# 安全边界：新增的可以清（送回收站）；**修改过的只报告不处理** —— 它覆盖了原有
# 内容，删掉只会弄坏原来的程序。
#
# 已知边界：安装程序若刻意把新文件 mtime 改旧（少数安装器保留原时间戳），
# 这类抓不到。对「刚装完想反悔」这个主用途影响不大。
# ==========================================================================
_FT_EPOCH = 11644473600          # 1601-01-01 → 1970-01-01 的秒数
_IM_REG_ROOTS = (("HKLM", r"SOFTWARE"), ("HKCU", r"SOFTWARE"))

_MONITOR = {"active": False, "since": 0.0, "started": "", "user_data": False,
            "roots": [], "files": {}, "dirs": {}, "reg_top": {}}


def _ft_from_unix(u):
    """unix 秒 → 注册表用的 100ns FILETIME。"""
    return int((float(u) + _FT_EPOCH) * 10000000)


def _unix_from_ft(ft):
    """FILETIME → unix 秒（无效返回 0.0）。"""
    try:
        return (float(ft) / 10000000.0) - _FT_EPOCH if ft else 0.0
    except (TypeError, ValueError):
        return 0.0


def _im_hive(label):
    if winreg is None:
        return None
    return (winreg.HKEY_LOCAL_MACHINE if label == "HKLM"
            else winreg.HKEY_CURRENT_USER if label == "HKCU" else None)


def install_monitor_roots(user_data=False):
    """监视范围：默认只盯「安装会碰的位置」。

    %LOCALAPPDATA% / %APPDATA% 各自有 12 万 / 3.7 万个文件（实测 +7 秒 / +3 秒），
    所以放进可选项 —— 装绿色软件、或要连配置一起记录时才需要。
    """
    out = []
    for k in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        p = os.environ.get(k)
        if p and os.path.isdir(p):
            out.append(p)
    up = os.environ.get("USERPROFILE") or ""
    lad = os.environ.get("LOCALAPPDATA") or ""
    rad = os.environ.get("APPDATA") or ""
    for p in (os.path.join(up, "Desktop"),
              os.path.join(os.environ.get("PUBLIC") or r"C:\Users\Public", "Desktop"),
              os.path.join(rad, r"Microsoft\Windows\Start Menu\Programs"),
              os.path.join(lad, "Programs"),
              os.path.join(lad, r"Microsoft\Windows\Start Menu\Programs")):
        if p and os.path.isdir(p):
            out.append(p)
    if user_data:
        for p in (lad, rad):
            if p and os.path.isdir(p):
                out.append(p)
    # 短的先入，长的若落在已选根之下就跳过（否则同一批文件会被扫两遍）
    out.sort(key=len)
    res, seen = [], []
    for p in out:
        np = os.path.normcase(os.path.abspath(p))
        if np in seen or any(np.startswith(q + os.sep) for q in seen):
            continue
        seen.append(np)
        res.append(p)
    return res


def _im_snapshot(roots, cap=600000):
    """扫文件系统 → ({normcase: (mtime, 原路径)}, {normcase: 原路径})。

    键用 normcase 是必须的：Windows 路径大小写不敏感，不统一会把同一个文件
    当成两个、新增/删除判断全错。原路径一起存是为了报告时能显示（normcase
    会把整条路径变小写）。
    """
    files, dirs = {}, {}
    for base in roots:
        stack = [base]
        while stack and len(files) <= cap:
            cur = stack.pop()
            try:
                with os.scandir(cur) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                dirs[os.path.normcase(e.path)] = e.path
                                stack.append(e.path)
                            elif e.is_file(follow_symlinks=False):
                                files[os.path.normcase(e.path)] = (e.stat().st_mtime,
                                                                   e.path)
                        except OSError:
                            pass
            except OSError:
                pass
    return files, dirs


def _im_top_baseline():
    """注册表浅层基线：HKLM/HKCU 下 SOFTWARE 的直接子键名。

    用来判断「这次装的是哪个厂商」—— 安装程序必然新建自己的厂商根，这是最
    有价值的信号，而几千个字符串几乎不耗时。
    """
    out = {}
    for label, sub in _IM_REG_ROOTS:
        root = _im_hive(label)
        names = set()
        if root is not None:
            try:
                with winreg.OpenKey(root, sub, 0, winreg.KEY_READ) as k:
                    for i in range(winreg.QueryInfoKey(k)[0]):
                        try:
                            names.add(winreg.EnumKey(k, i))
                        except OSError:
                            pass
            except OSError:
                pass
        out[label] = names
    return out


def _im_scan_reg(since_ft, skip=(), cap=1500):
    """剪枝扫描注册表改动：LastWriteTime 早于起点 → 整棵子树跳过。

    实测 HKLM\\SOFTWARE（61 万个键）从 29.9 秒降到 0.0~0.4 秒 —— 监视期间只有
    极少数路径被写过，绝大多数子树在根上就被剪掉。
    """
    out = []
    if winreg is None:
        return out
    skip = tuple(s for s in (skip or ()) if s)
    for label, sub in _IM_REG_ROOTS:
        root = _im_hive(label)
        if root is None:
            continue
        stack = [sub]
        while stack and len(out) < cap:
            s = stack.pop()
            if any(s == p or s.startswith(p + "\\") for p in skip):
                continue                  # 整棵新建的子树已单独报过
            try:
                with winreg.OpenKey(root, s, 0, winreg.KEY_READ) as k:
                    nk, nv, mt = winreg.QueryInfoKey(k)
                    if not mt or mt <= since_ft:
                        continue
                    out.append({"hive": label, "path": s, "keys": nk, "vals": nv,
                                "when": _unix_from_ft(mt)})
                    for i in range(nk):
                        try:
                            stack.append(s + "\\" + winreg.EnumKey(k, i))
                        except OSError:
                            pass
            except OSError:
                pass
    return out


def _im_new_top_keys(base):
    """对比浅层基线 → 监视期间新建的厂商根（可整棵删）。"""
    out = []
    for label, names in _im_top_baseline().items():
        for nm in sorted(names - set((base or {}).get(label) or ())):
            out.append({"hive": label, "path": r"SOFTWARE\%s" % nm})
    return out


def install_monitor_start(user_data=False):
    """开始监视：记时间戳 + 建基线（文件约 4 秒，注册表瞬时）。"""
    roots = install_monitor_roots(user_data)
    if not roots:
        return {"ok": False, "msg": "没有可监视的目录"}
    t0 = time.time()
    files, dirs = _im_snapshot(roots)
    _MONITOR.update({
        "active": True, "since": t0,
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "user_data": bool(user_data), "roots": roots,
        "files": files, "dirs": dirs, "reg_top": _im_top_baseline()})
    return {"ok": True, "files": len(files), "dirs": len(dirs),
            "secs": round(time.time() - t0, 1), "roots": roots}


def install_monitor_status():
    if not _MONITOR.get("active"):
        return {"active": False, "files": 0, "elapsed": 0, "roots": []}
    return {"active": True, "started": _MONITOR.get("started") or "",
            "elapsed": int(time.time() - (_MONITOR.get("since") or 0)),
            "files": len(_MONITOR.get("files") or {}),
            "roots": list(_MONITOR.get("roots") or []),
            "user_data": bool(_MONITOR.get("user_data"))}


def install_monitor_cancel():
    """放弃本次监视（不扫描、不报告）。"""
    was = bool(_MONITOR.get("active"))
    _MONITOR.update({"active": False, "files": {}, "dirs": {}, "reg_top": {}})
    return {"ok": was, "msg": "已放弃监视" if was else "当前没有进行中的监视"}


def _im_under(p, ns):
    """p 的任一祖先目录在 ns（normcase 集合）里。"""
    par = os.path.dirname(p)
    while len(par) > 3:
        if os.path.normcase(par) in ns:
            return True
        par = os.path.dirname(par)
    return False


def install_monitor_stop(cap=3000):
    """停止监视 → 「这次安装动了什么」。"""
    st = dict(_MONITOR)
    if not st.get("active"):
        return {"ok": False, "msg": "当前没有正在进行的监视"}
    _MONITOR.update({"active": False, "files": {}, "dirs": {}, "reg_top": {}})
    t0, since = time.time(), st["since"]
    old_f, old_d = st.get("files") or {}, st.get("dirs") or {}
    roots = st.get("roots") or install_monitor_roots(st.get("user_data"))

    new_f, new_d = _im_snapshot(roots)
    created, modified, deleted = [], [], []
    for np, (mt, op) in new_f.items():
        o = old_f.get(np)
        if o is None:
            created.append((op, mt))
        elif mt - o[0] > 0.5:
            modified.append(op)
    for np, (_mt, op) in old_f.items():
        if np not in new_f:
            deleted.append(op)

    # 新增目录只留最上层：子目录一起列是噪音（删父时子跟着没）
    nd = [op for np, op in new_d.items() if np not in old_d]
    nd_ns = {os.path.normcase(p) for p in nd}
    files_out = [{"path": p, "kind": "dir", "bytes": 0,
                  "tag": "新增目录", "deletable": True}
                 for p in nd if os.path.normcase(os.path.dirname(p)) not in nd_ns]
    in_dir = 0
    for p, _mt in created:
        if _im_under(p, nd_ns):
            in_dir += 1                   # 已被上层新增目录涵盖，不重复列
            continue
        try:
            sz = os.path.getsize(p)
        except OSError:
            sz = 0
        files_out.append({"path": p, "kind": "file", "bytes": sz,
                          "tag": "新增文件", "deletable": True})
    # 被覆盖的文件只报告不删除 —— 删掉会弄坏原来的程序
    for p in modified[:cap]:
        files_out.append({"path": p, "kind": "file", "bytes": 0,
                          "tag": "被覆盖", "deletable": False})

    new_top = _im_new_top_keys(st.get("reg_top"))
    reg_hit = _im_scan_reg(_ft_from_unix(since),
                           skip=[k["path"] for k in new_top], cap=1500)
    # 剪枝是按「子键被写过 → 父键时间戳也更新」冒泡的，所以 `SOFTWARE` 这种
    # 顶层容器必然命中却毫无信息量（它自己没被写，只是记录了下属变化）。
    reg_hit = [k for k in reg_hit
               if k["path"].upper() not in ("SOFTWARE", r"SOFTWARE\WOW6432NODE")]
    reg_out = []
    for k in new_top:
        # 也过一遍删除规则（防御性）：万一将来扫描逻辑带出可删范围外的路径，
        # 这里会自动降级成「只报告」而不是让清理端拒绝。
        ok_del, is_val = _reg_del_ok({"hive": k["hive"], "path": k["path"]})
        reg_out.append({"hive": k["hive"], "path": k["path"], "value": "",
                        "tag": "新建键", "deletable": bool(ok_del and not is_val),
                        "why": "这次安装新建的厂商根（可整棵删除）"})
    for k in reg_hit:
        # ⚠️ 「被写入」**一律不可删**，哪怕路径落在可删除规则内。
        # 原因：它是**覆盖既有键**（原有程序还在用它），删掉会弄坏原来的程序；
        # 而且监视期间别的程序也在写（实测捞到搜狗输入法的键），
        # 那些根本不是这次安装带来的。只有「新建键」才是本次新增、可安全删除。
        reg_out.append({"hive": k["hive"], "path": k["path"], "value": "",
                        "tag": "被写入", "deletable": False,
                        "why": "监视期间被写入（可能是别的程序在写、也可能覆盖了"
                               "原有配置）—— 只报告，不清理"})
    return {"ok": True, "secs": int(time.time() - since),
            "scan_secs": round(time.time() - t0, 1),
            "files": files_out[:cap], "reg": reg_out[:cap],
            "deleted": deleted[:200],
            "n_created": len(created), "n_in_dir": in_dir,
            "n_new_dirs": len(nd),
            "n_modified": len(modified), "n_deleted": len(deleted),
            "n_new_top": len(new_top), "n_reg_hit": len(reg_hit),
            "truncated": len(files_out) > cap or len(reg_out) > cap}


# --------------------------------------------------------------------------
# 全机「卸载残留」扫描（孤儿项 / 残留检测）
#
# 与 residual_scan 的分工：那个是「卸载某个程序之后」针对它的复查；
# 本函数不需要任何选中项，直接找整机里**已经失去主人**的东西：
#   A 孤儿卸载项  —— ARP 里有条目，但它的卸载程序已经不存在了（最确定的残留）
#   B 孤儿启动项 / 孤儿服务 —— 指向的可执行文件已不存在
#   C 失效快捷方式 —— 桌面 / 开始菜单里指向不存在目标的 .lnk
#   D 疑似残留目录 —— 常见位置下没有任何已注册程序认领的目录（需人工确认，
#      默认不勾选）
# 结果结构与 residual_scan 一致，可直接交给同一个清理对话框（复用安全白名单）。
# --------------------------------------------------------------------------
_IMMUNE_DIRS = {
    "windows", "windowsapps", "winsxs", "temp", "tmp", "cache", "drivers",
    # 容器型目录：它们只是"放东西的地方"，本身永远不是程序残留
    "programs", "program files", "packages", "downloads", "documents",
    "desktop", "appdata", "local", "roaming", "locallow", "public", "default",
    "common files", "internet explorer", "windows nt", "windows defender",
    "windows mail", "windows media player", "windows portable devices",
    "windows security", "windows photo viewer", "windows sidebar",
    "microsoft", "microsoft corporation", "microsoft office", "microsoft edge",
    "microsoft.net", "msbuild", "reference assemblies", "windows kits",
    "uninstall information", "installshield installation information",
    "package cache", "nvidia", "nvidia corporation", "intel", "amd", "realtek",
    "google", "mozilla", "apple", "adobe", "all users",
    "modifiablewindowsapps", "systemapps", "node_modules", "dotnet",
    "windows powershell", "crashdumps", "connecteddevicesplatform",
    # 盘根上的系统级目录 —— 漏在名单外过一次就会被当成"疑似残留"扫出来，
    # 而这几个一旦被删就是整机级灾难（用户目录 / 程序数据 / 回收站 / 恢复分区）。
    "users", "programdata", "perflogs", "recovery", "msocache",
    "documents and settings", "config.msi", "onedrivetemp",
    "system volume information", "$recycle.bin",
}
# 目录名"像系统本体"的前缀：windows* / microsoft* 一律不碰
_IMMUNE_PREFIXES = ("windows", "microsoft", "system32", "syswow64", "$", ".")
# 盘根直接子项里的硬保护名单：无论扫描端怎么给，清理端都拒绝
_DRIVE_ROOT_PROTECTED = {
    "users", "programdata", "perflogs", "recovery", "msocache", "config.msi",
    "documents and settings", "system volume information", "$recycle.bin",
    "onedrivetemp", "windows", "program files", "program files (x86)",
}


def _norm_path(p):
    """规范化路径用于比对（小写、去尾部分隔符、展开环境变量）。"""
    try:
        s = os.path.expandvars((p or "").strip().strip('"'))
        if not s:
            return ""
        return os.path.normcase(os.path.abspath(s)).rstrip("\\/")
    except Exception:
        return ""


def _never_delete_paths():
    """「绝对不许删」的完整路径集合（规范化后精确比对）。

    与 _IMMUNE_DIRS 的分工：那张表按**名字**拦（任何层级的同名目录都拦），
    这张表按**完整路径**拦 —— 覆盖那些名字普通、但路径特殊的容器：
    用户配置目录、公共目录、程序数据、各盘根、系统目录、临时目录、
    桌面 / 文档 / 下载 / 开始菜单。
    """
    out = set()
    for k in ("USERPROFILE", "PUBLIC", "ProgramData", "ProgramFiles",
              "ProgramFiles(x86)", "LOCALAPPDATA", "APPDATA", "windir",
              "SystemRoot", "TEMP", "TMP", "OneDrive"):
        v = os.environ.get(k)
        if v:
            n = _norm_path(v)
            if n:
                out.add(n)
    up = os.environ.get("USERPROFILE") or ""
    pub = os.environ.get("PUBLIC") or r"C:\Users\Public"
    for base, subs in ((up, ("Desktop", "Documents", "Downloads",
                             r"AppData\LocalLow", r"AppData\Roaming",
                             r"AppData\Roaming\Microsoft\Windows\Start Menu")),
                       (pub, ("Desktop",))):
        if not base:
            continue
        for s in subs:
            n = _norm_path(os.path.join(base, s))
            if n:
                out.add(n)
    for d in ("C", "D", "E", "F", "G"):
        n = _norm_path(d + ":\\")
        if n:
            out.add(n)
    out.discard("")
    return out


_NEVER_DELETE = _never_delete_paths()
# 目录名以这些开头的一律保护。**不含 "."** —— `.vscode` / `.gradle` 这类
# 用户主目录点目录是合法的残留候选（residual_scan 专门扫它们），不能被误杀。
_PROTECT_PREFIXES = ("windows", "microsoft", "system32", "syswow64", "$")


def _is_protected_dir(path):
    """这个路径是否属于「系统 / 容器目录」—— 任何层级都判，不只看盘根一层。

    为什么升级：删除端原本只保护「盘根的直接子项」，依据是一张名字表。
    历史事故就是名字表漏了 users / programdata —— C:\\Users 被当残留扫出来，
    又被「盘根直接子项一律放行」的规则真的 rmtree 掉。名单式保护天然会漏，
    所以这里改成四层判定，命中任意一条即拒绝：
      ① 完整路径命中 _NEVER_DELETE（用户目录 / 盘根 / 系统目录 / 临时目录…）
      ② basename 命中容器或系统名单（users / appdata / local / windows …）
      ③ basename 以 _PROTECT_PREFIXES 开头（windows* / microsoft* / $*）
      ④ 父目录 basename 是 users —— 即 C:\\Users\\<任何人>，整个用户配置根
    扫描端与删除端共用这一套判定，不存在「扫得到删得掉」或「删得掉但扫不出来」
    的错位。
    """
    np = _norm_path(path)
    if not np:
        return True
    if np in _NEVER_DELETE:
        return True
    base = os.path.basename(np)
    if not base:
        return True
    if base in _DRIVE_ROOT_PROTECTED or base in _IMMUNE_DIRS:
        return True
    if base.startswith(_PROTECT_PREFIXES):
        return True
    par = os.path.dirname(np)
    if not par or re.fullmatch(r"[a-z]:", par):
        return True                       # 盘根本身（或只剩盘符）
    if os.path.basename(par) == "users":
        return True                       # C:\Users\<任意> —— 用户配置根
    return False


# --- 「送进回收站」（可还原） ------------------------------
class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p),
                ("wFunc", ctypes.c_uint),
                ("pFrom", ctypes.c_wchar_p),
                ("pTo", ctypes.c_wchar_p),
                ("fFlags", ctypes.c_ushort),
                ("fAnyOperationsAborted", ctypes.c_bool),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", ctypes.c_wchar_p)]


class _SHQUERYRBINFO(ctypes.Structure):
    # ⚠️ 字段顺序是 cbSize → i64Size → i64NumItems（别写反，写反不报错、
    # 只是永远查不到数：cbSize 不对时直接返回 E_INVALIDARG）
    _fields_ = [("cbSize", ctypes.c_ulong),
                ("i64Size", ctypes.c_longlong),
                ("i64NumItems", ctypes.c_longlong)]


_FO_DELETE = 0x0003
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040          # ← 有了它才是「移入回收站」而不是永久删除
_FOF_NOERRORUI = 0x0400


def _recycle_bin_items(root):
    """某个卷的回收站里现有多少项。查不到返回 -1。"""
    if os.name != "nt":
        return -1
    try:
        info = _SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(_SHQUERYRBINFO)
        fn = ctypes.windll.shell32.SHQueryRecycleBinW
        fn.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(_SHQUERYRBINFO)]
        fn.restype = ctypes.c_long
        if fn(root, ctypes.byref(info)) != 0:
            return -1
        return int(info.i64NumItems)
    except Exception:
        return -1


def _recycle_one(path):
    """把一个文件/目录送进回收站。返回 (结果, 说明)。

    结果 ∈ "recycled"（确实进了回收站）/ "gone"（原处没了但没进回收站，
    等于被永久删除）/ "nobin"（该卷没有回收站，没试）/ "failed"（没删掉）。

    走 shell32!SHFileOperationW(FO_DELETE | FOF_ALLOWUNDO) —— 资源管理器自己点
    「删除」用的就是它，同盘只改目录项不搬数据，大目录也快。

    ⚠️ **判定不看返回码**：实测 rc 会给出假阴性（返回 2，但文件其实已经好好
    躺在回收站里了）。这里用「文件是否还在 + 回收站项目数有没有 +1」两个事实
    来判断，避免把「已移入回收站」误报成「删除失败」，也避免把「永久删除」
    谎报成「可还原」。
    """
    if os.name != "nt":
        return "failed", "非 Windows 环境"
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if drive and not os.path.isdir(drive + "\\$Recycle.Bin"):
        return "nobin", "该磁盘没有回收站"
    root = (drive + "\\") if drive else None
    before = _recycle_bin_items(root)
    try:
        buf = ctypes.create_unicode_buffer(os.path.abspath(path) + "\0")
        op = _SHFILEOPSTRUCTW()
        op.wFunc = _FO_DELETE
        op.pFrom = ctypes.cast(buf, ctypes.c_wchar_p)
        op.fFlags = (_FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_NOERRORUI
                     | _FOF_SILENT)
        fn = ctypes.windll.shell32.SHFileOperationW
        fn.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
        fn.restype = ctypes.c_int
        rc = fn(ctypes.byref(op))
    except Exception as e:
        return "failed", "回收站调用失败（%s）" % str(e)[:40]
    if os.path.exists(path):
        return "failed", "删除未生效（码 %d）" % rc
    if _recycle_bin_items(root) > before >= 0:
        return "recycled", ""
    if rc == 0 and not op.fAnyOperationsAborted:
        return "recycled", ""
    return "gone", "该卷回收站不可用，已永久删除"


_REG_TYPE_SZ = "REG_SZ"
_BACKUP_HEADER = "Windows Registry Editor Version 5.00\r\n"


def _recycle_many(paths):
    """把一批文件/目录**一次调用**送进回收站。返回 (成功数, 失败数)。

    为什么要批量：`SHFileOperationW` 的 `pFrom` 接受「多个路径用 \\0 分隔、
    末尾再补一个 \\0」的格式，一次就能送一批 —— 垃圾清理动辄几千个文件，
    逐个调用（`_recycle_one`）会慢到没法用。

    SHFileOperation 要求同一次调用里的路径**在同一个卷**上，所以先按盘符分组。
    判定与 `_recycle_one` 一致：**不信返回码**，看文件是否真的不在了。
    """
    if os.name != "nt":
        return 0, len(paths or ())
    groups = {}
    for p in (paths or ()):
        try:
            ap = os.path.abspath(str(p))
        except Exception:
            continue
        if not os.path.exists(ap):
            continue
        drive = os.path.splitdrive(ap)[0]
        groups.setdefault(drive, []).append(ap)
    ok = bad = 0
    for drive, items in groups.items():
        # 回收站不可用的卷：交给调用方按"永久删除"计数
        if drive and not os.path.isdir(drive + "\\$Recycle.Bin"):
            bad += len(items)
            continue
        # 一次最多 2000 条，避免超长路径列表（实测这个量级没问题）
        for i in range(0, len(items), 2000):
            chunk = items[i:i + 2000]
            try:
                blob = "\0".join(chunk) + "\0\0"
                buf = ctypes.create_unicode_buffer(blob)
                op = _SHFILEOPSTRUCTW()
                op.wFunc = _FO_DELETE
                op.pFrom = ctypes.cast(buf, ctypes.c_wchar_p)
                op.fFlags = (_FOF_ALLOWUNDO | _FOF_NOCONFIRMATION
                             | _FOF_NOERRORUI | _FOF_SILENT)
                fn = ctypes.windll.shell32.SHFileOperationW
                fn.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
                fn.restype = ctypes.c_int
                fn(ctypes.byref(op))
            except Exception:
                pass
            for p in chunk:                       # 以"文件是否还在"为准
                if os.path.exists(p):
                    bad += 1
                else:
                    ok += 1
    return ok, bad


def _utf16z(s):
    return str(s).encode("utf-16-le") + b"\x00\x00"


def _multi_sz_bytes(v):
    parts = [str(x) for x in (v if isinstance(v, (list, tuple)) else [v])]
    return ("".join(p + "\0" for p in parts) + "\0").encode("utf-16-le")


def _hex_bytes(b):
    return ",".join("%02x" % x for x in bytes(b))


def _reg_key_to_text(key, sub, root_name):
    """用 winreg 递归把一个键序列化成 .reg 正文（不等价于 reg.exe，但够还原用）。"""
    out = []

    def esc(s):
        return str(s).replace("\\", "\\\\").replace('"', '\\"')

    def walk(k, cur):
        try:
            n_sub, n_val, _t = winreg.QueryInfoKey(k)
        except OSError:
            return
        vals = []
        for i in range(n_val):
            try:
                nm, data, typ = winreg.EnumValue(k, i)
            except OSError:
                continue
            vals.append((nm, data, typ))
        subs = []
        for i in range(n_sub):
            try:
                subs.append(winreg.EnumKey(k, i))
            except OSError:
                continue
        out.append("")
        out.append("[%s\\%s]" % (root_name, cur) if cur else "[%s]" % root_name)
        for nm, data, typ in vals:
            kname = "@" if nm == "" else '"%s"' % esc(nm)
            if typ == winreg.REG_DWORD:
                out.append("%s=dword:%08x" % (kname, int(data)))
            elif typ == winreg.REG_QWORD:
                out.append("%s=hex(b):%s"
                           % (kname, _hex_bytes(int(data).to_bytes(8, "little"))))
            elif typ == winreg.REG_BINARY:
                out.append("%s=hex:%s" % (kname, _hex_bytes(data)))
            elif typ == winreg.REG_EXPAND_SZ:
                out.append("%s=hex(2):%s" % (kname, _hex_bytes(_utf16z(data))))
            elif typ in (winreg.REG_MULTI_SZ, 7):
                out.append("%s=hex(7):%s" % (kname, _hex_bytes(_multi_sz_bytes(data))))
            else:
                out.append('%s="%s"' % (kname, esc(data)))
        for nm in subs:
            try:
                with winreg.OpenKey(k, nm, 0, winreg.KEY_READ) as sk:
                    walk(sk, (cur + "\\" + nm) if cur else nm)
            except OSError:
                continue

    walk(key, sub)
    return "\r\n".join(out) + "\r\n"


def _reg_backup(hive, sub, stamp):
    """删除注册表键之前导出 .reg 备份，双击即可还原。

    用 winreg 手工序列化，**不调用 reg.exe** —— 项目里已有这条约定：外部命令行
    工具可能被安全策略直接拦掉（sc.exe 就这样栽过），能走 API 就走 API。
    """
    name = _HIVE_NAMES.get(hive)
    root = _HIVE_KEYS.get(hive)
    if winreg is None or not name or not sub or root is None:
        return ""
    d = os.path.join(BACKUP, "uninstall")
    try:
        os.makedirs(d, exist_ok=True)
        with winreg.OpenKey(root, sub, 0, winreg.KEY_READ) as k:
            body = _reg_key_to_text(k, sub, name)
    except OSError:
        return ""
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", sub)[:80] or "key"
    out = os.path.join(d, "%s_%s_%s.reg" % (stamp, hive, safe))
    try:
        # .reg 5.00 必须是 UTF-16LE + BOM
        with open(out, "w", encoding="utf-16", newline="") as fh:
            fh.write(_BACKUP_HEADER)
            fh.write(body)
    except OSError:
        return ""
    return out if os.path.exists(out) else ""


def _entry_gone_exe(it):
    """一个 ARP 条目是否「已失效」—— 返回那个已不存在的卸载程序路径，否则 ""。

    判定：只有卸载程序本身没了，这个程序的安装记录才算失效，
    它指向的目录才有资格当残留候选。MSI 条目由系统维护，一律不算。
    """
    cmd = (it.get("uninstall") or it.get("quiet") or "").strip()
    if not cmd or "msiexec" in cmd.lower():
        return ""
    exe, _a = parse_uninstall_cmd(cmd)
    if not exe or not os.path.isabs(exe) or os.path.exists(exe):
        return ""
    return exe


def _orphan_dir_evidence(inst):
    """正向证据链：残留目录 =「已失效安装记录」指向、且仍然存在的目录。

    返回 {规范化路径: {"path", "src", "why", "missing"}}。

    与旧实现方向完全相反 ——
      旧：遍历盘根与常见目录，列出「没有任何已装程序认领的目录」，**负向排除**。
          安全性 100% 押在免疫名单上：名单漏一个名字（历史事故 users /
          programdata）就会被扫出来，而删除端「盘根直接子项一律放行」会真的
          rmtree 掉 —— 结构性风险，补名单只是打补丁。
      新：只认有证据的目录 —— 必须存在一条失效记录，且它记录的安装位置 /
          卸载程序目录 / 图标目录正好落在这个目录上，**正向证据**。也就是
          卸载后复查残留、检查孤立安装目录的做法：
          先确定「这是谁的」，再谈要不要删。
    代价是检出数量下降（宁可漏报），这正是安全该有的方向。
    """
    ev = {}
    for it in inst or []:
        gone = _entry_gone_exe(it)
        if not gone:
            continue
        name = it.get("name") or ""
        cands = []
        loc = str(it.get("location") or "").strip()
        if loc and os.path.isabs(loc):
            cands.append((loc, "原安装位置"))
        cands.append((os.path.dirname(gone), "卸载程序所在目录"))
        icon = str(it.get("icon") or "").split(",")[0].strip().strip('"')
        if icon and os.path.isabs(icon):
            cands.append((os.path.dirname(icon), "图标所在目录"))
        for raw, why in cands:
            np = _norm_path(raw)
            if not np or np in ev or not os.path.isdir(raw):
                continue
            if _is_protected_dir(raw):
                continue                      # 系统 / 容器目录直接不要
            ev[np] = {"path": os.path.abspath(raw), "src": name,
                      "why": why, "missing": gone}
    return ev


def orphan_index():
    """已装程序索引。

    claimed：被「有主」的东西认领的目录 —— ARP 的 InstallLocation / 卸载程序目录 /
             图标目录 + MSI 安装目录索引 + 所有服务的 ImagePath 目录 +
             启动项 Exe 目录。多来源认领是精度的关键：只看 InstallLocation 会把
             Logitech G HUB、SQL Server、MuMu 这类没写 InstallLocation 的程序
             全报成「残留目录」（实测 111 个里大半是这类误报）。
    toks：   所有已装软件名/发布者的特征词，供目录名比对。
    """
    claimed, toks = set(), set()
    inst = list_software()
    for it in inst:
        for src in (it.get("name"), it.get("publisher")):
            full, _w = _res_tokens(src or "", "")
            toks |= full
        # 已失效的记录不能「认领」目录 —— 否则它自己的残留目录会被自己挡住，
        # 证据链刚找到就又被排除掉（两个方向必须一致）。
        if _entry_gone_exe(it):
            continue
        loc = _norm_path(it.get("location"))
        if loc:
            claimed.add(loc)
        icon = (it.get("icon") or "").split(",")[0].strip().strip('"')
        if icon:
            d = _norm_path(os.path.dirname(icon))
            if d:
                claimed.add(d)
        for cmd in (it.get("uninstall"), it.get("quiet")):
            exe, _a = parse_uninstall_cmd(cmd or "")
            if exe and os.path.isabs(exe):
                d = _norm_path(os.path.dirname(exe))
                if d:
                    claimed.add(d)
    claimed |= _msi_folder_dirs()
    claimed |= _svc_image_dirs()
    claimed |= _startup_dirs()
    return claimed, toks, inst


def _orphan_entries(inst, cap):
    """A. 孤儿卸载项：卸载程序文件已不存在（MSI 条目由系统维护，跳过）。"""
    out = []
    for it in inst:
        cmd = it.get("uninstall") or it.get("quiet") or ""
        key = it.get("key") or ""
        if not cmd or "msiexec" in cmd.lower():
            continue
        exe, _a = parse_uninstall_cmd(cmd)
        if not exe or not os.path.isabs(exe) or os.path.exists(exe):
            continue
        hive, _, rest = key.partition("\\")
        out.append({"hive": hive, "path": rest, "tag": "孤儿卸载项",
                    "value": it.get("name") or "", "missing": exe})
        if len(out) >= cap:
            break
    return out


def _svc_exe(img):
    """服务 ImagePath → 可执行文件绝对路径。

    解析不确定时返回 ""（宁可漏报也不误报）：无引号且含空格的路径无法可靠切分，
    这类直接放弃 —— 否则 `C:\\Program Files\\...` 会被截成 `C:\\Program`，
    把仍在运行的服务误报成孤儿（实测 edgeupdate / MySQL80 等大面积误报）。
    """
    s = str(img or "").strip()
    if not s:
        return ""
    m = re.match(r'^"([^"]+)"', s)           # "C:\...\x.exe" -k
    if m:
        s = m.group(1)
    else:
        s = s.split(" ")[0] if " " in s else s
    s = s.strip('"')
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    s = re.sub(r"^\\\?\?\\", "", s)          # \??\C:\... → C:\...
    low = s.lower()
    if low.startswith("\\systemroot\\"):     # \SystemRoot\System32\... → C:\Windows
        s = os.path.join(sysroot, s[len("\\systemroot\\"):])
    elif low.startswith("system32\\"):
        s = os.path.join(sysroot, s)
    s = os.path.expandvars(s)
    if not re.search(r"\.(?:exe|sys|dll)$", s, re.I):
        return ""
    return s


def _svc_image_dirs():
    """所有服务的 ImagePath 所在目录（有服务的程序目录不该被当成残留目录）。"""
    dirs = set()
    if winreg is None:
        return dirs
    base = r"SYSTEM\CurrentControlSet\Services"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base, 0, winreg.KEY_READ) as k:
            for i in range(winreg.QueryInfoKey(k)[0]):
                try:
                    sn = winreg.EnumKey(k, i)
                    with winreg.OpenKey(k, sn) as sk:
                        img = str(winreg.QueryValueEx(sk, "ImagePath")[0])
                except OSError:
                    continue
                exe = _svc_exe(img)
                if exe and os.path.isabs(exe):
                    d = _norm_path(os.path.dirname(exe))
                    if d:
                        dirs.add(d)
    except OSError:
        pass
    return dirs


def _startup_dirs():
    """启动项指向的目录（有自启动的程序目录不该被当成残留目录）。"""
    dirs = set()
    if winreg is None:
        return dirs
    for hive, sub in STARTUP_LOCATIONS:
        root = winreg.HKEY_LOCAL_MACHINE if hive == "HKLM" else winreg.HKEY_CURRENT_USER
        try:
            with winreg.OpenKey(root, sub, 0, winreg.KEY_READ) as k:
                for i in range(winreg.QueryInfoKey(k)[1]):
                    try:
                        _vn, vd, _t = winreg.EnumValue(k, i)
                    except OSError:
                        continue
                    exe, _a = parse_uninstall_cmd(str(vd))
                    if exe and os.path.isabs(exe):
                        d = _norm_path(os.path.dirname(exe))
                        if d:
                            dirs.add(d)
        except OSError:
            continue
    return dirs


def _msi_folder_dirs():
    """MSI 安装目录索引（`Installer\\Folders` 的值名即目录路径）——
    MSI 装的程序大多不写 InstallLocation，靠这个索引认领它们的目录。"""
    dirs = set()
    if winreg is None:
        return dirs
    sub = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Installer\Folders"
    for flag in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub, 0,
                                winreg.KEY_READ | flag) as k:
                for i in range(winreg.QueryInfoKey(k)[1]):
                    try:
                        vn, _vd, _t = winreg.EnumValue(k, i)
                    except OSError:
                        continue
                    d = _norm_path(vn)
                    if d:
                        dirs.add(d)
        except OSError:
            continue
    return dirs


def _orphan_startup(cap):
    """B. 孤儿启动项：Run/RunOnce 的值指向不存在的可执行文件。"""
    out = []
    if winreg is None:
        return out
    for hive, sub in STARTUP_LOCATIONS:
        root = winreg.HKEY_LOCAL_MACHINE if hive == "HKLM" else winreg.HKEY_CURRENT_USER
        try:
            with winreg.OpenKey(root, sub, 0, winreg.KEY_READ) as k:
                for i in range(winreg.QueryInfoKey(k)[1]):
                    try:
                        vn, vd, _t = winreg.EnumValue(k, i)
                    except OSError:
                        continue
                    if _exe_from_path(vd):          # 目标还在
                        continue
                    exe, _a = parse_uninstall_cmd(str(vd))
                    if not exe or not os.path.isabs(exe) or os.path.exists(exe):
                        continue                    # 系统命令 / 相对路径，不动
                    out.append({"hive": hive, "path": sub, "value": vn,
                                "tag": "孤儿启动项"})
                    if len(out) >= cap:
                        return out
        except OSError:
            continue
    return out


def _orphan_services(cap):
    """B. 孤儿服务：ImagePath 指向的可执行文件已不存在。"""
    out = []
    base = r"SYSTEM\CurrentControlSet\Services"
    if winreg is None:
        return out
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base, 0, winreg.KEY_READ) as k:
            for i in range(winreg.QueryInfoKey(k)[0]):
                try:
                    sn = winreg.EnumKey(k, i)
                except OSError:
                    continue
                if sn.lower() in _SKIP_ENTRIES:
                    continue
                try:
                    with winreg.OpenKey(k, sn) as sk:
                        try:
                            img = str(winreg.QueryValueEx(sk, "ImagePath")[0])
                        except OSError:
                            continue
                except OSError:
                    continue
                exe = _svc_exe(img)
                if not exe or not os.path.isabs(exe) or os.path.exists(exe):
                    continue
                out.append({"hive": "HKLM", "path": base + "\\" + sn,
                            "tag": "孤儿服务", "value": exe})
                if len(out) >= cap:
                    break
    except OSError:
        pass
    return out


def _orphan_dirs(claimed, toks, cap, inst=None):
    """D. 残留目录 —— 走正向证据链，而不是「无人认领」。

    旧实现在这里遍历盘根与常见目录、列出「没有程序认领的目录」，属负向排除；
    详见 _orphan_dir_evidence 的说明：那条路一旦名单漏名就会删到系统目录。
    现在只认「有失效安装记录指向」的目录，并逐条带上证据供界面显示来源。
    """
    ev = _orphan_dir_evidence(list_software() if inst is None else list(inst))
    claimed = claimed or set()
    out = []
    for np, info in ev.items():
        if len(out) >= min(cap, 45):
            break
        p = info["path"]
        # 只按「整目录被认领」排除（活着的程序的安装位置正好是它）。
        # 不复用旧的「父目录被认领就跳过」：claimed 里含 %TEMP%、MSI 索引这类
        # 宽泛容器，父级前缀匹配会把证据链刚找到的目录整片吞掉（实测如此）。
        if np in claimed:
            continue
        if not os.path.isdir(p) or _is_protected_dir(p):
            continue
        try:
            sz = dir_size(p, cap=600)[0]
        except Exception:
            sz = 0
        out.append({"path": p, "kind": "dir", "bytes": sz,
                    "tag": "残留目录·%s" % info["why"], "weak": True,
                    "evidence": "程序「%s」的%s；卸载程序已不存在：%s"
                                % (info["src"], info["why"], info["missing"])})
    out.sort(key=lambda x: -x["bytes"])
    return out[:min(cap, 45)]


# 安装类根目录：卸载后留下的「空目录」几乎都在这几个下面
_LEFTOVER_ROOTS = ("ProgramFiles", "ProgramFiles(x86)", "ProgramData", "LOCALAPPDATA")

# 空残留扫描额外要跳过的目录名 —— 这些是 **Windows / 安装框架自己建的**，
# 空着也不代表有程序被卸载了，报出来只会伤可信度（本机实测这类占了十几条）：
#   templates            C:\ProgramData\Templates（系统模板）
#   virtualstore         文件系统虚拟化重定向目录（系统）
#   placeholdertilelogofolder  开始菜单磁贴占位图缓存（系统，本机十几条）
#   package cache        ⚠️ **MSI 的安装包缓存，删了会导致程序无法修复/卸载**
#   installer            C:\Windows\Installer 同类（本工具本来就刻意不收）
#   dbg / deployment / elevateddiagnostics / history / application data /
#   temporary internet files  都是 OS 自己建的
_LEFTOVER_EXTRA_SKIP = {
    "templates", "virtualstore", "placeholdertilelogofolder", "package cache",
    "installer", "dbg", "deployment", "elevateddiagnostics", "history",
    "application data", "temporary internet files", "publisher", "clrgen",
    "assembly", "whesvc", "pnputil", "logs", "log", "record", "crashlogs",
}


def _dir_has_any_file(path, cap=500):
    """目录（含子树）里是否存在任何文件。读不到 → 当作有文件（保守，不误报）。"""
    n = 0
    try:
        for _dp, _dn, fn in os.walk(path):
            if fn:
                return True
            n += 1
            if n > cap:
                return True                # 目录极深，避免拖死
    except OSError:
        return True
    return False


def _leftover_empty_dirs(claimed=None, cap=40, max_depth=2, toks=None):
    """空目录残留 —— 安装根下「整棵子树不含任何文件」的目录。

    **为什么必须单独有这一条**：`_orphan_dir_evidence` 要求「存在一条**已失效的
    ARP 记录**指向这个目录」。但程序被**完全卸载**时，ARP 记录会一起消失 ——
    此时留下的空文件夹**没有任何证据链可依**，于是永远不显示。
    而「卸载完只留一个空文件夹」恰恰是最常见的情形（本机实测：350 条 ARP 里
    只有 1 个目录能被证据链捕获）。

    对应的做法是「搜索词 × 搜索路径」直接找，
    不依赖安装记录。这里取一个**更保守**的版本：只报**完全不含文件**的目录 ——
    删掉它不可能丢任何数据，所以即使没有证据也安全。另外必须：
      · 不属于任何在装程序（claimed / 装程序名下的目录）；
      · 不是受保护目录（`_is_protected_dir`）；
      · 名字不像系统目录（走 _SKIP_ENTRIES）。
    结果标 weak=True（低置信、默认不勾选），由用户逐项确认。

    `toks`（在装程序名/发布者的特征词，取自 `orphan_index()`）非空时，
    路径里**任意一段**命中就跳过：那段说明这个空目录属于**仍在装**的厂商
    （`ProgramData` 下的 `ASUS\\Virtual Pet`、`Local` 下的 `Tencent\\QQ` 这种），
    有主 ≠ 孤儿。本机实测这一步把 147 条降到 120 条，砍掉的全是同类噪音。
    """
    claimed = {os.path.normcase(str(c)) for c in (claimed or ())}
    toks = set(toks or ())
    out = []
    # 内部多收一些再排序：只按根目录顺序取前 cap 个会让 `%ProgramFiles%` 把
    # 名额占满，`%LOCALAPPDATA%\Programs\<产品>`（最常见的空残留）反而看不到。
    pool = max(int(cap), 400)
    stop = False
    for env in _LEFTOVER_ROOTS:
        if stop:
            break
        base = os.environ.get(env) or ""
        if not base or not os.path.isdir(base):
            continue
        stack = [(base, 0)]
        base_len = len(os.path.normpath(base)) + 1
        while stack:
            if len(out) >= pool:
                stop = True
                break
            cur, depth = stack.pop()
            try:
                entries = list(os.scandir(cur))
            except (OSError, PermissionError):
                continue
            for e in entries:
                if len(out) >= pool:          # 单个目录里的条目也要实时卡住
                    stop = True
                    break
                if not e.is_dir(follow_symlinks=False):
                    continue
                nm = e.name
                if nm.lower() in _SKIP_ENTRIES or nm.startswith("."):
                    continue
                p = e.path
                # 路径中**任意一段**命中跳过名单就跳过 —— 只查叶子名不够：
                # `...\Microsoft Update Health Tools\Logs`、`...\Adobe Acrobat\DC`
                # 这类要按中间段（microsoft / adobe）挡掉，否则系统/厂商目录会被
                # 当成"卸载残留"报出来。
                rel = os.path.normpath(p)[base_len:]
                segs = [s.lower() for s in rel.split(os.sep) if s]
                # 整段命中，或段里的**某个词**命中（`Microsoft Office` /
                # `NVIDIA Corporation` 这类要按词挡，不能只看整段）
                if any((s in _SKIP_ENTRIES or s in _LEFTOVER_EXTRA_SKIP)
                       or any(w in _SKIP_ENTRIES or w in _LEFTOVER_EXTRA_SKIP
                              for w in s.split())
                       for s in segs):
                    continue
                # 属于**仍在装**的程序 → 有主，不是孤儿残留（复用 `_res_hit`，
                # 按词匹配、不模糊，避免通用词误伤）。
                # ponytail: 厂商级判断，"厂商在装"不等于"这个子目录不是残留"
                # （如 Rockstar 在装但 GTAV 已卸载）。真需要极精确就改逐条人工确认；
                # 被跳过的空目录在「磁盘工具 → 空文件夹清理」里仍能找到。
                if toks and any(_res_hit(s, set(), toks, fuzzy=False) for s in segs):
                    continue
                np = os.path.normcase(os.path.abspath(p))
                owned = np in claimed
                # ⚠️ 受保护 ≠ 不能往里看。`%LOCALAPPDATA%\Programs`、`C:\Program Files`
                # 都在免疫名单里（**删除**时必须挡住），但它们的**子目录**才是残留
                # 目录真正所在（`%LOCALAPPDATA%\Programs\<产品>`）。早先在这里直接
                # `continue`，整棵 Programs 子树都不看 —— 本机实测漏掉全部 120 个。
                # 所以：受保护目录本身永不上报，但允许在**第一层**往里下钻找空目录。
                is_prot = _is_protected_dir(p)
                if not owned and not is_prot and not _dir_has_any_file(p):
                    # 整棵子树无文件 → 空残留。**不再往下报子目录**：父目录已经空了，
                    # 子目录必然也空，一起列出来只是噪音（删父时子跟着没）。
                    try:
                        age_h = (time.time() - os.path.getmtime(p)) / 3600.0
                    except OSError:
                        age_h = 0.0
                    out.append({"path": p, "kind": "dir", "bytes": 0,
                                "tag": "空残留目录", "weak": True,
                                "age_hours": round(age_h, 1),
                                "evidence": "整个目录不含任何文件（%%%s%% 下），"
                                            "既没有在装程序认领、也没有安装记录 —— "
                                            "典型是卸载后遗留的空壳目录" % env})
                    continue
                # 有文件（或属于在装程序 / 受保护）→ 自己不能删，但**空子目录**
                # 仍可能在里面，继续下钻一层。覆盖 `%ProgramFiles%\<厂商>\<产品>`。
                if owned:
                    continue                      # 在装程序自己的目录：整棵不看
                if depth + 1 < max_depth and (not is_prot or depth == 0):
                    stack.append((p, depth + 1))
    # 越旧的越可能是残留（刚创建的可能是程序正在用），按年龄降序取前 cap 个
    out.sort(key=lambda x: -x.get("age_hours", 0))
    return out[:cap]


def _broken_shortcuts(cap):
    """C. 失效快捷方式：桌面 / 开始菜单里指向不存在目标的 .lnk。"""
    out = []
    up = os.environ.get("USERPROFILE") or ""
    pub = os.environ.get("PUBLIC") or r"C:\Users\Public"
    dat = os.environ.get("ProgramData") or ""
    sm = r"Microsoft\Windows\Start Menu\Programs"
    dirs = [os.path.join(up, "Desktop"), os.path.join(pub, "Desktop"),
            os.path.join(up, "AppData", "Roaming", sm), os.path.join(dat, sm)]
    dirs = [d for d in dirs if d and os.path.isdir(d)]
    if not dirs:
        return out
    script = ("$sh=New-Object -ComObject WScript.Shell;"
              "foreach($d in @(%s)){ Get-ChildItem -LiteralPath $d -Filter *.lnk "
              "-Recurse -Depth 1 -ErrorAction SilentlyContinue | ForEach-Object {"
              " $t=$sh.CreateShortcut($_.FullName).TargetPath;"
              " if($t -and -not (Test-Path -LiteralPath $t)){"
              " '{0}|||{1}' -f $_.FullName,$t } } }"
              % ",".join("'%s'" % d.replace("'", "''") for d in dirs))
    try:
        rc, o = ps(script, timeout=90)
    except Exception:
        return out
    for line in (o or "").splitlines():
        line = line.strip()
        if "|||" not in line:
            continue
        lnk, _target = line.split("|||", 1)
        lnk = lnk.strip()
        if lnk and os.path.exists(lnk):
            out.append({"path": lnk, "kind": "file", "bytes": 0,
                        "tag": "失效快捷方式"})
            if len(out) >= cap:
                break
    return out


def orphan_scan(cap=120, should_stop=None):
    """全机卸载残留扫描（不需要选中程序）。

    返回结构与 residual_scan 相同（files / reg），可直接交给残留清理对话框 ——
    删除沿用同一套安全白名单与注册表分类规则。
    高置信项（孤儿卸载项 / 启动项 / 服务 / 失效快捷方式）排在前，
    低置信项（疑似残留目录）标 weak 由用户确认。
    """
    claimed, toks, inst = orphan_index()
    files, regs = [], []
    # 每一步之间给一次「暂停」的机会（每步内部都很快，没必要再往里钻）
    if should_stop is not None and should_stop():
        return {"files": files, "reg": regs, "tokens": [], "stopped": True}
    regs += _orphan_entries(inst, cap)
    regs += _orphan_startup(cap)
    regs += _orphan_services(cap)
    files += _broken_shortcuts(cap)
    if should_stop is not None and should_stop():
        return {"files": files, "reg": regs, "tokens": [], "stopped": True}
    files += _orphan_dirs(claimed, toks, cap, inst=inst)
    # 空目录残留：没有失效 ARP 记录可依（程序被完全卸载时记录也没了），
    # 只能靠「安装根下的空壳目录」判定。放在证据链之后、统一标 weak。
    files += _leftover_empty_dirs(claimed, cap=min(cap, 60), toks=toks)
    for f in files:                   # 统一数据契约：每项都显式带 weak
        f.setdefault("weak", False)
    return {"files": files[:cap], "reg": regs[:cap], "tokens": []}


def _exe_from_path(cmd):
    """从命令行 / 路径里提取可执行文件路径（供 UI 显示图标）。"""
    c = (cmd or "").strip()
    if not c:
        return ""
    if c.startswith('"'):
        end = c.find('"', 1)
        p = c[1:end] if end > 0 else c.strip('"')
    else:
        p = c.split(" ")[0]
    p = os.path.expandvars(p).strip().strip('"')
    return p if os.path.isfile(p) else ""


# --------------------------------------------------------------------------
# 开机启动项：启用/禁用状态、发布者、以及「定位」需要的辅助信息
#
# Windows 任务管理器的「禁用」**不删除** Run 值，而是在
#     ...\Explorer\StartupApproved\Run（或 Run32 / StartupFolder）
# 下写一条 12 字节记录：首字节最低位 = 1 表示已禁用，后 8 字节是时间戳。
# 读它 → 能把「以前禁用过的项」也一起列出来；写它 → 就是启用/禁用。
# --------------------------------------------------------------------------
_APPR_KEYS = {
    "run": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run",
    "run32": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run32",
    "folder": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder",
}
# 来源子键 → StartupApproved 里的对应位置（RunOnce 不受任务管理器管理）
_APPR_KIND = {
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run": "run",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run": "run32",
}
_PUB_CACHE = {}


def _approved_state(kind, name):
    """True=已启用 / False=已禁用 / None=没有记录（按启用算）。"""
    if winreg is None or not kind or not name:
        return None
    sub = _APPR_KEYS.get(kind)
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ) as k:
                data, _t = winreg.QueryValueEx(k, name)
        except OSError:
            continue
        if isinstance(data, (bytes, bytearray)) and data:
            return not (int(data[0]) & 1)
    return None


def set_startup_enabled(hive, sub, name, folder=False, on=True):
    """启用/禁用开机启动项：只动 StartupApproved 标志，不删 Run 值。

    禁用后该项仍会出现在「启动与服务」页（状态显示「已禁用」），也仍能在
    任务管理器里看到 —— 与 Windows 自身行为一致，随时可以再启用。
    """
    if winreg is None:
        return {"ok": False, "msg": "此系统不支持注册表操作", "verified": False}
    kind = "folder" if folder else _APPR_KIND.get(sub)
    if not kind:
        return {"ok": False, "verified": False,
                "msg": "RunOnce 项不支持启用/禁用，需要时请用「移除所选启动项」。"}
    root = winreg.HKEY_LOCAL_MACHINE if hive == "HKLM" else winreg.HKEY_CURRENT_USER
    if on:
        data = b"\x02\x00\x00\x00" + b"\x00" * 8
    else:
        ft = int((time.time() + 11644473600) * 10000000) & 0xFFFFFFFFFFFFFFFF
        data = b"\x03\x00\x00\x00" + struct.pack("<Q", ft)
    try:
        k = winreg.CreateKeyEx(root, _APPR_KEYS[kind], 0, winreg.KEY_SET_VALUE)
    except OSError as e:
        return {"ok": False, "verified": False, "msg": "无法写入（多为权限不足）：%s" % e}
    try:
        winreg.SetValueEx(k, name, 0, winreg.REG_BINARY, data)
    except OSError as e:
        return {"ok": False, "verified": False, "msg": "写入失败：%s" % e}
    finally:
        winreg.CloseKey(k)
    # 写操作一律回读复核：系统不接受就必须如实报错，不能写了就报成功
    got = _approved_state(kind, name)
    if got is None or got != bool(on):
        return {"ok": False, "verified": False,
                "msg": "写入后回读不一致，系统可能拒绝了这次修改。"}
    # 记一笔「这是本工具禁用的」——系统里没有别的线索，换机器就靠它（见 _REC_ROOT）
    if on:
        disabled_record_del("startup", hive=hive, sub=sub, name=name,
                            folder=bool(folder))
    else:
        disabled_record_add("startup", hive=hive, sub=sub, name=name,
                            folder=bool(folder))
    return {"ok": True, "verified": True,
            "msg": "已%s「%s」" % ("启用" if on else "禁用", name)}


_VER_DLL = None


def _ver_dll():
    """version.dll（文件版本资源）：按需初始化一次，签名一次写全。"""
    global _VER_DLL
    if _VER_DLL is None:
        d = ctypes.WinDLL("version", use_last_error=True)
        d.GetFileVersionInfoSizeW.argtypes = [wt.LPCWSTR, ctypes.POINTER(wt.DWORD)]
        d.GetFileVersionInfoSizeW.restype = wt.DWORD
        d.GetFileVersionInfoW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p]
        d.GetFileVersionInfoW.restype = wt.BOOL
        d.VerQueryValueW.argtypes = [ctypes.c_void_p, wt.LPCWSTR,
                                     ctypes.POINTER(ctypes.c_void_p),
                                     ctypes.POINTER(ctypes.c_uint)]
        d.VerQueryValueW.restype = wt.BOOL
        _VER_DLL = d
    return _VER_DLL


def file_publisher(path):
    """读文件版本资源里的「公司名」（任务管理器里的「发布者」列），取不到返回 ""。"""
    key = os.path.normcase(path or "")
    if key in _PUB_CACHE:
        return _PUB_CACHE[key]
    val = ""
    if key and os.path.isfile(path):
        try:
            ver = _ver_dll()
            size = ver.GetFileVersionInfoSizeW(path, None)
            if size:
                buf = ctypes.create_string_buffer(size)
                if ver.GetFileVersionInfoW(path, 0, size, buf):
                    ptr = ctypes.c_void_p()
                    ln = ctypes.c_uint()
                    if (ver.VerQueryValueW(buf, "\\VarFileInfo\\Translation",
                                           ctypes.byref(ptr), ctypes.byref(ln))
                            and ln.value >= 4):
                        lang, cp = struct.unpack("<HH", ctypes.string_at(ptr, 4))
                        for field in ("CompanyName", "ProductName"):
                            sub = "\\StringFileInfo\\%04x%04x\\%s" % (lang, cp, field)
                            p2 = ctypes.c_void_p()
                            l2 = ctypes.c_uint()
                            if (ver.VerQueryValueW(buf, sub, ctypes.byref(p2),
                                                   ctypes.byref(l2)) and l2.value):
                                txt = (ctypes.wstring_at(p2, l2.value) or "").strip()
                                if txt:
                                    val = txt
                                    break
        except Exception:
            val = ""
    _PUB_CACHE[key] = val
    return val


_SVC_REG_CACHE = {"t": 0.0, "rows": []}
_SVC_REG_LOCK = threading.Lock()


def services_registry():
    """只读注册表枚举 Win32 服务（比 CIM 快一个数量级），供「定位服务」用。

    除了名称/显示名/镜像路径，还带上启动类型、登录身份、描述与依赖列表
    —— 这些正好够「服务详情」弹窗用，不用再去起 CIM 查询。
    """
    with _SVC_REG_LOCK:
        if _SVC_REG_CACHE["rows"] and time.time() - _SVC_REG_CACHE["t"] < 60:
            return _SVC_REG_CACHE["rows"]
    rows = []
    if winreg is not None:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SYSTEM\CurrentControlSet\Services") as root:
                n = winreg.QueryInfoKey(root)[0]
                for i in range(n):
                    try:
                        nm = winreg.EnumKey(root, i)
                    except OSError:
                        continue
                    try:
                        with winreg.OpenKey(root, nm) as k:
                            def _g(v, _k=k):
                                try:
                                    return winreg.QueryValueEx(_k, v)[0]
                                except OSError:
                                    return None
                            typ = _g("Type")
                            if typ is None or not (int(typ) & 0x30):
                                continue        # 只要 Win32 服务（0x10/0x20），跳过内核驱动
                            dep = _g("DependOnService") or []
                            if isinstance(dep, str):
                                dep = [dep]
                            rows.append({"name": nm,
                                         "display": str(_g("DisplayName") or ""),
                                         "desc": str(_g("Description") or ""),
                                         "img": str(_g("ImagePath") or ""),
                                         "account": str(_g("ObjectName") or ""),
                                         "start": int(_g("Start") if _g("Start") is not None else 3),
                                         "type": int(typ),
                                         "depend": [str(d) for d in dep]})
                    except (OSError, ValueError, TypeError):
                        continue
        except OSError:
            rows = []
    with _SVC_REG_LOCK:
        _SVC_REG_CACHE["t"] = time.time()
        _SVC_REG_CACHE["rows"] = rows
    return rows


_SVC_START_NAMES = {0: "启动引导", 1: "系统加载", 2: "自动", 3: "手动", 4: "已禁用"}


_SHLWAPI = None


def resolve_indirect(s):
    """把 `@文件,-资源号` 形式的**间接字符串**翻成真实文本（翻不了就原样返回）。

    Windows 服务的 DisplayName / Description 大量以这种形式存放，
    直接显示出来就是 `@%systemroot%\\system32\\spoolsv.exe,-2` 这种看不懂的东西。
    """
    if not s or not s.startswith("@"):
        return s
    global _SHLWAPI
    try:
        if _SHLWAPI is None:
            d = ctypes.WinDLL("shlwapi", use_last_error=True)
            d.SHLoadIndirectString.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.UINT,
                                               ctypes.c_void_p]
            d.SHLoadIndirectString.restype = ctypes.c_long
            _SHLWAPI = d
        buf = ctypes.create_unicode_buffer(1024)
        if _SHLWAPI.SHLoadIndirectString(s, buf, 1024, None) == 0 and buf.value.strip():
            return buf.value.strip()
    except Exception:
        pass
    return s


def service_detail(name):
    """服务的只读详情：注册表信息 + 依赖关系 + 当前运行状态。

    做成自绘弹窗的数据源，替代「打开 services.msc 让用户自己找」。
    """
    nm = (name or "").strip()
    if not nm:
        return {}
    row = next((s for s in services_registry()
                if s["name"].lower() == nm.lower()), None)
    if row is None:
        return {}
    out = dict(row)
    out["status"] = service_status(row["name"])
    out["state_text"] = service_state_text(row["name"]) or "无法读取"
    out["start_text"] = _SVC_START_NAMES.get(int(row.get("start") or 3), "未知")
    out["type_text"] = "共享进程（svchost）" if (int(row.get("type") or 0) & 0x20) \
        else "独立进程"
    low = nm.lower()
    out["dependents"] = [s["name"] for s in services_registry()
                         if low in [d.lower() for d in (s.get("depend") or [])]]
    # DisplayName / Description 多为间接字符串（@文件,-资源号），翻成人话再给界面
    out["display_text"] = resolve_indirect(out.get("display") or "")
    out["desc_text"] = resolve_indirect(out.get("desc") or "")
    return out


def find_service_for(name, exe=""):
    """给一个启动项找它对应的服务名，找不到返回 ""。

    顺序：服务名 / 显示名精确匹配 → 去掉 .exe 再比 → ImagePath 指向同一个可执行文件。
    最后一条要排除 svchost.exe 这类被几十个服务共享的宿主，否则没有区分度。
    """
    nm = (name or "").strip().lower()
    base = nm[:-4] if nm.endswith(".exe") else nm
    exe_l = os.path.normcase(exe or "").strip().strip('"')
    shared_host = os.path.basename(exe_l) in ("svchost.exe", "rundll32.exe", "dllhost.exe")
    exact, by_exe = [], []
    for s in services_registry():
        if nm and nm in (s["name"].lower(), s["display"].lower()):
            exact.append(s)
            continue
        if base and base == s["name"].lower():
            exact.append(s)
            continue
        if exe_l and s.get("img") and not shared_host:
            raw = os.path.normcase(str(s["img"]).strip().strip('"'))
            if raw.startswith(exe_l):
                by_exe.append(s)
    if exact:
        return exact[0]["name"]
    if len(by_exe) == 1:
        return by_exe[0]["name"]
    return ""


# --------------------------------------------------------------------------
# 服务当前状态（advapi32 直读，不起任何外部进程）
# --------------------------------------------------------------------------
_SC_MANAGER_CONNECT = 0x0001
_SERVICE_QUERY_STATUS = 0x0004
_SERVICE_CHANGE_CONFIG = 0x0002
_SERVICE_NO_CHANGE = 0xFFFFFFFF
_SVC_STATE_TEXT = {1: "已停止", 2: "正在启动", 3: "正在停止", 4: "运行中",
                   5: "正在继续", 6: "正在暂停", 7: "已暂停"}
_ADVAPI32 = None


class _SERVICE_STATUS_PROCESS(ctypes.Structure):
    _fields_ = [("dwServiceType", wt.DWORD), ("dwCurrentState", wt.DWORD),
                ("dwControlsAccepted", wt.DWORD), ("dwWin32ExitCode", wt.DWORD),
                ("dwServiceSpecificExitCode", wt.DWORD), ("dwCheckPoint", wt.DWORD),
                ("dwWaitHint", wt.DWORD), ("dwProcessId", wt.DWORD),
                ("dwServiceFlags", wt.DWORD)]


def _advapi():
    """advapi32 句柄：按需初始化一次，签名一次写全。

    凡是传 HANDLE 的调用都必须先把 argtypes/restype 声明清楚 ——
    不声明时 ctypes 会按 C int 截断 64 位句柄（本项目的血泪教训）。
    """
    global _ADVAPI32
    if _ADVAPI32 is None:
        d = ctypes.WinDLL("advapi32", use_last_error=True)
        d.OpenSCManagerW.argtypes = [wt.LPCWSTR, wt.LPCWSTR, wt.DWORD]
        d.OpenSCManagerW.restype = ctypes.c_void_p
        d.OpenServiceW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, wt.DWORD]
        d.OpenServiceW.restype = ctypes.c_void_p
        d.QueryServiceStatusEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                           ctypes.c_void_p, wt.DWORD,
                                           ctypes.POINTER(wt.DWORD)]
        d.QueryServiceStatusEx.restype = wt.BOOL
        d.CloseServiceHandle.argtypes = [ctypes.c_void_p]
        d.CloseServiceHandle.restype = wt.BOOL
        # 改服务启动类型（经 SCM，绕开受保护的服务键 ACL，见 _svc_set_start_scm）
        d.ChangeServiceConfigW.argtypes = [
            ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.DWORD,
            wt.LPCWSTR, wt.LPCWSTR, ctypes.POINTER(wt.DWORD),
            wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR]
        d.ChangeServiceConfigW.restype = wt.BOOL
        _ADVAPI32 = d
    return _ADVAPI32


def service_state(name):
    """服务当前状态码（1 停止 / 2 启动中 / 3 停止中 / 4 运行 / 7 暂停），失败 None。"""
    if not name:
        return None
    d = _advapi()
    hscm = d.OpenSCManagerW(None, None, _SC_MANAGER_CONNECT)
    if _bad_handle(hscm):
        return None
    try:
        hs = d.OpenServiceW(hscm, str(name), _SERVICE_QUERY_STATUS)
        if _bad_handle(hs):
            return None
        try:
            buf = _SERVICE_STATUS_PROCESS()
            need = wt.DWORD()
            if not d.QueryServiceStatusEx(hs, 0, ctypes.byref(buf),
                                          ctypes.sizeof(buf), ctypes.byref(need)):
                return None
            return int(buf.dwCurrentState)
        finally:
            d.CloseServiceHandle(hs)
    finally:
        d.CloseServiceHandle(hscm)


def service_state_text(name):
    """服务状态的文字形式（查不到返回空串）。"""
    st = service_state(name)
    return _SVC_STATE_TEXT.get(st, "未知") if st is not None else ""


def set_regedit_lastkey(hive, sub):
    """把 regedit 的「上次位置」写成目标键，下次启动 regedit 会展开到那里。

    前缀（中文系统「计算机」/ 英文系统「Computer」）从现有值里沿用 —— 硬编码
    「计算机\\」在英文系统上会定位不到。
    """
    if winreg is None:
        return False
    full = ("HKEY_LOCAL_MACHINE\\" if hive == "HKLM" else "HKEY_CURRENT_USER\\") + sub
    key = r"Software\Microsoft\Windows\CurrentVersion\Applets\Regedit"
    prefix = ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            cur, _t = winreg.QueryValueEx(k, "LastKey")
            if isinstance(cur, str) and "\\" in cur:
                prefix = cur.split("\\", 1)[0] + "\\"
    except OSError:
        pass
    try:
        k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE)
    except OSError:
        return False
    try:
        winreg.SetValueEx(k, "LastKey", 0, winreg.REG_SZ, prefix + full)
    except OSError:
        return False
    finally:
        winreg.CloseKey(k)
    return True


def system_uses_dark():
    """系统「应用主题」是不是深色（True/False；读不到返回 None）。

    `AppsUseLightTheme` = 0 表示深色。设置页选「跟随系统」时以它为兜底
    —— Qt 的 colorScheme 在个别环境下拿不到值，注册表总是有的。
    """
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            v, _t = winreg.QueryValueEx(k, "AppsUseLightTheme")
        return int(v) == 0
    except (OSError, ValueError, TypeError):
        return None


# --------------------------------------------------------------------------
# UWP / 商店应用（先列清单、手动勾选，再逐个卸载）
#
# 为什么不做一键全删：那会连「商店 / winget
# 宿主 / 开始菜单宿主 / 运行时框架」一起卸载，删完开始菜单与搜索就废了。
# 这里改成：列清单 → 手动勾选 → 列出来确认 → 逐个卸载 → 回读复核。
# --------------------------------------------------------------------------
# 下面这些包绝对动不得（删了会直接坏掉商店 / 开始菜单 / 搜索 / 运行时）
UWP_PROTECTED = (
    "Microsoft.WindowsStore",
    "Microsoft.DesktopAppInstaller",          # winget 本体
    "Microsoft.Windows.ShellExperienceHost",
    "Microsoft.Windows.StartMenuExperienceHost",
    "Microsoft.Windows.CloudExperienceHost",
    "Microsoft.Windows.Search",
    "Microsoft.Windows.Cortana",
    "MicrosoftWindows.Client",                # Win11 外壳（CBS / WebExperience）
    "Microsoft.AAD.BrokerPlugin",
    "Microsoft.AccountsControl",
    "Microsoft.UI.Xaml",
    "Microsoft.VCLibs",
    "Microsoft.NET.Native",
    "Microsoft.WindowsAppRuntime",
    "Microsoft.Services.Store.Engagement",
    "Microsoft.StorePurchaseApp",
    "Microsoft.Windows.SecureAssessmentBrowser",
    "Microsoft.Windows.ParentalControls",
    "Microsoft.Windows.SecHealthUI",
    "Microsoft.Win32WebViewHost",
)
_UWP_CACHE = {"t": 0.0, "rows": []}
_UWP_LOCK = threading.Lock()
_UWP_TTL = 120


def _uwp_protected(name):
    low = (name or "").strip().lower()
    return any(low == p.lower() or low.startswith(p.lower() + ".")
               for p in UWP_PROTECTED)


def list_uwp_apps(force=False):
    """列出当前用户的 UWP / 商店应用。

    两次 PowerShell：`Get-StartApps` 拿开始菜单里的友好名称（AppID 形如
    `<PackageFamilyName>!<AppId>`，正好能对上包），`Get-AppxPackage` 拿包清单。
    结果缓存 120 秒 —— 界面上还有搜索与筛选，不必每次重查。
    """
    with _UWP_LOCK:
        if not force and _UWP_CACHE["rows"] and time.time() - _UWP_CACHE["t"] < _UWP_TTL:
            return _UWP_CACHE["rows"]
    friendly = {}
    try:
        _rc, out = ps("Get-StartApps | ForEach-Object { '{0}|||{1}' -f $_.Name,$_.AppID }",
                      timeout=90)
        for line in (out or "").splitlines():
            parts = line.strip().split("|||")
            if len(parts) >= 2 and "!" in parts[1]:
                fam = parts[1].split("!", 1)[0].strip()
                if fam and fam not in friendly:
                    friendly[fam] = parts[0].strip()
    except Exception:
        pass
    script = ("Get-AppxPackage | ForEach-Object { "
              "'{0}|||{1}|||{2}|||{3}|||{4}|||{5}|||{6}|||{7}' -f "
              "$_.Name,$_.PackageFullName,$_.PackageFamilyName,$_.Version,$_.Publisher,"
              "$_.InstallLocation,[int]($_.IsFramework -eq $true),"
              "[int]($_.NonRemovable -eq $true) }")
    rc, out = ps(script, timeout=180)
    rows = []
    for line in (out or "").splitlines():
        p = [x.strip() for x in line.strip().split("|||")]
        if len(p) < 8 or not p[0]:
            continue
        pub = p[4]
        rows.append({
            "name": p[0], "full": p[1], "family": p[2], "version": p[3],
            "publisher": (pub.split(",")[0].replace("CN=", "").strip() or pub),
            "publisher_raw": pub, "location": p[5],
            "framework": p[6] == "1", "non_removable": p[7] == "1",
            "title": friendly.get(p[2]) or p[0],
            "protected": _uwp_protected(p[0]) or p[7] == "1",
        })
    rows.sort(key=lambda x: (x["protected"], x["framework"], x["name"].lower()))
    with _UWP_LOCK:
        _UWP_CACHE["t"] = time.time()
        _UWP_CACHE["rows"] = rows
    return rows


def remove_uwp_apps(items, provisioned=False):
    """卸载选中的 UWP 应用。

    `items` —— [{'full','family','name','title'}]，`full` 是 PackageFullName。
    `provisioned` —— 同时从**系统预置镜像**里移除（新建用户不再自动装上），需管理员。
    结束后会重新枚举一次做**回读复核** —— 只看命令返回码是不够的（本项目的规矩）。
    """
    packs = [x for x in (items or []) if x.get("full")]
    if not packs:
        return {"ok": False, "msg": "没有选中任何应用", "done": [], "failed": [],
                "prov_done": [], "prov_failed": [], "still": []}
    arr = ",".join("'%s'" % str(x["full"]).replace("'", "''") for x in packs)
    script = ("$pkgs = @(%s); foreach ($p in $pkgs) { try { "
              "Remove-AppxPackage -Package $p -ErrorAction Stop; "
              "Write-Output ('OK,' + $p) } catch { "
              "Write-Output ('FAIL,' + $p + ',' + $_.Exception.Message) } }" % arr)
    rc, out = ps(script, timeout=900)
    done, failed = [], []
    for line in (out or "").splitlines():
        parts = line.strip().split(",", 2)
        if len(parts) < 2:
            continue
        if parts[0] == "OK":
            done.append(parts[1])
        elif parts[0] == "FAIL":
            failed.append({"package": parts[1],
                           "err": (parts[2] if len(parts) > 2 else "").strip()[:200]})

    prov_done, prov_failed = [], []
    if provisioned:
        fams = [x["family"] for x in packs if x.get("family")]
        if fams:
            arr2 = ",".join("'%s'" % str(f).replace("'", "''") for f in fams)
            script2 = ("$names = @(%s); foreach ($n in $names) { try { "
                       "Remove-AppxProvisionedPackage -Online -PackageName $n "
                       "-ErrorAction Stop | Out-Null; Write-Output ('OK,' + $n) } catch { "
                       "Write-Output ('FAIL,' + $n + ',' + $_.Exception.Message) } }" % arr2)
            _rc2, out2 = ps(script2, timeout=900)
            for line in (out2 or "").splitlines():
                parts = line.strip().split(",", 2)
                if len(parts) < 2:
                    continue
                if parts[0] == "OK":
                    prov_done.append(parts[1])
                elif parts[0] == "FAIL":
                    prov_failed.append({"package": parts[1],
                                        "err": (parts[2] if len(parts) > 2 else "").strip()[:200]})

    left = {r["full"] for r in list_uwp_apps(force=True)}
    still = [x["full"] for x in packs if x["full"] in left]
    msg = "已卸载 %d 个应用" % len(done)
    if prov_done:
        msg += "，并从系统预置中移除 %d 个" % len(prov_done)
    if failed:
        msg += "；%d 个失败" % len(failed)
    if still:
        msg = "未全部完成：成功 %d 个，仍有 %d 个留在系统里" % (len(done), len(still))
    return {"ok": not still, "done": done, "failed": failed, "prov_done": prov_done,
            "prov_failed": prov_failed, "still": still, "msg": msg}


def reinstall_store_apps():
    """重装（重新注册）全部商店应用。

    给「应用打不开 / 开始菜单点不动 / 图标全丢」这类损坏当修复手段：对每个
    Appx 包用 Add-AppxPackage -Register 重注册（不重新下载，只是重建注册）。
    几十秒到几分钟；框架包与受保护包失败属正常，按计数如实返回。
    """
    script = ("$ErrorActionPreference='SilentlyContinue';"
              "$ok=0;$fail=0;"
              "Get-AppxPackage -AllUsers | ForEach-Object {"
              "$m = Join-Path $_.InstallLocation 'AppxManifest.xml';"
              "if ($m -and (Test-Path $m)) { "
              "if (Add-AppxPackage -DisableDevelopmentMode -Register $m) { $ok++ } else { $fail++ } } };"
              "Write-Output ('RESULT,' + $ok + ',' + $fail)")
    rc, out = ps(script, timeout=1800)
    m = re.search(r"RESULT,(\d+),(\d+)", out or "")
    ok_n = int(m.group(1)) if m else 0
    fail_n = int(m.group(2)) if m else 0
    if m is None:
        return {"ok": False, "reinstalled": 0, "failed": 0,
                "msg": "重装命令没有返回结果（PowerShell 执行失败）"}
    return {"ok": True, "reinstalled": ok_n, "failed": fail_n,
            "msg": "已重新注册 %d 个商店应用%s"
                   % (ok_n, ("，%d 个失败（框架/受保护包属正常）" % fail_n) if fail_n else "")}


def service_status(name):
    """查一个服务当前是不是在运行（True/False/None）。

    早期版本想用 `sc query`，但那要起一个外部进程（还被部分安全策略拦），
    而且解析命令行输出本身就脆。这里直接用 advapi32：
    `OpenSCManager` → `OpenService` → `QueryServiceStatusEx`，几毫秒、无副作用。
    """
    st = service_state(name)
    return None if st is None else (st == 4)


def list_startup():
    """列出全部开机启动项（含已被禁用的）。

    相比旧版多了三项信息：
      * enabled   —— 从 StartupApproved 读出的真实状态，禁用项不再"消失"
      * publisher —— 可执行文件的公司名（任务管理器里的「发布者」）
      * service   —— 能对应上的服务名，供右键「定位到服务」用
    """
    items = []
    if winreg is None:
        return items
    for hive, sub in STARTUP_LOCATIONS:
        root = winreg.HKEY_LOCAL_MACHINE if hive == "HKLM" else winreg.HKEY_CURRENT_USER
        kind = _APPR_KIND.get(sub)
        runonce = sub.lower().endswith("runonce")
        try:
            with winreg.OpenKey(root, sub, 0, winreg.KEY_READ) as k:
                n = winreg.QueryInfoKey(k)[1]
                for i in range(n):
                    try:
                        nm, val, _ = winreg.EnumValue(k, i)
                    except OSError:
                        continue
                    exe = _exe_from_path(val)
                    state = None if runonce else _approved_state(kind, nm)
                    items.append({"hive": hive, "path": sub, "name": nm,
                                  "cmd": val, "exe": exe,
                                  "kind": "runonce" if runonce else kind,
                                  "enabled": True if state is None else state,
                                  "managed": not runonce, "folder": False,
                                  "publisher": file_publisher(exe),
                                  "service": find_service_for(nm, exe)})
        except FileNotFoundError:
            continue
    folders = [
        (os.path.join(os.environ.get("APPDATA", ""),
                      r"Microsoft\Windows\Start Menu\Programs\Startup"), "HKCU"),
        (os.path.join(os.environ.get("PROGRAMDATA", ""),
                      r"Microsoft\Windows\Start Menu\Programs\Startup"), "HKLM"),
    ]
    for fd, fhive in folders:
        if not os.path.isdir(fd):
            continue
        for f in os.listdir(fd):
            full = os.path.join(fd, f)
            state = _approved_state("folder", f)
            items.append({"hive": fhive, "path": fd, "name": f,
                          "cmd": full, "exe": full if os.path.isfile(full) else "",
                          "kind": "folder", "managed": True, "folder": True,
                          "enabled": True if state is None else state,
                          "publisher": file_publisher(full),
                          "service": find_service_for(os.path.splitext(f)[0], full)})
    return items


def list_services():
    """Single CIM query — much faster than one Get-CimInstance per service."""
    rc, out = ps("Get-CimInstance Win32_Service | ForEach-Object { "
                 "'{0}|||{1}|||{2}|||{3}|||{4}' -f $_.Name,$_.DisplayName,$_.State,"
                 "$_.StartMode,$_.PathName }",
                 timeout=120)
    res = []
    for line in out.splitlines():
        p = line.strip().split("|||")
        if len(p) >= 4 and p[0]:
            res.append({"name": p[0], "display": p[1], "status": p[2],
                        "start": {"Auto": "自动", "Manual": "手动",
                                  "Disabled": "禁用"}.get(p[3], p[3]),
                        "exe": (p[4].strip() if len(p) > 4 else "")})
    res.sort(key=lambda x: (x["start"] != "禁用", x["name"].lower()))
    return res


def _svc_start_value(name):
    """读服务的启动类型（注册表 Start：2=自动 3=手动 4=禁用），读不到 None。"""
    if winreg is None or not name:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Services\%s" % name, 0,
                            winreg.KEY_READ) as k:
            return int(winreg.QueryValueEx(k, "Start")[0])
    except (OSError, ValueError, TypeError):
        return None


def set_service(name, action):
    """启停服务 / 改启动类型。

    执行后**读回复核**：启动类型看注册表 `Start`、启停看当前运行状态。
    只看 `sc` 的返回码是不够的 —— 被组策略接管、服务不存在、权限不足时
    都可能返回 0 却没真的变（界面上就成了"点了没用还说成功"）。
    """
    if action == "stop":
        rc, o = run(["sc", "stop", name])
        got = service_state(name)
        ok = rc == 0 and got in (None, 1, 3)      # 1=已停止 3=正在停止
        note = "已停止" if ok else "命令返回成功，但服务仍在运行"
    elif action == "start":
        rc, o = run(["sc", "start", name])
        got = service_state(name)
        ok = rc == 0 and got in (None, 2, 4)      # 2=正在启动 4=运行中
        note = "已启动" if ok else "命令返回成功，但服务仍未运行"
    else:
        cfg = {"disable": (4, "disabled", "已禁用"),
               "auto": (2, "auto", "已改为自动"),
               "manual": (3, "demand", "已改为手动")}.get(action)
        if cfg is None:
            return False, "unknown action"
        want_start, sc_arg, note = cfg
        rc, o = run(["sc", "config", name, "start=", sc_arg])
        got = _svc_start_value(name)
        ok = rc == 0 and got in (None, want_start)
        if rc == 0 and got is not None and got != want_start:
            note = "命令返回成功，但启动类型没变（可能被组策略接管）"
    if ok:
        # 只记「禁用」这一类持久状态：stop/start 是临时动作，改回自动/手动就是不禁了
        if action == "disable":
            disabled_record_add("service", name=name)
        elif action in ("auto", "manual"):
            disabled_record_del("service", name=name)
        return True, note
    return False, (note + "；" + (o or "").strip()[:160]).strip("；")


def list_tasks():
    """计划任务（任务计划程序）：名称 / 路径 / 状态 / 执行程序（供显示图标）。"""
    rc, out = ps("Get-ScheduledTask | ForEach-Object { "
                 "$a=$_.Actions|Select-Object -First 1;"
                 "'{0}|||{1}|||{2}|||{3}' -f $_.TaskName,$_.TaskPath,$_.State,"
                 "$(if($a){[string]$a.Execute}else{''}) }",
                 timeout=120)
    state_cn = {"Ready": "就绪", "Running": "运行中", "Disabled": "已禁用",
                "Queued": "排队中", "Unknown": "未知"}
    res = []
    for line in (out or "").splitlines():
        p = line.strip().split("|||")
        if len(p) >= 3 and p[0]:
            res.append({"name": p[0], "path": p[1] or "\\",
                        "state": state_cn.get(p[2], p[2] or "未知"),
                        "exe": (p[3].strip() if len(p) > 3 else "")})
    # 已禁用的排后面，其余按名称排
    res.sort(key=lambda x: (x["state"] == "已禁用", x["name"].lower()))
    return res


def set_task(full_name, enable):
    """启用 / 禁用计划任务。full_name 形如 \\Microsoft\\Windows\\...\\任务名。

    ⚠️ 原来走 `schtasks /change`，**同样被安全策略拦**（rc 恒 2）——
    界面却只看 `rc == 0`，于是会弹「已禁用」而实际什么都没做
    （与「已修好但 reg.exe 被拦」同一类错误）。
    改走 `Enable-/Disable-ScheduledTask`，并**回读复核**。
    """
    path, tname = _task_split(full_name)
    if not tname:
        return False, "任务名无效"
    verb = "Enable" if enable else "Disable"
    st, why = _rp_run(
        "$ErrorActionPreference='Stop';"
        "try {"
        "  %s-ScheduledTask -TaskName '%s' -TaskPath '%s' -ErrorAction Stop | "
        "Out-Null; '%s'"
        "} catch { '%s' + $_.Exception.Message }"
        % (verb, tname.replace("'", "''"), path.replace("'", "''"),
           _SRP_OK, _SRP_ERR))
    if st != "ok":
        return False, (why or "%s 失败（需要管理员权限）" % verb)
    # 回读：只信「读回来的状态」，不信「命令返回成功」
    path2, tname2 = _task_split(full_name)
    rc, out = ps("Get-ScheduledTask -TaskName '%s' -TaskPath '%s' "
                 "-ErrorAction SilentlyContinue | ForEach-Object { $_.State }"
                 % (tname2.replace("'", "''"), path2.replace("'", "''")),
                 timeout=90)
    state = (out or "").strip().splitlines()
    state = state[0].strip() if state else ""
    if enable and state.lower() == "disabled":
        return False, "命令已执行，但回读仍是「已禁用」（状态：%s）" % (state or "?")
    if not enable and state.lower() not in ("disabled", ""):
        return False, "命令已执行，但回读状态是「%s」，未变成已禁用" % (state or "?")
    # 记一笔（Defender 自己那 4 个任务除外 —— 它们由 Defender 的快照逻辑管）
    if any(str(full_name).startswith(p) for p in _REC_SKIP_TASKS):
        pass
    elif enable:
        disabled_record_del("task", full=full_name)
    else:
        disabled_record_add("task", full=full_name)
    return True, "已%s" % ("启用" if enable else "禁用")


def _svc_start(name):
    """Return the 'Start' DWORD of a service, or None if not readable."""
    ex, typ, data = reg_read("HKLM\\SYSTEM\\CurrentControlSet\\Services\\" + name, "Start")
    if ex and data is not None:
        try:
            return int(data)
        except Exception:
            return None
    return None


def _svc_set_start_scm(name, start):
    """经 SCM（`ChangeServiceConfig`）改服务启动类型。返回 (ok, 说明)。

    ⚠️ **为什么还需要这条路**：`SecurityHealthService` 的服务键 ACL 只给
    `Administrators` **ReadKey**（Owner 是 SYSTEM、没有 Administrators 的写
    权限），于是 `reg_write(..., "Start", ...)` 连管理员身份也会 Access Denied
    —— 这就是「全关 Defender 时要先手动处理的那个前置服务」。
    `ChangeServiceConfig` 让 **SCM 以 SYSTEM 身份**去写，绕开该键的 ACL。

    与「改安全描述符 / 取得所有权」相比，这条路的优点：**不修改任何 ACL**，
    只在服务本身上改一个启动类型 —— 不会留下更宽松的权限（改 SD 是
    单向的、容易忘了收回来）。
    """
    d = _advapi()
    hscm = d.OpenSCManagerW(None, None, _SC_MANAGER_CONNECT)
    if _bad_handle(hscm):
        return False, "打不开服务控制管理器（需要管理员权限）"
    try:
        hs = d.OpenServiceW(hscm, str(name), _SERVICE_CHANGE_CONFIG)
        if _bad_handle(hs):
            return False, "打开服务 %s 失败（需要管理员权限）" % name
        try:
            if not d.ChangeServiceConfigW(
                    hs, _SERVICE_NO_CHANGE, int(start), _SERVICE_NO_CHANGE,
                    None, None, None, None, None, None, None):
                return False, "改启动类型被拒绝（WinError %s）" \
                              % ctypes.get_last_error()
        finally:
            d.CloseServiceHandle(hs)
    finally:
        d.CloseServiceHandle(hscm)
    return True, "Start=%d（经 SCM）" % int(start)


def _svc_set_start(name, start):
    """写服务 Start 值（0/1/2=自动 3=手动 4=禁用）。返回 (ok, 说明)。

    ⚠️ **不能用 `reg.exe`**：本机安全策略把它放进 Program Blacklist
    （`run(["reg", "add", ...])` 直接返回 rc=-1），而旧代码只看 rc==0
    就当写成功 → 界面报「已暂停更新」而一个字节都没写进去。

    两条写入路径，**都回读复核**：
      ① `winreg` 直写服务键（最快，覆盖绝大多数服务）
      ② ① 报权限错误时退回 `advapi32!ChangeServiceConfig` —— 有些服务键的
         ACL 不给 Administrators 写权限（`SecurityHealthService` 就是），
         只有经 SCM 才改得动。见 `_svc_set_start_scm`。
    """
    via_scm = False
    try:
        reg_write("HKLM\\SYSTEM\\CurrentControlSet\\Services\\" + name,
                  "Start", "REG_DWORD", int(start))
    except PermissionError:
        ok, why = _svc_set_start_scm(name, int(start))
        if not ok:
            return False, why + "；winreg 直写也被拒（需要管理员权限）"
        via_scm = True
    except OSError as e:
        return False, "写入失败：%s" % str(e)[:50]
    got = _svc_start(name)
    if got is None:
        return False, "写入后读不回 Start 值（读取失败）"
    if got != int(start):
        return False, "写入未生效（读回 %s，期望 %s）" % (got, start)
    return True, "Start=%d%s" % (int(start), "（经 SCM）" if via_scm else "")


# (服务名, 恢复「更新」时应写回的默认值)
# ⚠️ 2026-10-08 起：「暂停更新」**不再碰这三个服务**（只写 Pause*EndTime）。
#    下面这套默认值只在「恢复更新」时用 —— 那是为了收拾旧版本 / 第三方工具
#    可能留下的禁用状态，让恢复后真的收得到更新。
# ⚠️ 两个默认值都踩过坑：
#   · wuauserv 的出厂默认是 **3（手动 / 触发器启动）**，不是 2（自动）——
#     Win10 1709 起它是按需拉起的触发式服务（官方文档与多方资料口径一致：
#     "Windows Update commonly uses 3 for manual trigger start"）。写成 2 会把
#     「恢复更新」调成自动启动，与系统默认不一致。
#   · WaaSMedicSvc 默认也是 3（手动），早先写成 4（禁用）→ 恢复后更新组件修不好。
_UPDATE_SVCS = (("wuauserv", 3), ("UsoSvc", 2), ("WaaSMedicSvc", 3))
_UPD_POL_KEY = "HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate"
_UPD_AU_KEY = _UPD_POL_KEY + "\\AU"
_UPD_UX_KEY = "HKLM\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings"
# 判断「是否仍在暂停」只看这三个**到期时间**
_PAUSE_END_KEYS = ("PauseUpdatesExpiryTime", "PauseFeatureUpdatesEndTime",
                   "PauseQualityUpdatesEndTime")
# 「恢复更新」时要清掉的暂停标记（含 StartTime，留着会让系统认为暂停过）
_PAUSE_ALL_KEYS = _PAUSE_END_KEYS + ("PauseUpdatesStartTime",
                                     "PauseFeatureUpdatesStartTime",
                                     "PauseQualityUpdatesStartTime")


def _reg_dword(key, name):
    """读一个 DWORD，读不到返回 None（不要和「值为 0」混淆）。"""
    e, _t, d = reg_read(key, name)
    if not e or d is None:
        return None
    try:
        return int(d)
    except (TypeError, ValueError):
        return None


def _reg_str(key, name):
    e, _t, d = reg_read(key, name)
    return "" if (not e or d is None) else str(d).strip()


def _parse_iso_utc(s):
    """'2026-11-07T04:57:21Z' → aware datetime(UTC)；解析失败返回 None。"""
    s = (s or "").strip()
    if not s:
        return None
    try:
        import datetime as _dt
        d = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d
    except (ValueError, TypeError):
        return None


def update_status():
    """Windows 更新是否**真的**能工作 —— 四层判定，不能只看服务。

    ⚠️ 只看 wuauserv 的 Start 值是错的（本机实测踩到）：wuauserv=2（看起来完全
    正常），但 `UX\\Settings\\PauseUpdatesExpiryTime = 2026-11-07`（未来时间）
    → 更新被**暂停**，根本收不到任何补丁，而旧实现却报「已开启」，把用户误导了。

    四层（任意一层命中都意味着更新用不了，全部写进 reasons 给 UI 直接显示）：
      服务层  wuauserv Start=4                    → 服务被禁用
      策略层  AU\\NoAutoUpdate=1                  → 策略关闭自动更新
              WindowsUpdate\\DisableWindowsUpdateAccess=1 → 关闭所有更新功能
      暂停层  PauseXxxExpiryTime/EndTime 在未来   → 更新被暂停
      接管层  AU\\UseWUServer=1                   → 走企业 WSUS 服务器

    `enabled` 只反映「服务与策略是否放行」（决定按钮该是开还是关）；
    暂停 / WSUS 属于「功能没被禁、但实际拿不到更新」，用 paused / managed
    单独返回，让界面能如实提示，又不把开关逻辑搅成多态。
    """
    import datetime as _dt
    start = _svc_start("wuauserv")
    svc_off = start is not None and int(start) == 4
    no_auto = _reg_dword(_UPD_AU_KEY, "NoAutoUpdate")
    no_access = _reg_dword(_UPD_POL_KEY, "DisableWindowsUpdateAccess")
    use_wsus = _reg_dword(_UPD_AU_KEY, "UseWUServer")

    # 暂停：取所有到期时间里**最晚**的那个，只要还在未来就是暂停中
    now = _dt.datetime.now(_dt.timezone.utc)
    until, paused = None, False
    for k in _PAUSE_END_KEYS:
        d = _parse_iso_utc(_reg_str(_UPD_UX_KEY, k))
        if d is None:
            continue
        if d > now:
            paused = True
        if until is None or d > until:
            until = d

    policy_off = (no_auto == 1) or (no_access == 1)
    reasons = []
    if svc_off:
        reasons.append("更新服务 wuauserv 被禁用")
    if no_auto == 1:
        reasons.append("策略 NoAutoUpdate=1 关闭了自动更新")
    if no_access == 1:
        reasons.append("策略 DisableWindowsUpdateAccess=1 关闭了所有更新功能")
    if paused:
        reasons.append("更新被暂停到 %s" % until.strftime("%Y-%m-%d"))
    if use_wsus == 1:
        reasons.append("更新由企业 WSUS 服务器接管")
    return {
        "enabled": None if start is None else not (svc_off or policy_off),
        "start": start,
        "policy_off": policy_off,
        "access_blocked": no_access == 1,
        "paused": paused,
        "paused_until": until.strftime("%Y-%m-%d") if (paused and until) else "",
        # 「暂停到 2099」= 本工具的「关闭」状态（2026-10-08 起）：
        # 服务没被禁、策略没写，只是把更新挂起很久 —— 界面要区别对待，
        # 因为恢复它只要点一下「恢复更新」，不需要重启。
        "paused_long": bool(paused and until and until.year >= 2099),
        "svc_disabled": svc_off,
        "managed": use_wsus == 1,
        "blocked": bool(svc_off or policy_off),
        "reasons": reasons,
        "aux": {n: _svc_start(n) for n, _a in _UPDATE_SVCS if n != "wuauserv"},
    }


def set_update(enabled):
    """一键开 / 关 Windows 更新。返回逐项结果，**每项都回读复核**。

    ## 「关闭」= 把更新**暂停到 2099 年**，不碰服务、不写禁止策略

    这是 2026-10-08 改的行为。原来是「禁用三个服务 + 写 NoAutoUpdate=1」，
    问题很实际：
      · 禁服务是**不可逆的副作用** —— `WaaSMedicSvc` 被拉起后还能自动改回，
        而用户根本不知道自己「关闭」过；别的软件（驱动更新器、企业的
        补丁基线检查）会看到「更新服务不存在」而报错。
      · 一旦禁了服务，「开启」就必须靠重新写 Start 值 + 重启才能恢复，
        中间那段时间用户是**裸奔**的（补丁完全收不到，且没有任何提示）。
      · 写 `NoAutoUpdate=1` 还会被企业策略 / 域环境接管冲突。

    改成「暂停」后：三个服务保持默认（`wuauserv=3` / `UsoSvc=2` /
    `WaaSMedicSvc=3`），只往 `UX\\Settings` 写三个 Pause*EndTime = 2099 年。
    这是 Windows 自己「设置 → Windows 更新 → 暂停更新」走的那条路 ——
    系统完全认可，「暂停更新」按钮会真的显示成暂停到 2099-12-31。
    要恢复只要清掉这几个值，**不需要重启**。

    ## 「开启」= 清掉暂停标记，并把可能残留的旧式禁用改回来

    「开启」这一侧**必须**同时做两件事，否则用户点了没反应：
      ① 清暂停标记（`PauseXxxEndTime` / `StartTime`）
      ② **恢复服务 Start 值 + 删 NoAutoUpdate** —— 因为本工具的旧版本
         （以及用户手改 / 第三方工具）可能已经禁用了服务或写了策略。
         只清暂停的话，服务还禁着，用户会以为「点了开启还是收不到更新」。

    ⚠️ 写入前先记下原值（`_UPD_BACKUP`）：用户点「开启」后仍然可以自己
    点「关闭」回到 2099，不需要恢复历史原值 —— 但把原值留一份在日志里，
    出问题时能查。
    """
    results = []
    # ⚠️ 显式转 bool：本函数是按真值分支的（`if not enabled` → 暂停），
    # 而 `None` / `0` / `""` 都是假值 → **会静默走到「暂停」那一支**。
    # 调用方万一传了个 None（比如从某个没取到值的配置里读出来），
    # 用户的更新就被悄悄暂停到 2099 了 —— 这种「参数没传对就改了系统状态」
    # 是最不该有的。转一下至少让意图明确。
    enabled = bool(enabled)
    # ---------- 关闭：暂停到 2099，不动服务 ----------
    if not enabled:
        import datetime as _dt
        end = _dt.datetime(2099, 12, 31, 23, 59, 59,
                           tzinfo=_dt.timezone.utc)
        # Windows 写的是 ISO8601 UTC（带 Z），Start/End 都要
        s_val = "2026-01-01T00:00:00Z"
        e_val = end.strftime("%Y-%m-%dT%H:%M:%SZ")
        for k in _PAUSE_END_KEYS:
            try:
                reg_write(_UPD_UX_KEY, k, "REG_SZ", e_val)
                back = _reg_str(_UPD_UX_KEY, k)
                results.append({"name": k, "want": "暂停到 2099-12-31",
                                "ok": back == e_val,
                                "note": "已写入并回读一致" if back == e_val
                                        else "写入后回读=%r" % (back or "（空）")[:40]})
            except OSError as e:
                results.append({"name": k, "ok": False,
                                "note": str(e)[:60]})
        # StartTime 也写上：系统据此判断「处于暂停中」，只写 EndTime
        # 在部分版本上会被当成过期而自动恢复更新。
        try:
            reg_write(_UPD_UX_KEY, "PauseUpdatesStartTime", "REG_SZ", s_val)
            results.append({"name": "PauseUpdatesStartTime",
                            "want": s_val, "ok": True, "note": "已写入"})
        except OSError as e:
            results.append({"name": "PauseUpdatesStartTime", "ok": False,
                            "note": str(e)[:60]})
        # 回读复核：让 UI 能确认「现在真的是暂停状态」
        st = update_status()
        results.append({
            "name": "复核", "want": "paused=True",
            "ok": bool(st.get("paused")),
            "note": ("已暂停到 %s，服务保持默认（未禁用）" % st.get("paused_until")
                     or "写入后仍读不到暂停标记")
                if st.get("paused") else
                ("写入后仍读不到暂停标记：%s" % "；".join(st.get("reasons") or [])[:60])})
        return results

    # ---------- 开启：清暂停 + 恢复服务与策略 ----------
    for name, auto in _UPDATE_SVCS:
        ok, note = _svc_set_start(name, auto)
        results.append({"name": name, "want": auto, "ok": ok, "note": note})
    try:
        reg_delete(_UPD_AU_KEY, "NoAutoUpdate")
        results.append({"name": "策略键 NoAutoUpdate", "want": "删除",
                        "ok": True, "note": "已删除"})
    except OSError as e:
        results.append({"name": "策略键 NoAutoUpdate", "ok": False,
                        "note": str(e)[:50]})
    cleared, failed = [], []
    for k in _PAUSE_ALL_KEYS:
        try:
            reg_delete(_UPD_UX_KEY, k)
            if _reg_str(_UPD_UX_KEY, k):
                failed.append(k)
            else:
                cleared.append(k)
        except OSError as e:
            failed.append("%s(%s)" % (k, str(e)[:24]))
    if cleared or failed:
        results.append({
            "name": "更新暂停标记", "want": "清除",
            "ok": not failed,
            "note": ("已清除 %d 项（暂停已解除）" % len(cleared)) if not failed
                    else "未清掉：%s" % ", ".join(failed[:3])})
    return results



# Defender 相关常量
_DEF_POL = "HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows Defender"
_DEF_RT_KEY = _DEF_POL + "\\Real-Time Protection"
# 「实时监控」开关的**策略**键（= 组策略「关闭实时保护」那一项）。
# ⚠️ 别用 `DisableAntiSpyware`：微软自 2020 起在客户端 SKU 上**已忽略**它，
#    写了也不生效（只对 Server 有效）。本工具早期版本就是写它，
#    等于「界面报已关闭、实际 Defender 照跑」。
_DEF_RT_VALUE = "DisableRealtimeMonitoring"
# (服务名, 恢复「开启」时应写回的默认值)
_DEF_SVCS = (("WinDefend", 2), ("WdNisSvc", 3), ("SecurityHealthService", 2))

# ---- Defender **全关闭**要动的完整清单（用户要求「全关闭」） ----
# 默认 Start 值来自官方/多方一致的口径（Boot/Auto/Manual），已交叉核对：
#   Sense=3  WdBoot=0  WdFilter=0  WdNisDrv=3  WdNisSvc=3  WinDefend=2
# 另外两项是断开「自动修复」链必需的：
#   SecurityHealthService（Windows 安全中心 UI，键 ACL 对管理员只读）
#   WaaSMedicSvc（更新医疗，会把改动**悄悄改回来** —— 与 wuauserv 那次同理）
# ⚠️ WdBoot / WdFilter 默认是 **0（Boot 启动）**，不是 3 —— 恢复时写错会让
#    Defender 的启动期驱动不加载。所以恢复优先用「关之前抓的快照」。
_DEF_COMPONENTS = (
    ("WinDefend", "服务", 2, "反病毒主服务（PPL 保护，运行中停不了）"),
    ("WdNisSvc", "服务", 3, "网络检查服务"),
    ("WdNisDrv", "驱动", 3, "网络检查驱动"),
    ("WdFilter", "驱动", 0, "实时防护的过滤驱动（Boot 启动）"),
    ("WdBoot", "驱动", 0, "启动期扫描驱动（Boot 启动）"),
    ("Sense", "服务", 3, "高级威胁防护 / EDR"),
    ("SecurityHealthService", "服务", 2, "Windows 安全中心（**键 ACL 只给管理员只读**）"),
    ("WaaSMedicSvc", "服务", 3, "更新医疗（不禁它会**把改动改回去**）"),
)
# ⚠️ 这些组件**会被 Windows 自己改回来**，不是本工具没写成：
#    WaaSMedicSvc（更新医疗）会定期把 Start 恢复成手动 —— 与 wuauserv 同理。
#    判定「Defender 是否已实际关闭」时必须把它排除在外，否则用户永远看到
#    「8 个里还差 1 个」，以为工具点了没用（用户 2026-10-12 就是这么反馈的）。
_DEF_SELF_HEAL = ("WaaSMedicSvc",)
_DEF_TASKS = (
    r"\Microsoft\Windows\Windows Defender\Windows Defender Cache Maintenance",
    r"\Microsoft\Windows\Windows Defender\Windows Defender Cleanup",
    r"\Microsoft\Windows\Windows Defender\Windows Defender Scheduled Scan",
    r"\Microsoft\Windows\Windows Defender\Windows Defender Verification",
)
_DEF_MAIN_KEY = "HKLM\\SOFTWARE\\Microsoft\\Windows Defender"
_DEF_SNAP = os.path.join(DATA, "defender_orig.json")


def defender_prereq_text():
    """给 UI 用的「全关闭 Defender 需要动什么」清单（用户明确要这份说明）。"""
    svc = "\n".join("· %s（%s）—— %s" % (n, k, d)
                    for n, k, _s, d in _DEF_COMPONENTS)
    return ("全关闭 Defender 要同时处理 8 个服务/驱动 + 4 个计划任务：\n"
            + svc +
            "\n· 4 个计划任务：Cache Maintenance / Cleanup /"
            " Scheduled Scan / Verification\n\n"
            "其中两个是「前置」：\n"
            "① SecurityHealthService —— 它的服务键 ACL 只给管理员只读，"
            "直接写注册表会被拒；本工具改走 SCM（ChangeServiceConfig）绕过，"
            "不改任何 ACL。\n"
            "② WaaSMedicSvc —— 不禁它，前面做的会被 Windows 自动改回来。\n\n"
            "⚠️ 所以**事后看到它「又变回手动」不是本工具没写成** —— 更新医疗会定期"
            "把它改回去，属正常现象。判定「到底关掉没」请看实时保护 / 防病毒的"
            "实际状态，那一项不受它影响。\n\n"
            "⚠️ 普通模式下 WinDefend 有 PPL 自我保护，改完**要重启**才生效；"
            "若改不动或重启后复活，需进**安全模式**再执行一次。")


def _mp_run(script, timeout=120):
    """跑一段 Defender 脚本，返回 `(成功?, 输出文本 或 失败原因)`。

    ⚠️ **不能直接用 `_rp_run`** —— 它只回「状态」把输出丢掉了，
    而 `Get-MpComputerStatus` 的**值**才是我们要的。所以这里自己解析：
    逐行找哨兵，非哨兵行即输出（哨兵行本身要剔除，否则会混进数据里）。

    哨兵仍用 `_SRP_OK` / `_SRP_ERR` 互斥（同 `_rp_run` 的教训：
    PowerShell 出错时退出码仍是 0，不能只看 rc）。
    """
    rc, out = ps("$ErrorActionPreference='Stop';"
                 "try { %s; '%s' } catch { '%s' + $_.Exception.Message }"
                 % (script, _SRP_OK, _SRP_ERR), timeout=timeout)
    lines = (out or "").splitlines()
    if not any(x.strip() == _SRP_OK for x in lines):
        for x in lines:
            if x.strip().startswith(_SRP_ERR):
                return False, _rp_zh(x.strip()[len(_SRP_ERR):].strip())[:180]
        return False, _rp_first_error(out) or "PowerShell 没有返回结果"
    data = [x for x in lines
            if x.strip() != _SRP_OK and not x.strip().startswith(_SRP_ERR)]
    return True, "\n".join(data).strip()


def _mp_ok(script):
    """只关心成功与否时用它（如 `Set-MpPreference`）。"""
    ok, _why = _mp_run(script)
    return ok, _why


# Windows 安全中心「病毒和威胁防护 **设置**」的 URI —— 实时保护与篡改防护的
# 开关就在那一页。3 个独立来源口径一致（winhelponline / positioniseverything /
# pupuweb）：`windowsdefender://threatsettings`（`//threat` 只是仪表板那一页）。
DEFENDER_SETTINGS_URI = "windowsdefender://threatsettings"
# 兜底：走「设置」App 的 Windows 安全中心入口（要多点一下，但总能到）
DEFENDER_SETTINGS_FALLBACK = "ms-settings:windowsdefender"


def open_defender_settings():
    """打开「病毒和威胁防护」设置页（实时保护 / 篡改防护 在那里）。返回 (ok, 说明)。

    ⚠️ 这些 URI 要**安全中心服务（SecurityHealthService）在跑**才有效 ——
    而本工具的「全关闭」正是把它禁掉。所以**关完之后这个链接可能打不开**
    （系统会弹「你需要新的应用来打开此链接」）。因此：
      · 有 `ms-settings:` 兜底；
      · 两个都失败时**如实告诉用户手动去哪里开**，不假装成功。
    """
    for uri in (DEFENDER_SETTINGS_URI, DEFENDER_SETTINGS_FALLBACK):
        try:
            os.startfile(uri)               # noqa: S606  Windows 专有
            return True, uri
        except OSError:
            continue
    return False, ("打不开 Windows 安全中心 —— 安全中心服务可能已被禁用。"
                   "请手动打开：开始菜单搜索「Windows 安全中心」→"
                   "「病毒和威胁防护」→「管理设置」。")


def defender_status():
    """Defender 状态 —— **实时保护 + 全关闭清单的禁用进度**。

    ⚠️ 只看「服务在不在」或只看「实时保护」都不够：
      · 服务在跑、实时保护被单独关掉是常见状态 → 要看 `RealTimeProtectionEnabled`
      · 「全关闭」是 8 个组件的事，只报一个总数用户看不出还差哪几个
        → 另外返回 `disabled`（已禁用的组件名）与 `remaining`（没禁掉的）。

    另外两个**给用户看的结论字段**（用户 2026-10-12 反馈「关了 Defender 但看起来没生效」）：
      · `effectively_off` —— 实时保护与防病毒是否**都确实关着**（三态，读不到为 None）
      · `self_heal` / `sticky_remaining` —— 把「Windows 会自己改回」的组件
        （`_DEF_SELF_HEAL`）与「真的没禁成」的分开，界面才能说实话。
    """
    ok, out = _mp_run(
        "$s = Get-MpComputerStatus; "
        "'{0}|{1}|{2}' -f $s.RealTimeProtectionEnabled,"
        "$s.AntivirusEnabled,$s.IsTamperProtected")
    start = _svc_start("WinDefend")
    # 全关闭进度：逐个读 Start，4 = 已禁用
    starts = {n: _svc_start(n) for n, _k, _d, _x in _DEF_COMPONENTS}
    disabled = [n for n, v in starts.items() if v == 4]
    remaining = [n for n, v in starts.items() if v != 4]
    # ⚠️ 有些组件**会被 Windows 自己改回**（WaaSMedicSvc 由更新医疗定期恢复成手动），
    #    把它们和「真的没禁成」混在一句里报，用户会永远看到「8 个里还差 1 个」，
    #    以为工具没生效。这里分开算，并给出「是否已实际关闭」的结论字段。
    self_heal = [n for n in remaining if n in _DEF_SELF_HEAL]
    sticky = [n for n in remaining if n not in _DEF_SELF_HEAL]
    base = {"start": start, "svc_starts": starts,
            "disabled": disabled, "remaining": remaining,
            "self_heal": self_heal, "sticky_remaining": sticky,
            "n_disabled": len(disabled), "n_total": len(_DEF_COMPONENTS)}
    if not ok:
        # 读不到（Defender 被移除 / 第三方杀软接管 / 权限不足）→
        # 退回「服务是否禁用」，并如实标注这是退化的判据。
        base.update({"enabled": None if start is None else int(start) < 4,
                     "realtime": None, "tamper": None,
                     # 读不到实时保护 → 无法断言「已实际关闭」（三态，别退化成默认值）
                     "effectively_off": None,
                     "note": out or "读不到 Defender 状态"})
        return base
    parts = (out or "").strip().splitlines()
    parts = parts[-1].split("|") if parts else []

    def _b(x):
        return x.strip().lower() == "true"

    rt = _b(parts[0]) if parts and parts[0].strip() else None
    av = _b(parts[1]) if len(parts) > 1 and parts[1].strip() else None
    tp = _b(parts[2]) if len(parts) > 2 and parts[2].strip() else None
    # 「已实际关闭」= 实时保护与防病毒**都确实关着**。篡改防护是防「改设置」的
    # 开关，它开着不代表防护还在（本机实测：实时/防病毒已关、篡改防护 False）。
    # 两者都关 → Defender 实际上已经不起作用了，这才是用户要看的结论。
    base.update({"enabled": rt, "realtime": rt, "antivirus": av,
                 "tamper": tp, "note": "",
                 "effectively_off": (rt is False) and (av is not True)})
    return base


def _def_snapshot_save():
    """把「关之前」的 Start 值存下来 —— 恢复时按它写回，别猜默认值。

    ⚠️ 两个坑：
      ① 为什么不直接用默认值表：`WdBoot`/`WdFilter` 默认是 **0（Boot 启动）**，
         不是 3；写错会让 Defender 的启动期驱动不加载。快照才是可靠来源。
      ② **值为 4（已禁用）的不记**：用户可能在本工具之前就手工关过 Defender
         （本机就是），那时快照记下的是「已禁用」—— 恢复时写回 4 等于
         **「点了恢复却什么都没恢复」而且是静默的**。不记 4，恢复就会退回
         默认值表（并在界面上说明「是按默认值恢复的」）。
    **只在还没有快照时写**：反复点「关闭」不能把禁用后的值当原始值存进去。
    """
    if os.path.exists(_DEF_SNAP):
        return False, 0
    kept, skipped = {}, []
    for n, _k, _d, _x in _DEF_COMPONENTS:
        v = _svc_start(n)
        if v is None:
            continue
        if int(v) == 4:
            skipped.append(n)      # 已经是禁用的，不作为「原值」
        else:
            kept[n] = int(v)
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(_DEF_SNAP, "w", encoding="utf-8") as f:
            json.dump({"svcs": kept, "skipped_disabled": skipped,
                       "ts": time.strftime("%Y-%m-%d %H:%M:%S")},
                      f, ensure_ascii=False, indent=2)
        return True, len(skipped)
    except OSError:
        return False, 0


def _def_snapshot_restore_values():
    """读快照；没有/损坏就退回默认值表（并让调用方注明是猜的）。

    返回 `(名→Start, 是否是猜的, 说明)`。**值为 4 的一律不采用**（见
    `_def_snapshot_save` 的第 ② 条），避免「恢复」把禁用值写回去。
    """
    svcs, note = {}, ""
    try:
        with open(_DEF_SNAP, encoding="utf-8") as f:
            d = json.load(f) or {}
        svcs = d.get("svcs") or {}
        skipped = d.get("skipped_disabled") or []
        if skipped:
            note = ("快照里 %d 个组件当时已是禁用状态（你之前手工关过），"
                    "这些按 Windows 默认值恢复" % len(skipped))
    except (OSError, ValueError):
        svcs, note = {}, "没有找到关闭前的快照"
    out, guessed = {}, not svcs
    for n, _k, dflt, _x in _DEF_COMPONENTS:
        v = svcs.get(n)
        if v is None or int(v) == 4:
            out[n] = dflt
            guessed = True
        else:
            out[n] = int(v)
    return out, guessed, note


def set_defender(enabled, progress=None):
    """一键**全开 / 全关** Microsoft Defender。返回逐项结果，**每项都回读复核**。

    `progress`（可选回调，签名 `progress(str)`）用于**上报进度**：
    全关闭要写 8 个服务/驱动 + 4 个策略键 + 4 个计划任务，逐个回读复核要好几秒 ——
    不报进度用户会以为「点了没反应」。它**只上报、绝不影响流程**，
    回调抛异常也被吞掉（进度是附加品，不能让它影响主流程）。
    ⚠️ 回调在**后台线程**里执行，实现方不得直接碰 Qt 控件。

    ## 「关闭」= 全关闭（用户 2026-10-09 明确要求）

    之前一版只关「实时保护」不动服务；用户要求改回**全关闭**，并要工具把
    「需要动哪些必要服务」说清楚（他早先手工关过，已记不得是哪几个了）。
    完整清单见 `_DEF_COMPONENTS` / `defender_prereq_text()`：

      · 8 个服务/驱动 → Start=4（含 **SecurityHealthService**、**WaaSMedicSvc**）
      · 策略键 `DisableAntiSpyware=1` + `Real-Time Protection\\DisableRealtimeMonitoring=1`
      · `HKLM\\SOFTWARE\\Microsoft\\Windows Defender\\DisableAntiSpyware=1`
      · 4 个 Windows Defender 计划任务 → 禁用

    两个「前置」必须一起处理，否则关不干净：
      ① **SecurityHealthService**：服务键 ACL 只给管理员 ReadKey，
         直接写注册表会被拒 → 本工具经 **SCM（ChangeServiceConfig）** 写。
      ② **WaaSMedicSvc**：不禁它，Windows 会把前面的改动**悄悄改回来**。

    ⚠️ **普通模式下 WinDefend 是 PPL 进程、自我保护会拦写入，且改完必须
       重启才生效**。这里如实返回「需重启」；若写入被拒，界面会提示进安全模式。

    ## 「开启」= 全部还原

    优先按**关闭前抓的快照**写回 Start（`data/defender_orig.json`）；
    没有快照时退回默认值表，并**在结果里注明是猜的** ——
    `WdBoot`/`WdFilter` 的 Boot 启动值写错会让启动期驱动不加载，不能含糊。
    同时删策略键、清 `ServiceStartStates`、重新启用 4 个计划任务。
    """
    enabled = bool(enabled)
    results = []
    tamper = (defender_status() or {}).get("tamper")

    def _pg(fmt, *a):
        """上报一条进度（可选）。任何异常都不许打断主流程。"""
        if progress is None:
            return
        try:
            progress(fmt % a if a else fmt)
        except Exception:
            pass

    if not enabled:
        # ---------- 全关闭 ----------
        # ① 先存快照（只在首次），别把「已禁用的值」当原始值
        _pg("保存关闭前的原值快照…")
        saved, n_skip = _def_snapshot_save()
        if saved:
            results.append({
                "name": "原值快照", "want": "保存", "ok": True,
                "note": "已记录关闭前的 Start 值，恢复时按它写回"
                        + ("（其中 %d 个当时已是禁用状态，恢复时按默认值处理）"
                           % n_skip if n_skip else "")})

        # ② 8 个服务/驱动全设 4
        _nc = len(_DEF_COMPONENTS)
        for _i, (name, kind, _dflt, _desc) in enumerate(_DEF_COMPONENTS):
            _pg("禁用服务/驱动 %d/%d：%s（%s）…", _i + 1, _nc, name, kind)
            ok, note = _svc_set_start(name, 4)
            results.append({"name": "%s（%s）" % (name, kind), "want": 4,
                            "ok": ok, "note": note})

        # ③ 策略键 + 反间谍键 + 实时保护策略
        for _i, (key, val, typ) in enumerate(
                ((_DEF_POL, "DisableAntiSpyware", "REG_DWORD"),
                 (_DEF_RT_KEY, _DEF_RT_VALUE, "REG_DWORD"),
                 (_DEF_MAIN_KEY, "DisableAntiSpyware", "REG_DWORD"),
                 (_DEF_MAIN_KEY, "DisableAntiVirus", "REG_DWORD"))):
            label = "%s \\ %s" % (key.rsplit("\\", 1)[-1], val)
            _pg("写入策略键 %d/4：%s…", _i + 1, label)
            try:
                reg_write(key, val, typ, 1)
                back = _reg_dword(key, val)
                results.append({"name": label, "want": 1, "ok": back == 1,
                                "note": "已写入并回读一致" if back == 1
                                        else "写入后回读=%r" % (back,)})
            except OSError as e:
                results.append({"name": label, "ok": False,
                                "note": str(e)[:60]})

        # ④ 4 个计划任务
        for _i, t in enumerate(_DEF_TASKS):
            _pg("禁用计划任务 %d/%d：%s…", _i + 1, len(_DEF_TASKS),
                t.rsplit("\\", 1)[-1])
            ok, note = set_task(t, False)
            results.append({"name": "计划任务 %s" % t.rsplit("\\", 1)[-1],
                            "want": "禁用", "ok": ok, "note": note})

        # ⑤ 回读复核：8 个组件里还有几个没被禁掉
        _pg("复核（逐个回读）…")
        st = defender_status()
        left = st.get("remaining") or []
        results.append({
            "name": "复核", "want": "8 个组件全部禁用",
            "ok": not left,
            "note": ("已全部禁用（重启后生效）" if not left
                     else "仍未禁用：%s。普通模式下 WinDefend 有 PPL 自我保护，"
                          "请**进安全模式**再执行一次（本页有说明）"
                          % "、".join(left))})
    else:
        # ---------- 全部还原 ----------
        _pg("读取关闭前记录的原值…")
        vals, guessed, snap_note = _def_snapshot_restore_values()
        if guessed:
            note = ((snap_note + "；" if snap_note else "")
                    + "下面按 **Windows 默认值**恢复（WdBoot/WdFilter=0 Boot、"
                      "WinDefend=2、其余=3）；若你之前手工改过，请核对一遍")
        else:
            note = snap_note or ("已按关闭前记录的原值恢复（%d 项）" % len(vals))
        results.append({"name": "原值快照", "want": "读取",
                        "ok": not guessed, "note": note})
        _nc2 = len(_DEF_COMPONENTS)
        for _i, (name, kind, dflt, _desc) in enumerate(_DEF_COMPONENTS):
            _pg("恢复服务/驱动 %d/%d：%s（%s）…", _i + 1, _nc2, name, kind)
            want = vals.get(name)
            want = dflt if want is None else int(want)
            ok, note = _svc_set_start(name, want)
            results.append({"name": "%s（%s）" % (name, kind), "want": want,
                            "ok": ok, "note": note})

        for _i, (key, val) in enumerate(
                ((_DEF_POL, "DisableAntiSpyware"),
                 (_DEF_RT_KEY, _DEF_RT_VALUE),
                 (_DEF_MAIN_KEY, "DisableAntiSpyware"),
                 (_DEF_MAIN_KEY, "DisableAntiVirus"))):
            label = "%s \\ %s" % (key.rsplit("\\", 1)[-1], val)
            _pg("清理策略键 %d/4：%s…", _i + 1, label)
            try:
                reg_delete(key, val)
                left = _reg_dword(key, val)
                results.append({"name": label, "want": "删除",
                                "ok": left is None,
                                "note": "已删除" if left is None
                                        else "删除后仍读到 %r" % (left,)})
            except OSError as e:
                results.append({"name": label, "ok": False,
                                "note": str(e)[:60]})
        # 更新侧留下的 ServiceStartStates 会让 Defender 干脆不启动
        try:
            reg_delete(_DEF_MAIN_KEY, "ServiceStartStates")
            results.append({"name": "ServiceStartStates", "want": "删除",
                            "ok": True, "note": "已删除（留着会让 Defender 不启动）"})
        except OSError as e:
            results.append({"name": "ServiceStartStates", "ok": False,
                            "note": str(e)[:60]})

        for _i, t in enumerate(_DEF_TASKS):
            _pg("启用计划任务 %d/%d：%s…", _i + 1, len(_DEF_TASKS),
                t.rsplit("\\", 1)[-1])
            ok, note = set_task(t, True)
            results.append({"name": "计划任务 %s" % t.rsplit("\\", 1)[-1],
                            "want": "启用", "ok": ok, "note": note})

        _pg("复核（读回实时保护状态）…")
        st = defender_status()
        results.append({
            "name": "复核", "want": "实时保护恢复",
            "ok": bool(st.get("realtime")),
            "note": ("实时保护已恢复" if st.get("realtime")
                     else "读回实时保护仍是关闭；**需重启**后才会真正恢复"
                          + ("；篡改防护开着，可能被改回去" if tamper else ""))})

    # 篡改防护开着时，任何写入都可能被回滚 —— 统一提示（不谎报成功）
    if tamper and any(not r.get("ok") for r in results):
        results.append({
            "name": "篡改防护", "want": "需要先关闭", "ok": False,
            "note": "**篡改防护正在开启**，Windows 会把改动改回去。"
                    "请先在「Windows 安全中心 → 病毒和威胁防护 → 管理设置」"
                    "里关闭篡改防护，再执行一次"})
    return results




# --------------------------------------------------------------------------
# process / memory
# --------------------------------------------------------------------------
def list_processes(limit=200):
    """进程列表：名称 / PID / 内存 / exe / CPU% / GPU%。

    一次 CIM 调用同时取回进程信息与 CPU 占用
    （`Win32_PerfFormattedData_PerfProc_Process.PercentProcessorTime` 是「占单核百分比」，
    除以逻辑核数换算成任务管理器口径）；GPU 占用取自 PDH GPU Engine（按 pid 聚合）。
    CIM 不可用时退回 tasklist（此时没有 exe / CPU / GPU）。
    """
    temp_sampler_start()      # GPU per-pid 占用靠常驻采样线程喂养，这里确保它已启动
    procs = []
    script = ("$perf=@{};"
              "Get-CimInstance Win32_PerfFormattedData_PerfProc_Process "
              "-ErrorAction SilentlyContinue | ForEach-Object { "
              "$perf[[int]$_.IDProcess]=$_.PercentProcessorTime };"
              "Get-CimInstance Win32_Process | ForEach-Object { "
              "'{0}|||{1}|||{2}|||{3}|||{4}' -f $_.Name,$_.ProcessId,"
              "$_.WorkingSetSize,$_.ExecutablePath,$perf[[int]$_.ProcessId] }")
    rc, out = ps(script, timeout=120)
    ncpu = max(1, os.cpu_count() or 1)
    for line in (out or "").splitlines():
        p = line.strip().split("|||")
        if len(p) < 3 or not p[0]:
            continue
        try:
            pid = int(p[1])
        except Exception:
            continue
        try:
            mem = int(p[2] or 0)
        except Exception:
            mem = 0
        cpu = None
        if len(p) > 4 and p[4].strip():
            try:
                cpu = max(0.0, min(100.0, float(p[4]) / ncpu))
            except Exception:
                cpu = None
        procs.append({"name": p[0], "pid": pid, "mem": mem,
                      "exe": (p[3].strip() if len(p) > 3 else ""),
                      "cpu": cpu, "gpu": None})
    if not procs:                       # CIM 不可用时退回 tasklist
        rc, out = run(["tasklist", "/fo", "csv", "/nh"], timeout=30)
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith('"'):
                continue
            m = re.findall(r'"([^"]*)"', line)
            if len(m) < 5:
                continue
            try:
                mem_kb = int(re.sub(r"[^\d]", "", m[4]) or 0)
            except Exception:
                mem_kb = 0
            try:
                procs.append({"name": m[0], "pid": int(m[1]),
                              "mem": mem_kb * 1024, "exe": "",
                              "cpu": None, "gpu": None})
            except Exception:
                continue
    # GPU 占用取 PDH GPU Engine 的「按进程聚合」，由常驻采样线程持续更新。
    # 注意：这里不能连调两次 _gpu_engine_utils() —— PDH 是速率计数器，两次采样必须间隔
    # ≥1 秒；立刻连调只会拿到空/全 0 的结果，还会把已有缓存覆盖掉。
    # 采样链路已建立时（_gpu_conn["last"] 非 None），没出现在聚合里的进程 = 0.0%
    # （任务管理器口径），显示 0.0% 而不是「—」。
    gpu_ok = _gpu_conn.get("last") is not None
    for p in procs:
        v = _GPU_BY_PID.get(p["pid"])
        if v:
            p["gpu"] = round(v, 1)
        elif gpu_ok:
            p["gpu"] = 0.0
    procs.sort(key=lambda x: -x["mem"])
    return procs[:limit]


# ---- 进程快照（纯 ctypes，毫秒级）——内存与性能页每秒自动刷新专用 ----
# list_processes 每次要起一个 PowerShell 跑 CIM（约 1.4s），绝不能 1 秒一次；
# 这里用 Toolhelp32 快照 + OpenProcess 查询，全 ctypes、不起任何子进程。
_TH32CS_SNAPPROCESS = 0x00000002
_PROC_QUERY_LIMITED = 0x00001000
_PROC_PREV = {}          # pid -> (kernel_100ns, user_100ns, monotonic)


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_ulong),
                ("cntUsage", ctypes.c_ulong),
                ("th32ProcessID", ctypes.c_ulong),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", ctypes.c_ulong),
                ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_ulong),
                ("szExeFile", ctypes.c_wchar * 260)]


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def _k32_gpmi():
    """GetProcessMemoryInfo：Win7+ 在 kernel32 里叫 K32GetProcessMemoryInfo，
    老系统走 psapi。返回可调用对象（模块级缓存）。

    签名必须声明：不声明时 HANDLE 参数按 C int 传，64 位句柄会被截断，
    调用静默失败（内存一栏永远读成 0）。
    """
    global _GPMI_FN
    try:
        return _GPMI_FN
    except NameError:
        pass
    try:
        _GPMI_FN = getattr(_k32, "K32GetProcessMemoryInfo")
    except AttributeError:
        _GPMI_FN = ctypes.WinDLL("psapi").GetProcessMemoryInfo
    _GPMI_FN.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD]
    _GPMI_FN.restype = wt.BOOL
    return _GPMI_FN


def procs_fast(limit=60):
    """进程快照：名称 / PID / 内存 / exe / CPU% / GPU%，纯 ctypes（<10ms）。

    - 名称/PID 来自 Toolhelp32 快照；工作集 / exe 路径 / CPU 时间来自
      OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)（系统进程打不开就跳过该字段）。
    - CPU% = 与上次快照的内核+用户时间差 ÷ 墙钟差 ÷ 逻辑核数（任务管理器口径）；
      首帧没有基准，返回 None（界面显示「—」，下一秒就有值）。
    - GPU% 直接复用常驻采样线程喂养的 _GPU_BY_PID（同 list_processes 口径），
      采样链路已建立时，没出现在聚合里的进程 = 0.0%。
    """
    temp_sampler_start()      # GPU per-pid 靠常驻采样线程喂养，这里确保它已启动
    k32 = _k32
    gpmi = _k32_gpmi()
    snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if _bad_handle(snap):                 # INVALID_HANDLE_VALUE（见 _bad_handle）
        return []
    rows = []
    try:
        e = _PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            rows.append((e.th32ProcessID, e.szExeFile))
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)

    now = time.monotonic()
    ncpu = max(1, os.cpu_count() or 1)
    out = []
    for pid, exe in rows:
        name = exe or "System"
        mem, cpu, exepath = 0, None, ""
        h = k32.OpenProcess(_PROC_QUERY_LIMITED, False, pid)
        if h:
            try:
                pmc = _PROCESS_MEMORY_COUNTERS()
                pmc.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
                if gpmi(h, ctypes.byref(pmc), pmc.cb):
                    mem = int(pmc.WorkingSetSize)
                buf = ctypes.create_unicode_buffer(1024)
                sz = ctypes.c_ulong(1024)
                if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(sz)):
                    exepath = buf.value
                kt = ctypes.c_ulonglong()
                ut = ctypes.c_ulonglong()
                x1 = ctypes.c_ulonglong()
                x2 = ctypes.c_ulonglong()
                if k32.GetProcessTimes(h, ctypes.byref(x1), ctypes.byref(x2),
                                       ctypes.byref(kt), ctypes.byref(ut)):
                    prev = _PROC_PREV.get(pid)
                    if prev and now > prev[2]:
                        d_cpu = ((kt.value + ut.value) - (prev[0] + prev[1])) / 1e7
                        cpu = max(0.0, min(100.0,
                                           d_cpu / (now - prev[2]) / ncpu * 100.0))
                    _PROC_PREV[pid] = (kt.value, ut.value, now)
            except Exception:
                pass
            finally:
                k32.CloseHandle(h)
        out.append({"name": name, "pid": int(pid), "mem": mem, "exe": exepath,
                    "cpu": cpu, "gpu": None})

    # 已消失的进程把 CPU 基准一起清掉，防止字典无限增长
    if len(_PROC_PREV) > len(rows) * 2 + 64:
        alive = {pid for pid, _ in rows}
        for pid in [p for p in _PROC_PREV if p not in alive]:
            _PROC_PREV.pop(pid, None)

    # GPU 占用（同 list_processes 口径）
    gpu_ok = _gpu_conn.get("last") is not None
    for p in out:
        v = _GPU_BY_PID.get(p["pid"])
        if v:
            p["gpu"] = round(v, 1)
        elif gpu_ok:
            p["gpu"] = 0.0
    out.sort(key=lambda x: -x["mem"])
    return out[:limit]


# ---- 检查更新（启动时静默调用；失败一律 ok=False，调用方安静跳过） ----
GITHUB_REPO = "ncbsz/WinToolbox"
GITHUB_RELEASES_URL = "https://github.com/ncbsz/WinToolbox/releases"
GH_API_PATH = "repos/%s/releases/latest" % GITHUB_REPO
GITHUB_API_LATEST = "https://api.github.com/" + GH_API_PATH

# ---- 多通道：国内访问 GitHub 的可用性很飘，单点必然时不时失败 ----------------
# 实测（2026-10 本机）：直连、gh-proxy、jsDelivr 可用；
# kkgithub / ghfast / gitmirror 当时不可用，但镜像站寿命短，仍留在后备位里。
GH_API_MIRRORS = [
    ("GitHub 直连", "https://api.github.com/" + GH_API_PATH),
    ("gh-proxy 镜像", "https://gh-proxy.com/https://api.github.com/" + GH_API_PATH),
    ("kkgithub 镜像", "https://api.kkgithub.com/" + GH_API_PATH),
    ("ghfast 镜像", "https://ghfast.top/https://api.github.com/" + GH_API_PATH),
    ("gitmirror 镜像", "https://hub.gitmirror.com/https://api.github.com/" + GH_API_PATH),
]
# 兜底通道：README 标题里就有 `## v1.4`，纯文本比 API 更容易被镜像覆盖到
GH_README_MIRRORS = [
    ("GitHub raw 直连",
     "https://raw.githubusercontent.com/%s/{br}/README.md" % GITHUB_REPO),
    ("gh-proxy raw",
     "https://gh-proxy.com/https://raw.githubusercontent.com/%s/{br}/README.md" % GITHUB_REPO),
    ("jsDelivr",
     "https://cdn.jsdelivr.net/gh/%s@{br}/README.md" % GITHUB_REPO),
]
GH_BRANCHES = ("main", "master")

# ---- 国内直连源（可选，推荐）：Gitee 镜像 ----------------------------------
# 只要在 Gitee 建一个同名仓库、把 README.md 同步过去，再把「用户名/仓库名」
# 填到设置文件 settings.json 的 "update_gitee_repo"（或下面的常量），
# 检查更新就会**优先走 Gitee 的纯国内线路** —— 不需要任何代理。
GITEE_REPO = ""
GITEE_BRANCH = "master"
GITEE_RAW = "https://gitee.com/%s/raw/%s/README.md"
GITEE_API = "https://gitee.com/api/v5/repos/%s/releases/latest"


def gitee_repo():
    """Gitee 镜像仓库名（settings.json 的 update_gitee_repo 优先于常量）。"""
    try:
        p = os.path.join(APP_DIR, "settings.json")
        with open(p, "r", encoding="utf-8") as f:
            v = str(json.load(f).get("update_gitee_repo") or "").strip().strip("/")
        if v:
            return v
    except Exception:
        pass
    return GITEE_REPO.strip().strip("/")

# 本机常见代理端口。「浏览器能上 GitHub、本程序不能」绝大多数是代理只配给了浏览器，
# 或者只存在于某个终端的环境变量里 —— 双击启动的 exe 继承不到。
LOCAL_PROXY_PORTS = (7890, 7897, 1595, 10809, 10808, 1080, 8889, 2080,
                     33210, 8118, 7891, 20171, 12333, 2081)
_PROXY_CACHE = {"t": 0.0, "list": []}
_PROXY_LOCK = threading.Lock()


def _ver_tuple(v):
    """'v1.10.2' -> (1, 10, 2)：纯数字逐段比较，非数字段按 0。"""
    out = []
    for part in str(v or "").strip().lstrip("vV").split("."):
        m = re.match(r"\d+", part)
        out.append(int(m.group()) if m else 0)
    return tuple(out) if out else (0,)


def _net_log(line):
    """检查更新的过程写进 data/update_check.log（只留最近 200 行）。

    exe 是无控制台的 GUI，出错时用户只能看到「网络不可达」—— 有了这个日志，
    任何一次失败都能直接翻出是哪一层出的问题。
    """
    try:
        os.makedirs(DATA, exist_ok=True)
        p = os.path.join(DATA, "update_check.log")
        old = []
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                old = f.readlines()[-200:]
        old.append("%s  %s\n" % (time.strftime("%m-%d %H:%M:%S"), line))
        with open(p, "w", encoding="utf-8") as f:
            f.writelines(old[-200:])
    except Exception:
        pass


def local_proxies(force=False):
    """本机可用代理列表：先取系统/环境变量里的，再探常见端口（缓存 10 分钟）。"""
    now = time.time()
    with _PROXY_LOCK:
        if not force and _PROXY_CACHE["list"] and now - _PROXY_CACHE["t"] < 600:
            return list(_PROXY_CACHE["list"])
        found = []
        try:
            import urllib.request as _u
            for scheme, purl in (_u.getproxies() or {}).items():
                if scheme in ("http", "https") and purl and purl not in found:
                    found.append(purl)
        except Exception:
            pass
        for port in LOCAL_PROXY_PORTS:
            try:
                sk = socket.socket()
                sk.settimeout(0.25)
                sk.connect(("127.0.0.1", port))
                sk.close()
            except OSError:
                continue
            purl = "http://127.0.0.1:%d" % port
            if purl not in found:
                found.append(purl)
        _PROXY_CACHE["list"], _PROXY_CACHE["t"] = found, now
        _net_log("可用代理: %s" % (", ".join(found) or "（无）"))
        return list(found)


def _gh_get(url, timeout=6, proxy=None, accept="application/vnd.github+json"):
    """GET 一个 URL，返回 (ok, text, err)。proxy=None → 走默认 opener。"""
    import urllib.request
    import urllib.error
    import ssl
    hdr = {"User-Agent": "WinToolbox-Update-Check", "Accept": accept}
    try:
        if proxy:
            op = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        else:
            op = urllib.request.build_opener()
        with op.open(urllib.request.Request(url, headers=hdr), timeout=timeout) as r:
            return True, r.read().decode("utf-8", "replace"), ""
    except urllib.error.HTTPError as e:
        return False, "", "HTTP %s" % e.code
    except Exception as e:
        return False, "", "%s: %s" % (type(e).__name__, str(e)[:90])


def _ver_from_readme(text):
    """从 README 的 `## v1.2.3` 标题里取最大版本号（兜底通道用）。"""
    best = ""
    for m in re.finditer(r"^#{2,3}\s*v?(\d+(?:\.\d+)+)", text or "", re.M):
        if _ver_tuple(m.group(1)) > _ver_tuple(best or "0"):
            best = m.group(1)
    return best


def check_update(current="0", timeout=6, budget=22):
    """查最新版本：多通道依次尝试，成功即止。

    通道顺序：① Gitee 国内直连（配了才走）→ ② 默认 opener（自动带环境变量/系统代理）
    → ③ 本机探测到的代理端口 → ④ 各镜像 API（gh-proxy / kkgithub / ghfast / gitmirror）
    → ⑤ README 纯文本兜底（raw 直连 / gh-proxy / jsDelivr）。

    返回 {"ok","latest","has_new","url","notes","via","err","tried"}。
    旧实现用 `except Exception` 把原因全吞了，界面永远只说「网络不可达」，
    根本没法排查 —— 现在把每次尝试的结果如实带回来。
    """
    deadline = time.time() + max(6, budget)
    tried = []
    _net_log("—— 检查更新（当前 v%s）——" % current)

    def hit_api(label, url, proxy=None):
        if time.time() > deadline:
            return None
        ok, body, err = _gh_get(url, timeout, proxy)
        if not ok:
            tried.append("%s → %s" % (label, err))
            _net_log("  [x] %-20s %s" % (label, err))
            return None
        try:
            data = json.loads(body)
        except Exception:
            tried.append("%s → 返回非 JSON（可能被劫持）" % label)
            _net_log("  [x] %-20s 非 JSON 响应" % label)
            return None
        tag = str(data.get("tag_name") or "").strip().lstrip("vV")
        if not tag:
            tried.append("%s → 无 tag_name（仓库还没发 Release）" % label)
            _net_log("  [x] %-20s 无 tag_name" % label)
            return None
        _net_log("  [v] %-20s 成功 → v%s" % (label, tag))
        return {"ok": True, "latest": tag,
                "has_new": _ver_tuple(tag) > _ver_tuple(current),
                "url": data.get("html_url") or GITHUB_RELEASES_URL,
                "notes": str(data.get("body") or "").strip()[:800],
                "via": label, "err": "", "tried": tried}

    # ① 国内直连源优先（配了 Gitee 就走它，不需要任何代理）
    gr = gitee_repo()
    if gr:
        r = hit_api("Gitee 镜像", GITEE_API % gr)
        if r:
            r["url"] = "https://gitee.com/%s/releases" % gr
            return r
        ok, body, err = _gh_get(GITEE_RAW % (gr, GITEE_BRANCH), timeout,
                                accept="text/plain")
        if not ok and GITEE_BRANCH != "master":
            ok, body, err = _gh_get(GITEE_RAW % (gr, "master"), timeout,
                                    accept="text/plain")
        if ok:
            v = _ver_from_readme(body)
            if v:
                _net_log("  [v] %-20s 成功 → v%s" % ("Gitee 镜像(README)", v))
                return {"ok": True, "latest": v,
                        "has_new": _ver_tuple(v) > _ver_tuple(current),
                        "url": "https://gitee.com/%s" % gr, "notes": "",
                        "via": "Gitee 镜像", "err": "", "tried": tried}
        else:
            tried.append("Gitee 镜像 → %s" % err)
            _net_log("  [x] %-20s %s" % ("Gitee 镜像", err))

    r = hit_api(GH_API_MIRRORS[0][0], GH_API_MIRRORS[0][1])
    if r:
        return r
    for purl in local_proxies()[:4]:
        r = hit_api("经 %s" % purl, GH_API_MIRRORS[0][1], proxy=purl)
        if r:
            return r
    for label, url in GH_API_MIRRORS[1:]:
        r = hit_api(label, url)
        if r:
            return r
    for label, tpl in GH_README_MIRRORS:
        for br in GH_BRANCHES:
            if time.time() > deadline:
                break
            ok, body, err = _gh_get(tpl.format(br=br), timeout, accept="text/plain")
            if not ok:
                tried.append("%s(%s) → %s" % (label, br, err))
                continue
            v = _ver_from_readme(body)
            if v:
                _net_log("  [v] %-20s README 兜底 → v%s" % (label, v))
                return {"ok": True, "latest": v,
                        "has_new": _ver_tuple(v) > _ver_tuple(current),
                        "url": GITHUB_RELEASES_URL, "notes": "",
                        "via": "%s（README）" % label, "err": "", "tried": tried}
    err = "；".join(tried[-3:]) if tried else "没有可用通道"
    _net_log("  [x] 全部通道失败：%s" % err)
    return {"ok": False, "latest": "", "has_new": False,
            "url": GITHUB_RELEASES_URL, "notes": "", "via": "",
            "err": err, "tried": tried}


def trim_process(pid):
    """把指定进程的工作集换出到页面文件（-1/-1 = 交给系统按需重新换入）。

    注意：`SetProcessWorkingSetSize` 必须走已声明 argtypes 的 `_k32`，否则
    SIZE_T(-1) 会溢出（历史 bug：ArgumentError / OverflowError: int too long
    to convert）。且必须看返回值 —— 早期实现无论成败都 return True，
    界面显示"已整理"而系统其实什么都没发生。
    """
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_SET_QUOTA = 0x0100
    h = _k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA, False, int(pid))
    if not h:
        return False
    try:
        return bool(_k32.SetProcessWorkingSetSize(h, ctypes.c_size_t(-1),
                                                  ctypes.c_size_t(-1)))
    finally:
        _k32.CloseHandle(h)


def trim_all():
    ok = 0
    total = 0
    for p in list_processes(limit=10 ** 6):
        total += 1
        if p["mem"] < 8 * 1024 * 1024:
            continue
        if trim_process(p["pid"]):
            ok += 1
    return ok, total


# --------------------------------------------------------------------------
# network
# --------------------------------------------------------------------------
def net_diagnose():
    steps = []

    def add(name, ok, detail):
        steps.append({"name": name, "ok": ok, "detail": detail})

    rc, out = ps("Get-NetAdapter | Where-Object Status -eq 'Up' | "
                 "ForEach-Object { '$($_.Name)|$($_.LinkSpeed)|$($_.InterfaceDescription)' }")
    adapters = [l.strip() for l in out.splitlines() if "|" in l]
    add("网卡状态", bool(adapters),
        ("; ".join(adapters) if adapters else "未检测到已连接(Up)的网卡"))

    rc, out = ps("(Get-NetIPConfiguration | Where-Object {$_.IPv4Address} | "
                 "ForEach-Object { $_.InterfaceAlias + ' IP=' + $_.IPv4Address.IPAddress + "
                 "' GW=' + $_.IPv4DefaultGateway.NextHop }) -join '; '")
    ipinfo = out.strip()
    add("IP / 网关配置", bool(re.search(r"IP=\d+\.\d+\.\d+\.\d+", ipinfo)), ipinfo or "未获取到 IPv4 配置")

    rc, out = ps("(Get-DnsClientServerAddress -AddressFamily IPv4 | "
                 "Where-Object {$_.ServerAddresses} | "
                 "ForEach-Object { $_.InterfaceAlias + ' DNS=' + ($_.ServerAddresses -join ',') }) -join '; '")
    add("DNS 服务器", bool(out.strip()), out.strip() or "未配置 DNS")

    hosts = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                         r"System32\drivers\etc\hosts")
    hosts_ok, hosts_note = True, "正常"
    try:
        with open(hosts, "r", encoding="utf-8", errors="ignore") as f:
            lines = [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]
        if lines:
            hosts_ok = False
            hosts_note = "hosts 有 %d 条自定义记录(可能被篡改)" % len(lines)
        else:
            hosts_note = "无自定义记录"
    except Exception as e:
        hosts_note = "无法读取: %s" % e
    add("HOSTS 文件", hosts_ok, hosts_note)

    ex, typ, data = reg_read(r"HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Internet Settings",
                             "ProxyEnable")
    proxy_on = bool(ex and int(data or 0) == 1)
    add("系统代理", not proxy_on, "已启用代理(可能异常)" if proxy_on else "未启用代理")

    rc, out = ps("(Get-NetFirewallProfile | ForEach-Object { $_.Name + '=' + $_.Enabled }) -join '; '")
    add("防火墙状态", True, out.strip() or "未知")

    rc, out = run(["ping", "-n", "1", "-w", "1500", "114.114.114.114"])
    add("外网连通(ping 114.114.114.114)", rc == 0 and ("TTL" in out.upper()),
        "可达" if rc == 0 and "TTL" in out.upper() else "不可达")

    rc, out = run(["nslookup", "www.baidu.com"])
    add("DNS 解析(www.baidu.com)", rc == 0 and "Address" in out, "解析正常" if rc == 0 else "解析失败")

    rc, out = run(["netsh", "winsock", "show", "catalog"])
    lsp = len([l for l in out.splitlines() if l.strip()])
    add("Winsock/LSP 协议链", lsp < 120, "条目 %d" % lsp)

    score = sum(1 for s in steps if s["ok"])
    return {"steps": steps, "score": score, "total": len(steps)}


REPAIR_ACTIONS = {
    "flushdns": ("刷新 DNS 解析缓存", [["ipconfig", "/flushdns"]]),
    "release": ("释放 IP 地址", [["ipconfig", "/release"]]),
    "renew": ("重新获取 IP 地址", [["ipconfig", "/renew"]]),
    "registerdns": ("重新注册 DNS", [["ipconfig", "/registerdns"]]),
    "arpr": ("清除 ARP 缓存", [["arp", "-d", "*"]]),
    "route": ("重置路由表", [["route", "-f"]]),
    "winsock": ("重置 Winsock 目录", [["netsh", "winsock", "reset"]]),
    "tcpip": ("重置 TCP/IP 协议栈", [["netsh", "int", "ip", "reset"]]),
    "ipv4": ("重置 IPv4 协议栈", [["netsh", "int", "ipv4", "reset"]]),
    "firewall": ("重置防火墙规则", [["netsh", "advfirewall", "reset"]]),
}


def net_repair(keys):
    results = []
    for k in keys:
        if k not in REPAIR_ACTIONS:
            continue
        label, cmds = REPAIR_ACTIONS[k]
        ok = True
        for c in cmds:
            rc, out = run(c, timeout=60)
            ok = ok and (rc == 0 or rc == 1)
        results.append({"key": k, "name": label, "ok": ok})
    return results


# --------------------------------------------------------------------------
# DNS test
# --------------------------------------------------------------------------
import random

DNS_SERVERS = {
    # 数据源：用户提供的 DnsList.json（DnsTools），已按运营商/厂商分类
    "domestic": [
        {"name": "114 DNS", "ip": "114.114.114.114"},
        {"name": "114 DNS", "ip": "114.114.115.115"},
        {"name": "阿里 AliDNS", "ip": "223.5.5.5"},
        {"name": "阿里 AliDNS", "ip": "223.6.6.6"},
        {"name": "百度 BaiduDNS", "ip": "180.76.76.76"},
        {"name": "DNSPod DNS+", "ip": "119.29.29.29"},
        {"name": "DNSPod DNS+", "ip": "182.254.116.116"},
        {"name": "CNNIC SDNS", "ip": "1.2.4.8"},
        {"name": "CNNIC SDNS", "ip": "210.2.4.8"},
        {"name": "oneDNS", "ip": "117.50.11.11"},
        {"name": "oneDNS", "ip": "117.50.22.22"},
        {"name": "DNS派 电信/移动/铁通", "ip": "101.226.4.6"},
        {"name": "DNS派 电信/移动/铁通", "ip": "218.30.118.6"},
        {"name": "DNS派 联通", "ip": "123.125.81.6"},
        {"name": "DNS派 联通", "ip": "140.207.198.6"},
        {"name": "安徽电信 DNS", "ip": "61.132.163.68"},
        {"name": "安徽电信 DNS", "ip": "202.102.213.68"},
        {"name": "北京电信 DNS", "ip": "219.141.136.10"},
        {"name": "北京电信 DNS", "ip": "219.141.140.10"},
        {"name": "重庆电信 DNS", "ip": "61.128.192.68"},
        {"name": "重庆电信 DNS", "ip": "61.128.128.68"},
        {"name": "福建电信 DNS", "ip": "218.85.152.99"},
        {"name": "福建电信 DNS", "ip": "218.85.157.99"},
        {"name": "甘肃电信 DNS", "ip": "202.100.64.68"},
        {"name": "甘肃电信 DNS", "ip": "61.178.0.93"},
        {"name": "广东电信 DNS", "ip": "202.96.128.86"},
        {"name": "广东电信 DNS", "ip": "202.96.128.166"},
        {"name": "广东电信 DNS", "ip": "202.96.134.33"},
        {"name": "广东电信 DNS", "ip": "202.96.128.68"},
        {"name": "广西电信 DNS", "ip": "202.103.225.68"},
        {"name": "广西电信 DNS", "ip": "202.103.224.68"},
        {"name": "贵州电信 DNS", "ip": "202.98.192.67"},
        {"name": "贵州电信 DNS", "ip": "202.98.198.167"},
        {"name": "河南电信 DNS", "ip": "222.88.88.88"},
        {"name": "河南电信 DNS", "ip": "222.85.85.85"},
        {"name": "黑龙江电信 DNS", "ip": "219.147.198.230"},
        {"name": "黑龙江电信 DNS", "ip": "219.147.198.242"},
        {"name": "湖北电信 DNS", "ip": "202.103.24.68"},
        {"name": "湖北电信 DNS", "ip": "202.103.0.68"},
        {"name": "湖南电信 DNS", "ip": "222.246.129.80"},
        {"name": "湖南电信 DNS", "ip": "59.51.78.211"},
        {"name": "江苏电信 DNS", "ip": "218.2.2.2"},
        {"name": "江苏电信 DNS", "ip": "218.4.4.4"},
        {"name": "江苏电信 DNS", "ip": "61.147.37.1"},
        {"name": "江苏电信 DNS", "ip": "218.2.135.1"},
        {"name": "江西电信 DNS", "ip": "202.101.224.69"},
        {"name": "江西电信 DNS", "ip": "202.101.226.68"},
        {"name": "内蒙古电信 DNS", "ip": "219.148.162.31"},
        {"name": "内蒙古电信 DNS", "ip": "222.74.39.50"},
        {"name": "山东电信 DNS", "ip": "219.146.1.66"},
        {"name": "山东电信 DNS", "ip": "219.147.1.66"},
        {"name": "陕西电信 DNS", "ip": "218.30.19.40"},
        {"name": "陕西电信 DNS", "ip": "61.134.1.4"},
        {"name": "上海电信 DNS", "ip": "202.96.209.133"},
        {"name": "上海电信 DNS", "ip": "116.228.111.118"},
        {"name": "上海电信 DNS", "ip": "202.96.209.5"},
        {"name": "上海电信 DNS", "ip": "108.168.255.118"},
        {"name": "四川电信 DNS", "ip": "61.139.2.69"},
        {"name": "四川电信 DNS", "ip": "218.6.200.139"},
        {"name": "天津电信 DNS", "ip": "219.150.32.132"},
        {"name": "天津电信 DNS", "ip": "219.146.0.132"},
        {"name": "云南电信 DNS", "ip": "222.172.200.68"},
        {"name": "云南电信 DNS", "ip": "61.166.150.123"},
        {"name": "浙江电信 DNS", "ip": "202.101.172.35"},
        {"name": "浙江电信 DNS", "ip": "61.153.177.196"},
        {"name": "浙江电信 DNS", "ip": "61.153.81.75"},
        {"name": "浙江电信 DNS", "ip": "60.191.244.5"},
        {"name": "北京联通 DNS", "ip": "123.123.123.123"},
        {"name": "北京联通 DNS", "ip": "123.123.123.124"},
        {"name": "北京联通 DNS", "ip": "202.106.0.20"},
        {"name": "北京联通 DNS", "ip": "202.106.195.68"},
        {"name": "重庆联通 DNS", "ip": "221.5.203.98"},
        {"name": "重庆联通 DNS", "ip": "221.7.92.98"},
        {"name": "广东联通 DNS", "ip": "210.21.196.6"},
        {"name": "广东联通 DNS", "ip": "221.5.88.88"},
        {"name": "河北联通 DNS", "ip": "202.99.160.68"},
        {"name": "河北联通 DNS", "ip": "202.99.166.4"},
        {"name": "河南联通 DNS", "ip": "202.102.224.68"},
        {"name": "河南联通 DNS", "ip": "202.102.227.68"},
        {"name": "黑龙江联通 DNS", "ip": "202.97.224.69"},
        {"name": "黑龙江联通 DNS", "ip": "202.97.224.68"},
        {"name": "吉林联通 DNS", "ip": "202.98.0.68"},
        {"name": "吉林联通 DNS", "ip": "202.98.5.68"},
        {"name": "江苏联通 DNS", "ip": "221.6.4.66"},
        {"name": "江苏联通 DNS", "ip": "221.6.4.67"},
        {"name": "内蒙古联通 DNS", "ip": "202.99.224.68"},
        {"name": "内蒙古联通 DNS", "ip": "202.99.224.8"},
        {"name": "山东联通 DNS", "ip": "202.102.128.68"},
        {"name": "山东联通 DNS", "ip": "202.102.152.3"},
        {"name": "山东联通 DNS", "ip": "202.102.134.68"},
        {"name": "山东联通 DNS", "ip": "202.102.154.3"},
        {"name": "山西联通 DNS", "ip": "202.99.192.66"},
        {"name": "山西联通 DNS", "ip": "202.99.192.68"},
        {"name": "陕西联通 DNS", "ip": "221.11.1.67"},
        {"name": "陕西联通 DNS", "ip": "221.11.1.68"},
        {"name": "上海联通 DNS", "ip": "210.22.70.3"},
        {"name": "四川联通 DNS", "ip": "119.6.6.6"},
        {"name": "四川联通 DNS", "ip": "124.161.87.155"},
        {"name": "天津联通 DNS", "ip": "202.99.104.68"},
        {"name": "天津联通 DNS", "ip": "202.99.96.68"},
        {"name": "浙江联通 DNS", "ip": "221.12.1.227"},
        {"name": "浙江联通 DNS", "ip": "221.12.33.227"},
        {"name": "辽宁联通 DNS", "ip": "202.96.69.38"},
        {"name": "辽宁联通 DNS", "ip": "202.96.64.68"},
        {"name": "江苏移动 DNS", "ip": "221.131.143.69"},
        {"name": "江苏移动 DNS", "ip": "112.4.0.55"},
        {"name": "安徽移动 DNS", "ip": "211.138.180.2"},
        {"name": "安徽移动 DNS", "ip": "211.138.180.3"},
        {"name": "山东移动 DNS", "ip": "218.201.96.130"},
        {"name": "山东移动 DNS", "ip": "211.137.191.26"},
        {"name": "广东移动 DNS", "ip": "221.179.38.7"},
        {"name": "广东移动 DNS", "ip": "120.196.165.7"},
        {"name": "广东移动 DNS", "ip": "211.136.192.6"},
        {"name": "广东移动 DNS", "ip": "120.196.165.24"},
    ],
    "foreign": [
        {"name": "Google DNS", "ip": "8.8.8.8"},
        {"name": "Google DNS", "ip": "8.8.4.4"},
        {"name": "IBM Quad9", "ip": "9.9.9.9"},
        {"name": "OpenDNS", "ip": "208.67.222.222"},
        {"name": "OpenDNS", "ip": "208.67.220.220"},
        {"name": "V2EX DNS", "ip": "199.91.73.222"},
        {"name": "V2EX DNS", "ip": "178.79.131.110"},
    ],
}

# IPv6 公共 DNS（数据源同 DnsTools 的 DnsList.v6.json，取国内运营商/公共 + 国外主流）
DNS_V6_SERVERS = [
    {"name": "阿里 IPv6 DNS", "ip": "2400:3200::1"},
    {"name": "阿里 IPv6 DNS", "ip": "2400:3200:baba::1"},
    {"name": "腾讯 DNSPod IPv6", "ip": "2402:4e00::"},
    {"name": "百度 IPv6 DNS", "ip": "2400:da00::6666"},
    {"name": "中国电信 IPv6 DNS", "ip": "240e:4c:4008::1"},
    {"name": "中国电信 IPv6 DNS", "ip": "240e:4c:4808::1"},
    {"name": "中国联通 IPv6 DNS", "ip": "2408:8899::8"},
    {"name": "中国联通 IPv6 DNS", "ip": "2408:8888::8"},
    {"name": "中国移动 IPv6 DNS", "ip": "2409:8088::a"},
    {"name": "下一代互联网 CNGI", "ip": "240C::6666"},
    {"name": "下一代互联网 CNGI", "ip": "240C::6644"},
    {"name": "CNNIC IPv6 DNS", "ip": "2001:dc7:1000::1"},
    {"name": "Google IPv6 DNS", "ip": "2001:4860:4860::8888"},
    {"name": "Cloudflare IPv6 DNS", "ip": "2606:4700:4700::1111"},
    {"name": "OpenDNS IPv6", "ip": "2620:0:ccc::2"},
    {"name": "Quad9 IPv6 DNS", "ip": "2620:fe::fe"},
]


def _dns_raw_query(ip, domain="www.baidu.com", timeout=2.5):
    """Send a minimal UDP DNS A-record query, return (ok, latency_ms, answer_ip).

    与 DnsTools（Tauri + trust-dns-resolver）同底层原理：向目标 DNS 服务器
    直发标准 UDP DNS 报文测往返延迟。自动兼容 IPv6 DNS 服务器地址。
    """
    sock = None
    try:
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        sock = socket.socket(fam, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        qname = b"".join(bytes([len(label)]) + label.encode("utf-8")
                        for label in domain.split(".")) + b"\x00"
        req = struct.pack(">HHHHHH", random.randint(0, 0xFFFF), 0x0100, 1, 0, 0, 0) + qname + struct.pack(">HH", 1, 1)
        t0 = time.perf_counter()
        sock.sendto(req, (ip, 53))
        data, _ = sock.recvfrom(512)
        ms = int((time.perf_counter() - t0) * 1000)
        flags = struct.unpack_from(">H", data, 2)[0]
        rcode = flags & 0x0F
        qdcount = struct.unpack_from(">H", data, 4)[0]
        ancount = struct.unpack_from(">H", data, 6)[0]

        # skip question section
        idx = 12
        for _ in range(qdcount):
            while True:
                length = data[idx]
                if length & 0xC0 == 0xC0:
                    idx += 2
                    break
                idx += 1
                if length == 0:
                    break
                idx += length
            idx += 4  # qtype + qclass

        # parse A-record answers
        answer = None
        for _ in range(ancount):
            if idx + 1 >= len(data):
                break
            while True:
                length = data[idx]
                if length & 0xC0 == 0xC0:
                    idx += 2
                    break
                idx += 1
                if length == 0:
                    break
                idx += length
            if idx + 10 > len(data):
                break
            typ, cls, ttl, rdlen = struct.unpack_from(">HHIH", data, idx)
            idx += 10
            if typ == 1 and rdlen == 4 and idx + 4 <= len(data):
                answer = ".".join(str(b) for b in data[idx:idx + 4])
            idx += rdlen

        return rcode == 0 and ancount > 0, ms, answer
    except Exception:
        return False, None, None
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# ICMP ping —— 与 DnsTools 一致的测速口径
#
# DnsTools 用 ping 测 DNS 延迟。这里直接调原生 ICMP（iphlpapi!IcmpSendEcho），
# 而不是起 `ping.exe` 进程：对上百台服务器测速时，进程创建开销会让整体慢到不可用
# （每台 0.5~2s）。原生 ICMP 单次往返只需毫秒级，普通用户即可发送，无需管理员。
# --------------------------------------------------------------------------
_ICMP = {"ready": False}


def _icmp_init():
    """惰性初始化 ctypes 签名。不设 argtypes 在 64 位下会指针截断而崩溃。"""
    if _ICMP.get("ready"):
        return _ICMP
    try:
        import ctypes
        import ctypes.wintypes as wt

        class IPO(ctypes.Structure):
            _fields_ = [("Ttl", ctypes.c_ubyte), ("Tos", ctypes.c_ubyte),
                        ("Flags", ctypes.c_ubyte), ("OptionsSize", ctypes.c_ubyte),
                        ("OptionsData", ctypes.c_void_p)]

        class REPLY(ctypes.Structure):
            _fields_ = [("Address", wt.ULONG), ("Status", wt.ULONG),
                        ("RoundTripTime", wt.ULONG), ("DataSize", wt.USHORT),
                        ("Reserved", wt.USHORT), ("Data", ctypes.c_void_p),
                        ("Options", IPO)]

        ip = ctypes.windll.iphlpapi
        ip.IcmpCreateFile.restype = ctypes.c_void_p
        ip.IcmpSendEcho.argtypes = [ctypes.c_void_p, wt.ULONG, ctypes.c_void_p,
                                    wt.USHORT, ctypes.c_void_p, ctypes.c_void_p,
                                    wt.DWORD, wt.DWORD]
        ip.IcmpSendEcho.restype = wt.DWORD
        ip.IcmpCloseHandle.argtypes = [ctypes.c_void_p]
        ws = ctypes.windll.ws2_32
        ws.inet_addr.argtypes = [ctypes.c_char_p]
        ws.inet_addr.restype = wt.ULONG
        _ICMP.update(ready=True, ctypes=ctypes, ip=ip, ws=ws, REPLY=REPLY)
    except Exception:
        _ICMP["ready"] = False
    return _ICMP


def _icmp_ping_v4(ip_str, timeout_ms=1000, tries=2):
    """IPv4 ICMP Echo，返回最小往返毫秒（int）；不通返回 None。"""
    st = _icmp_init()
    if not st.get("ready"):
        return None
    try:
        ctypes = st["ctypes"]
        dst = st["ws"].inet_addr(ip_str.encode("ascii"))
        if dst == 0xFFFFFFFF:                 # INADDR_NONE：不是合法 IPv4
            return None
        h = st["ip"].IcmpCreateFile()
        if not h or h == ctypes.c_void_p(-1).value:
            return None
        try:
            payload = b"WinToolboxPing01" * 3             # 45 字节
            rsz = ctypes.sizeof(st["REPLY"]) + len(payload) + 8
            buf = ctypes.create_string_buffer(rsz)
            best = None
            for _ in range(max(1, tries)):
                n = st["ip"].IcmpSendEcho(h, dst, payload, len(payload), None,
                                          buf, rsz, timeout_ms)
                if not n:
                    break                     # 超时：本次无应答
                rep = ctypes.cast(buf, ctypes.POINTER(st["REPLY"])).contents
                if rep.Status == 0:           # IP_SUCCESS
                    ms = float(rep.RoundTripTime)
                    if best is None or ms < best:
                        best = ms
            return int(round(best)) if best is not None else None
        finally:
            st["ip"].IcmpCloseHandle(h)
    except Exception:
        return None


def ping_ms(ip_str, timeout_ms=1000, tries=2):
    """DNS 测速的延迟口径：IPv4 走原生 ICMP；IPv6 退回系统 `ping -6` 一次。"""
    ip_str = (ip_str or "").strip()
    if not ip_str:
        return None
    if ":" in ip_str:                          # IPv6：原生路径不支持，退回 ping.exe
        try:
            r = ping_host(ip_str, count=1)
            return int(r["avg"]) if r.get("ok") and r.get("avg") is not None else None
        except Exception:
            return None
    return _icmp_ping_v4(ip_str, timeout_ms, tries)


def _dns_probe(ip, domain, timeout=1.5, measured=3, comm=False):
    """单个 DNS 的延迟探测（参照 DnsTools / trust-dns-resolver 的做法）。

    comm=False（默认）—— **解析延迟**：
      热门域名（如 www.baidu.com）会被运营商缓存/透明拦截秒回，测出的 2ms 是假延迟。
      故 1) 正常查一次拿解析结果（兼预热）；2) 再用「随机子域名」查 N 次 ——
      任何真实解析器都必须向上游递归，拿到的才是该服务器与根/权威链路的真实耗时；取最小。

    comm=True —— **通信延迟（本机 ↔ 该 DNS 服务器）**：
      只发普通域名查询（服务器有缓存就直接应答），取多次往返的最小值。
      不含服务器向上游递归的时间，反映的就是本机到这台服务器之间的链路快慢，
      用来回答「换哪个 DNS 对本机更快」。

    全程复用同一个已连接 socket（trust-dns 同款做法），排除建连开销。
    """
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    try:
        sock = socket.socket(fam, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.connect((ip, 53))
    except Exception:
        return {"ok": False, "ms": None, "answer": None}

    import time as _t

    def q(name):
        """发一次查询，返回 (data, ms)；失败返回 (None, None)。"""
        try:
            qname = b"".join(bytes([len(label)]) + label.encode("utf-8")
                             for label in name.split(".")) + b"\x00"
            req = struct.pack(">HHHHHH", random.randint(0, 0xFFFF), 0x0100,
                              1, 0, 0, 0) + qname + struct.pack(">HH", 1, 1)
            t0 = _t.perf_counter()
            sock.send(req)
            data, _ = sock.recvfrom(512)
            return data, (_t.perf_counter() - t0) * 1000.0
        except Exception:
            return None, None

    def parse(data):
        try:
            flags = struct.unpack_from(">H", data, 2)[0]
            ancount = struct.unpack_from(">H", data, 6)[0]
            if (flags & 0x0F) == 0 and ancount > 0:
                return _dns_parse_answer(data, domain)
        except Exception:
            pass
        return None

    answer = None
    # 1) 普通域名查询：验证可用 + 解析结果；comm 模式下它本身就是最终结果
    comm_best = None
    rounds = max(1, measured) if comm else 1
    for _ in range(rounds):
        data, ms = q(domain)
        if data is not None:
            if answer is None:
                answer = parse(data)
            if ms is not None and (comm_best is None or ms < comm_best):
                comm_best = ms
    if comm:
        try:
            sock.close()
        except Exception:
            pass
        return {"ok": comm_best is not None,
                "ms": int(round(comm_best)) if comm_best is not None else None,
                "answer": answer}

    # 2) 随机子域名强制递归，测真实解析往返
    best = None
    last = None
    for _ in range(measured):
        rnd = "%012x.%s" % (random.getrandbits(48), domain)
        data, ms = q(rnd)
        last = data
        if data is not None and ms is not None and (best is None or ms < best):
            best = ms
    try:
        sock.close()
    except Exception:
        pass
    return {"ok": last is not None,
            "ms": int(round(best)) if best is not None else None,
            "answer": answer}


def _dns_parse_answer(data, domain):
    """从 DNS 响应里解析第一条 A 记录 IP（仅用于展示解析结果）。"""
    try:
        qdcount = struct.unpack_from(">H", data, 4)[0]
        ancount = struct.unpack_from(">H", data, 6)[0]
        idx = 12
        for _ in range(qdcount):
            while True:
                length = data[idx]
                if length & 0xC0 == 0xC0:
                    idx += 2
                    break
                idx += 1
                if length == 0:
                    break
                idx += length
            idx += 4
        for _ in range(ancount):
            if idx + 1 >= len(data):
                break
            while True:
                length = data[idx]
                if length & 0xC0 == 0xC0:
                    idx += 2
                    break
                idx += 1
                if length == 0:
                    break
                idx += length
            if idx + 10 > len(data):
                break
            typ, cls, ttl, rdlen = struct.unpack_from(">HHIH", data, idx)
            idx += 10
            if typ == 1 and rdlen == 4 and idx + 4 <= len(data):
                return ".".join(str(b) for b in data[idx:idx + 4])
            idx += rdlen
    except Exception:
        pass
    return None


def local_dns_servers():
    """枚举本机当前生效的 DNS 服务器（取自已启用网卡的配置），用于「本机 DNS 测速」。

    纯依赖 ``ipconfig /all``，无需第三方库，Windows 专用。返回
    ``[{"name": "本机 · <适配器名>", "ip": "<DNS IP>"}, ...]``（已去重，
    仅包含已启用网卡）。若取不到（无网络 / 命令失败）返回空列表。

    做法参照 DnsTools 的「本机 DNS」思路：拿到本机真正在用的 DNS 服务器后再逐个测速，
    而不是只测内置公共 DNS 列表。
    """
    import subprocess
    import re
    try:
        out = subprocess.run(
            ["ipconfig", "/all"],
            capture_output=True, text=True,
            encoding="gbk", errors="ignore",
            creationflags=0x08000000,   # CREATE_NO_WINDOW：隐藏黑窗口
        ).stdout
    except Exception:
        return []
    if not out:
        return []

    ip_re = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")
    servers = []
    seen = set()
    cur_name = None
    cur_active = True
    in_dns = False

    def add(ip):
        if not cur_active or ip in seen:
            return
        seen.add(ip)
        label = "本机 · " + (cur_name or "未知网卡")
        servers.append({"name": label, "ip": ip})

    for raw in out.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        indented = line[:1] in (" ", "\t")
        stripped = line.strip()
        low = stripped.lower()
        # 适配器标题（行首无缩进、含「适配器」/「adapter」、以冒号结尾）
        if (not indented) and ("适配器" in stripped or "adapter" in low) and stripped.endswith(":"):
            cur_name = stripped[:-1].strip()
            cur_active = True
            in_dns = False
            continue
        # 媒体已断开 → 该适配器视为未启用
        if "媒体已断开" in stripped or "media disconnected" in low:
            cur_active = False
        # DNS 服务器行（带标签的那一行，可能本身已含 1 个 IP）
        if ("DNS 服务器" in stripped) or ("DNS Servers" in stripped):
            in_dns = True
            for ip in ip_re.findall(stripped):
                add(ip)
            continue
        # DNS 多行延续（缩进、无冒号标签、含 IP）—— ipconfig 会把多个 DNS 折行列出
        if in_dns and indented and (":" not in stripped):
            ips = ip_re.findall(stripped)
            if ips:
                for ip in ips:
                    add(ip)
            else:
                in_dns = False
            continue
        # 遇到下一个带冒号的属性，结束 DNS 延续区
        if in_dns and (":" in stripped):
            in_dns = False
    return servers


def dns_test(mode="mixed", domain="www.baidu.com", on_each=None):
    """mode: domestic / foreign / mixed / ipv6 / local.
    Returns list of {name, ip, cat, ok, ms, answer, dns_ok}.

    **延迟口径 = ICMP ping**（与 DnsTools 一致）：`ms` 是 ping 往返毫秒，`ok` 表示 ping 通。
    每台还会做一次 DNS 查询拿 `answer`（解析结果），并把能否解析记为 `dns_ok` —— 这样
    「禁 ping 但能解析」（如 114.114.114.114）的服务器能被区分出来，而不是直接判不可用。

    mode=="local"（本机 DNS 测速）：服务器集 = 本机网卡当前配置的 DNS（分类「本机在用」）
    + 内置公共 DNS（国内/国外）；本机没配 DNS 时该部分为空，但仍会测公共 DNS。

    `on_each` — 可选回调。每测完**一台**服务器就立刻调用一次（传入该台的结果 dict），
    供界面边测边显示，不必等全部跑完。注意：回调在**线程池的工作线程**里触发，
    实现必须是线程安全的（通常只是往 queue.Queue 里 put），不要直接碰 Qt 控件。
    回调抛异常会被忽略，不影响整体结果。
    """
    if mode == "domestic":
        src = [(s, "国内") for s in DNS_SERVERS["domestic"]]
    elif mode == "foreign":
        src = [(s, "国外") for s in DNS_SERVERS["foreign"]]
    elif mode == "ipv6":
        src = [(s, "IPv6") for s in DNS_V6_SERVERS]
    elif mode == "local":
        # 本机 DNS 测速 = 本机 ↔ 各 DNS 服务器的「通信延迟」。
        # 对象：本机网卡当前配置的 DNS（标「本机在用」，便于对比"现在用的排第几"）
        #       + 内置公共 DNS（国内 / 国外）。度量走 comm=True（不含向上游递归）。
        src = [(s, "本机在用") for s in local_dns_servers()] + \
              [(s, "国内") for s in DNS_SERVERS["domestic"]] + \
              [(s, "国外") for s in DNS_SERVERS["foreign"]]
    else:
        src = [(s, "国内") for s in DNS_SERVERS["domestic"]] + \
              [(s, "国外") for s in DNS_SERVERS["foreign"]]
    out = [None] * len(src)
    # 统一成 (序号, 服务器, 分类) 三元组：并行与串行兜底共用同一入参形态，
    # 避免「嵌套拆包」写错导致并行分支静默退化为串行。
    jobs = [(i, s, cat) for i, (s, cat) in enumerate(src)]

    def work(job):
        i, s, cat = job
        ip = s["ip"]
        # 延迟口径 = **ICMP ping**（对齐 DnsTools）；同时做一次 DNS 查询拿解析结果，
        # 于是「禁 ping 但能解析」的服务器（如 114）也能被正确标注，不会被误判为不可用。
        ms = ping_ms(ip)
        d = _dns_probe(ip, domain, comm=True)
        r = {"name": s["name"], "ip": ip, "cat": cat,
             "ok": ms is not None, "ms": ms,
             "answer": d.get("answer"), "dns_ok": bool(d.get("ok"))}
        if on_each is not None:
            try:
                on_each(r)
            except Exception:
                pass          # 推送失败（如界面已关闭）不影响测试本身
        return i, r

    # 并行探测：114 个国内 DNS 串行要跑几十秒到几分钟（大量服务器不可达时每次
    # 都要等满超时）。单次探测是「独立 UDP socket + 阻塞收发」，无线程共享状态、
    # 纯 IO 等待 —— 提高并发只增加文件描述符占用，无数据竞争。
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=32) as ex:
            for i, r in ex.map(work, jobs):
                out[i] = r
    except (ValueError, TypeError, AttributeError):
        # 这几类是**代码 bug**（拆包形态不匹配、字段缺失…），不是环境问题。
        # 一律上抛：绝不静默降级成串行掩盖过去 —— 旧实现就是在这里无声退化成
        # 串行，让「并行从未生效」被当成「只是有点慢」藏了很久。
        raise
    except Exception:
        # 仅环境类问题（线程/句柄不足等）才退化为串行，且入参形态与并行一致
        out = [work(j)[1] for j in jobs]
    out = [r for r in out if r]
    out.sort(key=lambda x: (not x["ok"], x["ms"] if x["ms"] is not None else 9999))
    return out


def ping_host(host, count=4):
    """ICMP ping a host, parse Windows `ping` output (Chinese/English locale aware).

    Returns {host, ip, ok, sent, recv, loss, avg}. `avg` is average RTT in ms.
    """
    host = (host or "").strip() or "www.baidu.com"
    rc, out = run(["ping", "-n", str(count), "-w", "1500", host], timeout=30)
    ok = "TTL" in out.upper()

    ip = None
    m = re.search(r"\[(\d{1,3}(?:\.\d{1,3}){3})\]", out)
    if m:
        ip = m.group(1)
    else:
        m = re.search(r"(?:来自|from)\s+(\d{1,3}(?:\.\d{1,3}){3})", out, re.I)
        if m:
            ip = m.group(1)

    sent = recv = loss = avg = None
    m = re.search(r"(?:已发送|Sent)\s*=\s*(\d+)", out, re.I)
    if m:
        sent = int(m.group(1))
    m = re.search(r"(?:已接收|Received)\s*=\s*(\d+)", out, re.I)
    if m:
        recv = int(m.group(1))
    m = re.search(r"(\d+)%\s*(?:丢失|loss)", out, re.I)
    if m:
        loss = int(m.group(1))
    m = re.search(r"(?:平均|Average)\s*=\s*(\d+)ms", out, re.I)
    if m:
        avg = int(m.group(1))
    else:
        times = re.findall(r"(?:时间|time)\s*[=<>]\s*(\d+)ms", out, re.I)
        if times:
            avg = int(sum(int(t) for t in times) / len(times))

    return {"host": host, "ip": ip, "ok": ok, "sent": sent, "recv": recv,
            "loss": loss, "avg": avg}


def ping_once(host, timeout=1500):
    """Single ICMP ping via Windows `ping -n 1`, return {host, ip, ok, ms}."""
    rc, out = run(["ping", "-n", "1", "-w", str(timeout), host], timeout=10)
    ok = "TTL" in out.upper()
    ip = None
    m = re.search(r"\[(\d{1,3}(?:\.\d{1,3}){3})\]", out)
    if m:
        ip = m.group(1)
    else:
        m = re.search(r"(?:来自|from)\s+(\d{1,3}(?:\.\d{1,3}){3})", out, re.I)
        if m:
            ip = m.group(1)
    ms = None
    m = re.search(r"(?:时间|time)\s*[=<>]\s*(\d+)ms", out, re.I)
    if m:
        ms = int(m.group(1))
    return {"host": host, "ip": ip, "ok": ok, "ms": ms}


# --------------------------------------------------------------------------
# cleanup
# --------------------------------------------------------------------------
def junk_targets(age_scale=1.0):
    """可清理项清单（分类：常规清理 / 浏览器缓存 / 系统缓存）。

    每项带：
      group — 常规清理 / 浏览器缓存 / 系统缓存（UI 分列显示，分类清楚）
      level — recommend(建议清理) / ok(可以清理) / caution(谨慎清理)
      what  — 这些文件是什么、删了会怎样
      kind  — clear(清空目录内容) / files(只删指定文件名) / glob(通配多目录)
      age   — 基准「最小文件年龄（小时）」，0 = 不限制。只给**真有风险**的项配：
              临时文件（程序正在写）、内存转储（可能正在生成）、错误报告队列。
              浏览器/缩略图缓存不配 —— 那些文件丢了随时重建，且被占用的会自动跳过。
      min_age_hours — 按 age_scale 缩放后的实际阈值（本函数填好，下游直接用）

    刻意不收：C:\\Windows\\Installer（Windows 安装程序缓存）—— 删了会导致
    程序无法卸载/修复，风险收益不成比例。
    """
    win = os.environ.get("SystemRoot", r"C:\Windows")
    lad = os.environ.get("LOCALAPPDATA", "")
    pb = os.environ.get("ProgramData", r"C:\ProgramData")
    out = [
        # 临时文件：24 小时。**最该有阈值的一项** —— 程序正在写/待读的临时文件
        # 一旦被清掉，表现是那个程序莫名报错，而且很难联想到是清理工具干的。
        {"id": "user_temp", "name": "用户临时文件 (%TEMP%)",
         "path": os.environ.get("TEMP", ""), "kind": "clear", "group": "常规清理",
         "level": "recommend", "age": 24,
         "what": "各软件运行时产生的临时文件（安装包解包、编辑器缓存等）。"
                 "可以放心删除，正在被占用的文件会自动跳过。"},
        {"id": "win_temp", "name": "系统临时文件 (Windows\\Temp)",
         "path": os.path.join(win, "Temp"), "kind": "clear", "group": "常规清理",
         "level": "recommend", "age": 24,
         "what": "系统与软件安装/更新时留下的临时文件，删除安全。"},
        {"id": "prefetch", "name": "预读取文件 (Prefetch)",
         "path": os.path.join(win, "Prefetch"), "kind": "clear", "group": "常规清理",
         "level": "caution",
         "what": "系统为加速程序启动而预生成的读取缓存（.pf）。"
                 "删掉系统会重建，但短期内程序启动会明显变慢，一般建议保留。"},
        {"id": "wu_cache", "name": "Windows 更新缓存",
         "path": os.path.join(win, "SoftwareDistribution", "Download"),
         "kind": "clear", "group": "常规清理", "level": "recommend",
         "what": "已下载并安装完成的更新安装包，删掉不影响已经装好的更新，通常最占空间。"},
        {"id": "wu_logs", "name": "Windows 更新日志",
         "path": os.path.join(win, "Logs", "WindowsUpdate"),
         "kind": "clear", "group": "常规清理", "level": "ok",
         "what": "更新过程的记录日志，只在排查“更新失败”时才需要，平时可以清理。"},
        {"id": "win_logs", "name": "Windows 系统日志文件",
         "path": os.path.join(win, "Logs"), "kind": "clear", "group": "常规清理",
         "level": "caution",
         "what": "系统组件的运行日志（CBS、DISM、存储等）。只有排查系统故障时才需要，"
                 "正被占用的日志会自动跳过。"},
        {"id": "font_cache", "name": "字体缓存",
         "path": os.path.join(win, "ServiceProfiles", "LocalService", "AppData",
                              "Local", "FontCache"),
         "kind": "clear", "group": "常规清理", "level": "ok",
         "what": "Windows 的字体缓存数据库，删除后自动重建（首次打开字体列表会稍慢）。"},
        {"id": "minidump", "name": "系统蓝屏转储 (Minidump)",
         "path": os.path.join(win, "Minidump"), "kind": "clear", "group": "常规清理",
         "level": "ok", "age": 48,   # 蓝屏转储可能正在写入
         "what": "系统蓝屏时生成的小型转储文件，只有要分析蓝屏原因时才需要保留。"},
        {"id": "crash", "name": "应用崩溃转储 (CrashDumps)",
         "path": os.path.join(lad, "CrashDumps"), "kind": "clear", "group": "常规清理",
         "level": "ok", "age": 48,   # 崩溃转储可能正在写入
         "what": "程序崩溃时生成的内存转储，只有要追查崩溃原因时才需要保留。"},
        {"id": "wer_all", "name": "Windows 错误报告队列",
         "path": os.path.join(pb, "Microsoft", "Windows", "WER"),
         "kind": "clear", "group": "常规清理", "level": "caution", "age": 48,
         "what": "全机范围的错误报告队列与存档。只影响问题反馈上报，可清理；"
                 "正被占用的会自动跳过。"},
        {"id": "thumb", "name": "缩略图缓存 (Explorer)",
         "path": os.path.join(lad, "Microsoft", "Windows", "Explorer"),
         "kind": "files", "group": "常规清理", "level": "ok",
         "what": "资源管理器为图片/视频生成的缩略图数据库。删除后会自动重建，"
                 "只是第一次浏览大文件夹时会稍慢。"},
        {"id": "inetcache", "name": "IE / 系统网络缓存",
         "path": os.path.join(lad, "Microsoft", "Windows", "INetCache"),
         "kind": "clear", "group": "常规清理", "level": "recommend",
         "what": "系统组件与 IE 内核的网页缓存和临时下载，删除安全。"},
        {"id": "store_cache", "name": "商店应用缓存 (UWP LocalCache)",
         "path": os.path.join(lad, "Packages", "*", "LocalCache"),
         "kind": "glob", "group": "系统缓存", "level": "ok",
         "what": "每个商店/UWP 应用自己的本地缓存。删除后自动重建，不影响应用数据。"},
        {"id": "chrome", "name": "Chrome 缓存",
         "path": os.path.join(lad, "Google", "Chrome", "User Data", "Default", "Cache"),
         "kind": "clear", "group": "浏览器缓存", "level": "ok",
         "what": "Chrome 缓存的网页图片/脚本等。清理后网页首次打开会重新下载变慢；"
                 "不含书签、密码、登录状态。"},
        {"id": "edge", "name": "Edge 缓存",
         "path": os.path.join(lad, "Microsoft", "Edge", "User Data", "Default", "Cache"),
         "kind": "clear", "group": "浏览器缓存", "level": "ok",
         "what": "Edge 缓存的网页资源。清理后首次打开网页会重新加载；"
                 "不影响收藏夹与登录状态。"},
        {"id": "firefox", "name": "Firefox 缓存",
         "path": os.path.join(lad, "Mozilla", "Firefox", "Profiles", "*", "cache2"),
         "kind": "glob", "group": "浏览器缓存", "level": "ok",
         "what": "Firefox 的网页缓存（各 Profile 下）。清理后首次打开网页变慢；"
                 "不影响书签与登录状态。"},
        {"id": "opera", "name": "Opera 缓存",
         "path": os.path.join(lad, "Opera Software", "Opera Stable", "Cache"),
         "kind": "clear", "group": "浏览器缓存", "level": "ok",
         "what": "Opera 的网页缓存，清理后首次打开网页变慢。"},
        {"id": "brave", "name": "Brave 缓存",
         "path": os.path.join(lad, "BraveSoftware", "Brave-Browser",
                              "User Data", "Default", "Cache"),
         "kind": "clear", "group": "浏览器缓存", "level": "ok",
         "what": "Brave 的网页缓存，清理后首次打开网页变慢。"},
        {"id": "vivaldi", "name": "Vivaldi 缓存",
         "path": os.path.join(lad, "Vivaldi", "User Data", "Default", "Cache"),
         "kind": "clear", "group": "浏览器缓存", "level": "ok",
         "what": "Vivaldi 的网页缓存，清理后首次打开网页变慢。"},
    ]
    # 按档位缩放每项的基准阈值，下游（扫描 / 清理）统一读 min_age_hours，
    # 保证「预览的体积」与「实际删掉的东西」是同一套判定。
    try:
        sc = max(0.0, float(age_scale))
    except (TypeError, ValueError):
        sc = 1.0
    for t in out:
        base = t.get("age") or 0
        t["min_age_hours"] = int(round(float(base) * sc))
    return out


# 回收站（单独一项，说明与等级同样标注）
JUNK_RECYCLE = {
    "id": "recyclebin", "name": "回收站", "path": "SHELL:RecycleBin",
    "kind": "recycle", "level": "caution", "group": "常规清理",
    "what": "回收站里是你自己删除过的文件。清空后无法找回，请先确认没有想恢复的内容。",
    "age": 0,        # 回收站无法按时间过滤（系统只提供整体清空），见 clean_junk
}


# ==========================================================================
# 清理项的「文件年龄」阈值
#
# 为什么需要：clean_junk 原先遍历目录下**所有**条目直接删，完全不看文件多新 ——
# 程序刚创建、过几秒才去读的临时文件会被当垃圾清掉（被占用的只是"删失败"计数，
# **不被占用但正要用的就直接没了**，典型表现是某个程序莫名报错）。常见的做法
# 是只清「够旧」的文件，这也正是「时间阈值」这项设置存在的意义。
#
# 基准值（normal 档）取保守的安全阈值；档位只做**整体缩放**，
# 避免 20 个清理项各配一个数字（难维护，用户也看不懂）。
# ==========================================================================
JUNK_AGE_MODES = {"off": 0.0, "normal": 1.0, "strict": 2.0}
JUNK_AGE_DEFAULT = "normal"
JUNK_AGE_LABELS = {
    "normal": "仅清理 1 天前的文件（推荐）",
    "strict": "仅清理 2 天前的文件（更保守）",
    "off": "不限制文件年龄（可能误删正在用的文件）",
}


def junk_age_scale(mode=None):
    """档位名 → 缩放系数（未知值按推荐档，绝不返回 None）。"""
    return JUNK_AGE_MODES.get(str(mode or JUNK_AGE_DEFAULT).strip().lower(), 1.0)


def _age_cutoff(min_age_hours):
    """把「最小年龄（小时）」转成 mtime 截止时间戳；0/无效 → None（不限制）。"""
    try:
        h = float(min_age_hours or 0)
    except (TypeError, ValueError):
        return None
    return (time.time() - h * 3600.0) if h > 0 else None


def _file_old_enough(path, cutoff):
    """文件是否「够旧」。cutoff=None 表示不限制。

    读不到 mtime 时返回 False（保守：不动它）—— 宁可少删，不可误删。
    """
    if cutoff is None:
        return True
    try:
        return os.path.getmtime(path) <= cutoff
    except OSError:
        return False


def _prune_dir(path, cutoff, collect=None):
    """递归删掉目录里「够旧」的文件，空目录一并删掉。返回 (释放字节, 错误数)。

    自下而上（topdown=False）遍历：先清空子目录才能把子目录本身删掉。
    cutoff=None 时行为与原先的 shutil.rmtree 接近（全部删）。

    `collect` 非 None 时**只收集够旧的文件路径、不删任何东西**（供「移入回收站」
    模式用：先把要删的攒起来，再一次性交给 `_recycle_many` —— 回收站每个文件
    都要建 $I/$R 记录，逐个调用实测 3.4 秒/个，批量 45 毫秒/个，差 76 倍）。
    """
    freed = 0
    errors = 0
    for dp, _dn, fn in os.walk(path, topdown=False):
        for f in fn:
            fp = os.path.join(dp, f)
            if not _file_old_enough(fp, cutoff):
                continue
            try:
                freed += os.path.getsize(fp)
            except OSError:
                pass
            if collect is not None:
                collect.append(fp)
                continue
            try:
                os.remove(fp)
            except OSError:
                errors += 1
        if collect is not None:
            continue                       # 收集模式不删目录（文件还没真删）
        try:
            if not os.listdir(dp):
                os.rmdir(dp)
        except OSError:
            pass
    return freed, errors


def dir_size(path, cap=200000, min_age_hours=0):
    """目录占用。`min_age_hours>0` 时只统计**够旧**的文件（与清理口径一致）。

    清理侧按文件年龄过滤，所以统计侧也必须过滤 —— 否则预览的体积与实际删掉的
    对不上，用户会以为工具算错了。
    """
    total = 0
    n = 0
    if not path or not os.path.isdir(path):
        return 0, 0
    cutoff = _age_cutoff(min_age_hours)
    for dp, dn, fn in os.walk(path):
        for f in fn:
            try:
                fp = os.path.join(dp, f)
                if cutoff is not None and not _file_old_enough(fp, cutoff):
                    continue
                total += os.path.getsize(fp)
                n += 1
            except OSError:
                pass
            if n > cap:
                return total, n
    return total, n


def recycle_bin_size():
    """回收站占用。

    ⚠️ 别再用 PowerShell + Shell.Application COM 求合（原实现）——实测这台机器
    回收站有 **1.8 万项**，COM 枚举要 **36~46 秒**，且随负载剧烈波动（15s→51s），
    是垃圾扫描里**唯一的瓶颈**（其余 19 项合计才 ~15s；它一项就把整轮扫描从
    7s 拖到 50s+，也让回归的 45s 等待偶发超时）。
    回收站本质就是 `X:\\$Recycle.Bin\\<SID>\\` 目录：`$R*` 是真实数据、
    `$I*` 是元数据（不占实际大小）。直接 `os.scandir` 求和实测 **0.5 秒**，
    数值与 Shell API 一致（实测差 <1KB）。查不到时退回 Shell API。
    """
    if os.name != "nt":
        return 0
    total, enumerated = 0, False
    try:
        for d in list_drives():
            bin_dir = os.path.join(d["root"], "$Recycle.Bin")
            if not os.path.isdir(bin_dir):
                continue
            try:
                sids = os.scandir(bin_dir)
            except OSError:
                continue
            with sids:
                enumerated = True
                for sid in sids:
                    if not sid.is_dir(follow_symlinks=False):
                        continue
                    try:
                        f = os.scandir(sid.path)
                    except OSError:
                        continue          # 别人 SID 读不到（非管理员）：跳过
                    with f:
                        for ent in f:
                            if not ent.name.startswith("$R"):
                                continue  # $I* 是元数据，不计
                            try:
                                if ent.is_dir(follow_symlinks=False):
                                    s, _m = dir_size(ent.path, min_age_hours=0)
                                    total += s
                                else:
                                    total += ent.stat(follow_symlinks=False).st_size
                            except OSError:
                                pass
    except Exception:
        enumerated = False
    if enumerated:
        return total
    # 退回 Shell API：SHQueryRecycleBinW(NULL) = 所有驱动器汇总
    try:
        info = _SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(_SHQUERYRBINFO)
        fn = ctypes.windll.shell32.SHQueryRecycleBinW
        fn.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(_SHQUERYRBINFO)]
        fn.restype = ctypes.c_long
        if fn(None, ctypes.byref(info)) == 0:
            return int(info.i64Size)
    except Exception:
        pass
    return 0


def _glob_dirs(pattern):
    """kind=glob 的路径展开（如 %LOCALAPPDATA%\\Packages\\*\\LocalCache）。"""
    try:
        import glob as _glob
        return sorted(p for p in _glob.glob(pattern) if os.path.isdir(p))
    except Exception:
        return []


def junk_item_size(tid, age_scale=1.0):
    """只扫描一个垃圾项（供 UI 逐项并行统计）。按档位过滤文件年龄。"""
    if tid == "recyclebin":
        return dict(JUNK_RECYCLE, size=recycle_bin_size(), files=0)
    for t in junk_targets(age_scale):
        if t["id"] != tid:
            continue
        age = t.get("min_age_hours") or 0
        if t["kind"] == "glob":
            size, n = 0, 0
            for d in _glob_dirs(t["path"]):
                s, m = dir_size(d, min_age_hours=age)
                size += s
                n += m
            return dict(t, size=size, files=n)
        size, n = dir_size(t["path"], min_age_hours=age)
        return dict(t, size=size, files=n)
    return None


def scan_junk(age_scale=1.0):
    """全量扫描（并行）。UI 目前用 junk_item_size 逐项显示，此函数保留作兜底。"""
    tids = [t["id"] for t in junk_targets(age_scale)] + ["recyclebin"]
    out = {}
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as ex:
            for r in ex.map(lambda i: junk_item_size(i, age_scale), tids):
                if r:
                    out[r["id"]] = r
    except Exception:
        for t in tids:
            r = junk_item_size(t)
            if r:
                out[r["id"]] = r
    order = [t["id"] for t in junk_targets()] + ["recyclebin"]
    return [out[i] for i in order if i in out]


def clean_junk(ids, age_scale=1.0, recycle=False):
    """清理选中的项。按 `age_scale` 对应的阈值只删「够旧」的文件。

    ⚠️ 原实现遍历目录下**所有**条目直接删（目录走 shutil.rmtree），完全不看文件
    多新 —— 程序刚创建、过几秒才读的临时文件会被当垃圾清掉；被占用的只是"删失败"
    计数，**没被占用但正要用的就直接没了**（现象是某程序莫名报错，极难联想到
    是清理工具干的）。现在改为逐文件判断 mtime，新文件一律不动。

    `recycle=True` 时改为**先移入回收站**（可还原）。代价是慢：回收站每个文件都要
    建 $I/$R 记录，实测批量约 45 毫秒/个 —— 5186 个文件要 3~4 分钟，所以**默认
    关闭**，由用户在确认框里自己权衡（`False` = 直接删除，快）。
    """
    results = []
    # 回收站不在 junk_targets() 里（它单独是 JUNK_RECYCLE），不并进来那条
    # recycle 分支就永远走不到 —— 用户勾了回收站也拿不到任何结果行。
    scale = age_scale
    for t in list(junk_targets(scale)) + [dict(JUNK_RECYCLE, min_age_hours=0)]:
        if t["id"] not in ids:
            continue
        freed = 0
        errors = 0
        skipped = 0          # 因"太新"而跳过的文件数 —— 要如实报给用户
        recycled_n = 0
        if t["kind"] == "recycle":
            rc, _ = ps("Clear-RecycleBin -Force -ErrorAction SilentlyContinue")
            results.append({"id": t["id"], "name": t["name"], "freed": 0,
                            "ok": rc == 0, "skipped": 0,
                            "note": "已清空回收站（系统不返回释放量，未计入合计；"
                                    "回收站不支持按时间过滤，会清掉其中全部内容）"})
            continue
        cutoff = _age_cutoff(t.get("min_age_hours") or 0)
        bases = ([p for p in _glob_dirs(t["path"])]
                 if t["kind"] == "glob" else [t["path"]])
        if recycle:
            # 先把「够旧」的文件攒起来，再一次性交给回收站（批量快 76 倍）
            pend, pend_dirs = [], []
            for base in bases:
                if not os.path.isdir(base):
                    continue
                for entry in os.listdir(base):
                    full = os.path.join(base, entry)
                    if t["kind"] == "files" and entry.lower() not in (
                            "thumbcache_256.db", "thumbcache_1024.db",
                            "iconcache_256.db", "iconcache_1024.db"):
                        continue
                    if os.path.isdir(full):
                        s, _e = _prune_dir(full, cutoff, collect=pend)
                        freed += s
                        pend_dirs.append(full)
                    elif _file_old_enough(full, cutoff):
                        try:
                            freed += os.path.getsize(full)
                        except OSError:
                            pass
                        pend.append(full)
                    else:
                        skipped += 1
            if pend:
                ok_n, bad_n = _recycle_many(pend)
                recycled_n = ok_n
                errors += bad_n
                # 文件搬走了，回头把空出来的目录删掉
                for d in pend_dirs:
                    if os.path.isdir(d):
                        _prune_dir(d, cutoff)
        else:
            for base in bases:
                if not os.path.isdir(base):
                    continue
                for entry in os.listdir(base):
                    full = os.path.join(base, entry)
                    if t["kind"] == "files" and entry.lower() not in (
                            "thumbcache_256.db", "thumbcache_1024.db",
                            "iconcache_256.db", "iconcache_1024.db"):
                        continue
                    try:
                        if os.path.isdir(full):
                            s, err = _prune_dir(full, cutoff)
                            freed += s
                            errors += err
                        else:
                            if not _file_old_enough(full, cutoff):
                                skipped += 1
                                continue
                            freed += os.path.getsize(full)
                            os.remove(full)
                    except Exception:
                        errors += 1
        note = "" if bases else "目录不存在"
        _tips = []
        if skipped:
            if t.get("min_age_hours"):
                _tips.append("保留了 %d 个较新的文件（%g 小时内的不动）"
                             % (skipped, t["min_age_hours"]))
            else:
                _tips.append("保留了 %d 个较新的文件" % skipped)
        if recycled_n:
            _tips.append("其中 %d 个已移入回收站（可还原）" % recycled_n)
        if _tips:
            note = "；".join(_tips)
        results.append({"id": t["id"], "name": t["name"], "freed": freed,
                        "ok": errors == 0, "errors": errors,
                        "skipped": skipped, "recycled": recycled_n,
                        "note": note})
    if "recyclebin" in ids and not any(r["id"] == "recyclebin" for r in results):
        ps("Clear-RecycleBin -Force -ErrorAction SilentlyContinue")
    return results


# ==========================================================================
# 深度清理四件套（空文件夹清理 / 重复文件查找 / 文件夹占用分析 /
#                文件粉碎）—— 统一原则：扫描只读，删除先过确认与退路
# ==========================================================================
DEFAULT_SCAN_ROOTS = ("Downloads", "Documents", "Desktop", "Pictures", "Videos", "Music")
# 已知文件夹名 → 用户目录下的子目录（不能靠环境变量，那几个不是环境变量）
# 已知文件夹的**标准名**（供 known_folder() 用；别和下面那个
# `_KNOWN_FOLDERS`（名字→GUID 映射表）重名 —— 同名会静默覆盖）
_KNOWN_FOLDER_NAMES = {"desktop": "Desktop", "documents": "Documents",
                       "downloads": "Downloads", "pictures": "Pictures",
                       "videos": "Videos", "music": "Music"}
# 盘根写法：`C:` / `C:\` / `d:/` 都算（下面 _scan_roots 会归一成 `C:\`）
_DRIVE_ROOT_RE = re.compile(r"^[A-Za-z]:[\\/]?$")


def _scan_roots(roots=None):
    """把用户给的路径 / 盘根 / 已知文件夹名 / 环境变量名解析成真实存在的目录。

    去重、保序。
    已知文件夹走 `known_folder()`（SHGetKnownFolderPath）——**必须走它**：
    很多机器的「下载 / 桌面 / 文档」被重定向到 D 盘，直接拼 %USERPROFILE%
    会得到一个不存在的路径（这台机器就是如此），扫描会静默扫 0 个。
    """
    out, seen = [], set()
    for r in (roots or DEFAULT_SCAN_ROOTS):
        r = str(r)
        std = _KNOWN_FOLDER_NAMES.get(r.lower())
        if std:
            p = known_folder(std) or os.path.join(
                os.environ.get("USERPROFILE", ""), std)
        elif _DRIVE_ROOT_RE.match(r):
            # 盘根（整盘扫描用）。⚠️ 必须排在 isupper() 之前：`"C:\\".isupper()`
            # 为真，会被当成环境变量名去 os.environ.get("C:\\") → 拿空串 →
            # 这个盘被**静默丢掉**（扫 0 个，还看不出哪儿错了）。
            p = r[:2].upper() + "\\"
        elif r.isupper():
            p = os.environ.get(r, "")
        else:
            p = os.path.expandvars(os.path.expanduser(r))
        p = os.path.abspath(p) if p else ""
        n = _norm_path(p)
        if p and os.path.isdir(p) and n not in seen:
            seen.add(n)
            out.append(p)
    return out


def file_hash(path, algo="sha256", limit=None):
    """文件哈希（重复文件查找 / 文件校验用）。失败返回 ""。"""
    try:
        import hashlib
        h = hashlib.new(algo)
        n = 0
        with open(path, "rb") as f:
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                h.update(b)
                n += len(b)
                if limit and n >= limit:
                    break
        return h.hexdigest()
    except (OSError, ValueError):
        return ""


def empty_dirs_scan(roots=None, cap=200, min_age_hours=24, max_depth=4,
                    max_visit=60000, should_stop=None):
    """扫描空文件夹。只读。

    判定：**整棵子树里没有任何文件**（可以有空的子目录）才算空 —— 这是
    大多数清理工具的语义。只报**最上层**那个：父目录空了，子目录必然
    也空，一起列出来只是噪音（删父时子会跟着没）。
    min_age_hours：最近改动过的不报（软件正在用的目录随时会再写入），设 0 关闭。

    ⚠️ 原实现有个致命逻辑错误：**只要当前目录"直接含文件"就 `continue`，跳过整棵
    子树**。于是 `D:\\桌面`、`D:\\文档` 这类第一层就有文件的目录被整体放弃，
    里面成千上万个空文件夹一个都报不出来 —— 本机实测只扫出 1 条、耗时 0.0 秒。
    正确做法：**不管自己有没有文件，子目录都要继续查**（自己不是空的不代表
    子目录不是空的）。max_visit 用于防御极端目录树（几万个目录）拖死扫描。
    """
    cand = {}
    sub_files = {}                          # normpath -> 该子树里是否有文件
    self_files = {}                         # normpath -> 自己**直接**是否有文件
    visited_n = 0
    for base in _scan_roots(roots):
        stack = [(base, 0, False)]
        while stack:
            if should_stop is not None and should_stop():
                break
            cur, depth, visited = stack.pop()
            if depth > max_depth:
                continue
            if not visited:
                visited_n += 1
                if visited_n > max_visit:
                    break
            try:
                entries = list(os.scandir(cur))
            except (OSError, PermissionError):
                # 读不到的目录：按「有文件」保守处理，避免把不可访问的当成空
                sub_files[_norm_path(cur)] = True
                self_files[_norm_path(cur)] = True
                continue
            k = _norm_path(cur)
            dirs = [e for e in entries if e.is_dir(follow_symlinks=False)]
            has_file = any(not e.is_dir(follow_symlinks=False) for e in entries)
            if not visited:
                if not dirs:
                    # 叶子目录：自己有没有文件就决定它空不空
                    sub_files[k] = has_file
                    self_files[k] = has_file
                    if not has_file:
                        cand[k] = (cur, depth)
                    continue
                # 有子目录 → 后序处理（先记录自己，再把子目录压栈）
                self_files[k] = has_file
                stack.append((cur, depth, True))
                for e in dirs:
                    stack.append((e.path, depth + 1, False))
            else:
                # 子目录都处理完了：自己没有文件 **且** 所有子目录子树都没文件 → 自己是空的
                ok = (not self_files.get(k, True)) and all(
                    not sub_files.get(_norm_path(e.path), True) for e in dirs)
                sub_files[k] = not ok
                if ok:
                    cand[k] = (cur, depth)
    out = []
    for k, (cur, depth) in cand.items():
        if _is_protected_dir(cur):
            continue
        par = _norm_path(os.path.dirname(cur))
        if par in cand:                      # 父目录也在候选里 → 只留顶层
            continue
        try:
            age_h = (time.time() - os.path.getmtime(cur)) / 3600.0
        except OSError:
            age_h = 0.0
        if age_h >= min_age_hours:
            out.append({"path": cur, "depth": depth, "age_hours": round(age_h, 1)})
    out.sort(key=lambda x: (-x["age_hours"], -x["depth"]))
    return out[:cap]


def duplicate_files_scan(roots=None, cap=60, min_size=10240, deep=True,
                         same_size_only=False, should_stop=None):
    """重复文件查找。只读。

    两段式：先按大小分组（便宜），再对同大小组算 SHA-256 确认内容一致。
    same_size_only=True 时只按大小判重（快，但可能误报同名不同内容）。
    返回 [{size, wasted, verified, files:[{path, mtime, hash?}]}]，按可省空间降序。
    """
    by_size = {}
    for base in _scan_roots(roots):
        for root, dirs, files in os.walk(base):
            if should_stop is not None and should_stop():
                break          # 暂停：拿已收集的继续分组，能算多少算多少
            dirs[:] = [d for d in dirs if not _is_protected_dir(os.path.join(root, d))]
            for fn in files:
                fp = os.path.join(root, fn)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                if st.st_size < min_size:
                    continue
                by_size.setdefault(st.st_size, []).append((fp, st.st_mtime))
    groups = []
    for size, items in by_size.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda x: x[0].lower())
        if same_size_only:
            groups.append({"size": size, "verified": False,
                           "files": [{"path": p, "mtime": m} for p, m in items],
                           "wasted": size * (len(items) - 1)})
            continue
        sig = {}
        for p, m in items:
            h = file_hash(p)
            if h:
                sig.setdefault(h, []).append((p, m))
        for h, lst in sig.items():
            if len(lst) < 2:
                continue
            groups.append({"size": size, "hash": h, "verified": True,
                           "files": [{"path": p, "mtime": m, "hash": h} for p, m in lst],
                           "wasted": size * (len(lst) - 1)})
    groups.sort(key=lambda g: -g["wasted"])
    return groups[:cap]


def folder_usage(roots=None, top=40, min_size=2 * 1024 * 1024, max_depth=3,
                 should_stop=None):
    """文件夹占用分析。只读。

    一次遍历把每个文件大小累加到各层祖先目录（最多 max_depth 层），
    返回 (前 top 项, 扫描总字节) —— 回答「C 盘空间去哪了」。
    """
    acc = {}
    total = 0
    for base in _scan_roots(roots):
        base_n = _norm_path(base)
        for root, dirs, files in os.walk(base):
            if should_stop is not None and should_stop():
                break
            dirs[:] = [d for d in dirs if not _is_protected_dir(os.path.join(root, d))]
            try:
                acc.setdefault(_norm_path(root), {"bytes": 0, "files": 0})["files"] += len(files)
            except OSError:
                continue
            for fn in files:
                try:
                    sz = os.path.getsize(os.path.join(root, fn))
                except OSError:
                    continue
                total += sz
                cur = root
                for _d in range(max_depth + 1):
                    k = _norm_path(cur)
                    if k:
                        acc.setdefault(k, {"bytes": 0, "files": 0})["bytes"] += sz
                    nxt = os.path.dirname(cur)
                    if nxt == cur or _norm_path(nxt) == base_n:
                        break
                    cur = nxt
    rows = [{"path": k.replace("/", "\\"), "bytes": v["bytes"], "files": v["files"],
             "pct": round(v["bytes"] * 100.0 / total, 1) if total else 0.0}
            for k, v in acc.items() if v["bytes"] >= min_size]
    rows.sort(key=lambda x: -x["bytes"])
    return rows[:top], total


# --- 整盘占用扫描（用户先选盘、再扫；不走 Windows 索引，直接遍历目录）---
_DRIVE_FIXED = 3


def list_drives():
    """列出本机**固定**磁盘（过滤光驱/网络盘/可移动盘 —— 扫它们会卡住）。

    容量走 GetDiskFreeSpaceExW，卷标走 GetVolumeInformationW，都是直接问文件系统，
    **不依赖 Windows 搜索索引**（索引常常没建全，用它算占用会明显偏小）。
    """
    out = []
    if os.name != "nt":
        return out
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        return out
    for i in range(26):
        if not (mask & (1 << i)):
            continue
        root = "%s:\\" % chr(ord("A") + i)
        try:
            if ctypes.windll.kernel32.GetDriveTypeW(root) != _DRIVE_FIXED:
                continue
        except Exception:
            continue
        free = total = 0
        try:
            free_b = ctypes.c_ulonglong(0)
            total_b = ctypes.c_ulonglong(0)
            ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                root, None, ctypes.byref(total_b), ctypes.byref(free_b))
            if ok:
                total, free = int(total_b.value), int(free_b.value)
        except Exception:
            pass
        label = ""
        try:
            buf = ctypes.create_unicode_buffer(261)
            ctypes.windll.kernel32.GetVolumeInformationW(
                root, buf, 260, None, None, None, None, 0)
            label = buf.value or ""
        except Exception:
            pass
        out.append({"root": root, "label": label, "total": total, "free": free,
                    "used": max(0, total - free)})
    return out


# 整盘占用扫描的「会话状态」：暂停之后能**接着扫**，而不是从头再来。
# 同时只可能有一个整盘扫描在跑，所以只保留一份。
_DISK_SESSION = {}

# 扫描时顺手记下来的「大文件」阈值与条数上限。
# 为什么要顺手记：目录表只回答「哪个文件夹大」，但用户真正想清理的常常是
# 「哪个**文件**大」。每个文件都存下来会撑爆内存（整盘几十万个），所以只留
# ≥ 这个阈值的；16 MB 在家用机上通常几千条，占几 MB。
_BIG_FILE_MIN = 16 * 1024 * 1024
_BIG_FILE_CAP = 20000

# 扫描期间「实时大小」的每目录批量：每凑够这么多**文件**就把这一层新增的字节
# 沿祖先链累加一次。太小则每层都往根走一遍（整盘几百万文件会明显拖慢），
# 太大则界面上的数字一跳一跳的。
_LIVE_FLUSH_FILES = 512

# `on_live` / `on_tick` 的节流间隔（秒）。界面每 400ms 取一批，比这更勤也没人看，
# 白费 CPU。设成模块级是为了测试能调小它（不然秒级小目录扫完也等不到一次回报）。
_LIVE_TICK_SECS = 0.4


def disk_scan_reset():
    """丢掉暂停中的扫描状态（换盘 / 重新开始扫描时调）。"""
    _DISK_SESSION.clear()


def disk_usage_scan(drive, top=None, min_size=0, on_dir=None, on_tick=None,
                    should_stop=None, resume=False, max_dirs=None, on_live=None):
    """整盘占用扫描：**每算完一个目录就回报一个**（流式）。只读，不依赖索引。

    做法是**后序遍历**：先下到最深，子目录算完就把它的大小并进父目录，父目录
    算完再回报。所以每个回报上来的目录，大小都是**最终值** —— 这正是
    「按文件夹大小降序排列」要的（不像「先收集再统计」，中途拿不到确定数字）。

    `min_size=0`（默认）时**不丢弃任何目录**，由 UI 决定显示多少。
    `top=None` 表示全给，不截断。

    **暂停/继续是真的能续的**：`should_stop()` 返回 True 时立刻收工，把遍历栈
    与已累计的数据留在 `_DISK_SESSION`；下次带 `resume=True` 调就接着扫。

    `on_dir(path, byte, files, depth)` 在**工作线程**里被调用 —— 回调里只做最便宜
    的事（塞进列表），绝对不要碰控件。

    `on_live(path, byte_so_far, files_so_far, depth)` 是**扫描期间**的实时快照，
    只回报「当前正在扫的那个目录 + 它的各级祖先」这一条链（每 0.4 秒一次）。
    和 `on_dir` 的区别：`on_dir` 是**最终值**（子树全扫完才有），`on_live` 是
    **目前已发现**的值，会随着扫描推进单调增长。两者的口径是一致的 ——
    某目录扫完时 `on_live` 的最后一次值就等于 `on_dir` 报的最终值。

    返回 {"rows", "total", "files", "dirs", "secs", "stopped", "truncated", "big"}。
    `big` 是扫描中顺手记下的 ≥ `_BIG_FILE_MIN` 的文件 (路径, 字节)，给「搜索」用。
    """
    root = drive.rstrip("\\/") + "\\"
    # ⚠️ st 必须**就是** _DISK_SESSION 本身：早先写成「本地 dict + update 一份」，
    # 结果标量（total / n_files）只落在本地那份上，暂停后会话里是 0，续扫时数字全错。
    st = _DISK_SESSION
    if not (resume and st.get("drive") == drive):
        st.clear()
        st.update({"drive": drive, "stack": [(root, False)], "own": {}, "cnt": {},
                   "parent": {}, "sizes": {}, "nf": {}, "total": 0, "n_files": 0,
                   "n_dirs": 0, "t0": time.time(), "root": root,
                   "root_n": _norm_path(root),
                   # 实时大小（扫描中给界面看）：live[norm] = 该子树**已发现**的字节
                   "live": {}, "liven": {}, "cur": "", "big": []})
    stack = st["stack"]
    own, cnt, parent = st["own"], st["cnt"], st["parent"]
    sizes, nf = st["sizes"], st["nf"]
    live, liven, big = st["live"], st["liven"], st["big"]
    last_tick = 0.0
    stopped = False
    truncated = False

    def _flush(ck, delta, dfile):
        """把「刚发现的直接文件」沿祖先链累加进 live/liven。

        ⚠️ 从 `ck` **自己**开始，不是从它的父目录 —— `live[X]` 的语义是
        「X 的子树里目前已发现的字节」，既含 X 自己的直接文件，也含各级子孙
        累加上来的部分。这样某目录扫完时 `live[X]` 恰好等于它的最终大小，
        界面上的数字不会在「完成」那一刻突然一跳。
        """
        node = ck
        while node:
            live[node] = live.get(node, 0) + delta
            liven[node] = liven.get(node, 0) + dfile
            node = parent.get(node)

    def _tick(force=False):
        """节流回报：总进度 + **当前目录链的实时大小**。"""
        nonlocal last_tick
        now = time.time()
        if not force and now - last_tick <= _LIVE_TICK_SECS:
            return
        last_tick = now
        if on_tick is not None:
            try:
                on_tick(st["n_files"], st["n_dirs"], st["total"])
            except Exception:
                pass
        if on_live is None:
            return
        node, guard = st.get("cur") or "", 0
        while node and guard < 64:
            guard += 1
            k = _norm_path(node)
            rel = ""
            if node.startswith(st["root"]):
                rel = node[len(st["root"]):].strip("\\")
            try:
                on_live(node, live.get(k, 0), liven.get(k, 0),
                        rel.count("\\") + 1 if rel else 0)
            except Exception:
                pass
            up = os.path.dirname(node.rstrip("\\/"))
            if not up or up == node:
                break
            node = up

    while stack:
        cur, done = stack.pop()
        ck = _norm_path(cur)
        if not done:
            own[ck] = 0
            cnt[ck] = 0
            st["cur"] = cur            # 实时回报「现在扫到哪个目录了」
            flushed, flushed_n = 0, 0
            stack.append((cur, True))
            try:
                with os.scandir(cur) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                parent[_norm_path(e.path)] = ck
                                stack.append((e.path, False))
                                st["n_dirs"] += 1
                            elif e.is_file(follow_symlinks=False):
                                sz = e.stat(follow_symlinks=False).st_size
                                own[ck] += sz
                                cnt[ck] += 1
                                st["total"] += sz      # 整盘合计按**文件**累加
                                st["n_files"] += 1     # 否则会把每层目录重算一遍
                                if sz >= _BIG_FILE_MIN and len(big) < _BIG_FILE_CAP:
                                    big.append((e.path, sz))
                                # 攒够一批就把这一层新增的字节推给祖先，
                                # 否则一个几十万文件的目录要等整层扫完界面才动
                                if cnt[ck] - flushed_n >= _LIVE_FLUSH_FILES:
                                    _flush(ck, own[ck] - flushed,
                                           cnt[ck] - flushed_n)
                                    flushed, flushed_n = own[ck], cnt[ck]
                                    _tick()
                        except OSError:
                            continue
            except OSError:
                pass
            if cnt[ck] > flushed_n:        # 这一层最后不足一批的余量
                _flush(ck, own[ck] - flushed, cnt[ck] - flushed_n)
        else:
            sz, fc = own.get(ck, 0), cnt.get(ck, 0)
            sizes[ck], nf[ck] = sz, fc
            if ck != st["root_n"]:
                p = parent.get(ck)
                if p:
                    own[p] = own.get(p, 0) + sz
                    cnt[p] = cnt.get(p, 0) + fc
                if on_dir is not None:
                    try:
                        rel = cur[len(st["root"]):].strip("\\")
                        on_dir(cur, sz, fc, rel.count("\\") + 1 if rel else 0)
                    except Exception:
                        pass
            if max_dirs is not None and len(sizes) >= max_dirs:
                truncated = True
                break
        if should_stop is not None and should_stop():
            stopped = True
            break
        _tick()

    # 先把要用的都取出来：st 就是会话本身，下面要清它，先读后清
    total = st["total"]
    n_files = st["n_files"]
    n_dirs = st["n_dirs"]
    secs = round(time.time() - st["t0"], 1)
    root_s, root_n_s = st["root"], st["root_n"]
    keep = stopped or truncated          # 没跑完 → 留着会话给「继续扫描」
    rows = []
    for k, v in sizes.items():
        if v < min_size or k == root_n_s:
            continue
        p = k.replace("/", "\\")
        rel = p[len(os.path.normpath(root_s)):].strip("\\")
        rows.append({"path": p, "bytes": v, "files": nf.get(k, 0),
                     "pct": round(v * 100.0 / total, 1) if total else 0.0,
                     "depth": rel.count("\\") + 1 if rel else 0,
                     "system": _is_protected_dir(p)})
    rows.sort(key=lambda x: -x["bytes"])
    if not keep:
        st.clear()
    return {"rows": rows[:top] if top else rows, "total": total, "files": n_files,
            "dirs": n_dirs, "secs": secs, "stopped": stopped,
            "truncated": truncated, "done": not keep, "big": big}
def shred_files(paths, passes=3):
    """文件粉碎：多轮覆写后删除，**不可恢复**。

    真读真写（不是假进度条）：第 1 轮随机数据，之后 0x00/0xFF 交替，
    最后截断原长度再删；逐条如实报告。目录不粉碎（要递归处理里面的文件）。
    """
    out = []
    for p in (paths or []):
        p = str(p)
        rec = {"path": p, "ok": False, "note": ""}
        if not os.path.isfile(p):
            rec["note"] = "不是文件（目录不粉碎）"
            out.append(rec)
            continue
        try:
            size = os.path.getsize(p)
            with open(p, "r+b") as f:
                for k in range(max(1, int(passes))):
                    f.seek(0)
                    left = size
                    while left > 0:
                        n = min(1 << 20, left)
                        f.write(os.urandom(n) if k == 0 else (b"\x00" if k % 2 else b"\xff") * n)
                        left -= n
                    f.flush()
                f.truncate(0)
            os.remove(p)
            rec["ok"] = True
            rec["note"] = "已覆写 %d 轮并删除" % max(1, int(passes))
        except (OSError, PermissionError) as e:
            rec["note"] = "失败：%s" % str(e)[:60]
        out.append(rec)
    return out


def delete_paths(paths, use_recycle=True):
    """批量删除（空文件夹 / 重复文件清理共用）：优先送回收站，可还原。

    每条独立报告；受保护目录由 _is_protected_dir 硬拦；目录为空才删。
    """
    out = []
    for p in (paths or []):
        p = str(p)
        if _is_protected_dir(p):
            out.append({"path": p, "ok": False, "note": "拒绝（系统/容器目录）"})
            continue
        if not os.path.exists(p):
            out.append({"path": p, "ok": False, "note": "已不存在"})
            continue
        if os.path.isdir(p):
            try:
                if any(os.scandir(p)):
                    out.append({"path": p, "ok": False, "note": "目录非空，已跳过"})
                    continue
            except OSError as e:
                out.append({"path": p, "ok": False, "note": str(e)[:50]})
                continue
        if use_recycle and not os.path.isdir(p):
            st, note = _recycle_one(p)
            if st == "recycled":
                out.append({"path": p, "ok": True, "note": "已移入回收站（可还原）"})
                continue
            if st == "gone":
                out.append({"path": p, "ok": True, "note": note})
                continue
        try:
            if os.path.isdir(p):
                os.rmdir(p)                   # 只删空目录，绝不 rmtree
            else:
                os.remove(p)
            out.append({"path": p, "ok": True, "note": "已删除"})
        except (OSError, PermissionError) as e:
            out.append({"path": p, "ok": False, "note": "失败：%s" % str(e)[:50]})
    return out


# ==========================================================================
# 批次B：注册表清理 / 备份还原中心
#
# 铁律（与残留清理一致）：
#   ① 扫描只读；② 任何删除都先导出 .reg 备份，**备份失败直接拒绝**；
#   ③ 删除后回读复核；④ 判定宁漏勿错 —— 把活的注册表项当"无效"删掉会毁掉软件。
# ==========================================================================
def _key_exists(hive, sub):
    root = _HIVE_KEYS.get(hive) if winreg else None
    if root is None or not sub:
        return False
    try:
        with winreg.OpenKey(root, sub, 0, winreg.KEY_READ):
            return True
    except OSError:
        return False


def reg_delete_tree(hive, sub):
    """递归删除整棵子树，返回删掉的键数。

    `winreg.DeleteKey` **只能删空键** —— 父键下有子键时直接报错，
    于是「删了父键」的清理代码其实什么都没删掉（测试残留就是这么来的）。
    ⚠️ 危险操作：只给「清掉本软件自己造的测试键」这类用途。
    """
    root = _HIVE_KEYS.get(hive) if winreg else None
    if root is None or not sub:
        return 0
    n = 0
    for s in _subkeys(hive, sub, cap=5000):
        n += reg_delete_tree(hive, sub + "\\" + s)
    try:
        winreg.DeleteKey(root, sub)
        n += 1
    except OSError:
        pass
    return n


# 「这次扫描有没有因为**枚举上限**而没读全」的旁记（只记，不改变任何行为）。
# 旧的每类上限只挡住「结果条数」，可枚举候选本身也被 400/600/2000 这类上限卡着 ——
# 那种截断最隐蔽：用户看不到任何提示，却真的漏了项。这里统一记下来，
# 由 `registry_issues_scan(stats=...)` 汇报给界面。
_SCAN_TRUNC = []
_SCAN_TRUNC_MAX = 200


def _note_enum_trunc(where, cap, total):
    """记一次「枚举被上限截断」。`_subkeys` / `_reg_values` 在 n > cap 时调用。"""
    if len(_SCAN_TRUNC) < _SCAN_TRUNC_MAX:
        _SCAN_TRUNC.append({"where": where, "cap": cap, "total": total})


def _reg_values(hive, sub, cap=2000):
    """读一个键的全部 (值名, 类型, 数据)。打不开返回 []。

    ⚠️ `winreg.EnumValue` 的返回是 **(名, 数据, 类型)** —— 顺序和
    `QueryValueEx` 相反。这里显式转成 (名, 类型, 数据)，直接透传会让
    所有调用点把「类型号」当成「数据」，判出一堆假无效项（踩过）。
    """
    root = _HIVE_KEYS.get(hive) if winreg else None
    if root is None or not sub:
        return []
    out = []
    try:
        with winreg.OpenKey(root, sub, 0, winreg.KEY_READ) as k:
            n = winreg.QueryInfoKey(k)[1]
            if n > cap:
                _note_enum_trunc("%s\\%s" % (hive, sub), cap, n)
            for i in range(min(n, cap)):
                try:
                    name, data, typ = winreg.EnumValue(k, i)
                except OSError:
                    continue
                out.append((name, typ, data))
    except OSError:
        return []
    return out


def _subkeys(hive, sub, cap=5000):
    root = _HIVE_KEYS.get(hive) if winreg else None
    if root is None or not sub:
        return []
    out = []
    try:
        with winreg.OpenKey(root, sub, 0, winreg.KEY_READ) as k:
            n = winreg.QueryInfoKey(k)[0]
            if n > cap:
                _note_enum_trunc("%s\\%s" % (hive, sub), cap, n)
            for i in range(min(n, cap)):
                try:
                    out.append(winreg.EnumKey(k, i))
                except OSError:
                    continue
    except OSError:
        return []
    return out


_NOT_A_PROGID = {"video", "audio", "image", "text", "document", "system",
                 "application", "folder", "directory",
                 # PerceivedType 的其它取值（Windows 文档里的一整组）：
                 # 把 `compressed` 当 ProgID 去查，`.cab` / `.gz` 必然被误报成
                 # 「默认打开程序 compressed 未注册」（实测踩过）。
                 "compressed", "developers", "game", "contact", "unknown",
                 "shortcut", "certificate", "driver", "font", "link"}


def _looks_like_progid(s):
    """`HKCR\\.ext` 的默认值有两种含义：**ProgID**（WPS.Docx.6）或
    **MIME 类型 / PerceivedType**（application/x-compressed、video）。
    把后者当 ProgID 去查注册表，必然报一堆「未注册」的假无效项。"""
    s = str(s or "").strip()
    if not s or "/" in s or " " in s or "," in s:
        return False
    return s.lower() not in _NOT_A_PROGID


# ==========================================================================
# 注册表无效项扫描：按「小类」分扫 + 分类清理
#
# 旧版是一把梭全扫、全局上限 300 —— CLSID 一类就能把名额吃满，用户看到的
# 常常只有两三类，观感就是「读不全」。现在：
#   · 每个小类**单独限量**，谁也挤不掉谁；
#   · UI 先让用户勾选要扫哪几类，扫完按类分组，也能按类清理。
# 删除依旧走 registry_issue_clean（先备份 .reg → 删 → 回读复核）。
# ==========================================================================
REG_CATEGORIES = (
    # (id, 名称, 一句话说明, 是否默认勾选, 风险档 recommend/ok/weak)
    ("com_server", "无效的 ActiveX / COM 组件",
     "COM 类登记的 DLL / EXE 已经不存在", True, "recommend"),
    ("com_ref", "无效的 COM 引用",
     "AppID / 类型库 / 代理存根指向的登记缺失", True, "ok"),
    ("filetype", "无效的文件类型关联",
     ".xxx 的默认打开程序或选定的打开方式已被卸载", True, "recommend"),
    ("shellex", "无效的右键菜单扩展",
     "右键菜单处理程序指向的 COM 类不存在", True, "ok"),
    ("apppath", "无效的应用程序路径",
     "App Paths 里登记的程序不存在", True, "ok"),
    ("sound", "无效的声音事件",
     "声音事件指定的音频文件不存在", True, "ok"),
    ("font", "无效的字体登记",
     "字体表里登记的字体文件不存在", True, "ok"),
    ("shareddll", "无效的共享 DLL 计数",
     "共享 DLL 计数指向的文件不存在", True, "ok"),
    ("startup", "无效的启动项",
     "开机启动项指向的程序不存在", True, "ok"),
    ("autostart", "无效的其他自启动位置",
     "Winlogon / AppInit_DLLs / BootExecute 等位置指向的文件不存在", True, "ok"),
    ("com_cat", "无效的组件类别",
     "组件类别（Component Categories）登记的接口不存在", True, "ok"),
    ("protocol", "无效的协议处理器",
     "自定义协议（http、steam:// 这类）指向的程序不存在", True, "ok"),
    ("installer", "无效的安装信息",
     "「已安装程序」的安装目录或卸载程序不存在", False, "weak"),
    ("service", "无效的服务 / 系统驱动",
     "服务或驱动登记的镜像文件不存在", False, "weak"),
    ("firewall", "无效的防火墙规则",
     "防火墙规则引用的程序不存在（数量多）", False, "weak"),
    ("muicache", "无效的 MUI 缓存记录",
     "程序显示名缓存指向的 exe 已不存在（数量多）", False, "weak"),
    ("history", "无效的历史记录",
     "「运行」「地址栏」「最近打开」里指向的文件已不存在", False, "weak"),
    ("com_interface", "无效的 COM 接口",
     "接口登记的代理存根类不存在（枚举近 3 万项，较慢）", False, "weak"),
)
REG_CAT_NAMES = {c[0]: c[1] for c in REG_CATEGORIES}
REG_CAT_DESC = {c[0]: c[2] for c in REG_CATEGORIES}
REG_CAT_WEAK = {c[0] for c in REG_CATEGORIES if c[4] == "weak"}
REG_CAT_DEFAULT = tuple(c[0] for c in REG_CATEGORIES if c[3])


def _reg_missing(p):
    """路径是否**确定不存在**。只要拿不准就返回 False —— 宁可漏报不误报
    （把活的注册表项当无效删掉，会毁掉别人的软件）。"""
    s = str(p or "").strip().strip('"')
    if not s or "%" in s or "*" in s or "?" in s:
        return False
    s = os.path.expandvars(s)
    if "%" in s or not (len(s) > 3 and s[1:3] == ":\\") and not s.startswith("\\\\"):
        return False
    try:
        return not os.path.exists(s)
    except (OSError, ValueError):
        return False


def _reg_exe_only(p):
    """把「可执行文件路径 + 命令行参数」切成纯路径。

    LocalServer32 的常见形态是 `D:\\App\\Photoshop.exe /Automation` 或
    `"D:\\App\\devenv.exe" -args`。拿整串去 `os.path.exists` 必然 False ——
    会把装得好好的程序报成「文件不存在」（实测就是这么误报了 8 条 Photoshop）。
    """
    s = str(p or "").strip()
    if not s:
        return ""
    m = re.match(r'^"([^"]+)"', s)
    if m:
        return m.group(1)
    # 参数分隔符不止空格：`Photoshop.exe/Automation`、`x.exe/a` 这种斜杠写法
    # 很常见（实测漏掉 → 装着的 Photoshop 被报「文件不存在」，8 条误报）。
    m = re.search(r"\.(?:exe|dll|ocx|cpl|com|sys)(?=$|[\s,;/])", s, re.I)
    if m:
        return s[:m.end()]
    return s


def _reg_scan_com(clsid_cap, limit):
    """COM 类：一次 CLSID 遍历同时产出「服务器文件不存在」与「引用缺失」两类。

    删除时只删**那一处子键/值**（如 CLSID\\{...}\\InprocServer32），不删整个
    CLSID —— 万一同一个类还有别的有效登记，删整棵就把人家弄坏了。
    """
    srv, ref = [], []
    for clsid in _subkeys("HKCR", "CLSID", clsid_cap):
        base = "CLSID\\%s" % clsid
        for sub in ("InprocServer32", "LocalServer32", "InprocHandler32"):
            for vn, _t, vd in _reg_values("HKCR", "%s\\%s" % (base, sub), 4):
                if vn != "":
                    continue
                if _reg_missing(_reg_exe_only(vd)):
                    srv.append({"kind": "invalid_com_server", "hive": "HKCR",
                                "path": "\\%s\\%s" % (base, sub), "value": "",
                                "why": "COM 类的 %s 指向的文件不存在：%s"
                                       % (sub, _reg_exe_only(vd)[:60])})
                break
        for vn, _t, vd in _reg_values("HKCR", base, 40):
            if not isinstance(vd, str) or not vd:
                continue
            v = vd.strip()
            if vn == "AppID":
                if not _key_exists("HKCR", "AppID\\%s" % v):
                    ref.append({"kind": "invalid_appid", "hive": "HKCR",
                                "path": "\\" + base, "value": vn,
                                "why": "COM 类引用的 AppID「%s」未登记" % v})
            elif vn == "TypeLib":
                if not _key_exists("HKCR", "TypeLib\\%s" % v):
                    ref.append({"kind": "invalid_typelib", "hive": "HKCR",
                                "path": "\\" + base, "value": vn,
                                "why": "COM 类引用的类型库「%s」不存在" % v})
            elif vn in ("ProxyStubClsid32", "ProxyStubClsid"):
                tgt = v.strip("{}")
                if not _key_exists("HKCR", "CLSID\\{%s}" % tgt):
                    ref.append({"kind": "invalid_proxystub", "hive": "HKCR",
                                "path": "\\" + base, "value": vn,
                                "why": "COM 代理存根指向的类不存在：{%s}" % tgt})
        if len(srv) >= limit and len(ref) >= limit:
            break
    return srv[:limit], ref[:limit]


def _hkcr_root_names(cap=8000):
    """HKCR 根下的子键名（`.ext` 与协议处理器都从这里筛，只枚举一次）。

    ⚠️ 不能用 `_subkeys("HKCR", "")` —— 那个函数对空 sub 直接返回 []（会一条都
    扫不到，实测踩过）。
    """
    out = []
    root = _HIVE_KEYS.get("HKCR") if winreg else None
    if root is None:
        return out
    try:
        with winreg.OpenKey(root, "", 0, winreg.KEY_READ) as k:
            n = winreg.QueryInfoKey(k)[0]
            for i in range(min(n, cap)):
                try:
                    out.append(winreg.EnumKey(k, i))
                except OSError:
                    break
    except OSError:
        pass
    return out


def _reg_scan_filetype(ext_cap, limit):
    """无效文件关联：.ext 的 ProgID 未注册 / 用户选定的打开方式已卸载。

    「候选打开方式（OpenWithProgids）」未注册属正常（只是备选），标 weak。
    """
    out = []
    exts = [nm for nm in _hkcr_root_names(ext_cap)
            if nm.startswith(".") and len(nm) > 1]
    for ext in exts:
        # ⚠️ 必须读**默认值**，不能「枚举第一个值」。`EnumValue` 是按索引来的，
        # `.cab` 的第一个值是 `PerceivedType=compressed`，被当成程序名去查 →
        # 报「默认打开程序 compressed 未注册」（实测的假阳性）。
        ex, _t, vd = reg_read("HKCR\\%s" % ext, "")
        if ex:
            pg = str(vd or "").strip()
            if _looks_like_progid(pg) and not _key_exists("HKCR", "\\%s" % pg):
                out.append({"kind": "invalid_filetype", "hive": "HKCR",
                            "path": "\\%s" % ext, "value": "", "weak": False,
                            "why": "文件类型 .%s 的默认打开程序「%s」未注册"
                                   % (ext[1:], pg)})
        uc = "\\FileExts\\%s\\UserChoice" % ext
        ex, _t, ud = reg_read("HKCR" + uc, "ProgId")
        if ex and isinstance(ud, str) and ud and not _key_exists("HKCR", "\\%s" % ud):
            out.append({"kind": "invalid_filetype", "hive": "HKCR",
                        "path": uc, "value": "ProgId", "weak": False,
                        "why": "文件类型 .%s 选定的打开方式「%s」已卸载"
                               % (ext[1:], ud)})
        for pn, _t, pd in _reg_values("HKCR", "\\%s\\OpenWithProgids" % ext, 40):
            if not pd or not pn or _key_exists("HKCR", "\\%s" % pn):
                continue
            out.append({"kind": "invalid_filetype_openwith", "hive": "HKCR",
                        "path": "\\%s\\OpenWithProgids" % ext, "value": pn,
                        "weak": True,
                        "why": "文件类型 .%s 的候选打开方式「%s」未注册"
                               % (ext[1:], pn)})
        if len(out) >= limit:
            break
    return out[:limit]


_SHELLEX_ROOTS = (
    r"*\shellex\ContextMenuHandlers", r"*\shellex\PropertySheetHandlers",
    r"AllFilesystemObjects\shellex\ContextMenuHandlers",
    r"Directory\shellex\ContextMenuHandlers",
    r"Directory\Background\shellex\ContextMenuHandlers",
    r"Directory\shellex\PropertySheetHandlers",
    r"Folder\shellex\ContextMenuHandlers",
    r"Drive\shellex\ContextMenuHandlers",
    r"Drive\shellex\PropertySheetHandlers",
    r"LibraryFolder\shellex\ContextMenuHandlers",
    r"lnkfile\shellex\ContextMenuHandlers",
)


def _reg_scan_shellex(limit):
    """右键菜单扩展：处理程序的 CLSID 整条不存在（不是「服务器文件丢了」，
    那种已被 com_server 覆盖）。"""
    out = []
    for root in _SHELLEX_ROOTS:
        for name in _subkeys("HKCR", root, 400):
            for vn, _t, vd in _reg_values("HKCR", "%s\\%s" % (root, name), 2):
                if vn != "":
                    continue
                clsid = str(vd or "").strip()
                if clsid.startswith("{") and not _key_exists("HKCR", "CLSID\\%s" % clsid):
                    out.append({"kind": "invalid_shellex", "hive": "HKCR",
                                "path": "\\%s\\%s" % (root, name), "value": "",
                                "why": "右键菜单处理程序指向的 COM 类不存在：%s"
                                       % clsid})
                break
            if len(out) >= limit:
                return out
    return out


_APPPATH_KEYS = (
    ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
    ("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths"),
    ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
)


def _reg_scan_apppath(limit):
    out = []
    for hive, base in _APPPATH_KEYS:
        for name in _subkeys(hive, base, 600):
            for vn, _t, vd in _reg_values(hive, "%s\\%s" % (base, name), 4):
                if vn != "":
                    continue
                if _reg_missing(_reg_exe_only(vd)):
                    out.append({"kind": "invalid_apppath", "hive": hive,
                                "path": "\\%s\\%s" % (base, name), "value": "",
                                "why": "App Paths 登记的「%s」已不存在：%s"
                                       % (name, _reg_exe_only(vd)[:56])})
                break
            if len(out) >= limit:
                return out
    return out


def _reg_scan_sound(limit):
    """声音事件：HKCU\\AppEvents\\Schemes\\Apps\\<程序>\\<事件>\\<方案>\\(默认) = .wav"""
    out = []
    base = r"AppEvents\Schemes\Apps"
    for app in _subkeys("HKCU", base, 200):
        a = "%s\\%s" % (base, app)
        for ev in _subkeys("HKCU", a, 100):
            e = "%s\\%s" % (a, ev)
            for sch in _subkeys("HKCU", e, 20):
                s2 = "%s\\%s" % (e, sch)
                for vn, _t, vd in _reg_values("HKCU", s2, 2):
                    if vn != "":
                        continue
                    if _reg_missing(vd):
                        out.append({"kind": "invalid_sound", "hive": "HKCU",
                                    "path": "\\" + s2, "value": "",
                                    "why": "声音事件指定的音频文件不存在：%s"
                                           % str(vd)[:56]})
                    break
                if len(out) >= limit:
                    return out
    return out


def _reg_scan_font(limit):
    """字体登记：值名是显示名，数据是文件名（可能只有 basename）。"""
    out = []
    key = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
    fonts = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts")
    for vn, _t, vd in _reg_values("HKLM", key, 2000):
        if not isinstance(vd, str) or not vd.strip():
            continue
        f = vd.strip()
        if len(f) > 3 and f[1:3] == ":\\":
            p = f
        elif "\\" in f:
            p = os.path.join(fonts, os.path.basename(f))
        else:
            p = os.path.join(fonts, f)
        if _reg_missing(p):
            out.append({"kind": "invalid_font", "hive": "HKLM",
                        "path": "\\" + key, "value": vn,
                        "why": "字体表登记的「%s」文件不存在" % f[:52]})
        if len(out) >= limit:
            break
    return out


def _reg_scan_shareddll(limit):
    """共享 DLL 计数：值名就是文件全路径。"""
    out = []
    key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\SharedDLLs"
    for vn, _t, _vd in _reg_values("HKLM", key, 3000):
        if _reg_missing(vn):
            out.append({"kind": "invalid_shareddll", "hive": "HKLM",
                        "path": "\\" + key, "value": vn,
                        "why": "共享 DLL 计数指向的文件不存在：%s" % str(vn)[:60]})
        if len(out) >= limit:
            break
    return out


def _reg_scan_startup(limit):
    """无效启动项（复用残留扫描那套：只认绝对路径且确实不存在的）。"""
    out = []
    for it in _orphan_startup(limit):
        out.append({"kind": "invalid_startup", "hive": it["hive"],
                    "path": it["path"], "value": it["value"], "weak": False,
                    "why": "启动项「%s」指向的程序已不存在" % it["value"]})
    return out


def _reg_scan_installer(limit):
    """「已安装程序」记录指向的安装目录 / 卸载程序不存在。

    ⚠️ weak：删掉这条记录，程序会从「已安装」列表里消失。对已被用户手工
    删掉目录的程序这是"清理"，对装在别处（U 盘、绿色路径）的程序就是误伤，
    所以默认不勾，由用户自己确认。
    """
    out = []
    for hive in ("HKLM", "HKCU"):
        for sub in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            for name in _subkeys(hive, sub, 900):
                vals = {}
                for n, _t, d in _reg_values(hive, "%s\\%s" % (sub, name), 40):
                    vals[n.lower()] = d
                loc = str(vals.get("installlocation") or "").strip().strip('"')
                us = str(vals.get("uninstallstring") or "").strip()
                dn = str(vals.get("displayname") or name)
                why = ""
                # MSI 管理的记录一律不碰：删掉 `Uninstall\{GUID}` 会让 MSI 数据库
                # 对不上（程序再也无法修复/卸载），而它本来就由 MSI 自己维护。
                msi = (str(vals.get("windowsinstaller") or "").strip() == "1"
                       or "msiexec" in us.lower())
                if not msi:
                    if loc and _reg_missing(loc):
                        why = "安装目录已不存在：%s" % loc[:56]
                    elif us:
                        exe, _a = parse_uninstall_cmd(us)
                        if exe and os.path.isabs(exe) and _reg_missing(exe):
                            why = "卸载程序已不存在：%s" % exe[:56]
                if why:
                    out.append({"kind": "invalid_installer", "hive": hive,
                                "path": "\\%s\\%s" % (sub, name), "value": "",
                                "weak": True,
                                "why": "已装程序「%s」%s" % (dn[:36], why)})
                if len(out) >= limit:
                    return out
    return out


def _reg_scan_service(limit):
    """服务镜像文件不存在。weak：删服务记录会影响系统行为，默认不勾。"""
    out = []
    base = r"SYSTEM\CurrentControlSet\Services"
    for name in _subkeys("HKLM", base, 1200):
        for vn, _t, vd in _reg_values("HKLM", "%s\\%s" % (base, name), 40):
            if vn != "ImagePath":
                continue
            exe = _svc_exe(vd)
            if exe and _reg_missing(exe):
                out.append({"kind": "invalid_service", "hive": "HKLM",
                            "path": "\\%s\\%s" % (base, name), "value": "",
                            "weak": True,
                            "why": "服务「%s」的镜像文件不存在：%s"
                                   % (name[:28], exe[:56])})
            break
        if len(out) >= limit:
            break
    return out


def _reg_scan_firewall(limit):
    """防火墙规则里的 App= 路径不存在。weak：实测一台机器能扫出几百条。"""
    out = []
    fw = (r"SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters"
          r"\FirewallPolicy\FirewallRules")
    for rn, _rt, rd in _reg_values("HKLM", fw, 3000):
        if not isinstance(rd, str) or "App=" not in rd:
            continue
        try:
            app = rd.split("App=", 1)[1].split(";", 1)[0].strip().strip('"')
        except IndexError:
            continue
        if not app or not ("\\" in app or ":" in app):
            continue
        p = os.path.expandvars(app)
        if "%" in p:
            continue
        try:
            if os.path.exists(p):
                continue
        except OSError:
            continue
        out.append({"kind": "invalid_firewall", "hive": "HKLM",
                    "path": "\\" + fw, "value": rn, "weak": True,
                    "why": "防火墙规则引用的程序不存在：%s" % p[:60]})
        if len(out) >= limit:
            break
    return out


def _reg_scan_muicache(limit):
    """MUI 缓存：值名形如 `C:\\...\\foo.exe.ApplicationCompany`，程序没了就该清。
    weak：纯显示名缓存，删了顶多让「应用和功能」里短暂显示成路径。"""
    out = []
    key = (r"Software\Classes\Local Settings\Software\Microsoft\Windows"
           r"\Shell\MuiCache")
    for vn, _t, _vd in _reg_values("HKCU", key, 4000):
        p = str(vn or "")
        low = p.lower()
        if not (low.endswith(".exe.applicationcompany")
                or low.endswith(".exe.friendlyappname")):
            continue
        exe = p.rsplit(".", 1)[0]
        if _reg_missing(exe):
            out.append({"kind": "invalid_muicache", "hive": "HKCU",
                        "path": "\\" + key, "value": vn, "weak": True,
                        "why": "显示名缓存指向的程序不存在：%s" % exe[:56]})
        if len(out) >= limit:
            break
    return out


def _reg_scan_autostart(limit, _ext_cap=None):
    """Run/RunOnce 以外的自启动位置 —— 这些地方常年被忽略，恰恰是残留高发区。

    覆盖：Winlogon 的 Userinit / Shell / AppSetup、Windows 的 AppInit_DLLs /
    Load / Run、Session Manager 的 BootExecute、Policies\\Explorer\\Run。
    `STARTUP_LOCATIONS`（Run/RunOnce）不动 —— 它同时被残留清理与启动项页用着，
    改它会连带改变那两个功能的口径，这里只做**追加**。
    """
    out = []
    winnt = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    locs = (
        ("HKLM", winnt + r"\Winlogon", ("Userinit", "Shell", "AppSetup")),
        ("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows NT\CurrentVersion\Winlogon",
         ("Userinit", "Shell", "AppSetup")),
        ("HKLM", winnt + r"\Windows", ("AppInit_DLLs", "Load", "Run")),
        ("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows NT\CurrentVersion\Windows",
         ("AppInit_DLLs", "Load", "Run")),
        ("HKCU", winnt + r"\Windows", ("Load", "Run")),
        ("HKLM", r"SYSTEM\CurrentControlSet\Control\Session Manager", ("BootExecute",)),
        ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Explorer\Run", None),
        ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Explorer\Run", None),
    )
    for hive, key, names in locs:
        for vn, _t, vd in _reg_values(hive, key, 40):
            if names is not None and vn not in names:
                continue
            # BootExecute 是 MULTI_SZ（列表）
            items = vd if isinstance(vd, (list, tuple)) else [vd]
            for raw in items:
                s = str(raw or "").strip()
                if not s or "%" in s:
                    continue
                for part in s.split(","):
                    exe, _a = parse_uninstall_cmd(part.strip())
                    if not exe:
                        continue
                    p = _reg_exe_only(part.strip()) or exe
                    if os.path.isabs(p) and _reg_missing(p):
                        out.append({"kind": "invalid_autostart", "hive": hive,
                                    "path": "\\%s" % key,
                                    "value": vn if len(items) == 1 else "",
                                    "weak": False,
                                    "why": "自启动位置「%s\\%s」指向的文件不存在：%s"
                                           % (key.rsplit("\\", 1)[-1], vn, p[:52])})
            if len(out) >= limit:
                return out
    return out


def _reg_scan_com_cat(limit, _ext_cap=None):
    """组件类别（Component Categories）：两个方向都查。

    ① `HKCR\\Component Categories\\{cat}` 里登记的接口 CLSID 不存在；
    ② `HKCR\\CLSID\\{cls}\\Implemented Categories\\{cat}` 指向的类别键不存在。
    """
    out = []
    base = "Component Categories"
    for cat in _subkeys("HKCR", base, 400):
        for vn, _t, vd in _reg_values("HKCR", "%s\\%s" % (base, cat), 300):
            if vn in ("", "CATID"):
                continue
            v = str(vd or "").strip()
            if v.startswith("{") and not _key_exists("HKCR", "Interface\\%s" % v):
                out.append({"kind": "invalid_com_cat", "hive": "HKCR",
                            "path": "\\%s\\%s" % (base, cat), "value": vn,
                            "why": "组件类别登记的接口不存在：%s" % v})
            if len(out) >= limit:
                return out
    for clsid in _subkeys("HKCR", "CLSID", 400):
        sub = "CLSID\\%s\\Implemented Categories" % clsid
        for cat in _subkeys("HKCR", sub, 40):
            if not _key_exists("HKCR", "%s\\%s" % (base, cat)):
                # weak：本机实测 `{000C0118-...}`（CATID_Control）这类标准类别
                # 不在 Component Categories 里，「声明了但没登记」未必是残留
                out.append({"kind": "invalid_com_cat", "hive": "HKCR",
                            "path": "\\%s" % sub, "value": "", "weak": True,
                            "why": "COM 类声明的组件类别没登记：%s（可能只是系统"
                                   "类别没写在注册表里）" % cat})
            if len(out) >= limit:
                return out
    return out


# 协议处理器的判定：HKCR 根下**不以 . 开头**、且带 `URL Protocol` 子键的项
def _reg_scan_protocol(limit, ext_cap=None):
    out = []
    for name in _hkcr_root_names(ext_cap or 8000):
        if name.startswith(".") or name.lower() in _NOT_A_PROTOCOL:
            continue
        if not _key_exists("HKCR", "%s\\URL Protocol" % name):
            continue                      # 不是协议处理器
        for vn, _t, vd in _reg_values("HKCR", "%s\\shell\\open\\command" % name, 2):
            if vn != "":
                continue
            cz = str(vd or "").strip()
            exe = _reg_exe_only(cz)
            if exe and os.path.isabs(exe) and _reg_missing(exe):
                out.append({"kind": "invalid_protocol", "hive": "HKCR",
                            "path": "\\%s\\shell\\open\\command" % name, "value": "",
                            "weak": False,
                            "why": "协议 %s:// 指向的程序已不存在：%s"
                                   % (name, exe[:52])})
            break
        if len(out) >= limit:
            return out
    return out


_NOT_A_PROTOCOL = {"clsid", "appid", "interface", "typelib", "component categories",
                   "directory", "drive", "folder", "help", "mime", "wow6432node",
                   "installer", "licenses", "local settings", "applications"}


def _reg_scan_history(limit, _ext_cap=None):
    """历史记录：运行框（RunMRU）、地址栏（TypedPaths）里指向已不存在文件的项。"""
    out = []
    locs = (
        ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Explorer\RunMRU",
         "「运行」记录"),
        ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Explorer\TypedPaths",
         "地址栏记录"),
    )
    for hive, key, who in locs:
        for vn, _t, vd in _reg_values(hive, key, 200):
            if vn.lower() in ("mrulist", ""):
                continue
            s = str(vd or "").strip()
            if not s or "%" in s:
                continue
            p = _reg_exe_only(s) or s
            if os.path.isabs(p) and _reg_missing(p):
                out.append({"kind": "invalid_history", "hive": hive,
                            "path": "\\%s" % key, "value": vn, "weak": True,
                            "why": "%s 指向的文件已不存在：%s" % (who, p[:52])})
            if len(out) >= limit:
                return out
    return out


def _reg_scan_interface(limit, _ext_cap=None):
    """COM 接口：`HKCR\\Interface\\{IID}\\ProxyStubClsid32` 指向的类不存在。
    近 3 万项，**默认不勾**（慢）。"""
    out = []
    for iid in _subkeys("HKCR", "Interface", 30000):
        for sub in ("ProxyStubClsid32", "ProxyStubClsid"):
            for vn, _t, vd in _reg_values("HKCR", "Interface\\%s\\%s" % (iid, sub), 2):
                if vn != "":
                    continue
                tgt = str(vd or "").strip().strip("{}")
                if tgt and not _key_exists("HKCR", "CLSID\\{%s}" % tgt):
                    out.append({"kind": "invalid_interface", "hive": "HKCR",
                                "path": "\\Interface\\%s\\%s" % (iid, sub),
                                "value": "", "weak": True,
                                "why": "接口的代理存根类不存在：{%s}" % tgt})
                break
        if len(out) >= limit:
            return out
    return out


def registry_issues_scan(kinds=None, clsid_cap=20000, ext_cap=4000, cap=6000,
                         per_cat=400, should_stop=None, stats=None):
    """按**小类**扫描注册表里的无效引用。只读。

    `kinds` 为 None 或空时扫全部分类；否则只扫指定的（UI 先让用户勾选）。
    每项都带：
      cat   — 分类 id（`REG_CAT_NAMES` 里能查到中文名）
      kind  — 细分类型（UI 的「类型」列）
      weak  — 低置信（UI 默认不勾）
    每个分类单独限量（`per_cat`），避免某一类刷屏把别的类挤没 —— 这正是
    旧版「读不全」的根因之一（全局 cap=300，CLSID 一类就吃满了）。

    ⚠️ `per_cat <= 0` 表示**不限**（选了某一类就该看到它的全部，而不是被 400 截掉）；
       `cap <= 0` 同理表示全局不限。
    ⚠️ `stats`（可选 dict 出参）回填「读没读全」的实情 —— 截断了就如实说：
       per_cat / unlimited / cap / total / shown / truncated /
       capped_cats（正好卡在上限的分类）/ enum_trunc（枚举阶段被截断的位置）
    """
    lim = per_cat if isinstance(per_cat, int) and per_cat > 0 else 0
    eff = lim or 10 ** 9                     # 不限 = 给个永远达不到的上限
    cap_eff = cap if isinstance(cap, int) and cap > 0 else 10 ** 9
    _SCAN_TRUNC.clear()                      # 只记本次扫描的枚举截断
    want = [k for k in (kinds or []) if k in REG_CAT_NAMES] or list(REG_CAT_NAMES)
    com = None                       # COM 那一遍遍历同时产两类，只跑一次
    out = []
    capped = {}
    for cat in want:
        if should_stop is not None and should_stop():
            break                    # 用户点了「暂停扫描」→ 保留已扫到的，立即收工
        try:
            if cat in ("com_server", "com_ref"):
                if com is None:
                    com = _reg_scan_com(clsid_cap, eff)
                items = com[0] if cat == "com_server" else com[1]
            elif cat == "filetype":
                items = _reg_scan_filetype(ext_cap, eff)
            elif cat == "protocol":
                items = _reg_scan_protocol(eff, ext_cap)
            else:
                items = _REG_SCANNERS[cat](eff)
        except KeyError:
            continue
        except Exception:
            items = []               # 单个分类出错不影响其它分类
        if lim and len(items) >= lim:
            # 正好等于上限 → 极可能被截断（也可能恰好这么多，所以文案写「只多不少」）
            capped[cat] = len(items)
        for x in items:
            x["cat"] = cat
            x.setdefault("weak", cat in REG_CAT_WEAK or bool(x.get("weak")))
            out.append(x)
    total = len(out)
    final = out[:cap_eff]
    if stats is not None:
        stats.clear()
        stats.update({"per_cat": per_cat, "unlimited": lim == 0,
                      "cap": cap, "total": total, "shown": len(final),
                      "truncated": total > len(final),
                      "capped_cats": dict(capped),
                      "enum_trunc": [dict(x) for x in _SCAN_TRUNC]})
    return final


_REG_SCANNERS = {
    "shellex": _reg_scan_shellex,
    "apppath": _reg_scan_apppath,
    "sound": _reg_scan_sound,
    "font": _reg_scan_font,
    "shareddll": _reg_scan_shareddll,
    "startup": _reg_scan_startup,
    "autostart": _reg_scan_autostart,
    "com_cat": _reg_scan_com_cat,
    "com_interface": _reg_scan_interface,
    "history": _reg_scan_history,
    "installer": _reg_scan_installer,
    "service": _reg_scan_service,
    "firewall": _reg_scan_firewall,
    "muicache": _reg_scan_muicache,
}


def registry_issue_stats(issues):
    """按分类汇总 (分类 id, 中文名, 条数, 其中可勾选条数)，按条数降序。"""
    cnt = {}
    for x in issues or []:
        c = x.get("cat") or "other"
        e = cnt.setdefault(c, [0, 0])
        e[0] += 1
        if not x.get("weak"):
            e[1] += 1
    rows = [(c, REG_CAT_NAMES.get(c, c), v[0], v[1]) for c, v in cnt.items()]
    rows.sort(key=lambda r: -r[2])
    return rows
def registry_issue_clean(items, stamp=None):
    """清理选中的无效注册表项：**先备份 → 再删 → 回读复核**，逐条如实报告。"""
    stamp = stamp or time.strftime("%Y%m%d_%H%M%S")
    out = []
    for it in items or []:
        hive, path = it.get("hive"), (it.get("path") or "")
        value = it.get("value")
        rec = {"kind": it.get("kind"), "why": it.get("why"),
               "hive": hive, "path": path, "value": value, "ok": False, "note": ""}
        if hive not in ("HKLM", "HKCU", "HKCR") or not path:
            rec["note"] = "拒绝（不受支持的 hive 或空路径）"
            out.append(rec)
            continue
        bk = _reg_backup(hive, path, stamp)          # 备份失败就不删
        if not bk:
            rec["note"] = "跳过（备份失败，未删除）"
            out.append(rec)
            continue
        rec["backup"] = bk
        try:
            ok = reg_delete(hive + path, value)
        except Exception as e:
            rec["note"] = "删除异常：%s" % str(e)[:60]
            out.append(rec)
            continue
        if not ok:
            rec["note"] = "删除失败"
            out.append(rec)
            continue
        # 回读复核：值/键真的没了吗
        ex, _t, _d = reg_read(hive + path, value if value else "")
        if value and ex:
            rec["note"] = "复核失败：值仍存在"
            out.append(rec)
            continue
        rec["ok"] = True
        rec["note"] = "已删除（备份 %s）" % os.path.basename(bk)
        out.append(rec)
    return out


# --------------------------------------------------------------------------
# 备份还原中心：把我们导出的 .reg 读回来（自己解析，不依赖 reg.exe / regedit）
# --------------------------------------------------------------------------
_REG_HIVE_BY_NAME = {
    "HKEY_LOCAL_MACHINE": "HKLM", "HKEY_CURRENT_USER": "HKCU",
    "HKEY_CLASSES_ROOT": "HKCR", "HKEY_USERS": "HKU",
}
_REG_TYPE_BY_NAME = {
    "REG_SZ": "REG_SZ", "REG_EXPAND_SZ": "REG_EXPAND_SZ",
    "REG_MULTI_SZ": "REG_MULTI_SZ", "REG_BINARY": "REG_BINARY",
    "REG_DWORD": "REG_DWORD", "REG_QWORD": "REG_QWORD",
}


def _unreg_escape(s):
    return s.replace("\\\\", "\x00ESC\x00").replace('\\"', '"').replace("\x00ESC\x00", "\\")


def parse_reg_file(path):
    """解析 .reg 文本（我们自己生成的那种格式）→ [{hive, path, name, type, data}]。"""
    if not os.path.isfile(path):
        return []
    try:
        raw = open(path, "r", encoding="utf-16").read()
    except (OSError, UnicodeError):
        try:
            raw = open(path, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            return []
    out = []
    cur = None
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith(";") or line.startswith("Windows Registry"):
            continue
        m = re.match(r"^\[(-?)([A-Z_]+)\\(.+)\]$", line)
        if m:
            hive = _REG_HIVE_BY_NAME.get(m.group(2))
            if not hive:
                cur = None
                continue
            cur = {"hive": hive, "path": m.group(3), "vals": []}
            out.append(cur)
            continue
        if cur is None or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        name = "" if k == "@" else _unreg_escape(k.strip('"'))
        v = v.strip()
        if v.startswith('"') and v.endswith('"') and '"' not in v[1:-1]:
            cur["vals"].append((name, "REG_SZ", _unreg_escape(v[1:-1])))
        elif v.startswith("dword:"):
            try:
                cur["vals"].append((name, "REG_DWORD", int(v[6:], 16)))
            except ValueError:
                pass
        elif v.startswith("qword:"):
            try:
                cur["vals"].append((name, "REG_QWORD", int(v[6:], 16)))
            except ValueError:
                pass
        elif v.startswith("hex"):
            hx = v.split(":", 1)[1] if ":" in v else ""
            hx = re.sub(r"[^0-9A-Fa-f]", "", hx)
            if len(hx) % 2:
                hx += "0"
            b = bytes.fromhex(hx)
            if v.startswith("hex(2)"):
                s = b.decode("utf-16-le", "replace").rstrip("\x00")
                cur["vals"].append((name, "REG_EXPAND_SZ", s))
            elif v.startswith("hex(7)"):
                parts = [x.decode("utf-16-le", "replace")
                         for x in b.split(b"\x00\x00") if x]
                cur["vals"].append((name, "REG_MULTI_SZ", parts[:-1] if parts else []))
            elif v.startswith("hex(b)"):
                cur["vals"].append((name, "REG_QWORD",
                                    int.from_bytes(b[:8].ljust(8, b"\x00"), "little")))
            else:
                cur["vals"].append((name, "REG_BINARY", b))
    return out


def reg_backups_list(cap=60):
    """列出可还原的 .reg 备份（策略清理 + 卸载残留清理产生的）。"""
    out = []
    for d, kind in ((os.path.join(BACKUP, "uninstall"), "卸载残留备份"),
                    (BACKUP, "策略备份")):
        if not os.path.isdir(d):
            continue
        for nm in os.listdir(d):
            if not nm.lower().endswith(".reg"):
                continue
            fp = os.path.join(d, nm)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            out.append({"path": fp, "name": nm, "kind": kind,
                        "size": st.st_size,
                        "mtime": time.strftime("%Y-%m-%d %H:%M:%S",
                                               time.localtime(st.st_mtime))})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out[:cap]


def reg_backup_restore(path):
    """把一个 .reg 备份写回注册表，并**逐个值回读复核**。返回逐项结果。

    ⚠️ 两个曾经的坑：
      · 按「键」计数 —— `CreateKeyEx` 成功就算一个成功，可空值的键
        （`[-HKEY_...]` 删除标记这类）**一个值都没写**也照样算成功 → 谎报；
      · 类型不认识的值直接 `continue` 静默跳过 —— 用户以为还原了，其实没有。
    现在只按**真的回读一致的值**计数，跳过与失败都写进结果里。
    """
    p = os.path.abspath(path)
    bdir = os.path.abspath(BACKUP)
    if not p.lower().startswith(bdir.lower()):       # 防路径穿越：只认 backup 目录里的
        return {"ok": False, "msg": "拒绝：只允许还原 backup 目录内的备份", "keys": []}
    if not os.path.isfile(p):
        return {"ok": False, "msg": "备份文件不存在：%s" % os.path.basename(p), "keys": []}
    keys = parse_reg_file(p)
    if not keys:
        return {"ok": False, "msg": "备份文件解析失败或为空", "keys": []}
    n_key, n_val, fail, skipped = 0, 0, [], []
    for k in keys:
        root = _HIVE_KEYS.get(k["hive"])
        if root is None:
            fail.append("%s（根不支持）" % k["hive"])
            continue
        try:
            # 同时要读权限：写完要回读复核
            with winreg.CreateKeyEx(root, k["path"], 0,
                                    winreg.KEY_SET_VALUE | winreg.KEY_READ) as kk:
                wrote = 0
                for name, typ, data in k["vals"]:
                    tname = _REG_TYPE_BY_NAME.get(typ)
                    wt = getattr(winreg, tname, None) if tname else None
                    if wt is None:
                        skipped.append("%s（类型 %s 不认识）" % (name or "@", typ))
                        continue
                    winreg.SetValueEx(kk, name, 0, wt, data)
                    # 回读复核：不信「没抛异常就是写成功」
                    try:
                        got = winreg.QueryValueEx(kk, name)[0]
                    except OSError:
                        got = None
                    if got == data or str(got) == str(data):
                        wrote += 1
                    else:
                        fail.append("%s（回读不一致）" % (name or "@"))
                n_key += 1
                n_val += wrote
        except OSError as e:
            fail.append("%s\\%s（%s）" % (k["hive"], k["path"][:40], str(e)[:40]))
        except TypeError as e:               # 值类型不匹配（winreg 抛的是 TypeError）
            fail.append("%s\\%s（值类型不匹配：%s）"
                        % (k["hive"], k["path"][:40], str(e)[:40]))
    msg = "已还原 %d 个键 / %d 个值" % (n_key, n_val)
    if skipped:
        msg += "；%d 个值类型不认识已跳过（%s）" % (len(skipped), skipped[0][:50])
    if fail:
        msg += "；%d 个失败（%s）" % (len(fail), "；".join(fail[:3]))
    # ⚠️ 只有**真的写进去至少一个值**才算成功
    return {"ok": n_val > 0, "keys": n_key, "vals": n_val,
            "failed": fail, "skipped": skipped, "msg": msg}


# --------------------------------------------------------------------------
# 配置迁移：软件自有的注册表状态（HKCU\Software\WinToolbox 整棵子树）
#
# 为什么要有这个：导出的 settings.json 只覆盖「外观 / 偏好」，而右键菜单禁用、
# 系统组件禁用这些**功能状态存在注册表里** —— 换电脑只导入 settings.json，
# 这些状态就丢了（用户反馈「导入了但没把所有设置恢复过来」）。
#
# 做法：整棵子树 dump 成可 JSON 的结构，导入时按原结构写回 + 逐值回读复核。
#   为什么不做成「逐个功能各自的导出」：以后新增一个把状态写进这里的开关，
#   不用再改这段代码。
# --------------------------------------------------------------------------
_OWN_ROOT = r"Software\WinToolbox"
# ---- 「本工具禁用了什么」的记录 -------------------------------------------
# 必须在 `_OWN_ROOT` 子树下 —— 这样 `own_state_export()` 会自动把它带进配置档案。
#
# 为什么需要它：禁用**启动项 / 服务 / 计划任务**改的是 **Windows 自己的状态**
# （`StartupApproved` 标志 / 服务 `Start=<n>` / 任务的 Enabled），系统里**没有**
# 任何「这是谁干的」的线索。换台机器时那边只会看到「都是启用的」——
# 这是「配置档案覆盖不全」的另一处缺口（与右键菜单禁用同理）。
#
# ⚠️ 记录**不是**系统状态本身，只是「把这个动作再做一次」的凭据；
#    导入时按本机实况算差集，本机已经禁用的不重复提示。
_REC_ROOT = _OWN_ROOT + r"\DisabledByTool"
_REC_KINDS = ("startup", "service", "task")
_REC_MAX = 300
# Defender 的 4 个计划任务由它自己那套快照/还原逻辑管，且属**本机**状态 →
# 不进这里的通用记录，免得换台机器上莫名去禁 Windows Defender 的任务。
_REC_SKIP_TASKS = (r"\Microsoft\Windows\Windows Defender",)


def _rec_ident(kind, d):
    """一条记录的身份（同一种动作只留一条）。"""
    if kind == "startup":
        return "%s|%s|%s|%s" % (d.get("hive"), d.get("sub"), d.get("name"),
                                1 if d.get("folder") else 0)
    if kind == "service":
        return str(d.get("name") or "")
    return str(d.get("full") or "")


def _rec_label(kind, d):
    if kind == "startup":
        return "%s（%s）" % (d.get("name"), d.get("hive"))
    if kind == "task":
        return str(d.get("full") or "").rsplit("\\", 1)[-1]
    return str(d.get("name") or "")


def disabled_record_label(kind, d):
    """给界面用的条目名（公开别名，别让 app 去碰下划线函数）。"""
    return _rec_label(kind, d)


def disabled_records(kind=None):
    """读记录：`{kind: [dict, ...]}`，每个 dict 带 `kind` 字段。"""
    out = {}
    for k in _REC_KINDS:
        if kind is not None and k != kind:
            continue
        items = []
        for _nm, _typ, data in _reg_values("HKCU", "%s\\%s" % (_REC_ROOT, k),
                                           _REC_MAX):
            try:
                d = json.loads(data)
            except (TypeError, ValueError):
                continue          # 读不动的条目跳过，不猜
            if isinstance(d, dict):
                d["kind"] = k
                items.append(d)
        out[k] = items
    return out


def _rec_save(kind, items):
    """整类重写（条目只有几条，重写最省心，也避免残留旧下标）。

    ⚠️ `reg_write` / `reg_delete` 收的是**带 hive 的完整路径**（`HKCU\\...`），
    而 `_reg_values` 是 `(hive, sub)` 两段式 —— 两者别混（混了会静默写不进去）。
    """
    full = "HKCU\\%s\\%s" % (_REC_ROOT, kind)
    for i in range(min(len(items) + 8, _REC_MAX)):
        try:
            reg_delete(full, str(i))
        except (OSError, ValueError):
            pass
    for i, d in enumerate(items):
        payload = dict((k, v) for k, v in d.items() if k != "kind")
        reg_write(full, str(i), "REG_SZ",
                  json.dumps(payload, ensure_ascii=False))
    if not items:
        # 清空后把空壳键也删掉（`reg_delete` 只删值、不删键）
        try:
            winreg.DeleteKey(_HIVE_KEYS.get("HKCU"),
                             "%s\\%s" % (_REC_ROOT, kind))
        except (OSError, AttributeError):
            pass
    return len(items)


def disabled_record_add(kind, **fields):
    """记一条「本工具禁用了它」。**记录失败绝不影响主流程**（记录是附加品）。"""
    try:
        cur = disabled_records(kind).get(kind) or []
        ident = _rec_ident(kind, fields)
        cur = [x for x in cur if _rec_ident(kind, x) != ident]
        cur.append(fields)
        _rec_save(kind, cur)
        return True
    except (OSError, TypeError, ValueError):
        return False


def disabled_record_del(kind, **fields):
    """已启用 → 记录要删掉（留着会让别处以为它还被禁着）。"""
    try:
        cur = disabled_records(kind).get(kind) or []
        ident = _rec_ident(kind, fields)
        keep = [x for x in cur if _rec_ident(kind, x) != ident]
        if len(keep) != len(cur):
            _rec_save(kind, keep)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _startup_already_off(d):
    """启动项现在是不是「已禁用」？拿不准返回 None（交给执行方自己复核）。"""
    kind = _APPR_KIND.get(d.get("sub"))
    if d.get("folder") or not kind:
        return None
    return _approved_state(kind, d.get("name")) is False


def _task_state_map():
    """{完整任务名: 状态中文}。读不到就返回空 dict（那就都得再试一次）。"""
    try:
        return dict(("%s%s" % ((t.get("path") or "\\"), t.get("name")),
                     t.get("state")) for t in (list_tasks() or []))
    except Exception:
        return {}


def disabled_records_pending(records=None):
    """按记录筛出**本机还没禁用**的那些（= 真正需要补做的）。"""
    recs = records if records is not None else disabled_records()
    pend = list(recs.get("startup") or [])
    pend = [d for d in pend if _startup_already_off(d) is not False]
    for d in (recs.get("service") or []):
        if _svc_start_value(d.get("name")) != 4:
            pend.append(d)
    tasks = recs.get("task") or []
    if tasks:
        smap = _task_state_map()
        for d in tasks:
            if smap.get(str(d.get("full"))) != "已禁用":
                pend.append(d)
    return pend


def disabled_records_apply(pending=None):
    """把「待补做」的项逐个再禁用一次。返回逐项结果，**成败如实报**。"""
    items = pending if pending is not None else disabled_records_pending()
    res = []
    for d in items:
        k = d.get("kind")
        if k == "startup":
            r = set_startup_enabled(d.get("hive"), d.get("sub"), d.get("name"),
                                    bool(d.get("folder")), on=False)
            res.append({"kind": k, "name": _rec_label(k, d),
                        "ok": bool(r.get("ok")), "note": r.get("msg", "")})
        elif k == "service":
            ok, note = set_service(d.get("name"), "disable")
            res.append({"kind": k, "name": _rec_label(k, d),
                        "ok": ok, "note": note})
        elif k == "task":
            ok, note = set_task(d.get("full"), False)
            res.append({"kind": k, "name": _rec_label(k, d),
                        "ok": ok, "note": note})
    return res
_OWN_MAX_DEPTH = 8          # 自有子树不该更深；防止异常结构把内存吃干
_OWN_MAX_KEYS = 3000        # 同上：上限明确写出来，超了就有问题

# 类型号 -> 类型名（`_reg_values` 返回的是数字）
_REG_NAME_BY_ID = {}
if winreg is not None:
    for _n in ("REG_SZ", "REG_EXPAND_SZ", "REG_MULTI_SZ", "REG_BINARY",
               "REG_DWORD", "REG_QWORD", "REG_DWORD_BIG_ENDIAN", "REG_NONE"):
        _t = getattr(winreg, _n, None)
        if _t is not None and _t not in _REG_NAME_BY_ID:
            _REG_NAME_BY_ID[_t] = _n


def _own_dump_key(path, depth, budget):
    """递归 dump 一个键成 {v: [[名,类型,数据]], k: {子键名: node}}。"""
    if depth > _OWN_MAX_DEPTH or budget[0] <= 0:
        return None
    budget[0] -= 1
    node = {"v": [], "k": {}}
    for nm, typ, data in _reg_values("HKCU", path, cap=500):
        tname = _REG_NAME_BY_ID.get(typ, "REG_SZ")
        if isinstance(data, bytes):
            # JSON 存不了 bytes → 用 hex: 前缀 + 十六进制
            node["v"].append([nm, tname, "hex:" + data.hex()])
        elif isinstance(data, (list, tuple)):
            node["v"].append([nm, tname, [str(x) for x in data]])
        else:
            node["v"].append([nm, tname, data])
    for sub in _subkeys("HKCU", path, cap=500):
        child = _own_dump_key(path + "\\" + sub, depth + 1, budget)
        if child is not None:
            node["k"][sub] = child
    if not node["v"] and not node["k"]:
        # ⚠️ 空键不导出。功能用过又被清掉时会留下空壳键（`reg_delete` 只删值、
        #    不删键），不剪掉的话：备份里全是空壳、导入端还会照建一遍空键，
        #    而且「跑完 own_state_export() 必须为空」这条断言会假失败。
        return None
    return node


def own_state_export():
    """导出软件自有的注册表状态（可 JSON）。没有就返回 {}。"""
    if winreg is None or not _key_exists("HKCU", _OWN_ROOT):
        return {}
    budget = [_OWN_MAX_KEYS]
    return _own_dump_key(_OWN_ROOT, 0, budget) or {}


PROFILE_FORMAT = 1              # 档案格式版本（旧文件没有这个标记）
PROFILE_INCLUDES = ("外观与偏好（主题 / 强调色 / 开机启动 / 关闭行为 / 更新检查等）",
                    "右键菜单禁用记录",
                    "系统组件禁用记录",
                    "系统优化**已生效项清单**（导入时可一键补齐没生效的那些）",
                    "在「启动与服务」页被禁用的启动项 / 服务 / 计划任务记录"
                    "（系统里没有「谁禁的」这种线索，靠它才能在新机器上照做）")
PROFILE_EXCLUDES = ("显卡改名备份（对应**本机**那块显卡，换机器没有意义）",
                    "Defender 原始状态快照（是**本机**的快照）",
                    "操作日志 / .reg 操作备份文件（体积大，且只对本机有意义）")


def profile_build(settings, ver=""):
    """把要迁移的东西打成一个**单文件档案**（可 JSON 化）。

    比只导 settings.json 多带了「软件自有的注册表状态」—— 那才是
    右键菜单 / 系统组件禁用这些功能状态的存放处，也是「导入后没完全恢复」的原因。
    """
    return {
        "_app": "WinToolbox",
        "_format": PROFILE_FORMAT,
        "_version": str(ver or ""),
        "_exported": time.strftime("%Y-%m-%d %H:%M:%S"),
        "_includes": list(PROFILE_INCLUDES),
        "_excludes": list(PROFILE_EXCLUDES),
        "settings": dict(settings or {}),
        "own_reg": own_state_export(),
        # 只存 id：规则库随程序走，换机器上装同版本后按 id 就能对上。
        # ⚠️ 优化项**不是**「勾选状态」而是「导出时本机真正生效的项」，
        #    所以导入端要按本机实测结果算出「还差哪些」，再让用户确认补齐。
        "optimize_applied": optimize_profile_ids(),
    }


def profile_normalize(raw):
    """把读到的文件规范化成档案结构。**兼容旧格式**。

    旧格式 = 直接把 settings 的键平铺在最外层（没有 `_app` 标记）。
    返回 None 表示「不是本软件能认的文件」。
    """
    if not isinstance(raw, dict) or not raw:
        return None
    if raw.get("_app") == "WinToolbox" or "settings" in raw or "own_reg" in raw:
        st = raw.get("settings")
        if st is not None and not isinstance(st, dict):
            return None
        return {
            "settings": dict(st or {}),
            "own_reg": raw.get("own_reg") if isinstance(raw.get("own_reg"), dict) else {},
            "optimize_applied": [str(x) for x in (raw.get("optimize_applied") or [])
                                 if isinstance(x, (str, int))],
            "_legacy": False,
            "_version": str(raw.get("_version") or ""),
        }
    # 旧格式：平铺的 settings（至少得有一个我们认识的键，免得把随便一个 json 当配置）
    known = {"dark", "theme", "accent", "startup", "close_action", "check_update",
             "junk_age_mode", "uninstall_quiet", "minimize_to_tray", "window",
             "optimize_only_safe", "update_checked_on"}
    if not (set(raw.keys()) & known):
        return None
    return {"settings": dict(raw), "own_reg": {}, "optimize_applied": [],
            "_legacy": True, "_version": ""}


def profile_backup_current(settings, ver=""):
    """导入前给**当前**配置留一份恢复副本，返回文件路径（失败返回 ""）。

    照 TweakHub 的做法：先能回来，再改东西。
    """
    try:
        os.makedirs(BACKUP, exist_ok=True)
        out = os.path.join(BACKUP, "profile_before_import_%s.json"
                           % time.strftime("%Y%m%d_%H%M%S"))
        with open(out, "w", encoding="utf-8") as f:
            json.dump(profile_build(settings, ver), f, ensure_ascii=False, indent=1)
        return out if os.path.exists(out) else ""
    except (OSError, TypeError, ValueError):
        return ""


def own_state_import(tree):
    """把 own_state_export() 的结果写回。返回 {ok, keys, vals, failed, msg}。

    ⚠️ 逐值**回读复核** —— 与 reg_backup_restore 同一条规矩：不信「没抛异常就是写成功」。
    """
    if not isinstance(tree, dict) or not tree:
        return {"ok": False, "keys": 0, "vals": 0, "failed": [],
                "msg": "档案里没有本软件的状态"}
    if winreg is None:
        return {"ok": False, "keys": 0, "vals": 0, "failed": [],
                "msg": "当前环境无法写注册表"}
    root = _HIVE_KEYS.get("HKCU")
    n_key, n_val, fail = 0, 0, []
    stack = [(_OWN_ROOT, tree)]
    guard = 0
    while stack:
        guard += 1
        if guard > _OWN_MAX_KEYS:
            fail.append("键数超过上限 %d，已停止" % _OWN_MAX_KEYS)
            break
        path, node = stack.pop()
        if not isinstance(node, dict):
            continue
        try:
            # 同时要读权限：写完要回读复核
            with winreg.CreateKeyEx(root, path, 0,
                                    winreg.KEY_SET_VALUE | winreg.KEY_READ) as kk:
                wrote = 0
                for item in (node.get("v") or []):
                    if not (isinstance(item, (list, tuple)) and len(item) == 3):
                        continue
                    nm, tname, data = item
                    wt = getattr(winreg, tname, None) if isinstance(tname, str) else None
                    if wt is None:
                        fail.append("%s（类型 %s 不认识）" % (nm or "@", tname))
                        continue
                    val = data
                    if isinstance(data, str) and data.startswith("hex:"):
                        try:
                            val = bytes.fromhex(data[4:])
                        except ValueError:
                            fail.append("%s（十六进制数据坏了）" % (nm or "@"))
                            continue
                    try:
                        winreg.SetValueEx(kk, nm, 0, wt, val)
                        got = winreg.QueryValueEx(kk, nm)[0]
                    except (OSError, TypeError) as e:
                        fail.append("%s（%s）" % (nm or "@", str(e)[:40]))
                        continue
                    if got == val or str(got) == str(val):
                        wrote += 1
                    else:
                        fail.append("%s（回读不一致）" % (nm or "@"))
                n_key += 1
                n_val += wrote
        except OSError as e:
            fail.append("%s（%s）" % (path[:50], str(e)[:40]))
            continue
        for sub, child in (node.get("k") or {}).items():
            if isinstance(child, dict):
                stack.append((path + "\\" + sub, child))
    msg = "已写回 %d 个键 / %d 个值" % (n_key, n_val)
    if fail:
        msg += "；%d 个失败（%s）" % (len(fail), "；".join(fail[:3]))
    return {"ok": n_key > 0, "keys": n_key, "vals": n_val, "failed": fail, "msg": msg}


# ==========================================================================
# 批次B（续）：右键菜单管理 / 快捷方式修复
# ==========================================================================
CM_SCOPES = (("文件", r"*\shellex\ContextMenuHandlers"),
             ("文件夹", r"Directory\shellex\ContextMenuHandlers"),
             ("文件夹背景", r"Directory\Background\shellex\ContextMenuHandlers"),
             ("驱动器", r"Drive\shellex\ContextMenuHandlers"))
CM_STORE = r"Software\WinToolbox\DisabledContextMenu"
CM_RECORD = os.path.join(BACKUP, "context_menu.json")
_WINROOT = os.environ.get("SystemRoot", r"C:\Windows").lower()


def _is_system_clsid(clsid):
    """处理器 CLSID 是否属于 Windows 自带（InprocServer32 在 system32 下）。"""
    if not clsid:
        return True
    for vn, _vt, vd in _reg_values("HKCR", "CLSID\\%s\\InprocServer32" % clsid, 2):
        if vn == "" and str(vd).lower().startswith(_WINROOT):
            return True
    return False


def context_menu_list(cap=500, show_system=False):
    """列出右键菜单（扩展）项。第三方与系统自带分开标记 —— 禁系统的没意义。"""
    out = []
    for scope, sub in CM_SCOPES:
        for nm in _subkeys("HKCR", sub, 300):
            p = "\\%s\\%s" % (sub, nm)
            disp, clsid = "", ""
            for vn, _vt, vd in _reg_values("HKCR", p, 4):
                if vn == "":
                    # shellex 项的默认值既是显示名来源，**也常是处理器 CLSID**
                    # （如 7-Zip 的 {23170F69-…}）—— 两个都要认，别写成 if/elif
                    disp = str(vd).strip()
                    if disp.startswith("{"):
                        clsid = disp
                elif isinstance(vd, str) and vd.strip().startswith("{"):
                    clsid = vd.strip()
            if _key_exists("HKCU", CM_STORE + "\\" + scope + "\\" + nm):
                continue                       # 已被本工具禁用，不重复列
            sysflag = _is_system_clsid(clsid)
            if sysflag and not show_system:
                continue
            out.append({"scope": scope, "name": nm, "display": disp or nm,
                        "clsid": clsid, "system": sysflag, "path": p})
            if len(out) >= cap:
                return out
    return out


def _copy_key(src_root, src_sub, dst_root, dst_sub):
    """递归复制一个注册表键（禁用右键菜单时用）。"""
    import winreg as _w
    with _w.OpenKey(src_root, src_sub, 0, _w.KEY_READ) as k:
        vals = [(_w.EnumValue(k, i), _w.QueryInfoKey(k)[1]) for i in range(_w.QueryInfoKey(k)[1])]
        subs = [_w.EnumKey(k, i) for i in range(_w.QueryInfoKey(k)[0])]
    with _w.CreateKeyEx(dst_root, dst_sub, 0, _w.KEY_ALL_ACCESS) as dk:
        for (name, data, typ), _n in vals:
            _w.SetValueEx(dk, name, 0, typ, data)
    for s in subs:
        _copy_key(src_root, src_sub + "\\" + s, dst_root, dst_sub + "\\" + s)


def _cm_records():
    try:
        with open(CM_RECORD, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def context_menu_disable(items):
    """禁用右键菜单项：先把整棵子树**搬到 HKCU 备份区**，再删原键（可还原）。"""
    recs = _cm_records()
    done = []
    for it in items or []:
        scope, name, path = it.get("scope"), it.get("name"), it.get("path") or ""
        sub = path.lstrip("\\")
        if not scope or not sub or not _key_exists("HKCR", sub):
            done.append({"path": path, "ok": False, "note": "键已不存在"})
            continue
        try:
            _copy_key(_HIVE_KEYS["HKCR"], sub, _HIVE_KEYS["HKCU"],
                      CM_STORE + "\\" + scope + "\\" + name)
            _delete_tree(_HIVE_KEYS["HKCR"], sub)
            if _key_exists("HKCR", sub):
                done.append({"path": path, "ok": False, "note": "删除后复核仍存在"})
                continue
        except OSError as e:
            done.append({"path": path, "ok": False, "note": "失败：%s" % str(e)[:50]})
            continue
        recs.append({"scope": scope, "name": name, "path": sub,
                     "display": it.get("display") or name,
                     "time": time.strftime("%Y-%m-%d %H:%M:%S")})
        done.append({"path": path, "ok": True, "note": "已禁用（可在本页还原）"})
    try:
        os.makedirs(BACKUP, exist_ok=True)
        with open(CM_RECORD, "w", encoding="utf-8") as f:
            json.dump(recs, f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    return done


def context_menu_disabled_list():
    """已禁用的菜单项（从 HKCU 备份区读）。"""
    out = []
    for scope in (s[0] for s in CM_SCOPES):
        for nm in _subkeys("HKCU", CM_STORE + "\\" + scope, 200):
            disp = nm
            for vn, _vt, vd in _reg_values("HKCU", CM_STORE + "\\%s\\%s" % (scope, nm), 4):
                if vn == "" and isinstance(vd, str) and vd.strip():
                    disp = vd.strip()
                    break
            out.append({"scope": scope, "name": nm, "display": disp})
    return out


def context_menu_enable(names):
    """还原被禁用的右键菜单项。"""
    out = []
    recs = _cm_records()
    for key in names or []:
        scope, _, nm = str(key).partition("|")
        scope = scope or key
        src = CM_STORE + "\\%s\\%s" % (scope, nm)
        dst_sub = None
        for r in recs:
            if r.get("scope") == scope and r.get("name") == nm:
                dst_sub = r.get("path")
                break
        if not dst_sub or not _key_exists("HKCU", src):
            out.append({"key": key, "ok": False, "note": "备份区里找不到该项"})
            continue
        try:
            _copy_key(_HIVE_KEYS["HKCU"], src, _HIVE_KEYS["HKCR"], dst_sub)
            _delete_tree(_HIVE_KEYS["HKCU"], src)
        except OSError as e:
            out.append({"key": key, "ok": False, "note": "失败：%s" % str(e)[:50]})
            continue
        out.append({"key": key, "ok": True, "note": "已还原"})
    recs = [r for r in recs if "%s|%s" % (r.get("scope"), r.get("name"))
            not in set(names or [])]
    try:
        with open(CM_RECORD, "w", encoding="utf-8") as f:
            json.dump(recs, f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------
# 快捷方式：批量读 .lnk（COM）→ 找失效项 → 猜新目标重建
# --------------------------------------------------------------------------
def _shortcut_roots():
    up = os.environ.get("USERPROFILE", "")
    out = []
    for nm, sub in (("Desktop", known_folder("Desktop") or os.path.join(up, "Desktop")),
                    ("StartMenu", os.path.join(up, r"AppData\Roaming\Microsoft\Windows\Start Menu")),
                    ("CommonStart", os.path.join(os.environ.get("ProgramData", ""),
                                                 r"Microsoft\Windows\Start Menu"))):
        if sub and os.path.isdir(sub):
            out.append((nm, sub))
    return out


_LNK_READ_PS = (
    "$sh = New-Object -ComObject WScript.Shell; $r = @();"
    "Get-ChildItem -Path @(%s) -Filter *.lnk -Recurse -ErrorAction SilentlyContinue | ForEach-Object {"
    "  try { $l = $sh.CreateShortcut($_.FullName);"
    "    $r += [PSCustomObject]@{ P=$_.FullName; T=$l.TargetPath; A=$l.Arguments; W=$l.WorkingDirectory }"
    "  } catch {} };"
    "if ($r.Count -gt 0) { ConvertTo-Json -InputObject $r -Compress } else { '[]' }")


def shortcut_scan(cap=600):
    """扫描快捷方式并标出失效项（目标可执行文件不存在）。只读。"""
    roots = _shortcut_roots()
    if not roots:
        return []
    paths = ",".join("'%s'" % p.replace("'", "''") for _n, p in roots)
    rc, out = ps(_LNK_READ_PS % paths, timeout=120)
    items = []
    txt = (out or "").strip()
    if txt and txt not in ("[]", ""):
        try:
            data = json.loads(txt)
            if isinstance(data, dict):
                data = [data]
            for it in data:
                tgt = (it.get("T") or "").strip()
                if not tgt:
                    continue
                p = os.path.expandvars(tgt)
                ok = os.path.exists(p)
                items.append({"path": it.get("P") or "", "target": tgt,
                              "args": it.get("A") or "", "ok": ok,
                              "why": "" if ok else "目标不存在：%s" % tgt})
        except ValueError:
            return []
    return items[:cap]


def shortcut_rebuild(items):
    """按「同名程序在别处」猜测新目标并重建快捷方式（用 WScript.Shell 写 .lnk）。"""
    fixed, failed = [], []
    inst = {str(x.get("name") or "").lower(): x for x in list_software()}
    for it in items or []:
        lnk, old = it.get("path"), (it.get("target") or "")
        if not lnk or not os.path.isfile(lnk):
            failed.append((lnk, "快捷方式本身不存在"))
            continue
        base = os.path.splitext(os.path.basename(old))[0].lower()
        new = ""
        cand = inst.get(base)
        if cand:
            loc = (cand.get("location") or "").strip()
            if loc and os.path.isdir(loc):
                for dirpath, _dn, fns in os.walk(loc):
                    for fn in fns:
                        if fn.lower() == os.path.basename(old).lower():
                            new = os.path.join(dirpath, fn)
                            break
                    if new:
                        break
        if not new:
            for root in (os.path.join(os.environ.get("ProgramFiles", ""), ""),
                         os.path.join(os.environ.get("ProgramFiles(x86)", ""), ""),
                         os.environ.get("LOCALAPPDATA", "")):
                if not root or not os.path.isdir(root):
                    continue
                for dirpath, _dn, fns in os.walk(root):
                    for fn in fns:
                        if fn.lower() == os.path.basename(old).lower():
                            new = os.path.join(dirpath, fn)
                            break
                    if new:
                        break
                if new:
                    break
        if not new:
            failed.append((lnk, "找不到可替换的新目标（%s）" % os.path.basename(old)))
            continue
        script = ("$sh = New-Object -ComObject WScript.Shell; $l = $sh.CreateShortcut('%s');"
                  "$l.TargetPath = '%s'; $l.Save()"
                  % (lnk.replace("'", "''"), new.replace("'", "''")))
        rc, _o = ps(script, timeout=60)
        if rc == 0 and os.path.isfile(lnk):
            fixed.append({"path": lnk, "old": old, "new": new})
        else:
            failed.append((lnk, "重建失败"))
    return {"ok": not failed, "fixed": fixed, "failed": failed,
            "msg": "已修复 %d 个%s" % (len(fixed),
                                     ("，%d 个失败" % len(failed)) if failed else "")}


# ==========================================================================
# 批次 C：程序组件 / 浏览器扩展 / 还原点 / 文件校验
#
# 铁律延续：扫描只读；写操作一律先备份；系统自带组件默认不提供禁用。
# ==========================================================================
_COM_SYSTEM_MARKERS = ("microsoft", "windows", "system32", "syswow64",
                       "winsxs", "windowsapps", "common files")


def _is_system_component(impl_path):
    """组件的 InprocServer32 / LocalServer32 路径是否落在系统目录 → 系统自带。"""
    p = (impl_path or "").strip().strip('"')
    if not p:
        return True                      # 没有实现路径 = 残缺项，当系统项处理
    p = os.path.expandvars(p)
    low = p.lower()
    if not os.path.isabs(low):
        low = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                           "System32", low).lower()
    return any(m in low for m in _COM_SYSTEM_MARKERS)


# 32 位组件在 HKCR\Wow6432Node\CLSID 下；两类都要看，否则 32 位程序全被漏掉
_CLSID_ROOTS = ("CLSID", r"WOW6432Node\CLSID")


def list_components(cap=1200, include_system=False, need_impl=True):
    """枚举注册的 COM/ActiveX 组件。只读。

    每项：clsid / name / impl / kind（inproc=进程内 DLL / local=EXE）/
          sys（系统自带）/ risk（无实现路径的残缺项 = 最该清）。
    """
    out, seen = [], set()
    for root in _CLSID_ROOTS:
        for clsid in _subkeys("HKCR", root, cap):
            if clsid in seen:
                continue
            seen.add(clsid)
            base = "%s\\%s" % (root, clsid)
            vals = _reg_values("HKCR", base, 12)
            name = ""
            for vn, _vt, vd in vals:
                if vn == "" and isinstance(vd, str) and vd.strip():
                    name = vd.strip()
                    break
            impl, kind = "", ""
            for sub, tag in (("InprocServer32", "inproc"), ("LocalServer32", "local")):
                vv = _reg_values("HKCR", base + "\\" + sub, 4)
                d = next((str(v[2]) for v in vv if v[0] == ""), "")
                if d:
                    impl, kind = d.strip().strip('"'), tag
                    break
            if need_impl and not impl:
                continue                   # 只要能加载的组件
            issys = _is_system_component(impl)
            if issys and not include_system:
                continue
            out.append({"clsid": clsid, "name": name or "(无名称) " + clsid[:8],
                        "impl": impl, "kind": kind, "sys": issys,
                        "risk": (not impl)})
            if len(out) >= cap:
                return out
    out.sort(key=lambda x: (x["sys"], -x["risk"], x["name"].lower()))
    return out


def disable_component(clsid, enable=False):
    """禁用/启用一个 COM 组件：**移到 HKCU 影子区**（不是删），可一键还原。

    思路与右键菜单管理一致：整棵子树先备份到
    HKCU\\Software\\WinToolbox\\DisabledComponents\\<CLSID>，确认复制成功
    才删原位置 —— 中途失败能还原。
    """
    shadow_root = r"Software\WinToolbox\DisabledComponents"
    shadow = "%s\\%s" % (shadow_root, clsid)
    if winreg is None or not clsid:
        return {"ok": False, "msg": "参数无效"}
    already = _key_exists("HKCU", shadow)
    src = None
    for root in _CLSID_ROOTS:
        p = "%s\\%s" % (root, clsid)
        if _key_exists("HKCR", p):
            src = p
            break
    if enable:                                       # 还原：把影子区的搬回去
        if not already:
            return {"ok": False, "msg": "该组件不在禁用区（可能从未禁用）"}
        if not src:
            return {"ok": False, "msg": "原注册位置已不存在，无法还原"}
        try:
            _copy_tree("HKCU", shadow, "HKCR", src)
            reg_delete("HKCU" + shadow, None)
            return {"ok": True, "msg": "已还原为原 CLSID"}
        except Exception as e:
            return {"ok": False, "msg": "还原失败：%s" % str(e)[:60]}
    if already:
        return {"ok": False, "msg": "该组件已在禁用区"}
    if not src:
        return {"ok": False, "msg": "原注册位置已不存在"}
    try:
        _copy_tree("HKCR", src, "HKCU", shadow)
    except Exception as e:
        return {"ok": False, "msg": "移动失败：%s" % str(e)[:60]}
    try:
        _delete_tree(_HIVE_KEYS["HKCR"], src)         # 真删原位置 → 才算禁用
    except Exception as e:
        reg_delete("HKCU" + shadow, None)             # 回滚影子区
        return {"ok": False, "msg": "移除原键失败：%s" % str(e)[:60]}
    return {"ok": True, "msg": "已禁用（原键已备份到 HKCU 影子区，可一键还原）"}


def _copy_tree(hive, src, dhive, dst):
    """把一个注册表子树深拷贝到另一个 hive（组件禁用/还原用）。"""
    if winreg is None:
        raise OSError("无winreg")
    root = _HIVE_KEYS[hive]
    droot = _HIVE_KEYS[dhive]
    with winreg.OpenKey(root, src, 0, winreg.KEY_READ) as k:
        names, sub = [], []
        n = winreg.QueryInfoKey(k)[1]
        for i in range(n):
            try:
                vn, data, typ = winreg.EnumValue(k, i)
                names.append((vn, typ, data))
            except OSError:
                continue
        m = winreg.QueryInfoKey(k)[0]
        for i in range(m):
            try:
                sub.append(winreg.EnumKey(k, i))
            except OSError:
                continue
    # 先取完名字与子键再写，避免持有同一 key 时重入
    with winreg.CreateKeyEx(droot, dst, 0, winreg.KEY_ALL_ACCESS) as dk:
        for vn, typ, data in names:
            try:
                winreg.SetValueEx(dk, vn, 0, typ, data)
            except OSError:
                continue
    for s in sub:
        _copy_tree(hive, src + "\\" + s, dhive, dst + "\\" + s)


# --------------------------------------------------------------------------
# 浏览器扩展
#
# Chromium 系：User Data\<profile>\Extensions\<id>\<版本>\manifest.json，
#   启用状态看 Preferences 的 extensions.settings[eid].state；
# Firefox：Profiles\<p>\extensions.json。
#
# ⚠️ **只读不改**：Chromium 的 Secure Preferences 带哈希校验，直接改状态会被
#   浏览器立刻还原；真要停用应该删扩展目录（会丢扩展数据）或在浏览器里关。
#   所以这里只提供清单、权限、所在目录与跳转。
# --------------------------------------------------------------------------
_BROWSER_PROFILES = {
    "chrome": ("Google\\Chrome\\User Data", "Chrome"),
    "edge": ("Microsoft\\Edge\\User Data", "Edge"),
    "brave": ("BraveSoftware\\Brave-Browser\\User Data", "Brave"),
    "vivaldi": ("Vivaldi\\User Data", "Vivaldi"),
    "opera": ("Opera Software\\Opera Stable", "Opera"),
    "firefox": ("Mozilla\\Firefox\\Profiles", "Firefox"),
    "chromium": ("Chromium\\User Data", "Chromium"),
}


def list_browser_extensions(cap=400):
    """列出各浏览器的扩展 / 附加组件（只读）。"""
    lad = os.environ.get("LOCALAPPDATA", "")
    out = []
    if not lad:
        return out
    for bid, (rel, label) in _BROWSER_PROFILES.items():
        base = os.path.join(lad, rel)
        if not os.path.isdir(base):
            continue
        if bid == "firefox":
            out += _firefox_addons(base, label)
            continue
        try:
            profiles = [p for p in os.listdir(base)
                        if p == "Default" or p.startswith("Profile ")]
        except OSError:
            continue
        for prof in profiles:
            extdir = os.path.join(base, prof, "Extensions")
            if not os.path.isdir(extdir):
                continue
            try:
                ids = sorted(os.listdir(extdir))
            except OSError:
                continue
            for eid in ids:
                ed = os.path.join(extdir, eid)
                if not os.path.isdir(ed):
                    continue
                try:
                    vers = [v for v in sorted(os.listdir(ed))
                            if os.path.isdir(os.path.join(ed, v))]
                except OSError:
                    continue
                man = None
                for v in reversed(vers):
                    p = os.path.join(ed, v, "manifest.json")
                    if os.path.isfile(p):
                        man = p
                        break
                info = _read_manifest(man) or {}
                out.append({
                    "browser": label, "id": eid,
                    "name": info.get("name") or eid[:12],
                    "version": info.get("version") or (vers[-1] if vers else ""),
                    "desc": (info.get("description") or "")[:120],
                    "perm": info.get("perms") or [],
                    "path": os.path.dirname(man) if man else ed,
                    "size": dir_size(ed, cap=200 * 1024 * 1024)[0] if man else 0,
                    "enabled": _chromium_ext_enabled(base, prof, eid),
                    "kind": "扩展"})
                if len(out) >= cap:
                    return out
    return out


def _read_manifest(path):
    if not path or not os.path.isfile(path):
        return None
    try:
        import json as _json
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            d = _json.load(f)
        # manifest 里的 name/description 常常是 __MSG_xxx__ 占位符，
        # 真实文案在 _locales\<lang>\messages.json 里 —— 不解析就会显示一串
        # __MSG_web2pdfExtnName__ 给用户看。
        loc = _manifest_locale(d, os.path.dirname(path))
        perms = list(d.get("permissions") or [])
        perms += list(d.get("host_permissions") or [])
        return {"name": _msgt(d, "name", loc),
                "version": d.get("version") or "",
                "description": _msgt(d, "description", loc),
                "perms": [str(x)[:40] for x in perms][:8]}
    except Exception:
        return None


def _manifest_locale(d, extdir):
    """按 default_locale → zh_CN → en 顺序找 messages.json（找不到返回 {}）。"""
    import json as _json
    order = []
    dl = ((d.get("default_locale") or "").replace("-", "_"))
    if dl:
        order.append(dl)
    order += ["zh_CN", "zh_TW", "en", "en_US"]
    for lang in order:
        p = os.path.join(extdir, "_locales", lang, "messages.json")
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    return _json.load(f) or {}
            except Exception:
                continue
    return {}


def _msgt(d, field, loc):
    """取 manifest 的 name/description，遇到 __MSG_xxx__ 去 messages.json 查。"""
    v = d.get(field)
    if not isinstance(v, str):
        return ""
    m = re.match(r"^__MSG_(.+)__$", v.strip())
    if m:
        e = (loc.get(m.group(1)) or {})
        msg = e.get("message") if isinstance(e, dict) else e
        return str(msg or "").strip() or v
    return v.strip()


def _chromium_ext_enabled(base, prof, eid):
    """Chromium 系：读 Preferences 里 extensions.settings[eid].state（1=启用）。

    读不到就返回 None（未知）—— 不猜。
    """
    import json as _json
    for fn in ("Preferences", "Secure Preferences"):
        p = os.path.join(base, prof, fn)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                d = _json.load(f)
            st = ((d.get("extensions") or {}).get("settings") or {}).get(eid)
            if isinstance(st, dict) and "state" in st:
                return int(st["state"]) == 1
        except Exception:
            continue
    return None


def _firefox_addons(base, label):
    """Firefox 用 extensions.json（扁平结构，addons 数组）。"""
    out = []
    try:
        profs = os.listdir(base)
    except OSError:
        return out
    for prof in profs:
        p = os.path.join(base, prof, "extensions.json")
        if not os.path.isfile(p):
            continue
        try:
            import json as _json
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                d = _json.load(f)
        except Exception:
            continue
        for a in (d.get("addons") or []):
            if not a.get("id"):
                continue
            loc = a.get("defaultLocale") or {}
            out.append({"browser": label, "id": a["id"],
                        "name": loc.get("name") or a["id"][:12],
                        "version": a.get("version") or "",
                        "desc": (loc.get("description") or "")[:120],
                        "perm": [], "path": "", "size": 0,
                        "enabled": bool(a.get("active", True)),
                        "kind": "附加组件"})
    return out


# --------------------------------------------------------------------------
# 系统还原点：列出 / 新建 / 删除
# --------------------------------------------------------------------------
# ⚠️ 三条实测得来的硬约束（改之前先读完，别再走老路）：
#
# 1) **Windows 没有 `Remove-ComputerRestorePoint` 这个命令。**
#    `Get-Command -Name *RestorePoint*` 只返回 `Get-ComputerRestorePoint`
#    一个（SystemRestore 模块只提供 Get / Checkpoint-Computer）。
#    早先 `remove_restore_point` 调的就是这个不存在的命令 → PowerShell 报
#    「找不到命令」→ 一个都没删 → 回读发现还在 → 界面报「删除未生效」，
#    看起来像功能坏了，实际是**命令名从一开始就是错的**。
#
# 2) **正确途径是对象上的 `.Delete()`**：`Get-ComputerRestorePoint` 返回
#    `System.Management.ManagementObject`（OutputType:
#    `System.Management.ManagementObject#root\default\SystemRestore`），
#    它带 4 个 `Delete` 重载，0 参那个就是入口。
#    `SystemRestore` 这个 WMI 类**只有** CreateRestorePoint / Enable /
#    Disable / Restore / GetLastRestoreStatus —— 同样没有 Delete，别往那边找。
#
# 3) **不能用退出码判成败**：`powershell -Command` 遇到 WMI/CIM 抛出的
#    ErrorRecord 时**退出码仍然是 0**（实测 `rc=0` 而输出里全是「拒绝访问」）。
#    `; 'OK'` 这种「失败也会打出来」的哨兵同样是假的 —— 实测
#    `Checkpoint-Computer` 失败时 stdout 里 `OK` 和报错**同时存在**。
#    唯一可靠的做法：`try { ... 'SRP_OK' } catch { 'SRP_ERR:' + $_.Exception.Message }`
#    配 `-ErrorAction Stop`，让成功标记和失败原因**互斥**（实测无 admin 时
#    只出 `SRP_ERR:拒绝访问`，一个 SRP_OK 都没有）。
# --------------------------------------------------------------------------
_SRP_OK = "SRP_OK"
_SRP_NOTFOUND = "SRP_NOTFOUND"
_SRP_ERR = "SRP_ERR:"


def _rp_first_error(text):
    """从 PowerShell 的多行报错里挑出第一句有信息量的话。

    它的报错长这样（要滤掉位置行、`+` 续行、CategoryInfo / FullyQualified）：
        Get-ComputerRestorePoint : Access denied
        所在位置 行:1 字符: 101
        + ...ncoding=[System.Text.Encoding]::UTF8;Get-Computer...
            ~~~~~
            CategoryInfo          : InvalidOperation: (:) [...]
            FullyQualifiedErrorId : GetWMIManagementException,...
    """
    skip = ("所在位置", "CategoryInfo", "FullyQualifiedErrorId", "At line:")
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(("+", "~", "-")):
            continue
        if any(k in line for k in skip):
            continue
        if line.startswith(" char:") or line.startswith("位置"):
            continue
        return _rp_zh(line[:180])
    return ""


# PowerShell 的报错是英文（Access denied / 拒绝访问…），而界面全中文。
# 只翻这几个真正会命中的 —— 目的是让用户看懂「为什么没删掉」，
# 不是做完整的错误码翻译表。
_RP_ERR_ZH = (
    ("access is denied", "没有权限（需要以管理员身份运行本程序）"),
    ("access denied", "没有权限（需要以管理员身份运行本程序）"),
    ("拒绝访问", "没有权限（需要以管理员身份运行本程序）"),
    ("the service cannot be started", "系统还原服务（VSS）没启动 —— "
                                       "系统保护处于关闭状态"),
    ("cannot be started", "系统还原服务（VSS）没启动 —— 系统保护处于关闭状态"),
    ("system protection is off", "系统保护处于关闭状态"),
    ("system restore is disabled", "系统还原功能已被禁用"),
    ("not enough memory", "内存不足"),
    ("not enough space", "磁盘空间不足"),
    ("timed out", "操作超时（系统响应太慢，可以稍后再试）"),
    ("timeout", "操作超时（系统响应太慢，可以稍后再试）"),
)


def _rp_zh(msg):
    """把 PowerShell 的英文报错翻成一句中文（命中不了就原样返回）。"""
    low = (msg or "").lower()
    for key, zh in _RP_ERR_ZH:
        if key in low:
            return zh
    return msg


def _rp_run(script, timeout=180):
    """跑一段还原点 PowerShell，返回 `(状态, 原因)`。

    状态 ∈ {"ok", "notfound", "err"}。`try/catch` 保证 `_SRP_OK` 与
    `_SRP_ERR` 互斥，所以判定是**逐行精确匹配**而不是 `'OK' in out`
    （后者会被失败时同行的 OK 骗过去 —— 实测确实会）。
    """
    rc, out = ps(script, timeout=timeout)
    text = (out or "").strip()
    err = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line == _SRP_OK:
            return "ok", ""
        if line.startswith(_SRP_ERR):
            err = _rp_zh(line[len(_SRP_ERR):].strip()[:180])
        elif line == _SRP_NOTFOUND:
            return "notfound", ""
    # 一个哨兵都没打出来 = 脚本自己没跑起来（语法错 / 宿主异常）
    return "err", err or _rp_first_error(text) or "PowerShell 没有返回任何结果"


def list_restore_points():
    """列出系统还原点（只读）。失败返回 []。"""
    rc, out = ps("Get-ComputerRestorePoint | ForEach-Object { "
                 "'{0}|||{1}|||{2}|||{3}' -f $_.SequenceNumber,$_.Description,"
                 "$_.RestorePointType,$_.CreationTime }", timeout=90)
    res = []
    for line in (out or "").splitlines():
        p = line.strip().split("|||")
        if len(p) < 4 or not p[0]:
            continue
        m = re.search(r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})", p[3].strip())
        stamp = ("%04d%02d%02d" % (int(m.group(1)), int(m.group(2)),
                                  int(m.group(3)))) if m else ""
        res.append({"seq": p[0], "desc": p[1] or "(无描述)",
                    "type": {"0": "安装", "1": "系统", "2": "应用",
                             "12": "安装"}.get(p[2].strip(), p[2]),
                    "date": stamp, "raw_date": p[3].strip()})
    res.sort(key=lambda x: x["seq"], reverse=True)
    return res


def create_restore_point(desc="WinToolbox 手动还原点"):
    """新建还原点（需管理员 + 系统保护已开启）。失败时把原因如实返回。"""
    safe = str(desc).replace("'", "''")        # 描述里带单引号会截断脚本
    st, why = _rp_run(
        "$ErrorActionPreference='Stop';"
        "try {"
        "  Checkpoint-Computer -Description '%s' "
        "-RestorePointType 'APPLICATION_INSTALL' -ErrorAction Stop;"
        "  '%s'"
        "} catch { '%s' + $_.Exception.Message }" % (safe, _SRP_OK, _SRP_ERR))
    if st == "ok":
        return {"ok": True, "msg": "已创建还原点"}
    return {"ok": False,
            "msg": why or "创建失败（需要管理员权限，且系统保护须已开启）"}


def remove_restore_point(seq):
    """删除某个还原点（需管理员），删后回读复核。"""
    try:
        n = int(seq)
    except (TypeError, ValueError):
        return {"ok": False, "msg": "序列号无效"}
    st, why = _rp_run(
        "$ErrorActionPreference='Stop';"
        "try {"
        "  $rp = @(Get-ComputerRestorePoint -ErrorAction Stop |"
        "         Where-Object { $_.SequenceNumber -eq %d });"
        "  if (-not $rp) { '%s' }"
        "  else { foreach ($r in $rp) { $r.Delete() }; '%s' }"
        "} catch { '%s' + $_.Exception.Message }"
        % (n, _SRP_NOTFOUND, _SRP_OK, _SRP_ERR))
    if st == "notfound":
        return {"ok": False,
                "msg": "序列号 %d 已不存在（列表可能已过期，请刷新）" % n}
    if st == "err":
        return {"ok": False,
                "msg": why or "删除失败（需要管理员权限，且系统保护须已开启）"}
    still = any(r["seq"] == str(n) for r in list_restore_points())
    if still:
        # .Delete() 没抛错但东西还在：最典型的是「最早的那个还原点不允许删」
        # （Windows 强制保留一个回滚点），这是系统限制、不是 bug —— 如实说。
        return {"ok": False,
                "msg": "序列号 %d 仍在 —— 最早的那个还原点 Windows 不允许删除"
                       % n}
    return {"ok": True, "msg": "已删除"}


# --------------------------------------------------------------------------
# 文件校验：本地哈希 + Authenticode 数字签名
#
# ⚠️ WinVerifyTrust 有两个**不报错、只静默判成「未签名」**的坑：
#   ① 第 2 个参数是 GUID 指针，不是 c_void_p —— 传整数会被当 GUID 读，
#      轻则返回 TRUST_E_NOSIGNATURE，重则读越界抛访问违例；
#   ② 失败后必须再调一次 dwStateAction=WTD_STATEACTION_CLOSE 释放状态数据。
# --------------------------------------------------------------------------
_WINT_TYPES = None        # (GUID, WINTRUST_FILE_INFO, WINTRUST_DATA) 惰性构造


def _wintrust_types():
    global _WINT_TYPES
    if _WINT_TYPES is not None:
        return _WINT_TYPES
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class FI(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD),
                    ("pcwszFilePath", wintypes.LPCWSTR),
                    ("hFile", wintypes.HANDLE),
                    ("pgKnownSubject", ctypes.c_void_p)]

    class DATA(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD),
                    ("pPolicyCallbackData", ctypes.c_void_p),
                    ("pSIPClientData", ctypes.c_void_p),
                    ("dwUIChoice", wintypes.DWORD),
                    ("fdwRevocationChecks", wintypes.DWORD),
                    ("dwUnionChoice", wintypes.DWORD),
                    ("pFile", ctypes.POINTER(FI)),
                    ("dwStateAction", wintypes.DWORD),
                    ("hWVTStateData", wintypes.HANDLE),
                    ("pwszURLReference", wintypes.LPCWSTR),
                    ("dwProvFlags", wintypes.DWORD),
                    ("dwUIContext", wintypes.DWORD),
                    ("pSignatureSettings", ctypes.c_void_p)]
    _WINT_TYPES = (GUID, FI, DATA)
    return _WINT_TYPES


def file_signature(path):
    """校验文件数字签名：返回 (状态, 签名者)。

    状态 ∈ "已签名" / "签名无效" / "未签名" / "不受信任" / "非程序文件" /
    "证书已过期" / "无文件" / "查不了"。
    """
    if not path or not os.path.isfile(path):
        return "无文件", ""
    try:
        GUID, FI, DATA = _wintrust_types()
        wt = ctypes.WinDLL("wintrust", use_last_error=True)
        wt.WinVerifyTrust.argtypes = [ctypes.c_void_p, ctypes.POINTER(GUID),
                                      ctypes.POINTER(DATA)]
        wt.WinVerifyTrust.restype = ctypes.c_long
    except Exception:
        return "查不了", ""
    # WINTRUST_ACTION_GENERIC_VERIFY_V2
    action = GUID(0x00AAC56B, 0xCD44, 0x11D0,
                  (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2,
                                       0x95, 0xEE))
    fi = FI()
    fi.cbStruct = ctypes.sizeof(FI)
    fi.pcwszFilePath = os.path.abspath(str(path))
    wtd = DATA()
    wtd.cbStruct = ctypes.sizeof(DATA)
    wtd.dwUIChoice = 2                 # WTD_UI_NONE：不弹任何 UI
    wtd.fdwRevocationChecks = 0
    wtd.dwUnionChoice = 1              # WTD_CHOICE_FILE
    wtd.pFile = ctypes.pointer(fi)
    wtd.dwStateAction = 1              # WTD_STATEACTION_VERIFY
    wtd.dwProvFlags = 0x0400           # WTD_REVOCATION_CHECK_NONE（离线可用）
    try:
        rc = wt.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(wtd))
    except Exception:
        return "查不了", ""
    finally:
        try:
            wtd.dwStateAction = 2      # 无论成败都必须 CLOSE，否则状态数据泄漏
            wt.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(wtd))
        except Exception:
            pass
    code = rc & 0xFFFFFFFF
    if code == 0:
        return "已签名", file_publisher(path)
    if code == 0x800B0100:
        return "签名无效", ""          # 有签名但校验不过（被改过/证书已过期）
    if code in (0x800B0001, 0x800B0004):
        return "未签名", ""
    if code == 0x800B0003:
        return "非程序文件", ""        # .txt 之类没有安全数据目录
    if code in (0x800B0109, 0x800B010A, 0x8009202B):
        return "不受信任", ""
    if code == 0x80092012:
        return "证书已过期", ""
    return "查不了", ""


def file_verify(paths, algo="sha256"):
    """本地哈希 + Windows 数字签名状态。只读。"""
    out = []
    for p in (paths or []):
        p = str(p)
        row = {"path": p, "ok": os.path.isfile(p), "hash": "", "size": 0,
               "sign": "无文件", "signer": "", "trusted": None}
        if not row["ok"]:
            out.append(row)
            continue
        try:
            row["size"] = os.path.getsize(p)
        except OSError:
            pass
        row["hash"] = file_hash(p, algo)
        st, signer = file_signature(p)
        row["sign"] = st
        row["signer"] = signer
        row["trusted"] = True if st == "已签名" else (False if st == "签名无效" else None)
        out.append(row)
    return out


# --------------------------------------------------------------------------
# policies  ("managed by your organization")
# --------------------------------------------------------------------------
POLICY_OFFENDERS = [
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows Defender", "Windows 安全中心显示“由你的组织管理”"),
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows Defender\SmartScreen", "安全中心 SmartScreen 被接管"),
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate", "Windows 更新显示“某些设置由你的组织管理”"),
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows\DataCollection", "隐私页遥测项被接管"),
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows\CloudContent", "建议/推广项被接管"),
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows\AppPrivacy", "应用权限项被接管"),
    (r"HKLM\SOFTWARE\Policies\Microsoft\MicrosoftEdge", "Edge 显示“由你的组织管理”"),
    (r"HKLM\SOFTWARE\Policies\Microsoft\Edge", "Edge 策略被接管"),
    (r"HKLM\SOFTWARE\Policies\Microsoft\EdgeUpdate", "Edge 更新策略被接管"),
    (r"HKCU\SOFTWARE\Policies\Microsoft\MicrosoftEdge", "Edge 显示“由你的组织管理”"),
    (r"HKCU\SOFTWARE\Policies\Microsoft\Edge", "Edge 策略被接管"),
    (r"HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Associations", "文件类型风险策略被改写"),
    (r"HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Attachments", "下载文件安全策略被改写"),
]

POLICY_EXTRA_VALUES = [
    (r"HKLM\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings", ["PauseUpdatesExpiryTime",
     "PauseFeatureUpdatesStartTime", "PauseFeatureUpdatesEndTime", "PauseQualityUpdatesStartTime",
     "PauseQualityUpdatesEndTime", "PauseUpdatesStartTime", "FlightSettingsMaxPauseDays"],
     "Windows 更新被暂停到 2999 年"),
    (r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer", ["SmartScreenEnabled"],
     "SmartScreen 被关闭"),
]


def enum_values(root, sub):
    out = []
    if winreg is None:
        return out
    try:
        with winreg.OpenKey(root, sub, 0, winreg.KEY_READ) as k:
            n = winreg.QueryInfoKey(k)[1]
            for i in range(n):
                try:
                    nm, v, _ = winreg.EnumValue(k, i)
                    out.append({"name": nm, "value": (v if isinstance(v, (int, str)) else str(v))})
                except OSError:
                    pass
    except OSError:
        pass
    return out


def policies_scan():
    findings = []
    for path, why in POLICY_OFFENDERS:
        hive, sub = split_hive(path)
        root = winreg.HKEY_LOCAL_MACHINE if hive == "HKLM" else winreg.HKEY_CURRENT_USER
        vals = enum_values(root, sub)
        # also detect "key exists but empty" (still triggers the banner)
        exists = False
        try:
            with winreg.OpenKey(root, sub, 0, winreg.KEY_READ):
                exists = True
        except OSError:
            exists = False
        if exists or vals:
            findings.append({"path": path, "why": why, "values": vals, "count": len(vals)})
    for path, names, why in POLICY_EXTRA_VALUES:
        present = {}
        for nm in names:
            ex, typ, data = reg_read(path, nm)
            if ex:
                present[nm] = data
        if present:
            findings.append({"path": path, "why": why,
                             "values": [{"name": k, "value": v} for k, v in present.items()],
                             "count": len(present)})
    return findings


def policies_fix(paths):
    """清除策略残留：每个键先 reg export 备份到 BACKUP 目录，**备份成功才删**。"""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    results = []
    backups = []
    try:
        os.makedirs(BACKUP, exist_ok=True)
    except OSError:
        pass
    for p in paths:
        safe = p.replace("\\", "_")
        outfile = os.path.join(BACKUP, "policy_%s_%s.reg" % (safe, stamp))
        brc, _bout = run(["reg", "export", p, outfile, "/y"])
        if brc != 0 or not os.path.exists(outfile):
            # 备份没成功就绝不删：策略键一删就没法对照原始值还原了
            results.append({"path": p, "ok": False, "backup": None,
                            "why": "备份失败，已跳过（未删除）"})
            continue
        backups.append(outfile)
        rc, out = run(["reg", "delete", p, "/f"])
        results.append({"path": p, "ok": rc == 0, "backup": outfile})
    # 「暂停更新到 2999 年」「SmartScreen 被关闭」这类值不在 Policies 下，
    # 只清**用户真正勾选了的**那些父键 —— 早先无条件全删，等于勾一项清一片。
    want = {str(x).rstrip("\\").lower() for x in (paths or [])}
    for path, names, why in POLICY_EXTRA_VALUES:
        if path.rstrip("\\").lower() not in want:
            continue
        for nm in names:
            run(["reg", "delete", path, "/v", nm, "/f"])
    return {"results": results, "backups": backups, "backup_dir": BACKUP}


# --------------------------------------------------------------------------
# GPU / disk / temperature
# --------------------------------------------------------------------------
def _nvidia_smi():
    """Return list of {index, name, util, temp} for each NVIDIA GPU, or []."""
    import shutil as _sh
    cand = [r"C:\Windows\System32\nvidia-smi.exe", _sh.which("nvidia-smi")]
    for p in cand:
        if p and os.path.exists(p):
            rc, out = run([p, "--query-gpu=index,name,utilization.gpu,temperature.gpu",
                           "--format=csv,noheader,nounits"], timeout=10)
            if rc == 0 and out.strip():
                gpus = []
                for line in out.strip().splitlines():
                    try:
                        idx, name, util, temp = line.split(",")
                        gpus.append({
                            "index": int(idx.strip()),
                            "name": name.strip(),
                            "util": int(util.strip()) if util.strip() else None,
                            "temp": int(temp.strip()) if temp.strip() else None,
                        })
                    except Exception:
                        pass
                return gpus
    return []


# nvidia-smi 明细 1 秒缓存：概览页每秒刷新时复用，避免重复起进程
_NV_CACHE = {"t": 0.0, "v": None}
# TTL 略大于后台采样间隔（1s），避免刷新任务正好撞上缓存过期的瞬间而自己又读一次
_NV_TTL = 4.0


def _nvidia_smi_cached():
    with _NV_LOCK:
        now = time.time()
        if _NV_CACHE["v"] is not None and now - _NV_CACHE["t"] < _NV_TTL:
            return _NV_CACHE["v"]
        v = _nvidia_smi()
        _NV_CACHE.update(t=now, v=v)
        return v


# --------------------------------------------------------------------------
# 全显卡（多 GPU）：PDH「GPU Engine」计数器（所有厂商通用，任务管理器同款）
# + HKLM\SOFTWARE\Microsoft\DirectX 的 AdapterLuid -> 显卡名 映射
# --------------------------------------------------------------------------
def _dx_adapters():
    """返回 {AdapterLuid(int): 显卡名}。DirectX 子键每个适配器一条，含 AdapterLuid。"""
    import winreg
    out = {}
    try:
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\DirectX")
    except OSError:
        return out
    i = 0
    while True:
        try:
            sub = winreg.EnumKey(k, i)
        except OSError:
            break
        i += 1
        try:
            sk = winreg.OpenKey(k, sub)
            try:
                name = winreg.QueryValueEx(sk, "Description")[0]
                luid = int(winreg.QueryValueEx(sk, "AdapterLuid")[0])
                out[luid] = name
            except OSError:
                pass
            finally:
                winreg.CloseKey(sk)
        except OSError:
            continue
    winreg.CloseKey(k)
    return out


def _gpu_luid_of(inst_name):
    """'pid_1_luid_0x00000000_0x00013819_phys_0_...' -> int64 LUID。"""
    m = re.search(r"luid_0x([0-9A-Fa-f]{8})_0x([0-9A-Fa-f]{8})", inst_name)
    if not m:
        return None
    return (int(m.group(1), 16) << 32) | int(m.group(2), 16)


# 只取 3D 引擎实例（任务管理器的 GPU 占用口径），避免几百个实例拖慢采样
_GPU_ENGINE_TYPES = ("engtype_3D",)
_gpu_conn = {"t": 0.0, "q": None, "luids": [], "lt": 0.0, "last": None}
_GPU_CONN_TTL = 120.0
_gpu_conn_lock = threading.Lock()


# 按进程聚合的 GPU 占用（每次 GPU Engine 采样时同步更新，供进程列表使用）
_GPU_BY_PID = {}


def _gpu_pid_of(inst):
    """从 GPU Engine 实例名取进程号：'pid_1234_luid_0x...' -> 1234。"""
    m = re.search(r"pid_(\d+)", inst or "")
    return int(m.group(1)) if m else None


def _gpu_engine_utils():
    """1s 采样：返回 {luid_int: 占用%}，同时把 {pid: 占用%} 写入 _GPU_BY_PID。
    刚建查询的首秒返回 {}（无基线）。
    内部节流 ≥0.9s：调用方有常驻采样线程与 perf_stats 两处，过近的第二次采样
    会因 PDH 速率计数器没有新基线得到全 0，覆盖掉 _GPU_BY_PID。"""
    if _pdh is None:
        return {}
    now = time.time()
    with _gpu_conn_lock:
        if _gpu_conn["q"] is not None and now - _gpu_conn["lt"] < 0.9:
            return _gpu_conn["last"] or {}
        if now - _gpu_conn["t"] > _GPU_CONN_TTL or _gpu_conn["q"] is None:
            if _gpu_conn["q"]:
                _gpu_conn["q"].close()
                _gpu_conn["q"] = None
            insts = [i for i in pdh_instances("GPU Engine")
                     if any(t in i for t in _GPU_ENGINE_TYPES)][:256]
            q = _PdhQuery(["\\GPU Engine(%s)\\Utilization Percentage" % i
                           for i in insts])
            luids = sorted({l for l in (_gpu_luid_of(i) for i in insts)
                            if l is not None})
            if q.open():
                _gpu_conn.update(t=now, q=q, luids=luids)
            else:
                _gpu_conn.update(t=now, q=None, luids=[])
                return {}
            _gpu_conn.update(lt=now, last=None)
            return {}          # 首个样本只做基线
        vals = _gpu_conn["q"].collect_and_read()
        _gpu_conn["lt"] = now
    agg = {}
    by_pid = {}
    for p, v in vals.items():
        if v:
            fv = float(v)
            lu = _gpu_luid_of(p)
            if lu is not None:
                agg[lu] = min(100.0, agg.get(lu, 0.0) + fv)
            pid = _gpu_pid_of(p)
            if pid is not None:
                by_pid[pid] = min(100.0, by_pid.get(pid, 0.0) + fv)
    _gpu_conn["last"] = agg
    _GPU_BY_PID.clear()
    _GPU_BY_PID.update(by_pid)
    return agg


def _gpu_base():
    """8s：全显卡名单。{index, name, luid, temp, nv_util}（nv_util/temp 是 NVIDIA 兜底值）。"""
    nv = _nvidia_smi_cached()
    adapters = _dx_adapters()
    out = []
    for lu in _gpu_conn["luids"] or []:
        name = adapters.get(lu)
        if not name or "Microsoft Basic Render" in name:   # WARP 软渲染不是真显卡
            continue
        e = {"name": name, "luid": lu, "temp": None, "nv_util": None}
        for g in nv:
            if g["name"].strip().lower() in name.strip().lower():
                e["temp"] = g.get("temp")
                e["nv_util"] = g.get("util")
        out.append(e)
    # nvidia-smi 有、但引擎实例没出现的（驱动刚加载等），兜底补上
    for g in nv:
        if not any(g["name"].strip().lower() in e["name"].lower() for e in out):
            out.append({"name": g["name"], "luid": None, "temp": g.get("temp"),
                        "nv_util": g.get("util")})
    if not out:
        agg = _gpu_aggregate_cached()
        if agg is not None:
            out = [{"name": "GPU（聚合）", "luid": None, "temp": None,
                    "nv_util": int(round(agg))}]
    for i, e in enumerate(out):
        e["index"] = i
    return out


# --------------------------------------------------------------------------
# CPU 调试：大小核拓扑 / 电源计划 / 处理器状态 / EPP / 进程亲和
# --------------------------------------------------------------------------
def cpu_topology():
    """用 GetLogicalProcessorInformationEx 拿每个物理核的 EfficiencyClass。

    EfficiencyClass 越大越"性能核"（Intel 混合架构：P核=1，E核=0）。
    返回 {"logical": n, "cores": [...], "p_logicals": [...], "e_logicals": [...],
          "base_mhz": int, "name": str}
    """
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    GLPIE = k32.GetLogicalProcessorInformationEx
    GLPIE.restype = wt.BOOL
    GLPIE.argtypes = [wt.DWORD, ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]

    RelationProcessorCore = 0
    size = wt.DWORD(0)
    GLPIE(RelationProcessorCore, None, ctypes.byref(size))
    buf = ctypes.create_string_buffer(size.value)
    if not GLPIE(RelationProcessorCore, buf, ctypes.byref(size)):
        return None
    # SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX (x64):
    #   0: Relationship(DWORD)  4: Size(DWORD)  8: PROCESSOR_RELATIONSHIP
    #   PROCESSOR_RELATIONSHIP: +0 Flags(B) +1 EfficiencyClass(B) +22 GroupCount(W)
    #   +24 GROUP_AFFINITY { Mask(ULONG_PTR), Group(WORD) }
    MASK = (1 << ctypes.sizeof(ctypes.c_void_p) * 8) - 1
    off = 0
    raw = buf.raw
    end = size.value
    cores = []
    logical_index = 0
    while off + 8 <= end:
        rel, esize = struct.unpack_from("<II", raw, off)
        if esize <= 0:
            break
        if rel == 0:  # ProcessorCore
            eff = raw[off + 8 + 1]
            mask = int.from_bytes(raw[off + 8 + 24: off + 8 + 24 + ctypes.sizeof(ctypes.c_void_p)],
                                  "little")
            logicals = []
            m = mask
            bit = 0
            while m:
                if m & 1:
                    logicals.append(bit)
                m >>= 1
                bit += 1
            # 掩码位是全局逻辑处理器编号（单组系统）
            cores.append({"efficiency": eff, "mask": mask, "logicals": logicals})
            logical_index += len(logicals)
        off += (esize + 7) & ~7  # 8 字节对齐
    p_logicals, e_logicals = [], []
    for c in cores:
        (p_logicals if c["efficiency"] > 0 else e_logicals).extend(c["logicals"])
    name, mhz = "", 0
    try:
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        name = winreg.QueryValueEx(k, "ProcessorNameString")[0]
        mhz = int(winreg.QueryValueEx(k, "~MHz")[0])
        winreg.CloseKey(k)
    except OSError:
        pass
    all_l = sorted(l for c in cores for l in c["logicals"])
    return {"logical": len(all_l), "cores": cores,
            "p_logicals": p_logicals, "e_logicals": e_logicals,
            "p_cores": len([c for c in cores if c["efficiency"] > 0]),
            "e_cores": len([c for c in cores if c["efficiency"] == 0]),
            "base_mhz": mhz, "name": name}


def cpu_plans():
    """powercfg /list -> [{'guid','name','active'}]，并附当前活跃计划。

    活跃项原先靠「行里有没有 * 号」判断 —— 那依赖 powercfg 的输出格式，
    本地化或版本不同就可能失效。这里改成和注册表里的 ActivePowerScheme 比对，
    拿不到才退回 * 号。
    """
    rc, out = run(["powercfg", "/list"], timeout=30)
    act = _active_scheme().lower()
    plans = []
    for line in (out or "").splitlines():
        m = re.search(r"([0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})\s+(?:\((.+?)\))?", line)
        if not m:
            continue
        guid = m.group(1)
        plans.append({"guid": guid, "name": (m.group(2) or "").strip(),
                      "active": (guid.lower() == act) if act else ("*" in line)})
    return plans


def cpu_set_plan(guid):
    rc, out = run(["powercfg", "/setactive", guid], timeout=30)
    return {"ok": rc == 0, "err": (out or "").strip()[:120]}


# --------------------------------------------------------------------------
# 电源计划：内置模板 / 本机 .pow 扫描 / 导入并回读验证
#
# 对标 LaoYing Toolkit 的电源计划面板，但只做「启用 + 导入 + 回读验证」：
#   * 内置模板 —— 高性能直接启用；卓越性能在多数机器上是隐藏模板，
#     先 `-duplicatescheme` 复制一份再启用（Windows 官方模板，非第三方文件）。
#   * .pow 导入 —— 交给 Windows 自己 `-import`，随后**回读**计划列表，
#     确认新 GUID 真的被系统列出，避免"命令返回 0 但其实没导入"。
# --------------------------------------------------------------------------
POWER_TEMPLATES = {
    "high":     ("高性能",   "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"),
    "ultimate": ("卓越性能", "e9a42b02-d5df-448d-aa00-03f14749eb61"),
    "balanced": ("平衡",     "381b4222-f694-41f0-9685-ff5bb260df2e"),
    "saver":    ("节能",     "a1841308-3541-4fab-bc81-f71556f20b4a"),
}
_GUID_RE = re.compile(r"([0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})")


def power_plan_activate_template(kind="high"):
    """启用内置电源模板（high / ultimate / balanced / saver）。返回 {ok,msg,guid,name}。

    顺序：已有同名计划 → 直接启用；系统直接认得该 GUID → 启用；
    否则当隐藏模板处理，复制一份再启用（卓越性能走这条）。
    """
    tpl = POWER_TEMPLATES.get(kind)
    if not tpl:
        return {"ok": False, "msg": "未知的电源计划模板：%s" % kind}
    label, guid = tpl
    for p in cpu_plans():                       # 1) 同名计划已存在（可能是之前复制出来的）
        if (p.get("name") or "").strip() == label:
            r = cpu_set_plan(p["guid"])
            return {"ok": bool(r.get("ok")), "guid": p["guid"],
                    "name": p.get("name") or label,
                    "msg": ("已启用「%s」。" % (p.get("name") or label)) if r.get("ok")
                           else "启用失败：%s" % r.get("err")}
    rc, out = run(["powercfg", "/setactive", guid], timeout=30)
    if rc == 0:                                 # 2) 系统直接认得该模板
        return {"ok": True, "guid": guid, "name": label,
                "msg": "已启用「%s」。" % label}
    rc2, out2 = run(["powercfg", "-duplicatescheme", guid], timeout=30)
    m = _GUID_RE.search(out2 or "")
    if rc2 != 0 or not m:                       # 3) 隐藏模板：复制后再启用
        return {"ok": False, "msg": "系统未提供该电源模板，无法启用：%s"
                % ((out2 or out or "").strip()[:140] or "powercfg 无输出")}
    new_guid = m.group(1)
    r = cpu_set_plan(new_guid)
    return {"ok": bool(r.get("ok")), "guid": new_guid, "name": label,
            "msg": ("已创建并启用「%s」。" % label) if r.get("ok")
                   else "已复制该模板但启用失败：%s" % r.get("err")}


def power_plan_delete(guid):
    """删除一个电源计划（当前正在使用的不能删）。"""
    if not _is_guid(guid):
        return {"ok": False, "msg": "无效的电源计划 ID"}
    act = next((p for p in cpu_plans() if p.get("active")), None)
    if act and act["guid"].lower() == guid.lower():
        return {"ok": False, "msg": "不能删除当前正在使用的电源计划"}
    rc, out = run(["powercfg", "-delete", guid], timeout=30)
    return {"ok": rc == 0,
            "msg": "已删除该电源计划" if rc == 0 else
                   "删除失败：%s" % ((out or "").strip()[:140] or "未知原因")}


def power_plans_states(guids=None):
    """批量读各电源计划保存的处理器参数 —— 供「参数对比」表用。

    `cpu_proc_states` 每个计划要起一次 powercfg，所以这张表是按需加载的。
    """
    out = []
    for p in cpu_plans():
        if guids and p.get("guid") not in guids:
            continue
        st = cpu_proc_states(p.get("guid"))
        out.append({"guid": p.get("guid"), "name": p.get("name"),
                    "active": bool(p.get("active")),
                    "min": st.get("min"), "max": st.get("max"), "epp": st.get("epp")})
    return out


def reset_power_defaults():
    """恢复默认电源计划：切到「平衡」，并把它的处理器参数改回 Windows 默认值。

    只动这一个计划 —— 不会像 `powercfg -restoredefaultschemes` 那样把用户自建的
    全部计划一并抹掉（那是不可逆的，删了找不回来）。
    """
    label, guid = POWER_TEMPLATES["balanced"]
    r = power_plan_activate_template("balanced")
    if not r.get("ok"):
        return r
    results = [cpu_set_state(k, v, guid)
               for k, v in (("min", 5), ("max", 100), ("epp", 50))]
    bad = [x.get("err") for x in results if not x.get("ok")]
    if bad:
        return {"ok": False, "guid": guid, "name": label,
                "msg": "已切回「%s」，但部分参数没写进去：%s" % (label, "; ".join(bad))}
    return {"ok": True, "guid": guid, "name": label,
            "msg": "已切回「%s」，处理器参数恢复为默认值"
                   "（最小 5%% / 最大 100%% / EPP 50）。" % label}


# --- 本机 .pow 扫描 ------------------------------------------------------
_POW_MAX_FILES = 400
_POW_MAX_DEPTH = 2          # 每个扫描根最多下探两层，避免全盘遍历拖慢界面

# KNOWNFOLDERID —— 走 Shell API 取「真实」用户目录：
# 很多机器把桌面/下载重定向到别的盘（D:\桌面 之类），
# 直接拼 %USERPROFILE%\Desktop 会扫不到，所以这里必须问系统。
# 已知文件夹 GUID（KNOWNFOLDERID）。**键名大小写不敏感**（见 known_folder）——
# 早先只有 Desktop/Downloads/Documents 三个且大小写敏感，调用方传 "desktop"
# 就静默拿到空串，于是退回 `%USERPROFILE%\Desktop`；在「桌面/下载/文档被重定向到
# 其它盘」的机器上（本机就是：D:\桌面、D:\下载、D:\文档）会指向错误路径、静默扫 0 个。
_KNOWN_FOLDERS = {
    "desktop":   "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "pictures":  "33E28130-4E1E-4676-835A-98395C3BC3BB",
    "videos":    "18989B1D-99B5-455B-841C-AB7C74E4DDFC",
    "music":     "4BD8D571-6D19-48D3-BE97-422220080E43",
}
_KNOWN_FOLDER_CACHE = {}


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def _guid_from_str(s):
    parts = s.strip("{}").split("-")
    g = _GUID()
    g.Data1 = int(parts[0], 16)
    g.Data2 = int(parts[1], 16)
    g.Data3 = int(parts[2], 16)
    for i, b in enumerate(bytes.fromhex(parts[3] + parts[4])):
        g.Data4[i] = b
    return g


def known_folder(name):
    """取真实用户目录（含被重定向到其它盘的桌面 / 下载 / 文档）。失败返回 ""。

    **键名大小写不敏感**，且结果带缓存：`SHGetKnownFolderPath` 是 COM 调用，
    清理类功能会按目录反复问它（扫描/删除循环里），缓存掉能省不少时间。
    缓存只存**成功**的结果 —— 失败可能是瞬时问题，不该被记死。
    """
    key = str(name or "").strip().lower()
    if not key or os.name != "nt":
        return ""
    if key in _KNOWN_FOLDER_CACHE:
        return _KNOWN_FOLDER_CACHE[key]
    gid = _KNOWN_FOLDERS.get(key)
    if not gid:
        return ""
    try:
        g = _guid_from_str(gid)
        out = ctypes.c_wchar_p()
        hr = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(g), 0, None, ctypes.byref(out))
        if hr != 0 or not out.value:
            return ""
        path = out.value
        ctypes.windll.ole32.CoTaskMemFree(out)
        _KNOWN_FOLDER_CACHE[key] = path
        return path
    except Exception:
        return ""


def power_plan_scan_dirs():
    """本机常见的 .pow 存放位置（存在才返回，去重）。"""
    home = os.path.expanduser("~")
    cands = []
    for nm in ("Desktop", "Downloads", "Documents"):
        p = known_folder(nm)
        if p:
            cands.append(p)
        cands.append(os.path.join(home, nm))          # 未重定向时的常规位置
    cands += [APP_DIR, os.path.join(APP_DIR, "tools"),
              os.path.join(APP_DIR, "assets"), os.path.join(APP_DIR, "data")]
    out = []
    for c in cands:
        if not c:
            continue
        try:
            c = os.path.abspath(c)
        except Exception:
            continue
        if os.path.isdir(c) and c not in out:
            out.append(c)
    return out


def power_plan_scan_pow(extra_dirs=None):
    """扫描本机 .pow 文件 → [{name,file,path,dir,size,mtime}]（按修改时间降序）。"""
    roots = []
    for d in list(extra_dirs or []) + power_plan_scan_dirs():
        if d and os.path.isdir(d) and d not in roots:
            roots.append(d)
    seen, out = set(), []
    for root in roots:
        base_depth = os.path.abspath(root).rstrip("\\/").count(os.sep)
        for dp, dns, fns in os.walk(root):
            if dp.rstrip("\\/").count(os.sep) - base_depth >= _POW_MAX_DEPTH:
                dns[:] = []
            else:
                dns[:] = [d for d in dns if not d.startswith(".")]
            for f in fns:
                if not f.lower().endswith(".pow"):
                    continue
                p = os.path.join(dp, f)
                k = p.lower()
                if k in seen:
                    continue
                seen.add(k)
                try:
                    st = os.stat(p)
                    size, mt = st.st_size, st.st_mtime
                except OSError:
                    size, mt = 0, 0.0
                out.append({"name": os.path.splitext(f)[0], "file": f,
                            "path": p, "dir": dp, "size": size, "mtime": mt})
                if len(out) >= _POW_MAX_FILES:
                    break
            if len(out) >= _POW_MAX_FILES:
                break
    out.sort(key=lambda x: (-(x["mtime"] or 0), x["name"].lower()))
    return out


def power_plan_candidates(extra_dirs=None):
    """候选清单 = 已安装计划 + 本机 .pow 文件。

    items: [{kind:"plan"|"pow", name, source, guid, path, active}]
      kind=plan —— 系统里已安装的计划，source="已安装"
      kind=pow  —— 本机扫到的 .pow 文件，source="本机文件"
    """
    items = []
    plans = cpu_plans()
    for p in plans:
        items.append({"kind": "plan", "name": p.get("name") or p.get("guid"),
                      "source": "已安装", "guid": p.get("guid") or "",
                      "path": "", "active": bool(p.get("active"))})
    files = power_plan_scan_pow(extra_dirs)
    for f in files:
        items.append({"kind": "pow", "name": f["name"], "source": "本机文件",
                      "guid": "", "path": f["path"], "active": False})
    return {"items": items, "files": files,
            "pow_count": len(files), "plan_count": len(plans)}


def power_plan_import_pow(path, activate=True):
    """导入 .pow 电源计划：Windows 导入 → 回读验证 → 可选立即启用。

    回读验证：导入后重新枚举计划列表，必须能读回同一个 GUID，
    否则视为失败（命令行返回 0 但实际未导入的情况会被这一步拦住）。
    """
    p = (path or "").strip().strip('"')
    if not p:
        return {"ok": False, "msg": "请先选择一个 .pow 文件"}
    if os.path.splitext(p)[1].lower() != ".pow":
        return {"ok": False, "msg": "只支持 .pow 电源计划文件"}
    if not os.path.isfile(p):
        return {"ok": False, "msg": "文件不存在：%s" % p}
    rc, out = run(["powercfg", "-import", p], timeout=60)
    m = _GUID_RE.search(out or "")
    if rc != 0 or not m:
        return {"ok": False, "guid": "",
                "msg": "导入失败：%s" % ((out or "").strip()[:150]
                                       or "powercfg 未返回方案 GUID")}
    guid = m.group(1)
    rec = next((x for x in cpu_plans()
                if (x.get("guid") or "").lower() == guid.lower()), None)
    if rec is None:
        return {"ok": False, "guid": guid,
                "msg": "导入后回读验证失败：系统未列出该计划（可能被系统拒绝）"}
    name = rec.get("name") or guid
    res = {"ok": True, "guid": guid, "name": name, "active": False,
           "msg": "已导入并回读验证：%s" % name}
    if activate:
        r = cpu_set_plan(guid)
        if r.get("ok"):
            res["active"] = True
            res["msg"] = "已导入并启用：%s" % name
        else:
            res["msg"] = "已导入（回读验证通过），但启用失败：%s" % r.get("err")
    return res


# 隐藏的 Speed Shift EPP 阈值（0=最高性能，100=最高能效）
_EPP_GUID = "36687f9e-e3a5-4dbf-b1dc-15eb381c6863"
_PROC_SUB = "SUB_PROCESSOR"


def _active_scheme():
    """当前生效的电源计划 GUID（读注册表，比再起一个 powercfg 进程便宜得多）。"""
    if winreg is None:
        return ""
    try:
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes") as k:
            return str(winreg.QueryValueEx(k, "ActivePowerScheme")[0] or "")
    except OSError:
        return ""


def power_active_snapshot():
    """当前生效的电源计划 {guid, name, error}（**纯只读**）。

    只做一次注册表查询 —— 不起子进程、不改任何东西。
    用途：软件启动时记一笔；运行期间若它变了、而**不是本软件改的**，
    电源页就如实告诉用户，免得「好端端计划被换了」还以为是我们干的。
    """
    g = (_active_scheme() or "").lower()
    if not g:
        return {"guid": "", "name": "", "error": "读不到当前生效的电源计划"}
    name = ""
    if winreg is not None:
        try:
            with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes"
                    "\\" + g) as k:
                name = str(winreg.QueryValueEx(k, "FriendlyName")[0] or "")
        except OSError:
            pass
    return {"guid": g, "name": name.strip(), "error": ""}


def _parse_q_processor(scheme="SCHEME_CURRENT"):
    """powercfg -q <scheme> SUB_PROCESSOR -> {设置GUID: AC值}"""
    rc, out = run(["powercfg", "-q", scheme, _PROC_SUB], timeout=60)
    vals = {}
    cur = None
    for line in (out or "").splitlines():
        line = line.strip()
        m = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", line)
        if "电源设置 GUID" in line or "Power Setting GUID" in line or m:
            if m:
                cur = m.group(1)
            continue
        m2 = re.search(r"(?:当前交流电源设置索引|Current AC Power Setting Index)\s*:\s*(0x[0-9a-fA-F]+)", line)
        if m2 and cur:
            vals[cur] = int(m2.group(1), 16)
    return vals


def _is_guid(s):
    return bool(s and re.match(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", s))


_PROC_GUIDS = {
    "min": "893dee8e-2bef-41e0-89c6-b55d0929964c",   # 处理器最小状态 %
    "max": "bc5038f7-23e0-4960-96da-33abaf5935ec",   # 处理器最大状态 %
    "epp": _EPP_GUID,                                 # EPP 能效偏好
}


def cpu_proc_states(guid=None):
    """读取指定电源计划的处理器参数（不传则读当前生效计划）。

    ⚠️ **纯读，绝不改用户的电源计划**。
    这里原来会先跑一次
    `powercfg -attributes SUB_PROCESSOR <EPP> -ATTRIB_HIDE`
    把 EPP 设置从「隐藏」改成「显示」，好让 `powercfg -q` 能读出它。
    但 `-attributes` 是**写进当前生效计划**的 —— 等于用户一进「电源管理」页，
    他的电源计划就被我们改了（用户就是这么发现的）。
    而且本机实测那个计划里 **EPP 根本没设过**（SUB_PROCESSOR 下只有 3 个设置），
    解除隐藏纯属副作用、白改。
    现在读不到就如实返回 None，界面显示「未设置（默认）」——
    **读操作不该有任何写副作用。**
    """
    scheme = guid if _is_guid(guid) else "SCHEME_CURRENT"
    vals = _parse_q_processor(scheme)
    return {k: vals.get(g) for k, g in _PROC_GUIDS.items()}


def cpu_set_state(kind, value, guid=None):
    """设置处理器最小/最大状态(0-100)或 EPP(0-100)。

    guid 为空或为当前生效计划时：写入并立即生效（setactive 应用）；
    指定其他计划时：只写入该计划，等它被激活时才生效。
    """
    g = _PROC_GUIDS.get(kind)
    if not g:
        return {"ok": False, "err": "未知参数"}
    # ⚠️ 这里原来会先调 `_powercfg_set_hidden()` 把 EPP 从「隐藏」改成「显示」。
    #    已删除：`powercfg -setacvalueindex` 对隐藏设置照样写得进去，
    #    「隐藏」只影响经典电源对话框里的可见性，跟能不能设置无关。
    #    而那个副作用会**改到用户的生效计划**（哪怕只是想改另一个计划也会先动当前计划），
    #    用户已经因此发现「打开软件被改了电源计划」。
    #    要真正让用户能看见 EPP，应由用户自己在电源选项里勾「显示隐藏的设置」。
    scheme = guid if _is_guid(guid) else "scheme_current"
    rc1, o1 = run(["powercfg", "-setacvalueindex", scheme, _PROC_SUB,
                   g, str(int(value))], timeout=30)
    # 仅当目标是当前计划才需要 setactive 让参数立即生效；
    # 对其它计划 setactive 会把它切为当前计划，必须避免
    if scheme == "scheme_current":
        rc2, o2 = run(["powercfg", "-setactive", "scheme_current"], timeout=30)
    else:
        rc2, o2 = 0, ""
    ok = rc1 == 0 and rc2 == 0
    return {"ok": ok, "err": "" if ok else ((o1 or o2 or "").strip()[:120])}


def cpu_per_core():
    """1s：每逻辑处理器占用(%) 与 实时频率(MHz)（PDH，任务管理器同源）。"""
    conn = _cpu_conn_get()
    q = conn.get("q")
    if not q:
        return None
    vals = q.collect_and_read()
    base = conn.get("base_mhz") or 0
    cores = []
    for inst in conn.get("insts") or []:
        util = vals.get("\\Processor Information(%s)\\%% Processor Utility" % inst)
        perf = vals.get("\\Processor Information(%s)\\%% Processor Performance" % inst)
        try:
            idx = int(inst.split(",")[-1])
        except Exception:
            idx = -1
        freq = base * (perf or 0.0) / 100.0 if perf is not None else None
        cores.append({"id": idx, "util": util, "mhz": freq})
    cores.sort(key=lambda c: c["id"])
    return {"cores": cores, "base_mhz": base}


_cpu_conn = {"t": 0.0, "q": None, "insts": [], "base_mhz": 0}
_CPU_CONN_TTL = 120.0
_cpu_lock = threading.Lock()


def _cpu_conn_get():
    now = time.time()
    with _cpu_lock:
        if now - _cpu_conn["t"] <= _CPU_CONN_TTL and _cpu_conn["q"] is not None:
            return _cpu_conn
        if _cpu_conn["q"]:
            _cpu_conn["q"].close()
            _cpu_conn["q"] = None
        insts = [i for i in pdh_instances("Processor Information")
                 if re.match(r"^\d+,\d+$", i)]
        paths = []
        for i in insts:
            paths.append("\\Processor Information(%s)\\%% Processor Utility" % i)
            paths.append("\\Processor Information(%s)\\%% Processor Performance" % i)
        q = _PdhQuery(paths)
        base = 0
        try:
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                               r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            base = int(winreg.QueryValueEx(k, "~MHz")[0])
            winreg.CloseKey(k)
        except OSError:
            pass
        if q.open():
            _cpu_conn.update(t=now, q=q, insts=insts, base_mhz=base)
        else:
            _cpu_conn.update(t=now, q=None, insts=[], base_mhz=base)
        return _cpu_conn


def set_process_affinity(pid, target):
    """把进程绑定到 P 核 / E 核 / 全部核心。target: 'P' | 'E' | 'ALL'。

    ⚠️ 非混合架构（AMD 全大核、老 Intel）上 P 核与 E 核没有区分：
    旧版按「P」会报"该组核心为空"，而按「E」会拿到**全部核心** ——
    用户以为限定在小核，其实一点没限。所以这里先判架构，分不清就直接拒绝。
    """
    topo = cpu_topology()
    if not topo:
        return {"ok": False, "err": "无法读取 CPU 拓扑"}
    mixed = bool(topo.get("p_logicals")) and bool(topo.get("e_logicals"))
    if target in ("P", "E") and not mixed:
        return {"ok": False,
                "err": "本机不是大小核混合架构（没有 P/E 核之分），请改用「全核」。"}
    if target == "P":
        logicals = topo["p_logicals"]
    elif target == "E":
        logicals = topo["e_logicals"]
    else:
        logicals = list(range(topo["logical"]))
    if not logicals:
        return {"ok": False, "err": "该组核心为空（本机可能没有大小核）"}
    mask = cores_to_mask(logicals)          # 复用：自动丢弃 ≥64 的核
    if not mask:
        return {"ok": False, "err": "亲和掩码为空（逻辑核编号都 ≥64，暂不支持）"}
    PROCESS_SET_INFORMATION = 0x0200
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    # 用模块级 _k32（签名已声明）—— 就地 WinDLL() 会让 64 位句柄被截断
    h = _k32.OpenProcess(PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION,
                         False, int(pid))
    if _bad_handle(h):
        return {"ok": False, "err": "OpenProcess 失败（权限/系统进程）"}
    try:
        if not _k32.SetProcessAffinityMask(ctypes.c_void_p(h),
                                           ctypes.c_size_t(mask)):
            return {"ok": False, "err": "SetProcessAffinityMask 失败"}
        return {"ok": True, "mask": mask}
    finally:
        _k32.CloseHandle(ctypes.c_void_p(h))


# ---- 重启进「高级启动」（Windows 恢复环境） ---------------------------------
# 用途：不进系统设置也能直接进 Windows 恢复环境 —— 和「电脑**多次启动失败后
# 自动跳出来的那个修复界面**」是同一个环境。里面有：
#   启动修复 / 启动设置（可选安全模式）/ 系统还原 / 命令提示符 / UEFI 固件设置
#
# ⚠️ 刻意**不用 `/t 0`**：这不是关机脚本，而是要用户**先保存工作**。留 30 秒
#    倒计时，界面上同时给「取消重启」（`shutdown /a`）。
# ⚠️ 刻意**不加 `/f`**：有未保存的文档时应该让系统自己提示，而不是硬关掉。
REBOOT_FLAG = "/o"                 # shutdown /r /o → 重启后进「高级启动」
REBOOT_NAME = "「高级启动」"
REBOOT_DELAY_SECS = 30


def reboot_to(delay=None):
    """安排一次「重启并进入「高级启动」」。返回 {ok, name, delay, err}。

    `shutdown /r /o` 是 Microsoft 官方给出的进 Windows RE 的方式之一，
    与 Shift+重启、「设置 → 系统 → 恢复 → 立即重新启动」等价。

    ⚠️ **不只看返回码**：`shutdown` 失败时会把原因写在输出里（权限不足、策略
    禁用等），只看 rc 会把失败当成功 —— 而「以为会重启、结果什么都没发生」
    在这里是最糟的结果。
    """
    secs = REBOOT_DELAY_SECS if delay is None else max(0, int(delay))
    rc, out = run(["shutdown", "/r", REBOOT_FLAG, "/t", str(secs)], timeout=20)
    txt = (out or "").strip()
    if rc == 0 and not txt:
        return {"ok": True, "name": REBOOT_NAME, "delay": secs, "err": ""}
    return {"ok": False, "name": REBOOT_NAME, "delay": secs,
            "err": (txt.splitlines()[0][:140] if txt else
                    "shutdown 返回码 %s 且没有输出" % rc)}


def cancel_reboot():
    """取消已安排的重启（`shutdown /a`）。没有待执行的重启时**如实**返回失败。"""
    rc, out = run(["shutdown", "/a"], timeout=20)
    txt = (out or "").strip()
    if rc == 0:
        return {"ok": True, "err": ""}
    return {"ok": False, "err": (txt.splitlines()[0][:140] if txt else
                                 "取消失败（返回码 %s）" % rc)}

# --------------------------------------------------------------------------
# 核心调度增强（对标 LaoYing Toolkit 的 Scheduler / MemoryPriority / 核心预留）
#
# 与既有 `set_process_affinity`（只能整组绑 P / E / 全部）的区别：
#   · 可选**任意核心集合**，并支持「强亲和」—— 同时贯穿该进程的**所有线程**
#   · 设置**内存优先级 / IO 优先级**
#   · **释放工作集**（单进程 / 全系统）
#   · 读回当前调度状态（亲和掩码 + 优先级类 + 内存优先级）
# 全部是可打桩纯函数：测试里替换掉即不会真改系统。
# --------------------------------------------------------------------------
_PROCESS_SET_INFORMATION = 0x0200
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_SET_QUOTA = 0x0100          # SetProcessWorkingSetSize / EmptyWorkingSet 需要
_PROCESS_QUERY_INFORMATION = 0x0400  # 同上（EmptyWorkingSet 的文档要求）
_THREAD_SET_INFORMATION = 0x0020
_THREAD_QUERY_INFORMATION = 0x0040
_TH32CS_SNAPTHREAD = 0x00000004

MEM_PRIORITY_NAMES = {0: "很低", 1: "低", 2: "普通", 3: "中", 4: "高", 5: "很高"}
IO_PRIORITY_NAMES = {0: "很低", 1: "低", 2: "普通", 3: "高", 4: "很高"}
PRIORITY_CLASS_NAMES = {
    0x00000040: "空闲", 0x00004000: "低于普通", 0x00000020: "普通",
    0x00008000: "高于普通", 0x00000080: "高", 0x00000100: "实时",
}


def _open_process(pid, access=None):
    k32 = _k32
    acc = access if access is not None else (_PROCESS_SET_INFORMATION |
                                             _PROCESS_QUERY_LIMITED_INFORMATION)
    return k32, k32.OpenProcess(acc, False, int(pid))


def cores_to_mask(cores):
    """逻辑核号列表 → 亲和掩码（最多 64 位）。"""
    mask = 0
    for c in cores or []:
        try:
            c = int(c)
        except Exception:
            continue
        if 0 <= c < 64:
            mask |= (1 << c)
    return mask


def _thread_ids(pid):
    """枚举某进程的所有线程 ID（CreateToolhelp32Snapshot）。"""
    class THREADENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ThreadID", wt.DWORD), ("th32OwnerProcessID", wt.DWORD),
                    ("tpBasePri", wt.LONG), ("tpDeltaPri", wt.LONG),
                    ("dwFlags", wt.DWORD)]

    k32 = _k32
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if _bad_handle(snap):
        return []
    out = []
    try:
        te = THREADENTRY32()
        te.dwSize = ctypes.sizeof(THREADENTRY32)
        ok = k32.Thread32First(ctypes.c_void_p(snap), ctypes.byref(te))
        while ok:
            if int(te.th32OwnerProcessID) == int(pid):
                out.append(int(te.th32ThreadID))
            ok = k32.Thread32Next(ctypes.c_void_p(snap), ctypes.byref(te))
    except Exception:
        pass
    finally:
        k32.CloseHandle(ctypes.c_void_p(snap))
    return out


def set_process_affinity_cores(pid, cores, strong=False):
    """把进程绑定到**指定核心集合**（逻辑核号从 0 起）。

    strong=True 时还会遍历该进程的所有线程逐个 `SetThreadAffinityMask` ——
    即 LaoYing 的 "Strong affinity"，用于「进程设了但某些线程不听话」的场景。
    """
    mask = cores_to_mask(cores)
    if mask == 0:
        return {"ok": False, "err": "请至少选择一个核心"}
    k32, h = _open_process(pid)
    if not h:
        return {"ok": False, "err": "OpenProcess 失败（权限不足或系统进程）"}
    try:
        if not k32.SetProcessAffinityMask(ctypes.c_void_p(h), ctypes.c_size_t(mask)):
            return {"ok": False, "err": "SetProcessAffinityMask 失败"}
        threads = failed = 0
        if strong:
            for tid in _thread_ids(pid):
                th = k32.OpenThread(_THREAD_SET_INFORMATION | _THREAD_QUERY_INFORMATION,
                                    False, tid)
                if not th:
                    failed += 1
                    continue
                try:
                    if k32.SetThreadAffinityMask(ctypes.c_void_p(th),
                                                 ctypes.c_size_t(mask)):
                        threads += 1
                    else:
                        failed += 1
                finally:
                    k32.CloseHandle(ctypes.c_void_p(th))
        return {"ok": True, "mask": mask,
                "cores": sorted({int(c) for c in cores}),
                "threads": threads, "failed": failed}
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


def restore_process_affinity(pid):
    """恢复进程到全部逻辑核心（清掉自定义亲和）。"""
    n = int((cpu_topology() or {}).get("logical") or 1)
    return set_process_affinity_cores(pid, list(range(min(n, 64))))


def set_process_memory_priority(pid, level):
    """设置进程内存优先级（0..5，Win8+ 的 SetProcessInformation）。"""
    try:
        level = max(0, min(5, int(level)))
    except Exception:
        return {"ok": False, "err": "优先级取值 0..5"}

    class MEMORY_PRIORITY_INFORMATION(ctypes.Structure):
        _fields_ = [("MemoryPriority", wt.ULONG)]

    k32, h = _open_process(pid)
    if not h:
        return {"ok": False, "err": "OpenProcess 失败"}
    try:
        info = MEMORY_PRIORITY_INFORMATION(level)
        r = k32.SetProcessInformation(ctypes.c_void_p(h), 0,
                                      ctypes.byref(info), ctypes.sizeof(info))
        return {"ok": bool(r), "level": level,
                "err": "" if r else "SetProcessInformation 失败"}
    except Exception as e:
        return {"ok": False, "err": str(e)}
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


def set_process_io_priority(pid, level):
    """设置进程 IO 优先级（0..4，NtSetInformationProcess 的 ProcessIoPriority=33）。"""
    try:
        level = max(0, min(4, int(level)))
    except Exception:
        return {"ok": False, "err": "优先级取值 0..4"}
    try:
        ntdll = ctypes.WinDLL("ntdll")
        k32, h = _open_process(pid)
        if not h:
            return {"ok": False, "err": "OpenProcess 失败"}
        try:
            prio = wt.ULONG(level)
            st = ntdll.NtSetInformationProcess(ctypes.c_void_p(h), 33,
                                               ctypes.byref(prio),
                                               ctypes.sizeof(prio))
            return {"ok": st == 0, "level": level,
                    "err": "" if st == 0 else "NtSetInformationProcess 0x%x"
                    % (st & 0xFFFFFFFF)}
        finally:
            k32.CloseHandle(ctypes.c_void_p(h))
    except Exception as e:
        return {"ok": False, "err": str(e)}


def release_working_set(pid=None):
    """释放进程工作集（pid=None → 当前进程）。等价于 LaoYing 的 ReleaseHiddenWorkingSet。"""
    try:
        psapi = ctypes.WinDLL("psapi")
        psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
        psapi.EmptyWorkingSet.restype = wt.BOOL
        if pid is None:
            h = _k32.GetCurrentProcess()
            close = False
            k32 = _k32
        else:
            # EmptyWorkingSet 需要 PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA，
            # 默认的 SET_INFORMATION|QUERY_LIMITED 打不开 → 曾被误判成"EmptyWorkingSet 失败"
            k32, h = _open_process(pid, _PROCESS_QUERY_INFORMATION | _PROCESS_SET_QUOTA)
            close = True
        if not h:
            return {"ok": False, "err": "OpenProcess 失败"}
        try:
            r = psapi.EmptyWorkingSet(h)
            return {"ok": bool(r), "err": "" if r else "EmptyWorkingSet 失败"}
        finally:
            if close:
                k32.CloseHandle(h)
    except Exception as e:
        return {"ok": False, "err": str(e)}


def release_working_set_all(limit=4000):
    """释放所有可访问进程的工作集（无权限的系统进程会被跳过）。"""
    done = skipped = 0
    for p in list_processes(limit=limit):
        pid = p.get("pid")
        if not pid:
            continue
        if release_working_set(pid).get("ok"):
            done += 1
        else:
            skipped += 1
    return {"ok": True, "done": done, "skipped": skipped}


def process_sched_state(pid):
    """读回进程当前调度状态（亲和掩码 / 优先级类 / 内存优先级）。"""
    k32, h = _open_process(pid, _PROCESS_QUERY_LIMITED_INFORMATION | 0x0400)
    if not h:
        return {"ok": False, "err": "OpenProcess 失败"}
    try:
        pmask = ctypes.c_size_t(0)
        smask = ctypes.c_size_t(0)
        k32.GetProcessAffinityMask(ctypes.c_void_p(h), ctypes.byref(pmask),
                                   ctypes.byref(smask))
        prio = int(k32.GetPriorityClass(ctypes.c_void_p(h)))
        mem = None
        try:
            class MEMORY_PRIORITY_INFORMATION(ctypes.Structure):
                _fields_ = [("MemoryPriority", wt.ULONG)]
            info = MEMORY_PRIORITY_INFORMATION()
            if k32.GetProcessInformation(ctypes.c_void_p(h), 0,
                                         ctypes.byref(info), ctypes.sizeof(info)):
                mem = int(info.MemoryPriority)
        except Exception:
            pass
        return {"ok": True, "affinity": int(pmask.value),
                "system_mask": int(smask.value), "priority": prio,
                "priority_name": PRIORITY_CLASS_NAMES.get(prio, str(prio)),
                "memory_priority": mem,
                "memory_name": (MEM_PRIORITY_NAMES.get(mem, "—")
                                if mem is not None else "—")}
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


# --------------------------------------------------------------------------
# 显卡显示名伪装（对标 LaoYing 的 GpuDisplayAlias）
#
# 原理：设备管理器与多数程序读的是显卡**驱动实例**键里的 DriverDesc /
# HardwareInformation.* —— 改写它们即可让系统「显示成」另一款卡。
# 原值会先写入备份文件，支持一键还原。注意：只改名字，不改硬件能力；
# 部分反作弊 / 跑分工具会校验，改完可能报错 —— 界面里要明确提示。
# --------------------------------------------------------------------------
_GPU_CLASS = (r"SYSTEM\CurrentControlSet\Control\Class"
              r"\{4d36e968-e325-11ce-bfc1-08002be10318}")
_GPU_VIDEO = r"SYSTEM\CurrentControlSet\Control\Video"
_GPU_ENUM = r"SYSTEM\CurrentControlSet\Enum"
# 可能带「名字」的值。前三处是老实现就写的；DacType 有就顺带。
_GPU_VALUE_KEYS = ("DriverDesc", "HardwareInformation.AdapterString",
                   "HardwareInformation.ChipType", "HardwareInformation.DacType")
# ⚠️ 这些**不是名字**（显存大小等），绝不能碰。老实现靠「必须是 str」来挡 REG_BINARY，
#    但 Intel 核显的 AdapterString 本身就是 REG_BINARY → 一刀切会把真名字也挡掉。
_GPU_NEVER_TOUCH = ("HardwareInformation.qwMemorySize",
                    "HardwareInformation.MemorySize",
                    "HardwareInformation.BiosString")
# 预设显卡名（按分组维护，界面用分隔线分组显示）。
# `GPU_ALIAS_PRESETS` 是拍平后的全量清单 —— 单一数据源，别单独改它。
GPU_ALIAS_GROUPS = [
    ("NVIDIA GeForce · 桌面（RTX 50 / 40）", [
        "NVIDIA GeForce RTX 5090", "NVIDIA GeForce RTX 5080",
        "NVIDIA GeForce RTX 5070", "NVIDIA GeForce RTX 4090",
        "NVIDIA GeForce RTX 4060",
    ]),
    ("NVIDIA GeForce · 桌面（RTX 30 / 20 / 16）", [
        "NVIDIA GeForce RTX 3080", "NVIDIA GeForce RTX 3060",
        "NVIDIA GeForce RTX 2080 Ti", "NVIDIA GeForce GTX 1660 SUPER",
    ]),
    ("NVIDIA GeForce · 入门 / 老卡（GTX 10 / 9 / 7）", [
        "NVIDIA GeForce GTX 1080 Ti", "NVIDIA GeForce GTX 1070",
        "NVIDIA GeForce GTX 1060", "NVIDIA GeForce GTX 1050 Ti",
        "NVIDIA GeForce GT 1030", "NVIDIA GeForce GTX 960",
        "NVIDIA GeForce GTX 750 Ti",
    ]),
    ("NVIDIA · 笔记本 / 专业卡", [
        "NVIDIA GeForce RTX 4090 Laptop GPU", "NVIDIA GeForce RTX 3060 Laptop GPU",
        "NVIDIA RTX A4000",
    ]),
    ("AMD Radeon", [
        "AMD Radeon RX 7900 XTX", "AMD Radeon RX 7800 XT",
        "AMD Radeon RX 6800 XT", "AMD Radeon RX 6700 XT",
        "AMD Radeon RX 6600", "AMD Radeon RX 580",
    ]),
    ("Intel 核显 / Arc 独显", [
        "Intel(R) Arc(TM) B580 Graphics", "Intel(R) Arc(TM) A770 Graphics",
        "Intel(R) Iris(R) Xe Graphics", "Intel(R) UHD Graphics 770",
        "Intel(R) UHD Graphics 630",
    ]),
]
GPU_ALIAS_PRESETS = [n for _grp, _names in GPU_ALIAS_GROUPS for n in _names]
_GPU_BACKUP_FILE = os.path.join(APP_DIR, "gpu_alias_backup.json")


def _gpu_backup_load():
    try:
        with open(_GPU_BACKUP_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _gpu_backup_save(d):
    try:
        with open(_GPU_BACKUP_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        return True
    except Exception:
        return False


def _gpu_ps(cmd, timeout=90):
    """跑一段 PowerShell 拿文本行。显卡「类键序号 ↔ 设备实例」的对应关系只存在于
    PnP 里（注册表没有现成的链接），所以这一处必须走 PowerShell。"""
    if run is None:
        return []
    rc, out = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                  timeout=timeout)
    return [l.strip() for l in (out or "").splitlines() if l.strip()]


def gpu_instance_map():
    """`类键序号 -> 设备实例 ID`（如 `0002` -> `PCI\\VEN_10DE&...\\4&3a37517a&0&0008`）。

    靠 PnP 的 `DEVPKEY_Device_Driver` 反查 —— 它的值就是 `{4d36e968-…}\\NNNN`。
    """
    ps = ("Get-PnpDevice -Class Display -ErrorAction SilentlyContinue | "
          "ForEach-Object { $k=(Get-PnpDeviceProperty -InstanceId $_.InstanceId "
          "-KeyName 'DEVPKEY_Device_Driver' -ErrorAction SilentlyContinue).Data; "
          "if($k){ $_.InstanceId + '||' + $k } }")
    out = {}
    for line in _gpu_ps(ps):
        if "||" not in line:
            continue
        inst, key = line.split("||", 1)
        idx = key.strip().rsplit("\\", 1)[-1].strip()
        if re.fullmatch(r"\d{4}", idx):
            out[idx] = inst.strip()
    return out


def _gpu_read_str(sk, name):
    """读一个「文本型」值 → (文本, 原类型)。

    REG_BINARY 的值是 **UTF-16LE 编码的字符串**（Intel 核显就这样），解出来；
    解出来不是可读文本的（比如 MemorySize 那种纯二进制）抛 OSError —— 调用方跳过。
    """
    v, t = winreg.QueryValueEx(sk, name)
    if t == winreg.REG_BINARY:
        if not isinstance(v, bytes):
            raise OSError("二进制值类型异常")
        txt = v.decode("utf-16le", "ignore").rstrip("\x00")
        # 可打印比例过低 → 是纯二进制数据，不是名字
        if not txt or sum(1 for c in txt if c.isprintable()) < len(txt) * 0.6:
            raise OSError("该二进制值不是文本")
        return txt, t
    if isinstance(v, str):
        return v, t
    raise OSError("不是字符串值")


def _gpu_encode(text, vtype):
    """按**原来的类型**写回去：REG_BINARY 时编码成 UTF-16LE（带结尾 NUL）。"""
    if vtype == winreg.REG_BINARY:
        return (text + "\x00").encode("utf-16le")
    return text


def _gpu_write_name(sk, name, old_type, new_name):
    """写一个名字值；老值不存在时（old_type=None）按 REG_SZ 新建。"""
    winreg.SetValueEx(sk, name, 0,
                      old_type if old_type is not None else winreg.REG_SZ,
                      _gpu_encode(new_name, old_type))
    got, _t = _gpu_read_str(sk, name)
    return got == new_name          # 回读复核


def _gpu_open(path, write=True):
    return winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0,
                          winreg.KEY_READ | (winreg.KEY_WRITE if write else 0))


def gpu_adapters():
    """枚举显卡驱动实例 → [{index, name, vendor, alias, backup}]。"""
    if winreg is None:
        return []
    out = []
    bk = _gpu_backup_load()
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _GPU_CLASS) as k:
            n = winreg.QueryInfoKey(k)[0]
            for i in range(n):
                sub = winreg.EnumKey(k, i)
                if not re.match(r"^\d{4}$", sub):
                    continue
                try:
                    with winreg.OpenKey(k, sub) as sk:
                        def q(v, _sk=sk):
                            try:
                                return winreg.QueryValueEx(_sk, v)[0]
                            except OSError:
                                return ""
                        name = q("DriverDesc") or ""
                        if not name:
                            continue
                        rec = bk.get(sub) or {}
                        out.append({"index": sub, "name": name,
                                    "vendor": q("ProviderName") or "",
                                    "alias": rec.get("alias") or "",
                                    "backup": bool(rec.get("where") or rec.get("values"))})
                except OSError:
                    continue
    except FileNotFoundError:
        return []
    out.sort(key=lambda x: x["index"])
    return out


def _gpu_targets(index):
    """要写的所有位置 → ([(标签, 注册表路径, 值名, 是否允许新建)], 设备实例 ID)。

    实测四处都可能被当成「名字的来源」：
      1. 类键 `Control\\Class\\{4d36e968-…}\\NNNN` —— 类安装器 / 部分程序读它
      2. `Enum\\<设备实例>\\DeviceDesc` —— **设备管理器读这里**（老实现漏了）
      3. `Enum\\<设备实例>\\FriendlyName` —— 存在时**优先于** DeviceDesc；
         原本没有就新建（设备管理器「重命名硬件」写的就是它）
      4. `Control\\Video\\{GUID}\\0000\\DriverDesc` —— 显示驱动配置，能对上号就顺带改
    """
    idx = str(index).zfill(4)
    out = []
    inst = gpu_instance_map().get(idx)
    if inst:
        out.append(("设备实例 DeviceDesc", _GPU_ENUM + "\\" + inst, "DeviceDesc", False))
        out.append(("设备实例 FriendlyName", _GPU_ENUM + "\\" + inst, "FriendlyName", True))
    cur = ""
    try:
        with _gpu_open(_GPU_CLASS + "\\" + idx, write=False) as sk:
            cur = winreg.QueryValueEx(sk, "DriverDesc")[0]
    except OSError:
        pass
    if cur:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _GPU_VIDEO) as vk:
                cnt = winreg.QueryInfoKey(vk)[0]
                for i in range(cnt):
                    g = winreg.EnumKey(vk, i)
                    try:
                        with winreg.OpenKey(vk, g + "\\0000") as gk:
                            if winreg.QueryValueEx(gk, "DriverDesc")[0] == cur:
                                out.append(("显示驱动配置", _GPU_VIDEO + "\\" + g + "\\0000",
                                            "DriverDesc", False))
                                break
                    except OSError:
                        continue
        except OSError:
            pass
    return out, inst


def _gpu_label(path, name):
    """给用户看的短位置名（别把整条注册表路径糊上去）。"""
    if path.startswith(_GPU_ENUM):
        return "设备实例 " + name
    if path.startswith(_GPU_VIDEO):
        return "显示驱动配置"
    return name


def gpu_alias_apply(index, new_name):
    """改显示名：**写全所有位置 + 每处回读复核**，备份写不成功就拒绝改。返回 {ok, msg}。"""
    if winreg is None:
        return {"ok": False, "msg": "当前环境无法访问注册表"}
    new_name = (new_name or "").strip()
    if not new_name:
        return {"ok": False, "msg": "请填写要显示的显卡名称"}
    if len(new_name) > 64:
        return {"ok": False, "msg": "名称过长（≤64 字符）"}
    idx = str(index).zfill(4)
    if not re.fullmatch(r"\d{4}", idx):
        return {"ok": False, "msg": "适配器序号不合法"}

    where = {}

    def remember(path, name, make):
        """记下原值 [文本, 类型, 是否原本不存在]。"""
        try:
            with _gpu_open(path, write=False) as sk:
                txt, t = _gpu_read_str(sk, name)
            where.setdefault(path, {})[name] = [txt, int(t), 0]
        except OSError:
            if not make:
                raise
            # 值不存在（FriendlyName 常见）→ 记「原本没有」，还原时删掉
            where.setdefault(path, {})[name] = [None, int(winreg.REG_SZ), 1]

    # 读不到原值的位置 → **不改**（否则还原不回去），但要如实告诉用户
    skipped = []
    try:
        cls_path = _GPU_CLASS + "\\" + idx
        with _gpu_open(cls_path, write=False) as sk:
            for v in _GPU_VALUE_KEYS:
                if v in _GPU_NEVER_TOUCH:
                    continue
                try:
                    remember(cls_path, v, False)
                except OSError as e:
                    skipped.append("%s（读不到原值：%s）" % (v, e))
        tg, _inst = _gpu_targets(idx)
        for label, path, name, make in tg:
            try:
                remember(path, name, make)
            except OSError as e:
                # ⚠️ 读不到原值就**不能**改这里（否则还原不回去）。
                #    但**必须说出来** —— 静默跳过会让用户以为「全都改了」。
                skipped.append("%s %s（读不到原值：%s）" % (label, name, e))
    except PermissionError:
        return {"ok": False, "msg": "读取原值被拒绝 —— 需要管理员权限"}
    except OSError as e:
        return {"ok": False, "msg": "读取原值失败：%s" % e}

    if not where:
        return {"ok": False, "msg": "该适配器没有可改写的名称值"}

    bk = _gpu_backup_load()
    bk_before = json.loads(json.dumps(bk))      # 留一份，用于「一个都没写成」时回退
    old_rec = bk.get(idx) or {}
    merged = dict(old_rec.get("where") or {})
    for path, names in where.items():
        merged.setdefault(path, {}).update(names)
    bk[idx] = {"alias": new_name, "where": merged}
    if not _gpu_backup_save(bk):
        return {"ok": False,
                "msg": "备份原值失败，为安全起见**没有改动**"
                       "（检查程序目录是否可写，或杀软是否拦截写文件）"}

    # ⚠️ 结果要**按位置分组**呈现。曾经的 `bad_list[:4]` 会把最关键的
    #    「设备实例」那两条直接截掉 —— 用户看到「全都写成功了」，设备管理器却没变。
    res = {}                      # 位置标签 -> (符号, 说明)
    for path, names in where.items():
        for name, _v in names.items():
            label = _gpu_label(path, name)
            try:
                with _gpu_open(path) as sk:
                    good = _gpu_write_name(sk, name, _v[1], new_name)
                res[label] = ("✓", "已写入并回读确认" if good
                              else "写入后回读**不一致**")
            except PermissionError:
                res[label] = ("✗", "拒绝访问（需要管理员权限）")
            except OSError as e:
                res[label] = ("✗", str(e))
    for item in skipped:          # item 形如 "设备实例 DeviceDesc（读不到原值：…）"
        head = item.split("（")[0].strip()
        res.setdefault(head, ("—", "读不到原值，**未改动**（否则无法还原）"))

    lines = ["%s %s —— %s" % (v[0], k, v[1]) for k, v in res.items()]
    ok_n = sum(1 for v in res.values() if v[0] == "\u2713")
    # ⚠️ 一个都没写成 → 什么都没变，就**不该**留下备份记录
    #    （否则界面会显示「已备份原名，可还原」，让人以为真改过了）
    if ok_n == 0:
        _gpu_backup_save(bk_before)
    # 设备实例那一项是设备管理器真正读的，单独点出来
    dev = [k for k in res if k.startswith("设备实例")]
    dev_ok = all(res[k][0] == "\u2713" for k in dev) if dev else False
    tail = ("\n\n点「立即生效」重启显卡驱动即可看到（**不用重启电脑**）。"
            if ok_n else
            "\n\n若报「拒绝访问」，请以管理员身份运行本程序。")
    head_txt = ("已改名为：%s" % new_name if ok_n else "**一处都没写成功**")
    if dev and not dev_ok:
        tail += ("\n⚠️「设备实例」那项没写成 —— 设备管理器读的就是它，"
                 "所以设备管理器 / 任务管理器里的名字不会变。")
    return {"ok": ok_n > 0,
            "msg": "%s\n\n%s\n\n写入成功 %d / %d 处%s"
                   % (head_txt, "\n".join(lines), ok_n, len(res), tail)}


def gpu_alias_restore(index):
    """把备份的原值写回**所有位置**；原来没有的值（如 FriendlyName）删掉才算还原。"""
    if winreg is None:
        return {"ok": False, "msg": "当前环境无法访问注册表"}
    idx = str(index).zfill(4)
    bk = _gpu_backup_load()
    rec = bk.get(idx)
    where = rec.get("where") if rec else None
    if not where:
        return {"ok": False, "msg": "没有该适配器的备份记录"}
    failed = []
    for path, names in where.items():
        for name, val in names.items():
            try:
                txt, t, created = (list(val) + [0])[:3]
            except (TypeError, ValueError):
                continue
            try:
                with _gpu_open(path) as sk:
                    if created:
                        winreg.DeleteValue(sk, name)
                    else:
                        winreg.SetValueEx(sk, name, 0, int(t),
                                          _gpu_encode(txt, int(t)))
            except FileNotFoundError:
                pass
            except OSError as e:
                failed.append("%s(%s)" % (_gpu_label(path, name), e))
    if failed:
        # 有值没写回去就**不能**丢掉备份记录 —— 否则用户再也还原不了了
        return {"ok": False,
                "msg": "部分值还原失败：%s。备份已保留，可重试。" % "、".join(failed[:3])}
    bk.pop(idx, None)
    _gpu_backup_save(bk)
    return {"ok": True, "msg": "已还原为原显示名（点「立即生效」刷新）"}


def gpu_restart_device(index):
    """让改名**立刻生效**：只重启这一个显示设备（不用重启电脑）。

    `pnputil /restart-device`（Win10 2004+）。⚠️ 屏幕会闪一下/黑一两秒，正常现象。
    """
    idx = str(index).zfill(4)
    inst = gpu_instance_map().get(idx)
    if not inst:
        return {"ok": False, "msg": "找不到该显卡的设备实例 ID，无法免重启生效"}
    if run is None:
        return {"ok": False, "msg": "当前环境无法执行命令"}
    rc, out = run(["pnputil", "/restart-device", inst], timeout=150)
    txt = (out or "").strip()
    low = txt.lower()
    bad = (any(k in txt for k in ("失败", "错误", "无法"))
           or any(k in low for k in ("failed", "error", "not found")))
    if rc == 0 and not bad:
        return {"ok": True, "msg": "已重启显卡驱动 —— 现在去设备管理器 / 任务管理器看。"}
    return {"ok": False,
            "msg": "重启设备没成功：%s\n\n可手动执行：\n  pnputil /restart-device \"%s\"\n"
                   "或直接重启电脑。" % (txt.splitlines()[0][:160] if txt
                                       else "返回码 %s 且没有输出" % rc, inst)}


def gpu_current_wmi_names():
    """系统**现在实际报**的显卡名（设备管理器 / 任务管理器同源），用来验证改完生效没。"""
    ps = ("Get-CimInstance Win32_VideoController | "
          "ForEach-Object { $_.Name + '||' + $_.PNPDeviceID }")
    out = []
    for line in _gpu_ps(ps):
        if "||" in line:
            nm, pnp = line.split("||", 1)
            out.append({"name": nm.strip(), "pnp": pnp.strip()})
    return out


def _perf_script():
    """Return CPU temp and per-physical-disk activity (util % + R/W bytes/s).
    GPU details come from _nvidia_smi() in Python."""
    return (
        "$t=$null;"
        "try{$z=Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature"
        " -ErrorAction SilentlyContinue|Sort-Object CurrentTemperature -Descending"
        "|Select-Object -First 1;"
        "if($z){$t=[math]::Round($z.CurrentTemperature/10.0-273.15,1)}}catch{};"
        "if($null -eq $t){try{$z2=Get-CimInstance Win32_PerfFormattedData_Counters_ThermalZoneInformation"
        " -ErrorAction SilentlyContinue|Sort-Object Temperature -Descending|Select-Object -First 1;"
        "if($z2){$t=[math]::Round([double]$z2.Temperature-273.15,1)}}catch{}};"
        "if($null -eq $t){$t='NA'};"
        "'CPUTEMP='+$t;"
        "try{$d=Get-CimInstance Win32_PerfFormattedData_PerfDisk_PhysicalDisk"
        " -ErrorAction SilentlyContinue|Where-Object{$_.Name -ne '_Total'}|"
        "Sort-Object Name;}catch{};"
        "if($d){foreach($x in $d){"
        "'DISK|'+$x.Name+'|'+([math]::Round([double]$x.PercentDiskTime,1))"
        "+'|'+([math]::Round([double]$x.DiskReadBytesPerSec/1MB,2))"
        "+'|'+([math]::Round([double]$x.DiskWriteBytesPerSec/1MB,2))}}"
    )


# ==========================================================================
# 实时性能计数器（PDH）——「读系统资源管理器」那条路
#
# 之前读磁盘/网络实时速率走 PowerShell 起进程，一次往返 1.5s 以上，
# 既慢又不可能做 1s 级刷新；而 WMI 的 PerfFormattedData 缓存约 1s，
# 请求比它快就会拿到重复样本。
#
# 这里改用 Windows 自带的 PDH（Performance Data Helper）API —— 也就是
# 任务管理器 / 资源监视器底层用的同一套计数器：
#   \PhysicalDisk(*)\Disk Read Bytes/sec、Disk Write Bytes/sec、% Disk Time
#   \Network Interface(*)\Bytes Received/sec、Bytes Sent/sec
# 特点：
#   * 纯 ctypes 调用，单次采样 <1ms，无进程启动开销 -> 可以做到真正的 1s 实时；
#   * 速率由系统自己维护（两次 collect 之间自动平均），不用我们自己算差值；
#   * 不需要管理员权限（性能计数器默认对 Users 组可读）。
#
# 关键坑（已踩）：PDH_FMT_COUNTERVALUE 里 CStatus 后面有 4 字节填充，
# doubleValue 的偏移是 8 而不是 4；写成 c_double 直接接在 DWORD 后面会
# 读到填充区，结果恒为 0。
# ==========================================================================
class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", wt.DWORD),
                ("_pad", wt.DWORD),
                ("doubleValue", ctypes.c_double)]


_PDH_FMT_DOUBLE = 0x00000200
_PDH_FMT_NOCAP100 = 0x00008000
_PDH_MORE_DATA = 0x800007D2

_pdh = None
try:
    _pdh = ctypes.WinDLL("pdh", use_last_error=True)
    _pdh.PdhOpenQueryW.restype = wt.LONG
    _pdh.PdhOpenQueryW.argtypes = [wt.LPCWSTR, ctypes.c_void_p,
                                   ctypes.POINTER(ctypes.c_void_p)]
    _pdh.PdhAddEnglishCounterW.restype = wt.LONG
    _pdh.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, wt.LPCWSTR,
                                           ctypes.c_void_p,
                                           ctypes.POINTER(ctypes.c_void_p)]
    _pdh.PdhCollectQueryData.restype = wt.LONG
    _pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
    _pdh.PdhGetFormattedCounterValue.restype = wt.LONG
    _pdh.PdhGetFormattedCounterValue.argtypes = [
        ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
        ctypes.POINTER(PDH_FMT_COUNTERVALUE)]
    _pdh.PdhCloseQuery.restype = wt.LONG
    _pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
except Exception:
    _pdh = None


class _PdhQuery:
    """一组性能计数器的常驻查询句柄。

    PdhCollectQueryData 连续调用即可拿到「自上次调用以来」的平均速率，
    所以常驻一个 query、每秒 collect 一次就够，无需重建。
    """

    def __init__(self, paths):
        self.paths = list(paths)
        self.q = None
        self.handles = []       # [(path, handle)]
        self.ready = False

    def open(self):
        if _pdh is None:
            return False
        try:
            q = ctypes.c_void_p()
            if _pdh.PdhOpenQueryW(None, None, ctypes.byref(q)) != 0:
                return False
            self.q = q
            hs = []
            for p in self.paths:
                h = ctypes.c_void_p()
                if _pdh.PdhAddEnglishCounterW(q, p, None, ctypes.byref(h)) == 0:
                    hs.append((p, h))
            self.handles = hs
            # 首个样本无效（无基线），先 collect 一次预热
            _pdh.PdhCollectQueryData(q)
            return len(hs) > 0
        except Exception:
            self.close()
            return False

    def collect_and_read(self):
        """collect 一次并返回 {path: value}（无效项为 None）。"""
        if _pdh is None or self.q is None:
            return {}
        try:
            _pdh.PdhCollectQueryData(self.q)
            out = {}
            for p, h in self.handles:
                typ = wt.DWORD(0)
                cv = PDH_FMT_COUNTERVALUE()
                rc = _pdh.PdhGetFormattedCounterValue(
                    h, _PDH_FMT_DOUBLE, ctypes.byref(typ), ctypes.byref(cv))
                # CStatus: 0 = VALID_DATA, 1 = NEW_DATA，其余视为无效
                out[p] = cv.doubleValue if (rc == 0 and cv.CStatus <= 1) else None
            return out
        except Exception:
            return {}

    def close(self):
        try:
            if _pdh is not None and self.q is not None:
                _pdh.PdhCloseQuery(self.q)
        except Exception:
            pass
        self.q = None
        self.handles = []


def pdh_instances(obj):
    """用 PowerShell 枚举性能对象实例名（PDH 的 EnumObjectItems 在本机返回空，
    而 Get-Counter -ListSet 稳定可靠；只在启动时调一次，不影响实时性）。

    返回 [(实例名, 计数路径前缀)]，例如 [('0 C: D:', '\\PhysicalDisk(0 C: D:)')]。
    """
    script = (
        "try{(Get-Counter -ListSet '%s' -ErrorAction Stop).PathsWithInstances"
        "|ForEach-Object{'I|'+$_}}catch{}" % obj)
    rc, out = ps(script, timeout=20)
    seen = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line.startswith("I|"):
            continue
        path = line[2:]
        # \PhysicalDisk(0 C: D:)\Disk Read Bytes/sec -> 取括号里的实例名
        if "(" not in path or ")" not in path:
            continue
        inst = path[path.index("(") + 1:path.index(")")]
        if inst not in seen:
            seen.append(inst)
    return seen


# 磁盘/网络的主路径缓存
_perf_conn = {"t": 0.0, "disk_q": None, "net_q": None,
              "disk_inst": [], "net_inst": [], "units": {}}
_PERF_CONN_TTL = 120.0
_perf_conn_lock = threading.Lock()


def _perf_conn_get():
    """惰性建立 PDH 查询（含实例名发现），120 秒后重建以适应网卡插拔。"""
    now = time.time()
    with _perf_conn_lock:
        need = (now - _perf_conn["t"] > _PERF_CONN_TTL or
                _perf_conn["disk_q"] is None)
        if not need:
            return _perf_conn
        # 拆掉旧的
        for k in ("disk_q", "net_q"):
            if _perf_conn.get(k):
                _perf_conn[k].close()
        disk_inst = pdh_instances("PhysicalDisk")
        net_inst = pdh_instances("Network Interface")
        # _Total 放最前，用于聚合值
        disk_inst = ["_Total"] + [i for i in disk_inst if i != "_Total"]

        dq_paths = []
        for inst in disk_inst:
            base = "\\PhysicalDisk(%s)" % inst
            dq_paths += [base + "\\Disk Read Bytes/sec",
                         base + "\\Disk Write Bytes/sec",
                         base + "\\% Disk Time",
                         base + "\\% Idle Time"]
        dq = _PdhQuery(dq_paths)
        dq.open()

        nq_paths = []
        for inst in net_inst:
            base = "\\Network Interface(%s)" % inst
            nq_paths += [base + "\\Bytes Received/sec",
                         base + "\\Bytes Sent/sec",
                         base + "\\Current Bandwidth"]
        nq = _PdhQuery(nq_paths)
        nq.open() if nq_paths else None

        _perf_conn.update(t=now, disk_q=dq if dq.handles else None,
                          net_q=nq if nq.handles else None,
                          disk_inst=disk_inst, net_inst=net_inst)
        return _perf_conn


# --------------------------------------------------------------------------
# 网络实时速率：常驻后台采样
# --------------------------------------------------------------------------
_net_lock = threading.Lock()
_net_state = {
    "t": 0.0,          # 上次采样时间戳（time.monotonic）
    "last_read": 0.0,  # 上次有人调用 net_info() 的时间（空闲降频用）
    "spans": {},       # 实例名 -> {"rx": Mbps, "tx": Mbps, "bw": 协商带宽bps}
    "err": None,
    "stop": False,
    "thread": None,
    "warming": False,  # 首轮建立 PDH 查询中（此时尚无样本，属正常）
}


# --------------------------------------------------------------------------
# 监测暂停开关
#
# 窗口被最小化 / 收进系统托盘时，UI 侧所有 1 秒轮询都会被 page_active() 拦掉，
# 这两个常驻采样线程也就再没人来取数 —— 与其「降频空转」，不如直接停：由 UI 在
# 窗口状态变化时置位、恢复窗口时清零。暂停期间不 collect PDH、不喂 nvidia-smi、
# 不读热区，整机彻底安静。
#
# 「核心调度」这类功能都是即写即生效的注册表 / 电源计划写入（进程亲和性、
# 内存·IO 优先级、电源计划切换…），不依赖后台线程，所以停掉采样不影响它们。
# --------------------------------------------------------------------------
_MON_PAUSED = threading.Event()


def set_monitor_paused(on):
    """窗口最小化 / 收进托盘 → 暂停后台采样；重新显示 → 恢复。"""
    if on:
        _MON_PAUSED.set()
    else:
        _MON_PAUSED.clear()


def monitors_paused():
    """当前是否处于「窗口不可见、采样已停」的状态。"""
    return _MON_PAUSED.is_set()


def _net_loop():
    """常驻线程：每秒 collect 一次 PDH 网络计数器。

    PDH 的 Bytes Received/sec 已经是「自上次 collect 以来的平均速率」，
    所以只要保持稳定的 collect 节奏就得到连续真实的曲线。

    首次迭代要建立 PDH 查询（含 PowerShell 实例枚举，约 5~8s），
    这段耗时发生在后台线程里，不阻塞任何 HTTP 请求。

    窗口被最小化 / 收进托盘时整个循环停下（见 _MON_PAUSED）：不 collect、
    不查网卡；恢复后先丢掉一帧重建基线再继续。
    """
    prev = 0.0
    first = True
    skip = 0                 # >0：这一帧只把 PDH 基准挪到「现在」，数值不写进状态
    while not _net_state["stop"]:
        if _MON_PAUSED.is_set():
            # 窗口不可见 → 彻底停采。顺手清掉速率并标「准备中」，免得 UI 显示
            # 暂停前的旧值；恢复后的第一帧差值是跨暂停算出来的（只是平均值），
            # 所以也一并丢掉，否则恢复瞬间会闪一个偏低的速率。
            if not skip:
                skip = 1
            with _net_lock:
                _net_state["warming"] = True
                _net_state["spans"] = {}
            time.sleep(0.5)
            continue
        try:
            if first:
                # 如实告知调用方「正在准备」，避免它误判为「无数据」
                with _net_lock:
                    _net_state["warming"] = True
            conn = _perf_conn_get()
            nq = conn.get("net_q")
            if nq is not None:
                vals = nq.collect_and_read()
                if skip:
                    skip -= 1                 # 基线帧：只把 PDH 基准挪到「现在」
                else:
                    spans = {}
                    for inst in conn.get("net_inst") or []:
                        base = "\\Network Interface(%s)" % inst
                        rx = vals.get(base + "\\Bytes Received/sec")
                        tx = vals.get(base + "\\Bytes Sent/sec")
                        bw = vals.get(base + "\\Current Bandwidth")
                        if rx is None and tx is None:
                            continue
                        spans[inst] = {
                            "rx": max(0.0, (rx or 0.0) * 8.0 / 1e6),   # B/s -> Mbps
                            "tx": max(0.0, (tx or 0.0) * 8.0 / 1e6),
                            "bw": bw,
                        }
                    with _net_lock:
                        _net_state["spans"] = spans
                        _net_state["t"] = time.monotonic()
                        _net_state["err"] = None
                        _net_state["warming"] = False
            else:
                _net_loop_fallback()
        except Exception as e:
            with _net_lock:
                _net_state["err"] = "%s: %s" % (type(e).__name__, e)
        finally:
            if first:
                first = False
        # 最近 20 秒没人读（停在别的页面）就降到 5 秒一次；
        # 一旦有人读立刻恢复 1 秒节奏
        with _net_lock:
            last_read = float(_net_state.get("last_read") or 0.0)
        idle = (time.monotonic() - last_read) > 20
        time.sleep(5.0 if idle else max(0.05, 1.0 - (time.monotonic() - prev)))
        prev = time.monotonic()


_NET_SAMPLE_SCRIPT = (
    "$sw=[System.Diagnostics.Stopwatch]::StartNew();"
    "try{foreach($x in Get-NetAdapterStatistics -ErrorAction SilentlyContinue){"
    "$n=[string]$x.Name;"
    "'NS|'+$n+'|'+[string]$x.ReceivedBytes+'|'+[string]$x.SentBytes}}catch{};"
    "'T|'+[string][int]$sw.ElapsedMilliseconds;"
)


def _net_sample_once():
    """PDH 不可用时的回退：单次采集所有网卡的累计收发字节。"""
    rc, out = ps(_NET_SAMPLE_SCRIPT, timeout=15)
    elapsed = 0
    res = {}
    for line in (out or "").splitlines():
        line = line.strip()
        if line.startswith("NS|"):
            p = line.split("|")
            if len(p) >= 4:
                try:
                    res[p[1]] = (int(float(p[2])), int(float(p[3])))
                except Exception:
                    pass
        elif line.startswith("T|"):
            try:
                elapsed = int(float(line.split("|", 1)[1]))
            except Exception:
                elapsed = 0
    return time.monotonic() - elapsed / 1000.0, res


_net_fb_prev = {"t": 0.0, "data": {}}


def _net_loop_fallback():
    """回退路径：两次 Get-NetAdapterStatistics 差值。"""
    now, cur = _net_sample_once()
    prev = _net_fb_prev
    spans = {}
    if prev["data"] and prev["t"]:
        dt = now - prev["t"]
        if dt > 0.05:
            for name, (rx, tx) in cur.items():
                if name in prev["data"]:
                    p0 = prev["data"][name]
                    spans[name] = {
                        "rx": max(0.0, (rx - p0[0]) * 8.0 / dt / 1e6),
                        "tx": max(0.0, (tx - p0[1]) * 8.0 / dt / 1e6),
                        "bw": None,
                    }
    _net_fb_prev.update(t=now, data=cur)
    with _net_lock:
        _net_state["spans"] = spans
        _net_state["t"] = now
        # 回退路径也要清「准备中」——否则 PDH 不可用的机器上界面永远显示准备中
        _net_state["warming"] = False
        _net_state["err"] = "PDH 网络计数器不可用，已回退 Get-NetAdapterStatistics"


def net_sampler_start():
    """幂等启动后台采样线程。"""
    with _net_lock:
        th = _net_state.get("thread")
        if th is not None and th.is_alive():
            return
        _net_state["stop"] = False
        th = threading.Thread(target=_net_loop, name="netsampler", daemon=True)
        _net_state["thread"] = th
    th.start()


def _net_ensure_async():
    """把「PDH 首次建立」丢到后台，绝不阻塞请求线程。

    只在网络采样尚未就绪时被 net_info() 调用；重复调用无副作用，
    因为 net_sampler_start() 本身是幂等的，而 _net_loop 内部会做收集。
    """
    net_sampler_start()


def _parse_linkspeed(s):
    """'1 Gbps' / '100 Mbps' / '2.5 Gbps' -> 数值 Mbps（int）。"""
    s = (s or "").strip().lower()
    if not s:
        return 0
    m = re.search(r"([\d.]+)", s)
    if not m:
        return 0
    try:
        v = float(m.group(1))
    except Exception:
        return 0
    if "gbps" in s or "gbit" in s or "g/" in s:
        v *= 1000.0
    elif "kbps" in s or "kbit" in s:
        v /= 1000.0
    return int(round(v))


_NET_META_SCRIPT = (
    "$a=@();"
    "try{$a=@(Get-NetAdapter -ErrorAction SilentlyContinue)}catch{};"
    "foreach($x in $a){$ls='';try{$ls=[string]$x.LinkSpeed}catch{};"
    "$mt='';try{$mt=[string]$x.MediaType}catch{};"
    "$ni='';try{$ni=[string]$x.InterfaceDescription}catch{};"
    "$sp='';try{$sp=[string]$x.Speed}catch{};"
    "$st='';try{$st=[string]$x.Status}catch{};"
    "'NET|'+$x.Name+'|'+$mt+'|'+$ls+'|'+$ni+'|'+$sp+'|'+$st}"
)

_net_adapters_cache = {"t": 0.0, "data": None}
_NET_ADAPTERS_TTL = float("inf")   # 进程内永久：网卡静态信息只在启动时查一次，
                                 # 之后每秒轮询不再起 PowerShell（要更新用 force=True）
_net_adapters_lock = threading.Lock()


def net_adapters_all(force=False):
    """全部网卡（含未连接）的静态信息：名称/介质/类型/协商速率/描述/是否在线。

    这项信息几乎不变（只在插拔网线/切换网卡时变化），但每次都要起 PowerShell
    （往返 1~2s）。UI 每秒轮询一次，若不缓存会把请求线程拖到秒级 —— 所以
    **进程内只查一次**，要更新用 force=True。
    """
    now = time.monotonic()
    with _net_adapters_lock:
        cur = _net_adapters_cache.get("all")
        if not force and cur is not None:
            return [dict(a) for a in cur]
        # 上一次查询刚失败过就退避：否则「查不到 → 不写缓存 → 下一秒再查」会死循环，
        # 退化成每秒起一个 PowerShell（拿不到网卡信息的老机器就是这么被吃满 CPU 的）
        if not force and (now - float(_net_adapters_cache.get("fail_t") or 0.0)
                          < _NET_ADAPTERS_FAIL_TTL):
            return [dict(a) for a in cur] if cur else []

    rc, out = ps(_NET_META_SCRIPT, timeout=15)
    items = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line.startswith("NET|"):
            continue
        p = line.split("|")
        if len(p) < 5:
            continue
        ls = _parse_linkspeed(p[3])
        if ls <= 0 and len(p) >= 6:
            # LinkSpeed 缺失时退回 Speed（bps 数值）
            try:
                ls = int(round(float(p[5]) / 1e6))
            except Exception:
                ls = 0
        mt = p[2].lower()
        kind = "WiFi" if ("wireless" in mt or "802.11" in mt or "wlan" in mt) else "以太网"
        status = p[6].strip() if len(p) >= 7 else ""
        items.append({"name": p[1], "media": p[2], "kind": kind, "link_mbps": ls,
                      "desc": p[4], "status": status,
                      "up": status.lower() == "up"})

    # 只有拿到结果才写「成功缓存」（避免一次失败把空列表固化）；失败记时间戳配合退避
    if items:
        with _net_adapters_lock:
            _net_adapters_cache["all"] = [dict(a) for a in items]
            _net_adapters_cache["t"] = time.monotonic()
            _net_adapters_cache["fail_t"] = 0.0
        return items
    with _net_adapters_lock:
        _net_adapters_cache["fail_t"] = time.monotonic()
    if cur is not None:
        return [dict(a) for a in cur]
    return items


def net_adapters(force=False):
    """**在线**网卡（Status=Up）—— 保持原语义，供 _pick_primary 选主网卡用。"""
    return [a for a in net_adapters_all(force) if a.get("up")]

_NET_VIRTUAL_KEYS = ("virtual", "vmware", "hyper-v", "loopback", "bluetooth",
                     "tap", "vpn", "wintun", "radmin")


def _net_is_virtual(a):
    """虚拟/隧道网卡（VMware、VPN、蓝牙 PAN、加速器 TAP…）不该出现在网卡选择里。"""
    d = ((a or {}).get("desc") or "").lower()
    return any(k in d for k in _NET_VIRTUAL_KEYS)


def _pick_primary(adapters):
    """选主网卡：优先非虚拟且协商速率最高的那张。"""
    best = None
    for a in adapters:
        if _net_is_virtual(a):
            continue
        if best is None or (a["link_mbps"] or 0) > (best["link_mbps"] or 0):
            best = a
    if best is None and adapters:
        best = adapters[0]
    return best


def _net_span_of(adapter, spans):
    """按驱动描述把某张网卡对到它的 PDH 实例。

    与 `_match_net_instance` 的区别：**不做「取流量最大」兜底** ——
    那个兜底是给主网卡准备的，用在其它网卡上会把主网卡的速率张冠李戴，
    所以次级网卡匹配不到就老实返回 None（界面显示 0）。
    """
    if not spans:
        return None
    desc = (adapter or {}).get("desc") or ""
    if not desc:
        return None
    if desc in spans:
        return spans[desc]
    norm = desc.replace("[", "").replace("]", "").lower()
    for k, v in spans.items():
        kn = k.replace("[", "").replace("]", "").lower()
        if kn == norm or kn.startswith(norm) or norm.startswith(kn):
            return v
    return None


def _match_net_instance(primary, spans, net_inst):
    """把 PDH 的 Network Interface 实例对到主网卡上。

    PDH 的实例名是「网卡驱动描述」（如 'Realtek PCIe GbE Family Controller'），
    而 Get-NetAdapter 的 Name 是「连接名」（如 '以太网'），两者不同名，
    所以用描述做包含匹配；重名时（PDH 用 ' _2' 后缀区分）按顺序兜底。
    """
    if not spans:
        return None
    desc = (primary or {}).get("desc") or ""
    if desc:
        # 优先精确
        if desc in spans:
            return spans[desc]
        # 再前缀/包含匹配（PDH 会把方括号转义成 [R] 之类）
        norm = desc.replace("[", "").replace("]", "").lower()
        for k, v in spans.items():
            kn = k.replace("[", "").replace("]", "").lower()
            if kn == norm or kn.startswith(norm) or norm.startswith(kn):
                return v
    # 兜底：取流量最大的那个
    return max(spans.values(), key=lambda v: (v.get("rx") or 0) + (v.get("tx") or 0))


def net_info():
    """当前活跃网卡的：名称 / 类型(WiFi|以太网) / 协商速率 / 实时收发速率。

    静态信息走 Get-NetAdapter；实时速率来自常驻采样线程，
    主路径是 PDH 性能计数器（\\Network Interface(*)\\Bytes Received/sec），
    PDH 不可用时回退 Get-NetAdapterStatistics 差值。
    """
    net_sampler_start()
    adapters = net_adapters()
    primary = _pick_primary(adapters)

    with _net_lock:
        _net_state["last_read"] = time.monotonic()     # 告诉采样线程"有人在看"
        spans = dict(_net_state.get("spans") or {})
        seen = float(_net_state.get("t") or 0.0)
        err = _net_state.get("err")
        warming = bool(_net_state.get("warming"))

    rx = tx = bw = None
    hit = _match_net_instance(primary, spans, None)
    if hit:
        rx = round(hit.get("rx") or 0.0, 3)
        tx = round(hit.get("tx") or 0.0, 3)
        if hit.get("bw"):
            try:
                bw = int(round(float(hit["bw"]) / 1e6))     # bps -> Mbps
            except Exception:
                bw = None

    # 采样线程刚起步、还没有样本时：**不能在这里同步等待**。
    # PDH 首次建立要枚举实例（PowerShell 往返约 5~8s），若在此 sleep，
    # UI 每秒一次的轮询会连续堆积，表现为「网络监测没反应」。
    # 正确做法：让后台线程去准备，本次请求先如实返回 rx=None，下一拍就有数。
    if rx is None and primary and not spans:
        _net_ensure_async()

    # 每张网卡各自的实时速率（供概览页「网络」卡切换网卡用）。
    # 用全量列表：未连接的也列出来（否则本机只连一张时下拉里就只有一个选项）；
    # 在线的排前面，未连接的速率留空、界面标「未连接」。
    alist = net_adapters_all()
    real = [a for a in alist if not _net_is_virtual(a)] or alist
    items = []
    for a in (sorted(real, key=lambda x: (not x.get("up"), -(x.get("link_mbps") or 0)))
              or adapters):
        hit = _net_span_of(a, spans)
        items.append({"name": a.get("name"), "kind": a.get("kind"),
                      "desc": a.get("desc"), "link_mbps": a.get("link_mbps"),
                      "up": bool(a.get("up")),
                      "rx_mbps": (round(hit.get("rx") or 0.0, 3) if hit else None),
                      "tx_mbps": (round(hit.get("tx") or 0.0, 3) if hit else None)})

    age = round(max(0.0, time.monotonic() - seen), 1) if seen else None
    return {"adapters": adapters, "primary": primary, "items": items,
            "rx_mbps": rx, "tx_mbps": tx, "speed_mbps": bw,
            "sample_age": age, "error": err, "warming": warming}


def _gpu_aggregate():
    """Try to get a single aggregate GPU % from performance counters (non-NVIDIA)."""
    rc, out = ps(
        "$g=$null;"
        "try{$g=(Get-CimInstance Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine"
        " -ErrorAction SilentlyContinue|Measure-Object -Property UtilizationPercentage -Sum).Sum}catch{};"
        "if($null -eq $g){try{$c=(Get-Counter '\\GPU Engine(*)\\Utilization Percentage'"
        " -ErrorAction SilentlyContinue).CounterSamples;"
        "if($c){$g=[math]::Round(($c|Measure-Object CookedValue -Sum).Sum,1)}}catch{}};"
        "if($null -eq $g){$g='NA'};"
        "$g", timeout=15)
    try:
        return float(out.strip())
    except Exception:
        return None


# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 实时磁盘活动：走 PDH 性能计数器（与任务管理器同一套数据源）
#
# 早期版本用 WMI `Win32_PerfFormattedData_PerfDisk_PhysicalDisk`，两个毛病：
#   1) WMI 原始样本缓存约 1s，请求比它快就会反复拿到同一个样本，看起来「不实时」；
#   2) 枚举 WMI 类要起 PowerShell，往返 1.5s 以上，做不了 1s 刷新。
# 也试过直接对 \\.\PhysicalDriveN 发 IOCTL_DISK_PERFORMANCE：非管理员下
# 返回的是 24 字节零值桩（Windows 对磁盘性能计数器有额外权限要求），不可用。
#
# 最终用 PDH（PdhOpenQuery / PdhCollectQueryData / PdhGetFormattedCounterValue）：
#   \PhysicalDisk(0 C: D:)\Disk Read Bytes/sec、Disk Write Bytes/sec、% Disk Time
#   * 纯 ctypes，单次采样 <1ms，无进程启动开销 -> 真正的 1s 实时；
#   * 速率型计数器由系统在两次 collect 之间自动平均，无需自己算差值；
#   * 默认对普通用户可读，不需要管理员。
# --------------------------------------------------------------------------
def disk_activity():
    """从常驻 PDH 磁盘查询里读一次，返回 (per_disk列表, 聚合util)。

    per_disk 元素：{"name", "util", "read_mbps", "write_mbps"}。
    PDH 不可用时返回 (None, None)，由调用方走 WMI 回退。
    """
    conn = _perf_conn_get()
    dq = conn.get("disk_q")
    if dq is None:
        return None, None
    vals = dq.collect_and_read()
    if not vals:
        return None, None

    disks = []
    utils = []
    # 单个物理盘（跳过 _Total，它只用于聚合）
    for inst in conn.get("disk_inst") or []:
        if inst == "_Total":
            continue
        base = "\\PhysicalDisk(%s)" % inst
        rd = vals.get(base + "\\Disk Read Bytes/sec")
        wr = vals.get(base + "\\Disk Write Bytes/sec")
        ut = vals.get(base + "\\% Disk Time")
        idle = vals.get(base + "\\% Idle Time")
        if rd is None and wr is None and ut is None and idle is None:
            continue
        util = _disk_util(ut, idle, rd, wr)
        disks.append({
            "name": inst,
            "util": round(util, 1) if util is not None else None,
            "read_mbps": round(max(0.0, (rd or 0.0)) / 1048576.0, 2),
            "write_mbps": round(max(0.0, (wr or 0.0)) / 1048576.0, 2),
        })
        if util is not None:
            utils.append(util)

    # 聚合：优先用 _Total 的 % Disk Time，最贴近任务管理器显示
    agg = None
    tot = vals.get("\\PhysicalDisk(_Total)\\% Disk Time")
    tot_rd = vals.get("\\PhysicalDisk(_Total)\\Disk Read Bytes/sec")
    tot_wr = vals.get("\\PhysicalDisk(_Total)\\Disk Write Bytes/sec")
    tot_idle = vals.get("\\PhysicalDisk(_Total)\\% Idle Time")
    if tot is not None or tot_idle is not None:
        agg = _disk_util(tot, tot_idle, tot_rd, tot_wr)
    if agg is None and utils:
        agg = sum(utils) / float(len(utils))
    if agg is not None:
        agg = max(0.0, min(100.0, agg))
    return disks, agg


def _disk_util(disk_time, idle_time, rd_bps=None, wr_bps=None):
    """把磁盘忙碌率归一成 0~100。

    三种情况：
      * 机械盘 / 走 % Disk Time：直接可用，但多队列系统可能 >100，此时用 % Idle Time 反推；
      * NVMe / SSD：% Disk Time 只统计「控制器忙」，高吞吐下依然很低（例如 112 MB/s
        写入也只有 3%），对用户没参考价值 —— 这时改用吞吐折算的等效忙碌度；
      * 都拿不到时返回 None。
    """
    busy = None
    if disk_time is not None:
        busy = float(disk_time)
        if busy > 100.0 and idle_time is not None:
            busy = 100.0 - float(idle_time)
    elif idle_time is not None:
        busy = 100.0 - float(idle_time)

    # 等效忙碌度：以 500 MB/s 作为满盘参考（SATA SSD ~550MB/s，NVMe 更高但不再线性）
    total_mbps = ((rd_bps or 0.0) + (wr_bps or 0.0)) / 1048576.0
    eff = min(100.0, total_mbps * 100.0 / 500.0)

    # 取两者较大值：SSD 上让吞吐主导，机械盘上让 % Disk Time 主导
    v = max(busy or 0.0, eff)
    if disk_time is None and idle_time is None:
        v = eff
    return max(0.0, min(100.0, v))


# 每秒刷新的轻量层：CPU 温度 + GPU 聚合 + 磁盘活动
_fast_cache = {"t": 0.0, "data": None}
_FAST_TTL = 1.0
_fast_lock = threading.Lock()

# 每 8 秒刷新的重量层：nvidia-smi 明细（进程启动 ~100ms）
_slow_cache = {"t": 0.0, "data": None}
_SLOW_TTL = 8.0
_slow_lock = threading.Lock()

_FAST_SCRIPT = (
    "$t=$null;"
    "try{$z=Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature"
    " -ErrorAction SilentlyContinue|Sort-Object CurrentTemperature -Descending"
    "|Select-Object -First 1;"
    "if($z){$t=[math]::Round($z.CurrentTemperature/10.0-273.15,1)}}catch{};"
    "if($null -eq $t){try{$z2=Get-CimInstance Win32_PerfFormattedData_Counters_ThermalZoneInformation"
    " -ErrorAction SilentlyContinue|Sort-Object Temperature -Descending|Select-Object -First 1;"
    "if($z2){$t=[math]::Round([double]$z2.Temperature-273.15,1)}}catch{}};"
    "if($null -eq $t){$t='NA'};"
    "'CPUTEMP='+$t;"
)

# ---- CPU 温度直读：PDH「Thermal Zone Information」（~1ms，免 PowerShell 进程）----
# 原实现每次读温度都要起一个 PowerShell（0.5~1.4s），后台每秒采样也吃 CPU；
# PDH 是 Windows 自带计数器 API，一次读取亚毫秒级，才能真正做到 1s 级刷新。
_tz_conn = {"q": None, "t": 0.0, "hi": False}
_TZ_TTL = 300.0
_tz_lock = threading.Lock()
_HI_TZ = r"\Thermal Zone Information(*)\High Precision Temperature"
_RAW_TZ = r"\Thermal Zone Information(*)\Temperature"


def _tz_open():
    """建立热区计数器查询：优先「High Precision Temperature」（1/10 K 刻度）。

    直接用通配符路径 open，**不**先 pdh_instances 预检（枚举计数器集要 1s+）；
    建立后丢弃首个样本（PDH 首次 collect 会拿到初始化假值，实测偏高近 7℃）。
    """
    q = _PdhQuery([_HI_TZ])
    if q.open():
        q.collect_and_read()          # 丢首帧
        return q, True
    q = _PdhQuery([_RAW_TZ])
    if q.open():
        q.collect_and_read()          # 丢首帧
        return q, False
    return None, False


def _cpu_temp_pdh():
    """PDH 直读 CPU 温度(℃)。多热区取**最高**（最热区代表 CPU；取平均会把
    主板/环境/电池热区混进来拉低读数）。不可用返回 None。"""
    if _pdh is None:
        return None
    now = time.time()
    with _tz_lock:
        if _tz_conn["q"] is None or now - _tz_conn["t"] > _TZ_TTL:
            if _tz_conn["q"] is not None:
                _tz_conn["q"].close()
            q, hi = _tz_open()
            _tz_conn.update(q=q, t=now, hi=hi)
        q = _tz_conn["q"]
        if q is None:
            return None
        try:
            vals = q.collect_and_read()       # 瞬时计数器，首次 collect 即可读
        except Exception:
            _tz_conn.update(q=None, t=0.0)
            return None
        hi = _tz_conn["hi"]
    raw = [float(v) for v in vals.values() if v and float(v) > 0]
    if not raw:
        return None
    mx = max(raw)
    return round((mx / 10.0 - 273.15) if hi else (mx - 273.15), 1)


_CPU_TEMP_CACHE = {"t": 0.0, "v": None}
# PDH 直读 ~1ms，可跟 1s 刷新同频（旧实现受限于 PowerShell 往返，只能 1.8s）
_CPU_TEMP_TTL = 1.0
_CPU_TEMP_HIST = []          # 中位数滤波窗口
_CPU_TEMP_EMA = {"v": None}


def _cpu_temp_filter(v):
    """ACPI 热区读数噪声大：实测 1s 间隔内就会 65.1 ↔ 71.1 反复跳（±3℃ 持续抖动）。
    窗口 5 中位数压掉连续尖峰 → EMA(0.35) 平滑到 ±0.5℃ 内；
    真实负载突变（>15℃，如空闲→满载）立即跟随，不迟钝。"""
    if v is None:
        return None
    _CPU_TEMP_HIST.append(v)
    del _CPU_TEMP_HIST[:-5]
    med = sorted(_CPU_TEMP_HIST)[len(_CPU_TEMP_HIST) // 2]
    prev = _CPU_TEMP_EMA["v"]
    if prev is None:
        out = med
    elif abs(med - prev) > 15:
        out = med                      # 负载突变，立即跟随
    else:
        out = round(prev * 0.65 + med * 0.35, 1)
    _CPU_TEMP_EMA["v"] = out
    return out


def cpu_temp():
    """CPU 温度(℃)：PDH 直读优先（~1ms），失败才回退 PowerShell CIM（~1s）。

    多热区取最高值；读数经中位数 + EMA 抑制 ACPI 固有抖动。读不到返回 None。
    """
    with _CPU_TEMP_LOCK:
        now = time.time()
        if now - _CPU_TEMP_CACHE["t"] < _CPU_TEMP_TTL:
            return _CPU_TEMP_CACHE["v"]
        v = None
        try:
            v = _cpu_temp_pdh()
        except Exception:
            v = None
        if v is None:                      # 部分机器没有该计数器集 → 回退旧路径
            try:
                rc, out = ps(_FAST_SCRIPT, timeout=12)
                for line in (out or "").splitlines():
                    line = line.strip()
                    if line.startswith("CPUTEMP="):
                        try:
                            v = float(line.split("=", 1)[1])
                        except Exception:
                            v = None
            except Exception:
                v = None
        v = _cpu_temp_filter(v)
        _CPU_TEMP_CACHE.update(t=now, v=v)
        return v


_GPU_AGG_CACHE = {"t": 0.0, "v": None}
_GPU_AGG_TTL = 30.0          # 纯兜底值（有 PDH 时被逐卡占用覆盖），拉长到 30s 省一次 WMI


def _gpu_aggregate_cached():
    """GPU 聚合占用（兜底）。WMI 枚举约 1 秒，缓存 10 秒。"""
    with _GPU_AGG_LOCK:
        now = time.time()
        if now - _GPU_AGG_CACHE["t"] < _GPU_AGG_TTL:
            return _GPU_AGG_CACHE["v"]
        v = _gpu_aggregate()
        _GPU_AGG_CACHE.update(t=now, v=v)
        return v


_WMI_PERF = {"t": 0.0, "v": None}


def _wmi_perf_fallback():
    """PDH 磁盘不可用时的回退：走 WMI PerfFormattedData。

    带 4 秒缓存：以前没有缓存，PDH 建不起来的老机器会退化成**每秒一个 PowerShell**，
    这是「老电脑一打开就卡」的隐藏放大器。
    """
    now = time.time()
    if _WMI_PERF["v"] is not None and now - _WMI_PERF["t"] < 4.0:
        return [dict(x) for x in _WMI_PERF["v"]]
    disks = []
    rc, out = ps(_perf_script(), timeout=25)
    for line in (out or "").splitlines():
        line = line.strip()
        if line.startswith("DISK|"):
            parts = line.split("|")
            if len(parts) >= 3:
                try:
                    util = max(0.0, min(100.0, float(parts[2])))
                except Exception:
                    util = None
                rd = wr = None
                try:
                    rd = float(parts[3])
                except Exception:
                    pass
                try:
                    wr = float(parts[4])
                except Exception:
                    pass
                disks.append({"name": parts[1], "util": util,
                              "read_mbps": rd, "write_mbps": wr})
    return disks


def _fast_fetch():
    """1 秒级：CPU 温度 + GPU 温度 + GPU 聚合占用 + 磁盘实时活动。
    （全部在后台线程调用，PowerShell / nvidia-smi 的开销不会卡住界面）"""
    disks, agg = disk_activity()
    if disks is None:
        disks = _wmi_perf_fallback()
        agg = (sum((d["util"] or 0) for d in disks) / len(disks)) if disks else None
    nv = _nvidia_smi_cached()
    gpu_temp = next((g["temp"] for g in nv if g.get("temp") is not None), None)
    return {"cpu_temp": cpu_temp(), "disks": disks, "disk": agg,
            "gpu": _gpu_aggregate_cached(), "gpu_temp": gpu_temp,
            "gpu_utils": _gpu_engine_utils(), "gpus": [],
            "realtime": True}


def _slow_fetch():
    """8 秒级：全显卡名单（PDH 引擎 LUID + nvidia-smi 温度/兜底占用）。"""
    gpus = _gpu_base()
    gpu_temp = next((g["temp"] for g in gpus if g.get("temp") is not None), None)
    return {"gpus": gpus, "gpu_temp": gpu_temp}


# --------------------------------------------------------------------------
# 温度后台采样：CPU 温度要走一次 PowerShell（约 0.5s），若放在刷新任务里会让
# 每秒的刷新耗时超过 1 秒而排队。这里用常驻后台线程每秒预热缓存，
# UI 读取时直接命中 → 温度照样 1 秒刷新，但刷新任务本身几乎零耗时。
# --------------------------------------------------------------------------
_sampler = {"thread": None, "last_read": 0.0}


def _temp_loop():
    while True:
        if _MON_PAUSED.is_set():
            # 窗口最小化 / 收进托盘：温度与 GPU 采样整体停掉 —— 不读热区、
            # 不起 nvidia-smi。恢复窗口时 UI 会立刻补一次，不必等下一轮。
            time.sleep(0.5)
            continue
        idle = (time.time() - _sampler["last_read"]) > 20
        try:
            cpu_temp()
        except Exception:
            pass
        if not idle:      # 没人看的时候连 nvidia-smi / GPU 引擎也不喂
            try:
                _nvidia_smi_cached()
            except Exception:
                pass
        # GPU Engine 按 pid 聚合 → _GPU_BY_PID（CPU 调试页进程表的 GPU 占用数据源）。
        # 必须常驻喂养：概览页不开时 perf_stats 不跑，进程表的 GPU 列会全空。
        # 函数内部有 ≥0.9s 节流，与 perf_stats 撞车安全
        try:
            _gpu_engine_utils()
        except Exception:
            pass
        # 最近没人读数据（停在别的页面）就降频，避免白耗 CPU
        time.sleep(5.0 if idle else 1.0)


def temp_sampler_start():
    """惰性启动温度采样线程（只启一次，daemon 线程随进程退出）。"""
    th = _sampler.get("thread")
    if th is not None and th.is_alive():
        return
    import threading
    th = threading.Thread(target=_temp_loop, name="wtb-temp-sampler", daemon=True)
    th.start()
    _sampler["thread"] = th


def perf_stats():
    """CPU temp, GPU list, disk list. Values are None when unsupported.

    两层缓存，合并后结构与老版本完全一致（UI 无需改字段）：
      - fast（1s）：CPU 温度 / GPU 温度 / GPU 聚合 / 磁盘实时活动 —— 负责「实时」；
      - slow（8s）：nvidia-smi 明细 —— 负责「完整」。
    """
    temp_sampler_start()          # 温度由后台线程预热，这里读到的永远是 1 秒内的新值
    _sampler["last_read"] = time.time()      # 有人在看 → 采样线程保持 1 秒频率
    now = time.time()

    fast = _fast_cache["data"]
    if fast is None or now - _fast_cache["t"] >= _FAST_TTL:
        with _fast_lock:
            if _fast_cache["data"] is None or time.time() - _fast_cache["t"] >= _FAST_TTL:
                fast = _fast_fetch()
                _fast_cache.update(t=time.time(), data=fast)
            else:
                fast = _fast_cache["data"]

    slow = _slow_cache["data"]
    if slow is None or now - _slow_cache["t"] >= _SLOW_TTL:
        with _slow_lock:
            if _slow_cache["data"] is None or time.time() - _slow_cache["t"] >= _SLOW_TTL:
                slow = _slow_fetch()
                _slow_cache.update(t=time.time(), data=slow)
            else:
                slow = _slow_cache["data"]

    data = dict(fast)
    data.update(slow)
    # GPU 温度用 1s 级的实时值（slow 里那份是 8s 的，只作兜底）
    ft = fast.get("gpu_temp")
    if ft is not None:
        data["gpu_temp"] = ft
    # 每块 GPU 的实时占用：PDH GPU Engine（全厂商通用）优先，NVIDIA 无 PDH 时用 nvidia-smi 兜底
    utils = data.get("gpu_utils") or {}
    if data.get("gpus"):
        for g in data["gpus"]:
            live = utils.get(g["luid"]) if g.get("luid") is not None else None
            g["util"] = live if live is not None else g.get("nv_util")
            # N 卡温度回填实时值，让 GPU 详情卡也每秒更新
            if ft is not None:
                nm = (g.get("name") or "").lower()
                if g.get("nv_util") is not None or "nvidia" in nm or "geforce" in nm:
                    g["temp"] = int(round(ft))
        vals = [g["util"] for g in data["gpus"] if g.get("util") is not None]
        if vals:
            data["gpu"] = max(0.0, min(100.0, sum(vals) / float(len(vals))))
    return data


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
        ".woff2": "font/woff2"}


class Handler(BaseHTTPRequestHandler):
    server_version = "WinToolbox/1.0"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path

        if p.startswith("/api/"):
            try:
                return self.api_get(p, q)
            except Exception as e:
                return self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)

        rel = p.lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(WEB, rel))
        if not full.startswith(WEB) or not os.path.isfile(full):
            return self._json({"ok": False, "error": "not found"}, 404)
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(os.path.splitext(full)[1].lower(),
                                                  "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            return self._json({"ok": False, "error": "not found"}, 404)
        try:
            return self.api_post(u.path, self._body())
        except Exception as e:
            return self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)

    # ---------------- GET ----------------
    def api_get(self, p, q):
        if p == "/api/health":
            return self._json({"ok": True, "data": {"admin": is_admin(), "python": sys.version.split()[0]}})

        if p == "/api/sysinfo":
            b = sys_basic()
            m = mem_status()
            return self._json({"ok": True, "data": {
                "os": {"caption": b["caption"], "version": b["version"], "arch": b["arch"]},
                "cpu": b["cpu"], "cores": b["cores"], "logical": b["logical"],
                "mem": {"total": m.ullTotalPhys, "avail": m.ullAvailPhys},
                "disks": disks(), "uptime": uptime_seconds(), "admin": is_admin(),
                "host": socket.gethostname(), "user": os.environ.get("USERNAME", ""),
            }})

        if p == "/api/metrics":
            m = mem_status()
            return self._json({"ok": True, "data": {
                "cpu": round(cpu_percent(), 1),
                "mem": round((m.ullTotalPhys - m.ullAvailPhys) * 100.0 / m.ullTotalPhys, 1),
                "mem_total": m.ullTotalPhys, "mem_used": m.ullTotalPhys - m.ullAvailPhys,
            }})

        if p == "/api/perf":
            return self._json({"ok": True, "data": perf_stats()})

        if p == "/api/netinfo":
            return self._json({"ok": True, "data": net_info()})

        if p == "/api/processes":
            limit = int(q.get("limit", ["50"])[0])
            return self._json({"ok": True, "data": list_processes(limit)})

        if p == "/api/rules":
            return self._json({"ok": True, "data": load_rules_with_state()})

        if p == "/api/software":
            return self._json({"ok": True, "data": list_software()})

        if p == "/api/startup":
            return self._json({"ok": True, "data": list_startup()})

        if p == "/api/services":
            return self._json({"ok": True, "data": list_services()})

        if p == "/api/network/diagnose":
            return self._json({"ok": True, "data": net_diagnose()})

        if p == "/api/cleanup/scan":
            return self._json({"ok": True, "data": scan_junk()})

        if p == "/api/policies/scan":
            return self._json({"ok": True, "data": policies_scan()})

        if p == "/api/network/dns":
            return self._json({"ok": True, "data": dns_test(q.get("mode", ["mixed"])[0])})

        if p == "/api/network/ping":
            return self._json({"ok": True, "data": ping_host(q.get("host", ["www.baidu.com"])[0])})

        return self._json({"ok": False, "error": "unknown endpoint " + p}, 404)

    # ---------------- POST ----------------
    def api_post(self, p, body):
        if p == "/api/memory/trim":
            mode = body.get("mode", "all")
            if mode == "all":
                ok, total = trim_all()
                return self._json({"ok": True, "data": {"trimmed": ok, "total": total}})
            pid = int(body.get("pid", 0))
            ok = trim_process(pid)
            return self._json({"ok": True, "data": {"pid": pid, "ok": ok}})

        if p == "/api/optimize/apply":
            rules = load_rules()
            return self._json({"ok": True, "data": snapshot_and_apply(rules, body.get("ids", []))})

        if p == "/api/optimize/restore":
            rules = load_rules()
            return self._json({"ok": True, "data": restore_rules(rules, body.get("ids", []))})

        if p == "/api/optimize/undo":
            j = journal_load()
            if not j:
                return self._json({"ok": False, "error": "没有可撤销的记录"}, 400)
            entry = j.pop(0)
            journal_save(j)
            fixed = 0
            for it in entry["items"]:
                try:
                    if it["existed"]:
                        reg_write(it["key"], it["value"], it["type"], it["data"])
                    else:
                        reg_delete(it["key"], it["value"])
                    fixed += 1
                except Exception:
                    pass
            return self._json({"ok": True, "data": {"time": entry["time"], "restored": fixed}})

        if p == "/api/soft/uninstall":
            cmd = body.get("cmd") or ""
            if not cmd:
                return self._json({"ok": False, "error": "缺少卸载命令"}, 400)
            if cmd.lower().startswith("msiexec"):
                rc, out = run(["cmd", "/c", cmd + " /qn /norestart"], timeout=300)
            else:
                rc, out = run(["cmd", "/c", "start", "", cmd], timeout=60)
            return self._json({"ok": True, "data": {"rc": rc, "note": out.strip()[:300]}})

        if p == "/api/startup/toggle":
            hive, sub, name = body["hive"], body["path"], body["name"]
            enable = bool(body.get("enable"))
            # 与桌面端一致：只改 StartupApproved 标志，不删 Run 值（删了就再也列不出来）
            r = set_startup_enabled(hive, sub, name,
                                    folder=bool(body.get("folder")), on=enable)
            if not r.get("ok"):
                return self._json({"ok": False, "error": r.get("msg") or "操作失败"}, 400)
            return self._json({"ok": True, "data": {"enable": enable}})

        if p == "/api/service":
            ok, note = set_service(body["name"], body["action"])
            return self._json({"ok": True, "data": {"ok": ok, "note": note}})

        if p == "/api/network/repair":
            return self._json({"ok": True, "data": net_repair(body.get("steps", []))})

        if p == "/api/cleanup/clean":
            return self._json({"ok": True, "data": clean_junk(body.get("ids", []))})

        if p == "/api/policies/fix":
            return self._json({"ok": True, "data": policies_fix(body.get("paths", []))})

        if p == "/api/win/restart":
            rc, _ = run(["shutdown", "/r", "/t", "30"])
            return self._json({"ok": True, "data": {"scheduled": rc == 0}})

        return self._json({"ok": False, "error": "unknown endpoint " + p}, 404)


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    url = "http://%s:%d/" % (HOST, PORT)
    print("=" * 62)
    print(" WinToolbox  ->  %s" % url)
    print(" admin        : %s" % ("yes" if is_admin() else "NO  (run as admin for registry/service changes)"))
    print(" python       : %s" % sys.version.split()[0])
    print(" Ctrl+C to stop")
    print("=" * 62)
    threading.Timer(0.8, lambda: webbrowser.open(url)).start() if not os.environ.get("WTB_NO_BROWSER") else None
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        srv.shutdown()


if __name__ == "__main__":
    main()
