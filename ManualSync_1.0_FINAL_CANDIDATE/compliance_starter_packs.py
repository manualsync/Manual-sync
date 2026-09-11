"""Milestone 4F.2: curated compliance starter packs.

Turns the 3.5A/3.5B/3.5C compliance engine from an empty framework into a
usable product by shipping ONE curated, versioned, offline pack of
DOCUMENTATION obligations for Regulation (EU) 2023/1230 on machinery.

Pure data + pure functions. No gradio import, no network, no runtime
fetch of regulatory content: the pack travels with the application and
works entirely offline. Official URLs are carried as PROVENANCE only --
they are shown to the user so a decision can be traced back to the
source, never fetched.

## What this module is NOT

It is not a conformity engine. Nothing here decides whether machinery is
legally compliant, and nothing here may ever produce a COMPLIANT verdict.
ERYLOS records documentation traceability: which obligation exists, where
it comes from, what a human decided about its applicability, and which
manual content has been linked as evidence. The legal conclusion stays
with the manufacturer.

## Verification status

Every requirement carries `verification_status`. The pack ships in
Italian, so the Italian consolidated text is the SOURCE OF RECORD and
every requirement was verified against it clause by clause:

    Consolidated TEXT: 02023R1230 — IT — 27.07.2026 — 002.001
    Cross-check:       02023R1230 — EN — 27.07.2026 — 002.001
    (base Regulation (EU) 2023/1230, as amended by Regulation (EU)
    2024/2748 and Regulation (EU) 2026/1744)

both obtained from the EU Publications Office Cellar endpoint
(`publications.europa.eu/resource/celex/02023R1230-20260727`, content-
negotiated as XHTML with `Accept-Language`) because eur-lex.europa.eu
itself answers automated requests with a JS/consent shell rather than
the text.

Verifying against a translation is not a formality here. The Italian
text is what the user reads and cites, and it names things differently
from the English one. Wording that merely *translates* the English is a
defect in a traceability tool: an auditor searching the Regulation for
the phrase shown in ERYLOS must find it. So the summaries now use the
Regulation's own Italian vocabulary, including:

  * `utilizzatore` (not "utente"), `avaria` (not "guasto"),
    `mandatario` (not "rappresentante autorizzato").
  * `dichiarazione di conformità UE` and `dichiarazione di
    incorporazione UE` -- the noun order the Italian text uses.
  * `ciclo di vita previsto` for the online-availability duty.
  * `informazioni sulla sicurezza` -- the Article 10(7) term of art for
    the paper-copy duty, distinct from generic "informazioni di
    sicurezza".
  * `codici a lettura ottica` (Article 10(8)) / `codice ottico`
    (Annex III and Annex XI) -- the Regulation itself is not uniform,
    and each provision keeps its own form.
  * `attrezzature di protezione individuale` (not "dispositivi").
  * `istruzioni per l'assemblaggio` for partly completed machinery --
    NOT "istruzioni di montaggio", which in Italian collides with
    1.7.4.2(1)(i) "istruzioni per il montaggio" of full machinery.
  * `se del caso` as the 1.7.4.2 / Annex XI chapeau qualifier, replacing
    the paraphrase "ove applicabile".
  * Official citation style: `Articolo 10, paragrafo 7, lettera c)` and
    `Allegato III, punto 1.7.4.2, punto 1, lettera u)`.

Two near-homonyms are deliberately kept apart, because the Regulation
keeps them apart: Article 10(7) addresses `utilizzatori non
professionali` while Annex III 1.7.4.1(b) addresses `operatori non
professionali`. Collapsing them into one word would silently merge two
different scopes.

The Italian pass also corrected substance, not only vocabulary:

  * 1.7.4 maintenance-language derogation covers staff appointed by the
    manufacturer OR BY ITS `mandatario`; the second half was missing.
  * 1.7.4.1(a) is one obligation phrased as "not only ... but also".
    The two records now carry that shape instead of asserting a bare
    duty to describe intended use.
  * 1.7.4.2(1)(h): the Italian is "che potrebbero, in base
    all'esperienza, presentarsi" -- misuse that experience suggests MAY
    occur, not misuse "experience has shown" does occur.
  * 1.7.4.2(1)(l): `misure di protezione integrate nella progettazione`,
    not the looser "misure di progettazione sicura".
  * 1.7.4.2(1)(o) listed "smontaggio fuori servizio" as one condition
    and omitted `montaggio`; the official list is montaggio, smontaggio,
    condizioni di fuori servizio, prove, avarie prevedibili.
  * 1.7.4.2(1)(r) omitted "tenendo conto della progettazione e
    dell'utilizzo".
  * 1.7.4.2(1)(u) was a vague pointer to "soglie previste" and now
    carries the actual thresholds (70 dB(A), 63 Pa / 130 dB, 80 dB(A)).
  * 1.7.4.2(1)(w) omitted `attivi o non attivi` for implantable medical
    devices.
  * Annex XI point 1 omitted animali domestici, beni and ambiente.
  * Annex XI point 2 omitted items (k) to (n), including the duty to
    identify which version of the assembly instructions corresponds to
    the model.

An earlier 0.1.0 draft, superseded before release, additionally missed
eight whole 1.7.4.2 obligations and both Article 10(7)/(8) linkage
duties; those were added by the English verification pass.

Article 11(7)(c) sets ONLY a 10-year online-availability period for
partly completed machinery, with no expected-lifetime clause -- unlike
Article 10(7)(c). Article 11(8) is the same. That asymmetry is real and
is preserved rather than smoothed over.

Any requirement that cannot be supported by the official text must stay
UNVERIFIED_DRAFT, remain visibly flagged, and be excluded from
authoritative coverage -- see `pack_preview()` and
`compliance_view.documentation_coverage()`.

Requirement summaries remain explicitly NON-NORMATIVE paraphrases: the
Regulation's own wording is not reproduced here, only referenced.

Any requirement that cannot be supported by the official text must stay
UNVERIFIED_DRAFT, remain visibly flagged, and be excluded from
authoritative coverage -- see `pack_preview()` and
`compliance_view.documentation_coverage()`.

## Versioning

Two numbers, two jobs:

  * `pack_version` (semver, user-facing) -- the pack's LEGAL SCOPE
    release. It moves when the set of obligations changes: 1.0.0 -> 1.1.0
    added Annex XI point 3, an obligation the pack did not carry at all.
  * `content_version` (monotonic int, internal) -- the source-content
    revision. It moves on ANY content change, wording corrections
    included, and never resets when `pack_version` moves. It is what a
    project compares itself against to detect that a newer source exists.

A project stores the `content_version` each requirement row came from, so
"is there an update?" is a local integer comparison and needs no network.
Rows imported before that column existed read as 0, which correctly means
"older than anything shipping today".

## Immutability, and why an update path is still required

A pack is a versioned SOURCE. Importing it creates requirement records in
a project; a user's applicability/coverage decisions live in the separate
project overlay (compliance_applicability / compliance_assessment) and
are never written back into the pack.

Import is additive and idempotent: it inserts requirement ids that are
not there yet and never rewrites one, so a second import cannot reset a
project's decisions. That is the right behaviour for import -- and it is
exactly why import alone is not enough. A project that imported an older
revision would otherwise keep pre-verification wording forever while its
provenance panel still said VERIFIED_OFFICIAL, which is worse than
showing nothing.

So updating is a SEPARATE, EXPLICIT action
(`ComplianceService.update_requirement_pack`) with its own contract: it
refreshes source text, adds newly introduced obligations, retires
withdrawn ones without deleting history, and preserves the project
overlay for every stable requirement id. See `pack_diff()` for how a
wording correction and a widened duty are deliberately treated
differently.
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------
# Provenance vocabulary
# ---------------------------------------------------------------------

PROVENANCE_LEGAL = "legal"
PROVENANCE_REFERENCE_STANDARD = "reference_standard"
PROVENANCE_TYPES = {PROVENANCE_LEGAL, PROVENANCE_REFERENCE_STANDARD}

VERIFIED_OFFICIAL = "VERIFIED_OFFICIAL"
UNVERIFIED_DRAFT = "UNVERIFIED_DRAFT"
VERIFICATION_STATES = {VERIFIED_OFFICIAL, UNVERIFIED_DRAFT}

# Applicability groups let a project choose a subscope instead of being
# handed a checklist that mixes full machinery with partly completed
# machinery -- two legally different situations.
GROUP_FULL_MACHINERY = "full_machinery"
GROUP_PARTLY_COMPLETED = "partly_completed"
APPLICABILITY_GROUPS = {
    GROUP_FULL_MACHINERY: "Macchina completa",
    GROUP_PARTLY_COMPLETED: "Quasi-macchina (assemblaggio)",
}

_EURLEX_EN = "https://eur-lex.europa.eu/eli/reg/2023/1230/oj/eng"
_EURLEX_IT = "https://eur-lex.europa.eu/eli/reg/2023/1230/oj/ita"
_IEC_82079 = "https://webstore.iec.ch/en/publication/29075"

# The disclaimer every entry point must show. Single source of truth so
# the wording cannot drift between the import preview, the coverage view
# and the requirement detail.
PACK_DISCLAIMER = (
    "ERYLOS aiuta a tracciare la documentazione e la sua copertura. Non certifica "
    "automaticamente la conformità legale della macchina: le decisioni di applicabilità e "
    "la valutazione finale restano di competenza del fabbricante."
)

UNVERIFIED_NOTICE = (
    "Contenuto del pacchetto non ancora verificato rispetto al testo ufficiale consolidato. "
    "Verifica i riferimenti sulla fonte ufficiale prima di usarlo come riferimento in un "
    "contesto reale."
)


def _req(rid: str, ref: str, title: str, summary: str, *,
         group: str = GROUP_FULL_MACHINERY, qualifier: str = "",
         verified: bool = True, substantive_revision: int = 1) -> dict[str, Any]:
    """One traceable documentation requirement.

    `summary` is always a short, non-normative paraphrase written for a
    documentation manager -- never a quotation of the legal text, so the
    pack carries no reproduction of the official wording.

    `qualifier` preserves an applicability condition carried by the
    provision itself ("se del caso", "solo formato digitale", ...).
    Losing those qualifiers is how a traceability aid quietly turns into
    a wrong checklist, so they are stored as data rather than buried in
    prose.

    `substantive_revision` increments ONLY when the obligation itself
    changes what must be documented -- never when its wording is merely
    corrected. A pack update bumps the stored requirement version (and so
    sends existing human decisions back to "Da verificare") exactly for
    those, so a terminology fix cannot quietly invalidate a project's
    work while a widened duty cannot quietly keep a stale "Coperto"."""
    return {
        "requirement_id": rid,
        "article_ref": ref,
        "title": title,
        "summary": summary,
        "qualifier": qualifier,
        "substantive_revision": int(substantive_revision),
        "provenance_type": PROVENANCE_LEGAL,
        "source_url": _EURLEX_IT,
        "source_lang": "it",
        "applicability_group": group,
        "verification_status": VERIFIED_OFFICIAL if verified else UNVERIFIED_DRAFT,
    }


_Q_WHERE_APPLICABLE = "Se del caso"
_Q_DIGITAL = "Solo se le istruzioni sono fornite in formato digitale"


# ---------------------------------------------------------------------
# Regulation (EU) 2023/1230 -- documentation obligations
#
# Scope is deliberately limited to what a TECHNICAL DOCUMENTATION team
# owns: instructions for use, their delivery format and language, and the
# declaration-of-conformity linkage. It is not the whole Regulation and
# must never be presented as such.
# ---------------------------------------------------------------------

_MACHINERY_REQUIREMENTS: list[dict[str, Any]] = [
    # ---- Articolo 10, paragrafo 7: istruzioni e loro fornitura ----
    _req("MR2023-A10-7-INSTRUCTIONS", "Articolo 10, paragrafo 7",
         "Istruzioni per l'uso e informazioni di cui all'allegato III",
         "La macchina o il prodotto correlato deve essere accompagnato dalle istruzioni per l'uso e "
         "dalle informazioni di cui all'allegato III."),
    _req("MR2023-A10-7-MODEL-IDENTIFICATION", "Articolo 10, paragrafo 7",
         "Modello di prodotto descritto nelle istruzioni",
         "Le istruzioni e le informazioni devono descrivere chiaramente il modello di prodotto al quale "
         "corrispondono."),
    _req("MR2023-A10-7-DIGITAL-FORM", "Articolo 10, paragrafo 7",
         "Facoltà di fornire le istruzioni in formato digitale",
         "Le istruzioni possono essere fornite in formato digitale; in tal caso si applicano le "
         "condizioni delle lettere a), b) e c)."),
    _req("MR2023-A10-7-ACCESS-INDICATION", "Articolo 10, paragrafo 7, lettera a)",
         "Modalità di accesso alle istruzioni in formato digitale",
         "Occorre indicare sulla macchina o sul prodotto correlato oppure, ove ciò non sia possibile, "
         "sull'imballaggio o in un documento di accompagnamento, le modalità di accesso alle istruzioni "
         "in formato digitale.", qualifier=_Q_DIGITAL),
    _req("MR2023-A10-7-PRINT-DOWNLOAD", "Articolo 10, paragrafo 7, lettera b)",
         "Formato che consente stampa, download e salvataggio",
         "Il formato deve consentire all'utilizzatore di stampare e scaricare le istruzioni e di "
         "salvarle su un dispositivo elettronico, in maniera da potervi accedere in qualsiasi momento, "
         "in particolare in caso di avaria; vale anche quando le istruzioni sono integrate nel software.",
         qualifier=_Q_DIGITAL),
    _req("MR2023-A10-7-AVAILABILITY", "Articolo 10, paragrafo 7, lettera c)",
         "Accessibilità online delle istruzioni digitali",
         "Le istruzioni devono restare accessibili online durante il ciclo di vita previsto della "
         "macchina o del prodotto correlato e per un periodo di almeno 10 anni dopo l'immissione sul "
         "mercato.", qualifier=_Q_DIGITAL),
    _req("MR2023-A10-7-PAPER-ON-REQUEST", "Articolo 10, paragrafo 7",
         "Formato cartaceo su richiesta al momento dell'acquisto",
         "Su richiesta dell'utilizzatore al momento dell'acquisto, il fabbricante fornisce gratuitamente "
         "le istruzioni per l'uso in formato cartaceo entro un mese."),
    _req("MR2023-A10-7-PAPER-SAFETY-INFO", "Articolo 10, paragrafo 7",
         "Informazioni sulla sicurezza cartacee per utilizzatori non professionali",
         "Per macchine o prodotti correlati destinati a utilizzatori non professionali, o che in "
         "condizioni ragionevolmente prevedibili possono essere utilizzati da essi anche se non ad essi "
         "destinati, le informazioni sulla sicurezza essenziali per la messa in servizio e per l'uso "
         "sicuro vanno fornite in formato cartaceo."),
    _req("MR2023-A10-7-LANGUAGE", "Articolo 10, paragrafo 7",
         "Lingua, chiarezza e leggibilità",
         "Istruzioni per l'uso, informazioni sulla sicurezza e informazioni di cui all'allegato III "
         "devono essere redatte in una lingua facilmente comprensibile dagli utilizzatori, secondo "
         "quanto stabilito dallo Stato membro interessato, e devono essere chiare, comprensibili e "
         "leggibili."),

    # ---- Articolo 10, paragrafo 8: collegamento alla dichiarazione ----
    _req("MR2023-A10-8-DOC-ACCOMPANY", "Articolo 10, paragrafo 8",
         "Dichiarazione di conformità UE che accompagna il prodotto",
         "La macchina o il prodotto correlato deve essere accompagnato dalla dichiarazione di "
         "conformità UE di cui all'allegato V, parte A."),
    _req("MR2023-A10-8-DOC-DIGITAL-ACCESS", "Articolo 10, paragrafo 8",
         "Indirizzo internet o codici a lettura ottica per la dichiarazione",
         "In alternativa, nelle istruzioni per l'uso e nelle informazioni di cui all'allegato III va "
         "fornito l'indirizzo internet o codici a lettura ottica dove è possibile accedere alla "
         "dichiarazione di conformità UE."),
    _req("MR2023-A10-8-DOC-AVAILABILITY", "Articolo 10, paragrafo 8",
         "Accessibilità online della dichiarazione digitale",
         "Le dichiarazioni di conformità UE digitali devono essere rese accessibili online per il ciclo "
         "di vita previsto della macchina o del prodotto correlato e, in ogni caso, per un periodo di "
         "almeno 10 anni dopo l'immissione sul mercato o la messa in servizio."),

    # ---- Allegato III, punti 1.7.4 e 1.7.4.1 ----
    _req("MR2023-A3-1740-INSTRUCTIONS", "Allegato III, punto 1.7.4",
         "Redazione delle istruzioni per l'uso",
         "In aggiunta agli obblighi di cui all'articolo 10, paragrafo 7, le istruzioni per l'uso devono "
         "essere redatte secondo quanto previsto dall'allegato III."),
    _req("MR2023-A3-1740-MAINT-LANGUAGE-EXCEPTION", "Allegato III, punto 1.7.4",
         "Deroga linguistica per la manutenzione specialistica",
         "In deroga all'articolo 10, paragrafo 7, le istruzioni per la manutenzione destinate ad essere "
         "usate da personale specializzato incaricato dal fabbricante o dal suo mandatario possono "
         "essere fornite in una sola lingua ufficiale dell'Unione compresa da detto personale.",
         substantive_revision=2),
    _req("MR2023-A3-17441-INTENDED-USE", "Allegato III, punto 1.7.4.1, lettera a)",
         "Uso previsto nelle istruzioni per l'uso",
         "Il contenuto delle istruzioni per l'uso non deve riguardare soltanto l'uso previsto della "
         "macchina o del prodotto correlato."),
    _req("MR2023-A3-17441-MISUSE", "Allegato III, punto 1.7.4.1, lettera a)",
         "Uso scorretto ragionevolmente prevedibile",
         "Il contenuto delle istruzioni per l'uso deve tener conto anche dell'uso scorretto "
         "ragionevolmente prevedibile."),
    _req("MR2023-A3-17441-NON-PROFESSIONAL", "Allegato III, punto 1.7.4.1, lettera b)",
         "Redazione per operatori non professionali",
         "Per macchine o prodotti correlati destinati all'utilizzazione da parte di operatori non "
         "professionali, la redazione e la presentazione delle istruzioni per l'uso devono tenere conto "
         "del livello di formazione generale e della perspicacia che ci si può ragionevolmente "
         "aspettare da tali operatori."),

    # ---- Allegato III, punto 1.7.4.2, punto 1: contenuto, lettere a)-x) ----
    # Il chapeau qualifica l'INTERO elenco con "se del caso": il
    # qualificatore viene conservato su ogni voce invece di essere perso.
    _req("MR2023-A3-17442-MANUFACTURER", "Allegato III, punto 1.7.4.2, punto 1, lettera a)",
         "Ragione sociale e indirizzo del fabbricante",
         "La ragione sociale e l'indirizzo completo del fabbricante e, se del caso, del suo mandatario.",
         qualifier=_Q_WHERE_APPLICABLE),
    _req("MR2023-A3-17442-DESIGNATION", "Allegato III, punto 1.7.4.2, punto 1, lettera b)",
         "Designazione della macchina o del prodotto correlato",
         "La designazione della macchina o del prodotto correlato come indicata sul prodotto stesso, "
         "eccetto il numero di serie.", qualifier=_Q_WHERE_APPLICABLE),
    _req("MR2023-A3-17442-DECLARATION-REF", "Allegato III, punto 1.7.4.2, punto 1, lettera c)",
         "Dichiarazione di conformità UE o riferimento di accesso",
         "La dichiarazione di conformità UE oppure l'indirizzo internet o il codice ottico dove è "
         "possibile accedere alla dichiarazione di conformità UE, conformemente all'articolo 10, "
         "paragrafo 8.", qualifier=_Q_WHERE_APPLICABLE),
    _req("MR2023-A3-17442-GENERAL-DESCRIPTION", "Allegato III, punto 1.7.4.2, punto 1, lettera d)",
         "Descrizione generale del prodotto",
         "Una descrizione generale della macchina o del prodotto correlato.",
         qualifier=_Q_WHERE_APPLICABLE),
    _req("MR2023-A3-17442-DRAWINGS", "Allegato III, punto 1.7.4.2, punto 1, lettera e)",
         "Disegni, diagrammi, descrizioni e spiegazioni",
         "I disegni, i diagrammi, le descrizioni e le spiegazioni necessari per l'uso, la manutenzione e "
         "la riparazione del prodotto e per verificarne il corretto funzionamento.",
         qualifier=_Q_WHERE_APPLICABLE),
    _req("MR2023-A3-17442-WORKSTATIONS", "Allegato III, punto 1.7.4.2, punto 1, lettera f)",
         "Descrizione delle postazioni di lavoro",
         "Una descrizione della o delle postazioni di lavoro che possono essere occupate dagli "
         "operatori.", qualifier=_Q_WHERE_APPLICABLE),
    _req("MR2023-A3-17442-INTENDED-USE", "Allegato III, punto 1.7.4.2, punto 1, lettera g)",
         "Descrizione dell'uso previsto",
         "Una descrizione dell'uso previsto della macchina o del prodotto correlato.",
         qualifier=_Q_WHERE_APPLICABLE),
    _req("MR2023-A3-17442-MISUSE-WARNINGS", "Allegato III, punto 1.7.4.2, punto 1, lettera h)",
         "Avvertenze sui modi in cui il prodotto non deve essere usato",
         "Le avvertenze concernenti i modi nei quali la macchina o il prodotto correlato non devono "
         "essere usati e che potrebbero, in base all'esperienza, presentarsi.",
         qualifier=_Q_WHERE_APPLICABLE, substantive_revision=2),
    _req("MR2023-A3-17442-ASSEMBLY-INSTALL", "Allegato III, punto 1.7.4.2, punto 1, lettera i)",
         "Istruzioni per montaggio, installazione e collegamento",
         "Le istruzioni per il montaggio, l'installazione e il collegamento, inclusi i disegni, i "
         "diagrammi, i sistemi di fissaggio e la designazione del telaio o dell'installazione su cui il "
         "prodotto deve essere montato.", qualifier=_Q_WHERE_APPLICABLE),
    _req("MR2023-A3-17442-NOISE-VIBRATION", "Allegato III, punto 1.7.4.2, punto 1, lettera j)",
         "Installazione e montaggio per ridurre rumore e vibrazioni",
         "Le istruzioni per l'installazione e il montaggio volte a ridurre il rumore e le vibrazioni "
         "prodotti.", qualifier=_Q_WHERE_APPLICABLE),
    _req("MR2023-A3-17442-COMMISSIONING", "Allegato III, punto 1.7.4.2, punto 1, lettera k)",
         "Messa in servizio, uso e formazione degli operatori",
         "Le istruzioni per la messa in servizio e l'uso del prodotto e, se necessario, le istruzioni "
         "per la formazione degli operatori.", qualifier=_Q_WHERE_APPLICABLE),
    _req("MR2023-A3-17442-RESIDUAL-RISKS", "Allegato III, punto 1.7.4.2, punto 1, lettera l)",
         "Rischi residui",
         "Le informazioni in merito ai rischi residui che permangono malgrado le misure di protezione "
         "integrate nella progettazione e malgrado le protezioni e le misure di protezione "
         "complementari adottate.", qualifier=_Q_WHERE_APPLICABLE),
    _req("MR2023-A3-17442-PPE", "Allegato III, punto 1.7.4.2, punto 1, lettera m)",
         "Misure di protezione a carico dell'utilizzatore e DPI",
         "Le istruzioni sulle misure di protezione che devono essere adottate dall'utilizzatore, incluse, "
         "se del caso, le attrezzature di protezione individuale che devono essere fornite.",
         qualifier=_Q_WHERE_APPLICABLE),
    _req("MR2023-A3-17442-TOOLS", "Allegato III, punto 1.7.4.2, punto 1, lettera n)",
         "Caratteristiche essenziali degli utensili",
         "Le caratteristiche essenziali degli utensili che possono essere montati sulla macchina o sul "
         "prodotto correlato.", qualifier=_Q_WHERE_APPLICABLE),
    _req("MR2023-A3-17442-STABILITY", "Allegato III, punto 1.7.4.2, punto 1, lettera o)",
         "Condizioni di stabilità",
         "Le condizioni in cui il prodotto soddisfa i requisiti di stabilità durante l'utilizzo, il "
         "trasporto, il montaggio, lo smontaggio, in condizioni di fuori servizio, durante le prove o le "
         "avarie prevedibili.", qualifier=_Q_WHERE_APPLICABLE, substantive_revision=2),
    _req("MR2023-A3-17442-TRANSPORT-STORAGE", "Allegato III, punto 1.7.4.2, punto 1, lettera p)",
         "Trasporto, movimentazione e stoccaggio in condizioni di sicurezza",
         "Le istruzioni per effettuare in condizioni di sicurezza le operazioni di trasporto, "
         "movimentazione e stoccaggio, indicanti la massa del prodotto nonché dei suoi vari elementi "
         "allorché devono essere regolarmente trasportati separatamente.",
         qualifier=_Q_WHERE_APPLICABLE),
    _req("MR2023-A3-17442-ACCIDENT-BREAKDOWN", "Allegato III, punto 1.7.4.2, punto 1, lettera q)",
         "Protocollo operativo in caso di infortunio o avaria",
         "Il protocollo operativo da rispettare in caso di infortunio o avaria e, se si può verificare "
         "un blocco, il metodo operativo per sbloccare la macchina in condizioni di sicurezza.",
         qualifier=_Q_WHERE_APPLICABLE),
    _req("MR2023-A3-17442-USER-MAINTENANCE", "Allegato III, punto 1.7.4.2, punto 1, lettera r)",
         "Regolazione e manutenzione a carico dell'utilizzatore",
         "La descrizione delle operazioni di regolazione e manutenzione che devono essere effettuate "
         "dall'utilizzatore nonché le misure di manutenzione preventiva da rispettare, tenendo conto "
         "della progettazione e dell'utilizzo del prodotto.", qualifier=_Q_WHERE_APPLICABLE,
         substantive_revision=2),
    _req("MR2023-A3-17442-SAFE-MAINTENANCE", "Allegato III, punto 1.7.4.2, punto 1, lettera s)",
         "Regolazione e manutenzione in condizioni di sicurezza",
         "Le istruzioni per effettuare in condizioni di sicurezza la regolazione e la manutenzione, "
         "incluse le misure di protezione che dovrebbero essere adottate durante tali operazioni.",
         qualifier=_Q_WHERE_APPLICABLE),
    _req("MR2023-A3-17442-SPARE-PARTS", "Allegato III, punto 1.7.4.2, punto 1, lettera t)",
         "Specifiche dei pezzi di ricambio",
         "Le specifiche dei pezzi di ricambio da utilizzare, se incidono sulla salute e la sicurezza "
         "degli operatori.", qualifier=_Q_WHERE_APPLICABLE),
    _req("MR2023-A3-17442-NOISE-EMISSIONS", "Allegato III, punto 1.7.4.2, punto 1, lettera u)",
         "Informazioni sull'emissione di rumore aereo",
         "Il livello di pressione acustica dell'emissione ponderato A nelle postazioni di lavoro se "
         "supera 70 dB(A) (se non lo supera, va indicato), il valore massimo della pressione acustica "
         "istantanea ponderata C se supera 63 Pa (130 dB rispetto a 20 microPa) e il livello di potenza "
         "acustica ponderato A se la pressione ponderata A supera 80 dB(A), con le incertezze relative "
         "e le condizioni di funzionamento e di misurazione.", qualifier=_Q_WHERE_APPLICABLE,
         substantive_revision=2),
    _req("MR2023-A3-17442-RESCUE", "Allegato III, punto 1.7.4.2, punto 1, lettera v)",
         "Precauzioni e mezzi per il soccorso delle persone",
         "Le informazioni sulle precauzioni, sui dispositivi e sui mezzi necessari per soccorrere "
         "immediatamente e con cautela le persone.", qualifier=_Q_WHERE_APPLICABLE),
    _req("MR2023-A3-17442-RADIATION", "Allegato III, punto 1.7.4.2, punto 1, lettera w)",
         "Informazioni sulle radiazioni non ionizzanti",
         "Se il prodotto può emettere radiazioni non ionizzanti che potrebbero nuocere alle persone, in "
         "particolare se portatrici di dispositivi medici impiantabili attivi o non attivi, le "
         "informazioni riguardanti le radiazioni emesse per l'operatore e le persone esposte.",
         qualifier=_Q_WHERE_APPLICABLE, substantive_revision=2),
    _req("MR2023-A3-17442-HAZARDOUS-SUBSTANCES", "Allegato III, punto 1.7.4.2, punto 1, lettera x)",
         "Emissioni di sostanze pericolose e dispositivi di captazione",
         "Se la progettazione consente emissioni di sostanze pericolose, le caratteristiche del "
         "dispositivo di captazione, filtrazione o scarico qualora non sia fornito con il prodotto, "
         "nonché la portata di emissione, la concentrazione attorno al prodotto o l'efficacia del "
         "dispositivo e le condizioni per preservarla nel tempo.", qualifier=_Q_WHERE_APPLICABLE),

    # ---- Quasi-macchine: sottoinsieme separato e opt-in ----
    _req("MR2023-A11-ASSEMBLY-INSTRUCTIONS", "Articolo 11, paragrafo 7",
         "Istruzioni per l'assemblaggio delle quasi-macchine",
         "La quasi-macchina deve essere accompagnata dalle istruzioni per l'assemblaggio di cui "
         "all'allegato XI.", group=GROUP_PARTLY_COMPLETED),
    _req("MR2023-A11-ASSEMBLY-DIGITAL", "Articolo 11, paragrafo 7, lettere a), b) e c)",
         "Condizioni per le istruzioni per l'assemblaggio in formato digitale",
         "Se fornite in formato digitale: indicare le modalità di accesso, consentirne stampa, download "
         "e salvataggio anche in caso di avaria, e renderle accessibili online per un periodo di almeno "
         "10 anni dopo l'immissione sul mercato. Su richiesta della persona incaricata "
         "dell'incorporazione al momento dell'acquisto, vanno fornite gratuitamente in formato cartaceo "
         "entro un mese.", group=GROUP_PARTLY_COMPLETED, qualifier=_Q_DIGITAL),
    _req("MR2023-A11-ASSEMBLY-LANGUAGE", "Articolo 11, paragrafo 7",
         "Lingua delle istruzioni per l'assemblaggio",
         "Le istruzioni per l'assemblaggio devono essere redatte in una lingua facilmente comprensibile "
         "dalla persona incaricata dell'incorporazione della quasi-macchina, secondo quanto stabilito "
         "dallo Stato membro interessato, e devono essere chiare, comprensibili e leggibili.",
         group=GROUP_PARTLY_COMPLETED),
    _req("MR2023-A11-DOI-LINK", "Articolo 11, paragrafo 8",
         "Dichiarazione di incorporazione UE o riferimento di accesso",
         "La quasi-macchina deve essere accompagnata dalla dichiarazione di incorporazione UE di cui "
         "all'allegato V, parte B, oppure, in alternativa, dall'indirizzo internet o dal codice ottico "
         "indicato nelle istruzioni per l'assemblaggio; le dichiarazioni digitali sono rese accessibili "
         "online per un periodo di almeno 10 anni dopo l'immissione sul mercato.",
         group=GROUP_PARTLY_COMPLETED),
    _req("MR2023-AXI-ASSEMBLY-CONTENT", "Allegato XI, punto 1",
         "Condizioni per una corretta incorporazione",
         "Le istruzioni per l'assemblaggio devono descrivere le condizioni da soddisfare per assicurare "
         "che la quasi-macchina sia incorporata correttamente e che l'insieme risultante non "
         "comprometta la salute e la sicurezza delle persone e, ove opportuno, degli animali domestici, "
         "nonché la tutela dei beni e, se del caso, dell'ambiente.",
         group=GROUP_PARTLY_COMPLETED, substantive_revision=2),
    _req("MR2023-AXI-CONTENT-LIST", "Allegato XI, punto 2",
         "Contenuto delle istruzioni per l'assemblaggio",
         "Le istruzioni per l'assemblaggio devono contenere le informazioni pertinenti da utilizzare "
         "nelle istruzioni della macchina in cui la quasi-macchina sarà incorporata e, se del caso, "
         "almeno: descrizione generale; disegni, diagrammi, descrizioni e spiegazioni; avvertenze sugli "
         "usi da evitare; istruzioni per montaggio, installazione e collegamento; informazioni su rumore "
         "o vibrazioni riducibili con l'incorporazione; requisiti essenziali dell'allegato III "
         "applicabili; caratteristiche degli utensili; condizioni di stabilità; trasporto, "
         "movimentazione e stoccaggio in sicurezza; protocollo in caso di infortunio o avaria; "
         "regolazione e manutenzione a carico dell'utilizzatore e loro esecuzione sicura; specifiche dei "
         "pezzi di ricambio; descrizione chiara della versione delle istruzioni corrispondente al "
         "modello di quasi-macchina.",
         group=GROUP_PARTLY_COMPLETED, qualifier=_Q_WHERE_APPLICABLE, substantive_revision=2),
    # Annex XI point 3 is NOT a duplicate of Article 11(8). Article 11(8)
    # is about what must ACCOMPANY the partly completed machinery; this
    # is about what the assembly instructions must themselves CONTAIN.
    # A manufacturer can satisfy one and miss the other.
    _req("MR2023-AXI-3-DOI-IN-INSTRUCTIONS", "Allegato XI, punto 3",
         "Dichiarazione di incorporazione UE nelle istruzioni per l'assemblaggio",
         "Le istruzioni per l'assemblaggio devono contenere la dichiarazione di incorporazione UE "
         "oppure l'indirizzo internet o il codice ottico dove è possibile accedere a tale "
         "dichiarazione.", group=GROUP_PARTLY_COMPLETED),
]


# ---------------------------------------------------------------------
# Reference standard metadata -- METADATA ONLY, never the standard text
# ---------------------------------------------------------------------

_MACHINERY_STANDARDS: list[dict[str, Any]] = [
    {
        "standard_id": "STD-IEC-IEEE-82079-1-2019",
        "designation": "IEC/IEEE 82079-1:2019",
        "title": ("Preparation of information for use (instructions for use) of products — "
                  "Part 1: Principles and general requirements"),
        "edition": "2019",
        "publisher": "IEC / IEEE",
        "external_reference_url": _IEC_82079,
        "provenance_type": PROVENANCE_REFERENCE_STANDARD,
        # Deliberately EMPTY: a customer who owns a licence can point this
        # at their own internal mapping. ERYLOS never bundles the text.
        "licensed_content_ref": "",
        "relationship": "optional_guidance",
        "note": ("Metadati di riferimento. ERYLOS non include il testo della norma, che è "
                 "protetto da copyright e va acquistato dall'editore. La norma è indicata come "
                 "guida opzionale per la redazione delle istruzioni: non è di per sé obbligatoria "
                 "per legge e questo pacchetto non attribuisce alcuna presunzione di conformità."),
    },
]


MACHINERY_DOC_PACK: dict[str, Any] = {
    "pack_id": "PACK-EU-MACHINERY-2023-1230-DOC",
    "pack_version": "1.1.0",
    "content_version": 4,
    "name": "Regolamento Macchine UE 2023/1230 — Documentazione tecnica",
    "summary": ("Obblighi documentali per le istruzioni per l'uso e il collegamento alla "
                "dichiarazione di conformità UE."),
    "instrument": {
        "instrument_id": "INSTR-EU-2023-1230",
        "kind": "regulation",
        "official_identifier": "Regulation (EU) 2023/1230",
        "title": "Regolamento (UE) 2023/1230 sulle macchine",
        "official_source_ref": _EURLEX_EN,
        "source_url_it": _EURLEX_IT,
        # Article 54: the Regulation applies from 20 January 2027 IN
        # GENERAL. It also sets four earlier/later dates for specific
        # articles, so the product states the general date as the general
        # date and never as "everything starts here".
        "applies_from": "2027-01-20",
        "applies_from_note": ("Data di applicazione generale (articolo 54). Lo stesso articolo prevede "
                              "date diverse per alcune disposizioni specifiche del regolamento."),
        "application_exceptions": [
            {"provisions": "Articoli da 26 a 42", "applies_from": "2024-01-20"},
            {"provisions": "Articolo 50(1)", "applies_from": "2026-10-20"},
            {"provisions": "Articolo 6(7), articoli 48 e 52", "applies_from": "2023-07-19"},
            {"provisions": "Articolo 6(2)-(6), (8) e (11), articoli 47 e 53(3)",
             "applies_from": "2024-07-20"},
        ],
    },
    # Provenance of the text this pack was verified against.
    "source_version": "02023R1230 — IT — 27.07.2026 — 002.001",
    "source_version_crosscheck": "02023R1230 — EN — 27.07.2026 — 002.001",
    "source_consolidated_date": "2026-07-27",
    "source_celex": "02023R1230-20260727",
    "source_retrieved_from": "publications.europa.eu/resource/celex/02023R1230-20260727",
    "verified_date": "2026-09-10",
    "reviewed_date": "2026-09-10",
    "source_lang": "it",
    "requirements": _MACHINERY_REQUIREMENTS,
    "standards": _MACHINERY_STANDARDS,
    "disclaimer": PACK_DISCLAIMER,
}

STARTER_PACKS: dict[str, dict[str, Any]] = {MACHINERY_DOC_PACK["pack_id"]: MACHINERY_DOC_PACK}


def available_packs() -> list[dict[str, Any]]:
    return [pack_preview(p) for p in STARTER_PACKS.values()]


def get_pack(pack_id: str) -> dict[str, Any]:
    pack = STARTER_PACKS.get(str(pack_id or "").strip())
    if not pack:
        raise KeyError(pack_id)
    return pack


def pack_preview(pack: dict[str, Any]) -> dict[str, Any]:
    """Everything a user must see BEFORE importing: where it comes from,
    which version, when it was reviewed, when the law applies, how big it
    is, and the explicit statement that this is a documentation
    traceability aid rather than automatic certification."""
    groups: dict[str, int] = {}
    unverified = 0
    for r in pack["requirements"]:
        groups[r["applicability_group"]] = groups.get(r["applicability_group"], 0) + 1
        if r["verification_status"] != VERIFIED_OFFICIAL:
            unverified += 1
    return {
        "pack_id": pack["pack_id"],
        "pack_version": pack["pack_version"],
        "content_version": pack["content_version"],
        "name": pack["name"],
        "summary": pack["summary"],
        "official_identifier": pack["instrument"]["official_identifier"],
        "source_url": pack["instrument"]["source_url_it"],
        "source_url_official": pack["instrument"]["official_source_ref"],
        "reviewed_date": pack["reviewed_date"],
        "verified_date": pack.get("verified_date", ""),
        "source_version": pack.get("source_version", ""),
        "source_consolidated_date": pack.get("source_consolidated_date", ""),
        "applies_from": pack["instrument"]["applies_from"],
        "applies_from_note": pack["instrument"].get("applies_from_note", ""),
        "application_exceptions": pack["instrument"].get("application_exceptions", []),
        "requirement_count": len(pack["requirements"]),
        "groups": {APPLICABILITY_GROUPS.get(k, k): v for k, v in sorted(groups.items())},
        "standards": [{"designation": s["designation"], "publisher": s["publisher"],
                       "url": s["external_reference_url"], "relationship": s["relationship"]}
                      for s in pack["standards"]],
        "unverified_requirements": unverified,
        "fully_verified": unverified == 0,
        "disclaimer": pack["disclaimer"],
        "verification_notice": "" if unverified == 0 else UNVERIFIED_NOTICE,
    }


def requirements_for_groups(pack: dict[str, Any], groups: list[str] | None = None) -> list[dict[str, Any]]:
    """Requirements limited to the chosen applicability subscopes. Default
    is full machinery only: partly completed machinery is a legally
    different situation and must be an explicit choice, never silently
    mixed into a full-machinery checklist."""
    wanted = set(groups or [GROUP_FULL_MACHINERY])
    return [r for r in pack["requirements"] if r["applicability_group"] in wanted]


def installed_groups(installed: list[dict[str, Any]]) -> list[str]:
    """Which applicability subscopes a project actually imported.

    Derived from the stored rows rather than stored separately: a project
    that imported only full machinery must never be told that seven
    quasi-macchina obligations are "new", because it never opted into
    that subscope in the first place."""
    return sorted({str(r.get("applicability_group") or "") for r in installed
                   if r.get("applicability_group")}) or [GROUP_FULL_MACHINERY]


def pack_diff(pack: dict[str, Any], installed: list[dict[str, Any]],
              groups: list[str] | None = None) -> dict[str, Any]:
    """Local (never remote) comparison between a pack definition and what
    a project already has installed from that pack.

    `installed` is a list of stored requirement rows carrying at least
    requirement_id / title / summary-or-description / article_ref /
    pack_version. Answers the four questions a pack upgrade must answer
    before touching anything: what would be ADDED, what CHANGED, what was
    RETIRED, and what is UNAFFECTED -- so an upgrade is presented to a
    human instead of silently rewriting their project.

    `groups` limits the comparison to the subscopes the project opted
    into; it defaults to the subscopes the installed rows already belong
    to.

    CHANGED is split in two, because the two demand opposite treatment:

    * `changed_substantive` -- the obligation now requires documenting
      something it did not name before. Applying the update bumps the
      stored requirement version, so any human decision pinned to the old
      version reads as "Da verificare" again.
    * `changed_editorial` -- the same obligation, corrected wording (the
      Italian verification pass is almost entirely this). Applying the
      update refreshes the text and leaves every decision untouched.

    Getting that split wrong in either direction is a real failure: churn
    every decision on a typo fix, or silently keep "Coperto" against a
    duty that grew."""
    wanted = set(groups) if groups else set(installed_groups(installed))
    by_id = {r["requirement_id"]: r for r in pack["requirements"]
             if r["applicability_group"] in wanted}
    installed_by_id = {r["requirement_id"]: r for r in installed}
    added = sorted(set(by_id) - set(installed_by_id))
    retired = sorted(set(installed_by_id) - set(by_id))
    changed_substantive, changed_editorial, unaffected = [], [], []
    for rid in sorted(set(by_id) & set(installed_by_id)):
        src, cur = by_id[rid], installed_by_id[rid]
        cur_summary = cur.get("summary", cur.get("description", ""))
        if (src["title"] != cur.get("title") or src["summary"] != cur_summary
                or src["article_ref"] != cur.get("article_ref")):
            # A stored row from before this column existed reads as 1, so
            # an older project is compared on the same footing.
            if int(src.get("substantive_revision", 1)) > int(cur.get("substantive_revision") or 1):
                changed_substantive.append(rid)
            else:
                changed_editorial.append(rid)
        else:
            unaffected.append(rid)
    changed = sorted(changed_substantive + changed_editorial)
    return {"pack_id": pack["pack_id"], "pack_version": pack["pack_version"],
            "content_version": pack.get("content_version", 0),
            "groups": sorted(wanted),
            "added": added, "changed": changed,
            "changed_substantive": changed_substantive,
            "changed_editorial": changed_editorial,
            "retired": retired, "unaffected": unaffected}
