# ManualSync — Bootstrap Report

Data: 2026-08-24
Fase: PROJECT BOOTSTRAP / PRE-DEVELOPMENT SETUP (nessuna feature implementata)

## 1. Ambiente rilevato

| Verifica | Risultato |
|---|---|
| Working directory | `C:\Users\baxai\OneDrive\Desktop\Manual-Sync` |
| `python --version` | Python 3.12.10 |
| Interprete Python attivo | `C:\Users\baxai\OneDrive\Desktop\Manual-Sync\.venv\Scripts\python.exe` |
| `python -m pip --version` | pip 25.0.1, da `.venv\Lib\site-packages\pip` (python 3.12) |
| `git --version` | git version 2.55.0.windows.3 |
| `claude --version` | 2.1.241 (Claude Code) |

Nessuna modifica è stata fatta alla configurazione globale di Python, Git o
Windows. Non è stato installato Node.js, Docker, Java, .NET, database
server o altri runtime.

## 2. Python effettivamente utilizzato / stato `.venv`

- L'interprete attivo nella sessione **è già** quello di `.venv`
  (`.venv\Scripts\python.exe`), Python **3.12.10** — coerente con il
  vincolo `>=3.12,<3.13`.
- `pyvenv.cfg` conferma: `home = ...pythoncore-3.12-64`,
  `version = 3.12.10`, nessun `include-system-site-packages`.
- `.venv` preesisteva già nella root prima del bootstrap; non è stato
  ricreato né modificato.

## 3. Versione Git

git version 2.55.0.windows.3. Repository Git **non esisteva** prima di
questa fase (`fatal: not a git repository`). Inizializzato con `git init`.
Identità Git (`user.name` / `user.email`) **non configurata**, né a
livello globale né locale: non è stata impostata automaticamente, come da
istruzioni.

## 4. Struttura creata

```text
docs/
  product/
  architecture/   (.gitkeep)
  reports/         (questo report)
src/
  manualsync/       (__init__.py vuoto)
tests/
  unit/            (.gitkeep)
  integration/     (.gitkeep)
  e2e/             (.gitkeep)
  fixtures/        (.gitkeep)
scripts/           (.gitkeep)
tools/             (.gitkeep)
.gitignore
.gitattributes
pyproject.toml
README.md
CLAUDE.md
```

Nessuna directory applicativa aggiuntiva è stata inventata oltre a quanto
richiesto.

## 5. File spostati

Tutti e quattro i documenti erano presenti nella root e sono stati
spostati (non copiati, non rinominati, contenuto non modificato) in
`docs/product/`:

- `ManualSync_Guida_Prodotto_Completo_V4.docx` (nota: **non è un PDF**,
  vedi sezione 10)
- `ManualSync_Product_Doctrine_V4.md`
- `ManualSync_Prompt_Pack_Claude_V4.md`
- `ManualSync_CLAUDE_HANDOFF_V4.sha256.txt`

## 6. File creati

- `.gitignore`
- `.gitattributes`
- `pyproject.toml` (minimale, `dependencies = []`, nessun framework scelto)
- `README.md`
- `CLAUDE.md`
- `docs/reports/BOOTSTRAP_REPORT.md` (questo file)
- File segnaposto `.gitkeep` nelle sottocartelle vuote di `tests/`,
  `scripts/`, `tools/`, `docs/architecture/`
- `src/manualsync/__init__.py` (vuoto — nessun codice applicativo)

## 7. File già presenti non modificati

- `.venv/` (ambiente virtuale preesistente, non toccato)
- Contenuto dei quattro documenti di prodotto: spostati ma **non
  modificati** in alcun byte.

## 8. Documenti mancanti

- `ManualSync_Guida_Prodotto_Completo_V4.pdf` — **atteso come PDF**, ma
  nella root era presente invece `ManualSync_Guida_Prodotto_Completo_V4.docx`
  (stesso nome base, estensione diversa). Il PDF non esiste nel
  repository.
- `ManualSync_CLAUDE_HANDOFF_V4.zip` — referenziato dal file
  `ManualSync_CLAUDE_HANDOFF_V4.sha256.txt` ma **assente** dalla root e da
  tutto il repository. Non è stato possibile verificare l'hash.
- Nessun "V3 handoff" o "Final Candidate" (citati nel Prompt Pack, Prompt
  0) sono presenti nel repository.

## 9. Incongruenze V3/V4 rilevate (non corrette, solo documentate)

1. **Nome file vs intestazione interna**: i file si chiamano
   `ManualSync_Product_Doctrine_V4.md` e
   `ManualSync_Prompt_Pack_Claude_V4.md`, ma i rispettivi titoli interni
   sono `# ManualSync Product Doctrine V3` e
   `# ManualSync - Claude Prompt Pack V3`.
2. **Riferimenti a "Final Candidate"**: il Prompt 0 del Prompt Pack dice
   "Read the full V3 handoff and Final Candidate. Do not modify files." —
   nessun handoff né Final Candidate sono presenti in questo repository.
3. **Deliverable con suffisso V3**: tutti i deliverable dei prompt (es.
   `BASELINE_V3.md`, `COMPETITIVE_AND_ADOPTION_GAP_V3.md`) usano il
   suffisso V3 nonostante il pacchetto documentale sia etichettato V4.
4. **Riferimento SHA256 a uno ZIP assente**: `ManualSync_CLAUDE_HANDOFF_V4.sha256.txt`
   contiene un hash SHA-256 per `ManualSync_CLAUDE_HANDOFF_V4.zip`, file
   che non esiste da nessuna parte nel repository — impossibile verificare
   l'integrità di un handoff che non è mai stato fornito.
5. **Formato guida prodotto**: nome file `..._V4.docx` invece del
   `..._V4.pdf` atteso; il contenuto non è stato letto in modo affidabile
   in questa fase (il tool di lettura disponibile non supporta binari
   `.docx` e non è stata installata alcuna libreria di parsing, per non
   violare il vincolo "nessuna dipendenza installata in questa fase").
   Il contenuto della guida non è stato quindi verificato contro gli
   altri documenti.

Queste incongruenze **non sono state corrette**: sono bloccanti per il
Prompt 0 del Prompt Pack ("Read the full V3 handoff and Final Candidate"),
che richiede materiale non presente in questo repository.

## 10. Presenza/assenza di `ManualSync_CLAUDE_HANDOFF_V4.zip`

**Assente.** Solo il file di hash (`.sha256.txt`) è presente; lo ZIP
referenziato non è mai stato copiato nella root.

## 11. Sorgenti/build ManualSync preesistenti rilevati

**Nessuno.** Prima del bootstrap la root conteneva esclusivamente
`.venv/` e i quattro documenti sopra elencati. Nessun codice sorgente,
nessuno ZIP applicativo, nessuna build, nessun database.

## 12. Dipendenze installate durante questa fase

**NESSUNA.** Non è stato eseguito alcun `pip install`, né in `.venv` né
altrove. `pyproject.toml` dichiara `dependencies = []`.

## 13. `git status` (stato finale)

```
On branch master
No commits yet

Changes to be committed:
  new file:   .gitattributes
  new file:   .gitignore
  new file:   CLAUDE.md
  new file:   README.md
  new file:   docs/architecture/.gitkeep
  new file:   docs/product/ManualSync_CLAUDE_HANDOFF_V4.sha256.txt
  new file:   docs/product/ManualSync_Guida_Prodotto_Completo_V4.docx
  new file:   docs/product/ManualSync_Product_Doctrine_V4.md
  new file:   docs/product/ManualSync_Prompt_Pack_Claude_V4.md
  new file:   pyproject.toml
  new file:   scripts/.gitkeep
  new file:   src/manualsync/__init__.py
  new file:   tests/e2e/.gitkeep
  new file:   tests/fixtures/.gitkeep
  new file:   tests/integration/.gitkeep
  new file:   tests/unit/.gitkeep
  new file:   tools/.gitkeep
```

Il repository è pulito e contiene solo la baseline di bootstrap: i file
sono stati messi in staging (`git add`) per un commit iniziale, ma **il
commit non è stato eseguito** perché l'identità Git
(`user.name`/`user.email`) non è configurata in questo ambiente, né a
livello globale né locale, e non doveva essere impostata automaticamente.

## 14. Blocker prima del Prompt 0

1. **Identità Git mancante** — impedisce di finalizzare il commit
   iniziale. Richiede `git config user.name` / `user.email` (globale o
   locale al repo), decisione dell'utente.
2. **Handoff V3 / Final Candidate assente** — il Prompt 0 del Prompt Pack
   richiede esplicitamente di leggere "the full V3 handoff and Final
   Candidate", materiale non presente in questo repository.
3. **`ManualSync_CLAUDE_HANDOFF_V4.zip` assente** — impossibile verificare
   l'hash SHA-256 dichiarato o accedere al suo contenuto.
4. **Guida prodotto in formato DOCX, non PDF, e non letta** — non è
   possibile usarla in questa fase come "ulteriore specifica di prodotto"
   affidabile senza uno strumento di parsing DOCX, che non è stato
   installato per rispettare il vincolo "nessuna dipendenza in questa
   fase".
5. **Incongruenze V3/V4 non risolte** — vedi sezione 9; richiedono una
   decisione esplicita dell'utente su quale versione documentale/nome
   deliverable sia autoritativa prima di eseguire il Prompt 0 e i
   successivi.

Nessuno di questi blocker è stato aggirato o corretto silenziosamente.
