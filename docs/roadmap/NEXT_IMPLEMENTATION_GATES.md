# NEXT_IMPLEMENTATION_GATES.md — ManualSync / ERYLOS

Scopo: registrare la sequenza di gate di implementazione decisa DOPO la
chiusura di 4F.2, perché nessun documento del repository la definiva.

Questo file è una decisione di pianificazione, non una fonte di
requisiti. Le fonti dei requisiti restano
`docs/product/ManualSync_Prompt_Pack_Claude_V4.md` (ordine di lavoro) e
`docs/product/ManualSync_Product_Doctrine_V4.md` (visione), e la
fotografia dell'implementato resta
`docs/reports/MANUALSYNC_PREVIOUS_WORK_HANDOFF.md` §4-6.

---

## 1. Baseline congelata

    commit 9c8c207c390a4727a9c3b7da6749c47cfb64261a
    feat(compliance): complete 4F.2 starter pack workflow

Tutto ciò che segue parte da qui.

---

## 2. Stato: 4F.2 COMPLETATO

Prompt 3.5 (Compliance Intelligence Architecture) è materialmente chiuso:
catena di tracciabilità requisito → norma → rischio → evidenza →
topic/blocco → release, tassonomia Covered / To verify / Missing / Not
applicable con giustificazione obbligatoria, tracciamento delle revisioni
normative con impatto sulle decisioni già prese, audit trail, e il
vincolo "mai certificazione legale automatica".

Il ruolo bloccante che il Prompt Pack assegna al Prompt 3.5 ("must be
settled before Prompt 4, because its data-model decisions constrain how
topics/blocks/evidence are modeled") non blocca più nulla.

Regression alla chiusura: 4F.2 243/243, 3.5A 67/67, 3.5B 63/63,
3.5C 54/54, UI callbacks RC2 13/13, R23.2 12/12.

---

## 3. Non esiste un "4F.3" documentato

Gli identificativi `4E.3 / 4F.1 / 4F.2` sono lo schema di ticket usato
dal committente, NON una numerazione del repository. Verificato con una
ricerca esaustiva su tutti i `.md`/`.txt`/`.json`/`.yaml` del
repository (esclusi `.venv`/`.git`): nessun file definisce una fase
successiva a 4F.2, e l'unica occorrenza letterale di "4F" è una riga di
tabella non correlata in `ZERO_TRAINING_UX_ARCHITECTURE_V1.md`.

Il Prompt Pack definisce Prompt 0-17, senza notazione 4F.

Nota sullo stato della documentazione: `README.md` e
`CLAUDE_DESKTOP_HANDOFF.md` sono entrambi gravemente obsoleti (il primo
descrive il repository come "pre-development", il secondo documenta la
milestone 3D come corrente e il Prompt 3.5 come "non ancora sviluppato").
Vanno riallineati, ma non in un gate di prodotto.

---

## 4. Gate corrente: Publishing Preflight v1 — integrità dell'output

Sottoinsieme minimo e coerente del Prompt 10 necessario a introdurre il
preflight di pubblicazione e l'export fail-closed.

**Che cosa NON è.** Non è un layout engine e non è un rilevatore di
overset general-purpose. Restano esplicitamente fuori: motore di reflow
automatico, motore di impaginazione general-purpose, motore di
cross-reference, tabelle di revisione automatiche, template designer.

In scope:

- modello e report di preflight per l'export di produzione;
- rilevamento di condizioni che rendono l'export non sicuro o incompleto;
- classificazione per severità (bloccante / avviso);
- export di produzione fail-closed sui bloccanti irrisolti;
- report leggibile: cosa è fallito, dove, perché l'export è stato
  bloccato, cosa va corretto;
- comportamento invariato quando non ci sono bloccanti.

Verifica architetturale fatta PRIMA di implementare, e risultato onesto:

- `CONTENT_LOSS` rileva in modo deterministico il contenuto testuale
  **campionato** che non è arrivato nel PDF finale (fino a 24 sonde:
  titolo, intestazioni di sezione, una parola distintiva per blocco,
  ultima colonna di ogni tabella). È il caso reale e raggiungibile in
  questa pipeline, ed è **orizzontale**: una tabella troppo larga viene
  tagliata dal renderer senza alcun segnale geometrico.
- L'**overset verticale non è oggi una condizione raggiungibile** in
  questo renderer: il contenuto normale viene impaginato su più pagine e
  le figure sovradimensionate vengono riscalate per entrare nella pagina.
  Due regole geometriche erano state scritte e sono state rimosse dopo
  misurazione, perché producevano falsi positivi su documenti
  perfettamente validi. Il motivo è registrato nel docstring di
  `publishing_preflight.py` perché non vengano reintrodotte.
- Un futuro layout a **frame fissi o guidato da template** renderà
  l'overset verticale raggiungibile e richiederà un **preflight
  geometrico aggiuntivo**, misurato su quel motore.

---

## 5. Priorità successive

### 5.1 DOCX import UX
`import_docx_outline()` esiste in `publishing_bridges.py` e non è
referenziata da nessun'altra parte del codice: nessun percorso UI.
Classificata PARTIAL in `BASELINE_V3.md` §3, è la Fase 4 del piano UX in
`ZERO_TRAINING_UX_ARCHITECTURE_V1.md` §15, ed è legata direttamente
all'obiezione commerciale "Lavoriamo con Word" (§13 dello stesso
documento). Superficie piccola, leva commerciale alta.

### 5.2 Grounded Authoring integrato nell'editor (Prompt 5)
Oggi esiste come pannello separato dentro "Redazione & contenuti" con un
flusso genera → invia in review. Il Prompt 5 chiede un assistente
IN-EDITOR. Formalmente ancora aperto, e precedente ai Prompt 10/11
nell'ordine di lavoro.

---

## 6. Gap successivi

- **Prompt 10 residuo**: reflow, cross-reference, TOC/index, tabelle di
  revisione, numerazione, semantic styles avanzati, mapping template
  DOCX/DOTX e IDML, asset localizzati. Il preflight di questo gate copre
  l'integrità dell'output e il fail-closed, non la resa tipografica: non
  esiste ancora un harness di regressione visiva, che l'acceptance del
  Prompt 10 richiede esplicitamente.
- **Prompt 11** — datasheet / product-data engine: implementazione zero
  (nessun modulo, nessun modello di specifica tipizzata).
- **Prompt 12-15** — interoperabilità con sistemi esistenti, performance
  e operazioni bulk, enterprise readiness, benchmark harness. Tutti
  ROADMAP, nessuno iniziato.
- **Prompt 16 — Final Commercial Candidate gate**: richiede
  esplicitamente "publishing/layout preflight" tra i gate da superare, e
  `VERSION.txt` riporta ancora "requires one Windows pilot verification
  before commercial freeze".

---

## 7. Disciplina

Ogni gate: implementazione → regression mirata → accettazione manuale →
commit solo su autorizzazione esplicita → push solo su autorizzazione
esplicita. Nessun gate si dichiara completo perché i test passano: si
dichiara completo quando la capability è realmente usabile dal prodotto.
