# ManualSync

**Stato: pre-development / bootstrap repository.**

Questo repository non contiene ancora un prodotto funzionante. Contiene la
baseline di progetto (struttura, documentazione, configurazione minima)
sulla quale lo sviluppo verrà avviato in modo controllato seguendo il
Prompt Pack in `docs/product/`.

Nessuna capability descritta nella documentazione di prodotto deve essere
considerata implementata, disponibile o commercialmente pronta solo perché
il repository esiste o perché del codice compila. Lo stato reale di ogni
capability è tracciato nei report in `docs/reports/`.

## Baseline tecnica

- Python: `>=3.12,<3.13` (baseline verificata: 3.12.10)
- Ambiente virtuale locale in `.venv/` (non versionato)
- Nessun framework applicativo, database, runtime Node.js/Docker/.NET o
  dipendenza di prodotto è stato scelto in questa fase

## Documentazione di prodotto

I documenti sorgente di ManualSync (Product Doctrine, Prompt Pack, Guida
Prodotto, hash di handoff) si trovano in `docs/product/` con i nomi file
originali, non modificati. Sono la fonte dei requisiti; lo sviluppo deve
seguire l'ordine definito nel Prompt Pack.

## Struttura

```text
docs/
  product/        documentazione di prodotto originale (fonte dei requisiti)
  architecture/    decisioni architetturali (da popolare durante lo sviluppo)
  reports/         report di baseline, bootstrap e avanzamento
src/manualsync/    codice sorgente applicativo (vuoto in questa fase)
tests/             unit / integration / e2e / fixtures (vuoti in questa fase)
scripts/           script di supporto allo sviluppo
tools/             strumenti ausiliari
```

## Attivare l'ambiente virtuale (Windows)

```powershell
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

Nessuna dipendenza applicativa è dichiarata in questa fase: `pyproject.toml`
è intenzionalmente minimale e non implica alcuna scelta architetturale
definitiva.

## Regole di governance

Le regole vincolanti per lo sviluppo assistito da Claude Code in questo
repository sono definite in `CLAUDE.md` e derivano da
`docs/product/ManualSync_Product_Doctrine_V4.md` e
`docs/product/ManualSync_Prompt_Pack_Claude_V4.md`.
