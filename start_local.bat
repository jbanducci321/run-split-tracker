@echo off
rem Double-click to run the tracker on this computer (see LOCAL_HOSTING.md).
rem Keep this window open while you want tracking to work; close it or press Ctrl+C to stop.
cd /d "%~dp0"
venv\Scripts\python.exe serve_local.py
pause
