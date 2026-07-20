@echo off
REM ASCII-only.
title WiKiVibe ExportHelper
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Launch.ps1" %*
exit /b %ERRORLEVEL%
