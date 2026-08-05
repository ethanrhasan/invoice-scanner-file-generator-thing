@echo off
cd /d "%~dp0src"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw gui.py
) else (
    start "" py gui.py
)
