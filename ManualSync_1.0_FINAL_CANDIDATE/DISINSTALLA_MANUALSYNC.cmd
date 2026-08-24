@echo off
setlocal EnableExtensions
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0DISINSTALLA_MANUALSYNC.ps1"
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%
