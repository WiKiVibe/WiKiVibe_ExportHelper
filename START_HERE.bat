@echo off
REM ASCII-only. First-run entry for new users.
title WiKiVibe ExportHelper - Setup
cd /d "%~dp0"
echo.
echo  WiKiVibe ExportHelper - first-time setup
echo  -----------------------------------------
echo  This will install Python packages and create shortcuts.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup\Install.ps1" %*
set ERR=%ERRORLEVEL%
if not "%ERR%"=="0" echo Exit code %ERR%.
echo.
pause
exit /b %ERR%
