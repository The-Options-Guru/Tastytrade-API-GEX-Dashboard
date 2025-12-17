@echo off
chcp 65001 >nul
echo.
echo ================================================
echo   Simple GEX Dashboard (Weekend-Compatible)
echo ================================================
echo.

cd /d "%~dp0"
python -m streamlit run simple_dashboard.py
