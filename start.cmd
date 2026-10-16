@echo off
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" "%~dp0launch.py"
) else (
  py -3 "%~dp0launch.py"
)
if errorlevel 1 pause
