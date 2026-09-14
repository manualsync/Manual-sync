# ERYLOS — UX Architecture & Localization Audit V1

Baseline auditata:

- Fase 4F.2 — `9c8c207c390a4727a9c3b7da6749c47cfb64261a`
- Publishing Preflight v1 — `3ba2075e164f03adde17fcdcbcadbddf09a0ce10`

Metodo: ispezione del sorgente **e** dell'applicazione in esecuzione
(Chrome reale, viewport 1920×1080, misurazioni DOM). I numeri riportati
sono misurati, non stimati. Nessun codice è stato modificato.

---

## 1. Diagnosi esecutiva

ERYLOS ha una profondità funzionale sproporzionata rispetto alla sua
architettura di interfaccia. Il prodotto sa fare cose che i concorrenti
non fanno — tracciabilità normativa con provenienza verificata, preflight
di integrità dell'output, revision intelligence — ma le espone come una
sequenza di pannelli aggiunti nel tempo dentro pagine che crescono
verticalmente senza un modello di interazione comune.

Il problema non è estetico. È strutturale, e si riassume in una frase:

> **Non esiste un contesto di lavoro persistente.** Ogni area richiede di
> nuovo all'utente le stesse coordinate (quale pubblicazione, quale
> profilo, chi sei) perché nessuna schermata sa da dove arrivi.

Da questa singola mancanza discendono quasi tutti i difetti rilevati:
campi duplicati, controlli lontani dal contenuto, pagine-monolite,
impossibilità di tornare al contesto precedente, e la sensazione — reale
— di usare otto strumenti diversi invece di un prodotto.

Le misurazioni:

| Area | Altezza | Schermate @1080 | Pulsanti | Campi | Tabelle |
|---|---:|---:|---:|---:|---:|
| Compliance | 3260 px | 3,0 | 38 | 15 | 6 |
| Contenuti | 2110 px | 2,0 | 18 | 15 | 20 |
| Impostazioni | 2089 px | 1,9 | 48 | 13 | 8 |
| Job & fornitori | 1684 px | 1,6 | 7 | 7 | 0 |
| Release | 1530 px | 1,4 | 3 | 7 | 0 |
| Revisione | 1070 px | 1,0 | 12 | 0 | 2 |
| Traduzioni | 885 px | 0,8 | 2 | 2 | 0 |
| Home | 826 px | 0,8 | 5 | 0 | 0 |

Il codice sorgente conferma lo squilibrio: la scheda «Redazione &
contenuti» occupa **774 righe e 152 componenti** in `app.py`, mentre
«Traduzioni» ne occupa **8 e 9**. La traduzione è dichiarata pilastro di
primo livello nella Product Doctrine; nell'interfaccia è la pagina più
vuota del prodotto.

Non è un giudizio sui test. Le suite passano, e devono continuare a
passare. È un giudizio sul fatto che un responsabile documentazione,
aperta l'applicazione, non riesce a rispondere alle sei domande che
contano: dove sono, cosa richiede attenzione, cosa devo fare adesso, dove
trovo il dettaglio, come torno indietro, e cosa di quello che vedo è
stato / azione / configurazione.

---

## 2. Inventario UI corrente

Navigazione: sidebar fissa a sinistra con otto voci; le schede sono
`gr.Tabs` commutate programmaticamente. Non esiste breadcrumb, non esiste
ritorno al contesto precedente, non esiste stato di navigazione
persistente.

### 2.1 Home

- **Scopo** — panoramica progetto e prossime attività.
- **Percorso** — voce di default all'avvio; `Sidebar → ⌂ Home`.
- **Azione primaria** — nessuna dichiarata. Quattro «Azioni rapide»
  competono con pari peso visivo.
- **Gerarchia** — quattro KPI (Pubblicazioni, Blocchi, Job aperti, QA
  bloccati), una «Azione consigliata», quattro card di scorciatoia.
- **Problemi**
  - I KPI contano **oggetti di implementazione**, non salute
    documentale. «Blocchi 7» non è un'informazione di business; un
    responsabile qualità non ha mai bisogno di sapere quanti blocchi
    esistono. Mancano invece i dati che le card stesse promettono:
    copertura normativa, stato traduzioni, prontezza al rilascio.
  - **Espone il percorso filesystem completo**: `Percorso progetto ·
    C:\Users\baxai\ManualSyncData\Projects\4E3_Rich_Text_Manual_Acceptance`.
    Lo stesso tipo di stringa che il gate 4F.1 aveva vietato nelle card
    di export è in chiaro sulla prima schermata del prodotto.
  - Riga metadati rotta sotto il nome progetto: `· · Rev.` — separatori
    senza valori, visibile a ogni avvio.
  - Kicker di sezione `WORKSPACE` in inglese.

### 2.2 Revisione (`Analizza revisione`)

- **Scopo** — confronto originale/traduzione/nuova revisione.
- **Azione primaria** — analisi comparativa.
- **Gerarchia** — una sola sezione, nessuna suddivisione.
- **Problemi** — 12 pulsanti senza alcun campo di input visibile:
  l'utente vede azioni prima di sapere su cosa agiscono. Titolo della
  voce sidebar («Revisione») diverso dal titolo pagina («Analizza
  revisione»).

### 2.3 Contenuti (`Redazione & contenuti`)

- **Scopo** — repository strutturato, editor, varianti, anteprima e
  export.
- **Azione primaria** — scrivere e revisionare contenuto.
- **Gerarchia misurata** — `La tua identità` (213) → `Revisione del
  contenuto importato` (395) → `Elementi da verificare` (532) → `Editor
  professionale` (719) → `Anteprima pubblicazione` (1748).
- **Problemi**
  - È la **pagina-monolite** del prodotto: 774 righe di codice, 152
    componenti, **20 tabelle** in una sola schermata logica.
  - Mescola quattro attività distinte — identità, onboarding legacy,
    editing, pubblicazione — senza separazione visiva forte.
  - L'anteprima di pubblicazione (1748 px) è di fatto irraggiungibile
    senza scroll deliberato, benché contenga l'azione commercialmente più
    importante del prodotto (export PDF/HTML).
  - Etichetta `Struttura manuale` ripetuta **8 volte** nella stessa
    pagina.
  - Titolo sidebar «Contenuti» ≠ titolo pagina «Redazione & contenuti».

### 2.4 Compliance

- **Scopo** — tracciabilità obblighi documentali e copertura.
- **Azione primaria** — valutare applicabilità e collegare evidenze.
- **Gerarchia misurata** — `Pacchetto di requisiti` (539) → `Copertura
  documentale` (760) → `Da completare` (1192) → `Requisiti` (1614) →
  `Applicabilità` + `Copertura` (2273) → `Evidenze collegate` (2855).
- **Problemi**
  - **La pagina più lunga del prodotto: 3 schermate piene, 38 pulsanti.**
  - Apre il requisito nel **fondo** della stessa pagina: l'utente perde
    completamente il contesto della lista mentre lavora sul dettaglio, e
    per passare al requisito successivo deve risalire ~1200 px.
  - Etichetta `Decisione` ripetuta 6 volte, `Commento — opzionale` 2
    volte: senza il contesto visivo circostante sono indistinguibili.
  - Unica sezione con nome inglese in una sidebar altrimenti italiana.
  - *Nota*: è anche l'area più curata del prodotto — pacchetto attivo in
    testa, azioni di aggiornamento condizionali, terminologia ufficiale.
    I difetti che restano sono di **architettura**, non di rifinitura.

### 2.5 Traduzioni

- **Scopo** — pacchetti lingua, XLIFF, code di revisione.
- **Gerarchia** — una sola sezione, `Pacchetti lingua`.
- **Problemi** — **885 px, 2 pulsanti, 2 campi**. Un pilastro dichiarato
  del prodotto è, nell'interfaccia, poco più di un form. Chiede `Object
  ID` e `Tipo`: identificatori interni come input primario.

### 2.6 Job & fornitori

- **Scopo** — invio, ritorno XLIFF, QA, approvazione umana.
- **Problemi** — nome ibrido inglese/italiano. Chiede `Job ID` e `Vendor
  ID — opzionale (solo se registrato)`: l'utente deve conoscere
  identificatori interni e anche la condizione tecnica che li rende
  validi. La gestione fornitori è in un accordion chiuso dentro l'area
  operativa.

### 2.7 Release

- **Scopo** — release controllata con gate di prontezza.
- **Problemi** — nome inglese. Chiede `Publication ID`, `Release ID —
  opzionale`, `Profilo — opzionale`. `Tipo release` compare due volte.
  1530 px per 3 pulsanti: densità informativa molto bassa.

### 2.8 Impostazioni

- **Scopo dichiarato** — gestione progetto, backup, informazioni build.
- **Problemi — è l'area più problematica dopo Compliance**
  - Contiene **Varianti (dimensioni, valori, profili)**, che non è
    configurazione di sistema ma **modellazione di contenuto usata ogni
    giorno** insieme all'editor e alla pubblicazione. Lavoro quotidiano
    sepolto dentro la configurazione.
  - L'interfaccia Varianti è **CRUD per identificatore**: `Id (es.
    market)`, `Id dimensione esistente`, `Id valore (es. eu)`, `Id
    profilo`, `Id profilo esistente`. Tredici campi, quasi tutti chiedono
    un id interno. È la superficie più orientata all'implementazione del
    prodotto.
  - Espone metadati interni di build: `Build:
    1.0.0-final-candidate-20260822`, `Core baseline: R18`.
  - Espone una nota di sviluppo: *«Il nome è ancora provvisorio durante
    il rebrand»*.
  - Tabelle vuote rendono griglie segnaposto `1 2 3 / 1 2 3`, ripetute
    sei volte.
  - 48 pulsanti — il numero più alto del prodotto.

### 2.9 Difetti trasversali

- **Identità chiesta ripetutamente**: `Il tuo nome` compare in aree
  diverse (Contenuti, Compliance) e in Job come `Revisore umano`, in
  Release come `Release manager`. Sono la stessa persona.
- **Identificatori interni come input primario** in 4 aree su 8:
  `Publication ID`, `Object ID`, `Job ID`, `Vendor ID`, `Release ID`,
  più tutta la famiglia `Id …` in Impostazioni.
- **Kicker di sezione incoerenti**: `WORKSPACE` (Home), `WORKFLOW` (cinque
  aree), `SISTEMA` (Impostazioni). Due in inglese, uno in italiano.
- **Nessun breadcrumb, nessun ritorno**: aperto un dettaglio, l'unico
  modo di tornare è lo scroll.

---

## 3. Audit terminologico e di lingua

> **SUPERATO in parte.** Le righe che mantenevano *Workflow*, *Preflight*,
> *Template* e *Batch* in inglese sono state corrette da
> `ERYLOS_TERMINOLOGY_POLICY_V1.md`, che è la fonte vincolante: la
> familiarità di un termine nel software non è una ragione sufficiente per
> non tradurlo. Il resto di questa sezione resta valido.

Principio adottato: **una lingua alla volta**. Nessuna etichetta doppia
tipo `Conformità / Compliance`. Italiano di default, inglese selezionabile
in seguito.

Criterio per «mantenere in inglese»: il termine è già lo standard
professionale nel settore italiano di riferimento (documentazione
tecnica, prestampa, traduzione) e tradurlo **ridurrebbe** la chiarezza per
l'utente esperto.

| Termine attuale | Italiano proposto | Inglese | Non tradurre? | Motivazione |
|---|---|---|---|---|
| Compliance | **Conformità** | Compliance | No | Termine giuridico italiano esatto e universalmente compreso nel settore macchine. È anche l'unica voce inglese in una sidebar italiana. |
| Release (sezione) | **Rilascio** | Release | No | «Rilascio controllato» è italiano tecnico corrente. Mantenere «release» nei soli dettagli tecnici. |
| Publishing | **Pubblicazione** | Publishing | No | Già usato altrove nel prodotto («Anteprima pubblicazione»): l'incoerenza è interna. |
| Preflight | **Controllo pre-esportazione** / **Verifica di pubblicazione** | Preflight | No | *Corretto dalla politica V1*, secondo il contesto. |
| Workflow | **Flusso di lavoro** | Workflow | No | *Corretto dalla politica V1.* Applicato al kicker di sezione. |
| Template | **Modello documento** / **Modello di impaginazione** | Template | No | *Corretto dalla politica V1*: la forma estesa risolve l'ambiguità di «modello». |
| Backup | *Backup* | Backup | **Sì** | Standard assoluto. |
| Evidence | **Evidenza** | Evidence | No | Già implementato correttamente in Conformità. |
| Coverage | **Copertura** | Coverage | No | Già implementato. |
| Scope | **Perimetro (normativo)** | Scope | No | Già implementato in 4F.2. «Scope» in italiano è gergo interno. |
| Status | **Stato** | Status | No | Nessuna ragione per l'inglese. |
| Missing | **Mancante** | Missing | No | Già implementato. |
| Covered | **Coperto** | Covered | No | Già implementato. |
| To verify | **Da verificare** | To verify | No | Già implementato. |
| Not applicable | **Non applicabile** | Not applicable | No | Già implementato. |
| Publication ID | **Pubblicazione** | Publication | No | L'utente sceglie *una pubblicazione*, non digita un id. Il campo va sostituito da un selettore, non solo rinominato. |
| Object ID | **Oggetto da tradurre** | Object | No | Come sopra: selettore, non identificatore. |
| Job ID | **Lavorazione** | Job | No | «Job» è l'oggetto interno del workflow; l'utente pensa in termini di lavorazione/incarico. |
| Vendor ID | **Fornitore** | Vendor | No | Idem. |
| Release ID | **Codice rilascio** | Release ID | No | Se resta visibile deve essere opzionale e chiaramente tecnico. |
| Job & fornitori | **Lavorazioni e fornitori** | Jobs & vendors | No | Elimina l'ibrido inglese/italiano nel nome di sezione. |
| Revision | **Revisione** | Revision | No | Già italiano nella sidebar; l'inglese resta solo in testi residui. |
| Grounded Authoring | **Redazione assistita** (con fonte) | Grounded authoring | No | «Grounded» è gergo anche in inglese. Il valore da comunicare è *«assistita e ancorata a fonti verificabili»*. |
| Draft | **Bozza** | Draft | No | Standard italiano. |
| Preview | **Anteprima** | Preview | No | Già usato. |
| Export | **Esporta / Esportazione** | Export | No | Già usato. |
| Batch | **Elaborazione in serie** | Batch | No | *Corretto dalla politica V1*; evita «lotto», ambiguo in contesto macchine. |
| XLIFF | *invariato* | — | **Sì** | Standard di settore. |
| TM | **Memoria di traduzione (TM)** | Translation memory | No | *Politica V1*: forma estesa con sigla. |
| QA | **QA** (specialisti) / **Controllo qualità** (UI generale) | QA | Parziale | *Politica V1*, secondo il pubblico. |
| WORKSPACE / WORKFLOW (kicker) | **AREA DI LAVORO / FLUSSO** | Workspace / Workflow | No | Oggi incoerenti tra loro e in inglese in un'interfaccia italiana. |

### 3.1 Nomi di sezione proposti

| Oggi | Proposto (IT) | Proposto (EN) |
|---|---|---|
| Home | **Panoramica** | Overview |
| Revisione / «Analizza revisione» | **Revisione** | Revision |
| Contenuti / «Redazione & contenuti» | **Contenuti** | Content |
| Compliance | **Conformità** | Compliance |
| Traduzioni | **Traduzioni** | Translation |
| Job & fornitori | **Lavorazioni** | Jobs |
| Release | **Rilascio** | Release |
| Impostazioni | **Impostazioni** | Settings |

Regola: **il titolo della pagina deve coincidere con la voce di
navigazione.** Oggi non accade in due sezioni su otto.

---

## 4. Architettura di localizzazione

### 4.1 Stato attuale — misurato

Non esiste alcuna infrastruttura di localizzazione: nessun `gettext`,
nessun `babel`, nessun catalogo, nessuna funzione di risoluzione. Le
stringhe sono letterali inline.

| Categoria | Occorrenze |
|---|---:|
| `label=` / `info=` / `placeholder=` / `title=` | **386** |
| Stringhe contenenti accenti italiani | **401** (324 distinte) |
| `gr.Button("…")` letterali | **152** |
| di cui in `app.py` | 148 label, 124 IT, 111 button |

### 4.2 Il rischio principale non è il numero, è la posizione

Il testo italiano **non è confinato al livello UI**. È presente nel
dominio:

- `authoring_service.py` — 60 stringhe italiane, incluse eccezioni
  mostrate all'utente: `raise ValueError("actor è obbligatorio per un
  draft recovery.")`
- `compliance_intelligence.py` — 15, incluse `ValueError` italiane
- `compliance_view.py` — 11, incluse le etichette di stato
- `layout_engine.py` — 58

Questo significa che **tradurre l'interfaccia oggi richiede di toccare la
logica di business**, che è esattamente ciò che l'architettura richiesta
deve impedire.

Distinzione necessaria — tre categorie di testo italiano, con destini
diversi:

1. **Testo di interfaccia** (etichette, pulsanti, messaggi) → va nel
   catalogo di localizzazione.
2. **Messaggi di errore di dominio** → devono diventare **codici**
   (`ERR_ACTOR_REQUIRED`), risolti in stringa dal livello UI. È il lavoro
   più delicato perché cambia firme e messaggi già testati.
3. **Contenuto normativo** (`compliance_starter_packs.py`, 36 stringhe) →
   **non è localizzazione**. È contenuto legale verificato sul testo
   consolidato italiano ufficiale. Una versione inglese richiederebbe una
   verifica indipendente sul testo consolidato EN
   (`02023R1230 — EN — 27.07.2026 — 002.001`), non una traduzione.
   Trattarlo come stringhe traducibili sarebbe un errore grave.

### 4.3 Architettura proposta

```
locales/
  it.json      ← default, completo
  en.json      ← aggiunto in seguito
i18n.py
  t(key, **kw) -> str      risoluzione con fallback a it
  set_language(code)       preferenza per sessione
```

Principi vincolanti:

- **Le enum e le chiavi persistite non cambiano mai.** `COVERED`,
  `TO_VERIFY`, `APPLIES`, `requirement_id`, `pack_id` restano
  identificatori stabili. Questo è già il modello adottato in 4F.2 con
  `STATUS_LABELS`, che è il precedente corretto da generalizzare.
- **I dati di progetto restano indipendenti dalla lingua.** Nessuna
  stringa localizzata entra in SQLite. Le decisioni di un utente restano
  leggibili cambiando lingua.
- **Il dominio non produce testo utente.** Solleva codici; la UI decide
  le parole.
- **Italiano è il default e il fallback**, non una traduzione dell'inglese.

### 4.4 Strategia di migrazione incrementale

| Fase | Contenuto | Rischio | Superficie |
|---|---|---|---|
| L0 | Introdurre `i18n.py` + `it.json` vuoto; `t()` con passthrough | Nullo | 1 file nuovo |
| L1 | Nomi sezione, voci sidebar, kicker, titoli pagina | Basso | ~40 stringhe, alta visibilità |
| L2 | Etichette e pulsanti per area, **una area per gate** | Basso-medio | ~386 stringhe, guardate dalle suite R23/RC2 |
| L3 | Messaggi di esito e card (export, preflight, compliance) | Medio | ~120 stringhe, molte asserite nei test |
| L4 | Codici di errore di dominio | **Alto** | firme di `AuthoringService`, `ComplianceService`; test esistenti asseriscono i messaggi |
| L5 | `en.json` + selettore lingua | Basso | additivo |

Aree a rischio più alto, in ordine: **L4** (cambia il contratto del
dominio), poi i messaggi asseriti letteralmente dalle suite 3.5B / 4F.2 /
preflight, poi il pacchetto normativo — che **non va toccato** in questa
migrazione.

---

## 5. Revisione dell'architettura UX

Difetti confermati, con evidenza:

- **Page-monolith** — Compliance 3 schermate / 38 pulsanti; Contenuti 774
  righe e 20 tabelle.
- **Scroll eccessivo** — l'azione commerciale principale (export PDF) è a
  1748 px in Contenuti; il dettaglio requisito a 2273 px in Compliance.
- **Informazione duplicata** — `Decisione` ×6, `Struttura manuale` ×8,
  `Tipo release` ×2, `Il tuo nome` in più aree.
- **Controlli lontani dal contenuto** — in Compliance il selettore di
  perimetro è in cima, la decisione che lo usa a 2273 px.
- **Azioni importanti nascoste** — gestione fornitori in un accordion
  chiuso; Varianti dentro Impostazioni.
- **Dettaglio tecnico eccessivo** — build interna, `Core baseline: R18`,
  nota di rebrand, percorso filesystem, sei famiglie di campi `Id …`.
- **Gerarchia visiva debole** — tutte le sezioni hanno lo stesso peso
  tipografico; niente distingue *stato* da *azione* da *configurazione*.
- **Azioni ambigue** — risolto in Conformità («Ricalcola stato»), ancora
  presente altrove: più pulsanti «Aggiorna» con significati diversi.
- **Le liste perdono il contesto** — aprire un requisito porta a 2273 px
  e la lista esce dallo schermo.
- **Larghezza desktop sprecata** — a 1920 px il prodotto usa una singola
  colonna centrale; il pattern lista+dettaglio affiancati non è mai usato.
- **Configurazione mescolata al lavoro quotidiano** — nella direzione
  peggiore: non è la configurazione a invadere il lavoro, è il lavoro
  quotidiano (Varianti) a essere sepolto nella configurazione.

---

## 6. Architettura informativa proposta

### 6.1 Modello di interazione comune — da definire **prima** delle pagine

**Shell a tre zone persistenti:**

```
┌─────────────┬──────────────────────────────────────────────────┐
│             │  BARRA DI CONTESTO                               │
│  NAVIGAZIONE│  Progetto ▾ · Pubblicazione ▾ · Profilo ▾ · Utente│
│             ├──────────────────────────────────────────────────┤
│  Panoramica │  Titolo sezione            [ Azione primaria ]   │
│  Revisione  │  ┌──────────────┬───────────────────────────────┐│
│  Contenuti  │  │              │                               ││
│  Conformità │  │  LISTA       │   DETTAGLIO CONTESTUALE       ││
│  Traduzioni │  │  (master)    │   (resta accanto alla lista)  ││
│  Lavorazioni│  │  filtri +    │                               ││
│  Rilascio   │  │  stato       │                               ││
│             │  └──────────────┴───────────────────────────────┘│
│  Impostaz.  │  ▸ Modalità avanzata (toggle di shell, unico)    │
└─────────────┴──────────────────────────────────────────────────┘
```

Quattro regole vincolanti:

1. **La barra di contesto è unica e persistente.** Progetto,
   pubblicazione, profilo e identità si scelgono **una volta**. Questo da
   solo elimina `Publication ID`, `Object ID`, `Profilo` e `Il tuo nome`
   da ogni singola pagina — cioè la maggior parte dei campi duplicati e
   degli identificatori interni rilevati.
2. **Lista e dettaglio coesistono.** Il dettaglio non sostituisce mai la
   pagina né si apre in fondo. Si chiude con Esc e restituisce la lista
   nella stessa posizione di scroll.
3. **L'azione primaria è una sola per sezione**, in alto a destra, sempre
   nello stesso punto.
4. **«Modalità avanzata» è un interruttore di shell, non un accordion per
   pannello.** Oggi esistono accordion «Dettagli tecnici» indipendenti in
   più aree: stessa idea, sei implementazioni.

### 6.2 Sezione per sezione

| Sezione | Landing | Azione primaria | Lista → dettaglio | Sotto «avanzate» |
|---|---|---|---|---|
| **Panoramica** | Cruscotto salute progetto | Prossima azione consigliata | Avvisi → area competente | percorso progetto, build |
| **Revisione** | Ultime analisi + nuova analisi | Analizza revisione | Analisi → confronto affiancato | soglie, policy allineamento |
| **Contenuti** | Albero manuale + editor | Nuovo/modifica contenuto | Topic/blocco → editor a destra | id oggetto, versioni, reuse |
| **Conformità** | Panoramica copertura | Valuta requisiti da completare | Requisito → pannello a destra | provenienza, versioni pacchetto |
| **Traduzioni** | Stato lingue per pubblicazione | Genera pacchetto lingua | Lingua → dettaglio pacchetto | TM esterne, soglie fuzzy |
| **Lavorazioni** | Lavorazioni aperte per stato | Avanza lavorazione | Lavorazione → dettaglio + QA | id job, configurazione fornitori |
| **Rilascio** | Gate di prontezza | Crea rilascio | Gate → cosa manca | id release, template |
| **Impostazioni** | Solo sistema | — | — | backup, diagnostica, build |

**Varianti esce da Impostazioni** e diventa una vista dentro Contenuti
(«Varianti e condizioni»), perché è modellazione di contenuto. In
Impostazioni resta solo ciò che non si usa lavorando.

---

## 7. Conformità come caso di riferimento

La struttura proposta nel brief — `Panoramica · Requisiti · Evidenze ·
Aggiornamenti normativi` — è **quasi giusta, con un difetto e
un'aggiunta**.

**Difetto: «Evidenze» non è una sezione.** Un'evidenza non esiste
indipendentemente dal requisito che copre; una scheda separata
costringerebbe l'utente a tenere a mente quale requisito stava valutando,
cioè esattamente il problema di perdita di contesto da risolvere. Le
evidenze appartengono al **pannello di dettaglio del requisito**.

Una vista «tutte le evidenze» ha senso solo per la domanda inversa —
*«questo topic che requisiti copre?»* — che è una funzione di impatto, non
una scheda di pari livello.

**Struttura proposta:**

```
Conformità
├── Panoramica          copertura, cosa manca, stato pacchetto
├── Requisiti           LISTA persistente ─┬─ DETTAGLIO contestuale
│                       filtri per stato   │   ├ applicabilità
│                       ricerca            │   ├ copertura
│                                          │   ├ evidenze collegate
│                                          │   └ provenienza (avanzate)
└── Aggiornamenti       revisione pacchetto: anteprima e applicazione
```

Tre note:

- **«Da completare» non è una scheda**: è il filtro predefinito della
  lista Requisiti. Oggi è una tabella separata che duplica righe già
  presenti nella tabella «Requisiti» sottostante.
- **«Aggiornamenti normativi» merita la sua scheda** — il lavoro 4F.2 è
  già lì (rilevamento, anteprima, applicazione con preservazione
  dell'overlay) ma oggi è compresso nella testa di una pagina lunga.
- Il risultato atteso: da **3260 px a una schermata** per l'attività
  quotidiana, con la lista sempre visibile.

---

## 8. Home / workspace di progetto

**Verdetto: oggi è un lanciatore con quattro numeri sopra.** Non risponde
a nessuna delle domande che una Home matura deve risolvere, e i numeri che
mostra sono i meno utili disponibili.

Conta oggetti di implementazione (`Blocchi 7`) invece dei segnali che le
sue stesse card promettono. Il prodotto **possiede già** i dati giusti:
`documentation_coverage()`, `to_complete_rows()`, `pack_update_status()`,
`release_readiness`, lo stato job. Non sono esposti.

**Deve esserci:**

- salute documentale per pubblicazione (copertura, requisiti da valutare);
- lavoro in sospeso, attribuito e ordinato per urgenza;
- stato traduzioni per lingua;
- prontezza al rilascio con i gate che mancano;
- aggiornamenti normativi in attesa;
- **una** prossima azione consigliata, non quattro equivalenti.

**Non deve esserci:** percorso filesystem, conteggi di blocchi, build
interna, e — soprattutto — non deve diventare un secondo cruscotto che
duplica ciò che le sezioni già mostrano. La Home indica *dove andare*; non
è il posto dove si lavora.

---

## 9. Valutazione del vincolo Gradio

Distinzione richiesta, basata sui requisiti reali di ERYLOS.

**A — Ottenibile pulitamente con Gradio**

- Barra di contesto persistente (componenti condivisi + `gr.State`).
- Sotto-schede per sezione (`gr.Tabs` annidati) — già usato.
- Toggle «modalità avanzata» di shell (`visible=`) — pattern già presente.
- Lista→dettaglio affiancati in due colonne (`gr.Row`) con selezione via
  `Dataframe.select` — **già dimostrato funzionante** nel polish 4F.2.
- Terminologia, gerarchia tipografica, densità, larghezza: CSS.
- Ordinamento delle pagine, azioni primarie in alto.

**B — Possibile ma fragile in Gradio**

- Filtri e ricerca persistenti che sopravvivono ai refresh di sessione —
  il reset dei valori lato client su reconnect è già documentato nel
  codice come trappola nota.
- Chiusura del dettaglio con tastiera (Esc), scorciatoie.
- Mantenere la posizione di scroll della lista riaprendo un dettaglio.
- Aggiornamenti parziali: molte callback restituiscono bundle posizionali
  ampi (`ST_OUT_FULL` a 49 elementi, `cp_out_full` a 17) — funziona, ma è
  una classe di bug già colpita più volte in questo progetto.
- Navigazione programmatica: `gr.Tabs(selected=…)` **non emette**
  `.select()`, difetto già incontrato e aggirato tre volte.

**C — Giustificherebbe una valutazione di altro frontend**

- URL indirizzabili e deep-link (`/conformita/requisiti/<id>`) — oggi
  impossibili: nessuno stato di navigazione è nell'URL. Questo blocca
  condivisione di link, bookmark e ripristino di contesto.
- Pulsante «indietro» del browser coerente.
- Lavoro multi-documento reale (più pubblicazioni aperte insieme).
- Editing collaborativo o presenza multiutente.
- Tabelle virtualizzate su migliaia di righe con filtro reattivo.

**Raccomandazione.** Gradio **rimane adeguato** per la UX target
descritta qui, e va mantenuto. Tutti i P0 identificati cadono nella
categoria A. Non esiste alcuna ragione di riscrivere il frontend oggi:
sarebbe sostituire un problema di architettura informativa — che il
rewrite non risolve da solo — con un rischio di regressione su un
prodotto che ha appena superato due gate di accettazione.

Il limite vero da tenere d'occhio è **C.1, i deep-link**. Se ERYLOS dovrà
supportare più utenti che si scambiano riferimenti a requisiti o
lavorazioni, quello è il momento in cui la valutazione va riaperta — non
prima, e non per ragioni estetiche.

---

## 10. Roadmap prioritizzata

### P0 — Problemi strutturali, prima di aggiungere superficie

| # | Intervento | Impatto | Rischio | Aree | Tocca logica? |
|---|---|---|---|---|---|
| P0.1 | **Barra di contesto persistente** (progetto · pubblicazione · profilo · utente) | Molto alto | Medio | tutte | No |
| P0.2 | **Lista + dettaglio affiancati in Conformità**, sotto-schede, «Da completare» come filtro | Molto alto | Medio | Conformità | No |
| P0.3 | **Scomporre Contenuti**: editing / anteprima-pubblicazione / varianti | Alto | Medio-alto | Contenuti, Impostazioni | No |
| P0.4 | **Spostare Varianti** da Impostazioni a Contenuti | Alto | Basso | 2 aree | No |
| P0.5 | **Rimuovere dettaglio interno dall'UI** (percorso filesystem, build, nota rebrand, `· · Rev.`) | Medio | Molto basso | Home, Impostazioni | No |

### P1 — Usabilità ad alto valore

| # | Intervento | Impatto | Rischio | Tocca logica? |
|---|---|---|---|---|
| P1.1 | Rinominare sezioni e allineare titolo pagina a voce di navigazione | Alto | Basso | No |
| P1.2 | Sostituire i campi `*_ID` con selettori a nome umano | Alto | Medio | No (solo presentazione) |
| P1.3 | Home orientata alla salute del progetto | Alto | Medio | No |
| P1.4 | Dare a Traduzioni una vera struttura di sezione | Alto | Medio | No |
| P1.5 | Un'unica «modalità avanzata» di shell | Medio | Basso | No |
| P1.6 | `i18n.py` + `it.json`, fasi L0–L2 | Medio | Basso | No |

### P2 — Coerenza e rifinitura

Kicker coerenti; gerarchia tipografica che distingua stato/azione/
configurazione; uso della larghezza a 1920 px; eliminazione delle griglie
segnaposto `1 2 3`; densità uniforme delle tabelle (il pattern
`.ms-cp-grid` di 4F.2 è il riferimento da estendere).

### Più avanti

Localizzazione L3–L5 e selettore lingua; codici di errore di dominio
(L4); filtri persistenti; deep-link se e quando il multiutente lo
richiede; scorciatoie da tastiera.

---

## 11. Primo gate di implementazione raccomandato

**UX Gate 1 — Barra di contesto persistente + Conformità come riferimento.**

Perché questi due insieme, e non altro:

- **P0.1 è il moltiplicatore.** Elimina alla radice i campi duplicati,
  gli identificatori interni come input primario e la domanda «dove
  sono?». Ogni sezione successiva ne beneficia senza lavoro aggiuntivo.
- **P0.2 dimostra il modello su un caso reale.** Conformità è l'area
  peggiore per lunghezza (3 schermate) ed è anche la più recente e meglio
  testata: 243 check la proteggono. È il posto più sicuro in cui provare
  il pattern lista+dettaglio, e il pattern viene poi riusato altrove
  invece di essere reinventato.
- **Nessuno dei due tocca la logica di business.** Nessuna modifica al
  motore compliance, al pack update, al preflight, alla persistenza.
- **Tutto ricade nella categoria A** di Gradio: nessuna decisione di
  frontend è richiesta per iniziare.

Criterio di accettazione proposto: il lavoro quotidiano di conformità —
aprire un requisito, decidere applicabilità, collegare un'evidenza,
passare al successivo — deve avvenire **senza perdere di vista la lista**
e **in una sola schermata a 1920 px**, con le 243 verifiche esistenti
ancora verdi.

Da **non** includere nel primo gate: rinomina delle sezioni (P1.1) e
localizzazione (P1.6). Sono a basso rischio ma toccano molte asserzioni di
test: vanno in un gate proprio, dove l'unica cosa che cambia sono le
parole.
