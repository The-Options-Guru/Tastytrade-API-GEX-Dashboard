@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo SPXW live trade recorder - leave this window running.
echo Status: http://127.0.0.1:8502/health
"%~dp0.venv\Scripts\python.exe" flow_recorder.py
if errorlevel 1 pause
