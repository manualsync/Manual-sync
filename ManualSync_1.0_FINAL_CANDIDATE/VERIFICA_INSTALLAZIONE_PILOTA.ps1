$ErrorActionPreference = 'Stop'
$InstallDir = Join-Path $env:LOCALAPPDATA 'Programs\ManualSync'
$Exe = Join-Path $InstallDir 'ManualSync.exe'
$DataDir = Join-Path $env:USERPROFILE 'ManualSyncData'
$LogDir = Join-Path $DataDir 'Logs'
$DesktopShortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) 'ManualSync.lnk'
$StartMenuShortcut = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\ManualSync\ManualSync.lnk'
$InstallMeta = Join-Path $InstallDir 'INSTALLATION.json'
$PayloadManifest = Join-Path $InstallDir 'PAYLOAD_MANIFEST_SHA256.json'
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$Results = @()

function Add-Check([string]$Name,[bool]$Ok,[string]$Detail='') {
  $script:Results += [pscustomobject]@{ check=$Name; status=$(if($Ok){'PASS'}else{'FAIL'}); detail=$Detail }
  if ($Ok) { Write-Host "PASS | $Name | $Detail" -ForegroundColor Green }
  else { Write-Host "FAIL | $Name | $Detail" -ForegroundColor Red }
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
      if (!(Test-Path $path)) { $ok=$false; $bad += "manca $rel"; continue }
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
    Add-Check 'Build installata corretta' ([string]$meta.build -eq '1.0.0-final-candidate-20260822') ([string]$meta.build)
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
$Payload = @{ build='1.0.0-final-candidate-20260822'; checked_at=(Get-Date).ToString('o'); pass=@($Results | Where-Object {$_.status -eq 'PASS'}).Count; fail=$Fail; results=$Results } | ConvertTo-Json -Depth 5
$Out = Join-Path $LogDir 'ManualSync_Pilot_Installation_Validation.json'
$Payload | Set-Content -LiteralPath $Out -Encoding UTF8
Write-Host "Report: $Out" -ForegroundColor DarkGray
if ($Fail -gt 0) { exit 1 }
exit 0
