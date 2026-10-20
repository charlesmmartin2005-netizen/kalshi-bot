@echo off
rem Double-click to launch the dashboard (opens http://127.0.0.1:8050).
cd /d "%~dp0"
".venv\Scripts\python.exe" dashboard.py
pause
