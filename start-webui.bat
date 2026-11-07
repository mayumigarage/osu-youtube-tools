@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo .venv not found. Please follow the setup steps in README.md first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m osu2yt serve
if errorlevel 1 pause
