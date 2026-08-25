# COMPETITIVE_AND_ADOPTION_GAP_V3.md — ManualSync Prompt 1 (Audit competitivo e obiezioni)

**Data:** 2026-08-25 (revisione dopo correzioni obbligatorie richieste dall'utente)
**Baseline Git di riferimento:** `b3317d79002ad2ef96c82cc329a67383c54c0492` (Prompt 0 congelato)
**Metodo:** ricerca evidence-based su fonti ufficiali e recensioni utente pubbliche (G2,
Capterra, TrustRadius, SourceForge, forum di settore), incrociata con lo stato reale
verificato di ManualSync in `docs/reports/BASELINE_V3.md` e
`docs/reports/MANUALSYNC_PREVIOUS_WORK_HANDOFF.md`. Non è stato scritto o modificato
codice. Dove una fonte pubblica non era disponibile o verificabile, è dichiarato
esplicitamente **"non verificato pubblicamente"** invece di essere ipotizzato.

**Regola di lettura di questo documento:** ogni riferimento a "ManualSync" distingue
sempre tra:
- **AVAILABLE OGGI** → verificato in `BASELINE_V3.md` con codice reale e test eseguiti realmente su Windows;
- **PARTIAL** → esiste codice/backend ma non un percorso prodotto completo o non è stato validato end-to-end;
- **ROADMAP** → obiettivo di Product Doctrine V4, non ancora implementato o non ancora dimostrato.

Nessun benchmark interno di ManualSync è trasformato in claim commerciale in questo
documento (regola di prodotto: "non dichiarare superiorità sui concorrenti senza
benchmark riproducibile").

---

## 1. Concorrenti analizzati, tipo di riferimento e fonti

| Prodotto | Tipo di riferimento | Copertura ricerca |
|---|---|---|
| Paligo | **Diretto** — CCMS cloud completo, topic-based XML | Piena (sito, docs, G2, TrustRadius, pricing) |
| RWS Tridion Docs | **Diretto** — CCMS enterprise DITA/XML | Piena (docs, community, Capterra, G2) — copertura recensioni più debole |
| SCHEMA ST4 (Quanos) | **Diretto e adiacente-industriale** — CCMS enterprise, orientamento esplicito a manualistica meccanica/industriale | Piena su sito ufficiale; **zero recensioni utente pubbliche trovate** (G2/Capterra/SourceForge) |
| Heretto (ex easyDITA) | **Diretto** — CCMS DITA-first | Piena (sito, Capterra con recensioni dirette, G2) |
| MadCap Flare | **Specialista di authoring/publishing** — spesso abbinato a IXIA CCMS per formare un'offerta CCMS completa | Piena (sito, G2, Capterra, forum, pricing 2026) |
| MadCap IXIA CCMS | **Diretto** — CCMS DITA enterprise (stesso vendor di Flare) | Piena (sito, G2) |
| EKR Orchestra | **Adiacente-industriale, serio riferimento** — PIM+CCMS+DAM+DMS integrato, perimetro più ampio di ManualSync ma stesso mondo industriale/automotive | Piena (sito, Capterra — incluse risposte dirette del vendor) |
| FrameEditor NEXT | **Specialista di pubblicazione/layout** per manualistica macchine — stesso segmento normativo di ManualSync (Direttiva Macchine), ma perimetro di prodotto diverso e più stretto | Parziale — buona su compliance/publishing, **nessuna evidenza pubblica** su import legacy e translation intelligence |
| AQRATE | **Specialista di capability singola** — non concorrente CCMS, ma benchmark rilevante per Translation Intelligence/QA linguistica/ottimizzazione fornitori | Piena, ma perimetro non comparabile 1:1 (vedi §2.9) |

**Nota sulla classificazione "diretto/adiacente/specialista":** nessuno dei prodotti
adiacenti o specialisti va liquidato come irrilevante. SCHEMA ST4 ed EKR Orchestra in
particolare restano **riferimenti seri per la documentazione tecnica industriale**,
anche dove il loro perimetro di prodotto (PIM/DAM/ERP integration nel caso EKR;
XML proprietario enterprise nel caso ST4) è più ampio o diverso da quello attuale di
ManualSync. Elenco completo delle fonti citate per prodotto in Appendice A.

---

## 2. Cosa fanno già bene i concorrenti (da non sottovalutare)

Per ciascun punto è indicata la natura della fonte: **[Documentazione ufficiale]**,
**[Recensione utente verificata]** (G2/Capterra/TrustRadius/SourceForge) o
**[Forum/community]** (discussion board, non una recensione strutturata).

### 2.1 Paligo
- **[Documentazione ufficiale]** Import wizard nativo per DITA, MadCap Flare, RoboHelp, Word — riuso granulare fino a livello di frase, version compare/rollback, branching maturo.
- **[Documentazione ufficiale]** Integrazione nativa con Phrase (TMS) restando neutrale sulla traduzione (non obbliga ad agenzia propria).
- **[Documentazione ufficiale]** Ampio catalogo di integrazioni già pronte (Zendesk, Salesforce, ServiceNow, GitHub, Slack, MS Teams, Algolia...).
- **[Recensione utente verificata]** Nonostante la curva di apprendimento (§3.1), un utente riporta che dopo un mese e mezzo l'investimento è risultato ripagante con riuso efficace su 3 linee di prodotto.

### 2.2 RWS Tridion Docs
- **[Documentazione ufficiale]** Repository DITA/XML enterprise maturo, workflow di approvazione configurabile, AI generativa/semantica integrata per l'authoring.
- **[Documentazione ufficiale, dato di parte]** Ecosistema di traduzione storicamente integrato (SDL TMS/WorldServer); setup dei connettori dichiarato "tipicamente meno di 30 minuti" — dato del vendor stesso, non confermato da recensioni indipendenti.

### 2.3 SCHEMA ST4 — riferimento industriale serio
- **[Documentazione ufficiale]** Gestione varianti drag&drop molto matura, generazione automatica varianti da metadata — un'area dove ManualSync non ha ancora un equivalente dimostrato.
- **[Documentazione ufficiale]** ST4 AI Translator integra DeepL nativamente per pre-traduzione.
- **[Documentazione ufficiale]** Layout designer WYSIWYG integrato per PDF/Word/HTML senza scrivere codice.
- Posizionamento esplicito su "mechanical engineering companies" — coerente con l'osservazione dell'utente che ST4 va trattato come riferimento industriale serio, non solo come CCMS generalista.

### 2.4 Heretto
- **[Documentazione ufficiale]** Migrazione da repository DITA esistenti.
- **[Recensione utente verificata]** Un utente riporta migrazione "near-zero friction" (G2). Altri utenti (Capterra) confermano accessibilità per piccole implementazioni: *"affordable for small implementations"*, *"easy to setup, easy to train folks to use"*.
- **[Documentazione ufficiale]** Branching/merging nativi nel CCMS (funzione avanzata rara a questo livello di semplicità dichiarata).

### 2.5 MadCap Flare
- **[Documentazione ufficiale]** Import wizard maturo per Word, Excel, Confluence, FrameMaker, RoboHelp, DITA.
- **[Documentazione ufficiale]** Pubblicazione multi-canale molto forte: HTML5, PDF, Word, PowerPoint, SCORM, push diretto a Salesforce/Zendesk/ServiceNow.
- **[Documentazione ufficiale]** MadCap Lingo esporta XLIFF standard compatibile con Trados/memoQ/Memsource — vendor-neutral, non obbliga a MadTranslations.
- **[Recensione utente verificata / analisi di terze parti]** Utenti (G2/Capterra) e l'analisi di settore Topicary confermano produttività di base raggiungibile in 1-2 giorni.

### 2.6 MadCap IXIA CCMS
- **[Documentazione ufficiale]** Update centralizzato di un componente propagato automaticamente a tutti i documenti che lo riusano — reuse a livello enterprise maturo.
- **[Documentazione ufficiale]** Version control e branching robusti, modulo dedicato di release management.
- **[Recensione utente verificata, dato di parte riportato in recensione]** Un cliente riporta -30% di tempo di creazione manuali su prodotti popolari — claim di un singolo cliente in un testimonial, non un audit indipendente.

### 2.7 EKR Orchestra — riferimento industriale serio
- **[Documentazione ufficiale]** Piattaforma integrata PIM+CCMS+DAM+DMS con integrazioni SAP ERP/CRM/PLM reali — copre un perimetro più ampio di ManualSync (dati prodotto strutturati, non solo documentazione).
- **[Documentazione ufficiale]** Gestione granulare del contenuto XML matura per il settore industriale/automotive/HVAC&R/macchinari.
- Per il perimetro industriale (dati prodotto + documentazione + DAM in un'unica piattaforma), EKR Orchestra è un riferimento più completo di ManualSync su scala enterprise, anche se meno agile su singole capability di translation intelligence.

### 2.8 FrameEditor NEXT — specialista di pubblicazione, stesso segmento normativo
- **[Documentazione ufficiale]** Esplicitamente allineato alla Direttiva Macchine 2006/42/CE e al nuovo Regolamento 2023/1230 — stesso terreno normativo di ManualSync, mercato italiano.
- **[Documentazione ufficiale]** Output nativo verso InDesign/FrameMaker/HTML5/PDF fino a 50 lingue (vedi analisi rigorosa in §6 sulla natura esatta di questo output).
- **[Documentazione ufficiale]** Clienti industriali reali e verificabili (Ideal Standard, Siemens, Artemide).
- **Nota di riposizionamento:** FrameEditor NEXT non è "l'unico concorrente nella stessa identica nicchia" di ManualSync — è uno specialista di pubblicazione/layout per un segmento normativo condiviso, con un perimetro di prodotto più stretto (nessuna evidenza pubblica di translation intelligence/reuse/TM paragonabile). SCHEMA ST4 ed EKR Orchestra (§2.3, §2.7) sono riferimenti industriali altrettanto o più seri su un perimetro di prodotto più ampio.

### 2.9 AQRATE — benchmark rilevante, non concorrente CCMS
AQRATE non è un concorrente CCMS/authoring, ma è un **benchmark importante su tre
dimensioni specifiche** che si sovrappongono a parti di ManualSync:
- **[Documentazione ufficiale]** Ottimizzazione del lavoro linguistico: text analysis per ridurre il volume di parole da tradurre realmente, centralizzazione di TM aziendali frammentate.
- **[Documentazione ufficiale]** QA linguistica AI-assistita con revisione umana (QAi/LanguageCheck.ai) — confrontabile concettualmente con l'ambizione di QA/explainability di ManualSync, anche se con meccanismo diverso.
- **[Documentazione ufficiale]** Modello di integrazione con fornitori/agenzie di traduzione esistenti (non li sostituisce, li ottimizza) — un modello di neutralità verso i fornitori concettualmente vicino alla regola di prodotto di ManualSync ("non obbligare il vendor a operare come agenzia di traduzione").
- Non copre invece: reuse exact/context/fuzzy, technical-signature matching, XLIFF/workflow proprio, CCMS/repository — su questi punti il confronto diretto non è applicabile.
- **Nessuna recensione utente pubblica trovata** su G2/Capterra/TrustRadius per AQRATE.

---

## 3. Debolezze reali dei concorrenti (con fonte esplicitamente tipizzata)

### 3.1 Curva di apprendimento — il pattern più forte trovato, con fonti separate per tipo

| Prodotto | Evidenza | Tipo di fonte |
|---|---|---|
| Paligo | Titolo di recensione: "Steep Learning Curve, Worth The Effort" | **[Recensione utente verificata]** — TrustRadius |
| Paligo | "the learning curve can be steep for those unfamiliar with structured authoring" | **[Recensione utente verificata]** — G2 |
| RWS Tridion Docs | "complexity and learning curve" indicate come aree di miglioramento; "quite intuitive **if** you have previous experience of CCMS's" | **[Recensione utente verificata]** — Capterra/G2 |
| MadCap Flare | Produttività base in 1-2 giorni, ma padronanza reale del single-sourcing "circa un mese" | **[Recensione utente verificata + analisi di terze parti]** — G2/Capterra + Topicary (blog di settore) |
| MadCap Flare | Discussione "How long is the learning curve for MadCap Flare" | **[Forum/community]** — thread di discussione G2, non una recensione strutturata |
| MadCap IXIA CCMS | UI descritta come "overwhelming", "could be more friendlier" | **[Recensione utente verificata]** — G2, 3.8/5 |
| EKR Orchestra | *"We know some part of EKR Orchestra are not easy... we are now trying to make them much easier"* | **[Dichiarazione ufficiale del vendor]** — risposta pubblica del vendor a una recensione su Capterra (non è una recensione utente, è un commento diretto del produttore) |
| EKR Orchestra | Ease of Use 3.3/5, overall 4.2/5 | **[Recensione utente verificata]** — Capterra, valore aggregato |

**Lettura corretta:** la curva di apprendimento ripida è confermata da recensioni utente
indipendenti su Paligo, Tridion Docs, Flare e IXIA CCMS; per EKR Orchestra l'evidenza più
forte è una dichiarazione diretta e pubblica del vendor stesso (non un'illazione da
recensioni). Sono due tipi di prova diversi e vanno citati come tali — entrambi validi,
ma non intercambiabili.

### 3.2 Altre debolezze reali

| Debolezza | Evidenza | Tipo di fonte | Concorrenti coinvolti |
|---|---|---|---|
| Nessun bridge InDesign/IDML nativo (import/export bidirezionale) | Vedi analisi rigorosa dedicata in §6 | Documentazione ufficiale + forum | Paligo, Tridion Docs, Flare (confermato) |
| Migrazione da legacy non strutturato (Word/PDF) è un processo largamente manuale | "Standard content migration solutions do not exist... customers are largely relegated to copying content by hand"; conversione automatica limitata, "reuse is never 100%" | Analisi di settore (entitechsolutions.com, oxygenxml.com) | Pattern di categoria, non specifico di un vendor |
| Pricing enterprise non trasparente / solo su richiesta | Nessun prezzo pubblico | Documentazione ufficiale (assenza di pagina prezzi) | Tridion Docs, IXIA CCMS, ST4, FrameEditor NEXT |
| Copertura recensioni indipendenti debole o assente | ST4: zero recensioni su G2/Capterra/SourceForge; Tridion Docs: G2 segnala copertura insufficiente | Assenza verificata di recensioni | ST4, Tridion Docs |
| Implementazione CCMS enterprise lenta e costosa in generale | Timeline di settore: ~6 mesi implementazione completa, ROI/payback tipico ~9-10 mesi | Analisi di settore (Author-it — **fonte di parte**, un vendor CCMS che descrive sé stesso/la categoria) | Categoria CCMS enterprise nel complesso |

---

## 4. Dove ManualSync deve almeno EGUAGLIARE (parity requirements)

1. **Import Word con mappatura stili→struttura.** Paligo e Flare hanno wizard dedicati
   (con richiesta di pulizia stili). ManualSync: `import_docx_outline()` esiste
   (**PARTIAL**) — non ancora un percorso UX completo dimostrato.
2. **Vendor-neutralità sulla traduzione** (XLIFF standard + TMS/CAT esterni a scelta del
   cliente). ManualSync: **già a parità** — verificato in `BASELINE_V3.md`, coerente
   anche con l'approccio "non sostitutivo" osservato in AQRATE (§2.9).
3. **Version control/revisioni con audit trail.** ManualSync ha un modello diverso
   (revision/change intelligence automatico) ma deve garantire tracciabilità
   equivalente — verificato in parte (workflow state machine auditata, PASS reale), non
   ancora confrontato direttamente con branching/merge come in Heretto/IXIA CCMS.
4. **Pubblicazione multi-formato di base** (almeno DOCX + PDF). ManualSync: DOCX export
   **AVAILABLE** e verificato; **pubblicazione PDF nativa non risulta presente** nella
   baseline attuale (solo import PDF, non publishing PDF) — gap concreto da colmare per
   eguagliare Flare/Paligo/ST4.
5. **Un livello minimo di validazione esterna indipendente** prima di poter competere
   sulla fiducia, come hanno Paligo/Heretto e ST4/Tridion non hanno del tutto.
   ManualSync oggi: zero recensioni, zero clienti pilota conclusi — gap strutturale, non
   risolvibile con codice.

---

## 5. CURRENT PROVEN STRENGTHS vs DIFFERENTIATION BETS di ManualSync

Separazione esplicita richiesta: cosa è **oggi verificato con codice reale e test
eseguiti** (non promesse) rispetto a cosa è ancora **da costruire e da dimostrare**.

### 5.1 CURRENT PROVEN STRENGTHS (AVAILABLE, verificato in `BASELINE_V3.md`)

| Capability | Evidenza reale | Cosa NON afferma |
|---|---|---|
| Import PDF a topic strutturati | `import_pdf_as_topics`, usato realmente nella pipeline di benchmark | Non esiste una metrica pubblica di "tempo/tasso di successo di onboarding PDF" separata dall'allineamento — non va confusa con la riga successiva |
| Allineamento legacy bilingue (vecchio originale ↔ vecchia traduzione) | Benchmark reale misurato: **98,3% coverage aggregata**, **93,3% dei blocchi sorgente con confidence >=80**, su corpus industriali comparabili inclusi nel repository | Questo è un dato di **coverage/confidence dell'allineamento**, non una misura di velocità o tasso di successo dell'import/onboarding PDF, e non è generalizzabile a documenti cliente arbitrari senza ulteriore validazione |
| Technical-safety matching (numeri/codici/unità non bypassabili da un fuzzy score alto) | `cat_tm.signatures_compatible`/`source_target_safe`, verificato nei test reali | Non garantisce accuratezza universale su ogni dominio tecnico, solo sui pattern testati |
| Alignment confidence distinta dal fuzzy score, con policy esplicita | Verificato isolatamente, 5/5 PASS reale | Vale per la policy attuale (>=90 / 80-89 / unsafe), non è un claim di accuratezza assoluta |
| Revision/change intelligence technical-value-aware | `layout_engine.py`, classificazione change reale testata | Non copre ancora layout/publishing universale (vedi §6) |
| QA linguistica cross-language fail-closed | `linguistic_safety.py`, test reali PASS (RC22, R18) | Copre i pattern testati (copia/quasi-copia cross-language); non è una garanzia di qualità linguistica generale |
| Vendor-neutralità traduzione (XLIFF/TMS esterni) | Architettura verificata, nessun obbligo di agenzia propria | Parità di mercato (§4.2), non superiorità |
| Pubblicazione DOCX | `export_publication_docx`, DOCX quality gate R14/R15 | Solo DOCX; PDF publishing nativo non presente (gap, §4.4) |

### 5.2 DIFFERENTIATION BETS ancora da costruire (PARTIAL o ROADMAP, non ancora dimostrati)

| Bet | Stato | Cosa manca per diventare "proven" |
|---|---|---|
| Zero-training UX / time-to-value (<=15 min) | **ROADMAP** in Product Doctrine V4 | Nessun test di usabilità con utenti novizi eseguito |
| Nessuna esposizione XML/DITA all'utente come vantaggio percepito | Architetturalmente vero (SQLite/JSON, non XML/DITA) | Non validato con utenti reali che confermino la percezione |
| Bridge InDesign/IDML completo | **PARTIAL** — template integration, non round-trip universale (§6) | Import/export bidirezionale reale su documenti arbitrari |
| Onboarding legacy Word end-to-end | **PARTIAL** | Percorso UX completo, non solo funzione backend |
| Coesistenza/interoperabilità con CCMS/fornitori esistenti | **ROADMAP** (Prompt 12 del Prompt Pack, non iniziato) | Adapter reali verso sistemi esterni — requisito trasversale, vedi §7 |
| Track record di mercato | Inesistente | Clienti pilota, recensioni indipendenti, casi d'uso pubblici |

---

## 6. Analisi rigorosa: integrazione InDesign/IDML per prodotto

Distinzione esplicita tra quattro capability diverse, spesso confuse nel marketing di
settore:

- **Import/export** = leggere/scrivere un file IDML esistente arbitrario;
- **Publishing verso InDesign** = generare automaticamente un output in formato
  InDesign/IDML da contenuto strutturato (motore di impaginazione);
- **Template integration** = inserire contenuto in slot predefiniti di un template IDML
  esistente, senza ricostruire il layout da zero;
- **Vero round-trip/bridge nativo** = capacità bidirezionale completa (importare un
  IDML arbitrario, modificarlo, ripubblicarlo mantenendo fedeltà di layout).

| Prodotto | Import/export | Publishing verso InDesign | Template integration | Vero round-trip nativo |
|---|---|---|---|---|
| **ManualSync** | `import_idml_story_outline` estrae testo da IDML (import parziale, solo outline testuale) | Non presente | `patch_idml_template` — inserisce contenuto in slot di un template esistente (**PARTIAL**, verificato: `idml_hardening.py`, `audit_idml_roundtrip`) | **Non presente** — nessuna ricostruzione di layout arbitrario |
| Paligo | Solo indiretto, via conversione preliminare a HTML/DITA/Word (non è un vero import IDML) | Non presente | Non risulta | Non presente |
| RWS Tridion Docs | Non trovato in fonti pubbliche | Non trovato | Non trovato | Non verificato pubblicamente |
| MadCap Flare | **Confermato assente** (thread ufficiali forum MadCap e community Adobe) | Non presente | Non presente | Non presente — solo workaround via Word/PDF |
| SCHEMA ST4 | Non verificato pubblicamente | Non verificato pubblicamente | Non verificato pubblicamente | Non verificato pubblicamente |
| MadCap IXIA CCMS | Non verificato pubblicamente | Non verificato pubblicamente | Non verificato pubblicamente | Non verificato pubblicamente |
| EKR Orchestra | Non verificato pubblicamente | Non verificato pubblicamente | Non verificato pubblicamente | Non verificato pubblicamente |
| **FrameEditor NEXT** | Non verificato pubblicamente (nessuna evidenza di import da IDML esistente) | **Sì, dichiarato come punto di forza nativo** — genera output InDesign/FrameMaker da contenuto gestito, fino a 50 lingue | Presumibile (architettura publishing-first), non dettagliato pubblicamente | Non verificato pubblicamente |

**Conclusione corretta:** nessuno dei prodotti analizzati, ManualSync incluso, ha un vero
round-trip IDML bidirezionale dimostrato pubblicamente. La differenza reale è che
**FrameEditor NEXT è forte in publishing-verso-InDesign** (genera output, non importa
arbitrariamente), mentre **ManualSync è oggi limitato a template integration + import
parziale di outline testuale** — due capability diverse, non direttamente comparabili
come "chi è più avanti". Su questo fronte ManualSync non è né alla pari né superiore a
FrameEditor NEXT; ha un profilo diverso (template-driven vs publishing-engine) e più
debole in termini di maturità dimostrata.

---

## 7. Coesistenza con sistemi e fornitori esistenti — requisito trasversale

La coesistenza con CCMS, TMS/CAT e fornitori già in uso dal cliente non è una capability
tra le altre: è un **requisito trasversale** che condiziona la credibilità di ManualSync
su quasi tutte le obiezioni commerciali, in particolare "abbiamo già Paligo/un sistema
simile" e "abbiamo già fornitori di redazione e traduzione".

- Tutti i concorrenti diretti analizzati (Paligo, Tridion Docs, Flare/Lingo) sono
  vendor-neutral sulla traduzione (XLIFF + TMS esterni) — ManualSync è già a parità su
  questo punto specifico (§4.2).
- Nessun concorrente analizzato risolve però esplicitamente "coesistenza con un CCMS
  concorrente già installato" — è un'area dove né i concorrenti né ManualSync hanno
  oggi una soluzione matura dimostrata.
- Per ManualSync, il layer di interoperabilità/coesistenza con CCMS esterni è
  **ROADMAP** (Prompt 12 del Prompt Pack, non iniziato). Finché resta ROADMAP,
  l'obiezione "abbiamo già un sistema simile" non è risolvibile con una risposta
  tecnica, solo con argomentazioni di valore aggiuntivo (§8).

---

## 8. I 5 differenziatori strategici prioritari

Tassonomia fissata secondo la Product Doctrine e il Prompt Pack. Per ciascuno: cosa
ManualSync possiede oggi, cosa manca, come verrà misurato, quale obiezione risolve.

### 8.1 Legacy-first / value-before-migration
- **Cosa possiede oggi:** import PDF a topics (AVAILABLE), deployment locale/desktop
  senza ciclo di vendita enterprise pesante, principio di dottrina "valore prima della
  migrazione completa".
- **Cosa manca:** layer di coesistenza con CCMS/TMS esterni (§7, ROADMAP); onboarding
  multi-formato automatico (Word/PDF/IDML/cartelle) non ancora unificato; nessuna
  misura pubblica di quanto valore reale un cliente ottiene prima di una migrazione
  completa.
- **Come verrà misurato:** tempo e completezza del primo progetto utile costruito su
  documenti legacy reali del cliente, senza migrazione strutturale forzata (metrica
  ancora da raccogliere, non disponibile oggi).
- **Obiezione risolta:** "non vogliamo migrare tutto", parzialmente "siamo piccoli e
  abbiamo pochi manuali".

### 8.2 Zero-training UX e time-to-value
- **Cosa possiede oggi:** UI Gradio (R23.2) senza esposizione diretta di XML/DITA
  all'utente (differenza architetturale reale rispetto a Paligo/Tridion/ST4/
  Heretto/IXIA CCMS, che sono ambienti XML/DITA anche con GUI).
- **Cosa manca:** qualunque dato misurato — Product Doctrine V4 classifica questo come
  obiettivo **ROADMAP**; nessun test di usabilità con utenti novizi è stato eseguito;
  target dichiarati ("<=15 minuti al primo risultato utile", "90% completamento al
  primo tentativo su 5 task core") non ancora verificati.
- **Come verrà misurato:** test di usabilità strutturato con utenti novizi su documenti
  legacy preparati, con cronometraggio e tasso di completamento (previsto da Prompt 2
  del Prompt Pack, non ancora eseguito).
- **Obiezione risolta:** "non abbiamo tempo di imparare un nuovo software" — è
  l'obiezione con il gap più ampio tra ambizione e prova, ma anche l'opportunità di
  mercato più forte: è la debolezza più diffusa e più esplicitamente ammessa dai
  concorrenti (§3.1, 5 prodotti su 8 con evidenza diretta).

### 8.3 Revision & Reuse Intelligence
- **Cosa possiede oggi:** classificazione automatica dei cambiamenti technical-value-
  aware (`layout_engine.py`, test reali PASS); allineamento automatico vecchio
  originale ↔ vecchia traduzione con confidence esplicita, **misurato realmente**:
  98,3% coverage aggregata, 93,3% blocchi con confidence >=80 su corpus industriali
  comparabili (non generalizzabile a documenti cliente arbitrari); alignment confidence
  esplicitamente distinta dal fuzzy translation score, con policy verificata (5/5 PASS
  isolato).
- **Cosa manca:** editor visuale di Alignment Review con remap/merge/split 1:N/N:1
  (PARTIAL secondo l'handoff); validazione su corpus cliente reali oltre ai fixture
  industriali inclusi; nessun confronto diretto pubblicato con le funzioni di reuse di
  Paligo/IXIA CCMS/ST4 (che hanno reuse maturo ma non descrivono pubblicamente una
  logica di allineamento/confidence equivalente).
- **Come verrà misurato:** precisione/recall per fascia di confidence su corpora
  etichettati aggiuntivi, non solo sui 4 fixture industriali già inclusi (richiesto da
  Prompt 7 del Prompt Pack).
- **Obiezione risolta:** "facciamo tutto internamente" (automazione di lavoro ripetitivo
  non ingegneristico), parzialmente "abbiamo già Paligo/un sistema simile" (funzione
  non descritta pubblicamente dai concorrenti diretti).

### 8.4 Technical-safe + explainable Translation Intelligence
- **Cosa possiede oggi:** technical signature matching che impedisce a un fuzzy score
  alto di bypassare un mismatch numerico/codice/unità (verificato nei test reali); 4
  bucket semantici distinti (AUTO_REUSE/LINGUISTIC_REVIEW/TECHNICAL_REVIEW/
  NEW_TRANSLATION); QA linguistica cross-language fail-closed con secondo gate
  indipendente in Produzione (test reali PASS); protected/non-translatable handling ed
  exact repetition reuse (verificati).
- **Cosa manca:** benchmark di precision/recall per fascia (richiesto da Prompt 8 del
  Prompt Pack, non ancora eseguito); UX specialistica completa per il CAT workspace
  (PARTIAL lato prodotto secondo l'handoff); nessun confronto con l'approccio di QA
  linguistica di AQRATE (che copre concettualmente uno spazio simile ma con meccanismo
  diverso, §2.9).
- **Come verrà misurato:** precision/recall per fascia di confidence su benchmark
  etichettato, tasso di falsi AUTO_REUSE, parole protette/riusate/da revisione
  linguistica/tecnica (metriche già definite nella Product Doctrine, non ancora
  raccolte su scala).
- **Obiezione risolta:** "non ci fidiamo dell'AI sul contenuto tecnico", "il nostro
  contenuto è safety-critical", parzialmente "non vogliamo cambiare agenzia di
  traduzione o CAT/TMS" (perché il matching resta interno e non dipende dal fornitore
  scelto dal cliente).

### 8.5 Layout-aware publishing fino al deliverable finale
- **Cosa possiede oggi:** pubblicazione DOCX con quality gate (AVAILABLE, verificato);
  template integration IDML (PARTIAL, §6).
- **Cosa manca:** pubblicazione PDF nativa (non presente, gap di parità §4.4); reflow/
  overset/tabelle native/figure/TOC/cross-reference/preflight universale (PARTIAL
  secondo l'handoff); vero round-trip IDML bidirezionale (assente, §6) — su questo
  fronte specifico FrameEditor NEXT è oggi più maturo di ManualSync in un'area diversa
  (publishing-verso-InDesign vs template integration).
- **Come verrà misurato:** fixture di layout predefiniti con zero overset irrisolto e
  risultati di visual regression stabili (già previsto come acceptance criterion nel
  Prompt Pack, non ancora eseguito su scala).
- **Obiezione risolta:** "lavoriamo con InDesign" (parzialmente, con onestà sul gap
  verso FrameEditor NEXT), "lavoriamo con Word" (parzialmente, via DOCX già disponibile).

---

## 9. Risposta alle 9 obiezioni (tabella riassuntiva)

| Obiezione | Risposta corretta oggi | Stato capability richiesta | Risolvibile ORA? |
|---|---|---|---|
| "Abbiamo già Paligo/un sistema simile" | Differenziatori §8.3-8.4 + coesistenza (§7) | Differenziatori: parzialmente AVAILABLE. Coesistenza: ROADMAP | **NO** — manca sia prova di mercato sia layer di coesistenza |
| "Abbiamo già fornitori di redazione e traduzione" | Vendor-neutralità già a parità col mercato | AVAILABLE | **SÌ**, strutturalmente |
| "Siamo piccoli e abbiamo pochi manuali" | Deployment locale proporzionato (§8.1) | Deployment: AVAILABLE. Zero-training dimostrato: ROADMAP (§8.2) | **PARZIALMENTE** |
| "Facciamo tutto internamente" | Automazione revision/alignment/reuse (§8.3) | AVAILABLE a livello di codice/test, non validato con cliente reale in UI finita | **PARZIALMENTE** |
| "Non abbiamo tempo di imparare un nuovo software" | Opportunità di mercato più forte (§3.1), ma zero-training non ancora dimostrato (§8.2) | ROADMAP | **NO, non ancora** |
| "Lavoriamo con InDesign" | Template integration esiste, gap onesto verso specialisti (§6) | PARTIAL | **PARZIALMENTE**, con onestà sul gap |
| "Lavoriamo con Word" | Import DOCX esiste, percorso UX da completare | PARTIAL | **PARZIALMENTE** |
| "Abbiamo solo PDF" | Import PDF disponibile e usato realmente nei benchmark (senza sovra-interpretare il numero di allineamento come metrica di onboarding, §5.1) | AVAILABLE | **SÌ**, il punto di ingresso più solido oggi |
| "Non vogliamo migrare tutto" | Value-before-migration (§8.1), coerente con le difficoltà di migrazione note nel settore (§3.2) | Deployment locale: AVAILABLE. Onboarding progressivo end-to-end: PARTIAL | **PARZIALMENTE** |

**Obiezioni non ancora risolvibili in modo completo, in ordine di gap:**
1. Qualunque obiezione, di fatto — il Final Windows Gate non è stato eseguito
   (`BASELINE_V3.md` §8): finché il prodotto non è installabile e verificato su Windows
   reale, nessuna obiezione è chiudibile con un prodotto consegnabile.
2. "Non abbiamo tempo di imparare" — zero-training UX è ROADMAP, non misurato.
3. "Abbiamo già un sistema simile" — manca track record e coesistenza tecnica (§7).
4. "Siamo piccoli" — nessun framework di qualificazione/deployment proporzionato
   ancora definito operativamente (previsto da Prompt 17, non eseguito).

---

## 10. Criteri misurabili per semplicità d'uso e tempo di onboarding

| Criterio | Target dichiarato (Product Doctrine) | Termine di paragone di settore | Stato di verifica ManualSync |
|---|---|---|---|
| Tempo al primo risultato utile | <=15 minuti su documenti legacy preparati | Implementazione CCMS enterprise completa: ~6 mesi (fonte di parte, Author-it) | **NON ANCORA TESTATO** |
| Completamento al primo tentativo, 5 task core, utenti novizi | >=90% | Nessun dato quantitativo indipendente di settore trovato | **NON ANCORA TESTATO** |
| Comprensione XML/DITA richiesta per i flussi standard | Zero | Concorrenti diretti sono tutti ambienti XML/DITA sottostanti | Architetturalmente vero per ManualSync (SQLite/JSON) — **non validato con utenti reali** |
| Ogni errore bloccante contiene causa + rimedio | Richiesto da dottrina | Non misurato per i concorrenti | Messaggi fail-closed strutturati osservati nei test (es. QA_BLOCKED) — **UX del messaggio non validata con utenti reali** |
| Coverage/confidence allineamento legacy bilingue | — | — | **Misurato**: 98,3% coverage aggregata, 93,3% blocchi con confidence >=80 su corpus industriali comparabili — **non è una misura di onboarding/tempo di import**, è specificamente relativa alla qualità dell'allineamento (§5.1, §8.3) |

**Conclusione invariata:** ManualSync ha oggi un solo criterio misurato con dati reali
(l'allineamento legacy bilingue, ora correttamente scorporato dall'onboarding PDF in
sé). Tutti gli altri criteri di "zero-training"/"15 minuti"/"90% completamento" restano
obiettivi di dottrina non ancora sottoposti a verifica.

---

## Appendice A — Fonti consultate per prodotto

**Paligo:** paligo.net (CCMS product page, import docs, professional services, pricing),
docs.paligo.net (import from other formats), G2 (Paligo reviews), TrustRadius ("Steep
Learning Curve, Worth The Effort" review), Vendr (pricing).

**RWS Tridion Docs:** rws.com (content-management/tridion/docs, migration guide,
translation resources), docs.rws.com (translating docs, publishing with DITA-OT),
community.rws.com (forum migrazione), xtm.cloud (integrazione Tridion), Capterra, G2,
SoftwareWorld.

**SCHEMA ST4:** quanos.com (product page), G2 (schema-st4/reviews — solo snippet, fetch
diretto bloccato 403), SourceForge (0 recensioni), Capterra (0 recensioni).

**Heretto:** heretto.com (platform/ccms), Capterra (easyDITA — recensioni dirette con
citazioni), G2 (39 recensioni, 4.1★ — solo snippet, fetch diretto bloccato 403),
TrustRadius (pricing indicativo, fetch bloccato 403).

**MadCap Flare / IXIA CCMS:** madcapsoftware.com (Flare, IXIA CCMS, Lingo product pages),
G2 (Flare pros/cons, discussion learning curve, IXIA CCMS reviews 3.8/5), Topicary (review
2026, pricing 2026), forums.madcapsoftware.com e Adobe Community (thread IDML/InDesign).

**EKR Orchestra:** ekr.it (ccms page), Capterra (recensioni incluse risposta diretta del
vendor).

**FrameEditor NEXT:** frameeditor.it (sito ufficiale prodotto).

**AQRATE:** aqrate.biz (sito ufficiale), docs.rws.com (Trados TQA, nessun collegamento
diretto ad AQRATE trovato).

**Benchmark di settore:** author-it.com (timeline implementazione CCMS, ROI, onboarding —
fonte di parte, un vendor CCMS su sé stesso), entitechsolutions.com (difficoltà
migrazione Word/PDF→strutturato), blog.oxygenxml.com (limiti automazione conversione
DITA), componize.com (training DITA — nessun dato quantitativo trovato),
sourceforge.net e microgaps.com (proporzionalità CCMS per PMI).

**Nota metodologica trasversale:** G2 e TrustRadius hanno bloccato il fetch diretto (HTTP
403) in diversi casi; per quelle fonti i dati provengono dagli snippet di ricerca
indicizzati, non dalla lettura diretta della pagina — trattati come attendibili ma con
questo caveat esplicito. Ogni affermazione derivata da recensioni/community è ora
esplicitamente tipizzata (§2, §3.1) per non confondere dichiarazioni ufficiali del
vendor, recensioni utente verificate e discussioni di forum/community.
