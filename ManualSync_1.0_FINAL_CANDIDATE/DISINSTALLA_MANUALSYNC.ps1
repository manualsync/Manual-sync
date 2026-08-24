$ErrorActionPreference = 'Stop'
$InstallDir = Join-Path $env:LOCALAPPDATA 'Programs\ManualSync'
$DataDir = Join-Path $env:USERPROFILE 'ManualSyncData'
$DesktopShortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) 'ManualSync.lnk'
$StartMenuDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\ManualSync'

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
Write-Host 'ManualSync disinstallato.' -ForegroundColor Green
Write-Host "I dati progetto NON sono stati eliminati: $DataDir" -ForegroundColor Green
exit 0
