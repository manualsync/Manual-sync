# ZERO_TRAINING_UX_ARCHITECTURE_V1.md — ManualSync Prompt 2 (Zero-training UX)

**Data:** 2026-08-25
**Baseline Git di riferimento:** `1a0a77f67c9d6dc52495336c507d6c365c5e90ae` (Prompt 1 congelato)
**Natura del documento:** progettazione UX/UI. **Nessun codice è stato scritto o
modificato.** Ogni riferimento a moduli/funzioni esistenti è verificato contro il
sorgente reale in `ManualSync_1.0_FINAL_CANDIDATE/` (non contro l'handoff o la dottrina
soltanto), per evitare di progettare su capability inesistenti.

**Fonti rilette integralmente prima di questo documento:** `BASELINE_V3.md`,
`COMPETITIVE_AND_ADOPTION_GAP_V3.md`, `MANUALSYNC_PREVIOUS_WORK_HANDOFF.md`,
`ManualSync_Product_Doctrine_V4.md`. In più, per ancorare le proposte alla UI reale
(non alla sua descrizione), è stato riletto direttamente `app.py` (sidebar, 7 tab,
Home, gestione job/release) invece di fidarsi solo dei riassunti precedenti.

---

## 0. Principio guida

> stato del progetto → problema rilevato → azione consigliata → risultato

Questo sostituisce, come schermata di partenza per l'utente normale, il modello attuale
che è: *sidebar con 7 aree funzionali → dentro ciascuna, form con campi tecnici
(Object ID, kind, TM SQLite, Vendor ID, Release ID, Profilo, azione da dropdown con
nomi come "Ricevi XLIFF + controllo qualità (QA)")*. La UI attuale (R23.2) è ben
organizzata e coerente **come strumento per chi già capisce il dominio**, ma non è
ancora una UX guidata: è un pannello di controllo ordinato, non un flusso.

---

## 1. Stato reale della UI oggi (R23.2) — punto di partenza verificato

Letto direttamente da `app.py`:

- **Sidebar unica** con Home + 5 aree di workflow (Revisione, Contenuti, Traduzioni,
  Job & fornitori, Release) + Impostazioni. Stato attivo sincronizzato anche dalle
  Quick Actions della Home (`_navigate()`, `_NAV_IDS`).
- **Home** con card di progetto, pulsante "+ Nuovo progetto" che apre un form inline
  (non un wizard multi-step), e 3 "Azioni rapide" che sono in realtà scorciatoie di
  navigazione, non azioni contestuali basate sullo stato del progetto.
- **Ogni tab espone direttamente i parametri tecnici sottostanti**: es. in
  "Traduzioni" l'utente sceglie `Tipo` (publication/topic/block/table/figure/
  component) e inserisce a mano un `Object ID`; in "Job & fornitori" l'azione da
  compiere è un dropdown con voci come *"Ricevi XLIFF + controllo qualità (QA)"*; in
  "Release" compaiono `Release ID`, `Profilo`, upload diretto di un `Template IDML`.
- **Esistono già germi del pattern "prossimo passo"**: `_job_state_hint()` traduce lo
  stato di un job in un suggerimento testuale ("prossimo passo: **Segna inviato**"),
  e `_release_mode_help()`/`_friendly_*` funzioni già "ripuliscono" alcune tabelle
  grezze per la UI. Sono frammenti coerenti col principio guida, ma **isolati per
  singola entità**, non un motore unificato a livello di intero progetto.
- **Nessun concetto di ruolo esiste oggi**: la UI è unica e identica per chiunque la
  apra. Non c'è distinzione tra ufficio tecnico, technical writer, reviewer,
  responsabile traduzioni, responsabile release.
- **Nessuna modalità "avanzata" separata esiste oggi**: tutti i campi tecnici sono
  sempre visibili a tutti.

Questo è il punto di partenza reale, non un punto di partenza ipotetico.

---

## 2. Gestione dei ruoli

### 2.1 Ruoli e cosa devono vedere

| Ruolo | Cosa vede come priorità | Cosa NON deve vedere in primo piano |
|---|---|---|
| **Ufficio tecnico / project owner** | Home con stato di tutti i progetti, azione consigliata per ciascuno, accesso a creazione progetto e import legacy | Dettagli di singolo pacchetto XLIFF, dropdown QA tecnici |
| **Technical writer** | Progetto attivo → cosa importare/scrivere → cosa è stato rilevato (contenuto nuovo, riusabile, in conflitto) | Object ID grezzi, gestione vendor/job, Release ID |
| **Reviewer** | Coda di cose da approvare (sorgenti importate, proposte di redazione grounded, traduzioni QA-pronte) con causa+azione per ciascuna | Creazione progetto, configurazione fornitori, publishing |
| **Responsabile traduzioni** | Stato pacchetti lingua, job attivi, blocchi QA con causa+rimedio, fornitori | Redazione contenuti, alignment/revision engine interno |
| **Responsabile release** | Readiness matrix semplificata, gate mancanti con azione richiesta, differenza Collaudo/Produzione | Dettaglio tecnico di allineamento/matching, redazione |

### 2.2 Come si implementa senza riscrivere R18

Non esiste oggi un concetto di permessi/ruolo nel backend (nessun modulo di
autenticazione o autorizzazione in `BASELINE_V3.md` — coerente con "l'enterprise
architecture viene dopo la maturità del core"). La proposta per questa fase è quindi
**un ruolo scelto localmente dall'utente all'apertura del progetto (non un sistema di
permessi enforced)**: una preferenza di visualizzazione, salvata come stato UI, che
filtra quali sezioni della Home e quali scorciatoie sono in primo piano. Non blocca
l'accesso alle altre aree (restano raggiungibili dalla sidebar in modalità avanzata) —
evita di dover costruire autenticazione/permessi reali prima che il core sia maturo,
come richiesto dalla Product Doctrine.

---

## 3. Primo avvio

**Stato:** nessun progetto esiste ancora (`INITIAL_PROJECT` è vuoto/non risolto).

Schermata proposta (sostituisce l'attuale Home con card vuota + pulsante):

```
┌─────────────────────────────────────────────────────┐
│  ManualSync                                          │
│                                                        │
│   Benvenuto. Non hai ancora un progetto.              │
│                                                        │
│   [ Crea il tuo primo progetto ]                      │
│   [ Ho già dei manuali: fammeli vedere ]               │
│                                                        │
│   Non serve sapere cosa sono XML, DITA o XLIFF         │
│   per iniziare.                                        │
└─────────────────────────────────────────────────────┘
```

Due percorsi distinti fin dal primo secondo, perché rispondono a due obiezioni diverse:
- "Crea il tuo primo progetto" → utente senza materiale pregresso o che vuole partire
  pulito.
- "Ho già dei manuali: fammeli vedere" → risponde direttamente a "lavoriamo con
  Word/InDesign/abbiamo solo PDF/non vogliamo migrare tutto" portando subito
  all'onboarding legacy (§4), non a un form vuoto.

---

## 4. Onboarding nuovo progetto

### 4.1 Oggi
Form singolo inline con 8 campi tecnici simultanei (nome, cliente, prodotto, codice
manuale, revisione, lingua sorgente, lingue target, responsabile) — tutti obbligatori
prima di poter fare qualunque cosa.

### 4.2 Proposto — wizard a passi, con l'import legacy come primo passo reale

```
Passo 1/3 — Cosa vuoi portare in ManualSync?
  ( ) Un PDF che ho già
  ( ) Un documento Word che ho già
  ( ) Parto da zero
  [Continua]

Passo 2/3 — Info minime (nome progetto, lingua del documento)
  Nome progetto: [________]
  Lingua del documento: [rilevata automaticamente, modificabile]
  [Continua]

Passo 3/3 — Conferma
  "Ho creato il progetto e importato il tuo documento.
   Ho trovato 42 sezioni. Ecco cosa fare adesso."
  [Vai al progetto]
```

Campi oggi obbligatori nel form (cliente, prodotto, codice manuale, revisione, lingue
target multiple, responsabile) diventano **opzionali, compilabili dopo, da
Impostazioni** — non bloccano il primo risultato utile. Questo risponde direttamente a
"non abbiamo tempo di imparare un altro programma": il tempo-al-primo-valore non deve
dipendere da quanti metadati amministrativi l'utente inserisce.

### 4.3 Collegamento a backend reale
- Creazione progetto: `project_lifecycle.create_project` (AVAILABLE, già canonicalizza
  lingua — R18 invariant #2).
- Import PDF: `authoring_ccms.import_pdf_as_topics` (AVAILABLE).
- Import Word: `publishing_bridges.import_docx_outline` (PARTIAL — backend esiste, non
  ha oggi un percorso UI dedicato: è la funzione mancante più urgente per questo
  wizard, vedi §13).
- "Parto da zero": crea solo il progetto vuoto, nessun import.

---

## 5. Home / dashboard — "Recommended Next Action"

### 5.1 Principio
La Home diventa il punto in cui il "prossimo passo" oggi calcolato solo per i job
(`_job_state_hint`) viene **generalizzato a livello di intero progetto**: un motore che
guarda lo stato reale (via `project_lifecycle.project_health`, `release_readiness.
assess_release_readiness`, stato dei job da `WorkflowManager`) e produce **una singola
azione consigliata**, non un elenco di funzioni disponibili.

```
┌─────────────────────────────────────────────────────┐
│  Progetto: Manuale ACS880 — IT → EN, DE               │
│                                                        │
│   Cosa fare adesso                                     │
│   ──────────────                                       │
│   Ho trovato 3 sezioni con contenuto tecnico           │
│   cambiato rispetto alla versione precedente.           │
│   Vanno revisionate prima di tradurre.                  │
│                                                        │
│   [ Rivedi le 3 sezioni ]                               │
│                                                        │
│   Altre cose in sospeso (2)              [mostra tutto] │
└─────────────────────────────────────────────────────┘
```

- **Una** azione primaria, non tre "Azioni rapide" equivalenti come oggi.
- Le "altre cose in sospeso" restano accessibili ma non competono visivamente con
  l'azione raccomandata (oggi le 3 card Home hanno pari peso visivo).
- Se non c'è nulla di bloccante: la Home mostra lo stato positivo ("Tutto in ordine,
  puoi procedere a [prossimo passo naturale: es. genera pacchetto lingua]") invece di
  restare silenziosa.

### 5.2 Da dove viene calcolata l'azione (mapping a backend reale)
| Segnale rilevato | Fonte backend reale | Azione consigliata |
|---|---|---|
| Sorgente importata non ancora approvata | `approve_source_ui`/CCMS status | "Approva la sorgente importata" |
| Revisione con modifiche tecniche rilevate | `layout_engine`/`revision_analysis_ui` | "Rivedi le N sezioni cambiate" |
| Nessun pacchetto lingua generato ma target languages presenti | `translation_work_package` | "Genera il pacchetto per [lingua]" |
| Job in stato `IN_TRANSLATION` con XLIFF disponibile | `WorkflowManager` | "Ricevi la traduzione e controlla la qualità" |
| Job `QA_BLOCKED` | `translation_roundtrip` | "Correggi il problema segnalato" (con causa, §7) |
| Readiness con blockers | `release_readiness.assess_release_readiness` | "Risolvi N cose prima di poter rilasciare" |
| Nessun blocco, readiness OK | idem | "Puoi creare la release" |

---

## 6. Modalità guidata (standard) vs modalità avanzata

### 6.1 Cosa resta nascosto in modalità guidata
- Object ID grezzi, tipo/kind tecnico (publication/topic/block/table/figure/component)
- Upload manuale di TM SQLite esterna
- Vendor ID come campo tecnico separato dal nome fornitore
- Release ID manuale, Profilo, upload diretto di Template IDML
- Dropdown di azione job con nomi tecnici ("Ricevi XLIFF + controllo qualità")
- Terminologia "XLIFF", "TM", "QA", "repository", "publication ID" nel testo visibile

### 6.2 Come si accede alla modalità avanzata
Un singolo toggle visibile ma non invasivo nella sidebar/Impostazioni: **"Modalità
avanzata"**. Non un login separato, non un ruolo enforced (coerente con §2.2) — uno
switch di visualizzazione. Quando attivo:
- Riappaiono i campi tecnici oggi già presenti in `app.py` (Object ID, kind, TM
  SQLite, Vendor ID, Release ID, Profilo, IDML template) esattamente come sono oggi —
  **questa parte della UI attuale non va reinventata, va solo resa opzionale invece
  che sempre visibile**.
- La terminologia tecnica (XLIFF, TM, QA, publication ID) torna visibile perché è lo
  strumento di lavoro dello specialista, non un ostacolo per lui.

### 6.3 Perché non due prodotti separati
Il backend è identico in entrambe le modalità (stesse funzioni `*_ui` di `app.py`,
stesso R18). La modalità guidata è **una vista sopra le stesse azioni**, non un
sottoinsieme di funzionalità — nessuna capability reale viene rimossa, solo
riorganizzata e ridenominata.

---

## 7. Gestione di errori e blocchi senza linguaggio tecnico

### 7.1 Oggi
Alcuni messaggi sono già in italiano semplice (es. `_release_mode_help`: "fail-closed.
Marker di collaudo, anomalie linguistiche cross-language... bloccano la release") ma
mescolano linguaggio friendly e termini tecnici nella stessa frase. Altri sono
puramente tecnici (`gr.Error("Project e Job ID sono obbligatori.")`).

### 7.2 Regola proposta: causa + azione, sempre
Ogni blocco mostrato in modalità guidata deve avere questa forma fissa:

```
⚠ [Cosa è successo, in una frase senza gergo]
   Perché: [causa, in termini di dominio del cliente, non di sistema]
   Cosa fare: [azione concreta, con pulsante quando possibile]
   [ dettagli tecnici ▸ ] ← solo se modalità avanzata attiva
```

Esempio concreto, mappato su un caso reale già verificato in `BASELINE_V3.md`
(cross-language linguistic safety fail-closed):

- **Oggi (tecnico):** `QA_BLOCKED` con dettaglio linguistico grezzo.
- **Guidato:**
  > ⚠ Questa traduzione non può essere approvata.
  > Perché: il testo in inglese sembra identico o quasi identico all'italiano — di
  > solito significa che non è stato davvero tradotto.
  > Cosa fare: chiedi al fornitore di rivedere il segmento indicato, poi riprova il
  > controllo qualità.
  > [ Riesegui controllo qualità ]

Questo non richiede modificare la logica di `linguistic_safety.py`/
`translation_roundtrip.py` (che restano invariati, R18/invariant #18 preservato) —
richiede solo un layer di traduzione del messaggio in UI, mappato dal codice di errore
strutturato già presente nei risultati (`hard_reason`, `qa_status`, ecc., già
verificati come strutturati nei test reali).

---

## 8. Percorso utente completo (golden path)

```
Primo avvio
  │
  ▼
"Ho già un PDF" ──────────────► Import PDF (import_pdf_as_topics)
  │                                │
  ▼                                ▼
Progetto creato              "Ho trovato 42 sezioni.
  │                           3 contengono valori tecnici
  │                           che vanno controllati."
  │                                │
  ▼                                ▼
Home: Recommended Next Action → "Rivedi le 3 sezioni"
  │
  ▼
Reviewer approva/segnala conflitti (approve_source_ui, grounded review queue)
  │
  ▼
Home: "Puoi generare il pacchetto per IT → EN"
  │
  ▼
Genera pacchetto lingua (translation_work_package) → invio a fornitore esistente
  │
  ▼
Ritorno traduzione → QA automatico (translation_roundtrip, linguistic_safety)
  │
  ├─ QA bloccato → causa + azione (§7) → correggi → riprova
  │
  ▼
QA passato → Home: "Puoi verificare la readiness"
  │
  ▼
Readiness OK → "Puoi creare la release" → Collaudo interno / Produzione cliente
  │
  ▼
Output finale (DOCX oggi disponibile; PDF publishing e IDML pieno restano gap, §13)
```

Questo percorso non introduce nessuna capability di backend nuova: rimappa
esclusivamente funzioni `*_ui` già esistenti in `app.py` dentro un flusso a stati
invece che dentro 7 tab paritetiche.

---

## 9. Wireflow testuale — schermate principali

**Schermata A — Home (nessun problema aperto)**
```
[Sidebar: Home*, Progetto, Contenuti, Traduzioni, Release]   (*attiva)
┌───────────────────────────────────────────┐
│ Manuale ACS880 · IT → EN, DE                │
│ Tutto in ordine.                             │
│ Prossimo passo naturale: genera il pacchetto │
│ lingua per DE.                                │
│ [ Genera pacchetto DE ]                       │
└───────────────────────────────────────────┘
```

**Schermata B — Home (blocco attivo)**
```
┌───────────────────────────────────────────┐
│ ⚠ Una traduzione non può essere approvata     │
│ Perché: sembra identica al testo originale.   │
│ Cosa fare: chiedi al fornitore di rivederla.  │
│ [ Vedi il segmento ]  [ Riesegui controllo ]  │
└───────────────────────────────────────────┘
```

**Schermata C — Import legacy (in corso)**
```
┌───────────────────────────────────────────┐
│ Sto leggendo il tuo PDF...                    │
│ ▓▓▓▓▓▓▓▓░░░░  62%                              │
└───────────────────────────────────────────┘
```

**Schermata D — Import legacy (risultato)**
```
┌───────────────────────────────────────────┐
│ Ho trovato 42 sezioni.                        │
│ 39 sembrano invariate rispetto a prima.       │
│ 3 hanno valori tecnici diversi — da rivedere. │
│ [ Rivedi le 3 sezioni ]  [ Vedi tutto ]        │
└───────────────────────────────────────────┘
```

**Schermata E — Modalità avanzata (toggle attivo)**
```
[Sidebar: Home, Progetto, Contenuti, Traduzioni, Job & fornitori,
          Release, Impostazioni]     [Modalità avanzata: ON]
┌───────────────────────────────────────────┐
│ (contenuto identico alle tab R23.2 attuali,  │
│  con Object ID, kind, TM SQLite, Vendor ID,   │
│  Release ID, Profilo, Template IDML visibili) │
└───────────────────────────────────────────┘
```

---

## 10. Cosa mantenere della UI R23.2 (senza riscriverlo)

- **Sidebar unica con stato attivo sincronizzato** (`_navigate`, `_NAV_IDS`) — pattern
  valido, va solo esteso con l'item "Modalità avanzata" e reso role-aware (§2).
- **Il pattern Home + Quick Actions come contenitore** — l'idea di una Home con card
  resta corretta; cambia cosa ci va dentro (una azione raccomandata invece di 3 pari
  peso).
- **`_job_state_hint()` e `_release_mode_help()`** — sono già esattamente il pattern
  "causa/prossimo passo" richiesto: vanno generalizzati a Recommended Next Action
  invece di essere riscritti da zero.
- **Le funzioni `_friendly_*`** (`_friendly_jobs`, `_friendly_readiness_matrix`,
  `_friendly_release_artifacts`, ecc.) — già traducono colonne tecniche in etichette
  italiane leggibili; è la base su cui costruire le card guidate.
- **Il fail-closed su Produzione cliente** (`_release_mode_help`, gate R18) — logica e
  messaggistica di principio restano corrette, va solo resa più esplicita in termini
  di causa+azione (§7).
- **Tab-strip tecnica Gradio nascosta, footer disabilitato** (R23.2) — invariant UI
  già acquisiti (`BASELINE_V3.md` invariant #32-#34), da preservare.
- **Tutti i moduli backend e i loro contratti** (nessuna modifica architetturale
  proposta qui).

## 11. Cosa eliminare o riprogettare

- **Il form di creazione progetto a 8 campi obbligatori simultanei** → wizard a passi
  con solo 2 campi realmente necessari all'inizio (§4).
- **Le 3 "Azioni rapide" a pari peso in Home** → 1 azione raccomandata + accesso
  secondario alle altre (§5).
- **L'esposizione diretta di Object ID/kind/TM SQLite/Vendor ID/Release ID/Profilo/
  IDML in ogni tab, sempre visibile** → spostati dietro il toggle "Modalità avanzata"
  (§6).
- **Il dropdown di azione job con nomi tecnici** ("Ricevi XLIFF + controllo qualità
  (QA)") → in modalità guidata diventano pulsanti derivati dallo stato
  (`_job_state_hint` generalizzato), senza dropdown da leggere e scegliere.
- **L'assenza di un percorso di import Word in UI** → nuova schermata di import che
  richiama `import_docx_outline` (funzione già esistente, mai esposta in `app.py`
  oggi).
- **L'assenza di qualunque concetto di ruolo** → introduzione della preferenza di
  ruolo leggera descritta in §2.2.

---

## 12. Funzioni backend già esistenti e collegabili subito (nessuna nuova capability richiesta)

| Funzione UX proposta | Funzione backend reale già AVAILABLE |
|---|---|
| Import PDF guidato | `authoring_ccms.import_pdf_as_topics` |
| "Ho trovato N sezioni, M cambiate" | `layout_engine.analyze_layouts`/`compare_text` |
| Approvazione sorgente | `approve_source_ui` (già esistente) |
| Recommended Next Action su job | `WorkflowManager` stato + `_job_state_hint` (da generalizzare) |
| Recommended Next Action su readiness | `release_readiness.assess_release_readiness` |
| QA con causa+azione | `translation_roundtrip.inspect_translation_roundtrip`, `linguistic_safety.cross_language_issue` |
| Release Collaudo/Produzione | `release_manager.create_release_bundle`, `_release_mode_help` |
| Backup/restore in Impostazioni | `export_project_ui`/`restore_project_ui`/`support_diagnostics` |

## 13. Funzioni mancanti (da costruire, non presenti oggi)

- **Import Word esposto in UI** — backend (`import_docx_outline`) esiste, nessuna
  schermata lo richiama oggi.
- **Motore unificato di Recommended Next Action a livello di intero progetto** — oggi
  esiste solo per singolo job (`_job_state_hint`); serve una funzione che aggreghi
  stato CCMS + revisione + traduzione + readiness in un'unica priorità.
- **Preferenza di ruolo e vista Home filtrata per ruolo** (§2) — non esiste nessun
  concetto di ruolo oggi.
- **Toggle "Modalità avanzata"** — non esiste oggi (tutto è sempre visibile).
- **Wizard di creazione progetto a passi** — oggi è un form singolo.
- **Pubblicazione PDF nativa** — gap già segnalato in `COMPETITIVE_AND_ADOPTION_GAP_V3.md`
  §4.4, rilevante anche qui perché è parte dell'"output corretto" finale per l'utente.
- **Layer di traduzione messaggi tecnici → causa/azione**, generalizzato oltre i pochi
  casi già "friendly" oggi.
- **Onboarding multi-formato con auto-detect** (Word/PDF/IDML/cartelle insieme) — oggi
  ogni formato ha (o non ha) un solo punto di ingresso isolato.

---

## 14. Rischi di regressione

1. **Toccare `app.py` per introdurre la UI guidata senza isolare bene le funzioni `*_ui`
   esistenti** rischia di rompere i test `regressione_desktop_r23_ui.py` e
   `regressione_ui_callbacks_rc2.py` (entrambi verificati PASS in `BASELINE_V3.md`) se
   vengono rimosse stringhe/elementi che quei test cercano testualmente nel sorgente
   (es. `footer_links=[]`, `_NAV_IDS=[...]`, `create_release_bundle` referenziato nel
   codice).
2. **Confondere "nascondere in UI" con "rimuovere dal backend"** — nessuna funzione
   `*_ui` esistente deve essere eliminata; la modalità avanzata deve continuare a
   chiamarle esattamente come oggi.
3. **Introdurre un vero sistema di permessi/ruolo enforced** invece della preferenza
   leggera descritta in §2.2 — violerebbe la regola di dottrina "l'enterprise
   architecture viene dopo la maturità del core" e introdurrebbe superficie di
   sicurezza non necessaria in questa fase.
4. **Rompere gli invariant UI già acquisiti** (#29-#35 in `BASELINE_V3.md`: sidebar
   unica, Home attiva all'avvio, tab-strip nascosta, footer disabilitato, ecc.) — la
   nuova Home deve restare coerente con questi, non sostituirli.
5. **Il bug SQLite non chiuso in `_auto_project_legacy_tm()`** (`BASELINE_V3.md` §5) è
   invocato in un percorso vicino a quello che questo documento tocca concettualmente
   (allineamento legacy). Non va corretto in questa fase (esplicitamente vietato), ma
   va tenuto presente: qualunque futura implementazione del wizard di import non deve
   aggiungere nuovi punti che dipendono dalla stessa funzione senza sapere del bug
   noto.
6. **Costruire il layer "causa+azione" come testo hardcoded** invece che derivato dai
   campi strutturati già presenti nei risultati (`hard_reason`, `qa_status`, ecc.) —
   rischierebbe di disallinearsi dalla logica reale nel tempo.

---

## 15. Piano di implementazione incrementale (senza riscrivere il core R18)

**Principio:** ogni fase tocca solo il layer UI (`app.py`/nuovi moduli di presentazione)
e mai i moduli di dominio (`project_lifecycle`, `ccms_industrial`, `layout_engine`,
`cat_tm`, `translation_*`, `workflow_management`, `release_*`, `linguistic_safety`).

| Fase | Contenuto | Rischio | Dipendenze |
|---|---|---|---|
| **0** | Fix isolati già noti e bloccanti (bug SQLite, encoding console) — **non in questo documento**, ma prerequisito prima di toccare `app.py` in produzione | Basso se isolato | `BASELINE_V3.md` §5, §6 |
| **1** | Layer "causa+azione" per i messaggi già strutturati esistenti (§7), senza toccare layout Home | Basso — solo stringhe/formattazione | Nessuna |
| **2** | Toggle "Modalità avanzata" che nasconde/mostra i campi tecnici già esistenti in ogni tab | Medio — tocca `app.py` in più punti, rischio di rompere i test UI (§14.1); da fare con i test R23.2/RC2 come guardia | Fase 1 |
| **3** | Wizard di creazione progetto a passi (§4), riusando `create_project_ui` invariato | Medio | Fase 2 |
| **4** | Esposizione UI dell'import Word (`import_docx_outline`) — prima funzione backend "mancante in UI" collegata | Medio | Fase 3 |
| **5** | Motore Recommended Next Action a livello progetto, generalizzando `_job_state_hint` | Alto — logica nuova, serve test dedicato di non-regressione | Fase 1-4 |
| **6** | Nuova Home basata su Recommended Next Action, sidebar role-aware (§2) | Alto — cambia la superficie più visibile del prodotto | Fase 5 |
| **7** | Test di usabilità reale con utenti novizi (§16) | — | Fase 6 completata su ambiente Windows validato (Final Windows Gate) |

Ogni fase deve passare l'intera regression suite esistente (24 script +
`environment_guard.py`) prima di procedere alla successiva, come da disciplina di
sviluppo del progetto.

---

## 16. Acceptance criteria UX — test reale futuro

**Non eseguito in questa fase** (è un test con utenti umani, richiede un prodotto
installabile — il Final Windows Gate non è ancora superato, `BASELINE_V3.md` §8).
Definito qui perché richiesto dal Prompt 2, da eseguire quando le fasi 1-6 di §15 sono
complete e il gate Windows è superato.

### 16.1 Protocollo
Un utente che non ha mai usato ManualSync, senza formazione, senza documentazione
letta prima, deve riuscire a:
1. Creare un progetto.
2. Importare un documento (PDF o Word) che gli viene fornito.
3. Descrivere a voce, senza aiuto, cosa ManualSync ha trovato (verifica di
   comprensione, non solo di click).
4. Eseguire una modifica/revisione richiesta (es. approvare una sorgente o risolvere
   un blocco segnalato).
5. Arrivare al prossimo output corretto (es. pacchetto lingua generato o release di
   collaudo creata).

### 16.2 Target da validare (non ancora misurati — ROADMAP)
- Primo risultato utile entro **15 minuti**.
- Almeno **90%** di completamento dei task principali al primo tentativo.
- **Zero** necessità per l'utente di pronunciare o capire i termini XML, DITA, XLIFF
  nel flusso normale (la modalità avanzata può usarli, il flusso guidato no).
- Ogni blocco incontrato durante il test deve rispettare il formato causa+azione (§7)
  verificabile osservando se l'utente sa dire da solo "perché" e "cosa devo fare".

### 16.3 Nota di onestà
Nessuno di questi numeri è oggi misurato. Come già registrato in
`COMPETITIVE_AND_ADOPTION_GAP_V3.md` §10, restano obiettivi di dottrina fino
all'esecuzione di questo test reale.

---

## 17. Collegamento esplicito alle 8 obiezioni

| Obiezione | Elemento di questo documento che la indirizza |
|---|---|
| "Non abbiamo tempo di imparare un altro programma" | Modalità guidata come default (§6), Recommended Next Action (§5), wizard a 2 campi minimi (§4) |
| "Abbiamo già Paligo / un sistema simile" | Nessuna esposizione XML/DITA (§0, §6) — differenziatore #5 di `COMPETITIVE_AND_ADOPTION_GAP_V3.md`; coesistenza resta comunque ROADMAP, non risolta da sola UX |
| "Lavoriamo con Word" | Import Word esposto in UI come funzione mancante prioritaria (§13, Fase 4) |
| "Lavoriamo con InDesign" | Non risolto da questo documento — gap di backend (round-trip IDML), non di UX; resta PARTIAL come da `COMPETITIVE_AND_ADOPTION_GAP_V3.md` §6 |
| "Abbiamo solo PDF" | Primo avvio con percorso dedicato "Ho già dei manuali" (§3), import PDF come primo passo del wizard (§4) |
| "Non vogliamo migrare tutto" | Campi amministrativi resi opzionali/posticipabili (§4.2), nessun obbligo di popolare l'intero progetto prima di vedere valore |
| "Facciamo tutto internamente" | Recommended Next Action toglie il carico di capire quale tab aprire (§5) |
| "Siamo una piccola realtà" | Nessuna richiesta di configurare ruoli/permessi complessi (§2.2 — preferenza leggera, non un sistema enterprise) |

---

## 18. Cosa NON fa questo documento

- Non modifica `app.py` o alcun modulo backend.
- Non corregge il bug SQLite (`BASELINE_V3.md` §5).
- Non corregge il problema Unicode/cp1252 (`BASELINE_V3.md` §6).
- Non tocca le incongruenze documentali V3/V4.
- Non autorizza l'esecuzione del Prompt 3.
- Non trasforma nessun target UX (15 minuti, 90%, zero XML) in un claim commerciale:
  restano obiettivi di progettazione da validare, coerente con la regola di prodotto.
