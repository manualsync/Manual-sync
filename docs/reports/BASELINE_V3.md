# BASELINE_V3.md — ManualSync Prompt 0 (Freeze & Baseline Audit)

**Data:** 2026-08-25
**Baseline Git committata:** `bd350ba834a64f7a80b904336517b0d22ffaa759` ("chore: recover ManualSync final candidate baseline")
**Oggetto dell'audit:** `ManualSync_1.0_FINAL_CANDIDATE/` (build `1.0.0-final-candidate-20260822`), così come recuperato e committato nel repository.
**Esecutore:** Claude Code, sessione locale (VS Code + `.venv` Python 3.12.10, Windows 11).

Questo documento è il deliverable del Prompt 0 del Prompt Pack. È un audit **read-only**:
nessun file sorgente di `ManualSync_1.0_FINAL_CANDIDATE/` è stato modificato. Le
incongruenze documentali V3/V4 non sono state corrette (restano documentate in
`BOOTSTRAP_REPORT.md` e nell'handoff). Non è stato eseguito il Prompt 1.

---

## 1. Metodo

A differenza della prevalidazione originale (container Linux, Python 3.13.5), qui i test
sono stati eseguiti **realmente, su Windows 11 reale, con Python 3.12.10** (il runtime
esatto richiesto dal prodotto), nel seguente modo:

1. Installate nel `.venv` del repository le dipendenze già dichiarate dalla Final
   Candidate stessa in `requirements.lock.txt` (gradio, pandas, numpy, PyMuPDF, Pillow,
   rapidfuzz, python-docx) — nessuna dipendenza nuova o non documentata. **Non** sono
   state installate `pywebview`/`pyinstaller` (solo in `requirements.desktop.lock.txt`,
   necessarie solo per build EXE/desktop-launcher, fuori scope per un audit sorgente).
2. `py_compile` su tutti i 53 moduli `.py` di primo livello.
3. L'intera regression suite (24 script `regressione_*.py` + `environment_guard.py`)
   eseguita **su una copia isolata** della cartella in una directory scratch, per non
   alterare la cartella committata (i test scrivono file di output accanto a sé stessi).
   Ogni fallimento è stato investigato fino alla causa radice, non solo registrato come
   FAIL.
4. La cartella committata `ManualSync_1.0_FINAL_CANDIDATE/` è stata verificata invariata
   (92 file, `git status`/`git diff` puliti) dopo l'intera sessione di test.

---

## 2. Architettura reale (verificata leggendo il codice, non solo l'handoff)

Confermato per lettura diretta che l'handoff (`MANUALSYNC_PREVIOUS_WORK_HANDOFF.md`,
sezione 3) descrive accuratamente l'architettura reale. Moduli e responsabilità
principali:

| Modulo | Responsabilità reale (classi/funzioni chiave lette nel codice) |
|---|---|
| `project_lifecycle.py` | `create_project`, `read_project`, `update_project`, `project_health` |
| `ccms_industrial.py` | `IndustrialRepository(ContentRepository)`, `create_industrial_demo` |
| `authoring_ccms.py` | `ContentRepository`, `import_pdf_as_topics`, `export_publication_html` |
| `layout_engine.py` | `TextBlock`/`TableBlock`/`ImageBlock`/`DocumentLayout`, `extract_layout`, `hierarchical_align_text`, `compare_text`/`compare_tables`/`compare_images`, `analyze_layouts` |
| `cat_tm.py` | `TranslationMemory`, `TechnicalSignature`, `technical_signature`, `signatures_compatible`, `match_segments`, `export_xliff` |
| `cat_workspace.py`, `cat_batch.py` | editor/QA/repetition/batch pipeline attorno a `cat_tm` |
| `translation_impact.py` | `analyze_translation_impact`, `_decision_bucket` (4 bucket) |
| `translation_work_package.py` | `generate_translation_work_packages`, `export_work_xliff`, `inspect_work_xliff` |
| `translation_roundtrip.py` | `inspect_translation_roundtrip`, `commit_translation_roundtrip` |
| `workflow_management.py` | `WorkflowManager` (state machine job) |
| `release_readiness.py` | `assess_release_readiness` |
| `release_manager.py` | `create_release_bundle`, `ReleaseIdConflictError`, `_validate_production_linguistics` |
| `publishing_bridges.py` | `export_publication_docx`, `import_docx_outline`, `patch_idml_template`, `import_idml_story_outline` |
| `grounded_authoring.py` | `GroundingStore` |
| `generative_orchestrator.py` | `GenerativeOrchestrator`, `GenerationLedger`, `SafeSourceComposer` |
| `linguistic_safety.py` | `cross_language_issue`, `collect_cross_language_issues`, `is_nontranslatable_text` |
| `language_codes.py` | `canonical_lang`, `canonical_langs` |
| `idml_hardening.py` | `inspect_idml`, `patch_idml_slots`, `audit_idml_roundtrip` |
| `asset_intelligence.py`, `impact_economics.py` | audit TM/costi/leverage/vendor ranking |
| `support_diagnostics.py` | `system_health`, `create_support_bundle` |
| `commercial_acceptance.py` | `run_acceptance` (suite E2E isolata, distruttiva-free) |
| `app.py` / `app_engineering.py` | UI Gradio commerciale + callback engineering |
| `manualsync_desktop.py`, `manualsync_launcher.py`, `environment_guard.py` | avvio desktop, resource check, self-test runtime |

**Osservazione architetturale non presente nell'handoff:** `layout_engine.py` (il file più
grande e complesso) contiene **definizioni ripetute della stessa funzione** (es.
`extract_layout`, `compare_text`, `compare_tables`, `analyze_layouts`,
`hierarchical_align_text` ricorrono 5-10 volte ciascuna) corrispondenti a iterazioni
storiche v0.5→v0.11 accumulate nello stesso file. In Python l'ultima definizione vince a
runtime, quindi non è un bug funzionale, ma è debito tecnico reale: il file è difficile da
leggere/mantenere e nasconde quale versione di ogni funzione sia effettivamente attiva.
Da tenere presente per qualunque refactoring futuro (fuori scope ora).

---

## 3. Capability: AVAILABLE / PARTIAL / ROADMAP

La classificazione già prodotta nell'handoff (sezioni 4-6) è stata attraversata modulo
per modulo e **risulta coerente con il codice reale** — non è stata trovata alcuna
capability dichiarata AVAILABLE che risultasse assente o solo stub nel sorgente. Non la
riproduco integralmente qui (resta autoritativa in
`docs/reports/MANUALSYNC_PREVIOUS_WORK_HANDOFF.md`, sezioni 4-6); di seguito solo le
conferme/precisazioni dirette ottenute in questo audit:

- **Language canonicalization** (`language_codes.py`): confermata via lettura diretta e
  via `regressione_desktop_r18.py` (test reale eseguito, PASS).
- **Cross-language linguistic safety fail-closed** (`linguistic_safety.py`): confermata
  via lettura diretta e via `regressione_commercial_safety_rc22.py` (test reale
  eseguito, PASS) — un target IT copiato in EN viene rifiutato.
- **Alignment confidence policy** (>=90 zero-penalty, 80-89 review, unsafe escluso):
  confermata a livello di codice reale isolato (vedi §6.3) — 5/5 PASS.
- **Translation Work Package / Round-trip / Workflow**: confermati con dati reali
  end-to-end tramite `environment_guard.py` (37/37, vedi §6.2), non solo tramite
  ispezione statica.
- **Import DOCX** resta PARTIAL come dichiarato: `import_docx_outline()` esiste
  (`publishing_bridges.py`), ma nessun test nella suite ne dimostra un percorso UX
  end-to-end completo — coerente con la classificazione dell'handoff.

Nessuna capability è stata riclassificata. Product Doctrine V4 resta la **visione**;
questa sezione e le tabelle dell'handoff restano la **fotografia dell'implementato**.

---

## 4. Risultati reali dei test (non le cifre dichiarate — quelle rieseguite qui)

### 4.1 `py_compile`

**53/53 moduli `.py` compilano senza errori.** Conferma il claim `py_compile: PASS`.

### 4.2 Regression suite (24 script `regressione_*.py`), prima esecuzione grezza

| Esito grezzo | Conteggio |
|---|---|
| `rc=0` (script termina pulito) | 19/24 |
| `rc≠0` (script termina con errore) | 5/24 |

I 5 falliti, **investigati singolarmente fino alla causa radice** (vedi §5 e §6):
`regressione_alignment_policy.py`, `regressione_final_candidate.py`,
`regressione_ui_callbacks_rc2.py`, `regressione_v23.py`, `regressione_v24.py`.

### 4.3 Risultato reale dopo investigazione (conteggio dei singoli check, non degli script)

| Script | Check reali | Esito reale | Causa del `rc≠0` iniziale |
|---|---|---|---|
| `regressione_alignment_benchmark.py` | 5/5 PASS | OK | — |
| `regressione_alignment_policy.py` | **5/5 PASS** (isolato dal bug di cleanup) | script FAIL | **bug reale**, vedi §5 |
| `regressione_alignment_real_world.py` | 8/8 PASS | OK | — |
| `regressione_commercial_safety_rc22.py` | 15/15 PASS | OK | — |
| `regressione_commercial_stabilization_rc1.py` | 35/35 PASS | OK | — |
| `regressione_commercial_stabilization_rc2.py` | 38/38 PASS | OK | — |
| `regressione_desktop_r11.py` | 11/11 PASS | OK | — |
| `regressione_desktop_r12.py` | 27/27 PASS | OK | — |
| `regressione_desktop_r13.py` | 14/14 PASS | OK | — |
| `regressione_desktop_r14.py` | 16/16 PASS | OK | — |
| `regressione_desktop_r15.py` | 20/20 PASS | OK | — |
| `regressione_desktop_r16.py` | 14/14 PASS | OK | — |
| `regressione_desktop_r17.py` | 15/15 PASS | OK | — |
| `regressione_desktop_r18.py` | 36/36 PASS | OK | — |
| `regressione_desktop_r23_ui.py` | 12/12 PASS | OK | — |
| `regressione_e2e_v1.py` | 8/8 PASS | OK | — |
| `regressione_final_candidate.py` | **29/29 PASS** (confermato con encoding corretto) | script FAIL | **solo console/encoding**, vedi §6 |
| `regressione_grounded_r7.py` | 12/12 PASS | OK | — |
| `regressione_rc19_semantic_migration.py` | 11/11 PASS | OK | — |
| `regressione_ui_callbacks_rc2.py` | **13/13 PASS** (confermato con encoding corretto) | script FAIL | **solo console/encoding**, vedi §6 |
| `regressione_v23.py` | **13/13 PASS** (confermato con encoding corretto) | script FAIL | **solo console/encoding**, vedi §6 |
| `regressione_v24.py` | **16/16 PASS** (confermato con encoding corretto) | script FAIL | **solo console/encoding**, vedi §6 |
| `regressione_v25.py` | 24/24 PASS | OK | — |
| `regressione_v26_v28.py` | 11/11 PASS | OK | — |
| `commercial_acceptance.py` (invocato da final_candidate) | 15/15 PASS | OK (dopo fix encoding) | — |
| `environment_guard.py` (runtime self-test, fuori dai 24) | **37/37 PASS** su Windows 11 + Python 3.12.10 reale | OK (dopo fix encoding) | solo console/encoding |

**Totale singoli check verificati indipendentemente: 445/445 PASS.**
**Bug funzionali reali trovati: 1** (§5). **Falsi negativi da console Windows: 4 script +
2 componenti invocati** (§6).

Nota di onestà metodologica: questo totale (445) non è direttamente comparabile al
"357/357 RC2.2 regression baseline" dichiarato in `PREVALIDATION_FINAL_CANDIDATE.json`,
perché quella cifra si riferiva a una suite/composizione leggermente diversa (RC2.2, non
l'intera suite Final Candidate a 24 script + self-test eseguita qui) e perché non è stato
ricostruito un mapping esatto 1:1 fra le due enumerazioni. Quello che **è** stato
verificato in modo diretto è: nessuna check di merito è risultata realmente FAIL, tranne
l'esito di cleanup del bug in §5.

### 4.4 Runtime self-test (`environment_guard.py`) — confronto col claim di prevalidazione

| | Prevalidazione dichiarata | Verificato ora (Windows 11 reale) |
|---|---|---|
| Ambiente | container, Python 3.13.5 | Windows 11 fisico, Python 3.12.10 |
| Risultato | 36/37 (unico FAIL: versione Python errata) | **37/37 PASS** |

Questo è un miglioramento reale e diretto rispetto al claim: sull'esatto runtime target
del prodotto (Python 3.12 x64), il self-test passa **integralmente**.

---

## 5. Bug reale trovato (funzionale, non di encoding)

### Connessione SQLite non rilasciata in `_auto_project_legacy_tm()` (`app.py`)

**Riproducibilità:** 100% (riprodotto in 2 esecuzioni indipendenti, con directory temp
diverse).

**Sintomo:** `regressione_alignment_policy.py` termina con:
```
PermissionError: [WinError 32] Impossibile accedere al file. Il file è utilizzato da un
altro processo: '...\ManualSync_PROJECT_LEGACY_TM.sqlite'
```
durante il cleanup automatico di `tempfile.TemporaryDirectory()` (Windows rifiuta di
cancellare un file ancora aperto da un handle attivo; su Linux/macOS lo stesso codice non
avrebbe fallito, perché quei filesystem permettono l'unlink di file aperti).

**Causa:** `_auto_project_legacy_tm(project)` (in `app.py`) apre una connessione al
database TM (`ManualSync_PROJECT_LEGACY_TM.sqlite`), presumibilmente tramite
`cat_tm.TranslationMemory`, senza chiuderla esplicitamente prima di ritornare.

**Impatto sulla logica di prodotto (isolato indipendentemente, senza modificare il
sorgente):** le 5 assertion di policy che il test intende verificare (creazione TM
legacy, esclusione coppie tecnicamente unsafe, policy di confidence >=90/80-89/unsafe)
**passano tutte, 5/5**, quando il bug di cleanup viene isolato con
`tempfile.TemporaryDirectory(ignore_cleanup_errors=True)`. Il bug **non altera l'esito
della policy di allineamento**; è un difetto di gestione delle risorse (resource leak)
specifico di Windows.

**Perché è rilevante:** è esattamente il tipo di problema Windows-specifico che
l'handoff segnala come "gate Windows ancora da eseguire" — e conferma che quel gate non è
puramente burocratico: qui ha effettivamente trovato un bug reale, riproducibile, non
visibile in un container Linux. Se lo stesso pattern (`TranslationMemory`/connessione
sqlite non chiusa) si presenta altrove nel prodotto durante operazioni ripetute di
apertura/chiusura progetto su Windows, potrebbe manifestarsi come blocco di file reali
durante l'uso normale (es. impossibilità di cancellare/spostare un progetto appena
chiuso). **Non corretto in questa fase** (Prompt 0 è read-only); da pianificare come fix
mirato con test di non-regressione dedicato prima di qualunque hardening Windows
successivo.

---

## 6. Problemi di solo encoding/console Windows (NON bug funzionali)

**Causa comune:** più script scrivono `print(json.dumps(..., ensure_ascii=False))` (o
stringhe con caratteri come `✅`, `≠`) senza forzare l'encoding UTF-8 dello stdout. La
console Windows di questo ambiente usa la codepage legacy **cp1252**, che non sa
codificare quei caratteri: lo script va in `UnicodeEncodeError` **solo sulla stampa
finale**, dopo che tutte le verifiche funzionali sono già state eseguite (e nella
maggior parte dei casi già scritte su file in UTF-8 esplicito prima della stampa).

**Script confermati affetti (verificati singolarmente, uno per uno, con
`PYTHONIOENCODING=utf-8` o rileggendo il report scritto su disco prima del crash):**

| Script | Esito reale una volta rimossa la variabile di confondimento |
|---|---|
| `regressione_final_candidate.py` | 29/29 PASS (crash a valle di `commercial_acceptance.py`, stesso bug) |
| `commercial_acceptance.py` | 15/15 PASS, `status=PASS` |
| `regressione_ui_callbacks_rc2.py` | 13/13 PASS (verificato con rerun pulito dedicato, come richiesto) |
| `regressione_v23.py` | 13/13 PASS (letto dal report scritto su disco prima del crash) |
| `regressione_v24.py` | 16/16 PASS (letto dal report scritto su disco prima del crash) |
| `environment_guard.py` (self-test runtime) | 37/37 PASS (letto dal report scritto su disco prima del crash) |

**Non è un problema del prodotto ManualSync in sé** (l'app Gradio non stampa JSON su
console in uso normale), ma **è un problema reale di Developer Experience/CI** sulla
toolchain di test: chiunque esegua questi script da un prompt Windows con codepage
predefinita (non solo questo ambiente: è il default su molte installazioni Windows
italiane/europee) vedrà uno script "fallire" quando in realtà tutte le verifiche sono
passate — rischio concreto di falsi allarmi durante future sessioni di sviluppo o durante
il build Windows stesso, se `COSTRUISCI_MANUALSYNC_EXE.cmd` invoca questi script senza
`PYTHONIOENCODING=utf-8` impostato. **Non e' stato confermato esaustivamente su tutti i
24 script** (solo su quelli che in questa esecuzione contenevano un carattere non-ASCII
nel proprio output di dettaglio): il pattern di rischio è sistemico nel codice dei test,
anche se si è manifestato solo in 6 casi su questa run. **Non corretto in questa fase.**

---

## 7. NON-REGRESSION INVARIANTS

Riconfermo qui, senza modifiche, gli invariant elencati in
`MANUALSYNC_PREVIOUS_WORK_HANDOFF.md` §7 (45 invariant, da "Core R18 è baseline stabile"
a "Launcher non deve dipendere da `.pyc/__pycache__` residui"). Non li ripeto
integralmente per evitare divergenza dal documento sorgente — restano autoritativi lì.
Quelli **verificati direttamente in questo audit con codice ed esecuzione reali** (non
solo per lettura):

- **#2 Language canonicalization** — verificato (§3, `regressione_desktop_r18.py`).
- **#9 Technical safety prevale sul fuzzy score** — coerente col codice di
  `cat_tm.signatures_compatible`/`source_target_safe` letto direttamente.
- **#11 Alignment confidence policy (>=90 / 80-89 / unsafe)** — verificato isolatamente,
  5/5 PASS (§5).
- **#18 Cross-language linguistic fail-closed** — verificato
  (`regressione_commercial_safety_rc22.py`, PASS reale).
- **#20 Workflow state machine auditata** — verificato (`environment_guard.py`,
  "Workflow audited state machine" PASS reale con dati concreti).
- **#45 Launcher non deve dipendere da `.pyc/__pycache__` residui** — **osservazione
  procedurale**: in questo stesso audit ho generato accidentalmente `__pycache__/`
  dentro la cartella committata con un primo `py_compile` lanciato per errore sul
  sorgente reale invece che sulla copia scratch; l'ho rimosso subito e verificato che
  git lo ignora comunque (`__pycache__/` è in `.gitignore`). Nessun impatto sul
  repository, ma è un promemoria concreto del perché questo invariant esiste.

---

## 8. Cosa richiede ancora validazione Windows (blocker verso il Final Windows Gate)

Il "Final Windows Gate" definito in `PREVALIDATION_FINAL_CANDIDATE.json` **non è stato
eseguito in questo audit** e resta un blocker separato da quanto verificato qui. Non
eseguito perché fuori scope per un audit Prompt 0 read-only (comporta build reale
dell'EXE, installazione, modifica dello stato di sistema):

1. Build reale con `COSTRUISCI_MANUALSYNC_EXE.cmd` su Python 3.12 x64 → `BUILD_STATUS.json = PASS`.
2. Installazione di `release\ManualSync_Setup_FINAL_CANDIDATE.zip`.
3. `VERIFICA_INSTALLAZIONE_PILOTA.cmd` = PASS (hash payload, resource check, startup
   smoke, acceptance E2E isolata, shortcut Desktop/Start Menu).
4. Avvio da Desktop, creazione progetto, chiusura/riapertura con persistenza.
5. Verifica che un target `IT -> en-US` copiato risulti `QA_BLOCKED` e non possa produrre
   `PRODUZIONE CLIENTE` **sull'artefatto installato** (non solo a livello sorgente — qui
   verificato solo a livello sorgente/`.venv`).

**Quello che questo audit aggiunge rispetto alla prevalidazione precedente:** conferma
che a livello sorgente, sul runtime Python esatto richiesto (3.12 x64) e su Windows reale
(non container), l'intera suite regressione + self-test passa (445/445 check reali), con
un solo bug reale isolato e ben circoscritto (§5). Questo riduce ma **non elimina** il
gate Windows di pacchettizzazione/installer, che rimane interamente da eseguire.

---

## 9. Blocker aperti prima del Prompt 1

1. **Bug reale non corretto**: connessione SQLite non chiusa in `_auto_project_legacy_tm`
   (§5) — da pianificare come fix mirato con test di non-regressione dedicato, non ancora
   autorizzato in questa fase.
2. **Rischio DX/CI sistemico non corretto**: print non UTF-8-safe nella regression suite
   (§6) — può generare falsi allarmi nel build Windows automatizzato se non gestito.
3. **Final Windows Gate non eseguito** (§8) — build EXE, installer, pilot reale restano
   da fare.
4. **Incongruenze documentali V3/V4** — non corrette, come da istruzione esplicita
   (restano in `BOOTSTRAP_REPORT.md` e nell'handoff).
5. **`layout_engine.py`**: debito tecnico da definizioni di funzione ripetute
   (§2) — non bloccante per l'uso, ma da tenere presente per qualunque manutenzione
   futura di quel modulo.

Nessuno di questi blocker è stato risolto in questa fase, come da istruzione. Sono
condizioni da valutare esplicitamente prima di autorizzare il Prompt 1.
