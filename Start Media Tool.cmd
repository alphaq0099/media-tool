@echo off
setlocal
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
    echo The project's Python environment is missing. See README.md for setup.
    pause
    exit /b 1
)
start "" /D "%~dp0" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0run.py"
