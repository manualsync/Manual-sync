@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title ManualSync - Installazione pilota
if not exist "%~dp0INSTALLA_MANUALSYNC.ps1" (
  echo Pacchetto installazione incompleto.
  pause
  exit /b 2
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0INSTALLA_MANUALSYNC.ps1"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo Installazione non completata. Codice: %RC%
  pause
  exit /b %RC%
)
echo.
echo ManualSync installato correttamente.
pause
exit /b 0
