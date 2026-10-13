# -*- coding: utf-8 -*-
"""一键跑完整回测套件（顺序执行，各写一份日志，最后打印汇总）。

用法：
    python tests/run_all.py            # 跑全部
    python tests/run_all.py regress    # 只跑名字里含 regress 的

⚠️ 这套东西**只在用户明确要求时跑**（项目约定：改完代码默认只做编译 + 必要冒烟）。
全套约 12 分钟，其中 `regress_all` 占 8 分钟。
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(os.environ.get("TEMP") or ".", "wintoolbox_regress")
SUITE = [("全功能回归", "tests/regress_all.py"),
         ("逐功能审计", "tests/audit_features.py"),
         ("多配置布局", "tests/multiconfig_sim.py"),
         ("句柄/ctypes", "tests/verify_handle_api.py"),
         ("启动项/服务/主题/UWP", "tests/verify_startup_power.py")]


def main():
    keys = sys.argv[1:]
    os.makedirs(OUT, exist_ok=True)
    summary = []
    for name, rel in SUITE:
        if keys and not any(k in rel for k in keys):
            continue
        log = os.path.join(OUT, rel.replace("/", "_").replace(".py", "") + ".txt")
        t0 = time.time()
        with open(log, "w", encoding="utf-8", errors="replace") as f:
            r = subprocess.run([sys.executable, "-u", rel], cwd=ROOT, stdout=f,
                               stderr=subprocess.STDOUT, timeout=3600)
        dt = time.time() - t0
        txt = open(log, encoding="utf-8", errors="replace").read()
        tail = [l.strip() for l in txt.splitlines()
                if ("失败" in l or "FAIL" in l or "通过" in l or "结果" in l
                    or "合计" in l or "全部通过" in l)]
        summary.append((name, r.returncode, dt, log, tail[-3:]))
        print("== %-20s rc=%-11d %5.0fs  %s" % (name, r.returncode, dt, log))
        for l in tail[-3:]:
            print("     " + l[:110])
        sys.stdout.flush()
    print("\n===== 汇总 =====")
    for name, rc, dt, log, tail in summary:
        print("%-20s rc=%-11d %5.0fs   %s" % (name, rc, dt, log))
    print("\n日志目录：%s" % OUT)
    print("（提示：多配置布局脚本退出码可能是 Qt 收尾的 0xC0000409，看日志里的“合计”为准）")


if __name__ == "__main__":
    main()
