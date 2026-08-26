# Legacy .doc runtime packaging — V1

Milestone 3B.1. Questo documento descrive cosa serve per rendere il runtime di
conversione `.doc` → `.docx` (LibreOffice headless, sidecar) distribuibile
commercialmente. **Non è stato applicato nessun cambiamento a installer/build
in questa milestone** — `legacy_doc_conversion.py` risolve oggi solo un
runtime sidecar non ancora presente (fallback DEV su un'installazione locale
di LibreOffice, mai una dipendenza richiesta al cliente). Questo report è il
prerequisito da chiudere prima di quel lavoro di packaging.

## 1. Versione LibreOffice prevista

Non fissiamo qui una versione puntuale: va scelta al momento
dell'implementazione dell'installer, verificando l'ultima release **stabile**
disponibile su https://www.libreoffice.org/download/ (ramo "still", non
"fresh"/beta). Requisito minimo: la versione scelta deve essere quella
effettivamente validata nel test di accettazione manuale con il corpus di
`.doc` industriali reali — se in futuro si aggiorna la versione bundlata,
il test di accettazione va ripetuto (vedi §6).

Va registrata qui, alla prima implementazione reale del bundling, la tripla:
`versione — data di rilascio upstream — hash SHA-256 dell'installer scaricato`.

## 2. Runtime sidecar — layout previsto

`legacy_doc_conversion.py` si aspetta il binario in:

```
ManualSync_1.0_FINAL_CANDIDATE/runtime/libreoffice/program/soffice.exe
```

(`SIDECAR_RUNTIME_DIR` / `SIDECAR_SOFFICE` nel modulo). Questo non è
il layout ufficiale di installazione di LibreOffice — è la posizione in cui
l'installer di ManualSync dovrà **estrarre/copiare** (non installare via il
normale setup MSI di LibreOffice, che registrerebbe l'app nel menu Start,
creerebbe voci di Windows Programs and Features visibili all'utente, ecc.).

Opzioni tecniche da valutare in fase di packaging (non decise ora):
- **Estrazione da un archivio "portable"/zip** della distribuzione LibreOffice
  ufficiale nella cartella sidecar durante l'installazione di ManualSync,
  senza passare dal wizard MSI standard di LibreOffice — evita che compaia
  come app separata nel sistema del cliente, coerente con "l'utente non deve
  sapere che esiste LibreOffice".
- **Bundling selettivo**: verificare se è possibile includere solo i moduli
  necessari alla conversione Writer (`.doc`/`.docx`), escludendo Calc,
  Impress, Base, Draw, per ridurre il footprint — da validare che la
  conversione `.doc`→`.docx` continui a funzionare correttamente con un
  subset ridotto (rischio: filtri di import/export possono avere dipendenze
  interne trasversali tra moduli; non assumere che sia banale).

## 3. Footprint stimato

- LibreOffice Windows completo, installazione tipica: **~600–900 MB su disco**
  (a seconda della versione; l'installer scaricabile è ~300–400 MB compresso).
- Con un subset "solo Writer" (se tecnicamente validato): stima ottimistica
  **~200–350 MB**, ma **non verificata** — da misurare concretamente prima di
  comunicare questo numero come impegno di prodotto.
- Impatto sull'installer ManualSync: passa da un singolo eseguibile
  PyInstaller (dimensione attuale nell'ordine delle decine di MB) a un
  pacchetto nell'ordine delle centinaia di MB. Va comunicato esplicitamente
  nel materiale di rilascio pilota (tempo di download, spazio richiesto).

## 4. File/licenze da ridistribuire — dettaglio, non solo "attribuzione"

LibreOffice non è un singolo blocco MPL: è un aggregato di componenti con
licenze diverse (MPL 2.0, LGPLv3, e numerose librerie di terze parti interne
con licenze permissive — BSD, MIT, Apache, zlib — usate per formati, font
rendering, compressione, ecc.). Ridistribuire il binario **richiede tutti** i
seguenti elementi, non un singolo file di attribuzione:

1. **Il file `LICENSE`/`LICENSE.html` e `NOTICE`/`NOTICE.html` ufficiali** che
   accompagnano la distribuzione LibreOffice scelta — vanno copiati integri
   nel pacchetto ManualSync (non riassunti, non sostituiti da un link).
   Questi file elencano i crediti per-componente delle librerie di terze
   parti bundlate — nessuna di quelle notice può essere rimossa.
2. **Nessuna modifica ai file sorgente coperti da MPL 2.0** senza pubblicarne
   le modifiche sotto MPL — nel piano attuale (bundling del binario ufficiale
   non modificato) questo obbligo non scatta, ma va ri-verificato se in
   futuro si applica qualunque patch/fork al codice LibreOffice stesso.
3. **Componenti LGPLv3** (parte del core LibreOffice è dual/joint-licenziato
   MPL 2.0/LGPLv3+): la LGPL impone, per chi distribuisce i binari, di
   garantire all'utente la possibilità di sostituire/rilinkare la libreria
   con una versione modificata — nella pratica, per una distribuzione non
   modificata, questo si soddisfa fornendo un **"written offer" per il codice
   sorgente** dei componenti LGPL (tipicamente: un riferimento verificabile
   all'archivio sorgente ufficiale corrispondente esattamente alla build
   distribuita — versione e hash inclusi). Non basta linkare la home page del
   progetto in modo generico: il riferimento deve essere alla versione esatta
   bundlata.
4. **Notice di terze parti interne a LibreOffice** (elencate nei file del
   punto 1) — vanno preservate così come sono nella distribuzione ufficiale;
   ManualSync non deve produrne una versione propria riassunta.
5. **Nessuna implicazione di endorsement**: i nomi "LibreOffice"/"The
   Document Foundation" non possono essere usati per implicare che
   ManualSync sia approvato o sponsorizzato da loro (clausola trademark
   standard MPL/LGPL) — coerente comunque con il requisito UX di non
   esporre affatto questi nomi all'utente finale.

**Non è vero che "basta l'attribuzione"**: servono i file di licenza
integrali, l'offerta verificabile del sorgente per le parti LGPL, e la
tracciabilità versione-per-versione di cosa è stato bundlato. Prima della
release commerciale GA (non del pilota interno), va fatta una revisione
legale/compliance formale di questo pacchetto — questo documento è la base
tecnica per quella revisione, non la sostituisce.

## 5. Obblighi ricorrenti (non un adempimento una tantum)

Ogni volta che si aggiorna la versione di LibreOffice bundlata, va aggiornato
anche il set di file di licenza/notice ridistribuiti (possono cambiare le
librerie di terze parti incluse tra una versione e l'altra) — non è
sufficiente farlo una volta alla prima release.

## 6. Strategia di aggiornamento del runtime

- **Versione fissata per release ManualSync**, non auto-update lato cliente:
  coerente con il vincolo local-first/offline — il runtime non deve
  scaricare aggiornamenti da solo. Ogni nuova versione del sidecar LibreOffice
  è distribuita solo tramite un nuovo installer/aggiornamento di ManualSync.
- **Tracciamento sicurezza**: monitorare gli avvisi di sicurezza pubblici di
  LibreOffice (CVE) per la versione bundlata; un aggiornamento di sicurezza
  del sidecar richiede una nuova release di ManualSync anche se nessun'altra
  funzionalità cambia.
- **Prima di ogni bump di versione**: ripetere il test di accettazione
  manuale con il corpus di `.doc` industriali reali (stessa disciplina già
  applicata in 3B) — una nuova versione di LibreOffice può cambiare
  leggermente il comportamento dei filtri di conversione legacy.
- **Compatibilità Windows**: verificare la versione minima di Windows
  supportata dalla release LibreOffice scelta resti compatibile con la
  baseline Windows già definita per ManualSync (vedi `environment_guard.py`).
