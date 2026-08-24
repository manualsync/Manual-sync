# CLAUDE.md — ManualSync

Questo file governa il comportamento di Claude Code in questo repository.
Deriva da `docs/product/ManualSync_Product_Doctrine_V4.md` e
`docs/product/ManualSync_Prompt_Pack_Claude_V4.md`.

## Sources of truth

- La Product Doctrine e la guida prodotto (`docs/product/`) sono le fonti
  dei requisiti.
- Il Prompt Pack (`docs/product/ManualSync_Prompt_Pack_Claude_V4.md`)
  definisce l'ordine di lavoro. Seguirlo sequenzialmente.
- Non modificare i documenti originali di prodotto in `docs/product/`
  senza richiesta esplicita dell'utente.
- Nota: i documenti V4 contengono intestazioni interne, deliverable e
  riferimenti "V3" e a un "Final Candidate"/handoff non presente in questo
  repository. Non correggere silenziosamente queste incongruenze: vedi
  `docs/reports/BOOTSTRAP_REPORT.md`.

## Development discipline

- Nessuna broad implementation prima del completamento della baseline
  (Prompt 0 del Prompt Pack).
- Nessun broad refactoring senza test iniziali.
- Ogni capability deve risolvere un problema concreto e avere acceptance
  criteria misurabili.
- Il fatto che il codice compili non significa che la capability sia
  completa.
- Ogni modifica significativa deve indicare i file modificati e i test
  eseguiti.
- Prima di modifiche multi-modulo, produrre un piano.
- Preservare sempre le funzionalità già verificate.
- Mai nascondere test falliti o warning importanti.

## Dependency discipline

- Non installare pacchetti globalmente.
- Le dipendenze applicative devono essere dichiarate nel repository
  (`pyproject.toml`), mai installate silenziosamente.
- Non introdurre framework o runtime senza necessità architetturale
  documentata.
- Node.js, Docker, database server e altri runtime non vanno aggiunti
  preventivamente.

## Product rules

- ManualSync è una piattaforma completa per il ciclo di vita della
  documentazione tecnica (authoring, reuse, revision intelligence,
  product data, translation intelligence, publishing, governance,
  integrazioni, adozione).
- La traduzione è un pilastro di primo livello, ma ManualSync non deve
  obbligare il vendor a operare come agenzia di traduzione.
- Guided UX come default per utenti normali; complessità tecnica
  (XML/DITA/XLIFF/TM/ID) confinata alla modalità avanzata.
- Legacy-first: un cliente deve ottenere valore prima di una migrazione
  completa. Value-before-migration.
- Non dichiarare superiorità sui concorrenti senza benchmark riproducibile.
- Non confondere alignment confidence con fuzzy translation score.
- Un technical mismatch (numeri, codici, unità, parametri) non può essere
  ignorato da un fuzzy score elevato.
- Contenuto safety-critical e tecnico deve essere fail-closed dove
  appropriato (mai auto-approvare contenuto safety-critical).
- L'enterprise architecture (multi-utente, SSO, multi-tenant, ecc.) viene
  dopo la maturità del core e non deve complicare prematuramente il
  prodotto desktop/core.

## Workflow

- Seguire il Prompt Pack sequenzialmente, prompt per prompt.
- Prompt 0 (freeze e baseline) viene prima di qualsiasi sviluppo esteso.
- Prompt 1 (audit competitivo) non autorizza implementazione di feature.
- Fermarsi quando un prerequisito richiesto dal Prompt Pack manca (es.
  handoff V3/Final Candidate assente) invece di inventarlo o
  approssimarlo.
