# -*- coding: utf-8 -*-
"""WinToolbox 一键构建脚本。

用法：
    python build_exe.py              # 构建 exe 并放到 <项目根>\\<版本>\\安装\\
    python build_exe.py --zip        # 另外在该目录重建便携版 zip

特性：
  * 自动下载 UPX（首次），用 portable.spec 打包 + UPX 压缩；
  * 版本号从 app.py 的 APP_VER 读取，产物自动落到对应版本的「安装」目录；
  * 不再往项目根目录另放一份 exe（安装目录那份就是唯一成品）；
  * --zip 时校验 zip < 25MB 且完整性通过。
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
ROOT = os.path.dirname(HERE)                     # 项目根（下面按版本号分目录）
TOOLS = os.path.join(ROOT, "_buildtools")
UPX_URL = "https://github.com/upx/upx/releases/download/v5.2.1/upx-5.2.1-win64.zip"
PYI = r"C:\Users\29566\.workbuddy\binaries\python\envs\wintoolbox\Scripts\pyinstaller.exe"

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def app_version():
    """从 app.py 读 APP_VER，避免版本号在两处维护。"""
    with open(os.path.join(HERE, "app.py"), encoding="utf-8") as f:
        m = re.search(r'^APP_VER\s*=\s*["\']([^"\']+)["\']', f.read(), re.M)
    return m.group(1) if m else ""


def dist_dir():
    """成品目录：<项目根>\\<版本>\\安装\\"""
    v = app_version()
    return os.path.join(ROOT, v, "安装") if v else os.path.join(ROOT, "安装")


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
    d = dist_dir()
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, "WinToolbox.exe")
    shutil.copy2(exe, dst)
    print("已同步:", dst)
    with open(os.path.join(d, "使用说明.txt"), "w", encoding="utf-8") as f:
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


def make_zip(exe):
    d = dist_dir()
    os.makedirs(d, exist_ok=True)
    zpath = os.path.join(d, "WinToolbox-便携版.zip")
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
    if "--zip" in sys.argv:
        make_zip(exe)
    print("DONE %.0fs" % (time.time() - t0))
