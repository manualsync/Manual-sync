# ERYLOS — Politica terminologica V1

Stato: **vincolante** per ogni nuova stringa di interfaccia.
Supera la tabella terminologica di `ERYLOS_UX_ARCHITECTURE_AND_LOCALIZATION_V1.md`
§3, che resta valida solo dove non contraddetta da questo documento.

## Principio

L'italiano è la lingua di default del prodotto, non una traduzione
dell'inglese. **Un termine inglese non viene mantenuto solo perché è
familiare nel software.** Si mantiene l'inglese unicamente quando è uno
standard o un acronimo di settore, oppure quando una forma italiana
esiste ma sarebbe meno chiara per l'utente esperto — e in quel caso la
ragione va scritta, non sottintesa.

Questa politica corregge esplicitamente una scelta precedente: nella
prima stesura *Workflow*, *Preflight*, *Template* e *Batch* erano stati
lasciati in inglese per familiarità. Non è un motivo sufficiente.

## Tabella terminologica

| Inglese | Italiano preferito | Note |
|---|---|---|
| Compliance | **Conformità** | Termine giuridico italiano esatto nel settore macchine. |
| Release | **Rilascio** | «Rilascio controllato» è italiano tecnico corrente. |
| Workflow | **Flusso di lavoro** | Applicato al kicker di sezione. Non «Workflow». |
| Coverage | **Copertura** | Già in uso. |
| Evidence | **Evidenze** | Già in uso. |
| Covered | **Coperto** | Stato di copertura. |
| Missing | **Mancante** | Stato di copertura. |
| To verify | **Da verificare** | Stato di copertura. |
| Not applicable | **Non applicabile** | Stato di applicabilità. |
| Scope | **Perimetro (normativo)** | Già in uso. |
| Publishing | **Pubblicazione** | |
| Draft | **Bozza** | |
| Preview | **Anteprima** | |
| Export | **Esporta / Esportazione** | |

### Termini dipendenti dal contesto

| Inglese | Italiano preferito | Quando |
|---|---|---|
| Preflight | **Controllo pre-esportazione** | Verifiche eseguite prima di produrre un file. |
| | **Verifica di pubblicazione** | Verifiche sullo stato complessivo di una pubblicazione. |
| Template | **Modello documento** | Struttura/contenuto di un documento. |
| | **Modello di impaginazione** | Layout, stili, gabbia grafica. |
| Batch | **Elaborazione in serie** | Operazione su più oggetti in una volta. |
| TM (translation memory) | **Memoria di traduzione (TM)** | Forma estesa con sigla fra parentesi dove utile. |
| QA | **QA** | Contesti specialistici di traduzione. |
| | **Controllo qualità** | Interfaccia generale, utenti non specialisti. |

**Nota su «Template»:** la doppia resa è deliberata. «Modello» da solo è
ambiguo in questo prodotto, che ha già il concetto di *modello di
prodotto* nei pacchetti normativi. Scegliere la forma estesa in base a
cosa il template contiene davvero.

### Invariati — standard e acronimi

`PDF` · `DOCX` · `XLIFF` · `IDML` · `DITA` · `CE` · `UE`

Nomi di formato, di protocollo e sigle normative non si traducono mai.

## Fuori dal perimetro della localizzazione

Il **contenuto normativo verificato** (`compliance_starter_packs.py`:
titoli dei requisiti, riferimenti ad articoli, sintesi in italiano) **non
è materiale traducibile**. È testo verificato sul consolidato ufficiale
italiano. Una versione inglese richiede una verifica indipendente sul
consolidato EN (`02023R1230 — EN — 27.07.2026 — 002.001`), non una
traduzione. Trattarlo come stringhe di catalogo sarebbe un errore grave.

Lo stesso vale per i nomi degli **ambiti di applicabilità** definiti dal
pacchetto (es. «Macchina completa»): sono contenuto del pacchetto.

## Applicazione

Questa politica vale per le stringhe **già migrate** e per ogni stringa
nuova. Non giustifica di per sé la migrazione di schermate fuori
perimetro: Revisione, Contenuti, Traduzioni, Lavorazioni e Rilascio
adottano questi termini quando vengono migrate nel proprio gate.

Stato al momento della stesura: l'unico termine di questa tabella
presente nella superficie già migrata è *Workflow → Flusso di lavoro*,
applicato. *Preflight*, *Template*, *Batch*, *TM* e *QA* compaiono solo
in schermate non ancora migrate.
