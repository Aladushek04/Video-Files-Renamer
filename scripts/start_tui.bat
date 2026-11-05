@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo Preparing TUI environment...
    python -m venv .venv
    if errorlevel 1 goto error
    ".venv\Scripts\python.exe" -m pip install -r requirements\tui.txt
    if errorlevel 1 goto error
)

".venv\Scripts\python.exe" -c "import textual" >nul 2>&1
if errorlevel 1 (
    echo Installing TUI dependencies...
    ".venv\Scripts\python.exe" -m pip install -r requirements\tui.txt
    if errorlevel 1 goto error
)

".venv\Scripts\python.exe" -m video_renamer.tui %*
if errorlevel 1 pause
exit /b 0

:error
echo.
echo Failed to prepare the TUI environment.
pause
exit /b 1
