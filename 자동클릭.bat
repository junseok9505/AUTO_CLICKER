@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    if errorlevel 1 (
        echo ERROR: Package installation failed
        pause
        exit /b 1
    )
)

start "" ".venv\Scripts\pythonw.exe" "%~dp0run.py"
exit