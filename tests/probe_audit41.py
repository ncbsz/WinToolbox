# -*- coding: utf-8 -*-
"""静态演练审计 ㊶ 组的表达式（照抄审计里的写法，防 NameError / 防条件写反）。"""
import io
import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_app_src = io.open(os.path.join(_root, "app.py"), encoding="utf-8").read()

bad = []


def chk(c, label):
    print(("OK  " if c else "!!! ") + label)
    if not c:
        bad.append(label)


_mn = _app_src[_app_src.find("def main():"):_app_src.find("if __name__")]
_ral = _app_src[_app_src.find("def relaunch_as_admin"):]
_ral = _ral[:_ral.find("\ndef ", 10)]

chk('_ELEVATED_FLAG = "--wtb-elevated"' in _app_src, "有提权哨兵常量")
chk("--normal-mode" in _app_src and 'return "admin"' in _app_src, "choose_mode 默认 admin")
chk("relaunch_as_admin()" in _mn and '"runas"' in _ral, "main() 兜底调 relaunch_as_admin")
chk("_ELEVATED_FLAG not in sys.argv" in _mn, "门控含哨兵")
chk("_ELEVATED_FLAG" in _ral and "+ [_ELEVATED_FLAG]" in _ral, "子进程补哨兵参数")
_i_elev = _mn.find("relaunch_as_admin()")
_i_probe = _mn.find("probe_system_info")
chk(0 < _i_elev < _i_probe, "提权排在 probe_system_info 之前（%d < %d）" % (_i_elev, _i_probe))
_spec = os.path.join(_root, "portable.spec")
try:
    _spec_src = io.open(_spec, encoding="utf-8").read()
    chk("uac_admin=True" in _spec_src.replace(" ", ""), "portable.spec uac_admin=True")
except OSError:
    chk(False, "读不到 portable.spec")

print("结果:", "全过" if not bad else ("失败 " + str(bad)))
sys.exit(1 if bad else 0)
