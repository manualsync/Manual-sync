$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
# Build id: UNICA FONTE product_version.py. Era ripetuto a mano qui,
# nell'installer e nel validatore pilota; cambiarlo richiedeva sei modifiche
# coordinate e una di esse (regressione_final_candidate) lo imponeva come
# letterale. PowerShell non puo importare il modulo, quindi lo legge.
$ProductVersionFile = Join-Path $Root 'product_version.py'
if (!(Test-Path $ProductVersionFile)) { throw "product_version.py assente: $ProductVersionFile" }
$BuildIdMatch = [regex]::Match((Get-Content -LiteralPath $ProductVersionFile -Raw), "PRODUCT_BUILD\s*=\s*[`"']([^`"']+)[`"']")
if (!$BuildIdMatch.Success) { throw 'PRODUCT_BUILD non leggibile da product_version.py' }
$BuildId = $BuildIdMatch.Groups[1].Value
$SetupZipName = 'ManualSync_Setup_FINAL_CANDIDATE.zip'
$PortableZipName = 'ManualSync_Portable_FINAL_CANDIDATE.zip'
$SetupHashName = 'ManualSync_Setup_FINAL_CANDIDATE.sha256.txt'
$BuildLog = Join-Path $Root 'BUILD_FINAL_CANDIDATE_LOG.txt'
$BuildStatus = Join-Path $Root 'BUILD_STATUS.json'

# Never let a failed/interrupted build leave an older PASS or customer ZIP visible.
Remove-Item -LiteralPath $BuildStatus,$BuildLog -Force -ErrorAction SilentlyContinue
foreach ($dirName in @('build','dist','release','installer_stage','build_launcher','dist_launcher','setup_zip_probe','build_reports')) {
  Remove-Item -Recurse -Force (Join-Path $Root $dirName) -ErrorAction SilentlyContinue
}
Get-ChildItem -LiteralPath $Root -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -like 'RISULTATO_*' -or $_.Name -like 'RUNTIME_SELF_TEST_FINAL.*' } | Remove-Item -Force -ErrorAction SilentlyContinue

Start-Transcript -Path $BuildLog -Force | Out-Null
@{ status='BUILDING'; build=$BuildId; started_at=(Get-Date).ToString('o'); log=$BuildLog } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $BuildStatus -Encoding UTF8
Write-Host "Log build: $BuildLog" -ForegroundColor DarkGray
try {

function Step([string]$Message) {
  Write-Host "`n==> $Message" -ForegroundColor Cyan
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

# CP2.5-A: un gate di ManualSync.exe che fallisce deve dire PERCHE'. In CP2.4
# il messaggio era solo "exit -1": il launcher perdeva lo stato reale del figlio
# Python (vedi manualsync_launcher.exit_status) e nessun log lo riportava.
# Qui lo stato e' mostrato anche in esadecimale, con il significato degli
# NTSTATUS di Windows piu' comuni e la coda del log desktop della home di test.
# Il gate non cambia: qualunque codice diverso da 0 resta un FAIL, senza retry.
function Format-ExeFailure([string]$What, [int]$Code) {
  $hex = '0x{0:X8}' -f $Code
  $known = @{
    '0xC000013A' = 'STATUS_CONTROL_C_EXIT: processo terminato da un evento console (finestra chiusa, Ctrl+C, disconnessione)'
    '0xC0000005' = 'STATUS_ACCESS_VIOLATION: crash nel codice nativo'
    '0xC0000142' = 'STATUS_DLL_INIT_FAILED: avvio del processo fallito'
    '0xC0000017' = 'STATUS_NO_MEMORY: memoria esaurita'
    '0xC00000FD' = 'STATUS_STACK_OVERFLOW'
    '0xC0000409' = 'STATUS_STACK_BUFFER_OVERRUN / fail-fast'
    '0xFFFFFFFF' = '-1: stato non propagato dal launcher o errore del bootloader PyInstaller'
  }
  $meaning = if ($known.ContainsKey($hex)) { $known[$hex] } else { 'codice restituito dall''applicazione' }
  $log = Join-Path $env:USERPROFILE 'ManualSyncData\Logs\ManualSync_Desktop.log'
  $tail = if (Test-Path -LiteralPath $log) { (Get-Content -LiteralPath $log -Tail 6) -join ' | ' } else { 'log desktop assente' }
  return "$What fallito (exit $Code = $hex, $meaning). Ultime righe del log desktop: $tail"
}

Step 'Verifica Python 3.12 x64'
# CP2.5-A.1: la patch e' bloccata. Il runtime cliente E' questa installazione
# Python (copiata sotto), e i lock con hash sono risolti per CPython 3.12.10.
$PyInfo = & py -3.12 -c "import sys,struct; assert sys.version_info[:2]==(3,12); assert sys.version_info[:3]==(3,12,10); assert struct.calcsize('P')*8==64; print(sys.executable); print(sys.base_prefix)"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12.10 x64 non disponibile (versione esatta richiesta dai lock).' }
$PyLines = @($PyInfo)
$SystemPython = $PyLines[0].Trim()
$BasePython = $PyLines[1].Trim()
Write-Host "Python: $SystemPython" -ForegroundColor Green
Write-Host "Runtime base: $BasePython" -ForegroundColor Green

$Venv = Join-Path $Root '.buildvenv'
$BuildPython = Join-Path $Venv 'Scripts\python.exe'
# Un ambiente di build e' riutilizzabile solo se e' stato installato da QUESTI
# lock con hash: pip non ricontrolla gli hash di pacchetti gia' installati, quindi
# un venv nato da altri lock (o senza hash) verrebbe accettato in silenzio.
$BuildLocks = @('requirements.bootstrap.hashes.txt', 'requirements.build.hashes.txt') | ForEach-Object { Join-Path $Root $_ }
$BuildLockId = (($BuildLocks | ForEach-Object { (Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash }) -join '-')
$BuildLockMarker = Join-Path $Venv 'ERYLOS_BUILD_LOCK.txt'
if (Test-Path $BuildPython) {
  & $BuildPython -c "import sys,struct; assert sys.version_info[:2]==(3,12); assert sys.version_info[:3]==(3,12,10); assert struct.calcsize('P')*8==64"
  $LockMatches = (Test-Path -LiteralPath $BuildLockMarker) -and ((Get-Content -LiteralPath $BuildLockMarker -Raw).Trim() -eq $BuildLockId)
  if ($LASTEXITCODE -ne 0 -or -not $LockMatches) {
    Step 'Rimuove ambiente di build non conforme'
    Remove-Item -Recurse -Force $Venv
  }
} elseif (Test-Path $Venv) {
  Remove-Item -Recurse -Force $Venv
}
if (!(Test-Path $Venv)) {
  Step 'Crea ambiente di build isolato'
  & py -3.12 -m venv $Venv
  if ($LASTEXITCODE -ne 0) { throw 'Creazione ambiente di build fallita.' }
}
$BuildPython = Join-Path $Venv 'Scripts\python.exe'
$Pip = Join-Path $Venv 'Scripts\pip.exe'
$PyInstaller = Join-Path $Venv 'Scripts\pyinstaller.exe'

Step 'Installa dipendenze di build'
# CP2.5-A.1: niente piu' "pip install --upgrade pip" (l'ultima versione del
# giorno) ne' dipendenze indirette risolte al momento. pip e setuptools, poi
# l'intero ambiente di build, da lock con hash: --require-hashes --no-deps.
# --no-build-isolation: proxy_tools esiste solo come sorgente e si costruisce
# con il setuptools bloccato qui, non con uno scaricato al volo.
& $BuildPython -m pip install --disable-pip-version-check --require-hashes --no-deps -r (Join-Path $Root 'requirements.bootstrap.hashes.txt')
if ($LASTEXITCODE -ne 0) { throw 'Installazione bloccata di pip e setuptools fallita.' }
& $BuildPython -m pip install --disable-pip-version-check --require-hashes --no-deps --no-build-isolation -r (Join-Path $Root 'requirements.build.hashes.txt')
if ($LASTEXITCODE -ne 0) { throw 'Installazione dipendenze build fallita.' }
Set-Content -LiteralPath $BuildLockMarker -Value $BuildLockId -Encoding ASCII

Step 'Protezione dati reali - ogni test della build gira in una home temporanea'
# 22/09/2026: regressione, self-test, smoke e acceptance della build giravano
# con la home REALE dell'utente e scrivevano (log, puntatore, file WAL dei
# database) in ManualSyncData. Da qui in avanti USERPROFILE/HOME puntano a una
# home temporanea della build; l'impronta dei dati reali presa ora viene
# ricontrollata prima di dichiarare PASS: qualunque modifica fa fallire la build.
$ProtectedDataHome = $env:ERYLOS_PROTECTED_DATA_HOME
if (-not $ProtectedDataHome) { $ProtectedDataHome = Join-Path ([Environment]::GetFolderPath('UserProfile')) 'ManualSyncData' }
$env:ERYLOS_PROTECTED_DATA_HOME = $ProtectedDataHome
$BuildTestHome = Join-Path ([IO.Path]::GetTempPath()) ('erylos_build_test_home_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
New-Item -ItemType Directory -Path $BuildTestHome -Force | Out-Null
# Uno scheletro di profilo Windows, non una cartella vuota: alcuni programmi
# avviati dai test non partono con USERPROFILE vuoto (misurato con Chrome).
foreach ($sub in @('AppData\Local\Temp','AppData\Roaming','Desktop','Documents','Downloads')) {
  New-Item -ItemType Directory -Path (Join-Path $BuildTestHome $sub) -Force | Out-Null
}
$RealDataBefore = Join-Path $BuildTestHome 'dati_reali_prima.json'
$RealDataReport = Join-Path $BuildTestHome 'dati_reali_report.txt'
& $BuildPython (Join-Path $Root 'real_data_guard.py') --snapshot $RealDataBefore --root $ProtectedDataHome
if ($LASTEXITCODE -ne 0) { throw 'Impronta dei dati reali non registrata: nessun test eseguito.' }
$env:USERPROFILE = $BuildTestHome
$env:HOME = $BuildTestHome
$env:HOMEDRIVE = Split-Path -Qualifier $BuildTestHome
$env:HOMEPATH = $BuildTestHome.Substring($env:HOMEDRIVE.Length)
Write-Host "Home dei test della build: $BuildTestHome" -ForegroundColor Green

Step 'Guardia ambiente - prima di qualunque test'
# Prima veniva eseguita DOPO ventidue suite: un ambiente sbagliato si
# scopriva dopo venti minuti, e i risultati intermedi erano gia privi di
# significato. Un ambiente non conforme non e un test fallito: rende ogni
# altro risultato non interpretabile, quindi va accertato per primo.
& $BuildPython -c "import environment_guard; environment_guard.assert_supported_environment()"
if ($LASTEXITCODE -ne 0) { throw 'Ambiente di build non conforme al lock. Nessun test eseguito.' }

Step 'Regressione completa - ogni suite del repository'
# Qui stavano ventidue Step enumerati a mano, fermi a RC2.2: non
# includevano Gate 2A, localization, domain errors, impact intelligence,
# impact center ne translation intelligence. E lo stesso difetto della
# allowlist del payload -- un elenco che invecchia in silenzio -- con la
# stessa correzione: derivarlo. Le suite browser restano escluse perche
# pilotano Chrome e un server applicativo propri, non il pacchetto.
$BrowserSuites = @('regressione_browser_save_4e3.py','regressione_browser_export_4f1.py','regressione_browser_impact_center.py','regressione_browser_fonts.py')
$Suites = @(Get-ChildItem -LiteralPath $Root -File -Filter 'regressione_*.py' | Sort-Object Name | Where-Object { $BrowserSuites -notcontains $_.Name })
if ($Suites.Count -lt 40) { throw "Elenco suite sospetto: solo $($Suites.Count) trovate." }
Write-Host "Suite di regressione da eseguire: $($Suites.Count)" -ForegroundColor Green
$SuiteLogDir = Join-Path $Root 'build_reports\regressione'
New-Item -ItemType Directory -Path $SuiteLogDir -Force | Out-Null
$FailedSuites = @()
foreach ($suite in $Suites) {
  $SuiteOut = Join-Path $SuiteLogDir ($suite.BaseName + '.out.txt')
  $SuiteErr = Join-Path $SuiteLogDir ($suite.BaseName + '.err.txt')
  $rc = Invoke-PythonLogged $BuildPython @($suite.FullName) $Root $SuiteOut $SuiteErr
  if ($rc -ne 0) {
    $FailedSuites += $suite.Name
    Write-Host "  FAIL $($suite.Name) (exit $rc)" -ForegroundColor Red
    Show-Tail $SuiteErr
  }
}
if ($FailedSuites.Count -gt 0) { throw "Regressione fallita: $($FailedSuites -join ', '). Log: $SuiteLogDir" }
Write-Host "Regressione completa: $($Suites.Count)/$($Suites.Count) verdi" -ForegroundColor Green

Step 'Commercial acceptance sorgente'
& $BuildPython (Join-Path $Root 'commercial_acceptance.py')
if ($LASTEXITCODE -ne 0) { throw 'Commercial acceptance sorgente fallita.' }

Step 'Runtime self-test completo'
& $BuildPython (Join-Path $Root 'environment_guard.py')
if ($LASTEXITCODE -ne 0) { throw 'Runtime self-test fallito. Consultare RUNTIME_SELF_TEST_FINAL.txt per il gate esatto.' }

Step 'Smoke test sorgente prima del packaging'
& $BuildPython (Join-Path $Root 'manualsync_desktop.py') --headless-smoke
if ($LASTEXITCODE -ne 0) { throw 'Smoke test sorgente fallito.' }

$Dist = Join-Path $Root 'dist\ManualSync'
$Runtime = Join-Path $Dist 'runtime'
$App = Join-Path $Dist 'app'
Remove-Item -Recurse -Force (Join-Path $Root 'build') -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $Root 'dist') -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $Root 'release') -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $Dist,$Runtime,$App -Force | Out-Null

Step 'Crea runtime Python portabile completo'
# Copy the official CPython installation as runtime, then replace site-packages
# with a clean, locked dependency set. This deliberately does NOT freeze the
# Gradio stack with PyInstaller: package data remains exactly as pip installed it.
$Robo = Start-Process -FilePath 'robocopy.exe' -ArgumentList @("`"$BasePython`"","`"$Runtime`"",'/E','/NFL','/NDL','/NJH','/NJS','/NP') -PassThru -Wait -NoNewWindow
if ($Robo.ExitCode -ge 8) { throw "Copia runtime Python fallita (robocopy exit $($Robo.ExitCode))." }
$RuntimeSite = Join-Path $Runtime 'Lib\site-packages'
Remove-Item -Recurse -Force $RuntimeSite -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $RuntimeSite -Force | Out-Null

Step 'Installa dipendenze runtime bloccate nella distribuzione'
& $BuildPython -m pip install --disable-pip-version-check --no-compile --target $RuntimeSite --require-hashes --no-deps --no-build-isolation -r (Join-Path $Root 'requirements.runtime.hashes.txt')
if ($LASTEXITCODE -ne 0) { throw 'Installazione dipendenze nel runtime portabile fallita.' }

Step 'Riduce il runtime a cio che il prodotto usa'
# CP2.5-A.1. Rimossi SOLO elementi dimostrati inutili in CP2.5-A (resource
# check, smoke e commercial acceptance PASS sul runtime ridotto):
#  * site-packages\bin: 18 entry point CLI (gradio.exe, hf.exe, uvicorn.exe, ...)
#    che nessun modulo invoca; puntano al python del .buildvenv di QUESTA
#    macchina, quindi dal cliente non funzionano e ne rivelano il percorso;
#  * Scripts: pip*.exe della copia di Python, senza pip nel runtime;
#  * Doc (manuale CHM, ~56 MB) e Lib\test (test suite di CPython, ~31 MB).
# Avviene PRIMA di ogni smoke test: il runtime validato e' quello spedito.
$RuntimePrune = @((Join-Path $RuntimeSite 'bin'), (Join-Path $Runtime 'Scripts'), (Join-Path $Runtime 'Doc'), (Join-Path $Runtime 'Lib\test'))
foreach ($p in $RuntimePrune) {
  if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force }
  if (Test-Path -LiteralPath $p) { throw "Riduzione del runtime fallita: $p ancora presente." }
}
$StrayExe = @(Get-ChildItem -LiteralPath $RuntimeSite -Recurse -Filter '*.exe' -File | Where-Object { $_.Directory.Name -eq 'bin' })
if ($StrayExe.Count -gt 0) { throw "Entry point CLI ancora presenti nel runtime: $($StrayExe.FullName -join ', ')" }

Step 'Copia applicazione ManualSync - payload derivato dagli import'
# La allowlist scritta a mano che stava qui elencava 24 file mentre il
# runtime ne richiede 53: fra Gate 2A e Impact Center il prodotto e
# cresciuto di ventuno moduli e di una cartella locales, e l'elenco no.
# Il payload e ora calcolato da packaging_payload.py come chiusura
# transitiva degli import dei veri entry point, piu i dati letti a runtime.
$PayloadList = & $BuildPython (Join-Path $Root 'packaging_payload.py') --print
if ($LASTEXITCODE -ne 0) { throw 'Calcolo del payload fallito.' }
$PayloadEntries = @($PayloadList | Where-Object { $_.Trim() -ne '' })
if ($PayloadEntries.Count -lt 40) { throw "Payload sospetto: solo $($PayloadEntries.Count) elementi." }
foreach ($rel in $PayloadEntries) {
  $src = Join-Path $Root $rel
  if (!(Test-Path $src)) { throw "Runtime source mancante: $rel" }
  $dst = Join-Path $App $rel
  $dstDir = Split-Path -Parent $dst
  if (!(Test-Path $dstDir)) { New-Item -ItemType Directory -Path $dstDir -Force | Out-Null }
  Copy-Item -LiteralPath $src -Destination $dst -Force
}
Write-Host "Payload copiato: $($PayloadEntries.Count) elementi" -ForegroundColor Green

Step 'Verifica statica del payload prima di qualunque smoke test'
# Uno smoke test non puo sostituire questa verifica. Un MODULO mancante
# blocca l'avvio ed e quindi visibile; una cartella DATI mancante no:
# i18n.load_catalog degrada deliberatamente a catalogo vuoto, percio
# resource-check, headless smoke e commercial acceptance passerebbero
# tutti e il cliente installerebbe un prodotto che mostra le chiavi di
# localizzazione al posto delle etichette. Questo controllo e statico.
& $BuildPython (Join-Path $Root 'packaging_payload.py') --verify $App
if ($LASTEXITCODE -ne 0) { throw 'Payload incompleto: build interrotta prima di creare un installer difettoso.' }

# Hard gates for the exact data-file failures seen in R2/R3. Because the runtime
# is copied as normal site-packages, these should be physically present.
$Safe = Join-Path $RuntimeSite 'safehttpx\version.txt'
if (!(Test-Path $Safe)) { throw "Runtime incompleto: manca safehttpx\version.txt: $Safe" }
Write-Host "safehttpx/version.txt presente" -ForegroundColor Green
$IgorowDir = Join-Path $RuntimeSite 'igorow'
if (Test-Path $IgorowDir) {
  $IgorowVersion = Join-Path $IgorowDir 'version.txt'
  if (!(Test-Path $IgorowVersion)) { throw "Runtime incompleto: manca igorow\version.txt: $IgorowVersion" }
  Write-Host "igorow/version.txt presente" -ForegroundColor Green
}

Step 'Test del runtime portabile con python.exe E pythonw.exe prima di creare ManualSync.exe'
$RuntimePython = Join-Path $Runtime 'python.exe'
& $RuntimePython (Join-Path $App 'manualsync_desktop.py') --resource-check
if ($LASTEXITCODE -ne 0) { throw 'Resource check del runtime portabile fallito.' }
& $RuntimePython (Join-Path $App 'manualsync_desktop.py') --headless-smoke
if ($LASTEXITCODE -ne 0) { throw 'Smoke test del runtime portabile con python.exe fallito.' }

# CRITICAL: this is the real customer startup mode. pythonw.exe has no console
# stdout/stderr, so this catches Uvicorn logging regressions that python.exe cannot.
$RuntimePythonw = Join-Path $Runtime 'pythonw.exe'
$PythonwSmoke = Start-Process -FilePath $RuntimePythonw -ArgumentList @((Join-Path $App 'manualsync_desktop.py'),'--headless-smoke') -PassThru -Wait
if ($PythonwSmoke.ExitCode -ne 0) {
  $ErrLog = Join-Path $env:USERPROFILE 'ManualSyncData\Logs\ManualSync_Packaged_Smoke_Error.log'
  throw "Smoke test REALE con pythonw.exe fallito (exit $($PythonwSmoke.ExitCode)). Log: $ErrLog"
}
Write-Host 'pythonw.exe startup smoke PASS' -ForegroundColor Green

Step 'Commercial acceptance sul runtime portabile'
& $RuntimePython (Join-Path $App 'manualsync_desktop.py') --acceptance-smoke
if ($LASTEXITCODE -ne 0) { throw 'Commercial acceptance del runtime portabile fallita.' }

Step 'Crea piccolo launcher ManualSync.exe'
$LauncherWork = Join-Path $Root 'build_launcher'
$LauncherDist = Join-Path $Root 'dist_launcher'
Remove-Item -Recurse -Force $LauncherWork,$LauncherDist -ErrorAction SilentlyContinue
& $PyInstaller --noconfirm --clean --workpath $LauncherWork --distpath $LauncherDist (Join-Path $Root 'ManualSyncLauncher.spec')
if ($LASTEXITCODE -ne 0) { throw 'Build del launcher ManualSync.exe fallita.' }
$LauncherExe = Join-Path $LauncherDist 'ManualSync.exe'
if (!(Test-Path $LauncherExe)) { throw "Launcher non trovato: $LauncherExe" }
Copy-Item -LiteralPath $LauncherExe -Destination (Join-Path $Dist 'ManualSync.exe') -Force
Copy-Item -LiteralPath (Join-Path $Root 'manualsync.ico') -Destination (Join-Path $Dist 'manualsync.ico') -Force

Step 'Smoke test tramite il vero ManualSync.exe'
$FinalExe = Join-Path $Dist 'ManualSync.exe'
$ResProc = Start-Process -FilePath $FinalExe -ArgumentList '--resource-check' -PassThru -Wait
if ($ResProc.ExitCode -ne 0) { throw (Format-ExeFailure 'Resource check tramite ManualSync.exe' $ResProc.ExitCode) }
$SmokeProc = Start-Process -FilePath $FinalExe -ArgumentList '--headless-smoke' -PassThru -Wait
if ($SmokeProc.ExitCode -ne 0) { throw (Format-ExeFailure 'Smoke test tramite ManualSync.exe' $SmokeProc.ExitCode) }
$AcceptanceProc = Start-Process -FilePath $FinalExe -ArgumentList '--acceptance-smoke' -PassThru -Wait
if ($AcceptanceProc.ExitCode -ne 0) { throw (Format-ExeFailure 'Commercial acceptance tramite ManualSync.exe' $AcceptanceProc.ExitCode) }

Step 'Ripulisce solo cache applicative, senza toccare il runtime Python'
# INCIDENTE RC2.2: il resource check del dist passava, poi la pulizia ricorsiva
# cancellava bytecode/cache anche sotto runtime\Lib/site-packages. Il setup copiava
# quindi un payload diverso da quello effettivamente validato. La pulizia cliente
# e ora confinata all'app, dove i file .py sorgente restano sempre presenti.
$DistApp = Join-Path $Dist 'app'
if (Test-Path $DistApp) {
  Get-ChildItem -LiteralPath $DistApp -Directory -Recurse -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq '__pycache__' } | Sort-Object FullName -Descending | ForEach-Object { Remove-Item -Recurse -Force $_.FullName -ErrorAction SilentlyContinue }
  Get-ChildItem -LiteralPath $DistApp -File -Recurse -Force -ErrorAction SilentlyContinue | Where-Object { $_.Extension -in @('.pyc','.pyo') } | ForEach-Object { Remove-Item -Force $_.FullName -ErrorAction SilentlyContinue }
}

Step 'Rivalida il dist esatto dopo la pulizia applicativa'
$ResAfterCleanup = Start-Process -FilePath $FinalExe -ArgumentList '--resource-check' -PassThru -Wait
if ($ResAfterCleanup.ExitCode -ne 0) { throw (Format-ExeFailure 'Resource check post-cleanup' $ResAfterCleanup.ExitCode) }
$SmokeAfterCleanup = Start-Process -FilePath $FinalExe -ArgumentList '--headless-smoke' -PassThru -Wait
if ($SmokeAfterCleanup.ExitCode -ne 0) { throw (Format-ExeFailure 'Smoke test post-cleanup' $SmokeAfterCleanup.ExitCode) }
$AcceptanceAfterCleanup = Start-Process -FilePath $FinalExe -ArgumentList '--acceptance-smoke' -PassThru -Wait
if ($AcceptanceAfterCleanup.ExitCode -ne 0) { throw (Format-ExeFailure 'Commercial acceptance post-cleanup' $AcceptanceAfterCleanup.ExitCode) }

Step 'Crea pacchetto portable solo dopo tutti i test'
$ReleaseDir = Join-Path $Root 'release'
New-Item -ItemType Directory -Path $ReleaseDir -Force | Out-Null
$PortableZip = Join-Path $ReleaseDir $PortableZipName
Remove-Item $PortableZip -Force -ErrorAction SilentlyContinue
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($Dist, $PortableZip, [System.IO.Compression.CompressionLevel]::Optimal, $false)
Write-Host "Portable: $PortableZip" -ForegroundColor Green

Step 'Crea pacchetto PILOTA installabile separato dal BuildKit'
$InstallerStage = Join-Path $Root 'installer_stage'
Remove-Item -Recurse -Force $InstallerStage -ErrorAction SilentlyContinue
$InstallerPayload = Join-Path $InstallerStage 'payload'
New-Item -ItemType Directory -Path $InstallerPayload -Force | Out-Null
$RoboInstaller = Start-Process -FilePath 'robocopy.exe' -ArgumentList @("`"$Dist`"","`"$InstallerPayload`"",'/E','/NFL','/NDL','/NJH','/NJS','/NP') -PassThru -Wait -NoNewWindow
if ($RoboInstaller.ExitCode -ge 8) { throw "Copia payload installer fallita (robocopy exit $($RoboInstaller.ExitCode))." }

Step 'Verifica il payload installer esatto prima dello ZIP'
$InstallerPayloadExe = Join-Path $InstallerPayload 'ManualSync.exe'
if (!(Test-Path $InstallerPayloadExe)) { throw 'Payload installer incompleto: ManualSync.exe assente.' }
$InstallerPayloadResource = Start-Process -FilePath $InstallerPayloadExe -ArgumentList '--resource-check' -PassThru -Wait
if ($InstallerPayloadResource.ExitCode -ne 0) { throw "Resource check del payload installer fallito (exit $($InstallerPayloadResource.ExitCode))." }
$InstallerPayloadSmoke = Start-Process -FilePath $InstallerPayloadExe -ArgumentList '--headless-smoke' -PassThru -Wait
if ($InstallerPayloadSmoke.ExitCode -ne 0) { throw "Smoke test del payload installer fallito (exit $($InstallerPayloadSmoke.ExitCode))." }
$InstallerPayloadAcceptance = Start-Process -FilePath $InstallerPayloadExe -ArgumentList '--acceptance-smoke' -PassThru -Wait
if ($InstallerPayloadAcceptance.ExitCode -ne 0) { throw "Commercial acceptance del payload installer fallita (exit $($InstallerPayloadAcceptance.ExitCode))." }

Step 'Genera manifest SHA256 del payload cliente'
$PayloadFiles = @(Get-ChildItem -LiteralPath $InstallerPayload -File -Recurse -Force | Sort-Object FullName | ForEach-Object {
  [pscustomobject]@{
    path = $_.FullName.Substring($InstallerPayload.Length).TrimStart('\').Replace('\','/')
    sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    bytes = $_.Length
  }
})
if ($PayloadFiles.Count -lt 10) { throw "Payload manifest sospetto: solo $($PayloadFiles.Count) file." }
$PayloadManifestPath = Join-Path $InstallerStage 'PAYLOAD_MANIFEST_SHA256.json'
@{ build=$BuildId; file_count=$PayloadFiles.Count; files=$PayloadFiles } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $PayloadManifestPath -Encoding UTF8

foreach ($name in @('INSTALLA_MANUALSYNC.cmd','INSTALLA_MANUALSYNC.ps1','DISINSTALLA_MANUALSYNC.cmd','DISINSTALLA_MANUALSYNC.ps1','VERIFICA_INSTALLAZIONE_PILOTA.cmd','VERIFICA_INSTALLAZIONE_PILOTA.ps1','LEGGIMI_INSTALLAZIONE_PILOTA.txt')) {
  $src = Join-Path $Root $name
  if (!(Test-Path $src)) { throw "File installer mancante: $name" }
  Copy-Item -LiteralPath $src -Destination $InstallerStage -Force
}
$InstallerZip = Join-Path $ReleaseDir $SetupZipName
Remove-Item $InstallerZip -Force -ErrorAction SilentlyContinue
[System.IO.Compression.ZipFile]::CreateFromDirectory($InstallerStage, $InstallerZip, [System.IO.Compression.CompressionLevel]::Optimal, $false)
if (!(Test-Path $InstallerZip)) { throw 'Pacchetto installer pilota non creato.' }
# Guard against the Windows Explorer MAX_PATH failure observed when a long setup
# filename was nested inside the BuildKit. The customer ZIP name is intentionally
# short; report the longest internal member and fail only on an excessive member.
$ZipRead = [System.IO.Compression.ZipFile]::OpenRead($InstallerZip)
try {
  $LongestEntry = $ZipRead.Entries | Sort-Object { $_.FullName.Length } -Descending | Select-Object -First 1
  if ($LongestEntry -and $LongestEntry.FullName.Length -gt 160) { throw "Percorso interno ZIP troppo lungo ($($LongestEntry.FullName.Length)): $($LongestEntry.FullName)" }
  Write-Host "Path ZIP max interno: $($LongestEntry.FullName.Length) caratteri - $($LongestEntry.FullName)" -ForegroundColor Green
} finally { $ZipRead.Dispose() }
Write-Host "Installer pilota: $InstallerZip" -ForegroundColor Green

Step 'Estrae e valida lo ZIP cliente ESATTO prima di dichiarare PASS'
$ZipProbe = Join-Path $Root 'setup_zip_probe'
Remove-Item -Recurse -Force $ZipProbe -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $ZipProbe -Force | Out-Null
[System.IO.Compression.ZipFile]::ExtractToDirectory($InstallerZip, $ZipProbe)
$ProbeManifestPath = Join-Path $ZipProbe 'PAYLOAD_MANIFEST_SHA256.json'
if (!(Test-Path $ProbeManifestPath)) { throw 'ZIP cliente incompleto: PAYLOAD_MANIFEST_SHA256.json assente.' }
$ProbeManifest = Get-Content -LiteralPath $ProbeManifestPath -Raw | ConvertFrom-Json
$ProbePayload = Join-Path $ZipProbe 'payload'
$ProbeFiles = @(Get-ChildItem -LiteralPath $ProbePayload -File -Recurse -Force)
if ($ProbeFiles.Count -ne [int]$ProbeManifest.file_count) { throw "ZIP cliente alterato: file payload $($ProbeFiles.Count) != manifest $($ProbeManifest.file_count)." }
# -LiteralPath: i percorsi vengono dal manifest, cioe' dai nomi dei file dei
# pacchetti installati. python-docx contiene [Content_Types].xml, e per
# Test-Path senza -LiteralPath le parentesi quadre sono caratteri jolly:
# il file c'e' ma risulta assente. Bloccava la build e avrebbe bloccato
# l'installazione sulla macchina del cliente.
foreach ($item in @($ProbeManifest.files)) {
  $rel = ([string]$item.path).Replace('/','\')
  $path = Join-Path $ProbePayload $rel
  if (!(Test-Path -LiteralPath $path)) { throw "ZIP cliente incompleto: manca $rel" }
  $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($hash -ne ([string]$item.sha256).ToLowerInvariant()) { throw "ZIP cliente corrotto: hash non valido per $rel" }
}
$ProbeExe = Join-Path $ProbePayload 'ManualSync.exe'
foreach ($switch in @('--resource-check','--headless-smoke','--acceptance-smoke')) {
  $probe = Start-Process -FilePath $ProbeExe -ArgumentList $switch -PassThru -Wait
  if ($probe.ExitCode -ne 0) { throw "ZIP cliente estratto fallisce $switch (exit $($probe.ExitCode))." }
}
Write-Host "ZIP cliente estratto: manifest/hash/resource/smoke/acceptance PASS" -ForegroundColor Green
Remove-Item -Recurse -Force $ZipProbe -ErrorAction SilentlyContinue

Step 'Raccoglie report regressione e ripulisce output temporanei'
$ReportsDir = Join-Path $Root 'build_reports'
Remove-Item -Recurse -Force $ReportsDir -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $ReportsDir -Force | Out-Null
Get-ChildItem -LiteralPath $Root -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -like 'RISULTATO_*' -or $_.Name -like 'RUNTIME_SELF_TEST_FINAL.*' } | ForEach-Object { Move-Item -LiteralPath $_.FullName -Destination $ReportsDir -Force }
Get-ChildItem -LiteralPath $Root -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -like 'regressione_*_out' } | ForEach-Object { Remove-Item -Recurse -Force $_.FullName -ErrorAction SilentlyContinue }

Step 'Verifica che nessun test abbia toccato i dati reali'
& $BuildPython (Join-Path $Root 'real_data_guard.py') --verify $RealDataBefore --root $ProtectedDataHome --report $RealDataReport
if ($LASTEXITCODE -ne 0) { throw "I test della build hanno modificato i dati reali in ${ProtectedDataHome}: build NON valida. Report: $RealDataReport" }

$InstallerHash = (Get-FileHash -LiteralPath $InstallerZip -Algorithm SHA256).Hash.ToLowerInvariant()
$HashFile = Join-Path $ReleaseDir $SetupHashName
("{0}  {1}" -f $InstallerHash, (Split-Path -Leaf $InstallerZip)) | Set-Content -LiteralPath $HashFile -Encoding ASCII
$StatusPayload = @{ status='PASS'; build=$BuildId; final_exe=$FinalExe; portable=$PortableZip; pilot_installer=$InstallerZip; pilot_installer_sha256=$InstallerHash; completed_at=(Get-Date).ToString('o') } | ConvertTo-Json -Depth 4
$StatusPayload | Set-Content -LiteralPath $BuildStatus -Encoding UTF8

Step 'BUILD COMPLETATA CON SUCCESSO'
Write-Host "Apri per test interno: $FinalExe" -ForegroundColor Green
Write-Host "Pacchetto da validare come cliente pilota: $InstallerZip" -ForegroundColor Green
Write-Host 'ManualSync 1.0 FINAL CANDIDATE: exact-ZIP acceptance, installer hash manifest, linguistic fail-closed, dedup/non-translatable reuse e full E2E gates PASS.' -ForegroundColor Green
}
catch {
  $Message = $_.Exception.Message
  Write-Host "`nBUILD FALLITA: $Message" -ForegroundColor Red
  # Anche una build fallita dice se i suoi test hanno toccato i dati reali.
  if ((Get-Variable -Name RealDataBefore -ErrorAction SilentlyContinue) -and (Test-Path -LiteralPath $RealDataBefore)) {
    & $BuildPython (Join-Path $Root 'real_data_guard.py') --verify $RealDataBefore --root $ProtectedDataHome --report $RealDataReport
  }
  Remove-Item -Recurse -Force (Join-Path $Root 'setup_zip_probe') -ErrorAction SilentlyContinue
  # A failed build never leaves customer release artifacts behind.
  Remove-Item -Recurse -Force (Join-Path $Root 'release') -ErrorAction SilentlyContinue
  @{ status='FAIL'; build=$BuildId; error=$Message; failed_at=(Get-Date).ToString('o'); log=$BuildLog } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $BuildStatus -Encoding UTF8
  exit 1
}
finally {
  try { Stop-Transcript | Out-Null } catch {}
}
exit 0
