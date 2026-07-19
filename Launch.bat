@echo off
REM ASCII-only. Run the app (portable or after install).
title WiKiVibe ExportHelper
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup\Launch.ps1" %*
exit /b %ERRORLEVEL%
