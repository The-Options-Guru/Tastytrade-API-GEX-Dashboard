@echo off
chcp 65001 >nul
echo.
echo ================================================
echo   Installing Dependencies
echo ================================================
echo.

cd /d "%~dp0"

echo Checking Python installation...
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ ERROR: Python is not installed or not in PATH
    echo.
    echo Please install Python 3.8+ from https://www.python.org/downloads/
    echo ⚠️  Make sure to check "Add Python to PATH" during installation
    echo.
    pause
    exit /b 1
)

echo ✅ Python found
echo.
echo Installing packages from requirements.txt...
echo.

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

if errorlevel 1 (
    echo.
    echo ❌ Installation failed
    echo.
    echo Try running this command manually:
    echo   pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo.
echo ================================================
echo   ✅ Installation Complete!
echo ================================================
echo.
echo Next steps:
echo   1. Configure your API credentials in .env file
echo   2. Run: start_simple_dashboard.bat
echo.
pause
