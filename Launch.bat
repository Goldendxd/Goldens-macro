@echo off
setlocal EnableExtensions
title Golden's Slayers 2 Fishing Macro
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo Setup has not been completed.
    echo Run setup.exe first.
    pause
    exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" "slayers2_fisher.py"
exit /b 0
