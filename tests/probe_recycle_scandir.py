# -*- coding: utf-8 -*-
"""测第三种查法：直接遍历 `X:\\$Recycle.Bin\\<SID>\\` 下的 `$R*` 文件求和。

回收站本质就是个目录：`$R<随机>` = 真实数据，`$I<随机>` = 元数据（不占实际大小）。
若 scandir 求和能秒级完成，就可替代 Shell/PowerShell 的慢枚举。
"""
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as S


def scandir_recycle_total():
    total = 0
    n = 0
    for d in S.list_drives():
        bin_dir = os.path.join(d["root"], "$Recycle.Bin")
        if not os.path.isdir(bin_dir):
            continue
        try:
            sids = os.scandir(bin_dir)
        except OSError as e:
            print("   [skip] %s (%s)" % (bin_dir, e))
            continue
        with sids:
            for sid in sids:
                if not sid.is_dir(follow_symlinks=False):
                    continue
                try:
                    f = os.scandir(sid.path)
                except OSError:
                    continue
                with f:
                    for ent in f:
                        name = ent.name
                        if not name.startswith("$R"):
                            continue          # $I* 是元数据，不计
                        try:
                            if ent.is_dir(follow_symlinks=False):
                                s, m = S.dir_size(ent.path, min_age_hours=0)
                                total += s
                                n += m
                            else:
                                total += ent.stat(follow_symlinks=False).st_size
                                n += 1
                        except OSError:
                            pass
    return total, n


print("=== scandir 遍历 $Recycle.Bin ===")
t0 = time.time()
size, n = scandir_recycle_total()
print("size=%d (%.1f MB) | 文件数=%d | 耗时 %.2fs"
      % (size, size / 1048576.0, n, time.time() - t0))

print()
print("=== Shell API（对照，慢）===")
t0 = time.time()
info_size = None
rc, sz, cnt = S._recycle_bin_items(None), None, None
print("(跳过，前一轮已测 36s)")
