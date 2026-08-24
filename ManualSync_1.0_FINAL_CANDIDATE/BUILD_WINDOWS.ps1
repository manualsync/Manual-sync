$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$BuildId = '1.0.0-final-candidate-20260822'
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

Step 'Verifica Python 3.12 x64'
$PyInfo = & py -3.12 -c "import sys,struct; assert sys.version_info[:2]==(3,12); assert struct.calcsize('P')*8==64; print(sys.executable); print(sys.base_prefix)"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 x64 non disponibile.' }
$PyLines = @($PyInfo)
$SystemPython = $PyLines[0].Trim()
$BasePython = $PyLines[1].Trim()
Write-Host "Python: $SystemPython" -ForegroundColor Green
Write-Host "Runtime base: $BasePython" -ForegroundColor Green

$Venv = Join-Path $Root '.buildvenv'
$BuildPython = Join-Path $Venv 'Scripts\python.exe'
if (Test-Path $BuildPython) {
  & $BuildPython -c "import sys,struct; assert sys.version_info[:2]==(3,12); assert struct.calcsize('P')*8==64"
  if ($LASTEXITCODE -ne 0) {
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
& $BuildPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'Aggiornamento pip fallito.' }
& $Pip install -r (Join-Path $Root 'requirements.desktop.lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Installazione dipendenze build fallita.' }

Step 'R23.2 UI structure polish + overflow menu fix (core R18 invariato)'
& $BuildPython (Join-Path $Root 'regressione_desktop_r23_ui.py')
if ($LASTEXITCODE -ne 0) { throw 'R23.2 UX UI gate fallito.' }

Step 'Regressione Grounded Authoring R7 (invariata in R18)'
& $BuildPython (Join-Path $Root 'regressione_grounded_r7.py')
if ($LASTEXITCODE -ne 0) { throw 'Regressione Grounded Authoring R7 fallita: R23.2 UI Structure Polish non distribuibile.' }

Step 'R18 canonicalizzazione codici lingua + compatibilita stato persistente legacy'
& $BuildPython (Join-Path $Root 'regressione_desktop_r18.py')
if ($LASTEXITCODE -ne 0) { throw 'R18 language canonicalization / persistent-state hardening fallito.' }

Step 'R17 atomic release preflight + immutable ID safety'
& $BuildPython (Join-Path $Root 'regressione_desktop_r17.py')
if ($LASTEXITCODE -ne 0) { throw 'R17 atomic release preflight / ID safety fallito.' }

Step 'R16 friendly fail-closed release feedback'
& $BuildPython (Join-Path $Root 'regressione_desktop_r16.py')
if ($LASTEXITCODE -ne 0) { throw 'R16 friendly fail-closed release feedback fallito.' }

Step 'R15 production/test release safety + commercial UX (regressione)'
& $BuildPython (Join-Path $Root 'regressione_desktop_r15.py')
if ($LASTEXITCODE -ne 0) { throw 'R15 production/test release safety / commercial UX fallito.' }

Step 'R14 publishing normalization + DOCX quality gate'
& $BuildPython (Join-Path $Root 'regressione_desktop_r14.py')
if ($LASTEXITCODE -ne 0) { throw 'R14 publishing normalization / quality gate fallito.' }

Step 'R13 release isolation + CAT safety + UX cleanup gate (regressione)'
& $BuildPython (Join-Path $Root 'regressione_desktop_r13.py')
if ($LASTEXITCODE -ne 0) { throw 'R13 release isolation / CAT safety gate fallito.' }

Step 'Regressione R12 translation completeness + release hardening'
& $BuildPython (Join-Path $Root 'regressione_desktop_r12.py')
if ($LASTEXITCODE -ne 0) { throw 'Regressione R12 translation completeness fallita.' }

Step 'Regressione invarianti readiness R11 -> R12'
& $BuildPython (Join-Path $Root 'regressione_desktop_r11.py')
if ($LASTEXITCODE -ne 0) { throw 'Regressione invarianti readiness fallita.' }

Step 'Regressione migrazione semantica RC1.9'
& $BuildPython (Join-Path $Root 'regressione_rc19_semantic_migration.py')
if ($LASTEXITCODE -ne 0) { throw 'Regressione semantic migration fallita.' }

Step 'Benchmark sintetico auto-allineamento >=95%'
& $BuildPython (Join-Path $Root 'regressione_alignment_benchmark.py')
if ($LASTEXITCODE -ne 0) { throw 'Benchmark sintetico auto-allineamento fallito.' }

Step 'Benchmark reale pre-allineamento bilingue industriale'
& $BuildPython (Join-Path $Root 'regressione_alignment_real_world.py')
if ($LASTEXITCODE -ne 0) { throw 'Benchmark reale pre-allineamento bilingue fallito.' }

Step 'Policy di sicurezza confidence: auto-reuse >=90, review 80-89'
& $BuildPython (Join-Path $Root 'regressione_alignment_policy.py')
if ($LASTEXITCODE -ne 0) { throw 'Policy di confidence dell auto-allineamento fallita.' }

Step 'Regressione Translation Work Package v0.23'
& $BuildPython (Join-Path $Root 'regressione_v23.py')
if ($LASTEXITCODE -ne 0) { throw 'Regressione Translation Work Package v0.23 fallita.' }

Step 'Regressione Round-Trip QA v0.24'
& $BuildPython (Join-Path $Root 'regressione_v24.py')
if ($LASTEXITCODE -ne 0) { throw 'Regressione Round-Trip QA v0.24 fallita.' }

Step 'Regressione Workflow v0.25'
& $BuildPython (Join-Path $Root 'regressione_v25.py')
if ($LASTEXITCODE -ne 0) { throw 'Regressione Workflow v0.25 fallita.' }

Step 'Regressione Release / Readiness / Lifecycle v0.26-v0.28'
& $BuildPython (Join-Path $Root 'regressione_v26_v28.py')
if ($LASTEXITCODE -ne 0) { throw 'Regressione Release / Readiness / Lifecycle fallita.' }

Step 'Regressione end-to-end core'
& $BuildPython (Join-Path $Root 'regressione_e2e_v1.py')
if ($LASTEXITCODE -ne 0) { throw 'Regressione end-to-end fallita.' }

Step 'Commercial Stabilization RC1 — regressione legacy completa, 3x E2E'
& $BuildPython (Join-Path $Root 'regressione_commercial_stabilization_rc1.py')
if ($LASTEXITCODE -ne 0) { throw 'Commercial Stabilization RC1 regression gate fallito.' }

Step 'Commercial Stabilization RC2 — startup, shortcut, project-start UX e packaging safety'
& $BuildPython (Join-Path $Root 'regressione_commercial_stabilization_rc2.py')
if ($LASTEXITCODE -ne 0) { throw 'Commercial Stabilization RC2 gate fallito.' }

Step 'RC2 UI callback end-to-end — progetto -> traduzione -> QA -> release -> backup/restore'
& $BuildPython (Join-Path $Root 'regressione_ui_callbacks_rc2.py')
if ($LASTEXITCODE -ne 0) { throw 'RC2 UI callback E2E fallito.' }

Step 'RC2.2 commercial linguistic safety — QA, production fail-closed, repeats, non-translatables'
& $BuildPython (Join-Path $Root 'regressione_commercial_safety_rc22.py')
if ($LASTEXITCODE -ne 0) { throw 'RC2.2 commercial linguistic safety gate fallito.' }

Step 'Final Candidate commercial acceptance — callback E2E + production safety'
& $BuildPython (Join-Path $Root 'commercial_acceptance.py')
if ($LASTEXITCODE -ne 0) { throw 'Commercial acceptance sorgente fallita.' }

Step 'Final Candidate static packaging / installer / UX gate'
& $BuildPython (Join-Path $Root 'regressione_final_candidate.py')
if ($LASTEXITCODE -ne 0) { throw 'Final Candidate static gate fallito.' }

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
& $BuildPython -m pip install --no-compile --upgrade --target $RuntimeSite -r (Join-Path $Root 'requirements.runtime.lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Installazione dipendenze nel runtime portabile fallita.' }

Step 'Copia applicazione ManualSync — runtime allowlist pulita'
$RuntimeFiles = @(
  'app.py','authoring_ccms.py','cat_batch.py','cat_tm.py','ccms_industrial.py',
  'generative_orchestrator.py','grounded_authoring.py','idml_hardening.py','impact_economics.py',
  'language_codes.py','layout_engine.py','manualsync_desktop.py','product_version.py',
  'project_lifecycle.py','publishing_bridges.py','release_manager.py','release_readiness.py','support_diagnostics.py',
  'translation_impact.py','translation_roundtrip.py','translation_work_package.py','linguistic_safety.py','workflow_management.py','commercial_acceptance.py',
  'manualsync.png','VERSION.txt','qa_profile_esempio.json','glossario_equivalenze_esempio.csv',
  'terminologia_cat_esempio.csv','vendor_rates_esempio.csv'
)
foreach ($name in $RuntimeFiles) {
  $src = Join-Path $Root $name
  if (!(Test-Path $src)) { throw "Runtime source mancante: $name" }
  Copy-Item -LiteralPath $src -Destination $App -Force
}

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
  $ErrLog = Join-Path ([Environment]::GetFolderPath('UserProfile')) 'ManualSyncData\Logs\ManualSync_Packaged_Smoke_Error.log'
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
if ($ResProc.ExitCode -ne 0) { throw "Resource check tramite ManualSync.exe fallito (exit $($ResProc.ExitCode))." }
$SmokeProc = Start-Process -FilePath $FinalExe -ArgumentList '--headless-smoke' -PassThru -Wait
if ($SmokeProc.ExitCode -ne 0) { throw "Smoke test tramite ManualSync.exe fallito (exit $($SmokeProc.ExitCode))." }
$AcceptanceProc = Start-Process -FilePath $FinalExe -ArgumentList '--acceptance-smoke' -PassThru -Wait
if ($AcceptanceProc.ExitCode -ne 0) { throw "Commercial acceptance tramite ManualSync.exe fallita (exit $($AcceptanceProc.ExitCode))." }

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
if ($ResAfterCleanup.ExitCode -ne 0) { throw "Resource check post-cleanup fallito (exit $($ResAfterCleanup.ExitCode))." }
$SmokeAfterCleanup = Start-Process -FilePath $FinalExe -ArgumentList '--headless-smoke' -PassThru -Wait
if ($SmokeAfterCleanup.ExitCode -ne 0) { throw "Smoke test post-cleanup fallito (exit $($SmokeAfterCleanup.ExitCode))." }
$AcceptanceAfterCleanup = Start-Process -FilePath $FinalExe -ArgumentList '--acceptance-smoke' -PassThru -Wait
if ($AcceptanceAfterCleanup.ExitCode -ne 0) { throw "Commercial acceptance post-cleanup fallita (exit $($AcceptanceAfterCleanup.ExitCode))." }

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
foreach ($item in @($ProbeManifest.files)) {
  $rel = ([string]$item.path).Replace('/','\')
  $path = Join-Path $ProbePayload $rel
  if (!(Test-Path $path)) { throw "ZIP cliente incompleto: manca $rel" }
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
