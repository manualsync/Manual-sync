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
$Exe = Join-Path $InstallDir 'ManualSync.exe'
$DataDir = Join-Path $ProfileRoot 'ManualSyncData'
$LogDir = Join-Path $DataDir 'Logs'
$DesktopShortcut = Join-Path $DesktopRoot 'ManualSync.lnk'
$StartMenuShortcut = Join-Path $RoamingRoot 'Microsoft\Windows\Start Menu\Programs\ManualSync\ManualSync.lnk'
$InstallMeta = Join-Path $InstallDir 'INSTALLATION.json'
$PayloadManifest = Join-Path $InstallDir 'PAYLOAD_MANIFEST_SHA256.json'
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$Results = @()

function Add-Check([string]$Name,[bool]$Ok,[string]$Detail='') {
  $script:Results += [pscustomobject]@{ check=$Name; status=$(if($Ok){'PASS'}else{'FAIL'}); detail=$Detail }
  if ($Ok) { Write-Host "PASS | $Name | $Detail" -ForegroundColor Green }
  else { Write-Host "FAIL | $Name | $Detail" -ForegroundColor Red }
}

# Build id: letto dal payload, mai riscritto qui. Vedi product_version.py.
function Get-ProductBuildId([string]$AppDir) {
  $file = Join-Path $AppDir 'product_version.py'
  if (!(Test-Path $file)) { throw "product_version.py assente in $AppDir" }
  $m = [regex]::Match((Get-Content -LiteralPath $file -Raw), "PRODUCT_BUILD\s*=\s*[`"']([^`"']+)[`"']")
  if (!$m.Success) { throw "PRODUCT_BUILD non leggibile da $file" }
  return $m.Groups[1].Value
}

function Check-Shortcut([string]$Name,[string]$Path) {
  if (!(Test-Path $Path)) {
    Add-Check $Name $false "assente: $Path"
    return
  }
  try {
    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut($Path)
    $targetOk = ([IO.Path]::GetFullPath($Shortcut.TargetPath) -ieq [IO.Path]::GetFullPath($Exe))
    $workOk = ([IO.Path]::GetFullPath($Shortcut.WorkingDirectory) -ieq [IO.Path]::GetFullPath($InstallDir))
    $argsOk = [string]::IsNullOrWhiteSpace([string]$Shortcut.Arguments)
    $ok = $targetOk -and $workOk -and $argsOk
    $detail = "target=$($Shortcut.TargetPath); args=$($Shortcut.Arguments); cwd=$($Shortcut.WorkingDirectory)"
    Add-Check $Name $ok $detail
  } catch {
    Add-Check $Name $false $_.Exception.Message
  }
}

# -LiteralPath: i percorsi vengono dal manifest, cioe' dai nomi dei file dei
# pacchetti installati. python-docx contiene [Content_Types].xml, e per
# Test-Path senza -LiteralPath le parentesi quadre sono caratteri jolly:
# il file c'e' ma risulta assente. Bloccava la build e avrebbe bloccato
# l'installazione sulla macchina del cliente.
function Check-PayloadManifest {
  if (!(Test-Path $PayloadManifest)) {
    Add-Check 'Manifest integrita installazione' $false "assente: $PayloadManifest"
    return
  }
  try {
    $manifest = Get-Content -LiteralPath $PayloadManifest -Raw | ConvertFrom-Json
    $ok = $true
    $bad = @()
    foreach ($item in @($manifest.files)) {
      $rel = ([string]$item.path).Replace('/','\')
      $path = Join-Path $InstallDir $rel
      if (!(Test-Path -LiteralPath $path)) { $ok=$false; $bad += "manca $rel"; continue }
      if ((Get-Item -LiteralPath $path).Length -ne [int64]$item.bytes) { $ok=$false; $bad += "size $rel"; continue }
      $hash=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
      if ($hash -ne ([string]$item.sha256).ToLowerInvariant()) { $ok=$false; $bad += "hash $rel" }
    }
    $detail = if ($ok) { "$(@($manifest.files).Count) file verificati" } else { $bad -join '; ' }
    Add-Check 'Manifest integrita installazione' $ok $detail
  } catch {
    Add-Check 'Manifest integrita installazione' $false $_.Exception.Message
  }
}


Add-Check 'ManualSync.exe installato' (Test-Path $Exe) $Exe
if (Test-Path $Exe) {
  $r = Start-Process -FilePath $Exe -ArgumentList '--resource-check' -PassThru -Wait
  Add-Check 'Resource check installato' ($r.ExitCode -eq 0) "exit=$($r.ExitCode)"
  $s = Start-Process -FilePath $Exe -ArgumentList '--headless-smoke' -PassThru -Wait
  Add-Check 'Smoke test installato' ($s.ExitCode -eq 0) "exit=$($s.ExitCode)"
  $a = Start-Process -FilePath $Exe -ArgumentList '--acceptance-smoke' -PassThru -Wait
  Add-Check 'Commercial acceptance installato' ($a.ExitCode -eq 0) "exit=$($a.ExitCode)"
}
Check-PayloadManifest
Check-Shortcut 'Shortcut Desktop valido' $DesktopShortcut
Check-Shortcut 'Shortcut Start Menu valido' $StartMenuShortcut
Add-Check 'Metadati installazione presenti' (Test-Path $InstallMeta) $InstallMeta
if (Test-Path $InstallMeta) {
  try {
    $meta = Get-Content -LiteralPath $InstallMeta -Raw | ConvertFrom-Json
    # Confronto fra cio che l'installer ha registrato e cio che il payload
    # installato dichiara: due letterali uguali non avrebbero provato nulla.
    $ExpectedBuild = Get-ProductBuildId (Join-Path $InstallDir 'app')
    Add-Check 'Build installata corretta' ([string]$meta.build -eq $ExpectedBuild) "installato=$([string]$meta.build); payload=$ExpectedBuild"
  } catch { Add-Check 'Build installata corretta' $false $_.Exception.Message }
}
try {
  New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
  $Probe = Join-Path $DataDir '.pilot_write_probe'
  'ok' | Set-Content -LiteralPath $Probe -Encoding UTF8
  Remove-Item -LiteralPath $Probe -Force
  Add-Check 'ManualSyncData scrivibile' $true $DataDir
} catch { Add-Check 'ManualSyncData scrivibile' $false $_.Exception.Message }

$Fail = @($Results | Where-Object { $_.status -eq 'FAIL' }).Count
$Payload = @{ build=(Get-ProductBuildId (Join-Path $InstallDir 'app')); checked_at=(Get-Date).ToString('o'); pass=@($Results | Where-Object {$_.status -eq 'PASS'}).Count; fail=$Fail; results=$Results } | ConvertTo-Json -Depth 5
$Out = Join-Path $LogDir 'ManualSync_Pilot_Installation_Validation.json'
$Payload | Set-Content -LiteralPath $Out -Encoding UTF8
Write-Host "Report: $Out" -ForegroundColor DarkGray
if ($Fail -gt 0) { exit 1 }
exit 0
