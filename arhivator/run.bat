@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
setlocal
setlocal enabledelayedexpansion

set PROJECT_NAME=Arhivator
set MAIN_SOURCE_FILE=wavesnip
set VENV_DIR=.venv
set MIN_PYTHON=3.12

echo ===============================
echo   Starting %PROJECT_NAME%
echo ===============================

echo.
echo.         .-=+*#######*+=-.
echo.     =@@@@p. :#@@@@@#: .q@@@@@=
echo.    +@@@@@.#@. _-*-_ .@#.@@@@@+
echo.   .@@@@@@.#@@@@@@@@@@@#.@@@@@@.
echo.   +@@@@@@ @@@P@@@@@P@@@ @@@@@@+
echo.   +@++   +@@ @ #@# @ @@+   +@++
echo.       -+#@@@@@@* *@@@@@@#+-
echo.        -+@@@@:= : =:@@@@+-
echo.         *@@@@@@@@@@@@@@@*
echo.
echo.          M i š a s o f t
echo.

REM ---------- Find a suitable Python ----------
REM Python %MIN_PYTHON%+ is required: the stdlib "wave" module only learned to
REM read WAVE_FORMAT_EXTENSIBLE in 3.12, and many recorders emit it.
set "PYEXE="

py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
if not errorlevel 1 set "PYEXE=py -3"

if not defined PYEXE (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
    if not errorlevel 1 set "PYEXE=python"
)

if not defined PYEXE goto :no_python

%PYEXE% -c "import sys; print('Found Python', sys.version.split()[0])"

REM ---------- Create virtual environment if needed ----------
REM Also recreates it when the existing one was built with an older Python.
set "RECREATE="
if exist "%VENV_DIR%\Scripts\python.exe" (
    "%VENV_DIR%\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
    if errorlevel 1 set "RECREATE=1"
) else (
    set "RECREATE=1"
)

if defined RECREATE (
    if exist "%VENV_DIR%" (
        echo Removing outdated virtual environment...
        rmdir /s /q "%VENV_DIR%"
    )
    echo Creating virtual environment...
    %PYEXE% -m venv %VENV_DIR%
    if errorlevel 1 goto :venv_failed
)

REM ---------- Activate venv ----------
echo Activating virtual environment...
call "%VENV_DIR%\Scripts\activate.bat"
if errorlevel 1 goto :venv_failed

REM ---------- Upgrade pip ----------
echo Updating pip...
python -m pip install --upgrade pip
if errorlevel 1 echo WARNING: pip upgrade failed, continuing.

REM ---------- Install dependencies ----------
REM Not silenced on purpose: first run downloads PySide6, which is large.
echo Installing dependencies ^(first run downloads PySide6, please wait^)...
python -m pip install -r requirements.txt
if errorlevel 1 goto :deps_failed

REM ---------- Run app ----------
echo Running application...
python -m %MAIN_SOURCE_FILE%
if errorlevel 1 goto :app_failed

echo.
echo Laku noc.
pause
exit /b 0

:no_python
echo.
echo ERROR: Python %MIN_PYTHON% or newer is required, but was not found.
echo The stdlib "wave" module cannot read WAVE_FORMAT_EXTENSIBLE before
echo 3.12, which many audio recorders emit.
echo.
echo Install Python %MIN_PYTHON%+ from https://www.python.org/downloads/windows/
echo and tick "Add python.exe to PATH" during setup.
pause
exit /b 1

:venv_failed
echo.
echo ERROR: Failed to create or activate the virtual environment.
pause
exit /b 1

:deps_failed
echo.
echo ERROR: Dependency installation failed.
pause
exit /b 1

:app_failed
echo.
echo ERROR: Application exited with an error.
pause
exit /b 1
