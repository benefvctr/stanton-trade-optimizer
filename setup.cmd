@echo off
rem Repair or update the app: reinstalls packages and offers the shortcut again.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
echo.
pause
