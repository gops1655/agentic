@echo off
REM Double-click this file to set up / open the Invoice Agent on Windows.
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
    .venv\Scripts\python.exe -m pip install --upgrade pip >nul
)
call .venv\Scripts\activate.bat
echo Checking components...
pip install -q --disable-pip-version-check -r requirements.txt

set PYTHONIOENCODING=utf-8
if not exist "data\shortcut.done" (
    python main.py install-shortcut && (if not exist data mkdir data) && echo done> "data\shortcut.done"
)
python main.py %*
pause
