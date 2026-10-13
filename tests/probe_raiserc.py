# -*- coding: utf-8 -*-
"""直接测 ShellExecuteW("runas") 在当前环境的返回码。
>32 = 报告成功；<=32 = 失败码（5=拒绝访问 / 用户取消）。"""
import ctypes
import os
import sys

exe = sys.executable
params = '"%s" --wtb-elevated' % os.path.abspath("tests/__elev_probe_target.py")
rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
print("ShellExecuteW runas 返回码 =", int(rc), "(>32 表示「已发起」)")
print("is_admin =", bool(ctypes.windll.shell32.IsUserAnAdmin()))
