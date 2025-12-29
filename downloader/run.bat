@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
setlocal

setlocal enabledelayedexpansion

set PROJECT_NAME=SKC.fm Downloader
set MAIN_SOURCE_FILE=skcfm_downloader
set VENV_DIR=.venv
set MIN_PYTHON=3.10

echo ===============================
echo   Starting %PROJECT_NAME%
echo ===============================

REM ---------- Find Python ----------
where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH.
    echo Please install Python %MIN_PYTHON% or newer.
    echo https://www.python.org/downloads/windows/
    pause
    exit /b 1
)

REM ---------- Create venv if needed ----------
if not exist "%VENV_DIR%" (
    echo Creating virtual environment...
    python -m venv %VENV_DIR%
)

REM ---------- Activate venv ----------
echo Activating virtual environment...
call %VENV_DIR%\Scripts\activate.bat
if errorlevel 1 (
    echo ERROR: Failed to activate virtual environment.
    pause
    exit /b 1
)

REM ---------- Upgrade pip ----------
echo Updating pip...
python -m pip install --upgrade pip >nul

REM ---------- Install project ----------
echo Installing dependencies...
pip install -r requirements.txt >nul
if errorlevel 1 (
    echo ERROR: Dependency installation failed.
    pause
    exit /b 1
)

REM ---------- Run app ----------
echo Running application...
python -m %MAIN_SOURCE_FILE%

echo.
echo Laku noc.