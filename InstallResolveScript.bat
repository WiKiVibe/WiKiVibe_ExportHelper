@echo off
REM ASCII-only. Optional: install Resolve menu entry only.
title WiKiVibe ExportHelper - Resolve Script
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup\InstallResolveScript.ps1" %*
set ERR=%ERRORLEVEL%
if not "%ERR%"=="0" echo Exit code %ERR%.
echo.
pause
exit /b %ERR%
