@echo off
REM Double-click this file to start the Invoice Agent on Windows.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python is not installed. Download it from https://www.python.org/downloads/
    echo During install, tick "Add python.exe to PATH".
    pause
    exit /b 1
)

if not exist .venv (
    echo First run: setting up, this takes a minute...
    python -m venv .venv
    call .venv\Scripts\activate.bat
    python -m pip install --upgrade pip >nul
    pip install -r requirements.txt
) else (
    call .venv\Scripts\activate.bat
)

set PYTHONIOENCODING=utf-8
python main.py %*
pause
