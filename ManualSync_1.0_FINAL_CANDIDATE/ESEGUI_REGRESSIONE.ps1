# ESEGUI_REGRESSIONE.ps1 - la sweep di regressione, con guardia d'ambiente.
#
# Perche' esiste
# --------------
# Una sweep completa e' stata eseguita sull'interprete di sistema
# (Python 3.14, PyMuPDF 1.28) invece che sull'ambiente del progetto
# (3.12, PyMuPDF 1.26.7). Il rilevatore di tabelle di PyMuPDF e' cambiato
# fra quelle versioni, quindi una suite che fissa un conteggio di tabelle
# ha segnalato una regressione di prodotto inesistente -- creduta due
# volte, e riportata al committente come difetto aperto.
#
# Un ambiente sbagliato non e' un test fallito: rende ogni risultato privo
# di significato. Questo script accerta l'ambiente PRIMA, e si ferma.
#
# Non e' un secondo sistema di verifica: usa environment_guard, lo stesso
# che usa BUILD_WINDOWS.ps1.
#
# Uso:
#   .\ESEGUI_REGRESSIONE.ps1                 tutte le suite non-browser
#   .\ESEGUI_REGRESSIONE.ps1 -IncludeBrowser anche le suite browser (Chrome)
#   .\ESEGUI_REGRESSIONE.ps1 -Filter impact  solo le suite col nome corrispondente

[CmdletBinding()]
param(
  [switch]$IncludeBrowser,
  [string]$Filter = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

# L'interprete del progetto, non quello che capita nel PATH. E' esattamente
# la distinzione che questo script esiste per imporre.
$Python = Join-Path (Split-Path -Parent $Root) '.venv\Scripts\python.exe'
if (!(Test-Path $Python)) {
  Write-Host "Ambiente di progetto assente: $Python" -ForegroundColor Red
  Write-Host "Crearlo con: py -3.12 -m venv .venv; .venv\Scripts\pip install -r ManualSync_1.0_FINAL_CANDIDATE\requirements.lock.txt" -ForegroundColor Yellow
  exit 2
}

# Esegue Python con stdout/stderr su file e restituisce l'exit code.
# NON usare `& python ...` qui: in Windows PowerShell 5.1, con
# ErrorActionPreference='Stop' e un output reindirizzato, la prima riga
# scritta su stderr da un processo nativo diventa un NativeCommandError
# TERMINANTE. Un solo traceback interrompeva l'intera sweep senza nemmeno
# stampare FAIL, e nascondeva l'esito di tutte le suite successive.
function Invoke-PythonLogged([string]$Exe, [string[]]$Arguments, [string]$WorkDir, [string]$OutFile, [string]$ErrFile) {
  $quoted = @($Arguments | ForEach-Object { '"' + ($_ -replace '"', '\"') + '"' })
  $p = Start-Process -FilePath $Exe -ArgumentList $quoted -WorkingDirectory $WorkDir `
        -NoNewWindow -Wait -PassThru -RedirectStandardOutput $OutFile -RedirectStandardError $ErrFile
  return [int]$p.ExitCode
}

function Show-Tail([string]$Path, [int]$Lines = 6) {
  if ((Test-Path $Path) -and (Get-Item -LiteralPath $Path).Length -gt 0) {
    Get-Content -LiteralPath $Path -Tail $Lines | ForEach-Object { Write-Host "       $_" -ForegroundColor DarkRed }
  }
}

# One directory per run: logs, and a private TEMP for the suites.
# Kept only when the sweep fails; removed entirely when it is green.
$RunDir = Join-Path ([IO.Path]::GetTempPath()) ('ms_regressione_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
$LogDir = Join-Path $RunDir 'log'
$RunTemp = Join-Path $RunDir 'tmp'
New-Item -ItemType Directory -Path $LogDir, $RunTemp -Force | Out-Null

Write-Host '==> Guardia ambiente' -ForegroundColor Cyan
$GuardOut = Join-Path $LogDir 'environment_guard.out.txt'
$GuardErr = Join-Path $LogDir 'environment_guard.err.txt'
$rc = Invoke-PythonLogged $Python @('-c', "import sys; sys.path.insert(0, r'$Root'); import environment_guard; environment_guard.assert_supported_environment(); print('ambiente conforme:', sys.version.split()[0])") $Root $GuardOut $GuardErr
if ($rc -ne 0) {
  Show-Tail $GuardErr 12
  Write-Host 'Nessuna suite eseguita: i risultati non sarebbero significativi.' -ForegroundColor Red
  exit 2
}
Get-Content -LiteralPath $GuardOut | ForEach-Object { Write-Host "  $_" -ForegroundColor Green }

# Dati reali protetti (22/09/2026)
# --------------------------------
# Una sweep aveva toccato la cartella dati REALE dell'utente (progetti ZZ_
# creati e cancellati, puntatore al progetto attivo riscritto, database reali
# aperti). Ogni suite ora si isola da sola (test_isolation.py), ma il runner
# non si fida: registra l'impronta di ManualSyncData PRIMA e la ricontrolla
# DOPO. Un file creato, cancellato o modificato -- o una cartella in cui sono
# stati creati e cancellati file, anche solo temporanei -- rende la sweep
# FALLITA, con l'elenco esatto. Tenere ERYLOS chiuso sui dati reali durante
# la sweep: il suo stesso lavoro verrebbe (correttamente) segnalato.
$ProtectedDataHome = $env:ERYLOS_PROTECTED_DATA_HOME
if (-not $ProtectedDataHome) { $ProtectedDataHome = Join-Path ([Environment]::GetFolderPath('UserProfile')) 'ManualSyncData' }
$env:ERYLOS_PROTECTED_DATA_HOME = $ProtectedDataHome
$RealBefore = Join-Path $RunDir 'dati_reali_prima.json'
$RealReport = Join-Path $RunDir 'dati_reali_report.txt'
$RealGuard = Join-Path $Root 'real_data_guard.py'
Write-Host '==> Impronta dei dati reali (protetti, sola lettura)' -ForegroundColor Cyan
$rc = Invoke-PythonLogged $Python @($RealGuard, '--snapshot', $RealBefore, '--root', $ProtectedDataHome) $Root (Join-Path $LogDir 'dati_reali_prima.out.txt') (Join-Path $LogDir 'dati_reali_prima.err.txt')
if ($rc -ne 0) {
  Show-Tail (Join-Path $LogDir 'dati_reali_prima.err.txt') 12
  Write-Host 'Impronta dei dati reali non registrata: nessuna suite eseguita.' -ForegroundColor Red
  exit 2
}
Get-Content -LiteralPath (Join-Path $LogDir 'dati_reali_prima.out.txt') | ForEach-Object { Write-Host "  $_" -ForegroundColor Green }

$BrowserSuites = @('regressione_browser_save_4e3.py','regressione_browser_export_4f1.py','regressione_browser_impact_center.py')
$Suites = @(Get-ChildItem -LiteralPath $Root -File -Filter 'regressione_*.py' | Sort-Object Name)
if (-not $IncludeBrowser) { $Suites = @($Suites | Where-Object { $BrowserSuites -notcontains $_.Name }) }
if ($Filter -ne '') { $Suites = @($Suites | Where-Object { $_.Name -like "*$Filter*" }) }
if ($Suites.Count -eq 0) { Write-Host 'Nessuna suite selezionata.' -ForegroundColor Yellow; exit 1 }

Write-Host "==> $($Suites.Count) suite" -ForegroundColor Cyan
$Failed = @()
$Started = Get-Date
# Suites run with TEMP/TMP pointed at $RunTemp. Many suites, and some
# product code they exercise, create temporary directories and never
# remove them; before this, every green sweep left dozens behind in the
# user's real %TEMP%. Redirecting keeps the real %TEMP% clean whatever
# a suite does, and the count below keeps any such leak visible.
$SavedTemp = $env:TEMP
$SavedTmp = $env:TMP
$env:TEMP = $RunTemp
$env:TMP = $RunTemp
foreach ($suite in $Suites) {
  $t0 = Get-Date
  $SuiteOut = Join-Path $LogDir ($suite.BaseName + '.out.txt')
  $SuiteErr = Join-Path $LogDir ($suite.BaseName + '.err.txt')
  $rc = Invoke-PythonLogged $Python @($suite.FullName) $Root $SuiteOut $SuiteErr
  $secs = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
  if ($rc -eq 0) {
    Write-Host ("  OK   {0,-52} {1,6}s" -f $suite.Name, $secs) -ForegroundColor DarkGray
  } else {
    Write-Host ("  FAIL {0,-52} {1,6}s  exit={2}" -f $suite.Name, $secs, $rc) -ForegroundColor Red
    Show-Tail $SuiteErr
    $Failed += $suite.Name
  }
}

$env:TEMP = $SavedTemp
$env:TMP = $SavedTmp
$Elapsed = [math]::Round(((Get-Date) - $Started).TotalSeconds, 1)

Write-Host ''
Write-Host '==> Controllo dei dati reali dopo la sweep' -ForegroundColor Cyan
$RealOut = Join-Path $LogDir 'dati_reali_dopo.out.txt'
$rc = Invoke-PythonLogged $Python @($RealGuard, '--verify', $RealBefore, '--root', $ProtectedDataHome, '--report', $RealReport) $Root $RealOut (Join-Path $LogDir 'dati_reali_dopo.err.txt')
$RealDataChanged = ($rc -ne 0)
$RealColor = 'Green'
if ($RealDataChanged) { $RealColor = 'Red' }
if (Test-Path -LiteralPath $RealOut) { Get-Content -LiteralPath $RealOut | ForEach-Object { Write-Host "  $_" -ForegroundColor $RealColor } }
if ($RealDataChanged -and -not (Test-Path -LiteralPath $RealReport)) { Show-Tail (Join-Path $LogDir 'dati_reali_dopo.err.txt') 12 }
$Leftovers = @(Get-ChildItem -LiteralPath $RunTemp -Force -ErrorAction SilentlyContinue)
if ($Leftovers.Count -gt 0) {
  $Prefixes = $Leftovers | Group-Object { ($_.Name -replace '[A-Za-z0-9]{6,}$', '').TrimEnd('_', '-') } |
    Sort-Object Count -Descending | Select-Object -First 5 | ForEach-Object { "$($_.Name) x$($_.Count)" }
  Write-Host "Temporanei lasciati dalle suite (contenuti nella cartella di esecuzione): $($Leftovers.Count) - $($Prefixes -join ', ')" -ForegroundColor DarkYellow
}
Write-Host ''
if ($RealDataChanged) {
  Write-Host "SWEEP FALLITA: I DATI REALI SONO STATI MODIFICATI ($ProtectedDataHome)" -ForegroundColor Red
  Write-Host "Suite fallite: $($Failed.Count)/$($Suites.Count). Report dati reali: $RealReport" -ForegroundColor Red
  $Failed | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
  Write-Host "Log e temporanei conservati per la diagnosi: $RunDir" -ForegroundColor DarkGray
  exit 1
}
if ($Failed.Count -eq 0) {
  Write-Host "SWEEP VERDE: $($Suites.Count)/$($Suites.Count) in ${Elapsed}s -- dati reali invariati" -ForegroundColor Green
  Remove-Item -LiteralPath $RunDir -Recurse -Force -ErrorAction SilentlyContinue
  if (Test-Path -LiteralPath $RunDir) { Write-Host "Attenzione: cartella di esecuzione non rimossa: $RunDir" -ForegroundColor Yellow }
  exit 0
}
Write-Host "SWEEP FALLITA: $($Failed.Count)/$($Suites.Count) in ${Elapsed}s" -ForegroundColor Red
$Failed | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
Write-Host "Log e temporanei conservati per la diagnosi: $RunDir" -ForegroundColor DarkGray
exit 1
