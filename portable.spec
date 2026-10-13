# -*- mode: python ; coding: utf-8 -*-
# 便携版/安装版瘦身构建：去掉运行时用不到的大体积二进制。
# 用法：pyinstaller --noconfirm --upx-dir <upx目录> portable.spec
a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('rules.json', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['unittest', 'pydoc_data', 'test', 'xmlrpc', 'sqlite3', '_sqlite3',
              'py_compile', 'compileall', 'configparser', 'asyncio'],
    # 注意：email 绝不能排除 —— http.server 依赖它，缺了打包版启动即崩
    # （ModuleNotFoundError: No module named 'email'）。
    noarchive=False,
    optimize=0,
)

# ---- 二进制瘦身过滤（按目标路径匹配，全部为运行时确认用不到的组件） ----
EXCLUDE_WORDS = [
    'opengl32sw',        # 软件 OpenGL 后备（纯 QWidget 光栅渲染用不到）20MB
    'qdirect2d',         # 备用平台插件（qwindows 已覆盖）
    'qt6network',        # QtNetwork 全家：代码无一处使用
    'qtnetwork.pyd',
    # QtNetwork 的 SSL 后端（在 PySide6\ 目录下）。
    # ⚠️ 必须带上 'pyside6\' 前缀：裸写 'libcrypto' / 'libssl' 会连
    #    Python 运行时自己的 libcrypto-3-x64.dll / libssl-3-x64.dll 一起删掉，
    #    导致打包版 `import _ssl` 失败（DLL load failed）→ HTTPS 全线不可用，
    #    表现为「检查更新」永远失败。这是曾真实踩过的坑。
    'pyside6\\libcrypto',
    'pyside6\\libssl',
    'networkinformation',
    'qt6svg',            # SVG 支持：桌面端无 .svg 资源（web 图标是内联 HTML）
    'qsvg',
    'qwebp', 'qtiff', 'qicns', 'qwbmp',   # 用不到的图片格式插件（qico 保留）
    'translations\\',    # Qt 自带 30+ 语言翻译 .qm：界面为硬编码中文，用不上 ~1.5MB
    'qopensslbackend', 'qschannelbackend', 'qcertonlybackend',  # TLS 后端：QtNetwork 已删
    'qjpeg',             # 应用不加载 JPEG
    'qoffscreen', 'qminimal',             # 后备平台插件（桌面会话用不到）
    'qtuiotouch',        # 触摸输入插件
]

def _keep(entry):
    name = entry[0].replace('/', '\\').lower()
    if name == 'cygwin1.dll':
        # 根目录重复份（PyInstaller 依赖分析收进来的）；tools\cygwin1.dll 保留
        return False
    return not any(w in name for w in EXCLUDE_WORDS)

a.binaries = [e for e in a.binaries if _keep(e)]

# ---- SSL 依赖兜底：无论上面的过滤怎么写，Python 运行时的 OpenSSL 库必须留下 ----
# `_ssl.pyd` 依赖 libssl / libcrypto，缺了就是 "DLL load failed while importing _ssl"。
# 这里按名字显式捞一遍，防止将来有人再往 EXCLUDE_WORDS 里加宽泛的词把它误伤。
import glob
import os
import sys as _sys

_have = {e[0].replace('/', '\\').lower() for e in a.binaries}
for _pat in ('libssl*.dll', 'libcrypto*.dll'):
    for _p in glob.glob(os.path.join(_sys.base_prefix, 'DLLs', _pat)):
        _n = os.path.basename(_p)
        if _n.lower() not in _have:
            a.binaries.append((_n, _p, 'BINARY'))
            _have.add(_n.lower())
            print('[spec] 补回 SSL 依赖:', _n)
# PySide6 的翻译 .qm 等资源挂在 datas 下，同样按黑名单过滤
# （上一版只过滤了 binaries，导致 30+ 个 .qm 全部打进 exe，多占约 1MB）
a.datas = [e for e in a.datas if _keep(e)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='WinToolbox',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=['vcruntime140.dll', 'vcruntime140_1.dll',
                 # OpenSSL 库不压：UPX 压过的 libcrypto/libssl 有加载失败的报告，
                 # 而它一坏就是整条 HTTPS 链路报废，不值得为几百 KB 冒险
                 'libssl-3-x64.dll', 'libcrypto-3-x64.dll'],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
    # 请求管理员权限：exe 的 manifest 会写入 requireAdministrator，
    # 双击即由 Windows 直接弹出 UAC —— 进程从创建那一刻就带管理员令牌，
    # 服务 / 注册表 / 策略 / DNS 设置等写入类功能全部可用。
    uac_admin=True,
)
