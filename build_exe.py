# -*- coding: utf-8 -*-
"""WinToolbox 一键构建脚本。

用法：
    python build_exe.py              # 构建 dist/WinToolbox.exe 并同步到上层目录
    python build_exe.py --zip        # 另外重建 安装包/WinToolbox-便携版.zip

特性：
  * 自动下载 UPX（首次），用 portable.spec 打包 + UPX 压缩；
  * 产物同步到项目上层目录（用户直接双击的那个 WinToolbox.exe）；
  * --zip 时校验 zip < 25MB 且完整性通过。
"""
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                     # 项目上层（放成品 exe）
PKG_DIR = os.path.join(ROOT, "安装包")
TOOLS = os.path.join(ROOT, "_buildtools")
UPX_URL = "https://github.com/upx/upx/releases/download/v5.2.1/upx-5.2.1-win64.zip"
PYI = r"C:\Users\29566\.workbuddy\binaries\python\envs\wintoolbox\Scripts\pyinstaller.exe"

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


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
    dst = os.path.join(ROOT, "WinToolbox.exe")
    shutil.copy2(exe, dst)
    print("已同步:", dst)
    return exe


def make_zip(exe):
    readme = ("WinToolbox 便携版使用说明\n==========================\n\n"
              "1. 解压到任意目录（保持 WinToolbox.exe 单文件即可）\n"
              "2. 双击 WinToolbox.exe，允许 UAC 提权\n"
              "3. 启动即管理员模式，全部功能可用\n\n"
              "作者 by：xixidan\n"
              "GitHub：https://github.com/ncbsz/WinToolbox\n").encode("utf-8")
    os.makedirs(PKG_DIR, exist_ok=True)
    zpath = os.path.join(PKG_DIR, "WinToolbox-便携版.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.write(exe, "WinToolbox-便携版/WinToolbox/WinToolbox.exe")
        zf.writestr("WinToolbox-便携版/WinToolbox/使用说明.txt", readme)
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
