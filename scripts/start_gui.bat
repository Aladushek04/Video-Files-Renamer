@echo off
setlocal
chcp 65001 >nul
title Video Files Renamer
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo Preparing GUI environment...
    python -m venv .venv
    if errorlevel 1 goto error
    ".venv\Scripts\python.exe" -m pip install -r requirements\gui.txt
    if errorlevel 1 goto error
)

".venv\Scripts\python.exe" -c "import customtkinter" >nul 2>&1
if errorlevel 1 (
    echo Installing GUI dependencies...
    ".venv\Scripts\python.exe" -m pip install -r requirements\gui.txt
    if errorlevel 1 goto error
)

rem launcher.py сам проверяет обновления Git и затем запускает GUI.
".venv\Scripts\python.exe" launcher.py %*
if errorlevel 1 pause
exit /b 0

:error
echo.
echo Failed to prepare the GUI environment.
pause
exit /b 1
