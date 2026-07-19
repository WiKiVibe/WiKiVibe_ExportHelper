@echo off
REM ASCII-only. Same as START_HERE.bat (kept for compatibility).
title WiKiVibe ExportHelper - Setup
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup\Install.ps1" %*
set ERR=%ERRORLEVEL%
if not "%ERR%"=="0" echo Exit code %ERR%.
echo.
pause
exit /b %ERR%
