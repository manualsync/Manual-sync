@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title ManualSync 1.0 - FINAL CANDIDATE - Internal Build

set "ROOT=%~dp0"
set "BUILD_PS1=%ROOT%BUILD_WINDOWS.ps1"
set "FINAL_EXE=%ROOT%dist\ManualSync\ManualSync.exe"
set "BUILD_STATUS=%ROOT%BUILD_STATUS.json"
set "BUILD_LOG=%ROOT%BUILD_FINAL_CANDIDATE_LOG.txt"
set "PILOT_ZIP=%ROOT%release\ManualSync_Setup_FINAL_CANDIDATE.zip"

echo.
echo =====================================================================
echo ManualSync 1.0 - FINAL CANDIDATE
echo BUILD INTERNA - non e il pacchetto da consegnare al cliente
echo Cartella: %ROOT%
echo =====================================================================

echo(%ROOT%| findstr /I /C:"\AppData\Local\Temp\" >nul
if not errorlevel 1 (
  echo.
  echo BUILD NON AVVIATA.
  echo Il file e stato aperto direttamente dentro lo ZIP.
  echo Usa ESTRAI TUTTO e avvia questo file dalla cartella estratta.
  echo.
  pause
  exit /b 10
)

if not exist "%BUILD_PS1%" (
  echo.
  echo BUILD NON AVVIATA: manca BUILD_WINDOWS.ps1.
  echo Riestrai completamente il pacchetto.
  echo.
  pause
  exit /b 11
)

if exist "%ROOT%dist" rmdir /S /Q "%ROOT%dist" >nul 2>&1
if exist "%ROOT%release" rmdir /S /Q "%ROOT%release" >nul 2>&1
if exist "%BUILD_STATUS%" del /Q "%BUILD_STATUS%" >nul 2>&1

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%BUILD_PS1%"
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" goto :failed
if not exist "%FINAL_EXE%" (
  set "RC=12"
  goto :failed
)
if not exist "%PILOT_ZIP%" (
  set "RC=13"
  goto :failed
)

echo.
echo =====================================================================
echo BUILD COMPLETATA E VERIFICATA.
echo.
echo App interna: dist\ManualSync\ManualSync.exe
echo Pacchetto PILOTA da testare come cliente:
echo release\ManualSync_Setup_FINAL_CANDIDATE.zip
echo.
echo Il cliente NON deve ricevere questa cartella BuildKit.
echo =====================================================================
pause
exit /b 0

:failed
echo.
echo =====================================================================
echo BUILD FALLITA - codice %RC%.
echo NON usare e NON distribuire questa build.
echo Log: %BUILD_LOG%
echo Stato: %BUILD_STATUS%
echo =====================================================================
pause
exit /b %RC%
