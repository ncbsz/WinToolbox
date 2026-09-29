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
    _CHILD_SEM.acquire(timeout=30)
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


def reg_read(path, value):
    """Return (exists, type, data)."""
    if winreg is None:
        return False, None, None
    hive, sub = split_hive(path)
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT if hive == "HKCR" else
                            winreg.HKEY_LOCAL_MACHINE if hive == "HKLM" else
                            winreg.HKEY_CURRENT_USER, sub, 0, winreg.KEY_READ) as k:
            data, typ = winreg.QueryValueEx(k, value)
            return True, typ, data
    except FileNotFoundError:
        return False, None, None
    except OSError:
        return False, None, None


def reg_write(path, value, typ, data):
    hive, sub = split_hive(path)
    root = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER,
            "HKCR": winreg.HKEY_CLASSES_ROOT, "HKU": winreg.HKEY_USERS}[hive]
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
    root = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER,
            "HKCR": winreg.HKEY_CLASSES_ROOT, "HKU": winreg.HKEY_USERS}[hive]
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


def export_key(path, outfile):
    rc, _ = run(["reg", "export", path, outfile, "/y"])
    return rc == 0


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
                key = (info["pnp"], info["name"])
                if key in seen:
                    continue
                seen.add(key)
                info["_score"] = _monitor_score(info)
                out.append(info)

    # 排序：先"像真屏"的（型号名正常 / 厂商可识别 / 尺寸在面板常见区间），
    # 再按尺寸从大到小 —— 虚拟显示器驱动常伪造 EDID（型号写成 HDMI2.0、尺寸离谱），
    # 这样真正的内置屏 / 外接屏会排在前面，虚拟屏退到提示里。
    out.sort(key=lambda x: (-x.pop("_score", 0), -(x["inch"] or 0)))
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
    _MEM_MODULES["v"] = res
    return res


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


def rule_state(rule):
    """Return True if the rule currently reads as 'already optimized'."""
    ck = rule.get("checkKey")
    if not ck:
        return None
    ex, typ, data = reg_read(ck, rule.get("checkValue", ""))
    want = rule.get("optimizedValue")
    # optimizedValue 为 None 表示"该值不应存在"（例如删除强制值、恢复系统默认），
    # 此时读到值反而不算优化。
    if want is None:
        return not ex
    if not ex:
        return False
    try:
        return int(data) == int(want)
    except Exception:
        return str(data) == str(want)


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
                reg_write(path, value, typ, op["data"])
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

    字段比 Geek Uninstaller 需要的更全：除名称/版本/发布者/大小/日期外，还包括
    `uninstall`/`quiet`（卸载命令）、`location`（安装目录）、`icon`（图标）、
    `modify`（修改/修复命令）、`url`（官网）、`bit`（32/64 位）、`kb`（是否系统更新补丁）。
    缓存让「切换分类 / 排序 / 重新搜索」不再反复读注册表（Geek 用 XML 缓存达到同样效果）。
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
# 软件条目右键动作（对齐 Geek Uninstaller 的右键菜单）
#
# 这些动作会**真的操作系统**（打开资源管理器 / 浏览器 / 注册表、删注册表键），
# 因此统一写成可打桩的普通函数；设环境变量 WTB_NO_BROWSER=1 可让它们在测试中
# 只返回成功、不弹任何窗口。
# --------------------------------------------------------------------------
_HIVE_ROOTS = {
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
    """在 Microsoft Store 里搜索该程序（Geek 的「在商店中打开」）。"""
    from urllib.parse import quote
    return open_url("ms-windows-store://search/?query=" + quote(name or ""))


def _user_reg_root_label():
    """regedit 的 LastKey 前缀：简体中文系统是「计算机」，其它是「Computer」。"""
    try:
        import ctypes
        lang = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        if (lang & 0x3ff) == 0x04:                  # 0x0804 = 简体中文
            return "计算机"
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
    root_name = _HIVE_ROOTS.get(hive_s)
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
    """强制移除程序的 Uninstall 条目（Geek 的「强制移除条目」）。

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
# 软件卸载执行（参照 HiBit Uninstaller / Geek Uninstaller 的行为）
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


def uninstall_software(key_path, cmd, timeout=1800):
    """执行卸载并**等它真正结束**（后台线程调用，可能运行几十分钟）。

    - MSI（含 {GUID}）：直接用 `msiexec /x {GUID} /qb /norestart`，不照搬原串；
    - 普通 exe：解析出 exe 与参数，exe 加引号后交 CreateProcess（不走 cmd，避免
      空格/引号解析坑）；卸载程序文件不存在时直接报"疑似残留注册表项"。
    完成判据：顶层进程退出 **且** Uninstall 注册表键消失。
    """
    cmd = (cmd or "").strip()
    if not cmd:
        return {"ok": False, "msg": "该程序没有卸载命令"}
    guid = _msi_guid(cmd)
    exe = ""
    if guid and re.search(r"msiexec", cmd, re.I):
        exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                           "System32", "msiexec.exe")
        line = '"%s" /x %s /qb /norestart' % (exe, guid)
    else:
        exe, args = parse_uninstall_cmd(cmd)
        if not exe:
            return {"ok": False, "msg": "无法解析卸载命令"}
        if exe.lower().endswith((".bat", ".cmd")):
            line = '"%s" %s' % (os.environ.get("ComSpec", "cmd.exe"),
                                subprocess.list2cmdline([exe] + (args.split() if args else [])))
        else:
            probe = exe if os.path.isabs(exe) else (shutil.which(exe) or exe)
            if os.path.isabs(probe) and not os.path.exists(probe):
                return {"ok": False, "exe": exe, "left": True,
                        "msg": "卸载程序文件不存在：%s\n该注册表项可能是卸载残留，"
                               "可直接用「残留扫描」清理。" % exe}
            line = '"%s" %s' % (probe, args)
    try:
        proc = subprocess.Popen(line, shell=False)
    except Exception as e:
        return {"ok": False, "exe": exe, "msg": "启动卸载程序失败：%s" % str(e)[:160]}
    t0 = time.time()
    deadline = t0 + max(60, int(timeout))
    exited_at = None
    while time.time() < deadline:
        if exited_at is None:
            try:
                proc.wait(timeout=2)
                exited_at = time.time()
            except subprocess.TimeoutExpired:
                pass
        gone = not _arp_key_exists(key_path)
        if gone:
            break
        # 进程已退出但键还在：用户可能取消了卸载；给 20 秒宽限再收工
        if exited_at is not None and time.time() - exited_at > 20:
            break
        time.sleep(1.5)
    left = _arp_key_exists(key_path)
    return {"ok": not left, "exe": exe, "left": left,
            "secs": int(time.time() - t0),
            "msg": "" if not left else
                   "卸载程序已结束，但程序仍在「已安装」列表中（可能被取消、"
                   "或需要重启后完成）。"}


# ---- 残留扫描（参照 HiBit Uninstaller / Geek Uninstaller 的检查位置）----
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
    """编辑距离是否 ≤ cap（快速带长度/首字符预筛）。Geek 用同款思路容错
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


def _scan_tasks(full, words, cap):
    """计划任务残留：卸载程序常在计划任务里留下自动更新/守护任务。
    `\\Microsoft\\...` 开头的系统任务一律跳过。"""
    out = []
    try:
        rc, o = run(["schtasks", "/query", "/fo", "csv", "/nh"], timeout=60)
    except Exception:
        return out
    for line in (o or "").splitlines():
        m = re.match(r'\s*"([^"]+)"', line)
        if not m:
            continue
        name = m.group(1).lstrip("\\")
        if not name or name.lower().startswith("microsoft\\"):
            continue
        if _res_hit(name.split("\\")[-1], full, words):
            out.append({"hive": "TASK", "path": name, "kind": "task",
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
    if not name:
        return False
    try:
        rc, _o = run(["schtasks", "/query", "/tn", name], timeout=30)
        return rc == 0
    except Exception:
        return False


def _item_exists(it):
    """快照项是否仍然存在（按 kind 分派到各自的检查方式）。"""
    kind = (it or {}).get("kind") or "reg"
    if kind == "cred":
        return _cred_exists(it.get("path"), it.get("ctype") or 1)
    if kind == "task":
        return _task_exists(it.get("path"))
    return _reg_key_exists(it.get("hive"), it.get("path"), it.get("value") or "")


def install_dir_candidates(key_path="", location="", uninstall_cmd=""):
    """多来源推断安装目录（对齐 Geek 的 CInstallLocationFinder 思路）：
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
    """MSI 安装目录索引（Geek 同样读这个键）：值名即目录路径，
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
    """卸载前基线（HiBit 的 first snapshot 思路）：记录当前与程序相关的
    目录与注册表项。卸载后复查其中**仍存在**的项 —— 语义上就是确定的残留。"""
    r = residual_scan(name, publisher, location, uninstall_cmd, key_path, cap=80)
    _SOFT_SNAP[key_path or name] = {
        "files": [f["path"] for f in r.get("files") or []],
        "reg": r.get("reg") or [],
        "t": time.time(),
    }
    return {"files": len(r.get("files") or []), "reg": len(r.get("reg") or [])}


def snapshot_get(key_path, name=""):
    return _SOFT_SNAP.get(key_path or name)


def residual_scan(name, publisher="", location="", uninstall_cmd="",
                  key_path="", base=None, cap=80):
    """卸载后的残留扫描：只定位、不删除。返回 {"files", "reg", "tokens"}。

    覆盖范围（对齐 HiBit / Geek 的检查位置）：
      目录：安装目录多来源（InstallLocation / Inno Setup: App Path / 卸载程序目录）、
            ProgramFiles(+x86) / ProgramData / LOCALAPPDATA / LOCALAPPDATA\\Programs /
            APPDATA / LocalLow（两层）、各盘根目录、桌面与开始菜单快捷方式、
            **启动文件夹**、Recent 使用痕迹（一层）、**用户主目录点目录**（.vscode 等）、
            WER 崩溃报告（两层）；
      注册表：SOFTWARE（含 WOW6432Node）两层、四处已安装列表、MSI Installer\\Folders
              索引、Run/RunOnce 启动项、服务键、**事件日志源**；
      登录数据 / 守护：**Windows 凭据管理器**（CredEnumerateW，按程序名匹配 target）、
              **计划任务**（schtasks，系统任务除外）；
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

    # 0) 卸载前快照中仍存在的项 —— 语义上最确定的残留，放最前
    if base:
        for p in base.get("files") or []:
            if os.path.exists(p):
                add(p, "卸载前已有")

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
    """删除所选残留。文件直接删除（仅限白名单根目录下），注册表整键递归删除。

    返回 {"ok": bool, "msg": [...]}，逐条报告失败原因。
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
    for it in files or []:
        p = (it.get("path") or "").strip()
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
        try:
            if os.path.isdir(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
            msgs.append("已删除：%s" % p)
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
            try:
                rc, o = run(["schtasks", "/delete", "/tn", name, "/f"], timeout=60)
                if rc == 0:
                    msgs.append("已删除计划任务：%s" % name)
                else:
                    ok = False
                    msgs.append("删除任务失败：%s（%s）"
                                % (name, (o or "").strip()[:60]))
            except Exception as e:
                ok = False
                msgs.append("失败：%s（%s）" % (name, str(e)[:60]))
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
                _delete_tree(root, path)
                msgs.append("已删除注册表：HK%s\\%s"
                            % ("LM" if hive == "HKLM" else "CU", path))
        except Exception as e:
            ok = False
            msgs.append("失败：%s\\%s（%s）" % (hive, path, str(e)[:80]))
    return {"ok": ok, "msg": msgs}


# --------------------------------------------------------------------------
# 全机「卸载残留」扫描（参照 BCUninstaller 的 Orphan / Leftover 检测）
#
# 与 residual_scan 的分工：那个是「卸载某个程序之后」针对它的复查；
# 本函数不需要任何选中项，直接找整机里**已经失去主人**的东西：
#   A 孤儿卸载项  —— ARP 里有条目，但它的卸载程序已经不存在了（最确定的残留）
#   B 孤儿启动项 / 孤儿服务 —— 指向的可执行文件已不存在
#   C 失效快捷方式 —— 桌面 / 开始菜单里指向不存在目标的 .lnk
#   D 疑似残留目录 —— 常见位置下没有任何已注册程序认领的目录（需人工确认，
#      默认不勾选；BCUninstaller 对同类结果同样标为 Questionable）
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
}
# 目录名"像系统本体"的前缀：windows* / microsoft* 一律不碰
_IMMUNE_PREFIXES = ("windows", "microsoft", "system32", "syswow64", "$", ".")


def _norm_path(p):
    """规范化路径用于比对（小写、去尾部分隔符、展开环境变量）。"""
    try:
        s = os.path.expandvars((p or "").strip().strip('"'))
        if not s:
            return ""
        return os.path.normcase(os.path.abspath(s)).rstrip("\\/")
    except Exception:
        return ""


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


def _orphan_dirs(claimed, toks, cap):
    """D. 疑似残留目录：常见位置下无人认领的目录（低置信 → 默认不勾选）。"""
    out = []
    roots = []
    for kk in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData",
               "LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        p = os.environ.get(kk)
        if p and os.path.isdir(p):
            roots.append(p)
    local = os.environ.get("LOCALAPPDATA") or ""
    if local and os.path.isdir(os.path.join(local, "Programs")):
        roots.append(os.path.join(local, "Programs"))
    for drive in ("C", "D", "E", "F", "G"):
        r = drive + ":\\"
        if os.path.isdir(r):
            roots.append(r)
    # 收集阶段放宽，最后按体积取前 N —— 边扫边截断会留下"先扫到的小目录"，
    # 用户最该看到的大块残留反而进不来
    scan_limit = max(60, min(150, cap * 4))
    for base in roots:
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for nm in names:
            if len(out) >= scan_limit:
                break
            full = os.path.join(base, nm)
            if not os.path.isdir(full) or os.path.islink(full):
                continue
            nml = nm.lower()
            if nml in _SKIP_ENTRIES or nml in _IMMUNE_DIRS:
                continue
            if nml.startswith(_IMMUNE_PREFIXES):
                continue
            np = _norm_path(full)
            if not np:
                continue
            if np in claimed or any(np.startswith(c + os.sep) for c in claimed):
                continue                       # 有程序认领（是它的安装目录）
            if _res_score(nm, toks, set()) > 0:  # 名字与某个已装软件一致 → 有主
                continue
            # 子串认领：目录名与已装软件名互为子串也算有主
            # （如 "Code" ↔ "Visual Studio Code"、"dingtalk" ↔ "DingTalk 钉钉"）
            nmn = re.sub(r"[^\w\u4e00-\u9fff]+", "", nml)
            if len(nmn) >= 4 and any(len(t) >= 4 and (nmn in t or t in nmn)
                                     for t in toks):
                continue
            try:
                sz = dir_size(full, cap=600)[0]
            except Exception:
                sz = 0
            out.append({"path": full, "kind": "dir", "bytes": sz,
                        "tag": "疑似残留目录", "weak": True})
    # 体积大的更值得看；总数收紧，避免对话框被低置信噪音淹没
    out.sort(key=lambda x: -x["bytes"])
    return out[:min(cap, 45)]

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


def orphan_scan(cap=120):
    """全机卸载残留扫描（不需要选中程序）。

    返回结构与 residual_scan 相同（files / reg），可直接交给残留清理对话框 ——
    删除沿用同一套安全白名单与注册表分类规则。
    高置信项（孤儿卸载项 / 启动项 / 服务 / 失效快捷方式）排在前，
    低置信项（疑似残留目录）标 weak 由用户确认。
    """
    claimed, toks, inst = orphan_index()
    files, regs = [], []
    regs += _orphan_entries(inst, cap)
    regs += _orphan_startup(cap)
    regs += _orphan_services(cap)
    files += _broken_shortcuts(cap)
    files += _orphan_dirs(claimed, toks, cap)
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


def list_startup():
    items = []
    if winreg is None:
        return items
    for hive, sub in STARTUP_LOCATIONS:
        root = winreg.HKEY_LOCAL_MACHINE if hive == "HKLM" else winreg.HKEY_CURRENT_USER
        try:
            with winreg.OpenKey(root, sub, 0, winreg.KEY_READ) as k:
                n = winreg.QueryInfoKey(k)[1]
                for i in range(n):
                    try:
                        nm, val, _ = winreg.EnumValue(k, i)
                        items.append({"hive": hive, "path": sub, "name": nm,
                                      "cmd": val, "exe": _exe_from_path(val)})
                    except OSError:
                        continue
        except FileNotFoundError:
            continue
    folders = [
        os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup"),
        os.path.join(os.environ.get("PROGRAMDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup"),
    ]
    for fd in folders:
        if os.path.isdir(fd):
            for f in os.listdir(fd):
                full = os.path.join(fd, f)
                items.append({"hive": "DIR", "path": fd, "name": f,
                              "cmd": full, "exe": full if os.path.isfile(full) else ""})
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


def set_service(name, action):
    if action == "stop":
        rc, o = run(["sc", "stop", name])
    elif action == "start":
        rc, o = run(["sc", "start", name])
    elif action == "disable":
        rc, o = run(["sc", "config", name, "start=", "disabled"])
    elif action == "auto":
        rc, o = run(["sc", "config", name, "start=", "auto"])
    elif action == "manual":
        rc, o = run(["sc", "config", name, "start=", "demand"])
    else:
        return False, "unknown action"
    return rc == 0, o.strip()[:200]


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
    """启用 / 禁用计划任务。full_name 形如 \\Microsoft\\Windows\\...\\任务名。"""
    rc, o = run(["schtasks", "/change", "/tn", full_name,
                 "/enable" if enable else "/disable"], timeout=60)
    return rc == 0, (o or "").strip()[:200]


def _svc_start(name):
    """Return the 'Start' DWORD of a service, or None if not readable."""
    ex, typ, data = reg_read("HKLM\\SYSTEM\\CurrentControlSet\\Services\\" + name, "Start")
    if ex and data is not None:
        try:
            return int(data)
        except Exception:
            return None
    return None


def update_status():
    start = _svc_start("wuauserv")
    return {"enabled": start is not None and int(start) < 4, "start": start}


def set_update(enabled):
    """One-click enable/disable Windows Update (wuauserv / UsoSvc / WaaSMedicSvc)."""
    results = []
    for name, auto in (("wuauserv", 2), ("UsoSvc", 2), ("WaaSMedicSvc", 3)):
        start = 4 if not enabled else auto
        rc, out = run(["reg", "add", "HKLM\\SYSTEM\\CurrentControlSet\\Services\\" + name,
                       "/v", "Start", "/t", "REG_DWORD", "/d", str(start), "/f"])
        results.append({"name": name, "ok": rc == 0})
    if not enabled:
        reg_write("HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate\\AU",
                  "NoAutoUpdate", "REG_DWORD", 1)
    else:
        reg_delete("HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate\\AU",
                   "NoAutoUpdate")
    return results


def defender_status():
    start = _svc_start("WinDefend")
    return {"enabled": start is not None and int(start) < 4, "start": start}


def set_defender(enabled):
    """One-click enable/disable Windows Defender (WinDefend / WdNisSvc / SecurityHealthService)."""
    results = []
    for name, auto in (("WinDefend", 2), ("WdNisSvc", 3), ("SecurityHealthService", 2)):
        start = 4 if not enabled else auto
        rc, out = run(["reg", "add", "HKLM\\SYSTEM\\CurrentControlSet\\Services\\" + name,
                       "/v", "Start", "/t", "REG_DWORD", "/d", str(start), "/f"])
        results.append({"name": name, "ok": rc == 0})
    if not enabled:
        reg_write("HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows Defender",
                  "DisableAntiSpyware", "REG_DWORD", 1)
    else:
        reg_delete("HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows Defender",
                   "DisableAntiSpyware")
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


def trim_process(pid):
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_SET_QUOTA = 0x0100
    h = _k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA, False, pid)
    if not h:
        return False
    try:
        _k32.SetProcessWorkingSetSize(h, ctypes.c_size_t(-1).value, ctypes.c_size_t(-1).value)
        return True
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
def junk_targets():
    """可清理项清单。

    每项带：
      level — recommend(建议清理) / ok(可以清理) / caution(谨慎清理)
      what  — 这些文件是什么、删了会怎样
    """
    win = os.environ.get("SystemRoot", r"C:\Windows")
    lad = os.environ.get("LOCALAPPDATA", "")
    return [
        {"id": "user_temp", "name": "用户临时文件 (%TEMP%)",
         "path": os.environ.get("TEMP", ""), "kind": "clear",
         "level": "recommend",
         "what": "各软件运行时产生的临时文件（安装包解包、编辑器缓存等）。"
                 "可以放心删除，正在被占用的文件会自动跳过。"},
        {"id": "win_temp", "name": "系统临时文件 (Windows\\Temp)",
         "path": os.path.join(win, "Temp"), "kind": "clear",
         "level": "recommend",
         "what": "系统与软件安装/更新时留下的临时文件，删除安全。"},
        {"id": "prefetch", "name": "预读取文件 (Prefetch)",
         "path": os.path.join(win, "Prefetch"), "kind": "clear",
         "level": "caution",
         "what": "系统为加速程序启动而预生成的读取缓存（.pf）。"
                 "删掉系统会重建，但短期内程序启动会明显变慢，一般建议保留。"},
        {"id": "wu_cache", "name": "Windows 更新缓存",
         "path": os.path.join(win, "SoftwareDistribution", "Download"), "kind": "clear",
         "level": "recommend",
         "what": "已下载并安装完成的更新安装包，删掉不影响已经装好的更新，通常最占空间。"},
        {"id": "wu_logs", "name": "Windows 更新日志",
         "path": os.path.join(win, "Logs", "WindowsUpdate"), "kind": "clear",
         "level": "ok",
         "what": "更新过程的记录日志，只在排查“更新失败”时才需要，平时可以清理。"},
        {"id": "crash", "name": "崩溃转储 / WER 报告",
         "path": os.path.join(lad, "CrashDumps"), "kind": "clear",
         "level": "ok",
         "what": "程序崩溃时生成的内存转储与错误报告，只有要追查崩溃原因时才需要保留。"},
        {"id": "thumb", "name": "缩略图缓存 (Explorer)",
         "path": os.path.join(lad, "Microsoft", "Windows", "Explorer"), "kind": "files",
         "level": "ok",
         "what": "资源管理器为图片/视频生成的缩略图数据库。删除后会自动重建，"
                 "只是第一次浏览大文件夹时会稍慢。"},
        {"id": "inetcache", "name": "IE / 系统网络缓存",
         "path": os.path.join(lad, "Microsoft", "Windows", "INetCache"), "kind": "clear",
         "level": "recommend",
         "what": "系统组件与 IE 内核的网页缓存和临时下载，删除安全。"},
        {"id": "chrome", "name": "Chrome 缓存",
         "path": os.path.join(lad, "Google", "Chrome", "User Data", "Default", "Cache"),
         "kind": "clear",
         "level": "ok",
         "what": "Chrome 缓存的网页图片/脚本等。清理后网页首次打开会重新下载变慢；"
                 "不含书签、密码、登录状态。"},
        {"id": "edge", "name": "Edge 缓存",
         "path": os.path.join(lad, "Microsoft", "Edge", "User Data", "Default", "Cache"),
         "kind": "clear",
         "level": "ok",
         "what": "Edge 缓存的网页资源。清理后首次打开网页会重新加载；"
                 "不影响收藏夹与登录状态。"},
    ]


# 回收站（单独一项，说明与等级同样标注）
JUNK_RECYCLE = {
    "id": "recyclebin", "name": "回收站", "path": "SHELL:RecycleBin",
    "kind": "recycle", "level": "caution",
    "what": "回收站里是你自己删除过的文件。清空后无法找回，请先确认没有想恢复的内容。",
}


def dir_size(path, cap=200000):
    total = 0
    n = 0
    if not path or not os.path.isdir(path):
        return 0, 0
    for dp, dn, fn in os.walk(path):
        for f in fn:
            try:
                total += os.path.getsize(os.path.join(dp, f))
                n += 1
            except OSError:
                pass
            if n > cap:
                return total, n
    return total, n


def recycle_bin_size():
    """回收站占用（Shell.Application COM，单独查询便于并行）。"""
    try:
        rc, o = ps("(New-Object -ComObject Shell.Application).NameSpace(0xA).Items() | "
                   "Measure-Object -Property Size -Sum | Select-Object -ExpandProperty Sum",
                   timeout=60)
        return int(float((o or "0").strip() or 0))
    except Exception:
        return 0


def junk_item_size(tid):
    """只扫描一个垃圾项（供 UI 逐项并行统计）。"""
    if tid == "recyclebin":
        return dict(JUNK_RECYCLE, size=recycle_bin_size(), files=0)
    for t in junk_targets():
        if t["id"] != tid:
            continue
        size, n = dir_size(t["path"])
        return dict(t, size=size, files=n)
    return None


def scan_junk():
    """全量扫描（并行）。UI 目前用 junk_item_size 逐项显示，此函数保留作兜底。"""
    tids = [t["id"] for t in junk_targets()] + ["recyclebin"]
    out = {}
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as ex:
            for r in ex.map(junk_item_size, tids):
                if r:
                    out[r["id"]] = r
    except Exception:
        for t in tids:
            r = junk_item_size(t)
            if r:
                out[r["id"]] = r
    order = [t["id"] for t in junk_targets()] + ["recyclebin"]
    return [out[i] for i in order if i in out]


def clean_junk(ids):
    results = []
    for t in junk_targets():
        if t["id"] not in ids:
            continue
        freed = 0
        errors = 0
        if t["kind"] == "recycle":
            rc, _ = ps("Clear-RecycleBin -Force -ErrorAction SilentlyContinue")
            results.append({"id": t["id"], "name": t["name"], "freed": 0, "ok": True})
            continue
        if not os.path.isdir(t["path"]):
            results.append({"id": t["id"], "name": t["name"], "freed": 0, "ok": True, "note": "目录不存在"})
            continue
        for entry in os.listdir(t["path"]):
            full = os.path.join(t["path"], entry)
            if t["kind"] == "files" and entry.lower() not in ("thumbcache_256.db", "thumbcache_1024.db",
                                                              "iconcache_256.db", "iconcache_1024.db"):
                continue
            try:
                if os.path.isdir(full):
                    s, _ = dir_size(full)
                    shutil.rmtree(full, ignore_errors=True)
                    freed += s
                else:
                    freed += os.path.getsize(full)
                    os.remove(full)
            except Exception:
                errors += 1
        results.append({"id": t["id"], "name": t["name"], "freed": freed,
                        "ok": errors == 0, "errors": errors})
    if "recyclebin" in ids and not any(r["id"] == "recyclebin" for r in results):
        ps("Clear-RecycleBin -Force -ErrorAction SilentlyContinue")
    return results


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
    """清除策略残留：每个键先 reg export 备份到 BACKUP 目录，再删除。"""
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
        run(["reg", "export", p, outfile, "/y"])
        rc, out = run(["reg", "delete", p, "/f"])
        if os.path.exists(outfile):
            backups.append(outfile)
        results.append({"path": p, "ok": rc == 0,
                        "backup": outfile if os.path.exists(outfile) else None})
    # the pause-to-2999 values live outside Policies, delete them individually
    for path, names, why in POLICY_EXTRA_VALUES:
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
# CPU 调试：大小核拓扑 / 电源计划 / 处理器状态 / EPP / 进程亲和 / E-core 开关
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
    """powercfg /list -> [{'guid','name','active'}]，并附当前活跃计划。"""
    rc, out = run(["powercfg", "/list"], timeout=30)
    plans = []
    for line in (out or "").splitlines():
        m = re.search(r"([0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})\s+(?:\((.+?)\))?", line)
        if not m:
            continue
        plans.append({"guid": m.group(1), "name": (m.group(2) or "").strip(),
                      "active": "*" in line or "*" in line})
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


# --- 本机 .pow 扫描 ------------------------------------------------------
_POW_MAX_FILES = 400
_POW_MAX_DEPTH = 2          # 每个扫描根最多下探两层，避免全盘遍历拖慢界面

# KNOWNFOLDERID —— 走 Shell API 取「真实」用户目录：
# 很多机器把桌面/下载重定向到别的盘（D:\桌面 之类），
# 直接拼 %USERPROFILE%\Desktop 会扫不到，所以这里必须问系统。
_KNOWN_FOLDERS = {
    "Desktop":   "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "Downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "Documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
}


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
    """取真实用户目录（含被重定向到其它盘的桌面 / 下载 / 文档）。失败返回 ""。"""
    gid = _KNOWN_FOLDERS.get(name)
    if not gid or os.name != "nt":
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


def _powercfg_set_hidden():
    run(["powercfg", "-attributes", _PROC_SUB, _EPP_GUID, "-ATTRIB_HIDE"], timeout=30)


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
    """读取指定电源计划的处理器参数（不传则读当前生效计划）。"""
    scheme = guid if _is_guid(guid) else "SCHEME_CURRENT"
    _powercfg_set_hidden()
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
    if kind == "epp":
        _powercfg_set_hidden()
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
    """把进程绑定到 P 核 / E 核 / 全部核心。target: 'P' | 'E' | 'ALL'。"""
    topo = cpu_topology()
    if not topo:
        return {"ok": False, "err": "无法读取 CPU 拓扑"}
    if target == "P":
        logicals = topo["p_logicals"]
    elif target == "E":
        logicals = topo["e_logicals"]
    else:
        logicals = list(range(topo["logical"]))
    if not logicals:
        return {"ok": False, "err": "该组核心为空（本机可能没有大小核）"}
    mask = 0
    for l in logicals:
        mask |= (1 << l)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_SET_INFORMATION = 0x0200
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = k32.OpenProcess(PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION,
                        False, int(pid))
    if not h:
        return {"ok": False, "err": "OpenProcess 失败（权限/系统进程）"}
    try:
        r = k32.SetProcessAffinityMask(ctypes.c_void_p(h), ctypes.c_size_t(mask))
        if not r:
            return {"ok": False, "err": "SetProcessAffinityMask 失败"}
        return {"ok": True, "mask": mask}
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


def ecore_status():
    """读 bcdedit 当前 numproc（E-core 全局禁用开关的状态）。"""
    rc, out = run(["bcdedit", "/enum", "{current}"], timeout=30)
    m = re.search(r"numproc\s+(0x[0-9a-fA-F]+|\d+)", out or "", re.I)
    if m:
        v = m.group(1)
        return {"set": True, "numproc": int(v, 16) if v.startswith("0x") else int(v)}
    return {"set": False, "numproc": None}


def ecore_disable():
    """全局禁用 E-core：bcdedit 设 numproc = P 核逻辑处理器数（需重启生效）。"""
    topo = cpu_topology()
    if not topo or not topo["p_logicals"]:
        return {"ok": False, "err": "未检测到 P 核（本机可能不是大小核架构）"}
    n = len(topo["p_logicals"])
    rc, out = run(["bcdedit", "/set", "{current}", "numproc", str(n)], timeout=30)
    ok = rc == 0
    return {"ok": ok, "numproc": n if ok else None,
            "err": "" if ok else (out or "").strip()[:120]}


def ecore_restore():
    """恢复全部核心：删除 numproc 启动项（需重启生效）。"""
    rc, out = run(["bcdedit", "/deletevalue", "{current}", "numproc"], timeout=30)
    ok = rc == 0
    return {"ok": ok, "err": "" if ok else (out or "").strip()[:120]}


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
    if not snap or snap == ctypes.c_void_p(-1).value:
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
        if pid is None:
            h = _k32.GetCurrentProcess()
            close = False
            k32 = _k32
        else:
            k32, h = _open_process(pid)
            close = True
        if not h:
            return {"ok": False, "err": "OpenProcess 失败"}
        try:
            r = psapi.EmptyWorkingSet(ctypes.c_void_p(h))
            return {"ok": bool(r), "err": "" if r else "EmptyWorkingSet 失败"}
        finally:
            if close:
                k32.CloseHandle(ctypes.c_void_p(h))
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
_GPU_VALUE_KEYS = ("DriverDesc", "HardwareInformation.AdapterString",
                   "HardwareInformation.ChipType")
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
                                    "backup": bool(rec.get("values"))})
                except OSError:
                    continue
    except FileNotFoundError:
        return []
    out.sort(key=lambda x: x["index"])
    return out


def gpu_alias_apply(index, new_name):
    """把指定显卡的显示名改成 new_name（改前自动备份原值）。返回 {ok, msg}。"""
    if winreg is None:
        return {"ok": False, "msg": "当前环境无法访问注册表"}
    new_name = (new_name or "").strip()
    if not new_name:
        return {"ok": False, "msg": "请填写要显示的显卡名称"}
    if len(new_name) > 64:
        return {"ok": False, "msg": "名称过长（≤64 字符）"}
    idx = str(index).zfill(4)
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _GPU_CLASS + "\\" + idx,
                            0, winreg.KEY_READ | winreg.KEY_WRITE) as sk:
            cur = {}
            for v in _GPU_VALUE_KEYS:
                try:
                    cur[v] = winreg.QueryValueEx(sk, v)
                except OSError:
                    continue
            if not cur:
                return {"ok": False, "msg": "该适配器没有可改写的名称值"}
            bk = _gpu_backup_load()
            if idx not in bk:                     # 只在首次改写时记录原值
                bk[idx] = {"alias": new_name,
                           "values": {k: [v[0], int(v[1])] for k, v in cur.items()}}
            else:
                bk[idx]["alias"] = new_name
            for v, val in cur.items():
                if isinstance(val[0], str):
                    winreg.SetValueEx(sk, v, 0, val[1], new_name)
        _gpu_backup_save(bk)
        return {"ok": True,
                "msg": "已改名为：%s（重新枚举显示设备或重启后可见）" % new_name}
    except PermissionError:
        return {"ok": False, "msg": "写入被拒绝（需要管理员权限）"}
    except OSError as e:
        return {"ok": False, "msg": "写入失败：%s" % e}


def gpu_alias_restore(index):
    """把指定显卡的显示名还原成备份的原值。"""
    if winreg is None:
        return {"ok": False, "msg": "当前环境无法访问注册表"}
    idx = str(index).zfill(4)
    bk = _gpu_backup_load()
    rec = bk.get(idx)
    if not rec or not rec.get("values"):
        return {"ok": False, "msg": "没有该适配器的备份记录"}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _GPU_CLASS + "\\" + idx,
                            0, winreg.KEY_WRITE) as sk:
            for v, pair in rec["values"].items():
                try:
                    winreg.SetValueEx(sk, v, 0, int(pair[1]), pair[0])
                except OSError:
                    continue
        bk.pop(idx, None)
        _gpu_backup_save(bk)
        return {"ok": True, "msg": "已还原为原显示名"}
    except PermissionError:
        return {"ok": False, "msg": "写入被拒绝（需要管理员权限）"}
    except OSError as e:
        return {"ok": False, "msg": "还原失败：%s" % e}


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
    "try{$a=@(Get-NetAdapter -ErrorAction SilentlyContinue|Where-Object{$_.Status -eq 'Up'})}catch{};"
    "foreach($x in $a){$ls='';try{$ls=[string]$x.LinkSpeed}catch{};"
    "$mt='';try{$mt=[string]$x.MediaType}catch{};"
    "$ni='';try{$ni=[string]$x.InterfaceDescription}catch{};"
    "$sp='';try{$sp=[string]$x.Speed}catch{};"
    "'NET|'+$x.Name+'|'+$mt+'|'+$ls+'|'+$ni+'|'+$sp}"
)


_net_adapters_cache = {"t": 0.0, "data": None}
_NET_ADAPTERS_TTL = float("inf")   # 进程内永久：网卡静态信息只在启动时查一次，
                                 # 之后每秒轮询不再起 PowerShell（要更新用 force=True）
_net_adapters_lock = threading.Lock()


def net_adapters(force=False):
    """在线网卡的静态信息（名称 / 介质 / 协商速率 / 描述）。

    这项信息几乎不变（只在插拔网线/切换网卡时变化），但每次都要起
    PowerShell（往返 1~2s）。UI 每秒轮询一次，若不缓存会把请求线程
    拖到秒级，表现出来就是「网络监测卡死 / 没反应」——所以这里做 20s
    缓存，并允许 force=True 绕过（供插拔网卡后主动刷新）。
    """
    now = time.monotonic()
    with _net_adapters_lock:
        cur = _net_adapters_cache.get("data")
        if not force and cur is not None and (now - _net_adapters_cache["t"]) < _NET_ADAPTERS_TTL:
            return [dict(a) for a in cur]
        # 上一次查询刚失败过就退避：否则「查不到 → 不写缓存 → 下一秒再查」会死循环，
        # 退化成每秒起一个 PowerShell（拿不到网卡信息的老机器就是这么被吃满 CPU 的）
        if not force and (now - float(_net_adapters_cache.get("fail_t") or 0.0)
                          < _NET_ADAPTERS_FAIL_TTL):
            return [dict(a) for a in cur] if cur else []

    rc, out = ps(_NET_META_SCRIPT, timeout=15)
    adapters = []
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
        adapters.append({"name": p[1], "media": p[2], "kind": kind,
                         "link_mbps": ls, "desc": p[4]})

    # 只有拿到结果才写「成功缓存」（避免一次失败把空列表固化 20 秒）；
    # 失败则记一个失败时间戳，配合上面的退避避免每秒重试
    if adapters:
        with _net_adapters_lock:
            _net_adapters_cache["t"] = time.monotonic()
            _net_adapters_cache["data"] = [dict(a) for a in adapters]
            _net_adapters_cache["fail_t"] = 0.0
        return adapters
    with _net_adapters_lock:
        _net_adapters_cache["fail_t"] = time.monotonic()
    if cur is not None:
        return [dict(a) for a in cur]
    return adapters


def _pick_primary(adapters):
    """选主网卡：优先非虚拟且协商速率最高的那张。"""
    best = None
    for a in adapters:
        d = (a["desc"] or "").lower()
        virt = any(k in d for k in ("virtual", "vmware", "hyper-v", "loopback",
                                    "bluetooth", "tap", "vpn", "wintun", "radmin"))
        if virt:
            continue
        if best is None or (a["link_mbps"] or 0) > (best["link_mbps"] or 0):
            best = a
    if best is None and adapters:
        best = adapters[0]
    return best


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

    age = round(max(0.0, time.monotonic() - seen), 1) if seen else None
    return {"adapters": adapters, "primary": primary,
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

        if p == "/api/soft/leftover":
            keyword = (body.get("keyword") or "").strip().lower()
            if len(keyword) < 3:
                return self._json({"ok": False, "error": "关键词至少 3 个字符"}, 400)
            found = []
            for base in [os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", ""),
                         os.environ.get("PROGRAMDATA", ""), os.environ.get("LOCALAPPDATA", ""),
                         os.environ.get("APPDATA", "")]:
                if not base or not os.path.isdir(base):
                    continue
                try:
                    for entry in os.listdir(base):
                        if keyword in entry.lower():
                            full = os.path.join(base, entry)
                            s, _ = dir_size(full, cap=20000) if os.path.isdir(full) else (0, 0)
                            found.append({"path": full, "size": s,
                                          "is_dir": os.path.isdir(full)})
                except OSError:
                    pass
            return self._json({"ok": True, "data": found})

        if p == "/api/startup/toggle":
            hive, sub, name = body["hive"], body["path"], body["name"]
            enable = bool(body.get("enable"))
            if hive == "DIR":
                return self._json({"ok": False, "error": "启动文件夹项请手动处理"}, 400)
            if enable:
                ex, typ, data = reg_read(hive + "\\" + sub, name)
                if ex:
                    reg_write(hive + "\\" + sub, name, "REG_SZ", data)
                return self._json({"ok": True, "data": {"enable": True}})
            ok = reg_delete(hive + "\\" + sub, name)
            return self._json({"ok": True, "data": {"enable": False, "ok": ok}})

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
