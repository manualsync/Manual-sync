# ERYLOS — Impact Intelligence Discovery V1

Baseline: `45aac94ae49a66e5c504e2766ee7f22f16b281c3` (UX Gate 2A congelato).

Metodo: lettura del sorgente **e** interrogazione del dato persistito. Il
database del progetto canonico è stato aperto in sola lettura
(`sqlite mode=ro`); le verifiche che richiedono una modifica sono state
eseguite su un progetto `ZZ_` usa-e-getta costruito, verificato e
distrutto da `acceptance_fixture.py`. **Nessuna modifica al codice di
prodotto, nessuna modifica di schema, nessuna migrazione.**

Regola applicata in tutto il documento: una colonna non è una capacità.
Ogni capacità dichiarata esistente cita modulo, funzione e tabella/campo.
Dove la relazione non è dimostrabile dal codice o dal dato, è marcata
**non provata**.

---

## 1. Conclusione esecutiva

ERYLOS possiede **più Impact Intelligence di quanta ne esponga**, e la
possiede nel posto giusto: nel modello dati, non in uno strato di report.

Due catene che il brief descrive come obiettivo strategico **funzionano
già oggi**, e sono state osservate in esecuzione, non dedotte:

- **contenuto → conformità.** Una singola `save_block()` ha spostato la
  copertura di un requisito da `Covered` a `To verify`, da sola, senza
  alcuna chiamata aggiuntiva e senza tabelle nuove.
- **contenuto → traduzione.** La stessa modifica ha portato la traduzione
  `de-DE` di quel blocco da `Reviewed` a `STALE`.

Entrambe funzionano perché il dato è **version-aware alla radice**:
`compliance_evidence.object_version` e `*_translations.source_version`
sono confrontati con la versione viva dell'oggetto ogni volta che lo stato
viene calcolato.

Ciò che manca non è il modello: è la **navigazione inversa e
l'aggregazione**. Oggi il sistema sa rispondere a «questo requisito è
ancora coperto?» e «questa pubblicazione è traducibile?». Non sa
rispondere a «ho cambiato BLK-X, cosa ho appena rotto?», perché nessun
servizio parte dall'oggetto e attraversa i domini. Le query esistono
tutte; nessuno le mette in fila.

Il rischio da evitare in Gate 2B è quindi l'opposto di quello che ci si
aspetterebbe: non «serve costruire l'intelligenza», ma «serve smettere di
ricostruire identità che esistono già». `object_id` è **già** una chiave
cross-dominio (§8).

---

## 2. Matrice delle capacità

Legenda: **AN** = available now · **PA** = partially available ·
**DATA** = dato persistito, nessuna intelligenza · **MISS** = mancante.

### 2.1 Identità e storia

| Capacità | Stato | Evidenza |
|---|---|---|
| Versione corrente | **AN** | `content_blocks.version` |
| Versioni precedenti | **AN** | `block_versions` (testo completo per versione) |
| Diff fra versioni | **AN** | `authoring_view.py:534-542` — `difflib.SequenceMatcher` a livello di parola |
| Timestamp | **AN** | `content_blocks.created_at/updated_at`, `block_versions.created_at` |
| Stato archiviato/ripristinato | **AN** | `content_blocks.archived_at`; `AuthoringService.archive_block/restore_block` |
| Stato di review corrente | **PA** | `content_blocks.status` + `review_tasks.status`; vedi §5 |
| Eventi di audit dell'oggetto | **AN** | `audit_events.object_id` |
| **Autore di una specifica versione** | **PA** | `block_versions` **non ha colonna actor**. L'autore è recuperabile solo correlando `audit_events.actor` per timestamp |

### 2.2 Contesto strutturale

| Capacità | Stato | Evidenza |
|---|---|---|
| Topic contenitore | **AN** | `content_blocks.topic_id` (FK) |
| Pubblicazione contenitore | **AN** | `publication_topics` |
| Posizione/ordine | **AN** | `content_blocks.position`, `publication_topics.position` |
| Tabelle/figure figlie | **AN** | `content_blocks.object_ref_id` |
| Relazione shared-object | **AN** | `AuthoringService.get_reuse_impact()` (`authoring_service.py:1774`) |
| Relazione sorgente/origine | **DATA** | `source_refs_json` è testo libero citazionale, **non** una FK |

### 2.3 Riuso

| Capacità | Stato | Evidenza |
|---|---|---|
| Dove è riusato un oggetto | **AN** | `get_reuse_impact()`; `ccms_industrial.impact_map_industrial()` (`:1063`) |
| Topic che lo contengono | **AN** | idem |
| Pubblicazioni impattate | **AN** | idem, via `publication_topics` |
| Shared vs forked | **PA** | `is_shared` è calcolato contando i riferimenti; **il fork non lascia traccia** |
| **Lineage del fork** | **MISS** | `structured_tables` / `figures` non hanno colonna origine. `fork_table_local()` ripunta `object_ref_id` e nulla registra la provenienza |
| Propagazione o sola rilevazione | **AN (propagazione)** | «Aggiorna ovunque» modifica l'oggetto condiviso in loco, con token di impatto rileggibile (`_guard_table_or_figure_editable`) |

> Nota di perimetro: **il riuso è modellato solo per table / figure /
> component_ref**. Un blocco di testo non è riusabile: non esiste un
> meccanismo per referenziare lo stesso paragrafo da due topic.

### 2.4 Varianti

| Capacità | Stato | Evidenza |
|---|---|---|
| Dimensioni applicabili | **AN** | `condition_dimensions`, `content_blocks.condition_json` |
| Valori applicabili | **AN** | `condition_values` |
| Profili che usano l'oggetto | **PA (derivabile)** | `AuthoringService.evaluate_element_with_profile()` valuta `condition_json` contro `profiles.dimensions_json`. **Nessuna tabella profilo↔oggetto** |
| Pubblicazioni prodotte sotto quei profili | **AN** | `publication_releases(publication_id, profile_id, target_lang)` |
| Tutti i profili o un sottoinsieme | **PA (derivabile)** | iterando `evaluate_element_with_profile` su `profiles`; nessun servizio lo fa |
| **Contenuto specifico per profilo / forkato** | **MISS** | un blocco ha **un** testo più una condizione. Non esiste tabella di varianti testuali per profilo |

### 2.5 Traduzione — *sezione strategica*

| Capacità | Stato | Evidenza |
|---|---|---|
| Identità di segmento sorgente stabile | **MISS** | i segment id sono **ricalcolati** a ogni export (`translation_impact.units_to_segments`, `translation_work_package.export_work_xliff`), non persistiti |
| Traduzione → oggetto sorgente | **AN** | FK `block_translations.block_id` → `content_blocks` |
| Traduzione → **versione** sorgente | **AN** | `*_translations.source_version` su **tutte e cinque** le tabelle di traduzione |
| Lingue target esistenti | **AN** | `SELECT DISTINCT target_lang` sulle tabelle di traduzione |
| Stato traduzione per lingua | **AN** | `*_translations.status` |
| Stato review/approvazione | **PA** | lo `status` della traduzione è testo libero (`Reviewed`, …), non un workflow con task |
| Identità unità XLIFF | **DATA** | generata e verificata per export/import (`translation_work_package`, `translation_roundtrip._parse_xliff`), non persistita nel progetto |
| Identità di round-trip | **AN** | `translation_roundtrip.inspect_translation_roundtrip()` verifica sha256 e versione oggetto corrente (`_current_object_version`, `:142`) |
| **Distinguere traduzioni aggiornate da obsolete** | **AN — provato** | `assemble_structured_publication` marca `STALE` quando `source_version != version` (`ccms_industrial.py:846`) |
| **Sapere che v8 ha superato una traduzione fatta su v6** | **AN — provato** | stessa comparazione; osservato passare `Reviewed → STALE` |
| Classificazione exact / fuzzy / new | **AN** | `cat_tm.match_segments()`; bande in `cat_batch.match_band()` |
| Percentuale fuzzy | **AN** | `linguistic_score`; bande `100%`, `95-99%`, `85-94%`, `75-84%`, `<75%` |
| Context match | **AN** | `tm_entries.prev_norm` / `next_norm` → categoria `CONTEXT MATCH` |
| Translation Memory interna | **PA** | `cat_tm.TranslationMemory` è un **sqlite separato**, non dentro il progetto |
| Dati TM esterni importati/preservati | **AN** | `build_tm_from_documents()`, `tm_name` / `priority` / `penalty` per TM multiple |
| Conteggi parole/segmenti | **AN** | `cat_batch.word_count()`, `word_count_by_band()`, `repetition_report()` |
| Stima costi | **AN** | `analyze_translation_impact(rate_card=…)` con `rate_new` / `rate_fuzzy` / `rate_reuse` / `rate_technical_block` |
| Rilevazione cambi numerici/unità | **AN** | `cat_tm.technical_signature()` + `signatures_compatible()`: numeri, codici modello, unità, con motivazione esplicita |

> **Non sovrastimare XLIFF.** L'export/import funziona ed è verificato,
> ma da solo non è Translation Intelligence. Il valore reale qui è
> altrove: `source_version` su ogni tabella di traduzione, e la firma
> tecnica che blocca un riuso TM quando i numeri non coincidono.

### 2.6 Conformità

| Capacità | Stato | Evidenza |
|---|---|---|
| Evidenze che coinvolgono l'oggetto | **PA (derivabile)** | `compliance_evidence.object_id` — join diretto, **nessun servizio lo espone in questa direzione** |
| Requisiti supportati da quell'evidenza | **PA (derivabile)** | `compliance_evidence_link` |
| Stato di copertura del requisito | **AN** | `ComplianceService.compute_status()` (`:1265`) |
| Decisione di applicabilità | **AN** | `compliance_applicability.applies` |
| **L'evidenza punta a una versione o solo a un oggetto** | **AN — versione** | `compliance_evidence.object_version`, validata alla creazione da `_object_version_exists()` |
| **Una modifica può invalidare la copertura** | **AN — provato** | `compute_status()` degrada `COVERED → To verify` quando `_has_valid_current_evidence()` fallisce |
| Aggiornamenti pack regolatori correlati alla documentazione | **PA** | `requirement_is_current()` rileva la deriva dello strumento; la correlazione **verso i documenti impattati** non esiste |

### 2.7 Review e workflow

| Capacità | Stato | Evidenza |
|---|---|---|
| L'oggetto è in review? | **AN** | `content_blocks.status == 'In Review'`; `review_tasks.status == 'OPEN'` |
| Chi deve revisionare | **AN** | `review_tasks.assigned_to` |
| Quale decisione esiste | **AN** | `review_tasks.decision` (`APPROVE`/`REJECT`) |
| **Una nuova versione invalida la review?** | **PA** | `_status_after_edit()` (`authoring_service.py:344`) riporta `Approved`/`In Review` a `Draft`: l'approvazione **è** invalidata. Ma **nessun `review_task` viene riaperto o annullato** |
| Stato di approvazione a valle | **PA** | derivabile per pubblicazione via `review_queue(status='OPEN')` filtrata sugli oggetti (`release_readiness.py:89`) |
| **Review legata a versione o a identità** | **MISS** (legata all'identità) | `review_tasks` **non ha colonna version** (verificato sullo schema) |

### 2.8 Release

| Capacità | Stato | Evidenza |
|---|---|---|
| Pubblicazioni contenenti il contenuto modificato | **AN** | `impact_map_industrial()` |
| Stato di readiness | **AN** | `release_readiness.assess_release_readiness()` |
| Review pendenti | **AN** | incluse nella readiness (`:89-91`) |
| Completezza traduzioni | **AN** | `missing_translations` + `stale_translations` nella readiness (`:108-110`) |
| Readiness di conformità | **PA** | la readiness considera `evidence_status == SOURCE_GROUNDED`, **non** lo stato di copertura dei requisiti |
| Stato preflight | **AN** | `publishing_preflight.check_model()` / `PreflightReport` |
| **Una modifica rende una release «needs attention»** | **MISS** | nessuna rivalutazione: `publication_releases` è uno snapshot immutabile e nulla lo confronta con lo stato vivo |
| **Release version-aware** | **PA** | lo snapshot (`snapshot_json` + `snapshot_sha256`) è per costruzione un punto nel tempo, ma **non esiste indice release → versioni oggetto**: solo il parsing del JSON |

### 2.9 Grounded Authoring

| Capacità | Stato | Evidenza |
|---|---|---|
| Riferimenti sorgente ricevuti | **AN** | `GroundingStore.source_chunks(source_ref, page, technical_json)` |
| Provenienza persistita | **AN** | `grounding_audits(draft_sha256, cited_chunks_json, result_json)` |
| **Proposta legata a oggetto/versione** | **MISS** | `generation_proposals` non ha `object_id` né `version`. L'utente digita a mano topic/block id al momento dell'accodamento (`app.py:2028`) |
| Le proposte accettate entrano nella storia normale | **AN** | `repo.add_block(...)` → `content_blocks` + `block_versions` |
| Le citazioni sopravvivono all'accettazione | **AN** | `source_refs=refs` → `content_blocks.source_refs_json`; `evidence_status='SOURCE_GROUNDED'` |
| Approvazione umana obbligatoria | **AN** | `request_review()` immediata; nessuna auto-approvazione (`app.py:2038`) |
| Utilizzabile come fornitore contestuale in 2B senza riprogettare il motore | **AN** | il motore è già «ingerisci → proponi → valida → accoda a review». Manca solo il legame con l'oggetto aperto |

---

## 3. Grafo delle dipendenze attuale

`═══` arco persistito (FK o colonna) · `───` arco derivabile
deterministicamente · `╌╌╌` arco desiderato e **assente**.

```
                    ┌──────────────┐
                    │ Publication  │
                    └──────┬───────┘
              ═══════════ │ publication_topics ═══════════
                          ▼
                    ┌──────────────┐        ═══► topic_versions
                    │    Topic     │        ═══► topic_translations(source_version)
                    └──────┬───────┘
                   ═══ content_blocks.topic_id ═══
                          ▼
    ┌──────────────────────────────────────────────┐
    │                  BLOCK                       │
    │  content_blocks(version, status, condition)  │
    └───┬─────┬──────┬───────┬────────┬────────┬───┘
        ║     ║      ║       ║        ║        ║
        ║     ║      ║       ║        ║        ╚═══► audit_events(object_id, actor)
        ║     ║      ║       ║        ╚════════════► authoring_drafts(object_id)
        ║     ║      ║       ╚═════════════════════► review_tasks(object_id)   ← NO version
        ║     ║      ╚═════════════════════════════► block_versions(version, text)
        ║     ╚════════════════════════════════════► block_translations(target_lang, source_version)
        ╚══════════════════════════════════════════► content_blocks.object_ref_id
                                                          ║
                                                          ▼
                                            ┌─────────────────────────┐
                                            │ structured_tables /     │
                                            │ figures / components    │  (RIUSO)
                                            └─────────────────────────┘
                                                 ╌╌╌► forked_from   ASSENTE

   BLOCK ═══► compliance_evidence(object_id, object_version)
                    ═══► compliance_evidence_link ═══► compliance_requirement
                                                          ═══► compliance_assessment
                                                          ═══► compliance_applicability
                                                          ═══► compliance_regulatory_instrument

   BLOCK ───► profili applicabili        (evaluate_element_with_profile su condition_json
                                          vs profiles.dimensions_json — nessuna tabella)

   Publication + profile + lang ═══► publication_releases(snapshot_json, snapshot_sha256)
                                       ╌╌╌► release ↔ versioni oggetto contenute  ASSENTE
                                       ╌╌╌► rivalutazione «needs attention»       ASSENTE

   BLOCK ╌╌╌► generation_proposals    ASSENTE (proposta non legata all'oggetto)
```

**Osservazione centrale del grafo**: tutti gli archi verso conformità,
review, audit e draft passano per la *stessa* colonna non tipizzata,
`object_id`. Non è un caso da correggere: è la spina dorsale su cui
Impact Intelligence deve appoggiarsi.

---

## 4. Traccia di una modifica di contenuto (esempio reale)

Oggetto scelto nel progetto canonico: `BLK-25D4C6775D`, `block_type =
warning` — il caso rilevante per la sicurezza.

**Stato letto (sola lettura, demo canonica)**

```
version 1 · status Draft · evidence_status SOURCE_GROUNDED · condition {}
block_versions: 1 riga (v1, testo completo)
audit_events : 1 (AUTHORING_CREATE, actor 'Responsabile Qualità')
review_tasks : 0
topic        : T-SIC «Sicurezza» v1
publication  : PUB-MAIN, posizione 3
compliance   : EV-block-BLK-25D4C6775D-v1 → MR2023-A3-17442-MISUSE-WARNINGS (CURRENT)
```

**Esperimento (progetto `ZZ_` usa-e-getta, poi distrutto)** — una sola
`AuthoringService.save_block()`, nient'altro:

| | prima | dopo |
|---|---|---|
| versione blocco | 1 | **2** |
| `compute_status(scope, requisito)` | `Covered` | **`To verify`** |
| `evidence.is_current` | `True` | **`False`** |
| traduzione `de-DE` | `Reviewed` | **`STALE`** |
| `stale_translations` della pubblicazione | 0 | **1** |
| `review_tasks` | 0 | 0 *(invariato)* |

Due archi di impatto hanno reagito **da soli**. Nessuno dei due è
esposto all'utente, e nessuna API parte dall'oggetto per raccoglierli.

**Ciò che invece non ha reagito**, verificato nello stesso esperimento:
`impact_map_industrial(BLK, kind='block')` restituisce topic e
pubblicazione, ma riporta le traduzioni **del topic**, non quelle del
blocco — la traduzione `de-DE` appena resa obsoleta non compare — e non
ha alcuna colonna di conformità, review o release.

---

## 5. Valutazione della lineage di traduzione

Il modello di traduzione è **più forte di quanto il prodotto mostri**, e
il suo punto di forza non è XLIFF.

**Ciò che regge oggi.** Ogni tabella di traduzione porta
`source_version`. La comparazione con la versione viva è già implementata
e già consumata a valle: `assemble_structured_publication` produce
`translation_status ∈ {STALE, MISSING, NOT_REQUIRED, <status>}` e
`assess_release_readiness` ne conta i totali. La domanda «questa
traduzione discende da una versione superata?» ha quindi già una
risposta, per ogni oggetto e ogni lingua.

Sopra a questo, `cat_tm` fornisce una TM con qualità da CAT vero:
context match (`prev_norm`/`next_norm`), bande fuzzy, e soprattutto una
**firma tecnica** (`numbers_json`, `codes_json`, `units_json`) che rifiuta
un riuso quando i valori numerici, i codici modello o le unità non
coincidono — con motivazione leggibile. È esattamente la regola che la
Product Doctrine impone (un mismatch tecnico non può essere assorbito da
un punteggio fuzzy alto), ed è già codice funzionante.

**La vera lacuna.** Non esiste **identità di segmento persistita**. I
segment id nascono al momento dell'export e muoiono con esso. Ne
discende che:

- la lineage è a livello di **oggetto**, non di segmento: si sa che il
  blocco è cambiato, non *quale frase* è cambiata;
- la spiegazione «cosa è cambiato fra v6 e v8» esiste per il **sorgente**
  (`authoring_view` diff parola per parola) ma non è collegata alla
  traduzione;
- un match TM non può essere ricondotto a una specifica traduzione
  storica dell'oggetto.

Seconda lacuna: la TM vive **fuori** dal progetto, come file sqlite
passato per path (`analyze_translation_impact(tm_paths=…)`). Non esiste
una TM di progetto alimentata automaticamente dalle traduzioni approvate.

Terza: `analyze_translation_impact` è **fail-closed su contenuto
ufficiale** (`require_approved=True`, rifiuta status diverso da
`Approved`/`Published`). Corretto come politica, ma significa che
l'analisi non è disponibile durante la normale redazione in `Draft`.

---

## 6. Conformità, review e release

**Conformità — il dominio più maturo.** È l'unico che ha già una nozione
di *validità corrente* calcolata anziché memorizzata. `compute_status()`
degrada a `To verify` in quattro casi distinti: applicabilità stale,
strumento regolatorio avanzato di versione, assessment fatto su un'altra
versione del requisito, evidenza che non punta più alla versione viva
dell'oggetto. Tre di questi sono *già* impatto cross-dominio. La
direzione mancante è solo quella inversa (oggetto → requisiti toccati),
che è un join su `compliance_evidence.object_id`.

**Review — il dominio più debole.** `review_tasks` non ha versione. Ne
seguono due conseguenze concrete:

1. una review approvata resta chiusa anche quando il contenuto cambia —
   l'invalidazione avviene solo indirettamente, perché
   `_status_after_edit()` riporta l'oggetto a `Draft`;
2. non è possibile dire «questa approvazione riguardava la v3»: la
   traccia esiste solo in `audit_events`, per timestamp.

Questa è la **prima lacuna da colmare** se si vuole una catena di impatto
credibile, ed è una colonna, non un sottosistema.

**Release — aggregato forte, memoria assente.** `assess_release_readiness`
sa già rispondere, *nel momento in cui viene chiamato*, su review aperte,
traduzioni mancanti e stale, grounding e preflight. Ma
`publication_releases` è uno snapshot immutabile: nessun processo
riconfronta una release già emessa con lo stato vivo, quindi nessuna
release può diventare «needs attention» da sola.

**La frase obiettivo del brief** — «release tedesca bloccata perché due
traduzioni di sicurezza derivano da una versione sorgente obsoleta» —
si scompone così:

| componente | stato |
|---|---|
| «release tedesca» | **AN** — `publication_releases.target_lang`, readiness per lingua |
| «bloccata» | **AN** — `validate_publication_for_release()` produce già issues bloccanti |
| «due traduzioni» | **AN** — conteggio `stale_translations` per pubblicazione+lingua |
| «derivano da una versione sorgente obsoleta» | **AN — provato** | `source_version != version` |
| «**di sicurezza**» | **PA** — derivabile da `block_type == 'warning'`; nessuna classificazione di criticità esplicita |

Quattro componenti su cinque esistono. La frase è costruibile **oggi**,
in forma leggermente più debole («due traduzioni, di cui due su blocchi
di tipo avviso»). Non richiede nuovi concetti di dominio.

---

## 7. Relazioni e dati mancanti

In ordine di costo crescente.

1. **`review_tasks.version`** — una colonna. Senza, la review non è
   version-aware e la catena di impatto ha un anello rotto.
2. **Navigazione inversa oggetto → requisiti** — nessun dato nuovo, solo
   un metodo di servizio su un join esistente.
3. **Indice release → versioni oggetto** — oggi ricavabile solo
   parsendo `snapshot_json`. Serve per rendere una release rivalutabile.
4. **Lineage del fork** (`forked_from` su tabelle/figure) — una colonna;
   senza, «questa copia locale derivava dall'originale» è perso.
5. **Classificazione di criticità del contenuto** — oggi approssimata da
   `block_type='warning'`. Una nozione esplicita di safety-critical
   servirebbe a qualunque affermazione di sicurezza.
6. **Identità di segmento persistita** — il cambiamento di modello più
   grande della lista, e l'unico che sblocca la Translation Intelligence
   a livello di frase.
7. **Legame proposta → oggetto/versione** in `generation_proposals`.
8. **TM di progetto alimentata dalle traduzioni approvate.**

Non serve, e va evitato: una nuova identità di oggetto. Esiste già.

---

## 8. Identificatori cross-dominio

Misurato sullo schema reale (39 tabelle).

| Identificatore | Tabelle | Attraversa domini? |
|---|---:|---|
| `object_id` (non tipizzato) | 4 | **SÌ** — `audit_events`, `review_tasks`, `compliance_evidence`, `authoring_drafts` |
| `requirement_id` | 7 | sì, dentro conformità |
| `topic_id` | 6 | authoring ↔ traduzione ↔ pubblicazione |
| `publication_id` | 4 | **SÌ** — authoring, release, `compliance_scope` |
| `profile_id` | 3 | **SÌ** — varianti, release, `compliance_scope` |
| `block_id` | 3 | authoring ↔ traduzione |
| `target_lang` | 6 | traduzione ↔ release |
| `object_version` / `source_version` | 7 | **SÌ** — è il collante temporale |

**Conclusione operativa.** ERYLOS ha già un'identità di contenuto
universale: la coppia **(`object_type`, `object_id`)**, accompagnata da
una versione. Conformità, review, audit e draft la usano tutti. Impact
Intelligence deve **comporre questa identità**, non introdurne un'altra:
un nuovo `content_entity_id` duplicherebbe una chiave già adottata da
tre domini.

---

## 9. Impact Intelligence V1 proposta

Obiettivo: rispondere a «ho cambiato questo oggetto, cosa è impattato?».
Tutto ciò che segue si appoggia a dati esistenti.

| Voce restituita | Classificazione | Come |
|---|---|---|
| N pubblicazioni impattate | **già disponibile** | `impact_map_industrial()` |
| N sedi di riuso | **già disponibile** | `get_reuse_impact()` (table/figure/component) |
| N profili/varianti interessati | **derivabile ora** | iterare `evaluate_element_with_profile()` sui profili attivi |
| N traduzioni rese obsolete, per lingua | **derivabile ora** | `*_translations.source_version != version` — la query esiste già dentro `assemble_structured_publication`, va estratta per oggetto |
| Stato di review | **già disponibile** | `review_tasks` + `content_blocks.status` |
| Requisiti di conformità toccati e loro nuovo stato | **derivabile ora** | join `compliance_evidence.object_id` → `evidence_link` → `compute_status()` |
| Pubblicazioni la cui readiness cambierebbe | **piccolo gap** | `assess_release_readiness()` esiste ma è per pubblicazione; va invocata sulle pubblicazioni impattate |
| Release già emesse da marcare «needs attention» | **richiede nuova persistenza** | serve l'indice release → versioni |
| Impatto a livello di segmento/frase | **rinviato** | richiede identità di segmento (§7.6) |
| Stima costo di ri-traduzione | **derivabile ora, con TM** | `analyze_translation_impact(rate_card=…)`; richiede una TM collegata |

**Forma raccomandata**: una funzione pura di lettura, in un modulo nuovo
(`impact_intelligence.py`), firma `assess_object_impact(repo, object_id,
kind) -> dict`, che **non scrive nulla** e compone chiamate esistenti.
Nessuna tabella nuova in V1. Questo la rende testabile esattamente come
`publishing_preview`, che è già puro e testato standalone.

**Fuori da V1, deliberatamente**: riscrivere `impact_map_industrial`
(che resta utile a granularità topic), qualunque nozione di propagazione
automatica, e qualunque scrittura di stato.

---

## 10. Translation Intelligence V1 — fattibilità

| # | Capacità | Fattibilità | Giudizio |
|---|---|---|---|
| 1 | Lineage di versione sorgente | esiste (`source_version`) | **HIGH VALUE / LOW COST** |
| 2 | Rilevazione traduzioni obsolete | esiste e provata | **HIGH VALUE / LOW COST** |
| 3 | Exact match | esiste (`match_segments`) | **HIGH VALUE / LOW COST** |
| 4 | Context match | esiste (`prev_norm`/`next_norm`) | **HIGH VALUE / LOW COST** |
| 5 | Percentuale fuzzy | esiste (`linguistic_score`, bande) | **HIGH VALUE / LOW COST** |
| 6 | Spiegazione dei token cambiati | diff esiste per il sorgente, non legato alla traduzione | **DEPENDENCY FIRST** — richiede identità di segmento |
| 7 | Rilevazione cambi numerici/unità | esiste (`signatures_compatible`) | **HIGH VALUE / LOW COST** — è il differenziatore più forte |
| 8 | Impatto per lingua | esiste | **HIGH VALUE / LOW COST** |
| 9 | Flag traduzione safety/compliance-sensitive | `block_type='warning'` + evidenza di conformità sull'oggetto | **HIGH VALUE / HIGH COST** — approssimabile subito, corretta solo con una classificazione esplicita |
| 10 | Delta parole/segmenti/costo | esiste (`word_count_by_band`, `rate_card`) | **HIGH VALUE / HIGH COST** — dipende da una TM collegata al progetto |

**V1 raccomandata: le voci 1, 2, 3, 4, 5, 7, 8.** Sono tutte già
implementate a livello di servizio; il lavoro è esporle per oggetto e
per lingua, non costruirle. Le voci 6, 9, 10 sono da rinviare, ciascuna
per una dipendenza diversa e nominata.

---

## 11. Rilevanza competitiva

Nessuna ricerca esterna: giudizio basato solo sull'architettura letta.

**Genuinamente distintivo**
- La **firma tecnica** che blocca il riuso TM su discrepanza di numeri,
  codici o unità, con motivazione leggibile. Un fuzzy alto non può
  assorbire un mismatch tecnico: è una regola di sicurezza, non un
  punteggio.
- La **conformità che si riapre da sola** quando il contenuto che la
  sosteneva cambia versione. Non «un report di conformità», ma uno stato
  calcolato e fail-closed.
- Il **grounding gate** che impedisce l'auto-approvazione di contenuto
  generato, con citazioni che sopravvivono nell'oggetto salvato.

**Funzionalità CCMS standard**
- Versionamento topic/blocco, riuso di tabelle e figure, condizioni di
  variante e profili, export XLIFF, snapshot di release.

**Promettente ma non implementato**
- Impatto inverso da un oggetto attraverso i domini.
- Rivalutazione di una release già emessa.
- Intelligenza a livello di segmento.

**Affermazioni da NON fare oggi**
- «Translation Intelligence» in senso pieno: manca l'identità di
  segmento, quindi non si può spiegare *cosa* è cambiato in una frase.
- «Impact analysis» come capacità di prodotto: gli archi esistono, il
  servizio che li percorre no.
- «Compliance-aware releases»: la readiness **non** considera lo stato
  di copertura dei requisiti, solo `evidence_status`.
- Qualunque claim di propagazione automatica verso le traduzioni.

---

## 12. Raccomandazione per Gate 2B

Gate 2B non dovrebbe essere «mettere Grounded Authoring nel pannello
destro». Dovrebbe essere **rendere visibile l'impatto che il sistema già
calcola**, perché è il percorso con il rapporto valore/rischio più alto
oggi disponibile.

Ordine consigliato:

1. `impact_intelligence.py` — modulo puro, sola lettura, `assess_object_impact()`.
   Nessuna tabella nuova. Testabile standalone.
2. Aggiungere `review_tasks.version` — una colonna additiva, che chiude
   l'unico anello rotto della catena.
3. Esporre il risultato nel pannello contestuale destro del workspace
   2A, accanto all'oggetto aperto.
4. Solo dopo, l'integrazione di Grounded Authoring come fornitore di
   proposte ancorate, riusando lo stesso pannello.

Identità di segmento, rivalutazione delle release e lineage del fork
restano fuori: sono cambi di modello e meritano un gate proprio.

---

## 13. Quanto esiste già?

**Valutazione qualitativa.** Il *modello dati* di ERYLOS è
sostanzialmente pronto per la Documentation Intelligence: è
version-aware nei tre punti che contano (evidenza di conformità,
traduzione, storia dell'oggetto) e ha già un'identità di contenuto
condivisa fra domini. Il *livello di calcolo* è maturo dove è stato
costruito per necessità — conformità e traduzione — e assente dove
nessuna funzionalità lo ha ancora richiesto — impatto inverso e
rivalutazione. Il *livello di prodotto* è a zero: nulla di tutto questo
è raggiungibile da un utente.

Detto senza indulgenza: ERYLOS non deve costruire l'intelligenza, deve
**raccoglierla**. La parte difficile — rendere il dato version-aware e
fail-closed — è già stata pagata.

**Stima quantitativa.** Contando meccanicamente le 72 righe della
matrice §2 (conteggio riproducibile con uno script sul documento stesso):

| Stato | Conteggio | Quota |
|---|---:|---:|
| AVAILABLE NOW | 50 | 69% |
| PARTIALLY AVAILABLE | 14 | 19% |
| DATA EXISTS, NO INTELLIGENCE | 2 | 3% |
| MISSING | 6 | 8% |

Pesando *parziale* a metà e *data-only* a un quarto:

```
(50 + 14×0.5 + 2×0.25) / 72  =  57.5 / 72  ≈  80%
```

**Questo numero va corretto verso il basso, e la ragione è metodologica.**
Il conteggio per righe tratta «identità di segmento persistita» — che è un
cambio di modello dati, il più costoso dell'intero elenco — esattamente
come «lingue target esistenti», che è una `SELECT DISTINCT`. La matrice è
inoltre granulare proprio dove ERYLOS è forte (la traduzione ha da sola
18 righe) e sintetica dove è debole. Il conteggio grezzo misura quante
domande sanno rispondere, non quanto lavoro resta.

Ripesando per **costo di realizzazione** anziché per numero di voci — le
sei lacune di §7 che richiedono nuova persistenza contano per quello che
valgono in implementazione — la stima onesta scende a:

**≈ 60-65% esiste già.**

La ripartizione del residuo è la parte utile:

| Residuo | Quota del mancante | Natura |
|---|---:|---|
| Aggregazione e navigazione inversa | ~60% | nessun modello nuovo, solo un servizio che percorre join esistenti |
| Nuova persistenza | ~40% | identità di segmento, `review_tasks.version`, indice release, lineage fork |

Due avvertenze sulla misura, entrambe importanti.

La prima: questa percentuale descrive le **capacità di back-end**. La
quota raggiungibile da un utente oggi è sostanzialmente **0%** — nessuna
delle due catene di impatto dimostrate in §4 ha una superficie di
prodotto. La distanza fra ~62% e 0% è precisamente il motivo per cui
Gate 2B vale la pena, e spiega perché la raccomandazione di §12 mette
l'esposizione prima di qualunque nuovo modello.

La seconda: la percentuale è alta perché la parte *difficile* è già
stata pagata. Rendere un dato version-aware e fail-closed dopo il fatto è
costoso e rischioso; ERYLOS lo ha già fatto dove serviva. Ciò che resta
è in prevalenza lavoro di composizione, che è il tipo di lavoro che si
può fare in modo incrementale e verificabile.
