# -*- coding: utf-8 -*-
"""静态校验 ㊵ 组断言依赖的串 + 演练其运行时片段（防再出 NameError）。"""
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as S

sv = io.open("server.py", encoding="utf-8").read()
ra = io.open("tests/regress_all.py", encoding="utf-8").read()
au = io.open("tests/audit_features.py", encoding="utf-8").read()

_rbs = sv[sv.find("def recycle_bin_size"):]
_rbs = _rbs[:_rbs.find("\ndef ", 10)]
checks = [
    ("os.scandir(bin_dir)" in _rbs, "扫 $Recycle.Bin 目录"),
    ('"$Recycle.Bin"' in _rbs and 'startswith("$R")' in _rbs, "$R 前缀过滤"),
    ("New-Object -ComObject" not in _rbs, "旧 COM 枚举已删"),
    ("SHQueryRecycleBinW" in _rbs, "Shell API 兜底"),
    ("def wait_until(pred" in ra, "regress 有 wait_until"),
    ("wait_until(lambda: cl.table.rowCount() > 0" in ra, "垃圾扫描用条件轮询"),
    ("㊵" in au, "审计 ㊵ 组存在"),
    ("import time" in au, "审计已导入 time"),
]
bad = []
for ok, n in checks:
    print(("OK  " if ok else "!!! ") + n)
    if not ok:
        bad.append(n)

# 演练 ㊵ 的运行时片段（精确复制审计里的这几行）
_t0 = time.time()
_rv = S.recycle_bin_size()
_rsec = time.time() - _t0
print("运行时片段：_rv=%r（int=%s, >=0=%s）_rsec=%.2f（<10=%s）"
      % (_rv, isinstance(_rv, int), _rv >= 0, _rsec, _rsec < 10.0))
if not (isinstance(_rv, int) and _rv >= 0):
    bad.append("runtime int>=0")
if not _rsec < 10.0:
    bad.append("runtime <10s")

print("静态校验:", "全过" if not bad else ("失败 " + str(bad)))
sys.exit(1 if bad else 0)
