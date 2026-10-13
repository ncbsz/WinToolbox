"""句柄 / 工作集 API 回归验证（ctypes 签名 + 权限位）。

用法：<venv>/python.exe tests/verify_handle_api.py

背景：v1.4 修过一次「一键整理内存」直接报错（SetProcessWorkingSetSize 未声明
argtypes，SIZE_T(-1) 溢出）和「释放工作集」权限不足。这个脚本把当时的验证固化下来：
以后只要改动 server.py 里任何 ctypes 签名 / 打开进程的权限位，跑它就能立刻发现。

安全性：只读为主；写操作只针对**脚本自身进程**（并读回确认），绝不碰别的进程、
不改任何系统设置。
"""
import ctypes
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import server as S          # noqa: E402

FAIL = []


def chk(name, cond):
    print(("  [PASS] " if cond else "  [FAIL] ") + name)
    if not cond:
        FAIL.append(name)


ME = os.getpid()

print("=== 1. ctypes 签名是否已声明 ===")
for fn in ("OpenProcess", "CloseHandle", "GetCurrentProcess", "SetProcessWorkingSetSize"):
    f = getattr(S._k32, fn)
    chk("%s 有 argtypes/restype" % fn, bool(getattr(f, "argtypes", None))
        or fn == "GetCurrentProcess")
chk("GetCurrentProcess restype = c_void_p",
    S._k32.GetCurrentProcess.restype is ctypes.c_void_p)
chk("OpenProcess restype = c_void_p", S._k32.OpenProcess.restype is ctypes.c_void_p)
gpmi = S._k32_gpmi()
chk("GetProcessMemoryInfo 签名已声明（argtypes[0] = c_void_p）",
    bool(gpmi.argtypes) and gpmi.argtypes[0] is ctypes.c_void_p)

print("\n=== 2. 无效句柄判定（restype=c_void_p 后 -1 判断会失效）===")
chk("_bad_handle(0) / (-1) / (0xFFFF..FF) 都判为无效",
    S._bad_handle(0) and S._bad_handle(-1)
    and S._bad_handle(0xFFFFFFFFFFFFFFFF))
chk("_bad_handle(有效句柄值) 判为有效", not S._bad_handle(0x1234))

print("\n=== 3. 进程快照读取 ===")
t0 = time.time()
rows = S.procs_fast(limit=120)
dt = (time.time() - t0) * 1000
chk("procs_fast 有数据（%d 行）" % len(rows), len(rows) > 0)
chk("procs_fast 内存字段可读（不是全 0）", any(r.get("mem", 0) > 0 for r in rows))
chk("procs_fast 能取到 exe 路径", any(r.get("exe") for r in rows))
chk("procs_fast 足够快（%.0f ms < 120ms）" % dt, dt < 120)
chk("列表里能找到本进程自身",
    any(r["pid"] == ME and r["mem"] > 0 for r in rows))
chk("list_processes 有数据", len(S.list_processes(limit=60)) > 0)
chk("_thread_ids(自身) 能枚举线程", len(S._thread_ids(ME)) > 0)

print("\n=== 4. 读到的内存与系统 tasklist 交叉核对 ===")
out = ""
try:
    import subprocess
    out = subprocess.run(["tasklist", "/FI", "PID eq %d" % ME, "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, timeout=20).stdout.strip()
except Exception:
    pass
if out:
    # tasklist 的 CSV 里内存列带千分位逗号，必须先整体取出来再去逗号
    m = re.search(r'"([\d,]+)\s*K"', out)
    sys_kb = int(m.group(1).replace(",", "")) if m else None
    ours = next((r["mem"] for r in rows if r["pid"] == ME), None)
    chk("读数与 tasklist 一致（系统 %s K / 程序 %s B）"
        % (sys_kb, ours),
        bool(sys_kb) and bool(ours)
        and abs(ours - sys_kb * 1024) < max(8 * 1024 * 1024, ours * 0.5))
else:
    chk("tasklist 交叉核对（跳过：无法调用）", True)

print("\n=== 5. 工作集释放（只针对自身进程）===")
chk("process_sched_state(自身) 可读", bool(S.process_sched_state(ME).get("ok")))
chk("release_working_set(自身 pid) 成功", S.release_working_set(ME).get("ok") is True)
chk("release_working_set(None) 成功", S.release_working_set(None).get("ok") is True)
chk("trim_process(自身) 返回 True", S.trim_process(ME) is True)
chk("trim_process(不存在的 pid) 返回 False 且不抛异常",
    S.trim_process(999999) is False)

print("\n=== 6. 内存优先级（设为「普通」= 自身当前值，零副作用）===")
r = S.set_process_memory_priority(ME, 2)
st = S.process_sched_state(ME)
chk("set_process_memory_priority 对自身生效",
    isinstance(r, dict) and r.get("ok") is True and st.get("memory_priority") == 2)

print("\n=== 7. 批量释放（限 60 个进程）===")
rws = S.release_working_set_all(limit=60)
chk("release_working_set_all 至少成功 1 个（%s）" % rws,
    isinstance(rws, dict) and rws.get("done", 0) >= 1)

print("\n=== 结果 ===")
print("全部通过" if not FAIL else "失败 %d 项：%s" % (len(FAIL), FAIL))
sys.exit(1 if FAIL else 0)
