# -*- mode: python ; coding: utf-8 -*-
# 便携版/安装版瘦身构建：去掉运行时用不到的大体积二进制。
# 用法：pyinstaller --noconfirm --upx-dir <upx目录> portable.spec
a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('rules.json', '.'), ('tools', 'tools')],
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
    'libcrypto',         # QtNetwork 的 SSL 后端
    'libssl',
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
    upx_exclude=['vcruntime140.dll', 'vcruntime140_1.dll'],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
