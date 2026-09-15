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


def sys_basic():
    """One PowerShell round-trip instead of three (keeps /api/sysinfo fast)."""
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
    return {"caption": f[0].strip(), "version": f[1].strip(), "arch": f[2].strip(),
            "cpu": f[3].strip() or "Unknown CPU", "cores": cores, "logical": logical}


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
                            if not dn:
                                continue
                            if q("SystemComponent", 0) in (1, "1"):
                                continue
                            if q("ParentKeyName"):
                                continue
                            out.append({
                                "key": hive + "\\" + sub + "\\" + name,
                                "name": dn,
                                "version": q("DisplayVersion"),
                                "publisher": q("Publisher"),
                                "size": q("EstimatedSize", 0),
                                "date": q("InstallDate"),
                                "uninstall": q("UninstallString"),
                                "quiet": q("QuietUninstallString"),
                                "location": q("InstallLocation"),
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
                        items.append({"hive": hive, "path": sub, "name": nm, "cmd": val})
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
                items.append({"hive": "DIR", "path": fd, "name": f, "cmd": os.path.join(fd, f)})
    return items


def list_services():
    """Single CIM query — much faster than one Get-CimInstance per service."""
    rc, out = ps("Get-CimInstance Win32_Service | ForEach-Object { "
                 "'{0}|||{1}|||{2}|||{3}' -f $_.Name,$_.DisplayName,$_.State,$_.StartMode }",
                 timeout=120)
    res = []
    for line in out.splitlines():
        p = line.strip().split("|||")
        if len(p) == 4 and p[0]:
            res.append({"name": p[0], "display": p[1], "status": p[2],
                        "start": {"Auto": "自动", "Manual": "手动", "Disabled": "禁用"}.get(p[3], p[3])})
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
def list_processes(limit=60):
    rc, out = run(["tasklist", "/fo", "csv", "/nh"], timeout=30)
    procs = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith('"'):
            continue
        m = re.findall(r'"([^"]*)"', line)
        if len(m) < 5:
            continue
        name, pid, _, _, mem = m[0], m[1], m[2], m[3], m[4]
        try:
            mem_kb = int(re.sub(r"[^\d]", "", mem) or 0)
        except Exception:
            mem_kb = 0
        procs.append({"name": name, "pid": int(pid), "mem": mem_kb * 1024})
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
    "domestic": [
        {"name": "阿里 DNS", "ip": "223.5.5.5"},
        {"name": "阿里 DNS 2", "ip": "223.6.6.6"},
        {"name": "腾讯 DNS", "ip": "119.29.29.29"},
        {"name": "DNSPod", "ip": "182.254.116.116"},
        {"name": "114 DNS", "ip": "114.114.114.114"},
        {"name": "百度 DNS", "ip": "180.76.76.76"},
    ],
    "foreign": [
        {"name": "Google DNS", "ip": "8.8.8.8"},
        {"name": "Google DNS 2", "ip": "8.8.4.4"},
        {"name": "Cloudflare", "ip": "1.1.1.1"},
        {"name": "Cloudflare 2", "ip": "1.0.0.1"},
        {"name": "Quad9", "ip": "9.9.9.9"},
        {"name": "OpenDNS", "ip": "208.67.222.222"},
    ],
}


def _dns_raw_query(ip, domain="www.baidu.com", timeout=2.5):
    """Send a minimal UDP DNS A-record query, return (ok, latency_ms, answer_ip)."""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
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


def dns_test(mode="mixed", domain="www.baidu.com"):
    """mode: domestic / foreign / mixed. Returns list of {name, ip, ok, ms, answer}."""
    if mode == "domestic":
        src = DNS_SERVERS["domestic"]
    elif mode == "foreign":
        src = DNS_SERVERS["foreign"]
    else:
        src = DNS_SERVERS["domestic"] + DNS_SERVERS["foreign"]
    out = []
    for s in src:
        ok, ms, answer = _dns_raw_query(s["ip"], domain)
        out.append({"name": s["name"], "ip": s["ip"], "ok": ok, "ms": ms, "answer": answer})
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
    win = os.environ.get("SystemRoot", r"C:\Windows")
    lad = os.environ.get("LOCALAPPDATA", "")
    return [
        {"id": "user_temp", "name": "用户临时文件 (%TEMP%)",
         "path": os.environ.get("TEMP", ""), "kind": "clear"},
        {"id": "win_temp", "name": "系统临时文件 (Windows\\Temp)",
         "path": os.path.join(win, "Temp"), "kind": "clear"},
        {"id": "prefetch", "name": "预读取文件 (Prefetch)",
         "path": os.path.join(win, "Prefetch"), "kind": "clear"},
        {"id": "wu_cache", "name": "Windows 更新缓存",
         "path": os.path.join(win, "SoftwareDistribution", "Download"), "kind": "clear"},
        {"id": "wu_logs", "name": "Windows 更新日志",
         "path": os.path.join(win, "Logs", "WindowsUpdate"), "kind": "clear"},
        {"id": "crash", "name": "崩溃转储 / WER 报告",
         "path": os.path.join(lad, "CrashDumps"), "kind": "clear"},
        {"id": "thumb", "name": "缩略图缓存 (Explorer)",
         "path": os.path.join(lad, "Microsoft", "Windows", "Explorer"), "kind": "files"},
        {"id": "inetcache", "name": "IE / 系统网络缓存",
         "path": os.path.join(lad, "Microsoft", "Windows", "INetCache"), "kind": "clear"},
        {"id": "chrome", "name": "Chrome 缓存",
         "path": os.path.join(lad, "Google", "Chrome", "User Data", "Default", "Cache"), "kind": "clear"},
        {"id": "edge", "name": "Edge 缓存",
         "path": os.path.join(lad, "Microsoft", "Edge", "User Data", "Default", "Cache"), "kind": "clear"},
    ]


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


def scan_junk():
    out = []
    for t in junk_targets():
        size, n = dir_size(t["path"])
        out.append({"id": t["id"], "name": t["name"], "path": t["path"],
                    "size": size, "files": n, "kind": t["kind"]})
    rbin = 0
    try:
        rc, o = ps("(New-Object -ComObject Shell.Application).NameSpace(0xA).Items() | "
                   "Measure-Object -Property Size -Sum | Select-Object -ExpandProperty Sum")
        rbin = int(float(o.strip() or 0))
    except Exception:
        pass
    out.append({"id": "recyclebin", "name": "回收站", "path": "SHELL:RecycleBin",
                "size": rbin, "files": 0, "kind": "recycle"})
    return out


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
    stamp = time.strftime("%Y%m%d_%H%M%S")
    results = []
    for p in paths:
        safe = p.replace("\\", "_")
        outfile = os.path.join(BACKUP, "policy_%s_%s.reg" % (safe, stamp))
        run(["reg", "export", p, outfile, "/y"])
        rc, out = run(["reg", "delete", p, "/f"])
        results.append({"path": p, "ok": rc == 0, "backup": outfile if rc == 0 else None})
    # the pause-to-2999 values live outside Policies, delete them individually
    for path, names, why in POLICY_EXTRA_VALUES:
        for nm in names:
            run(["reg", "delete", path, "/v", nm, "/f"])
    return results


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
    " -ErrorAction SilentlyContinue|Sort-Object CurrentTemperature -Descending"
    "|Select-Object -First 1;"
    "if($z){$t=[math]::Round($z.CurrentTemperature/10.0-273.15,1)}}catch{};"
    "if($null -eq $t){try{$z2=Get-CimInstance Win32_PerfFormattedData_Counters_ThermalZoneInformation"
    " -ErrorAction SilentlyContinue|Sort-Object Temperature -Descending|Select-Object -First 1;"
    "if($z2){$t=[math]::Round([double]$z2.Temperature-273.15,1)}}catch{}};"
    "if($null -eq $t){$t='NA'};"
    "'CPUTEMP='+$t;"
)

_CPU_TEMP_CACHE = {"t": 0.0, "v": None, "ok": True}
_CPU_TEMP_TTL = 8.0


def cpu_temp():
    """CPU 温度。WMI/ACPI 读取要起 PowerShell，缓存 8 秒；读不到就不再重试太久。"""
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
    _CPU_TEMP_CACHE.update(t=now, v=v)
    return v


_GPU_AGG_CACHE = {"t": 0.0, "v": None}
_GPU_AGG_TTL = 2.0


def _gpu_aggregate_cached():
    """GPU 聚合占用。WMI 枚举较慢，缓存 2 秒。"""
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
    """1 秒级：CPU 温度（8s 缓存）+ GPU 聚合（2s 缓存）+ 磁盘实时活动（1s）。"""
    disks, agg = disk_activity()
    if disks is None:
        disks = _wmi_perf_fallback()
        agg = (sum((d["util"] or 0) for d in disks) / len(disks)) if disks else None
    return {"cpu_temp": cpu_temp(), "disks": disks, "disk": agg,
            "gpu": _gpu_aggregate_cached(), "gpu_temp": None, "gpus": [],
            "realtime": True}


def _slow_fetch():
    """8 秒级：nvidia-smi 明细（含温度/显存），失败时用 GPU 聚合占位。"""
    gpus = _nvidia_smi()
    if not gpus:
        agg = _gpu_aggregate_cached()
        if agg is not None:
            gpus = [{"index": 0, "name": "GPU0 (聚合)",
                     "util": int(round(agg)), "temp": None}]
    gpu_temp = next((g["temp"] for g in gpus if g.get("temp") is not None), None)
    return {"gpus": gpus, "gpu_temp": gpu_temp}


def perf_stats():
    """CPU temp, GPU list, disk list. Values are None when unsupported.

    两层缓存，合并后结构与老版本完全一致（UI 无需改字段）：
      - fast（1s）：CPU 温度 / GPU 聚合 / 磁盘实时活动 —— 负责「实时」；
      - slow（8s）：nvidia-smi 明细 —— 负责「完整」。
    """
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
    # GPU 聚合优先用 nvidia-smi 明细的平均值（更准），没有才用 fast 里的
    if data.get("gpus"):
        data["gpu"] = max(0.0, min(100.0,
                                   sum((g["util"] or 0) for g in data["gpus"]) /
                                   float(len(data["gpus"]))))
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
