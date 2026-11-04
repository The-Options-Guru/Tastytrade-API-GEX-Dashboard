@echo off
chcp 65001 >nul
echo.
echo ================================================
echo   Simple GEX Dashboard (Weekend-Compatible)
echo ================================================
echo.

cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" -m streamlit run simple_dashboard.py --server.address=127.0.0.1 --server.port=8501 --browser.gatherUsageStats=false
