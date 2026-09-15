# ERYLOS — Content Authoring UX V1 (Contenuti Workspace Discovery)

Baseline analizzata: `61e3d3cf12ab1343f172c452bdbfd5b6a7bb9f78`
(«refactor(authoring): migrate archived-object errors to codes»).

Metodo: inventario del sorgente (`app.py` righe 5314–6088,
`authoring_service.py`) **e** misurazione dell'applicazione in esecuzione —
Chrome via CDP, viewport 1920×1080, progetto `ERYLOS_Canonical_Demo`, con un
blocco **aperto in editing**, cioè lo stato reale di lavoro quotidiano. Ogni
numero in questo documento è misurato, non stimato. **Nessun codice di
prodotto è stato modificato.** Tutte le sessioni di misura hanno riportato
delta overlay `+0`.

Documento precedente di riferimento:
[`ERYLOS_UX_ARCHITECTURE_AND_LOCALIZATION_V1.md`](ERYLOS_UX_ARCHITECTURE_AND_LOCALIZATION_V1.md).
Quel documento misurava Contenuti **a blocco chiuso** (2110 px). Qui la misura
è a blocco aperto (2695 px): non è una contraddizione, è lo stato che l'utente
vede effettivamente mentre lavora.

---

## 1. Inventario dello stato attuale

### 1.1 Struttura del sorgente

La scheda Contenuti occupa **775 righe** di `app.py` (5314–6088).

| Metrica | Valore |
|---|---:|
| Costruzioni `gr.*` totali | 195 |
| Componenti con nome | 143 |
| Controlli interattivi | 100 |
| Contenitori di layout | 37 |
| `gr.Dataframe` dichiarati | 12 |
| `gr.Accordion` | 4 |

Ripartizione: Button 49, Textbox 27, Row 27, Markdown 22, HTML 16,
Dataframe 12, State 8, Dropdown 8, Column 5, Checkbox 4, Accordion 4,
Number 4, Radio 3, Image 3, File 2, Tab 1.

> Nota di riconciliazione: il documento V1 precedente riportava «774 righe e
> 152 componenti». La differenza dipende dal criterio di conteggio (qui si
> contano tutte le costruzioni `gr.*`, incluse quelle inline non assegnate a
> una variabile). Entrambi i numeri sono corretti nel proprio criterio; si
> adotta d'ora in poi quello di questa tabella.

Dei 12 `gr.Dataframe` dichiarati, solo **2 risultano montati e visibili**
contemporaneamente: gli altri stanno dentro accordion chiusi o rami non
attivi. La pagina è quindi pesante per struttura, non per rendering.

### 1.2 Ordine di comparsa (sorgente)

| Riga | Elemento |
|---:|---|
| 5329 | «La tua identità» (operatore) |
| 5364 | «Revisione del contenuto importato» |
| 5377 | «Approva sorgente» — pulsante primario blu |
| 5389 | «Editor professionale» |
| 5500 | barra strumenti rich-text |
| 5560 | tabelle |
| 5588 | figure |
| 5610 | «Varianti» |
| 5667 | «Salva contenuto» |
| 5681 | «Dettagli tecnici» (outline, cronologia, confronta/ripristina) |
| 5710 | «Anteprima pubblicazione» |
| 5731 | Esporta PDF / HTML |
| 5743 | «Dettagli tecnici» (secondo accordion omonimo) |
| 5749 | **«AI Grounded Authoring»** — annidata in due accordion |
| 5757 | «Coda di revisione» |

### 1.3 Geometria misurata (1920×1080, blocco aperto)

```
pageHeight              2695     (2,5 schermate)
identityTop              357
structureTop            1059
editorTop               1276     <- sotto la piega
editorHeight             120     (min-height 120px)
variantsTop             1422
saveTop                 2220
previewTop              2477
exportPdfTop            2780
controlsBeforeEditor      19
totalControls             52
editorToSaveGap          944
hScroll                    0
editorWidth              917     (48% del viewport)
```

Cosa contiene realmente la prima schermata (primi 1080 px): FLUSSO DI LAVORO,
La tua identità, Il tuo nome (×2), Revisione del contenuto importato, Elementi
da verificare, Confermo di aver revisionato (×2), Editor professionale,
Struttura manuale (×2).

Etichette duplicate nella stessa pagina: «Il tuo nome» ×2, «Titolo nuovo
topic» ×2, «Cerca nel manuale» ×2, **«Profilo» ×2** (una per la valutazione
varianti, una per l'anteprima).

ID tecnici grezzi visibili all'utente: **0**. Questo è un punto di forza già
acquisito e va preservato.

### 1.4 Meccanismi già corretti da preservare

Tre comportamenti esistenti sono buoni e **non** vanno persi nella
riprogettazione:

1. **Autosalvataggio bozza.** Ogni campo di testo è legato a
   `studio_save_draft_ui` su `.blur()`; la griglia tabella su `.input()`
   (scelta deliberata e commentata nel sorgente: `.change()` scriveva bozze
   spurie sui push programmatici). Il lavoro non salvato **non** viene perso.
2. **Recupero bozza esplicito.** `Ripristina bozza` / `Scarta bozza` /
   `Scarta modifiche` (righe 5649, 5650, 5669), nascosti finché non esiste
   una bozza.
3. **Canale HTML↔Python già indurito.** `gr.HTML(js_on_load=…)` +
   `window.__msRichEditableSaveSyncer` + hook click in fase di cattura su
   `#ms-save-btn`: l'editor rich-text scrive il proprio DOM dentro il
   `props.value` tracciato da Gradio **prima** che parta l'handler di
   salvataggio. È un precedente funzionante e riusabile (§9).

---

## 2. Problemi UX misurati

Le suite passano. Questo non implica che la schermata sia usabile. I problemi
che seguono sono misurati, non opinioni.

**P1 — L'editor è sotto la piega.** `editorTop = 1276` su un viewport di
1080. L'oggetto che l'utente è venuto a modificare non è visibile
all'apertura. Prima di raggiungerlo si attraversano **19 controlli**.

**P2 — L'editor è alto 120 px e largo il 48% dello schermo.** Su un monitor
da 1920 px la superficie di scrittura occupa 917×120 px, circa il 5%
dell'area disponibile. Il resto è cronologia, configurazione e azioni di
rilascio.

**P3 — Salvataggio a 944 px dall'editor.** `editorToSaveGap = 944`. Tra il
testo che si sta scrivendo e il pulsante che lo salva si interpone un pannello
varianti Criterio/Valori a 4 righe. L'azione più frequente della giornata
richiede uno scroll.

**P4 — Quattro cadenze diverse sulla stessa pagina.** La pagina mescola senza
gerarchia: *una volta per progetto* (identità operatore, dimensioni e profili
varianti), *una volta per manuale* (import, approva sorgente), *molte volte al
giorno* (trova → apri → modifica → salva), *una volta per release* (anteprima,
export PDF/HTML, coda di revisione). Il ciclo quotidiano è quello sepolto in
mezzo.

**P5 — Contesto duplicato e divergente.** Due selettori «Profilo» indipendenti
sulla stessa pagina (valutazione varianti e anteprima) possono essere
impostati su valori diversi: l'utente può valutare un blocco con un profilo e
pubblicare con un altro senza alcun segnale. «Il tuo nome» compare due volte.

**P6 — La struttura del manuale non è un albero.** `st_outline_radio` è un
`gr.Radio` piatto: 9 input radio, **zero** elementi annidati (`ul ul`,
`[role=tree]`). La gerarchia topic→blocco è resa con indentazione testuale
nelle etichette. Non è comprimibile, non è navigabile da tastiera come un
albero, non regge un manuale reale da centinaia di oggetti.

**P7 — La struttura scorre via.** Nessun pannello ha scorrimento indipendente
(`independentlyScrollable: 0`): tutta la pagina è un unico flusso. Scorrendo
fino a Salva, l'indice del manuale è già uscito dallo schermo.

**P8 — Grounded Authoring è irraggiungibile.** È annidata **due accordion in
profondità** a riga 5749, sotto un secondo accordion omonimo «Dettagli
tecnici». La capability differenziante del prodotto è di fatto invisibile.

**P9 — Difetto funzionale trovato dal vivo.** `_page_head_html` è definita
**due volte** in `app.py` (righe 758 e 768). La seconda oscura la prima e
inserisce il sottotitolo **verbatim** invece di tradurlo. Verificato in
esecuzione:

```
app._page_head_html('kicker.workflow','page.content.title','page.content.subtitle')
  -> "…<p>page.content.subtitle</p>…"
```

Sei pagine di sezione stampano la chiave di localizzazione al posto del testo.
**Non corretto in questo gate** (vietato modificare codice di prodotto); da
includere nello scope implementativo (§10).

---

## 3. Il flusso di lavoro reale

Ricostruito dal grafo dei callback — chi produce lo stato che serve a chi —
**non** dall'ordine dei widget.

```
  APERTURA PROGETTO
        |
        v
  authoring_tab.select -> studio_load_ui         (l'outline si carica da solo)
        |
        v
  +-----------------------------------------------+
  |  CICLO QUOTIDIANO  (decine di volte al giorno) |
  |                                                |
  |   trova l'oggetto                              |
  |     st_search_btn -> studio_search_ui          |
  |     oppure st_outline_radio.change             |
  |        -> studio_open_ui                       |
  |          |                                     |
  |          v                                     |
  |   modifica  (blur -> studio_save_draft_ui)     |
  |          |                                     |
  |          v                                     |
  |   st_save_btn -> studio_save_ui                |
  |          |                                     |
  |          +--> oggetto successivo --------------+
  +-----------------------------------------------+
        |
        v
  OCCASIONALE: st_submit_review_btn -> revisione umana
               st_archive_btn / st_restore_btn
               st_revert_btn / st_compare_btn
        |
        v
  UNA VOLTA PER RELEASE: pv_refresh -> anteprima
                         pv_export_pdf / pv_export_html
```

Eventi che **non** appartengono al ciclo quotidiano ma occupano la parte alta
della pagina:

- `c_import` → `import_manual_ui` e `c_approve` → una volta per manuale;
- `c_actor` (identità operatore) → una volta per sessione, oggi chiesta due
  volte;
- dimensioni / valori / profili varianti → una volta in fase di setup.

**Conclusione.** Il ciclo reale è `trova → apri → modifica → salva →
successivo`. Oggi questo ciclo richiede di attraversare verticalmente una
pagina di 2695 px progettata attorno a eventi che accadono una volta per
progetto.

---

## 4. Architettura informativa di destinazione

Principi ripresi da Conformità — **contesto persistente, un'azione primaria,
dettaglio su richiesta** — applicati alla natura di Contenuti, che è diversa:
Conformità è una **superficie di lettura e decisione**, Contenuti è una
**superficie di scrittura prolungata**. Copiare il layout di Conformità
sarebbe un errore: lì il contenuto è una tabella da scorrere, qui è un testo
da comporre.

Differenza portante: in Conformità il contesto può stare in alto perché lo si
consulta; in Contenuti il contesto deve stare **a lato**, perché lo si usa per
navigare mentre si scrive.

Tre zone stabili:

1. **Contesto (sinistra, persistente).** Pubblicazione, profilo, operatore —
   dichiarati **una sola volta**, condivisi da valutazione e anteprima. Sotto,
   l'albero del manuale con stato per oggetto (bozza / in revisione /
   approvato / archiviato).
2. **Lavoro (centro, dominante).** Il solo oggetto aperto: titolo, superficie
   di scrittura, barra strumenti, azione primaria «Salva». Nient'altro.
3. **Dettaglio (destra, contestuale e richiudibile).** Ciò che riguarda
   *l'oggetto aperto*: varianti applicabili, cronologia/confronto,
   riuso/impatto condiviso, Grounded Authoring.

Ciò che **esce** dalla schermata quotidiana:

| Oggi in Contenuti | Destinazione |
|---|---|
| Identità operatore | contesto globale dell'app (una volta per sessione) |
| Import manuale + Approva sorgente | flusso dedicato «Importa manuale» |
| Dimensioni / valori / profili varianti | Impostazioni → Varianti |
| Anteprima pubblicazione + Export PDF/HTML | Rilascio / Pubblicazione |
| Coda di revisione | Revisione |
| Secondo selettore «Profilo» | eliminato (unificato nel contesto) |

---

## 5. Layout desktop proposto (1920×1080)

```
+--------------------------------------------------------------------------+
| ERYLOS   Contenuti · <Pubblicazione>          [Profilo v]  [Operatore v]  |
+------------------+--------------------------------------+----------------+
| CONTESTO  260px  |   LAVORO            ~1000px           | DETTAGLIO 380px|
|                  |                                       |  (richiudibile)|
| [cerca manuale ] | Titolo oggetto            [stato]     | > Varianti     |
|                  | +-----------------------------------+ |   applicabili  |
| v 1 Uso previsto | | B I <> * 1.  link                 | | > Cronologia   |
|   - Paragrafo    | +-----------------------------------+ |   e confronto  |
|   - Tabella      | |                                   | | > Riuso e      |
| v 2 Installaz.   | |   superficie di scrittura         | |   impatto      |
|   - Paragrafo  <-| |   ~600px di altezza               | | > Grounded     |
|   - Figura       | |                                   | |   Authoring    |
| > 3 Sicurezza    | |                                   | |                |
|                  | +-----------------------------------+ |                |
| (scroll proprio) | [Salva contenuto]  bozza salvata     | (scroll proprio)|
+------------------+--------------------------------------+----------------+
```

Vincoli di progetto verificabili:

- L'editor comincia **sopra** la piega (`editorTop < 400`).
- «Salva contenuto» è visibile **senza scroll** quando l'editor è aperto
  (`editorToSaveGap` ≈ altezza dell'editor, non 944 px).
- L'albero resta visibile durante la modifica (pannello sticky, scorrimento
  indipendente).
- Un solo selettore «Profilo» e un solo «Operatore» nell'intera pagina.
- Nessun ID tecnico grezzo esposto (mantenere lo 0 attuale).

---

## 6. Integrazione di Grounded Authoring

Oggi: annidata in due accordion (riga 5749), invisibile.

Principio: ERYLOS **non** deve diventare un'applicazione di chat con l'AI.
Grounded Authoring non è un assistente conversazionale: è un **fornitore di
proposte tracciabili** ancorate a fonti (`g_chunks`, `g_evidence`, `g_claims`)
che entrano nel normale flusso di revisione umana (`g_queue` → «Invia in
review umana»).

Integrazione proposta — nel pannello Dettaglio, **accanto** all'oggetto
aperto, non al posto dell'editor:

- Il pannello mostra, per l'oggetto aperto, le **fonti ancorate** e le
  **affermazioni** estratte, con la loro evidenza.
- Una proposta non entra mai direttamente nel testo. Produce un **suggerimento
  rivedibile** che l'autore accetta, modifica o rifiuta; l'accettazione passa
  dal ciclo normale bozza → salva → revisione.
- Nessun campo di prompt libero nella schermata quotidiana. L'interazione è
  ancorata all'oggetto e alla fonte, non a una conversazione.
- Contenuto safety-critical: mai auto-approvato — vincolo già stabilito dalla
  Product Doctrine, qui semplicemente non violato.

**Non implementare in questo gate.** Qui si fissa solo la collocazione e il
contratto di interazione.

---

## 7. Implicazioni di localizzazione

Fondazione già disponibile: `i18n.py` (ContextVar, `it` default, `en`
supportato, chiavi semantiche stabili), `LocalizedUI`/`LOC`,
`switch_language_ui`.

Conseguenze per questo redesign:

1. **Nuove chiavi per le tre zone.** Namespace coerenti con quelli esistenti:
   `content.context.*`, `content.work.*`, `content.detail.*`,
   `content.tree.*`. Nessun testo italiano usato come chiave.
2. **Il difetto P9 è bloccante per la localizzazione.** Finché
   `_page_head_html` duplicata inserisce il sottotitolo verbatim, le
   intestazioni di sezione **non sono localizzate affatto**: stampano la
   chiave. Va corretto prima di aggiungere chiavi nuove, altrimenti si
   costruisce sopra un canale rotto.
3. **Etichette duplicate = chiavi duplicate.** Unificando «Profilo», «Il tuo
   nome», «Cerca nel manuale» si rimuove anche il rischio che due traduzioni
   divergano per lo stesso concetto.
4. **Stati dell'albero localizzati per chiave**, non per stringa: bozza / in
   revisione / approvato / archiviato sono stati di dominio, il widget porta
   il codice, l'etichetta è solo presentazione (stessa disciplina già
   applicata al Radio delle varianti).
5. **Terminologia.** Rispettare `ERYLOS_TERMINOLOGY_POLICY_V1.md` e la
   decisione già presa in Actor Normalization: «operatore», non «actor». Non
   congelare termini inglesi solo perché familiari agli sviluppatori.

---

## 8. Migrazioni DomainError richieste

Inventario reale di `authoring_service.py`: **92** raise non ancora migrati
(non ~25; il conteggio precedente escludeva i `KeyError` nudi).

```
KeyError              44
ValueError            31
VersionConflictError  14
SharedObjectError      2
MissingAssetError      1
```

Classificazione rispetto al flusso proposto (ogni riga assegnata una sola
volta; somma verificata = 92):

### A — Necessari per il Gate 2 — 60 raise

Raggiungibili dal percorso quotidiano `trova → apri → modifica → salva`.

- **36 `KeyError(object_id)`** su apri / salva / archivia / ripristina /
  versioni (262, 274, 289, 304, 364, 436, 1040, 1114, 1170, 1223, 1249, 1298,
  1330, 1367, 1377, 1421, 1431, 1554, 1609, 1696, 1709, 1952, 2008, 2065,
  2074, 2132, 2166, 2176, 2249, 2314, 2333, 2398, 2407, 2471, 2496, 2506).
  Non contengono prosa italiana, ma espongono l'ID grezzo:
  → `ERR_AUTHORING_OBJECT_NOT_FOUND`.
- **14 `VersionConflictError`** (396, 514, 1126, 1188, 1391, 1453, 1975, 2086,
  2169, 2209, 2270, 2427, 2499, 2538). Già strutturato (`object_id`,
  `expected_version`, `detail`) e già ricomposto in UI da
  `av.conflict_message()` (app.py 2785, 2939): è il caso **più vicino al
  bersaglio**, serve solo spostare la composizione italiana in catalogo.
- **7 `ValueError`** con prosa italiana (443, 1226, 1252, 1301, 1333, 1495,
  1945).
- **2 `SharedObjectError`** (1899, 1908) — salvataggio di tabella/figura
  condivisa e drift dell'impatto.
- **1 `MissingAssetError`** (2413) — ripristino figura senza immagine.

Di questi 60, **14 compongono prosa italiana dentro il layer di dominio** e
sono quindi i soli realmente **bloccanti** per una UI bilingue: 443, 1126,
1188, 1226, 1252, 1301, 1333, 1495, 1899, 1908, 1945, 2169, 2413, 2499.

### B — Non raggiungibili dal percorso quotidiano — 24 raise

CRUD di dimensioni, valori e profili varianti (653, 659, 674, 676, 678, 700,
704, 727, 759, 761, 763, 765, 786, 790, 807, 883, 887, 899, 901, 903, 918,
921, 936, 948). Nel design proposto questa superficie **esce** da Contenuti e
va in Impostazioni → Varianti. Vanno migrati **quando si riprogetta quella
schermata**, non ora.

### C — Invarianti di programmazione — 8 raise

Non raggiungibili da un utente dell'interfaccia: `kind` non valido (1038),
payload di riordino incoerenti costruiti dalla UI stessa (1104, 1120, 1164,
1182), contratto API `expected_impact_token` (1902), riferimento
blocco→tabella/figura incoerente (2179, 2509). **Restano eccezioni Python.**
Trasformarli in errori di dominio localizzati aggiungerebbe solo rumore.

> Vincolo di compatibilità, già stabilito nei gate precedenti: gli errori
> migrati devono restare istanze delle classi che i callback catturano oggi
> per nome, oltre che `DomainError`.

---

## 9. Fattibilità con Gradio

Verificata **in esecuzione**, non per ragionamento.

| Requisito | Esito | Evidenza misurata |
|---|---|---|
| Pannello sinistro persistente | **Fattibile** | `position:sticky` regge dopo aver portato `.gradio-container` da `overflow:hidden` a `visible`: a scroll 900 il pannello resta a 160 px dall'alto |
| Editor a tutta altezza | **Fattibile** | `min-height` forzato a 600 px → altezza reale 600 px |
| Tre colonne affiancate | **Fattibile** | `gr.Row`/`gr.Column` con `scale`; nessuno scroll orizzontale oggi (`hScroll 0`) |
| Scroll indipendente per pannello | **Fattibile con CSS** | oggi `independentlyScrollable: 0`; richiede `overflow-y:auto` + altezza vincolata sulle colonne |
| Pannelli contestuali richiudibili | **Nativo** | `gr.Accordion` già in uso |
| Stato editor persistente | **Già risolto** | autosalvataggio bozza su `.blur()`/`.input()` + `Ripristina bozza` |
| Albero gerarchico | **Non nativo** | `gr.Radio` piatto, 9 input, 0 elementi annidati |
| Navigazione da tastiera nell'albero | **Non nativo** | dipende dalla scelta sull'albero |
| Pannelli ridimensionabili dall'utente | **Non nativo** | richiederebbe JS custom |

**Opzione A — Riorganizzazione con i componenti standard.**
Tre colonne, accordion per il dettaglio, CSS per sticky e scroll indipendente,
`gr.Radio` con etichette indentate per la struttura. Costo basso, rischio
basso. Risolve P1, P2, P3, P4, P5, P7, P8. **Non** risolve P6 (nessun vero
albero, nessuna comprimibilità).

**Opzione B — A + albero in HTML custom.**
L'albero diventa `gr.HTML` con markup proprio, che riporta la selezione a
Python attraverso lo stesso meccanismo **già in produzione** per l'editor
rich-text: `js_on_load` che scrive nel `props.value` tracciato, più hook in
fase di cattura prima dell'handler. Non è un pattern nuovo da inventare: è
quello indurito in 4E.3, con le sue note di cautela già scritte nel sorgente.
Risolve anche P6 e abilita la navigazione da tastiera. Costo medio, rischio
medio, **nessuna dipendenza nuova**.

**Opzione C — Frontend custom.**
**Non raccomandata.** Nessuna interazione richiesta da questo design risulta
impossibile in Gradio: le due sole lacune native (albero, ridimensionamento)
hanno entrambe una soluzione già dimostrata nel repository o sono
rinunciabili. Una riscrittura introdurrebbe un runtime Node, un processo di
build e la perdita dell'allineamento con le suite esistenti, in cambio di
nulla che sia bloccante.

**Raccomandazione: A adesso, B subito dopo**, così la riorganizzazione
strutturale viene validata prima di introdurre markup custom.

---

## 10. Scope preciso per l'implementazione del Gate 2

Prima fetta, deliberatamente stretta — **nessuna nuova capability, solo
riorganizzazione e correzioni bloccanti**:

1. **Correggere `_page_head_html` duplicata** (app.py 758/768). Difetto reale,
   blocca la localizzazione delle intestazioni. Test di regressione che
   fallisca sulla versione attuale.
2. **Estrarre da Contenuti** ciò che non appartiene al ciclo quotidiano:
   identità operatore → contesto globale; import / approva sorgente → flusso
   dedicato; configurazione varianti → Impostazioni; anteprima ed export →
   Rilascio; coda di revisione → Revisione.
3. **Unificare il contesto duplicato**: un solo «Profilo», un solo
   «Operatore», una sola «Cerca nel manuale».
4. **Ricomporre in tre zone** (contesto / lavoro / dettaglio) con l'Opzione A:
   sticky, scroll indipendenti, editor sopra la piega, Salva adiacente
   all'editor.
5. **Portare Grounded Authoring fuori dai due accordion**, nel pannello
   Dettaglio, senza implementarne il comportamento nuovo.
6. **Migrare i 14 raise bloccanti** del gruppo A che compongono prosa italiana
   nel dominio; lasciare B e C dove sono.
7. **Chiavi di localizzazione** per le tre zone, `it` default, `en` allineato.

Criteri di accettazione misurabili (stessa metodologia di questo documento,
viewport 1920×1080, blocco aperto):

| Criterio | Oggi | Target |
|---|---:|---:|
| `editorTop` | 1276 | < 400 |
| `editorHeight` | 120 | ≥ 500 |
| `editorToSaveGap` | 944 | < 700 |
| `controlsBeforeEditor` | 19 | ≤ 6 |
| `pageHeight` | 2695 | ≤ 1200 |
| selettori «Profilo» | 2 | 1 |
| ID tecnici grezzi esposti | 0 | 0 |
| albero visibile durante la modifica | no | sì |
| `hScroll` | 0 | 0 |

Fuori scope per questa fetta: comportamento nuovo di Grounded Authoring,
albero HTML custom (Opzione B), pannelli ridimensionabili, migrazione dei
gruppi B e C, riprogettazione di Impostazioni e Rilascio.
