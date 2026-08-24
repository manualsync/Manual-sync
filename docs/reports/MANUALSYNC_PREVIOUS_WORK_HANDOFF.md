# MANUALSYNC_PREVIOUS_WORK_HANDOFF

**Scopo:** trasferire nel nuovo repository locale tutto il valore tecnico già prodotto, senza ricominciare da zero e senza trascinare build obsolete o assunzioni non verificate.

**Data handoff:** 2026-08-24  
**Nuovo repository operativo:** `C:\Users\bxai\OneDrive\Desktop\Manual-Sync`  
**Regola di governance:** ChatGPT = memoria/storico/supervisione; Claude Code = esecuzione operativa nel repository reale.

---

## 1. Ultima baseline certa

### Baseline sorgente più recente e recuperabile

**ManualSync 1.0 — FINAL CANDIDATE**  
**Build:** `1.0.0-final-candidate-20260822`

Dati dichiarati dal file `VERSION.txt` della Final Candidate:

- **Core baseline:** `R18 stable + validated commercial safety fixes`
- **UI baseline:** `R23.2 + project-creation idempotency + workflow/release guardrails`
- **Packaging:** exact customer ZIP re-extracted, hash-verified and commercial-acceptance tested before PASS
- **Stato:** Final Candidate, non ancora commercial freeze

Artifact sorgente recuperabile:

`ManualSync_1.0_FINAL_CANDIDATE_Full_BuildKit.zip`

SHA-256 verificato:

`21c411ca5e1a0a136dbe47acfb42f167f573618b5f760ff62d4d290daaeb0821`

Contenuto: **92 file**, inclusi sorgenti Python, regression suite, benchmark industriali, builder Windows, installer scripts, manifest e prevalidation.

### Stato di validazione della Final Candidate

Il file `PREVALIDATION_FINAL_CANDIDATE.json` dichiara:

- RC2.2 regression baseline: **357/357 PASS**
- Final Candidate static gate: **29/29 PASS**
- Commercial Acceptance: **15/15 PASS**
- Alignment confidence policy: **5/5 PASS**
- Real industrial alignment gate: **8/8 PASS**
- `py_compile`: **PASS**
- runtime container: **36/37**, unico FAIL = Python 3.13.5 nel container invece di Python 3.12 x64 richiesto

### Gate ancora mancante

La Final Candidate **NON era stata ancora validata end-to-end sul Windows reale dell'utente**.

Il gate previsto era:

1. build con `COSTRUISCI_MANUALSYNC_EXE.cmd` su Windows/Python 3.12 x64;
2. `BUILD_STATUS.json` con `status=PASS`;
3. installazione di `release\ManualSync_Setup_FINAL_CANDIDATE.zip`;
4. `VERIFICA_INSTALLAZIONE_PILOTA.cmd` = PASS;
5. apertura da Desktop;
6. creazione progetto;
7. chiusura/riapertura e persistenza;
8. falso target IT -> en-US deve risultare `QA_BLOCKED`;
9. lo stesso falso target non deve poter produrre `PRODUZIONE CLIENTE`.

**Conclusione:** questa è l'ultima baseline sorgente certa.  
**Commercial-ready:** **NON VERIFICATO**.

### Ultima build realmente provata a lungo su Windows

La **RC2.1** è stata installata e usata sul Windows reale fino a:

- avvio da icona Desktop;
- creazione progetto;
- import PDF;
- approvazione sorgente;
- Translation Work Package;
- creazione job;
- ritorno XLIFF;
- QA;
- approvazione;
- readiness;
- release interna;
- release Produzione cliente.

Ma RC2.1 aveva una falla critica: un target `en-US` copiato dall'italiano poteva superare QA e arrivare fino a `PRODUZIONE CLIENTE`.

Quindi RC2.1 **non è una baseline commerciale valida**.

---

## 2. Cronologia release rilevante

### Fondazioni

**v0.19 — Grounded Authoring**
- punto di ripartenza del progetto;
- redazione basata su fonti/evidenze.

**v0.21.1 — Environment Hardening**
- ambiente isolato;
- Python 3.12;
- dipendenze bloccate;
- self-test;
- comportamento fail-closed.

**v0.22 — Translation Impact Intelligence**
- collegamento CCMS -> TM;
- classificazioni:
  - `AUTO_REUSE`
  - `LINGUISTIC_REVIEW`
  - `TECHNICAL_REVIEW`
  - `NEW_TRANSLATION`

**v0.23 — Translation Work Package**
- pacchetto XLIFF 2.0 per lingua;
- `AUTO_REUSE` escluso/bloccato dal lavoro manuale;
- `TECHNICAL_REVIEW` con target obbligatorio.

**v0.24 — Translation Round-Trip QA**
- checksum pacchetto;
- snapshot CCMS;
- controllo lingua;
- controllo unità XLIFF;
- coerenza della sorgente/stale source.

**v0.25 — Workflow & Job Management**
- stati persistenti del job;
- macchina a stati da inviare -> inviato -> in traduzione -> ricevuto -> QA -> approvato.

### Desktop / commercial hardening

**1.0 RC1 / RC1.2–RC1.10**
- chiusura del flusso locale;
- numerosi fix avvio/reopen Windows;
- gestione porte/stale server;
- riapertura da icona;
- persistenza progetto.

**Desktop R5**
- startup hardening;
- runtime portabile / formatter defaults.

**Desktop R6**
- Grounded Authoring hardening;
- retrieval IT/EN;
- relevance gate;
- conflitti richiesta/fonte;
- guardrail per rewrite/summary/procedure/paragraph.

**Desktop R9**
- fix `QA_BLOCKED -> Riesegui QA` usando l'XLIFF corretto.

**Desktop R10**
- Commercial UX + Workflow;
- versione leggibile in UI.

**Desktop R11**
- Readiness Reconciliation;
- eliminazione divergenze CAT/Work Package vs Readiness.

**Desktop R12**
- ulteriore allineamento readiness -> work package.

**Desktop R13**
- Release Isolation & CAT Safety;
- manifest/checksum;
- rimozione riferimenti tecnici non destinati al cliente.

**Desktop R14**
- DOCX Quality Gate;
- fix newline/heading;
- rimozione carattere problematico U+FFED.

**Desktop R15**
- Production/Test Release Safety + UX;
- distinzione Produzione / Collaudo;
- gate Produzione.

**Desktop R16**
- Friendly Fail-Closed Feedback;
- blocchi di Produzione più comprensibili.

**Desktop R17**
- Atomic Preflight & Immutable ID;
- release ID immutabile;
- snapshot scritto solo dopo il superamento dei gate.

**Desktop R18**
- Language Canonicalization & Persistent State;
- canonicalizzazione `it-IT`, `it_it`, `it-it` ecc.;
- riduzione falsi `MISSING`;
- baseline core successivamente congelata.

### UI sopra core R18

**R21 — UX Final Candidate**
- core R18 congelato;
- Quick Actions;
- `+ Nuovo progetto`;
- backup/ripristino più visibili.

**R23 / R23.2 — UI Structure Polish**
- core R18 preservato;
- sidebar unica;
- Home attiva all'avvio;
- stato attivo sincronizzato anche dalle Quick Actions;
- tab-strip tecnica Gradio nascosta;
- overflow/ellipsis Gradio nascosto;
- footer tecnico disabilitato.
- gate UI Final Candidate: **12/12 PASS**.

### Commercial Stabilization

**Commercial Stabilization RC1**
- packaging/installer/pilot hardening;
- emersi bug Windows reali.

**RC2**
- fix path lunghi del setup;
- shortcut Desktop/Start Menu ricreati/validati;
- argomenti legacy RC1.9 eliminati;
- form `Nuovo progetto` reso raggiungibile;
- builder/installer più robusti.

**RC2.1**
- creazione progetto resa idempotente;
- doppio/delayed click non deve creare duplicati o lasciare `processing` bloccato.

**RC2.2**
- linguistic safety cross-language;
- target copiati/quasi copiati fra lingue diverse devono fallire;
- secondo gate indipendente prima di Produzione cliente;
- protected/non-translatable;
- exact repetition reuse;
- alignment policy più prudente.

**RC2.2 Build Gate Fix**
- corretto il self-test che pretendeva versioni `.0` obsolete dei moduli.

**RC2.2 Installer Payload Fix**
- corretto il problema per cui il builder validava il runtime e poi lo modificava prima di creare il setup;
- aggiunti controlli sull'exact installer payload.

**ManualSync 1.0 FINAL CANDIDATE**
- consolidazione RC2.2 + fix packaging/installazione + acceptance;
- artifact sorgente finale recuperabile;
- ultimo gate Windows ancora da eseguire.

---

## 3. Architettura/core già esistente

La Final Candidate recuperata è una codebase Python desktop/local-first composta da moduli separati, non un semplice script monolitico.

### Lifecycle / persistence

- `project_lifecycle.py`
  - `create_project`
  - `read_project`
  - `update_project`
  - `project_health`

### Repository / CCMS

- `ccms_industrial.py`
  - `IndustrialRepository`
- `authoring_ccms.py`
  - `ContentRepository`
  - import PDF
  - export HTML
  - componentizzazione/repository

### Parsing / layout / revision intelligence

- `layout_engine.py`
  - estrazione layout;
  - blocchi testo/tabelle/immagini;
  - allineamento gerarchico;
  - confronto vecchio/nuovo;
  - technical values/codes;
  - allineamento tabelle/righe;
  - review queue;
  - detection lingua.

### Publishing

- `publishing_bridges.py`
  - export publication DOCX;
  - import DOCX outline;
  - patch template IDML;
  - import outline da IDML.

### Grounded Authoring / generative safety

- `grounded_authoring.py`
- `generative_orchestrator.py`
  - evidence;
  - provider abstraction;
  - source composer;
  - generation ledger.

### CAT / TM / matching

- `cat_tm.py`
  - Translation Memory;
  - segmentazione;
  - technical signatures;
  - exact/context/fuzzy;
  - terminologia;
  - XLIFF;
  - CAT pipeline.
- `cat_workspace.py`
- `cat_batch.py`

### Translation impact/work package/round-trip

- `translation_impact.py`
- `translation_work_package.py`
- `translation_roundtrip.py`

### Workflow

- `workflow_management.py`
  - `WorkflowManager`

### Readiness / release

- `release_readiness.py`
- `release_manager.py`

### Asset/economics

- `asset_intelligence.py`
- `impact_economics.py`

### UI

- `app.py` = UI commerciale principale;
- `app_engineering.py` = funzioni/strumenti engineering e callback avanzate;
- UI Gradio customizzata.

### Desktop / Windows

- `manualsync_desktop.py`
- `manualsync_launcher.py`
- `environment_guard.py`
- `BUILD_WINDOWS.ps1`
- `INSTALLA_MANUALSYNC.ps1`
- `VERIFICA_INSTALLAZIONE_PILOTA.ps1`
- `DISINSTALLA_MANUALSYNC.ps1`

---

## 4. Capability AVAILABLE

> `AVAILABLE` qui significa: implementazione presente nella Final Candidate e coperta da codice/test o già osservata durante il pilot. Non significa automaticamente “commercialmente congelata”: il gate Windows finale della Final Candidate resta mancante.

| Capability | Stato | Evidenza / origine | Invariant principale |
|---|---|---|---|
| Creazione progetto | AVAILABLE | `project_lifecycle.py`; RC2.1 idempotency; acceptance | niente duplicati; manifest valido |
| Apertura/progetto attivo | AVAILABLE | lifecycle/UI; pilot Windows | project state persistente |
| Persistenza | AVAILABLE | R18; `ManualSyncData`; pilot reopen | aggiornamenti non cancellano `ManualSyncData` |
| Repository/CCMS | AVAILABLE | `IndustrialRepository`, `ContentRepository` | schema/data invariants R18 |
| Import PDF | AVAILABLE | `import_pdf_as_topics`; pilot reale | source provenance mantenuta |
| Parsing topic/blocchi/tabelle | AVAILABLE | layout + CCMS; pilot | niente perdita silenziosa dei dati tecnici |
| Approvazione umana sorgente | AVAILABLE | UI/workflow; pilot | nessuna approvazione implicita |
| Grounded Authoring core | AVAILABLE | v0.19/R6; regression suite | evidence/relevance/conflict fail-safe |
| Retrieval | AVAILABLE | R6 / GroundingStore | fonti approvate |
| Relevance gate | AVAILABLE | R6 | richieste non supportate non vanno trattate come grounded |
| Conflict detection | AVAILABLE | R6 / generative guardrails | conflitti devono essere segnalati |
| Revision/change intelligence | AVAILABLE | layout engine + regression/benchmark | cambi tecnici non nascosti da similarità testuale |
| Auto-allineamento old source -> old translation | AVAILABLE | CAT/layout; Final Candidate benchmark | confidence separata dal fuzzy score |
| Exact/context/fuzzy matching | AVAILABLE | `cat_tm.py` | technical mismatch ha precedenza |
| Translation Impact | AVAILABLE | v0.22 | 4 bucket semantici stabili |
| Protected/non-translatable | AVAILABLE | RC2.2 / FC | non deve gonfiare work package |
| Exact repetition reuse | AVAILABLE | RC2.2 / FC | ripetizioni non ritradotte inutilmente |
| Translation Work Package | AVAILABLE | v0.23 | AUTO_REUSE fuori dal lavoro |
| XLIFF 2.0 | AVAILABLE | work package/CAT | lingua/id/segmenti coerenti |
| Round-trip QA | AVAILABLE | v0.24 | checksum/snapshot/stale/wrong language |
| QA rerun | AVAILABLE | R9 | riusa l'XLIFF corretto |
| Workflow/job | AVAILABLE | v0.25 | state machine auditata |
| TM confermata | AVAILABLE | `cat_tm.py` / roundtrip commit | solo contenuto approvato |
| Readiness | AVAILABLE | R11/R12/R18 | coerente con work package/workflow |
| Release collaudo | AVAILABLE | R15+ | snapshot/checksum |
| Release Produzione | AVAILABLE | R15-R17 + RC2.2 | fail-closed |
| Linguistic safety cross-language | AVAILABLE nel sorgente FC | RC2.2 | copied/near-copied target bloccato |
| Independent Production linguistic gate | AVAILABLE nel sorgente FC | RC2.2 | Produzione ricontrolla indipendentemente |
| Backup progetto | AVAILABLE | workflow/commercial acceptance | manifest/hash |
| Restore transazionale | AVAILABLE | commercial stabilization | no merge parziale/corrotto |
| Diagnostics/support | AVAILABLE | `support_diagnostics.py`; acceptance | integrità locale verificabile |
| Publishing DOCX | AVAILABLE | `export_publication_docx`; release pilot | DOCX quality gate R14/R15 |
| Sidebar unica / active state | AVAILABLE | R23/R23.2 | Quick Actions e sidebar sincronizzati |
| Footer/tab tecnici nascosti | AVAILABLE | R23.2 | nessun elemento Gradio tecnico esposto |
| Shortcut validation logic | AVAILABLE nel sorgente FC | RC2 -> FC | target/args/cwd corretti |
| Installer rollback/preserve data | AVAILABLE nel sorgente FC | RC2/FC | `ManualSyncData` preservata |
| Exact payload verification | AVAILABLE nel sorgente FC | payload fix/FC | artifact testato = artifact consegnato |
| Alignment benchmark industriale | AVAILABLE come benchmark | FC | non convertirlo in garanzia commerciale |

### Benchmark già incluso

Pre-allineamento vecchio originale -> vecchia traduzione, sui corpus comparabili inclusi:

- coverage aggregata: **98,3%**
- blocchi sorgente con confidence >=80: **93,3%**

È un benchmark controllato, non una garanzia universale.

---

## 5. Capability PARTIAL

| Capability | Stato | Limite attuale |
|---|---|---|
| Import DOCX | PARTIAL | `import_docx_outline()` esiste nel backend; onboarding/round-trip Word professionale non è completo |
| Authoring professionale | PARTIAL | repository/componenti/grounded authoring esistono, ma manca un Authoring Studio maturo e intuitivo |
| Alignment Review manuale | PARTIAL | confidence/review queue presenti; editor visuale remap/merge/split 1:N / N:1 non completo |
| Translation Intelligence “best in class” | PARTIAL | exact/context/fuzzy/technical safety esistono; mancano benchmark di precision/recall sufficienti e UX specialistica completa |
| CAT workspace/batch | PARTIAL lato prodotto | backend presente; non è ancora una UX commerciale unificata |
| IDML/InDesign | PARTIAL | template bridge disponibile; non ricostruisce/layouta universalmente documenti arbitrari |
| Publishing/layout completo | PARTIAL | DOCX sì; reflow/overset/tabelle/figure/TOC/cross-reference/preflight universale non completo |
| Asset intelligence | PARTIAL | moduli backend esistenti, integrazione prodotto/UX non consolidata |
| Impact economics | PARTIAL | moduli presenti; dashboard ROI commerciale completa non consolidata |
| Zero-training UX | PARTIAL | UI R23.2 è migliorata, ma la Product Doctrine stessa la considera ancora obiettivo prioritario |
| Legacy onboarding multi-formato | PARTIAL | PDF e moduli esistenti; auto-detect massivo Word/PDF/IDML/cartelle non completo |
| Windows install/reopen della Final Candidate | PARTIAL / NON VERIFICATO | logica presente e versioni precedenti testate; exact FC non ancora collaudata sul Windows reale |

---

## 6. Capability ROADMAP

- Authoring Studio completo per manuali/procedure/schede tecniche.
- editor visuale di Alignment Review con remap/merge/split.
- Translation Intelligence 3.0 con benchmark precision/recall per fasce.
- provider-neutral translation execution con connettori CAT/TMS/API.
- AI/MT opzionale con credenziali del cliente.
- Publishing/Layout Engine professionale:
  - DOCX/DOTX template mapping avanzato;
  - IDML/InDesign robusto;
  - PDF finale;
  - HTML;
  - reflow;
  - overset;
  - tabelle native;
  - figure;
  - caption;
  - cross-reference;
  - TOC/index;
  - preflight.
- datasheet engine da dati tipizzati.
- connettori PIM/PLM/ERP/DAM/CCMS.
- onboarding legacy guidato e batch.
- multiutente/cloud/on-prem enterprise.
- ruoli/permessi/SSO.
- API/webhook.
- benchmark UX “zero training”.
- performance engineering su corpus grandi.
- interoperabilità profonda/coexistence con sistemi già presenti.

---

## 7. NON-REGRESSION INVARIANTS

Questi invariant derivano dalle release effettivamente conquistate e non devono essere rotti senza una decisione esplicita, migration plan e regression coverage.

### Core / dati

1. **Core R18 è baseline stabile.**
   - non riscriverlo arbitrariamente;
   - ogni modifica deve essere giustificata e regressione-coperta.

2. **Language canonicalization.**
   - varianti come `it-IT`, `it_it`, `it-it` devono convergere al codice canonico;
   - evitare falsi `MISSING`.

3. **Persistent state.**
   - progetto attivo e stato devono sopravvivere al reopen;
   - update standard non deve eliminare `ManualSyncData`.

4. **Project create idempotency.**
   - un doppio/delayed click non crea un duplicato;
   - progetto esistente viene riaperto in sicurezza.

5. **Backup/restore transazionale.**
   - manifest e checksum obbligatori;
   - nessun merge in target non vuoto;
   - restore fallito non lascia progetto parziale;
   - SQLite corrotta deve essere rifiutata.

### Grounded Authoring

6. **Evidence-grounded behavior.**
   - retrieval/relevance/conflict guardrail non vanno bypassati.

7. **Conflicting/unsupported technical claims non devono diventare contenuto approvato automaticamente.**

### Translation / CAT

8. **Translation Impact buckets restano semanticamente distinti:**
   - `AUTO_REUSE`
   - `LINGUISTIC_REVIEW`
   - `TECHNICAL_REVIEW`
   - `NEW_TRANSLATION`

9. **Technical safety prevale sul fuzzy score.**
   - numeri/codici/unità/placeholder incompatibili non possono essere auto-riusati.

10. **Alignment confidence non è fuzzy score.**

11. **Alignment confidence policy FC:**
   - confidence >=90: può essere eligible per zero-penalty reuse;
   - 80–89: suggestion/review, non auto-promozione cieca;
   - pair tecnicamente unsafe: esclusa.

12. **Protected/non-translatable handling.**
   - codici/simboli/placeholder/non traducibili non devono gonfiare il work package.

13. **Exact repetition reuse.**
   - ripetizioni identiche non devono generare lavoro duplicato.

14. **Work Package safety.**
   - `AUTO_REUSE` non entra nel workfile come normale nuova traduzione;
   - technical review resta esplicita.

15. **XLIFF round-trip safety.**
   - checksum;
   - snapshot CCMS;
   - language check;
   - unit consistency;
   - stale source rejection;
   - wrong-language rejection;
   - source immutability.

16. **QA rerun usa il corretto XLIFF.**

17. **Solo il contenuto approvato aggiorna TM/CCMS.**

18. **Cross-language linguistic fail-closed.**
   - copied/near-copied target tra lingue diverse deve essere bloccato.

19. **Produzione esegue un gate linguistico indipendente.**
   - un errore sfuggito a un gate precedente non deve automaticamente arrivare a Produzione.

### Workflow / release

20. **Workflow state machine auditata.**
   - niente transizioni inverse/non consentite silenziose;
   - invalid transition deve essere gestita senza corrompere stato.

21. **Readiness/work-package/workflow consistency.**

22. **Release isolation.**
   - artifact cliente isolato dagli output tecnici/test.

23. **Production/Test separation.**
   - Collaudo interno non equivale a Produzione cliente.

24. **Fail-closed Produzione.**
   - unresolved QA/readiness/safety issue = niente pacchetto cliente.

25. **Atomic preflight.**
   - nessun artifact/snapshot finale deve essere scritto come valido prima dei gate.

26. **Immutable release identity.**
   - release ID non deve essere riutilizzato/corrotto.

27. **Snapshot immutabili + checksum negli artifact release.**

28. **DOCX quality gate.**
   - mantenere validazioni introdotte R14/R15.

### UI

29. **Sidebar unica.**
30. **Home attiva all'avvio.**
31. **Quick Actions sincronizzano lo stato active della sidebar.**
32. **Tab-strip tecnica Gradio resta nascosta.**
33. **Overflow ellipsis tecnico resta nascosto.**
34. **Footer tecnico Gradio resta disabilitato.**
35. **Version/build e core baseline restano diagnosticabili.**

### Windows / packaging

36. **Shortcut Desktop/Start Menu devono essere realmente validati.**
   - target = `ManualSync.exe`;
   - args legacy vuoti;
   - working directory corretta.

37. **Rollback installazione deve preservare la versione precedente in caso di FAIL.**

38. **Installer/uninstaller non cancellano `ManualSyncData` durante operazioni standard.**

39. **No path explosion nel setup cliente.**

40. **Exact-artifact principle.**
   - il payload validato deve essere lo stesso payload inserito nello ZIP cliente.

41. **Exact customer ZIP deve essere riestratto e testato prima di `BUILD_STATUS=PASS`.**

42. **Builder deve rimuovere/storicizzare stale PASS/release prima di una nuova build.**

43. **Nessuna pulizia ricorsiva deve alterare il runtime dopo averlo validato.**

44. **Runtime product = Python 3.12 x64.**

45. **Launcher non deve dipendere da `.pyc/__pycache__` residui né produrre bytecode necessario al funzionamento.**

---

## 8. Test/suite/gate già eseguiti

### Final Candidate — dati ufficiali recuperati

`PREVALIDATION_FINAL_CANDIDATE.json`:

- RC2.2 regression baseline: **357/357 PASS**
- Final Candidate static gate: **29/29 PASS**
- Commercial Acceptance: **15/15 PASS**
- Alignment confidence policy: **5/5 PASS**
- Real industrial alignment gate: **8/8 PASS**
- `py_compile`: **PASS**

Runtime del container di prevalidazione:

- **36/37**
- unico FAIL: Python 3.13.5 invece di Python 3.12 x64

### R23.2 UI

- **12/12 PASS**

Controlli dichiarati dalla suite:
- footer disabilitato;
- Home inizialmente attiva;
- stato active dinamico;
- tutte le sezioni sidebar partecipano allo stato;
- Quick Actions sincronizzate;
- stile active;
- tab-strip nascosta;
- overflow ellipsis nascosto;
- core release invariato collegato;
- canonicalizzazione lingua collegata;
- app import;
- brand centralizzato.

### Commercial Acceptance — 15/15

Copre almeno:
- project create;
- project create idempotency;
- translation package;
- job create;
- invalid transition safely guided;
- round-trip QA;
- human approval commit;
- readiness;
- internal release;
- valid production release;
- project backup;
- diagnostics;
- transactional restore;
- production readiness linguistic fail-closed;
- production linguistic fail-closed.

### Gate Windows finale della Final Candidate

**NON ESEGUITO / NON VERIFICATO.**

È il blocker principale prima del Prompt 0 se si vuole che Prompt 0 descriva la baseline commerciale reale e non solo quella sorgente.

---

## 9. Problemi Windows/UX ancora aperti

### Problemi storici reali, poi corretti nel sorgente successivo

**Packaging/path**
- RC1: estrazione ZIP standard da Download falliva con `0x80010135 Percorso troppo lungo`.
- Stato Final Candidate: fix e path guard presenti.
- Final Candidate Windows: **NON VERIFICATO**.

**Shortcut**
- RC1: shortcut conservava vecchi argomenti RC1.9 e non avviava l'app.
- RC2: target/args/cwd ricreati e validati.
- Final Candidate Windows: **NON VERIFICATO**.

**Nuovo progetto**
- RC1: `+ Nuovo progetto` apparentemente non faceva nulla.
- RC2: form portato in viewport.
- RC2.1: doppio/delayed create reso idempotente.
- Final Candidate: regression-covered.
- Final Candidate Windows: **NON VERIFICATO**.

**Linguistic QA**
- RC2.1: falso target IT copiato in `en-US` superava QA e Produzione.
- RC2.2/FC: fix fail-closed.
- Final Candidate Windows reale: **NON VERIFICATO**.

**Build gate**
- RC2.2: 32/35 per version guard obsoleto.
- fix sorgente successivo: corretto.
- FC prevalidation: pass sorgente.

**Installer payload**
- una RC2.2 costruita PASS fallì poi in installazione con `Resource check exit 1`, perché il builder validava prima e alterava il payload dopo.
- Payload Fix/FC: exact-artifact verification aggiunta.
- Final Candidate Windows reale: **NON VERIFICATO**.

### UX ancora insufficiente / da verificare

**Zero-training**
- Product Doctrine V4 la considera ancora roadmap prioritaria.
- UI R23.2 è più pulita ma non dimostra ancora onboarding senza formazione.

**Readiness/Release**
- nel pilot il pulsante release disabilitato finché non veniva eseguita readiness risultava poco intuitivo.
- comportamento funzionale corretto, UX ancora migliorabile.

**Vendor**
- durante il pilot un Vendor ID non registrato generò confusione; il job funzionò lasciando Vendor ID vuoto.
- qualità del feedback su vendor inesistente nella Final Candidate: **NON VERIFICATO**.

**Placeholder/righe `1 2 3` nella UI Contenuti**
- osservato nel pilot come difetto cosmetico/placeholder.
- stato nella Final Candidate: **NON VERIFICATO**.

**Help/onboarding contestuale**
- non dimostrato come sufficiente.
- ROADMAP/UX.

---

## 10. Artifact e file che devo recuperare

### PRIORITÀ A — codice baseline

**1. `ManualSync_1.0_FINAL_CANDIDATE_Full_BuildKit.zip`**

SHA-256:

`21c411ca5e1a0a136dbe47acfb42f167f573618b5f760ff62d4d290daaeb0821`

Contiene la Final Candidate sorgente completa, 92 file.

### PRIORITÀ B — handoff precedente

**2. `ManualSync_CLAUDE_HANDOFF_V4.zip`**

SHA-256:

`c03729ef88fb30bf348b87881628f20e1baf0dbd876439998444955da7c102db`

Lo ZIP è stato realmente creato ed è recuperabile.

### PRIORITÀ C — documenti V4

- `ManualSync_Guida_Prodotto_Completo_V4.docx`
- `ManualSync_Product_Doctrine_V4.md`
- `ManualSync_Prompt_Pack_Claude_V4.md`

### File interni Final Candidate da preservare

- `00_START_HERE_FINAL_CANDIDATE.txt`
- `PREVALIDATION_FINAL_CANDIDATE.json`
- `PREVALIDATION_FINAL_CANDIDATE.txt`
- `SOURCE_MANIFEST_SHA256.json`
- `VERSION.txt`
- regression suite;
- benchmark industriali;
- requirements lock;
- builder/installer/validator.

### Artifact da NON considerare baseline commerciale

- `dist/` vecchi;
- `release/` vecchie;
- `installer_stage/` vecchi;
- `ManualSync_Setup_RC2*`;
- `ManualSync_Portable_RC2*`;
- XLIFF di test fittizi;
- build RC1/RC2.x obsolete.

---

## 11. Stato di `ManualSync_CLAUDE_HANDOFF_V4.zip`

### 1. Era stato realmente creato?

**SÌ. VERIFICATO.**

File recuperabile:

`ManualSync_CLAUDE_HANDOFF_V4.zip`

SHA-256 verificato:

`c03729ef88fb30bf348b87881628f20e1baf0dbd876439998444955da7c102db`

Il checksum coincide con `ManualSync_CLAUDE_HANDOFF_V4.sha256.txt`.

### 2. Il suo contenuto era stato definito?

**SÌ. VERIFICATO.**

Lo ZIP contiene **104 file**.

### 3. Era stato consegnato?

**SÌ.**
La conversazione lo aveva esposto come artifact scaricabile.

**Se sia stato effettivamente scaricato sul nuovo PC:** `NON VERIFICATO`.

### 4. Conteneva sorgenti o soltanto documentazione?

**CONTENEVA ENTRAMBI.**

Contiene l'intera cartella:

`ManualSync_1.0_FINAL_CANDIDATE/`

più documentazione/handoff.

### 5. Conteneva la Final Candidate?

**SÌ. VERIFICATO.**

Sono stati confrontati tutti i **92 file** della Final Candidate standalone contro i 92 file presenti nel subfolder dell'handoff V4:

**hash identici per tutti i file**.

### 6. Può essere rigenerato esattamente?

L'artifact originale è ancora recuperabile, quindi non serve rigenerarlo.

Se fosse perso di nuovo, il suo contenuto è ricostruibile dai file attualmente recuperati; tuttavia un nuovo ZIP non sarebbe necessariamente byte-for-byte identico a causa di metadata/timestamp ZIP.

Per conservare la provenienza, usare l'artifact originale con SHA noto.

### 7. Esiste un artifact equivalente più recente?

**NO per il codice.**
V4 è l'ultimo handoff della conversazione che contiene la stessa Final Candidate.

Esistono V3/V2 più vecchi, ma non devono sostituire V4.

---

## 12. Incongruenze V3/V4 da risolvere

### A. Prompt Pack V4 con header interno V3

`ManualSync_Prompt_Pack_Claude_V4.md` inizia ancora con:

`# ManualSync - Claude Prompt Pack V3`

e Prompt 0 dice:

`Read the full V3 handoff...`

Questa è una **incongruenza documentale reale** generata dall'aggiornamento incrementale V3 -> V4.

Non significa che il codice Final Candidate sia V3.

**Azione:** documentarla nel baseline. Non correggerla prima del recupero/commit; successivamente fare una normalizzazione documentale tracciata.

### B. Product Doctrine V4 con header interno V3

`ManualSync_Product_Doctrine_V4.md` inizia con:

`# ManualSync Product Doctrine V3`

ma contiene anche la nuova `Objection rule` aggiunta in V4.

Quindi è **contenuto V4 con intestazione legacy V3**.

### C. README handoff V4

Il README interno comincia con:

`# ManualSync Claude Handoff V3`

poi aggiunge una sezione `V4 addition`.

Quindi V4 è stato assemblato incrementalmente a partire da V3.

### D. Guide PDF vs DOCX

Esistono entrambe in File Library:

- `ManualSync_Guida_Prodotto_Completo_V4.docx`
- `ManualSync_Guida_Prodotto_Completo_V4.pdf`

Ma **NON devono essere considerate equivalenti**.

La copia PDF recuperata ha **10 pagine**; il DOCX V4 finale ha **12 pagine** e contiene la sezione estesa sulle obiezioni commerciali.

**Decisione canonica consigliata:**
`ManualSync_Guida_Prodotto_Completo_V4.docx` = source of truth V4.

Il PDF attuale = render precedente/stale; archiviarlo ma non usarlo come documento canonico.

### E. Quale versione governa?

- **Codice corrente:** Final Candidate 20260822.
- **Visione/prodotto corrente:** Product Doctrine V4 + Guide V4.
- **Prompt operativo:** Prompt Pack filename V4, con riferimenti V3 da correggere in una milestone documentale controllata.
- **Handoff:** V4.

---

## 13. Cosa deve essere copiato nel nuovo repository

### Copiare prima del Prompt 0

1. La cartella **intera e immutata**:
   `ManualSync_1.0_FINAL_CANDIDATE/`

   Posizionamento consigliato per non rompere i riferimenti del Prompt Pack:
   **root del nuovo repository**.

2. In `docs/product/` mantenere come documenti canonici:
   - `ManualSync_Guida_Prodotto_Completo_V4.docx`
   - `ManualSync_Product_Doctrine_V4.md`
   - `ManualSync_Prompt_Pack_Claude_V4.md`

3. In `docs/reports/` aggiungere:
   - `MANUALSYNC_PREVIOUS_WORK_HANDOFF.md`
   - una nota di recovery con:
     - SHA Final Candidate;
     - SHA Handoff V4;
     - data recovery;
     - risultato verifica.

4. Preservare all'interno della Final Candidate:
   - prevalidation;
   - source manifest;
   - VERSION;
   - regression suite;
   - benchmark fixtures;
   - builder/installer.

### Non migrare ancora i file Python dentro `src/manualsync/`

Prima del Prompt 0 **non trasformare** la vecchia codebase nella nuova struttura.

Il Prompt 0 deve prima mappare dipendenze/import, test, runtime e rischi di migrazione.

---

## 14. Cosa NON deve essere copiato

Non portare nella baseline Git come codice corrente:

- RC1;
- RC2;
- RC2.1;
- RC2.2 pre-fix;
- Build Gate Fix come baseline autonoma;
- Installer Payload Fix come baseline autonoma;
- cartelle `C:\MSRC1`, `C:\MSRC2`, `C:\MSRC21`, `C:\MSRC22`, `C:\MSRC22FIX`, `C:\MSRC22PAYLOADFIX`;
- vecchi `dist/`;
- vecchi `release/`;
- `installer_stage/`;
- `.buildvenv`;
- `__pycache__`;
- `.pyc`;
- log locali;
- `ManualSyncData` personale;
- progetti test/pilot personali;
- XLIFF fittizi di ritorno;
- ZIP setup RC obsolete;
- screenshot;
- vecchi BUILD_STATUS come prova della Final Candidate;
- PDF V4 stale come source of truth;
- V2/V3 doctrine/prompt come documentazione canonica.

Gli artifact vecchi possono essere conservati **fuori dal repository** come archivio storico fino al completamento del primo commit e del Prompt 0.

---

## 15. Rischi di ripartenza con Claude Code

1. **Riscrittura involontaria.**
   Se Claude vede solo lo skeleton nuovo, può trattarlo come progetto vuoto e reinventare il core.

2. **Migrazione prematura in `src/manualsync`.**
   Spostare i moduli prima di mappare import/runtime/build può rompere builder e regression suite.

3. **Confusione Product Vision / Current Product.**
   Doctrine V4 descrive anche roadmap; non usarla come prova di capability implementate.

4. **Confusione V3/V4.**
   Rinominare automaticamente i riferimenti può cancellare traccia della provenienza.

5. **Perdita invariant R18.**
   Refactoring dello schema/lingue/readiness/release senza migration tests può reintrodurre bug già risolti.

6. **Regression suite non eseguita integralmente.**
   Un singolo test PASS non equivale alla baseline.

7. **Windows packaging sottovalutato.**
   Molti bug storici erano visibili solo nell'artifact installato, non nel sorgente.

8. **Artifact mismatch.**
   Testare `dist` e consegnare un payload modificato dopo il test è un errore già realmente avvenuto.

9. **Benchmark interpretato male.**
   98,3% è coverage di pre-allineamento sui fixture comparabili, non accuracy universale.

10. **Commit iniziale incompleto.**
    Fare il primo commit senza la Final Candidate creerebbe una falsa baseline storica.

11. **Commit di binary/output inutili.**
    Dist/release/cache/log gonfierebbero il repository e confonderebbero i source artifact.

12. **Correzioni documentali insieme al recovery.**
    Prima recuperare byte-per-byte; poi correggere V3/V4 con commit separato.

---

## 16. Sequenza esatta consigliata prima del Prompt 0

1. **NON eseguire ancora Prompt 0.**
2. **NON fare ancora il primo commit.**
3. Recuperare:
   - `ManualSync_1.0_FINAL_CANDIDATE_Full_BuildKit.zip`
   - `ManualSync_CLAUDE_HANDOFF_V4.zip`
4. Verificare SHA-256:
   - Final Candidate = `21c411ca5e1a0a136dbe47acfb42f167f573618b5f760ff62d4d290daaeb0821`
   - Handoff V4 = `c03729ef88fb30bf348b87881628f20e1baf0dbd876439998444955da7c102db`
5. Estrarre gli ZIP in una cartella temporanea di recovery, non sovrascrivere subito il repository.
6. Verificare che `ManualSync_CLAUDE_HANDOFF_V4.zip` contenga `ManualSync_1.0_FINAL_CANDIDATE/`.
7. Confrontare il manifest/source hash della Final Candidate.
8. Copiare **immutata** `ManualSync_1.0_FINAL_CANDIDATE/` nella root del nuovo repository.
9. Confermare in `docs/product/` i tre documenti V4 canonici.
10. Considerare il PDF V4 esistente come render stale/non canonico.
11. Copiare questo `MANUALSYNC_PREVIOUS_WORK_HANDOFF.md` in `docs/reports/`.
12. Eseguire `git status` e controllare che:
    - nessun `dist/release/cache/log` sia staged;
    - Final Candidate sorgente e benchmark siano presenti;
    - documenti V4 siano presenti.
13. Configurare l'identità Git.
14. Creare il **primo commit di recovery/baseline** soltanto quando il contenuto è completo.
15. Verificare che il working tree sia pulito.
16. Solo a quel punto autorizzare **Prompt 0** in modalità read-only/baseline.
17. Dopo Prompt 0, riportare qui:
    - baseline report;
    - test PASS/FAIL;
    - architecture map;
    - `git status`;
    - eventuali blocker.
18. Questa chat decide se il freeze è accettabile prima di Prompt 1.

---

## 17. Informazioni che non posso ricostruire con certezza

### `BUILD_STATUS.json` della Final Candidate sul nuovo/vecchio Windows
**NON VERIFICATO.**
La Final Candidate non risulta essere stata costruita e validata sul Windows reale prima del cambio di metodo.

### `ManualSync_Setup_FINAL_CANDIDATE.zip` prodotto sul Windows dell'utente
**NON VERIFICATO / DA GENERARE.**
È il nome previsto dal BuildKit, non un installer Final Candidate già certificato.

### Dist e installer Final Candidate già eseguiti su Windows
**NON VERIFICATO.**

### Stato esatto del PDF V4 rispetto al DOCX V4 finale
**VERIFICATO CHE NON SONO LA STESSA REVISIONE DI CONTENUTO/PAGINAZIONE.**
PDF recuperato = 10 pagine; DOCX V4 finale = 12 pagine.
Non usare il PDF come source of truth.

### Download effettivo dello ZIP Handoff V4 sul nuovo PC
**NON VERIFICATO.**
L'artifact originale è però recuperabile da questa conversazione.

### Completezza della UX `Vendor ID` in Final Candidate
**NON VERIFICATO.**
Nel pilot l'uso di un vendor inesistente causò confusione.

### Difetto visuale `1 2 3` nella pagina Contenuti
**NON VERIFICATO** nella Final Candidate.

### Import DOCX end-to-end dalla UI commerciale
Il backend `import_docx_outline()` esiste.
UX/onboarding Word completo: **PARTIAL / NON VERIFICATO end-to-end**.

### Precisione universale dell'allineamento/fuzzy sui clienti reali
**NON VERIFICATO.**
Esistono benchmark inclusi, non una garanzia universale.

### Commercial readiness
**NON VERIFICATO.**
Manca ancora il Final Windows Gate sull'exact artifact.

---

# Decisione di handoff

La base da trasferire nel nuovo repository **non è una RC2.x**.

È:

**ManualSync 1.0 FINAL CANDIDATE — build `1.0.0-final-candidate-20260822`**

con:

- core R18 stabile;
- UI R23.2;
- commercial safety RC2.2/Final Candidate;
- exact-artifact packaging hardening;
- regression/prevalidation inclusa;
- Final Windows Gate ancora da chiudere.

La Product Doctrine V4 è la **direzione futura**.  
La Final Candidate è il **prodotto corrente da congelare e mappare**.  
Le capability PARTIAL e ROADMAP non devono essere presentate a Claude come già implementate.
