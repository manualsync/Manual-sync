@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0VERIFICA_INSTALLAZIONE_PILOTA.ps1"
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (echo VERIFICA INSTALLAZIONE: PASS) else (echo VERIFICA INSTALLAZIONE: FAIL)
pause
exit /b %RC%
