# -*- coding: utf-8 -*-
"""WinToolbox 一键构建脚本。

用法：
    python build_exe.py              # 构建 exe，输出到 WinToolbox\\ 与项目根目录
    python build_exe.py --zip        # 另外在 WinToolbox\\ 重建便携版 zip

特性：
  * 自动下载 UPX（首次），用 portable.spec 打包 + UPX 压缩；
  * 版本号从 app.py 的 APP_VER 读取；
  * 产物直接放 WinToolbox 目录与项目根目录（不建版本子目录）；
  * --zip 时校验 zip < 25MB 且完整性通过；
  * 打包后自动重画 _preview 里的界面预览图（--no-preview 可跳过）。
"""
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                     # 项目根
TOOLS = os.path.join(ROOT, "_buildtools")
UPX_URL = "https://github.com/upx/upx/releases/download/v5.2.1/upx-5.2.1-win64.zip"
PYI = r"C:\Users\29566\.workbuddy\binaries\python\envs\wintoolbox\Scripts\pyinstaller.exe"

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def app_version():
    """从 app.py 读 APP_VER，避免版本号在两处维护。"""
    with open(os.path.join(HERE, "app.py"), encoding="utf-8") as f:
        m = re.search(r'^APP_VER\s*=\s*["\']([^"\']+)["\']', f.read(), re.M)
    return m.group(1) if m else ""


def ensure_upx():
    for dp, _dn, fn in os.walk(os.path.join(TOOLS, "upx")):
        for f in fn:
            if f.lower() == "upx.exe":
                return os.path.join(dp, f)
    os.makedirs(TOOLS, exist_ok=True)
    z = os.path.join(TOOLS, "upx.zip")
    print("downloading UPX …")
    req = urllib.request.Request(UPX_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as r, open(z, "wb") as f:
        f.write(r.read())
    with zipfile.ZipFile(z) as zf:
        zf.extractall(os.path.join(TOOLS, "upx"))
    return ensure_upx()


def build():
    upx = ensure_upx()
    print("UPX:", upx)
    r = subprocess.run([PYI, "--noconfirm", "--upx-dir", upx,
                        "--distpath", os.path.join(HERE, "dist"),
                        "--workpath", os.path.join(HERE, "build"),
                        os.path.join(HERE, "portable.spec")],
                       capture_output=True, text=True, errors="replace",
                       cwd=HERE, timeout=1800)
    if r.returncode != 0:
        print(r.stdout[-3000:] if r.stdout else "")
        print((r.stderr or "")[-2000:])
        sys.exit("PyInstaller 构建失败")
    exe = os.path.join(HERE, "dist", "WinToolbox.exe")
    print("exe: %.2f MB" % (os.path.getsize(exe) / 1048576))
    # 成品：WinToolbox 目录 + 项目根目录各一份
    for dst in (os.path.join(HERE, "WinToolbox.exe"),
                os.path.join(ROOT, "WinToolbox.exe")):
        shutil.copy2(exe, dst)
        print("已同步:", dst)
    with open(os.path.join(HERE, "使用说明.txt"), "w", encoding="utf-8") as f:
        f.write(readme_text())
    return exe


def readme_text():
    return ("WinToolbox 便携版 v%s 使用说明\n" % app_version() + "=" * 34 + "\n\n"
            "1. 双击 WinToolbox.exe（单文件绿色版，免安装）\n"
            "2. 首次运行会弹 UAC，允许后进入管理员模式，全部功能可用\n"
            "3. 设置页可切换浅色 / 深色，并选择界面主题色"
            "（默认跟随 Windows 的强调色）\n\n"
            "作者 by：xixidan\n"
            "GitHub：https://github.com/ncbsz/WinToolbox\n")


def make_previews():
    """重画 _preview\\ 里的界面预览图。

    预览图以前是手工截的 —— 改完 UI 就过期，很容易「图与代码脱节」。
    渲染较慢（十几个页面状态 × 两套主题），所以给个 --no-preview 逃生口；
    失败不影响打包结果（只是预览图没更新）。
    """
    script = os.path.join(HERE, "tools", "make_previews.py")
    if not os.path.isfile(script):
        print("跳过预览图：找不到 tools/make_previews.py")
        return
    print("正在重画界面预览图（--no-preview 可跳过）…")
    try:
        r = subprocess.run([sys.executable, script], capture_output=True,
                           text=True, errors="replace", cwd=HERE, timeout=900)
    except subprocess.TimeoutExpired:
        print("预览图超时，已跳过")
        return
    lines = [x for x in (r.stdout or "").splitlines() if x.strip()]
    for line in lines[-3:]:
        print("  " + line)
    if r.returncode != 0:
        print((r.stderr or "")[-600:])


def make_zip(exe):
    zpath = os.path.join(HERE, "WinToolbox-便携版.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.write(exe, "WinToolbox-便携版/WinToolbox/WinToolbox.exe")
        zf.writestr("WinToolbox-便携版/WinToolbox/使用说明.txt",
                    readme_text().encode("utf-8"))
    size = os.path.getsize(zpath)
    print("zip: %.2f MB" % (size / 1048576))
    assert size < 25 * 1048576, "zip 超过 25MB"
    with zipfile.ZipFile(zpath) as zf:
        assert zf.testzip() is None
    print("zip 校验通过 (<25MB)")


if __name__ == "__main__":
    t0 = time.time()
    exe = build()
    if "--no-preview" not in sys.argv:
        make_previews()
    if "--zip" in sys.argv:
        make_zip(exe)
    print("DONE %.0fs" % (time.time() - t0))
