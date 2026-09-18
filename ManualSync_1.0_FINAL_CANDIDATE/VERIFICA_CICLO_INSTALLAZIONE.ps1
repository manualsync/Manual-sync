# VERIFICA_CICLO_INSTALLAZIONE.ps1 - ciclo di vita completo dell'installer,
# eseguito in una sandbox sullo ZIP cliente esatto prodotto da BUILD_WINDOWS.ps1.
#
# Perche' esiste
# --------------
# La build verifica il PAYLOAD (resource check, smoke, acceptance sullo ZIP
# ri-estratto). Non verifica l'INSTALLAZIONE: swap atomico, collegamenti,
# metadati, aggiornamento sopra una versione esistente, rollback dopo un
# guasto, disinstallazione, preservazione dei dati. Quelle parti si
# potevano provare solo installando sul profilo reale di qualcuno.
#
# Con -SandboxRoot gli script cliente installano sotto una cartella
# temporanea. Questo script li esegue in sequenza e verifica ogni passo,
# e alla fine controlla che il Desktop reale e l'eventuale installazione
# reale non siano stati toccati.
#
# NON e' distribuito al cliente: la build copia nello stage installer un
# elenco esplicito di file e questo non ne fa parte.
#
# Uso:
#   .\VERIFICA_CICLO_INSTALLAZIONE.ps1
#   .\VERIFICA_CICLO_INSTALLAZIONE.ps1 -SetupZip <percorso ZIP> -Keep

[CmdletBinding()]
param(
  [string]$SetupZip = '',
  [switch]$Keep
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $SetupZip) {
  # Cercato, non ripetuto: il nome dello ZIP e' deciso da BUILD_WINDOWS.ps1.
  # Una seconda copia qui si sarebbe rotta al primo cambio di etichetta.
  $Candidates = @(Get-ChildItem -LiteralPath (Join-Path $Root 'release') -File -Filter 'ManualSync_Setup_*.zip' -ErrorAction SilentlyContinue)
  if ($Candidates.Count -ne 1) {
    Write-Host "Atteso esattamente uno ZIP di setup in release\, trovati $($Candidates.Count). Usare -SetupZip." -ForegroundColor Red
    exit 2
  }
  $SetupZip = $Candidates[0].FullName
}
if (!(Test-Path $SetupZip)) {
  Write-Host "ZIP cliente non trovato: $SetupZip -- eseguire prima BUILD_WINDOWS.ps1" -ForegroundColor Red
  exit 2
}

$Results = @()
function Add-Check([string]$Name, [bool]$Ok, [string]$Detail = '') {
  $script:Results += [pscustomobject]@{ check = $Name; status = $(if ($Ok) { 'PASS' } else { 'FAIL' }); detail = $Detail }
  if ($Ok) { Write-Host "PASS | $Name | $Detail" -ForegroundColor Green }
  else { Write-Host "FAIL | $Name | $Detail" -ForegroundColor Red }
}

# Invoca uno script cliente in un processo PowerShell separato, come fa il
# .cmd, cosi' la modifica di USERPROFILE resta confinata a quel processo.
function Invoke-Client([string]$Dir, [string]$Script, [string]$Sandbox) {
  $p = Start-Process -FilePath 'powershell.exe' -ArgumentList @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$(Join-Path $Dir $Script)`"",
    '-SandboxRoot', "`"$Sandbox`"") -PassThru -Wait -NoNewWindow
  return $p.ExitCode
}

# --- Stato reale da preservare -------------------------------------------
$RealDesktopLnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'ManualSync.lnk'
$RealInstallDir = Join-Path $env:LOCALAPPDATA 'Programs\ManualSync'
$RealStartMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\ManualSync'
function Get-Fingerprint([string]$Path) {
  if (!(Test-Path $Path)) { return 'assente' }
  $i = Get-Item -LiteralPath $Path -Force
  return "presente;$($i.LastWriteTimeUtc.Ticks)"
}
$RealBefore = @{
  desktop = Get-Fingerprint $RealDesktopLnk
  install = Get-Fingerprint $RealInstallDir
  startmenu = Get-Fingerprint $RealStartMenu
}

$Work = Join-Path ([IO.Path]::GetTempPath()) ('ms_install_cycle_' + [guid]::NewGuid().ToString('N').Substring(0, 8))
$Extract = Join-Path $Work 'setup'
$Sandbox = Join-Path $Work 'sandbox'
New-Item -ItemType Directory -Path $Extract, $Sandbox -Force | Out-Null
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::ExtractToDirectory($SetupZip, $Extract)
Write-Host "Sandbox: $Sandbox" -ForegroundColor DarkGray

$InstallDir = Join-Path $Sandbox 'LocalAppData\Programs\ManualSync'
$DataDir = Join-Path $Sandbox 'UserProfile\ManualSyncData'
$DesktopLnk = Join-Path $Sandbox 'Desktop\ManualSync.lnk'
$StartMenuLnk = Join-Path $Sandbox 'AppData\Microsoft\Windows\Start Menu\Programs\ManualSync\ManualSync.lnk'
$Programs = Split-Path -Parent $InstallDir

try {
  # --- 1. Prima installazione ----------------------------------------------
  Write-Host "`n==> 1. Prima installazione" -ForegroundColor Cyan
  $rc = Invoke-Client $Extract 'INSTALLA_MANUALSYNC.ps1' $Sandbox
  Add-Check 'Installazione: exit 0' ($rc -eq 0) "exit=$rc"
  Add-Check 'ManualSync.exe installato' (Test-Path (Join-Path $InstallDir 'ManualSync.exe')) $InstallDir
  Add-Check 'Collegamento Desktop creato' (Test-Path $DesktopLnk) $DesktopLnk
  Add-Check 'Collegamento Start Menu creato' (Test-Path $StartMenuLnk) $StartMenuLnk
  Add-Check 'Cataloghi di localizzazione installati' ((Test-Path (Join-Path $InstallDir 'app\locales\it.json')) -and (Test-Path (Join-Path $InstallDir 'app\locales\en.json'))) ''
  $MetaPath = Join-Path $InstallDir 'INSTALLATION.json'
  Add-Check 'Metadati installazione scritti' (Test-Path $MetaPath) $MetaPath

  # Dati "del cliente" che ogni passo successivo deve preservare.
  $Sentinel = Join-Path $DataDir 'Projects\ZZ_CLIENTE_SENTINELLA\dati.txt'
  New-Item -ItemType Directory -Path (Split-Path -Parent $Sentinel) -Force | Out-Null
  'dati del cliente' | Set-Content -LiteralPath $Sentinel -Encoding UTF8
  $SentinelHash = (Get-FileHash -LiteralPath $Sentinel -Algorithm SHA256).Hash

  # --- 2. Verifica pilota --------------------------------------------------
  Write-Host "`n==> 2. Verifica installazione pilota" -ForegroundColor Cyan
  $rc = Invoke-Client $Extract 'VERIFICA_INSTALLAZIONE_PILOTA.ps1' $Sandbox
  Add-Check 'Verifica pilota: exit 0' ($rc -eq 0) "exit=$rc"

  # --- 2b. Avvio applicazione installata, UI, progetto, chiusura -----------
  # Usa SOLO i file installati: il runtime Python portabile e app\. Il
  # server viene avviato come fa manualsync_desktop, ma senza aprire la
  # finestra pywebview (che comparirebbe sul desktop di chi esegue il test).
  # La UI e' letta dall'endpoint /config di Gradio: e' li' che si vede se
  # le etichette sono tradotte o se i cataloghi mancano e compaiono chiavi.
  Write-Host "`n==> 2b. Avvio applicazione installata, UI, progetto, chiusura" -ForegroundColor Cyan
  $Probe = Join-Path $Work 'probe_app_installata.py'
  $ProbeOut = Join-Path $Work 'probe_app_installata.json'
  @'
import json, os, sys, traceback, urllib.request
from pathlib import Path
app_dir, out = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(app_dir))
os.chdir(app_dir)
result = {"python": sys.executable}
try:
    import manualsync_desktop as md
    demo, url = md.start_server(md.choose_free_port())
    result["url"] = url
    result["http_ok"] = md.wait_http(url, timeout=90)
    with urllib.request.urlopen(url.rstrip("/") + "/config", timeout=30) as r:
        cfg = json.dumps(json.loads(r.read().decode("utf-8")), ensure_ascii=False)
    result["labels_it"] = [s for s in ("Analisi impatto", "Conformit\u00e0", "Contenuti", "Traduzioni") if s in cfg]
    result["raw_keys"] = [s for s in ("nav.impact", "nav.conformity", "page.impact.title",
                                      "impactcenter.picker.label", "context.operator") if s in cfg]
    import app
    result["app_file"] = app.__file__
    result["data_root"] = str(app.DATA_ROOT)
    created = app.create_project_ui("ZZ Ciclo Installazione", "ACME", "Pompa", "CICLO-001", "A",
                                    "it-IT", "en-US", "Verifica")
    project = Path(created[0])
    result["project_dir"] = str(project)
    result["project_ok"] = project.exists() and (project / "ManualSync_Project.json").exists()
    demo.close()
    result["closed"] = True
except BaseException:
    result["error"] = traceback.format_exc()
out.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
sys.exit(0 if "error" not in result else 1)
'@ | Set-Content -LiteralPath $Probe -Encoding UTF8
  $RuntimePython = Join-Path $InstallDir 'runtime\python.exe'
  $AppDir = Join-Path $InstallDir 'app'
  # Stesse variabili che manualsync_launcher.py imposta per il processo reale:
  # senza PYTHONDONTWRITEBYTECODE la sonda scriverebbe __pycache__ dentro
  # l'installazione e il test modificherebbe cio' che sta misurando.
  $Saved = @{ USERPROFILE = $env:USERPROFILE; PYTHONDONTWRITEBYTECODE = $env:PYTHONDONTWRITEBYTECODE; PYTHONUTF8 = $env:PYTHONUTF8 }
  try {
    $env:USERPROFILE = Join-Path $Sandbox 'UserProfile'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PYTHONUTF8 = '1'
    $pp = Start-Process -FilePath $RuntimePython -ArgumentList @("`"$Probe`"", "`"$AppDir`"", "`"$ProbeOut`"") `
          -WorkingDirectory $AppDir -NoNewWindow -Wait -PassThru `
          -RedirectStandardOutput (Join-Path $Work 'probe.out.txt') -RedirectStandardError (Join-Path $Work 'probe.err.txt')
  } finally {
    $env:USERPROFILE = $Saved.USERPROFILE
    $env:PYTHONDONTWRITEBYTECODE = $Saved.PYTHONDONTWRITEBYTECODE
    $env:PYTHONUTF8 = $Saved.PYTHONUTF8
  }
  $Pycache = @(Get-ChildItem -LiteralPath $AppDir -Directory -Recurse -Force -Filter '__pycache__' -ErrorAction SilentlyContinue)
  Add-Check 'App installata: la sonda non ha scritto bytecode nell''installazione' ($Pycache.Count -eq 0) "$($Pycache.Count) cartelle __pycache__"
  $Info = if (Test-Path $ProbeOut) { Get-Content -LiteralPath $ProbeOut -Raw -Encoding UTF8 | ConvertFrom-Json } else { $null }
  $ProbeError = if ($Info -and ($Info.PSObject.Properties.Name -contains 'error')) { [string]$Info.error } else { '' }
  Add-Check 'App installata: avvio senza errori' (($pp.ExitCode -eq 0) -and $Info -and -not $ProbeError) "exit=$($pp.ExitCode) $($ProbeError.Split([Environment]::NewLine) | Select-Object -Last 1)"
  if ($Info -and -not $ProbeError) {
    Add-Check 'App installata: eseguita dal runtime installato' ([string]$Info.python -like "$InstallDir*") ([string]$Info.python)
    Add-Check 'App installata: codice caricato da app\ installata' ([string]$Info.app_file -like "$AppDir*") ([string]$Info.app_file)
    Add-Check 'App installata: UI raggiungibile via HTTP' ([bool]$Info.http_ok) ([string]$Info.url)
    Add-Check 'App installata: UI con etichette italiane' (@($Info.labels_it).Count -eq 4) (@($Info.labels_it) -join ', ')
    Add-Check 'App installata: nessuna chiave di localizzazione a schermo' (@($Info.raw_keys).Count -eq 0) (@($Info.raw_keys) -join ', ')
    Add-Check 'App installata: dati nel profilo sandbox' ([string]$Info.data_root -like "$Sandbox*") ([string]$Info.data_root)
    Add-Check 'App installata: progetto creato' ([bool]$Info.project_ok) ([string]$Info.project_dir)
    Add-Check 'App installata: server chiuso' ([bool]$Info.closed) ''
    $ProjectDir = [string]$Info.project_dir
  } else { $ProjectDir = '' }
  $Alive = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { ([string]$_.ExecutablePath) -like "$InstallDir*" })
  Add-Check 'App installata: nessun processo residuo dopo la chiusura' ($Alive.Count -eq 0) "$($Alive.Count) processi"

  # --- 3. Aggiornamento sopra l'installazione esistente --------------------
  Write-Host "`n==> 3. Aggiornamento sopra un'installazione esistente" -ForegroundColor Cyan
  $FirstInstalledAt = (Get-Content -LiteralPath $MetaPath -Raw | ConvertFrom-Json).installed_at
  Start-Sleep -Seconds 2
  $rc = Invoke-Client $Extract 'INSTALLA_MANUALSYNC.ps1' $Sandbox
  Add-Check 'Aggiornamento: exit 0' ($rc -eq 0) "exit=$rc"
  $SecondInstalledAt = (Get-Content -LiteralPath $MetaPath -Raw | ConvertFrom-Json).installed_at
  Add-Check 'Aggiornamento: installazione effettivamente sostituita' ($SecondInstalledAt -ne $FirstInstalledAt) "$FirstInstalledAt -> $SecondInstalledAt"
  Add-Check 'Aggiornamento: nessuna copia precedente residua' (@(Get-ChildItem -LiteralPath $Programs -Directory -Force -Filter '.ManualSync.previous.*').Count -eq 0) ''
  Add-Check 'Aggiornamento: dati cliente preservati' ((Test-Path $Sentinel) -and ((Get-FileHash -LiteralPath $Sentinel -Algorithm SHA256).Hash -eq $SentinelHash)) $Sentinel

  # --- 4. Pacchetto alterato: rifiutato prima di toccare l'installazione ---
  Write-Host "`n==> 4. Pacchetto alterato" -ForegroundColor Cyan
  $Tampered = Join-Path $Work 'tampered'
  Copy-Item -Recurse -LiteralPath $Extract -Destination $Tampered
  Add-Content -LiteralPath (Join-Path $Tampered 'payload\app\VERSION.txt') -Value 'alterato'
  $rc = Invoke-Client $Tampered 'INSTALLA_MANUALSYNC.ps1' $Sandbox
  Add-Check 'Pacchetto alterato: installazione rifiutata' ($rc -ne 0) "exit=$rc"
  $AfterTamper = (Get-Content -LiteralPath $MetaPath -Raw | ConvertFrom-Json).installed_at
  Add-Check 'Pacchetto alterato: installazione esistente intatta' ($AfterTamper -eq $SecondInstalledAt) $AfterTamper

  # --- 5. Guasto DOPO lo swap: rollback reale ------------------------------
  # Una directory non vuota al posto del collegamento Desktop fa fallire la
  # creazione del collegamento, che avviene dopo lo swap atomico e dopo
  # l'acceptance. E' il percorso di rollback vero, non un rifiuto a monte.
  Write-Host "`n==> 5. Guasto dopo lo swap: rollback" -ForegroundColor Cyan
  Remove-Item -LiteralPath $DesktopLnk -Force
  New-Item -ItemType Directory -Path (Join-Path $DesktopLnk 'blocco') -Force | Out-Null
  $rc = Invoke-Client $Extract 'INSTALLA_MANUALSYNC.ps1' $Sandbox
  Add-Check 'Rollback: installazione segnalata come fallita' ($rc -ne 0) "exit=$rc"
  Add-Check 'Rollback: versione precedente ripristinata' ((Test-Path $MetaPath) -and ((Get-Content -LiteralPath $MetaPath -Raw | ConvertFrom-Json).installed_at -eq $SecondInstalledAt)) ''
  Add-Check 'Rollback: eseguibile precedente presente' (Test-Path (Join-Path $InstallDir 'ManualSync.exe')) ''
  Add-Check 'Rollback: nessuno staging residuo' (@(Get-ChildItem -LiteralPath $Programs -Directory -Force -Filter '.ManualSync.install.*').Count -eq 0) ''
  Add-Check 'Rollback: nessuna copia precedente residua' (@(Get-ChildItem -LiteralPath $Programs -Directory -Force -Filter '.ManualSync.previous.*').Count -eq 0) ''
  Add-Check 'Rollback: dati cliente preservati' ((Test-Path $Sentinel) -and ((Get-FileHash -LiteralPath $Sentinel -Algorithm SHA256).Hash -eq $SentinelHash)) ''
  Remove-Item -Recurse -Force -LiteralPath $DesktopLnk

  # --- 6. Disinstallazione -------------------------------------------------
  Write-Host "`n==> 6. Disinstallazione" -ForegroundColor Cyan
  $rc = Invoke-Client $Extract 'DISINSTALLA_MANUALSYNC.ps1' $Sandbox
  Add-Check 'Disinstallazione: exit 0' ($rc -eq 0) "exit=$rc"
  Add-Check 'Disinstallazione: applicazione rimossa' (!(Test-Path $InstallDir)) $InstallDir
  Add-Check 'Disinstallazione: collegamenti rimossi' ((!(Test-Path $DesktopLnk)) -and (!(Test-Path $StartMenuLnk))) ''
  Add-Check 'Disinstallazione: dati cliente preservati' ((Test-Path $Sentinel) -and ((Get-FileHash -LiteralPath $Sentinel -Algorithm SHA256).Hash -eq $SentinelHash)) $Sentinel
  Add-Check 'Disinstallazione: progetto creato dall''app preservato' (($ProjectDir -ne '') -and (Test-Path (Join-Path $ProjectDir 'ManualSync_Project.json'))) $ProjectDir
  $ProgramsLeft = @(Get-ChildItem -LiteralPath $Programs -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -like '*ManualSync*' })
  Add-Check 'Disinstallazione: nessun residuo in Programs' ($ProgramsLeft.Count -eq 0) (($ProgramsLeft | ForEach-Object Name) -join ', ')
  $StartMenuDirLeft = Split-Path -Parent $StartMenuLnk
  Add-Check 'Disinstallazione: cartella Start Menu rimossa' (!(Test-Path $StartMenuDirLeft)) $StartMenuDirLeft
}
catch {
  Add-Check 'Ciclo completato senza eccezioni' $false $_.Exception.Message
}
finally {
  # --- 7. Il sistema reale non e' stato toccato ----------------------------
  Write-Host "`n==> 7. Sistema reale" -ForegroundColor Cyan
  Add-Check 'Desktop reale non modificato' ((Get-Fingerprint $RealDesktopLnk) -eq $RealBefore.desktop) $RealBefore.desktop
  Add-Check 'Installazione reale non modificata' ((Get-Fingerprint $RealInstallDir) -eq $RealBefore.install) $RealBefore.install
  Add-Check 'Start Menu reale non modificato' ((Get-Fingerprint $RealStartMenu) -eq $RealBefore.startmenu) $RealBefore.startmenu
  if (-not $Keep) { Remove-Item -Recurse -Force -LiteralPath $Work -ErrorAction SilentlyContinue }
  else { Write-Host "Sandbox conservata: $Work" -ForegroundColor Yellow }
}

$Fail = @($Results | Where-Object { $_.status -eq 'FAIL' }).Count
$Pass = @($Results | Where-Object { $_.status -eq 'PASS' }).Count
$Report = Join-Path $Root 'RISULTATO_CICLO_INSTALLAZIONE.json'
@{ setup_zip = $SetupZip; checked_at = (Get-Date).ToString('o'); pass = $Pass; fail = $Fail; results = $Results } |
  ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $Report -Encoding UTF8
Write-Host ''
if ($Fail -eq 0) { Write-Host "CICLO INSTALLAZIONE: $Pass/$($Pass + $Fail) PASS" -ForegroundColor Green; exit 0 }
Write-Host "CICLO INSTALLAZIONE: $Fail FAIL su $($Pass + $Fail)" -ForegroundColor Red
exit 1
