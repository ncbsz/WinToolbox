#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WinToolbox backend  —  pure stdlib, no pip installs required.

Serves the Fluent (blue/white) web UI from ./web and exposes a JSON API that
performs REAL Windows operations: registry tweaks, service control, process /
memory management, network repair, uninstall listing, junk cleanup, policy
diagnosis and an infected-PE (Synaptics/XRed) scanner.

Run:  python server.py            (browser opens automatically)
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import shutil
import socket
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
LOADER_CODE_MD5 = "33fbe30e8a64654287edd1bf05ae7c8c"   # Synaptics / XRed loader stub
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


def run(cmd, timeout=25, shell=False):
    """Run a command, return (rc, stdout+stderr text)."""
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


def uptime_seconds():
    return int(_k32.GetTickCount64() / 1000)


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


def list_software():
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
    return uniq


STARTUP_LOCATIONS = [
    ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
    ("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"),
    ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
    ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce"),
    ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce"),
]


def residual_scan(name, publisher="", location="", cap=40):
    """卸载后的残留扫描：只定位、不删除。返回 {"files": [...], "reg": [...]}。

    files: 在常见安装/数据目录里找名称相关的目录与文件（含大小）；
    reg:   HKLM/HKCU 的 SOFTWARE（含 WOW6432Node）第一层子键中名称相关的项。
    """
    tokens = set()
    for src in (name or "", publisher or ""):
        s = re.sub(r"[^\w\u4e00-\u9fff]+", " ", src or "").strip().lower()
        if s:
            tokens.add(s)
            for w in s.split():
                if len(w) >= 3 and w not in ("inc", "ltd", "llc", "software", "the"):
                    tokens.add(w)
    loc = (location or "").strip()
    if loc and os.path.isdir(loc):
        tokens.add(os.path.basename(loc.rstrip("\\/")).lower())

    files, seen = [], set()
    roots = [os.environ.get(k, "") for k in
             ("ProgramFiles", "ProgramFiles(x86)", "ProgramData",
              "LOCALAPPDATA", "APPDATA")]
    for base in roots:
        if not base or not os.path.isdir(base):
            continue
        try:
            entries = os.listdir(base)
        except OSError:
            continue
        for entry in entries:
            el = entry.lower()
            if not any(t == el or t in el for t in tokens):
                continue
            full = os.path.join(base, entry)
            if full.lower() in seen:
                continue
            seen.add(full.lower())
            try:
                sz = dir_size(full, cap=5000)[0] if os.path.isdir(full) \
                    else os.path.getsize(full)
            except OSError:
                sz = 0
            files.append({"path": full, "kind": "dir" if os.path.isdir(full) else "file",
                          "bytes": sz})
            if len(files) >= cap:
                break
        if len(files) >= cap:
            break

    regs = []
    if winreg is not None and tokens:
        targets = [(winreg.HKEY_LOCAL_MACHINE, "HKLM", r"SOFTWARE"),
                   (winreg.HKEY_LOCAL_MACHINE, "HKLM", r"SOFTWARE\WOW6432Node"),
                   (winreg.HKEY_CURRENT_USER, "HKCU", r"SOFTWARE")]
        for root, hive, sub in targets:
            try:
                k = winreg.OpenKey(root, sub, 0, winreg.KEY_READ)
            except OSError:
                continue
            try:
                n = winreg.QueryInfoKey(k)[0]
                for i in range(n):
                    try:
                        sk_name = winreg.EnumKey(k, i)
                    except OSError:
                        continue
                    skl = sk_name.lower()
                    if not any(t == skl or t in skl for t in tokens):
                        continue
                    regs.append({"hive": hive, "path": sub + "\\" + sk_name})
                    if len(regs) >= cap:
                        break
            finally:
                winreg.CloseKey(k)
            if len(regs) >= cap:
                break
    return {"files": files, "reg": regs, "tokens": sorted(tokens)}


def residual_clean(files, regs):
    """删除所选残留。文件直接删除（仅限白名单根目录下），注册表整键递归删除。

    返回 {"ok": bool, "msg": [...]}，逐条报告失败原因。
    """
    msgs = []
    ok = True
    allowed_roots = [os.path.abspath(os.environ.get(k, "")).lower() for k in
                     ("ProgramFiles", "ProgramFiles(x86)", "ProgramData",
                      "LOCALAPPDATA", "APPDATA")]
    allowed_roots = [r for r in allowed_roots if r]
    for it in files or []:
        p = (it.get("path") or "").strip()
        pl = os.path.abspath(p).lower()
        if not any(pl.startswith(r + os.sep) for r in allowed_roots):
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
        hive = it.get("hive")
        path = (it.get("path") or "").strip()
        # 安全闸：只允许删 SOFTWARE 与 SOFTWARE\WOW6432Node 的第一层子键
        if hive not in ("HKLM", "HKCU") or \
                not re.match(r"^SOFTWARE(\\WOW6432Node)?\\[^\\]+$", path, re.I):
            msgs.append("拒绝（超出安全范围）：%s\\%s" % (hive, path))
            ok = False
            continue
        root = winreg.HKEY_LOCAL_MACHINE if hive == "HKLM" else winreg.HKEY_CURRENT_USER
        try:
            _delete_tree(root, path)
            msgs.append("已删除注册表：HK%s\\%s" % ("LM" if hive == "HKLM" else "CU", path))
        except Exception as e:
            ok = False
            msgs.append("失败：%s\\%s（%s）" % (hive, path, str(e)[:80]))
    return {"ok": ok, "msg": msgs}


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


def _dns_probe(ip, domain, timeout=1.5, measured=3):
    """单个 DNS 的真实延迟探测（参照 DnsTools / trust-dns-resolver 的做法）。

    关键：热门域名（如 www.baidu.com）会被运营商缓存/透明拦截秒回，
    测出来的 2ms 是假延迟。这里改为：
      1. 正常域名查询一次 —— 验证服务器可用、拿到解析结果（同时当预热）；
      2. 再用「随机子域名」查询 N 次 —— 任何真实解析器都必须向上游递归，
         拿到的才是该服务器与根/权威链路的真实往返耗时；取最小值。
    全程复用同一个已连接 socket（trust-dns 同款做法），排除建连开销。
    """
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    try:
        sock = socket.socket(fam, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.connect((ip, 53))
    except Exception:
        return {"ok": False, "ms": None, "answer": None}

    def q(name):
        try:
            qname = b"".join(bytes([len(label)]) + label.encode("utf-8")
                             for label in name.split(".")) + b"\x00"
            req = struct.pack(">HHHHHH", random.randint(0, 0xFFFF), 0x0100,
                              1, 0, 0, 0) + qname + struct.pack(">HH", 1, 1)
            sock.send(req)
            data, _ = sock.recvfrom(512)
            ms = None
            return data
        except Exception:
            return None

    import time as _t
    answer = None
    # 1) 正常查询：验证可用 + 解析结果 + 预热
    data = q(domain)
    if data is not None:
        try:
            flags = struct.unpack_from(">H", data, 2)[0]
            ancount = struct.unpack_from(">H", data, 6)[0]
            if (flags & 0x0F) == 0 and ancount > 0:
                answer = _dns_parse_answer(data, domain)
        except Exception:
            pass
    # 2) 随机子域名强制递归，测真实往返
    best = None
    for _ in range(measured):
        rnd = "%012x.%s" % (random.getrandbits(48), domain)
        t0 = _t.perf_counter()
        data = q(rnd)
        if data is not None:
            ms = (_t.perf_counter() - t0) * 1000.0
            if best is None or ms < best:
                best = ms
    try:
        sock.close()
    except Exception:
        pass
    if best is not None:
        best = int(round(best))
    return {"ok": data is not None, "ms": best, "answer": answer}


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


def dns_test(mode="mixed", domain="www.baidu.com"):
    """mode: domestic / foreign / mixed / ipv6. Returns list of {name, ip, cat, ok, ms, answer}."""
    if mode == "domestic":
        src = [(s, "国内") for s in DNS_SERVERS["domestic"]]
    elif mode == "foreign":
        src = [(s, "国外") for s in DNS_SERVERS["foreign"]]
    elif mode == "ipv6":
        src = [(s, "IPv6") for s in DNS_V6_SERVERS]
    else:
        src = [(s, "国内") for s in DNS_SERVERS["domestic"]] + \
              [(s, "国外") for s in DNS_SERVERS["foreign"]]
    out = [None] * len(src)

    def work(i_s_cat):
        i, s, cat = i_s_cat
        r = _dns_probe(s["ip"], domain)
        r.update({"name": s["name"], "ip": s["ip"], "cat": cat})
        return i, r

    # 并行探测：122 个 DNS 串行要跑一分多钟，16 线程十几秒出全量结果
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=16) as ex:
            for i, r in ex.map(work, list(enumerate(src))):
                out[i] = r
    except Exception:
        out = [work((i, s, cat))[1] for i, (s, cat) in enumerate(src)]
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
# infected-PE scanner (Synaptics / XRed)
# --------------------------------------------------------------------------
import struct


def pe_sections(data):
    if len(data) < 0x100 or data[:2] != b"MZ":
        return None
    try:
        e = struct.unpack_from("<I", data, 0x3C)[0]
        if data[e:e + 4] != b"PE\0\0":
            return None
        nsec = struct.unpack_from("<H", data, e + 6)[0]
        optsize = struct.unpack_from("<H", data, e + 20)[0]
        sec_off = e + 24 + optsize
        secs = []
        for i in range(nsec):
            o = sec_off + i * 40
            nm = data[o:o + 8].rstrip(b"\0").decode("latin1", "replace")
            vs, va, rs, rp = struct.unpack_from("<IIII", data, o + 8)
            secs.append((nm, rs, rp))
        return secs
    except Exception:
        return None


INFECT_STRINGS = [b"xred.mooo.com", b"Synaptics2X", b"site50", b"SSLLibrary.dll",
                  b"KBHks.dll", b"USBHOOK", b"AUTORUNINJ"]


def scan_path_for_infection(path, max_files=4000, deep=False):
    hits = []
    scanned = 0
    for dp, dn, fn in os.walk(path):
        for f in fn:
            if scanned >= max_files:
                break
            full = os.path.join(dp, f)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if size < 4096 or size > 400 * 1024 * 1024:
                continue
            if not full.lower().endswith((".exe", ".dll", ".sys", ".ocx", ".scr", ".cpl")):
                continue
            try:
                with open(full, "rb") as fh:
                    head = fh.read(0x1000)
                    secs = pe_sections(head)
                    if not secs:
                        continue
                    scanned += 1
                    verdict = None
                    code = [s for s in secs if s[0] == "CODE"]
                    if code and code[0][1] == 629760:
                        nm, rs, rp = code[0]
                        fh.seek(rp)
                        blob = fh.read(rs)
                        if hex_md5(blob) == LOADER_CODE_MD5:
                            verdict = "infected-loader"
                        else:
                            fh.seek(0)
                            chunk = fh.read(2 * 1024 * 1024)
                            if any(s in chunk for s in INFECT_STRINGS):
                                verdict = "suspicious"
                    if verdict is None and deep:
                        fh.seek(0)
                        chunk = fh.read(3 * 1024 * 1024)
                        if any(s in chunk for s in INFECT_STRINGS):
                            verdict = "suspicious"
                    if verdict:
                        hits.append({"path": full, "size": size, "verdict": verdict})
            except Exception:
                continue
        if scanned >= max_files:
            break
    return {"base": path, "scanned": scanned, "hits": hits}


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
_NV_TTL = 1.8


def _nvidia_smi_cached():
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
    "spans": {},       # 实例名 -> {"rx": Mbps, "tx": Mbps, "bw": 协商带宽bps}
    "err": None,
    "stop": False,
    "thread": None,
    "warming": False,  # 首轮建立 PDH 查询中（此时尚无样本，属正常）
}


def _net_loop():
    """常驻线程：每秒 collect 一次 PDH 网络计数器。

    PDH 的 Bytes Received/sec 已经是「自上次 collect 以来的平均速率」，
    所以只要保持稳定的 collect 节奏就得到连续真实的曲线。

    首次迭代要建立 PDH 查询（含 PowerShell 实例枚举，约 5~8s），
    这段耗时发生在后台线程里，不阻塞任何 HTTP 请求。
    """
    prev = 0.0
    first = True
    while not _net_state["stop"]:
        try:
            if first:
                # 如实告知调用方「正在准备」，避免它误判为「无数据」
                with _net_lock:
                    _net_state["warming"] = True
            conn = _perf_conn_get()
            nq = conn.get("net_q")
            if nq is not None:
                vals = nq.collect_and_read()
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
            else:
                _net_loop_fallback()
        except Exception as e:
            with _net_lock:
                _net_state["err"] = "%s: %s" % (type(e).__name__, e)
        finally:
            if first:
                with _net_lock:
                    _net_state["warming"] = False
                first = False
        time.sleep(max(0.05, 1.0 - (time.monotonic() - prev)))
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
_NET_ADAPTERS_TTL = 20.0
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

    # 只有拿到结果才写缓存，避免一次失败把空列表固化 20 秒
    if adapters:
        with _net_adapters_lock:
            _net_adapters_cache["t"] = time.monotonic()
            _net_adapters_cache["data"] = [dict(a) for a in adapters]
    elif cur is not None:
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


# --------------------------------------------------------------------------
# 内网测速（iperf3）
# --------------------------------------------------------------------------
def iperf3_path():
    """找 iperf3.exe：优先随包目录 tools/，其次 PATH，最后常见安装路径。"""
    import shutil as _sh
    cands = [
        os.path.join(ROOT, "tools", "iperf3.exe"),
        os.path.join(APP_DIR, "iperf3.exe"),
        os.path.join(APP_DIR, "tools", "iperf3.exe"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    w = _sh.which("iperf3") or _sh.which("iperf3.exe")
    if w:
        return w
    for c in (r"C:\Windows\System32\iperf3.exe",
              r"C:\Program Files\iperf3\iperf3.exe"):
        if os.path.isfile(c):
            return c
    return None


_IPERF_SRV = {"proc": None, "port": 5201}


def firewall_allow_iperf(port=5201):
    """为 iperf3 端口添加入站放行规则（需管理员；已存在则跳过）。"""
    if not is_admin():
        return {"ok": False, "err": "需要管理员权限"}
    name = "WinToolbox iperf3 %d" % int(port)
    rc, out = run(["netsh", "advfirewall", "firewall", "show", "rule",
                   "name=%s" % name], timeout=15)
    if rc == 0:
        return {"ok": True, "existed": True}
    rc2, out2 = run(["netsh", "advfirewall", "firewall", "add", "rule",
                     "name=%s" % name, "dir=in", "action=allow",
                     "protocol=TCP", "localport=%d" % int(port)], timeout=20)
    return {"ok": rc2 == 0, "err": (out2 or "").strip()[:200]}


def firewall_remove_iperf(port=5201):
    name = "WinToolbox iperf3 %d" % int(port)
    run(["netsh", "advfirewall", "firewall", "delete", "rule",
         "name=%s" % name], timeout=15)
    return {"ok": True}


def iperf_server_start(port=5201):
    """启动 iperf3 服务端（让局域网其它设备对本机测速）。"""
    exe = iperf3_path()
    if not exe:
        return {"ok": False, "err": "未找到 iperf3.exe"}
    iperf_server_stop()
    # 尽量放行防火墙（非管理员会静默失败，不影响启动）
    fw = firewall_allow_iperf(port)
    try:
        p = subprocess.Popen(
            [exe, "-s", "-p", str(int(port))],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=0x08000000, cwd=os.path.dirname(exe) or None)
        _IPERF_SRV["proc"] = p
        _IPERF_SRV["port"] = int(port)
        time.sleep(0.6)
        if p.poll() is not None:
            out = (p.stdout.read() or b"").decode("utf-8", "replace")
            return {"ok": False, "err": out.strip() or "iperf3 启动后立即退出"}
        return {"ok": True, "port": int(port), "pid": p.pid, "firewall": fw,
                "hint": "局域网设备可用 iperf3 -c <本机IP> -p %d 测试" % int(port)}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def iperf_server_stop():
    p = _IPERF_SRV.get("proc")
    if p is not None:
        try:
            p.terminate()
        except Exception:
            pass
        _IPERF_SRV["proc"] = None
    return {"ok": True}


def iperf_server_status():
    p = _IPERF_SRV.get("proc")
    running = bool(p is not None and p.poll() is None)
    return {"running": running, "port": _IPERF_SRV.get("port", 5201)}


def _parse_iperf_json(txt):
    """从 iperf3 -J 输出里取：下载(接收)/上传(发送) Mbps。"""
    rx = tx = None
    try:
        import json as _j
        i = txt.find("{")
        if i >= 0:
            d = _j.loads(txt[i:])
            end = d.get("end") or {}
            # 本机作客户端：sum_sent=上传, sum_received=下载
            s = (end.get("sum_sent") or {}).get("bits_per_second")
            r = (end.get("sum_received") or {}).get("bits_per_second")
            if s is not None:
                tx = round(float(s) / 1e6, 1)
            if r is not None:
                rx = round(float(r) / 1e6, 1)
            return rx, tx
    except Exception:
        pass
    # 退化：正则抓 Bitrate
    import re as _re
    vals = [float(x) for x in _re.findall(r"([\d.]+)\s+(?:M|G)bits/sec", txt)]
    if vals:
        rx = rx or max(vals)
    return rx, tx


def iperf_client_run(host, port=5201, seconds=8, reverse=False,
                     parallel=1, udp=False, on_line=None):
    """对目标 iperf3 服务端做一次测速（本机为客户端）。

    reverse=True 测「下载」（服务端发、本机收），False 测「上传」。
    返回 {ok, rx_mbps, tx_mbps, raw}。
    """
    exe = iperf3_path()
    if not exe:
        return {"ok": False, "err": "未找到 iperf3.exe"}
    cmd = [exe, "-c", str(host), "-p", str(int(port)),
           "-t", str(int(seconds)), "-J", "-P", str(int(parallel or 1))]
    if reverse:
        cmd.append("-R")
    if udp:
        cmd.append("-u")
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             creationflags=0x08000000, cwd=os.path.dirname(exe) or None)
        out = (p.stdout.read() or b"").decode("utf-8", "replace")
        p.wait(timeout=seconds + 20)
    except Exception as e:
        return {"ok": False, "err": str(e)}
    if "error" in out.lower() and "bits_per_second" not in out:
        line = next((l for l in out.splitlines() if "error" in l.lower()), out[:200])
        return {"ok": False, "err": line.strip(), "raw": out}
    rx, tx = _parse_iperf_json(out)
    if rx is None and tx is None:
        return {"ok": False, "err": "未解析到测速结果", "raw": out}
    # 反向(-R)时本机是接收方 → 结果应记为下载
    if reverse:
        rx, tx = (rx if rx is not None else tx), None
    else:
        tx = tx if tx is not None else rx
        rx = None
    return {"ok": True, "rx_mbps": rx, "tx_mbps": tx, "raw": out}


def lan_ip():
    """本机在局域网中的 IPv4（不做实际连接，仅用于选路由）。"""
    import socket as _s
    try:
        s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return _s.gethostbyname(_s.gethostname())
        except Exception:
            return "127.0.0.1"


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
    " -ErrorAction SilentlyContinue;"
    "if($z){$a=($z|Measure-Object -Property CurrentTemperature -Average).Average;"
    "$t=[math]::Round($a/10.0-273.15,1)}}catch{};"
    "if($null -eq $t){try{$z2=Get-CimInstance Win32_PerfFormattedData_Counters_ThermalZoneInformation"
    " -ErrorAction SilentlyContinue;"
    "if($z2){$b=($z2|Measure-Object -Property Temperature -Average).Average;"
    "$t=[math]::Round([double]$b-273.15,1)}}catch{}};"
    "if($null -eq $t){$t='NA'};"
    "'CPUTEMP='+$t;"
)

_CPU_TEMP_CACHE = {"t": 0.0, "v": None, "ok": True}
# TTL 略大于采样间隔：后台线程每秒预热，UI 每次读都能命中，刷新耗时接近 0
_CPU_TEMP_TTL = 1.8
_CPU_TEMP_HIST = []          # 最近几次读数，用于平滑 ACPI 热区的 ±3℃ 抖动


def cpu_temp():
    """CPU 温度。WMI/ACPI 读取要起 PowerShell（约 0.5s），缓存 1.8 秒。

    ACPI 热区读数本身在几十度范围内有 ±3℃ 的抖动（不同传感器交替成为最热区），
    所以对最近 3 次取平均再返回，读数更稳；读不到返回 None。
    """
    now = time.time()
    if now - _CPU_TEMP_CACHE["t"] < _CPU_TEMP_TTL:
        return _CPU_TEMP_CACHE["v"]
    v = None
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
    if v is not None:
        _CPU_TEMP_HIST.append(v)
        del _CPU_TEMP_HIST[:-3]
        v = round(sum(_CPU_TEMP_HIST) / len(_CPU_TEMP_HIST), 1)
    _CPU_TEMP_CACHE.update(t=now, v=v)
    return v


_GPU_AGG_CACHE = {"t": 0.0, "v": None}
_GPU_AGG_TTL = 10.0          # 只是兜底值（有 PDH 时会被逐卡占用覆盖），拉长到 10s 省开销


def _gpu_aggregate_cached():
    """GPU 聚合占用（兜底）。WMI 枚举约 1 秒，缓存 10 秒。"""
    now = time.time()
    if now - _GPU_AGG_CACHE["t"] < _GPU_AGG_TTL:
        return _GPU_AGG_CACHE["v"]
    v = _gpu_aggregate()
    _GPU_AGG_CACHE.update(t=now, v=v)
    return v


def _wmi_perf_fallback():
    """PDH 磁盘不可用时的回退：走 WMI PerfFormattedData。"""
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
        try:
            cpu_temp()
        except Exception:
            pass
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
        # 最近没人读数据（窗口最小化 / 停留在别的页面）就降频，避免白耗 CPU
        idle = (time.time() - _sampler["last_read"]) > 20
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

        if p == "/api/speedtest/status":
            return self._json({"ok": True, "data": {
                "iperf3": bool(iperf3_path()), "server": iperf_server_status(),
                "lan_ip": lan_ip()}})

        if p == "/api/speedtest/server":
            act = (q.get("action", ["start"])[0] or "start").lower()
            port = int(q.get("port", ["5201"])[0] or 5201)
            if act == "stop":
                return self._json({"ok": True, "data": iperf_server_stop()})
            return self._json({"ok": True, "data": iperf_server_start(port)})

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

        if p == "/api/security/scan":
            base = q.get("path", [os.path.expanduser("~\\Desktop")])[0]
            deep = q.get("deep", ["0"])[0] in ("1", "true")
            if not os.path.isdir(base):
                return self._json({"ok": False, "error": "目录不存在: %s" % base}, 400)
            return self._json({"ok": True, "data": scan_path_for_infection(base, deep=deep)})

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
