@echo off
chcp 65001 >nul
echo Refreshing Tastytrade tokens...
echo.
cd /d "%~dp0"
python get_access_token.py
echo.
python get_streamer_token.py
echo.
echo Done! Tokens refreshed.
pause
