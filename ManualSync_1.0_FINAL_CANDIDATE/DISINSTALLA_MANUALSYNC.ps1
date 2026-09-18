# -SandboxRoot: SOLO per la verifica automatica dell'installazione.
# Senza parametro -- che e' come lo invocano i .cmd consegnati al cliente --
# il comportamento e' identico a prima: profilo utente reale, Desktop reale,
# Start Menu reale. Con il parametro, installazione, dati, collegamenti e
# Path.home() dei processi figli finiscono sotto quella cartella, cosi' il
# ciclo installa -> verifica -> aggiorna -> disinstalla si puo' eseguire su
# una macchina di lavoro senza toccarne il Desktop ne' un'installazione vera.
param([string]$SandboxRoot = '')

$ErrorActionPreference = 'Stop'
if ($SandboxRoot) {
  $SandboxRoot = [IO.Path]::GetFullPath($SandboxRoot)
  $LocalAppDataRoot = Join-Path $SandboxRoot 'LocalAppData'
  $ProfileRoot = Join-Path $SandboxRoot 'UserProfile'
  $RoamingRoot = Join-Path $SandboxRoot 'AppData'
  $DesktopRoot = Join-Path $SandboxRoot 'Desktop'
  foreach ($d in @($LocalAppDataRoot, $ProfileRoot, $RoamingRoot, $DesktopRoot)) {
    New-Item -ItemType Directory -Path $d -Force | Out-Null
  }
  # Il ManualSync.exe installato risolve Path.home() da USERPROFILE: senza
  # questa riga log e dati dei test finirebbero nel profilo reale.
  $env:USERPROFILE = $ProfileRoot
} else {
  $LocalAppDataRoot = $env:LOCALAPPDATA
  $ProfileRoot = $env:USERPROFILE
  $RoamingRoot = $env:APPDATA
  $DesktopRoot = [Environment]::GetFolderPath('Desktop')
}
$InstallDir = Join-Path $LocalAppDataRoot 'Programs\ManualSync'
$DataDir = Join-Path $ProfileRoot 'ManualSyncData'
$DesktopShortcut = Join-Path $DesktopRoot 'ManualSync.lnk'
$StartMenuDir = Join-Path $RoamingRoot 'Microsoft\Windows\Start Menu\Programs\ManualSync'

$running = @()
try { $running += @(Get-Process -Name 'ManualSync' -ErrorAction SilentlyContinue) } catch {}
try {
  $running += @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue | Where-Object {
    ($_.CommandLine -as [string]) -match '(?i)ManualSync.*manualsync_desktop\.py'
  })
} catch {}
if ($running.Count -gt 0) {
  Write-Host 'Chiudi ManualSync prima della disinstallazione.' -ForegroundColor Red
  exit 2
}
Remove-Item -LiteralPath $DesktopShortcut -Force -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $StartMenuDir -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue
# Prima il messaggio di successo era stampato comunque, con exit 0, anche
# quando un file bloccato impediva la rimozione: SilentlyContinue sopra
# nasconde l'errore, quindi l'esito va accertato qui.
$Leftover = @($InstallDir, $DesktopShortcut, $StartMenuDir) | Where-Object { Test-Path -LiteralPath $_ }
if ($Leftover.Count -gt 0) {
  Write-Host 'Disinstallazione incompleta. Elementi non rimossi:' -ForegroundColor Red
  $Leftover | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
  Write-Host 'Chiudere eventuali finestre o programmi che usano questi file e riprovare.' -ForegroundColor Yellow
  exit 1
}
Write-Host 'ManualSync disinstallato.' -ForegroundColor Green
Write-Host "I dati progetto NON sono stati eliminati: $DataDir" -ForegroundColor Green
exit 0
