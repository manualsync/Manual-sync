# -SandboxRoot: SOLO per la verifica automatica dell'installazione.
# Senza parametro -- che e' come lo invocano i .cmd consegnati al cliente --
# il comportamento e' identico a prima: profilo utente reale, Desktop reale,
# Start Menu reale. Con il parametro, installazione, dati, collegamenti e
# Path.home() dei processi figli finiscono sotto quella cartella, cosi' il
# ciclo installa -> verifica -> aggiorna -> disinstalla si puo' eseguire su
# una macchina di lavoro senza toccarne il Desktop ne' un'installazione vera.
param([string]$SandboxRoot = '')

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Payload = Join-Path $Root 'payload'
$PayloadExe = Join-Path $Payload 'ManualSync.exe'
$PayloadManifest = Join-Path $Root 'PAYLOAD_MANIFEST_SHA256.json'
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
$Programs = Join-Path $LocalAppDataRoot 'Programs'
$InstallDir = Join-Path $Programs 'ManualSync'
$DataDir = Join-Path $ProfileRoot 'ManualSyncData'
$DesktopShortcut = Join-Path $DesktopRoot 'ManualSync.lnk'
$StartMenuDir = Join-Path $RoamingRoot 'Microsoft\Windows\Start Menu\Programs\ManualSync'
$StartMenuShortcut = Join-Path $StartMenuDir 'ManualSync.lnk'
$InstallLogDir = Join-Path $DataDir 'Logs'
$InstallLog = Join-Path $InstallLogDir 'ManualSync_Installer.log'

New-Item -ItemType Directory -Path $InstallLogDir -Force | Out-Null
Start-Transcript -Path $InstallLog -Append | Out-Null
$Stage = $null
$Previous = $null
$HadPreviousInstall = $false
$ShortcutsWritten = $false

# Build id: letto dal payload, mai riscritto qui. Vedi product_version.py.
function Get-ProductBuildId([string]$AppDir) {
  $file = Join-Path $AppDir 'product_version.py'
  if (!(Test-Path $file)) { throw "product_version.py assente in $AppDir" }
  $m = [regex]::Match((Get-Content -LiteralPath $file -Raw), "PRODUCT_BUILD\s*=\s*[`"']([^`"']+)[`"']")
  if (!$m.Success) { throw "PRODUCT_BUILD non leggibile da $file" }
  return $m.Groups[1].Value
}

function Fail([string]$Message) {
  Write-Host "ERRORE: $Message" -ForegroundColor Red
  throw $Message
}

function Get-ManualSyncRuntimeProcesses {
  $items = @()
  try {
    $items += @(Get-Process -Name 'ManualSync' -ErrorAction SilentlyContinue)
  } catch {}
  # Compatibility with RC1 and older: their tiny launcher exited immediately,
  # leaving only pythonw.exe alive for the desktop session.
  try {
    $legacy = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue | Where-Object {
      ($_.CommandLine -as [string]) -match '(?i)ManualSync.*manualsync_desktop\.py'
    }
    $items += @($legacy)
  } catch {}
  return @($items)
}

function New-CleanShortcut([string]$Path, [string]$Target, [string]$WorkingDir, [string]$Icon) {
  # CreateShortcut() opens an existing .lnk and preserves fields not overwritten.
  # RC1 therefore retained the old RC1.9 Arguments value. Always rebuild the .lnk.
  Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
  $Shell = New-Object -ComObject WScript.Shell
  $Shortcut = $Shell.CreateShortcut($Path)
  $Shortcut.TargetPath = $Target
  $Shortcut.Arguments = ''
  $Shortcut.WorkingDirectory = $WorkingDir
  $Shortcut.IconLocation = $Icon
  $Shortcut.Description = 'ManualSync'
  $Shortcut.WindowStyle = 1
  $Shortcut.Save()
}

function Assert-Shortcut([string]$Path, [string]$ExpectedTarget, [string]$ExpectedWorkingDir) {
  if (!(Test-Path $Path)) { Fail "Collegamento non creato: $Path" }
  $Shell = New-Object -ComObject WScript.Shell
  $Shortcut = $Shell.CreateShortcut($Path)
  $targetOk = ([IO.Path]::GetFullPath($Shortcut.TargetPath) -ieq [IO.Path]::GetFullPath($ExpectedTarget))
  $workOk = ([IO.Path]::GetFullPath($Shortcut.WorkingDirectory) -ieq [IO.Path]::GetFullPath($ExpectedWorkingDir))
  $argsOk = [string]::IsNullOrWhiteSpace([string]$Shortcut.Arguments)
  if (!$targetOk) { Fail "Target collegamento non valido: $Path -> $($Shortcut.TargetPath)" }
  if (!$workOk) { Fail "Working directory collegamento non valida: $Path -> $($Shortcut.WorkingDirectory)" }
  if (!$argsOk) { Fail "Argomenti residui nel collegamento: $Path -> $($Shortcut.Arguments)" }
}

# -LiteralPath: i percorsi vengono dal manifest, cioe' dai nomi dei file dei
# pacchetti installati. python-docx contiene [Content_Types].xml, e per
# Test-Path senza -LiteralPath le parentesi quadre sono caratteri jolly:
# il file c'e' ma risulta assente. Bloccava la build e avrebbe bloccato
# l'installazione sulla macchina del cliente.
function Assert-PayloadManifest([string]$BaseDir, [string]$ManifestPath) {
  if (!(Test-Path $ManifestPath)) { Fail "Manifest integrita payload mancante: $ManifestPath" }
  $manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
  $expected = @($manifest.files)
  if ($expected.Count -lt 10) { Fail "Manifest payload non valido: $($expected.Count) file." }
  $actual = @(Get-ChildItem -LiteralPath $BaseDir -File -Recurse -Force)
  if ($actual.Count -ne [int]$manifest.file_count) { Fail "Payload incompleto: $($actual.Count) file presenti, $($manifest.file_count) attesi." }
  foreach ($item in $expected) {
    $rel = ([string]$item.path).Replace('/','\')
    $path = Join-Path $BaseDir $rel
    if (!(Test-Path -LiteralPath $path)) { Fail "Payload incompleto: manca $rel" }
    if ((Get-Item -LiteralPath $path).Length -ne [int64]$item.bytes) { Fail "Payload alterato: dimensione non valida per $rel" }
    $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -ne ([string]$item.sha256).ToLowerInvariant()) { Fail "Payload alterato: checksum non valido per $rel" }
  }
}


try {
  Write-Host 'ManualSync - Installazione / aggiornamento pilota' -ForegroundColor Cyan
  if ([Environment]::Is64BitOperatingSystem -ne $true) { Fail 'Windows x64 richiesto.' }
  if (!(Test-Path $PayloadExe)) { Fail "Pacchetto incompleto: manca $PayloadExe" }
  Write-Host '0/5 Verifica integrita pacchetto...' -ForegroundColor Cyan
  Assert-PayloadManifest $Payload $PayloadManifest

  $running = @(Get-ManualSyncRuntimeProcesses)
  if ($running.Count -gt 0) { Fail 'Chiudi ManualSync prima di installare o aggiornare.' }

  New-Item -ItemType Directory -Path $Programs -Force | Out-Null
  $Stage = Join-Path $Programs ('.ManualSync.install.' + [guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $Stage -Force | Out-Null

  Write-Host '1/5 Copia payload in staging...' -ForegroundColor Cyan
  $Robo = Start-Process -FilePath 'robocopy.exe' -ArgumentList @("`"$Payload`"","`"$Stage`"",'/E','/NFL','/NDL','/NJH','/NJS','/NP') -PassThru -Wait -NoNewWindow
  if ($Robo.ExitCode -ge 8) { Fail "Copia payload fallita (robocopy $($Robo.ExitCode))." }

  $StageExe = Join-Path $Stage 'ManualSync.exe'
  if (!(Test-Path $StageExe)) { Fail 'Staging incompleto: ManualSync.exe assente.' }
  Assert-PayloadManifest $Stage $PayloadManifest

  Write-Host '2/5 Verifica tecnica pre-installazione...' -ForegroundColor Cyan
  $Resource = Start-Process -FilePath $StageExe -ArgumentList '--resource-check' -PassThru -Wait
  if ($Resource.ExitCode -ne 0) { Fail "Resource check fallito (exit $($Resource.ExitCode))." }
  $Smoke = Start-Process -FilePath $StageExe -ArgumentList '--headless-smoke' -PassThru -Wait
  if ($Smoke.ExitCode -ne 0) { Fail "Smoke test fallito (exit $($Smoke.ExitCode))." }

  if (Test-Path $InstallDir) {
    $HadPreviousInstall = $true
    $Previous = Join-Path $Programs ('.ManualSync.previous.' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
    Write-Host '3/5 Prepara rollback della versione precedente...' -ForegroundColor Cyan
    Move-Item -LiteralPath $InstallDir -Destination $Previous
  } else {
    Write-Host '3/5 Prima installazione...' -ForegroundColor Cyan
  }

  try {
    Move-Item -LiteralPath $Stage -Destination $InstallDir
    $InstalledExe = Join-Path $InstallDir 'ManualSync.exe'
    if (!(Test-Path $InstalledExe)) { Fail 'Installazione incompleta dopo lo swap atomico.' }
    # Verify that the exact tree installed by the atomic swap still matches the
    # signed payload before running any application code or writing metadata.
    Assert-PayloadManifest $InstallDir $PayloadManifest

    Write-Host '4/5 Verifica installazione finale, acceptance e collegamenti...' -ForegroundColor Cyan
    $FinalResource = Start-Process -FilePath $InstalledExe -ArgumentList '--resource-check' -PassThru -Wait
    if ($FinalResource.ExitCode -ne 0) { Fail "Resource check installazione finale fallito (exit $($FinalResource.ExitCode))." }
    $FinalSmoke = Start-Process -FilePath $InstalledExe -ArgumentList '--headless-smoke' -PassThru -Wait
    if ($FinalSmoke.ExitCode -ne 0) { Fail "Smoke test installazione finale fallito (exit $($FinalSmoke.ExitCode))." }
    $FinalAcceptance = Start-Process -FilePath $InstalledExe -ArgumentList '--acceptance-smoke' -PassThru -Wait
    if ($FinalAcceptance.ExitCode -ne 0) { Fail "Commercial acceptance installazione finale fallita (exit $($FinalAcceptance.ExitCode))." }
    Copy-Item -LiteralPath $PayloadManifest -Destination (Join-Path $InstallDir 'PAYLOAD_MANIFEST_SHA256.json') -Force

    New-Item -ItemType Directory -Path $StartMenuDir -Force | Out-Null
    $ShortcutsWritten = $true
    New-CleanShortcut $DesktopShortcut $InstalledExe $InstallDir $InstalledExe
    New-CleanShortcut $StartMenuShortcut $InstalledExe $InstallDir $InstalledExe
    Assert-Shortcut $DesktopShortcut $InstalledExe $InstallDir
    Assert-Shortcut $StartMenuShortcut $InstalledExe $InstallDir

    $Meta = @{
      installed_at = (Get-Date).ToString('o')
      build = (Get-ProductBuildId (Join-Path $InstallDir 'app'))
      install_dir = $InstallDir
      data_dir = $DataDir
      version_file = (Join-Path $InstallDir 'app\VERSION.txt')
      shortcut_desktop = $DesktopShortcut
      shortcut_start_menu = $StartMenuShortcut
    } | ConvertTo-Json -Depth 3
    $Meta | Set-Content -LiteralPath (Join-Path $InstallDir 'INSTALLATION.json') -Encoding UTF8

    if ($Previous -and (Test-Path $Previous)) { Remove-Item -Recurse -Force $Previous }
    Write-Host '5/5 Installazione completata.' -ForegroundColor Green
    Write-Host "Applicazione: $InstalledExe" -ForegroundColor Green
    Write-Host "Dati progetto preservati in: $DataDir" -ForegroundColor Green
  }
  catch {
    Write-Host 'Installazione non completata: avvio rollback...' -ForegroundColor Yellow
    if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue }
    if ($Previous -and (Test-Path $Previous)) { Move-Item -LiteralPath $Previous -Destination $InstallDir }
    if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage -ErrorAction SilentlyContinue }
    if ($HadPreviousInstall -and (Test-Path (Join-Path $InstallDir 'ManualSync.exe'))) {
      # The previous version is back at the same stable install path. Recreate
      # clean shortcuts so rollback also repairs any legacy RC1.9 arguments.
      $PreviousExe = Join-Path $InstallDir 'ManualSync.exe'
      New-Item -ItemType Directory -Path $StartMenuDir -Force | Out-Null
      New-CleanShortcut $DesktopShortcut $PreviousExe $InstallDir $PreviousExe
      New-CleanShortcut $StartMenuShortcut $PreviousExe $InstallDir $PreviousExe
    } elseif (!$HadPreviousInstall) {
      Remove-Item -LiteralPath $DesktopShortcut -Force -ErrorAction SilentlyContinue
      Remove-Item -Recurse -Force $StartMenuDir -ErrorAction SilentlyContinue
    }
    throw
  }
}
catch {
  if ($Stage -and (Test-Path $Stage)) { Remove-Item -Recurse -Force $Stage -ErrorAction SilentlyContinue }
  Write-Host ''
  Write-Host 'INSTALLAZIONE FALLITA. La versione precedente e i dati progetto sono stati preservati.' -ForegroundColor Red
  Write-Host "Dettagli: $InstallLog" -ForegroundColor DarkGray
  exit 1
}
finally {
  try { Stop-Transcript | Out-Null } catch {}
}
exit 0
