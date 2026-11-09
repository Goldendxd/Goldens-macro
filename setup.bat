@echo off
setlocal EnableExtensions
title Golden's Macro Setup
cd /d "%~dp0"

set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
    where python >nul 2>nul
    if not errorlevel 1 set "PY=python"
)

if not defined PY (
    echo Python 3 was not found.
    echo Install Python from https://www.python.org/downloads/windows/
    echo Then run setup.exe again.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating private Python environment...
    %PY% -m venv ".venv"
    if errorlevel 1 goto :failed
)

echo Updating pip...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed

echo Installing macro dependencies...
".venv\Scripts\python.exe" -m pip install -r "requirements.txt"
if errorlevel 1 goto :failed

echo.
echo Setup completed successfully.
echo Start the macro with Launch.bat.
pause
exit /b 0

:failed
echo.
echo Setup failed. Check your internet connection and the messages above.
pause
exit /b 1
