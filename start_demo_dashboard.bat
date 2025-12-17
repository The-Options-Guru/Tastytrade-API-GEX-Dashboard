@echo off
chcp 65001 >nul
echo.
echo ================================================
echo   GEX Demo Dashboard (No API Required)
echo ================================================
echo.
echo Using public dxFeed demo endpoint
echo Data is DELAYED - SPX only
echo.
echo No Tastytrade API credentials needed!
echo.

cd /d "%~dp0"
python -m streamlit run demo_dashboard.py
