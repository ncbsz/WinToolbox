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

echo   [2/3] building with PyInstaller (this takes a few minutes)...
"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name WinToolbox --icon icon.ico --add-data "rules.json;." --add-data "tools;tools" app.py
if errorlevel 1 (
    echo.
    echo   [ERROR] build failed.
    pause
    exit /b 1
)

echo   [3/3] done.
echo.
echo   Output: %~dp0dist\WinToolbox.exe
echo.
pause
endlocal
