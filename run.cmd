@echo off
rem Start the Stanton Trade Optimizer. The first run sets everything up (Python included).
setlocal
cd /d "%~dp0"
title Stanton Trade Optimizer
set PYTHONUTF8=1
set NEEDS_SETUP=0
if not exist ".venv\Scripts\python.exe" set NEEDS_SETUP=1
if not exist ".venv\.setup-ok" set NEEDS_SETUP=1
if exist ".venv\.setup-ok" fc /b "requirements.txt" ".venv\.setup-ok" >nul 2>&1 || set NEEDS_SETUP=1
if "%NEEDS_SETUP%"=="1" (
  echo Setting things up. This only happens the first time, or after an update.
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
  if errorlevel 1 (
    echo.
    echo Setup did not finish. Read the messages above, then run setup.cmd to try again.
    pause
    exit /b 1
  )
)
".venv\Scripts\python.exe" -m sto %*
if errorlevel 1 pause
