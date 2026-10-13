# -*- coding: utf-8 -*-
"""验证启动提权链（不真弹 UAC）：

1) portable.spec 是否 uac_admin=True（打包版 manifest requireAdministrator）
2) main() 是否有「非管理员 → relaunch_as_admin → exit」的兜底
3) relaunch_as_admin() 在「源码」与「打包版」两种情况下，
   构造的 exe / 参数是否正确（含带空格的路径加引号、剔除 --admin-mode/--normal-mode）
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import app as A

bad = []


def chk(c, label):
    print(("OK  " if c else "!!! ") + label)
    if not c:
        bad.append(label)


# ---- 1) spec ----
spec = io.open("portable.spec", encoding="utf-8").read()
chk("uac_admin" in spec, "portable.spec 提到 uac_admin")
import re
m = re.search(r"uac_admin\s*=\s*(True|False)", spec)
chk(bool(m) and m.group(1) == "True", "uac_admin=True（清单 requireAdministrator）")

# ---- 2) main() 兜底 ----
src = io.open("app.py", encoding="utf-8").read()
_main = src[src.find("def main():"):src.find("if __name__")]
chk("relaunch_as_admin()" in _main, "main() 调 relaunch_as_admin")
chk("S.is_admin()" in _main, "main() 用 S.is_admin() 判定实际权限")
chk('mode == "admin" and not S.is_admin()' in _main,
    "门控：仅「管理员模式 且 当前非管理员」才提权")
chk("_ELEVATED_FLAG not in sys.argv" in _main,
    "门控含哨兵：已提权过一次的进程不再重启自己（防无限弹 UAC）")
chk("sys.exit(0)" in _main, "提权发起后当前进程退出，交给新进程")

# ---- 3) relaunch_as_admin 参数构造（打桩 ShellExecuteW）----
captured = {}


class _FakeShell:
    def ShellExecuteW(self, hwnd, verb, exe, params, cwd, show):
        captured["verb"] = verb
        captured["exe"] = exe
        captured["params"] = params
        return 42          # >32 = 成功


class _FakeWindll:
    shell32 = _FakeShell()


import ctypes
_orig_windll = ctypes.windll
ctypes.windll = _FakeWindll()

try:
    # 3a) 源码模式：sys.frozen 不存在
    sys.argv = ["app.py", "--normal-mode", "dummy arg"]
    if hasattr(sys, "frozen"):
        del sys.frozen
    ok = A.relaunch_as_admin()
    chk(ok, "源码模式：relaunch 返回 True（rc>32 视为成功）")
    chk(captured.get("verb") == "runas", "verb = runas（触发 UAC）")
    chk(captured.get("exe") == sys.executable, "exe = sys.executable（python）")
    chk("app.py" in (captured.get("params") or ""), "参数含 app.py（脚本入口）")
    chk("--normal-mode" not in (captured.get("params") or ""),
        "剔除 --normal-mode（提权后不回退普通模式）")
    chk('"dummy arg"' in (captured.get("params") or ""),
        "带空格的参数被加引号：%r" % captured.get("params"))
    chk(captured.get("params", "").rstrip().endswith("--wtb-elevated"),
        "末尾补提权哨兵 --wtb-elevated")

    # 3b) 打包模式：sys.frozen=True
    captured.clear()
    sys.frozen = True
    sys.argv = ["WinToolbox.exe"]
    ok2 = A.relaunch_as_admin()
    chk(ok2, "打包模式：relaunch 返回 True")
    chk(captured.get("exe") == sys.executable, "打包模式 exe = sys.executable（exe 自身）")
    chk((captured.get("params") or "").strip() == "--wtb-elevated",
        "打包模式参数只有哨兵：%r" % captured.get("params"))
finally:
    ctypes.windll = _orig_windll
    if hasattr(sys, "frozen"):
        del sys.frozen

print()
print("结果:", "全过" if not bad else ("失败 " + str(bad)))
os._exit(1 if bad else 0)
