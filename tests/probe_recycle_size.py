# -*- coding: utf-8 -*-
"""对比「回收站占用」两种查法：Shell API（SHQueryRecycleBinW，毫秒级）
vs 现有 PowerShell+COM 实现（慢、随负载波动）。

目的：确认 SHQueryRecycleBinW(NULL).i64Size 的数值可信，从而把
recycle_bin_size() 从 PowerShell 换成 Shell API（消除垃圾扫描的唯一瓶颈）。
"""
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as S


class _RB(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong),
                ("i64Size", ctypes.c_longlong),
                ("i64NumItems", ctypes.c_longlong)]


def shell_total(root=None):
    """SHQueryRecycleBinW：root=None → 所有驱动器汇总。返回 (rc, size, n)"""
    info = _RB()
    info.cbSize = ctypes.sizeof(_RB)
    fn = ctypes.windll.shell32.SHQueryRecycleBinW
    fn.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(_RB)]
    fn.restype = ctypes.c_long
    rc = fn(root, ctypes.byref(info))
    return rc, int(info.i64Size), int(info.i64NumItems)


print("=== 1) Shell API 查全部驱动器 ===")
t0 = time.time()
rc, size, n = shell_total(None)
dt = time.time() - t0
print("rc=%d | size=%d (%.1f MB) | items=%d | 耗时 %.4fs"
      % (rc, size, size / 1048576.0, n, dt))

print()
print("=== 2) Shell API 逐盘查（对照）===")
total_s, total_n = 0, 0
for d in S.list_drives():
    t0 = time.time()
    rc2, s2, n2 = shell_total(d["root"])
    print("  %-4s rc=%d size=%s items=%d (%.4fs)"
          % (d["root"], rc2, s2, n2, time.time() - t0))
    if rc2 == 0:
        total_s += s2
        total_n += n2
print("  逐盘合计：size=%d (%.1f MB) items=%d" % (total_s, total_s / 1048576.0, total_n))

print()
print("=== 3) 现有 PowerShell+COM 实现（慢）===")
t0 = time.time()
ps_size = S.recycle_bin_size()
print("PowerShell size=%d (%.1f MB) | 耗时 %.2fs"
      % (ps_size, ps_size / 1048576.0, time.time() - t0))

print()
print("=== 结论 ===")
diff = abs(size - ps_size)
print("Shell(全部盘) = %d；PowerShell = %d；差 = %d (%.2f MB)"
      % (size, ps_size, diff, diff / 1048576.0))
print("一致（差 <1MB）:", diff < 1048576)
