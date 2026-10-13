@echo off
rem ============================================================
rem  Build WinToolbox.exe  (single file, no console window)
rem  Double-click this file. Output: dist\WinToolbox.exe
rem ============================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\wintoolbox\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0..\..\..\.workbuddy\binaries\python\envs\wintoolbox\Scripts\python.exe"
if not exist "%PY%" (
    echo.
    echo   [ERROR] python venv not found.
    echo   Expected: %USERPROFILE%\.workbuddy\binaries\python\envs\wintoolbox
    echo   Create it with:
    echo       python -m venv "%%USERPROFILE%%\.workbuddy\binaries\python\envs\wintoolbox"
    echo       ^<venv^>\Scripts\python.exe -m pip install PySide6-Essentials pyinstaller
    echo.
    pause
    exit /b 1
)

echo   [1/3] generating icon...
"%PY%" make_icon.py

echo   [2/3] building (portable.spec, this takes a few minutes)...
rem 统一走 build_exe.py —— 它用 portable.spec（带 EXCLUDE_WORDS 体积瘦身 + OpenSSL 兜底）。
rem 这里以前手写了一份 PyInstaller 参数，与 portable.spec 漂移：那份参数没有 SSL 兜底，
rem 用它打出来的 exe 会 "import _ssl 失败"，HTTPS 全部不可用（检查更新永远失败）。
rem 只保留一条构建路径，才不会再次漂移。
"%PY%" build_exe.py --no-preview
if errorlevel 1 (
    echo.
    echo   [ERROR] build failed.
    pause
    exit /b 1
)

echo   [3/3] done.
echo.
echo   Output: %~dp0WinToolbox.exe  (以及项目根目录一份)
echo.
pause
endlocal
