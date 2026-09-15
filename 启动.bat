@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title WinToolbox - System Toolbox

rem ------------------------------------------------------------------
rem  Self-elevate: HKLM registry writes, service control, network reset
rem  and policy cleanup all need administrator rights.
rem ------------------------------------------------------------------
net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Requesting administrator privileges...
    echo.
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

rem ------------------------------------------------------------------
rem  Locate a Python 3 interpreter
rem ------------------------------------------------------------------
set "PY="
for %%P in (python.exe) do if not defined PY set "PY=%%~$PATH:P"

if not defined PY for %%D in (
    "%USERPROFILE%\AppData\Local\Programs\Python\Python313\python.exe"
    "%USERPROFILE%\AppData\Local\Programs\Python\Python312\python.exe"
    "%USERPROFILE%\AppData\Local\Programs\Python\Python311\python.exe"
    "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
    "%USERPROFILE%\.workbuddy\binaries\python\versions\3.14.5\python.exe"
    "C:\Python314\python.exe"
    "C:\Python313\python.exe"
    "C:\Python312\python.exe"
    "C:\Python311\python.exe"
) do if not defined PY if exist "%%~D" set "PY=%%~D"

if not defined PY (
    echo.
    echo   [ERROR] No python.exe found.
    echo   Please install Python 3.9+ ^(python.org^) and tick "Add to PATH",
    echo   or edit this file and set PY to your python.exe path.
    echo.
    pause
    exit /b 1
)

echo   Using   : %PY%
echo   Address : http://127.0.0.1:8760/
echo.
echo   Close this window to stop the toolbox.
echo.

"%PY%" server.py
echo.
pause
endlocal
