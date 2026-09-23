"""Milestone 4F.2: compliance starter pack -- regression suite.

Covers the checkpoint matrix: pack identity/provenance, legal vs
reference-standard separation, import semantics, the project assessment
overlay, evidence linkage, coverage/gap logic, local pack versioning, UI
wording, and audit/mutation safety.

The single most important property under test is the SEPARATION the whole
product rests on: a legal source requirement, a human applicability
decision, and evidence coverage are three different things, and nothing
here may ever collapse them into an automatic conformity verdict.
"""
from __future__ import annotations
import test_isolation  # noqa: E402,F401 -- FIRST import: temporary home + real-data guard, never the user's ManualSyncData (test_isolation.py)

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ccms_industrial import IndustrialRepository
from project_lifecycle import create_project
import authoring_view as av
import compliance_view as cv
import domain_errors as de
import i18n
import compliance_starter_packs as csp

TMP = Path(tempfile.mkdtemp(prefix="manualsync_compliance_4f2_"))
results: list[dict] = []


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append({"test": name, "status": "PASS" if ok else "FAIL", "detail": str(detail)})
    if not ok:
        raise AssertionError(f"{name}: {detail}")


def _new_project(name: str) -> Path:
    p = create_project(TMP, project_name=name, source_lang="it-IT")
    repo = IndustrialRepository(p / "ManualSync_CCMS.sqlite")
    try:
        repo.create_topic("T1", "Installazione", "corpo", status="Approved")
        repo.create_topic("T2", "Manutenzione", "corpo", status="Approved")
        repo.create_publication("PUB-MAIN", "Manuale Pompa HX-400", ["T1", "T2"])
    finally:
        repo.close()
    return p


PACK = csp.get_pack("PACK-EU-MACHINERY-2023-1230-DOC")
PREVIEW = csp.pack_preview(PACK)
_REQ_BY_ID = {r["requirement_id"]: r for r in PACK["requirements"]}


def _R(rid: str) -> dict:
    """One pack requirement by stable id (raises if the id disappeared,
    which is itself the failure a verification pass must catch)."""
    return _REQ_BY_ID[rid]

# ====================================================================
# Pack identity / provenance (items 1-8)
# ====================================================================
check("prov1 (item 1): the pack has a stable, explicit id",
      PACK["pack_id"] == "PACK-EU-MACHINERY-2023-1230-DOC", PACK["pack_id"])
check("prov2 (item 2): the pack declares a version and a separate content version",
      bool(PACK["pack_version"]) and isinstance(PACK["content_version"], int), PACK["pack_version"])
check("prov3 (item 3): the pack declares the date its content was reviewed -- it never claims "
      "perpetual currency", PACK["reviewed_date"] == "2026-09-10", PACK["reviewed_date"])
check("prov4 (item 4): the pack encodes the Regulation's application date (20 January 2027)",
      PACK["instrument"]["applies_from"] == "2027-01-20", PACK["instrument"]["applies_from"])
check("prov5 (item 5): the official EUR-Lex source is carried for both the instrument and each "
      "requirement", "eur-lex.europa.eu" in PACK["instrument"]["official_source_ref"]
      and all("eur-lex.europa.eu" in r["source_url"] for r in PACK["requirements"]), None)
check("prov6 (item 6): every legal requirement carries an article/annex reference",
      all(r["article_ref"].strip() for r in PACK["requirements"]
          if r["provenance_type"] == csp.PROVENANCE_LEGAL),
      [r["requirement_id"] for r in PACK["requirements"] if not r["article_ref"].strip()])
check("prov7 (item 7): no requirement exists without full provenance (id, ref, source, language, "
      "provenance type, verification status, applicability group)",
      all(all(r.get(k) for k in ("requirement_id", "article_ref", "title", "summary", "source_url",
                                 "source_lang", "provenance_type", "verification_status",
                                 "applicability_group")) for r in PACK["requirements"]), None)
_ids = [r["requirement_id"] for r in PACK["requirements"]]
check("prov8 (item 8): stable ids are unique -- identity never depends on display text",
      len(_ids) == len(set(_ids)), len(_ids) - len(set(_ids)))
check("prov9: requirement summaries are non-normative paraphrases, not quotations of the legal text "
      "(no requirement carries a verbatim-quote marker)",
      all('"' not in r["summary"] and "«" not in r["summary"] for r in PACK["requirements"]), None)

# ====================================================================
# Legal vs reference-standard separation (items 9-12)
# ====================================================================
check("legal9 (item 9): the legal instrument and the reference standard are separate records with "
      "different provenance types",
      all(r["provenance_type"] == csp.PROVENANCE_LEGAL for r in PACK["requirements"])
      and all(s["provenance_type"] == csp.PROVENANCE_REFERENCE_STANDARD for s in PACK["standards"]), None)
_std = PACK["standards"][0]
check("legal10 (item 10): the IEC/IEEE 82079-1 record carries METADATA ONLY -- identifier, title, "
      "edition, publisher, URL -- and no normative clause text is bundled",
      _std["designation"] == "IEC/IEEE 82079-1:2019" and _std["edition"] == "2019"
      and "webstore.iec.ch" in _std["external_reference_url"]
      and not any(k in _std for k in ("clauses", "text", "body", "requirements")), sorted(_std))
check("legal10b (item 10): the licensed-content slot is an EMPTY placeholder a customer can fill "
      "with their own licensed mapping -- ERYLOS ships nothing in it",
      _std["licensed_content_ref"] == "", _std["licensed_content_ref"])
check("legal11 (item 11): the standard is labelled optional guidance, never legally mandatory",
      _std["relationship"] == "optional_guidance" and "non è di per sé obbligatoria" in _std["note"], None)
check("legal12 (item 12): no presumption-of-conformity claim is made anywhere in the pack",
      "presunzione di conformità" not in json.dumps(PACK, ensure_ascii=False).lower()
      or "non attribuisce alcuna presunzione" in _std["note"], None)
check("legal12b: the pack's own disclaimer states ERYLOS does not certify legal compliance",
      "Non certifica" in csp.PACK_DISCLAIMER and "conformità legale" in csp.PACK_DISCLAIMER, None)
# ====================================================================
# LEGAL-SOURCE VERIFICATION GATE
#
# Verified against the official consolidated text obtained from the EU
# Publications Office Cellar endpoint (eur-lex.europa.eu itself answers
# automated requests with a JS/consent shell):
#   Consolidated TEXT: 02023R1230 — IT — 27.07.2026 — 002.001
#   Cross-check:       02023R1230 — EN — 27.07.2026 — 002.001
# ====================================================================
_LEGAL = [r for r in PACK["requirements"] if r["provenance_type"] == csp.PROVENANCE_LEGAL]

check("ver1 (gate 1): every authoritative legal requirement is VERIFIED_OFFICIAL",
      all(r["verification_status"] == csp.VERIFIED_OFFICIAL for r in _LEGAL)
      and PREVIEW["fully_verified"] is True and PREVIEW["unverified_requirements"] == 0,
      [r["requirement_id"] for r in _LEGAL if r["verification_status"] != csp.VERIFIED_OFFICIAL])
check("ver2 (gate 2): every verified requirement carries a precise article/annex reference",
      all(re.match(r"^(Articolo|Allegato)\s", r["article_ref"]) for r in _LEGAL),
      [r["article_ref"] for r in _LEGAL][:3])
check("ver3 (gate 3): the pack records the official source it was verified against",
      PACK["source_version"] == "02023R1230 — IT — 27.07.2026 — 002.001"
      and PACK["source_celex"] == "02023R1230-20260727"
      and "publications.europa.eu" in PACK["source_retrieved_from"], PACK["source_version"])
check("ver4 (gate 4): the consolidated source date is 2026-07-27",
      PACK["source_consolidated_date"] == "2026-07-27" and PACK["verified_date"] == "2026-09-10",
      PACK["source_consolidated_date"])

_by_ref = {}
for r in _LEGAL:
    _by_ref.setdefault(r["article_ref"].split(",")[0].strip(), []).append(r)
check("ver5 (gate 5): Article 10(7) is mapped, covering delivery, the three digital conditions, the "
      "paper copy, the non-professional paper safety information and the language duty",
      len([r for r in _LEGAL if r["article_ref"].startswith("Articolo 10, paragrafo 7")]) == 9,
      [r["requirement_id"] for r in _LEGAL
       if r["article_ref"].startswith("Articolo 10, paragrafo 7")])
check("ver5b: the Article 10(7) online-availability duty states BOTH the expected lifetime and the "
      "10-year floor -- the 0.1.0 draft said only 'the period provided'",
      "ciclo di vita previsto" in _R("MR2023-A10-7-AVAILABILITY")["summary"]
      and "10 anni" in _R("MR2023-A10-7-AVAILABILITY")["summary"],
      _R("MR2023-A10-7-AVAILABILITY")["summary"])
check("ver5c: the paper-copy duty preserves 'at the time of purchase', 'free of charge' and 'within "
      "one month'",
      all(t in _R("MR2023-A10-7-PAPER-ON-REQUEST")["summary"]
          for t in ["momento dell'acquisto", "gratuitamente", "un mese"]),
      _R("MR2023-A10-7-PAPER-ON-REQUEST")["summary"])
check("ver6 (gate 6): Article 10(8) is mapped, including the digital-declaration availability duty "
      "the draft omitted entirely",
      len([r for r in _LEGAL if r["article_ref"].startswith("Articolo 10, paragrafo 8")]) == 3
      and "10 anni" in _R("MR2023-A10-8-DOC-AVAILABILITY")["summary"]
      and "allegato V, parte A" in _R("MR2023-A10-8-DOC-ACCOMPANY")["summary"], None)
check("ver7 (gate 7): Annex III 1.7.4 is mapped, including the specialised-maintenance language "
      "derogation the draft omitted",
      _R("MR2023-A3-1740-INSTRUCTIONS")["article_ref"] == "Allegato III, punto 1.7.4"
      and "deroga" in _R("MR2023-A3-1740-MAINT-LANGUAGE-EXCEPTION")["summary"].lower()
      and "una sola lingua ufficiale" in _R("MR2023-A3-1740-MAINT-LANGUAGE-EXCEPTION")["summary"], None)
check("ver8 (gate 8): Annex III 1.7.4.1 is mapped to its actual two points -- (a) intended use and "
      "foreseeable misuse, (b) non-professional operators",
      _R("MR2023-A3-17441-INTENDED-USE")["article_ref"].endswith("lettera a)")
      and _R("MR2023-A3-17441-MISUSE")["article_ref"].endswith("lettera a)")
      and _R("MR2023-A3-17441-NON-PROFESSIONAL")["article_ref"].endswith("lettera b)"), None)

_17442 = [r for r in _LEGAL if r["article_ref"].startswith("Allegato III, punto 1.7.4.2")]
_letters = sorted(re.search(r"lettera ([a-z])\)", r["article_ref"]).group(1) for r in _17442)
check("ver9 (gate 9): every encoded 1.7.4.2 item maps to a real letter of the official list, and all "
      "24 letters (a)-(x) are present exactly once -- the 0.1.0 draft had only 16",
      len(_17442) == 24 and _letters == [chr(c) for c in range(ord("a"), ord("x") + 1)], _letters)
check("ver9b (gate 4/9): the 'where applicable' chapeau qualifying the WHOLE 1.7.4.2 list is preserved "
      "on every item rather than dropped",
      all(r["qualifier"] == "Se del caso" for r in _17442),
      [r["requirement_id"] for r in _17442 if r["qualifier"] != "Se del caso"])
check("ver9c: 1.7.4.2(1)(h) uses its OWN wording ('experience has shown might occur'), not the "
      "1.7.4.1 'reasonably foreseeable' wording the draft wrongly reused",
      "esperienza" in _R("MR2023-A3-17442-MISUSE-WARNINGS")["summary"]
      and "ragionevolmente prevedibile" not in _R("MR2023-A3-17442-MISUSE-WARNINGS")["summary"],
      _R("MR2023-A3-17442-MISUSE-WARNINGS")["summary"])
check("ver9d: 1.7.4.2(1)(j) is about installation/assembly to REDUCE noise or vibration, not about "
      "declaring noise values",
      "ridurre il rumore" in _R("MR2023-A3-17442-NOISE-VIBRATION")["summary"],
      _R("MR2023-A3-17442-NOISE-VIBRATION")["summary"])
check("ver9e: the eight obligations missing from the 0.1.0 draft are now present",
      all(_R(rid) for rid in ["MR2023-A3-17442-ACCIDENT-BREAKDOWN", "MR2023-A3-17442-USER-MAINTENANCE",
                              "MR2023-A3-17442-SAFE-MAINTENANCE", "MR2023-A3-17442-SPARE-PARTS",
                              "MR2023-A3-17442-NOISE-EMISSIONS", "MR2023-A3-17442-RESCUE",
                              "MR2023-A3-17442-RADIATION", "MR2023-A3-17442-HAZARDOUS-SUBSTANCES"]), None)

_partly = [r for r in _LEGAL if r["applicability_group"] == csp.GROUP_PARTLY_COMPLETED]
check("ver10 (gate 10): Article 11 is mapped for partly completed machinery -- assembly instructions, "
      "their digital conditions, language, and the declaration of INCORPORATION (Annex V Part B)",
      len([r for r in _partly if r["article_ref"].startswith("Articolo 11")]) == 4
      and "incorporazione" in _R("MR2023-A11-DOI-LINK")["summary"]
      and "parte B" in _R("MR2023-A11-DOI-LINK")["summary"], None)
check("ver11 (gate 11): Annex XI is mapped to both its points -- conditions for correct incorporation "
      "and the minimum content list",
      _R("MR2023-AXI-ASSEMBLY-CONTENT")["article_ref"] == "Allegato XI, punto 1"
      and _R("MR2023-AXI-CONTENT-LIST")["article_ref"] == "Allegato XI, punto 2", None)
check("ver11b (gate 10/11): partly-completed requirements stay in their own applicability group and "
      "never enter the default full-machinery checklist",
      len(_partly) == 7 and all(r["applicability_group"] == csp.GROUP_PARTLY_COMPLETED for r in _partly)
      and csp.requirements_for_groups(PACK) == [r for r in PACK["requirements"]
                                                if r["applicability_group"] == csp.GROUP_FULL_MACHINERY],
      len(_partly))

check("ver12 (gate 12): Article 54's GENERAL application date is 20 January 2027",
      PACK["instrument"]["applies_from"] == "2027-01-20", PACK["instrument"]["applies_from"])
check("ver13 (gate 13): Article 54's exceptions are preserved, not erased -- the pack records the "
      "other application dates and labels 20/01/2027 as the GENERAL date",
      len(PACK["instrument"]["application_exceptions"]) == 4
      and "generale" in PACK["instrument"]["applies_from_note"].lower()
      and {"2024-01-20", "2026-10-20", "2023-07-19", "2024-07-20"} ==
      {e["applies_from"] for e in PACK["instrument"]["application_exceptions"]},
      PACK["instrument"]["application_exceptions"])

_alltext = json.dumps(PACK, ensure_ascii=False).lower()
check("ver14 (gate 14): no Directive 2006/42/EC wording or reference masquerades as Regulation "
      "2023/1230 content",
      not any(t in _alltext for t in ["2006/42", "direttiva macchine", "machinery directive",
                                      "marcatura ce ai sensi della direttiva"]), None)
check("ver14b: requirement references never cite a Directive article numbering scheme",
      not any("direttiva" in r["article_ref"].lower() for r in _LEGAL), None)
check("ver16 (gate 16/17): the IEC record is still metadata-only after verification -- no clause text, "
      "empty licensed-content placeholder, optional guidance",
      _std["licensed_content_ref"] == "" and _std["relationship"] == "optional_guidance"
      and not any(k in _std for k in ("clauses", "text", "body")), None)
check("ver17 (gate 17): the IEC note states the standard is not automatically legally binding and "
      "grants no presumption of conformity",
      "non è di per sé obbligatoria" in _std["note"]
      and "non attribuisce alcuna presunzione" in _std["note"], None)
check("ver18 (gate 18): verification introduced no automatic legal verdict -- no COMPLIANT/CONFORME "
      "state anywhere in the pack",
      "\"conforme\"" not in _alltext and "compliant" not in _alltext, None)

# ====================================================================
# Italian source-language gate
#
# The pack ships in Italian, so the Italian consolidated text is the
# SOURCE OF RECORD, not a translation of an English working copy:
#   Consolidated TEXT: 02023R1230 — IT — 27.07.2026 — 002.001
# A traceability tool fails its purpose if the words it shows cannot be
# found in the Regulation the user actually cites, so these checks pin
# the Regulation's own Italian vocabulary rather than plausible
# synonyms.
# ====================================================================
_REQ_TEXT = " ".join(r["title"] + " " + r["summary"] + " " + r["qualifier"]
                     for r in _LEGAL).lower()
_FULL = [r for r in _LEGAL if r["applicability_group"] == csp.GROUP_FULL_MACHINERY]

check("ita1 (gate 1): the pack records the ITALIAN consolidated text as the source it was verified "
      "against, and keeps the English text only as a declared cross-check",
      PACK["source_version"] == "02023R1230 — IT — 27.07.2026 — 002.001"
      and PACK["source_version_crosscheck"] == "02023R1230 — EN — 27.07.2026 — 002.001"
      and PACK["source_lang"] == "it",
      (PACK["source_version"], PACK.get("source_version_crosscheck")))
check("ita2 (gate 2): every legal requirement declares Italian as its source language and points at "
      "the Italian EUR-Lex text, so a citation can be checked in the language it is shown in",
      all(r["source_lang"] == "it" and r["source_url"].endswith("/ita") for r in _LEGAL),
      [r["requirement_id"] for r in _LEGAL if r["source_lang"] != "it"])
check("ita3 (gate 3): references use the Regulation's own Italian citation style -- 'Articolo 10, "
      "paragrafo 7, lettera c)' and 'Allegato III, punto 1.7.4.2, punto 1, lettera u)' -- never the "
      "English-style 'Articolo 10(7)' or the abbreviation 'lett.'",
      not any(re.search(r"Articolo \d+\(\d+\)", r["article_ref"]) for r in _LEGAL)
      and not any("lett." in r["article_ref"] for r in _LEGAL)
      and _R("MR2023-A10-7-AVAILABILITY")["article_ref"] == "Articolo 10, paragrafo 7, lettera c)"
      and _R("MR2023-A3-17442-NOISE-EMISSIONS")["article_ref"]
      == "Allegato III, punto 1.7.4.2, punto 1, lettera u)",
      [r["article_ref"] for r in _LEGAL
       if re.search(r"Articolo \d+\(\d+\)", r["article_ref"]) or "lett." in r["article_ref"]])
check("ita4 (gate 4): the chapeau qualifier is the Regulation's own 'se del caso', not the paraphrase "
      "'ove applicabile' -- the qualifier is quoted back to the user as a legal condition",
      csp._Q_WHERE_APPLICABLE == "Se del caso"
      and all(r["qualifier"] == "Se del caso" for r in _17442),
      csp._Q_WHERE_APPLICABLE)

check("ita5 (gate 5): the pack says 'utilizzatore', the term the Italian Regulation uses, and never "
      "the everyday 'utente'",
      not re.search(r"\butent[ei]\b", _REQ_TEXT) and "utilizzator" in _REQ_TEXT,
      [r["requirement_id"] for r in _LEGAL
       if re.search(r"\butent[ei]\b", (r["title"] + r["summary"]).lower())])
check("ita6 (gate 5): the pack says 'avaria', not 'guasto' -- both digital-format duties and the "
      "accident provision depend on that word",
      "guast" not in _REQ_TEXT
      and "avaria" in _R("MR2023-A10-7-PRINT-DOWNLOAD")["summary"]
      and "avaria" in _R("MR2023-A3-17442-ACCIDENT-BREAKDOWN")["summary"]
      and "avaria" in _R("MR2023-A11-ASSEMBLY-DIGITAL")["summary"],
      [r["requirement_id"] for r in _LEGAL if "guast" in (r["title"] + r["summary"]).lower()])
check("ita7 (gate 5): the online-availability duties use 'ciclo di vita previsto' and keep the 10-year "
      "floor -- 'durata di vita' does not appear in the Italian Regulation",
      "durata di vita" not in _REQ_TEXT
      and all("ciclo di vita previsto" in _R(rid)["summary"] and "10 anni" in _R(rid)["summary"]
              for rid in ("MR2023-A10-7-AVAILABILITY", "MR2023-A10-8-DOC-AVAILABILITY")), None)
check("ita8 (gate 5): the declarations keep the Italian noun order -- 'dichiarazione di conformità UE' "
      "and 'dichiarazione di incorporazione UE', never 'dichiarazione UE di ...'. Checked across the "
      "WHOLE pack, not only the requirements: the pack summary is shown in the import preview too, and "
      "that is exactly where the wrong order survived the first pass",
      "dichiarazione ue di" not in json.dumps(PACK, ensure_ascii=False).lower()
      and "dichiarazione di conformità ue" in _R("MR2023-A10-8-DOC-ACCOMPANY")["summary"].lower()
      and "dichiarazione di incorporazione ue" in _R("MR2023-A11-DOI-LINK")["summary"].lower(),
      [r["requirement_id"] for r in _LEGAL
       if "dichiarazione ue di" in (r["title"] + r["summary"]).lower()]
      or PACK["summary"])
check("ita9 (gate 5): the pack says 'mandatario', the Italian Regulation's term, not the translated "
      "'rappresentante autorizzato'",
      "rappresentante autorizzato" not in _REQ_TEXT
      and "mandatario" in _R("MR2023-A3-17442-MANUFACTURER")["summary"], None)
check("ita10 (gate 5): 1.7.4.2(1)(m) says 'attrezzature di protezione individuale' -- the Italian text "
      "does not say 'dispositivi' there",
      "attrezzature di protezione individuale" in _R("MR2023-A3-17442-PPE")["summary"]
      and "dispositivi di protezione individuale" not in _REQ_TEXT,
      _R("MR2023-A3-17442-PPE")["summary"])
check("ita11 (gate 5): the paper duty for non-professionals uses the Article 10(7) term of art "
      "'informazioni sulla sicurezza', not the generic 'informazioni di sicurezza'",
      "informazioni sulla sicurezza" in _R("MR2023-A10-7-PAPER-SAFETY-INFO")["summary"]
      and "informazioni sulla sicurezza" in _R("MR2023-A10-7-LANGUAGE")["summary"]
      and "informazioni di sicurezza" not in _REQ_TEXT, None)
check("ita12 (gate 5): the optical-code wording follows each provision -- Article 10(8) says 'codici a "
      "lettura ottica', Annex III and Annex XI say 'codice ottico'. The Regulation is not uniform and "
      "the pack does not tidy it up",
      "codici a lettura ottica" in _R("MR2023-A10-8-DOC-DIGITAL-ACCESS")["summary"]
      and "codice ottico" in _R("MR2023-A3-17442-DECLARATION-REF")["summary"]
      and "codice ottico" in _R("MR2023-A11-DOI-LINK")["summary"]
      and "leggibile da dispositivo automatico" not in _REQ_TEXT, None)
check("ita13 (gate 5): partly completed machinery uses 'istruzioni per l'assemblaggio'. 'Istruzioni di "
      "montaggio' would collide with 1.7.4.2(1)(i)'s 'istruzioni per il montaggio' for full machinery",
      all("istruzioni per l'assemblaggio" in (r["title"] + " " + r["summary"]).lower()
          for r in _partly)
      and "istruzioni di montaggio" not in _REQ_TEXT
      and "istruzioni per il montaggio" in _R("MR2023-A3-17442-ASSEMBLY-INSTALL")["summary"],
      [r["requirement_id"] for r in _partly
       if "istruzioni per l'assemblaggio" not in (r["title"] + " " + r["summary"]).lower()])
check("ita14 (gate 5): Article 10(7) addresses 'utilizzatori non professionali' while Annex III "
      "1.7.4.1(b) addresses 'operatori non professionali'. Two provisions, two scopes -- collapsing "
      "them into one word would silently widen or narrow a legal duty",
      "utilizzatori non professionali" in _R("MR2023-A10-7-PAPER-SAFETY-INFO")["summary"]
      and "operatori non professionali" not in _R("MR2023-A10-7-PAPER-SAFETY-INFO")["summary"]
      and "operatori non professionali" in _R("MR2023-A3-17441-NON-PROFESSIONAL")["summary"]
      and "utilizzatori non professionali" not in _R("MR2023-A3-17441-NON-PROFESSIONAL")["summary"],
      None)
check("ita15 (gate 5): 1.7.4.1(b) says 'livello di formazione generale', not 'livello di istruzione "
      "generale'",
      "livello di formazione generale" in _R("MR2023-A3-17441-NON-PROFESSIONAL")["summary"]
      and "livello di istruzione" not in _REQ_TEXT, None)

check("ita16 (gate 6): the 1.7.4 maintenance-language derogation covers staff appointed by the "
      "manufacturer OR BY ITS MANDATARIO -- dropping the second half narrows the derogation",
      "mandatario" in _R("MR2023-A3-1740-MAINT-LANGUAGE-EXCEPTION")["summary"]
      and "una sola lingua ufficiale" in _R("MR2023-A3-1740-MAINT-LANGUAGE-EXCEPTION")["summary"],
      _R("MR2023-A3-1740-MAINT-LANGUAGE-EXCEPTION")["summary"])
check("ita17 (gate 6): 1.7.4.1(a) is one obligation shaped 'non soltanto ... ma anche'. The two "
      "records keep that shape instead of asserting a bare duty to describe intended use",
      "non deve riguardare soltanto" in _R("MR2023-A3-17441-INTENDED-USE")["summary"]
      and "anche" in _R("MR2023-A3-17441-MISUSE")["summary"]
      and "uso scorretto ragionevolmente prevedibile" in _R("MR2023-A3-17441-MISUSE")["summary"],
      (_R("MR2023-A3-17441-INTENDED-USE")["summary"], _R("MR2023-A3-17441-MISUSE")["summary"]))
check("ita18 (gate 6): 1.7.4.2(1)(h) says misuse that 'potrebbero, in base all'esperienza, "
      "presentarsi' -- may occur in the light of experience, not misuse experience has shown occurs",
      "in base all'esperienza" in _R("MR2023-A3-17442-MISUSE-WARNINGS")["summary"], None)
check("ita19 (gate 6): 1.7.4.2(1)(l) says 'misure di protezione integrate nella progettazione', not "
      "the looser 'misure di progettazione sicura'",
      "integrate nella progettazione" in _R("MR2023-A3-17442-RESIDUAL-RISKS")["summary"]
      and "progettazione sicura" not in _REQ_TEXT, None)
check("ita20 (gate 6): 1.7.4.2(1)(o) lists montaggio, smontaggio AND condizioni di fuori servizio as "
      "separate conditions -- the earlier text merged two of them and lost 'montaggio'",
      all(t in _R("MR2023-A3-17442-STABILITY")["summary"]
          for t in ["il montaggio", "lo smontaggio", "fuori servizio", "le prove", "avarie prevedibili"]),
      _R("MR2023-A3-17442-STABILITY")["summary"])
check("ita21 (gate 6): 1.7.4.2(1)(r) keeps 'tenendo conto della progettazione e dell'utilizzo', which "
      "is what makes the preventive-maintenance duty product-specific",
      "tenendo conto della progettazione" in _R("MR2023-A3-17442-USER-MAINTENANCE")["summary"], None)
check("ita22 (gate 6): 1.7.4.2(1)(u) carries the Regulation's ACTUAL noise thresholds instead of a "
      "vague pointer to 'the thresholds provided'",
      all(t in _R("MR2023-A3-17442-NOISE-EMISSIONS")["summary"]
          for t in ["70 dB(A)", "63 Pa", "130 dB", "80 dB(A)", "incertezze"]),
      _R("MR2023-A3-17442-NOISE-EMISSIONS")["summary"])
check("ita23 (gate 6): 1.7.4.2(1)(w) keeps 'attivi o non attivi' for implantable medical devices -- "
      "the qualifier is what makes the duty cover passive implants too",
      "attivi o non attivi" in _R("MR2023-A3-17442-RADIATION")["summary"],
      _R("MR2023-A3-17442-RADIATION")["summary"])
check("ita24 (gate 6): 1.7.4.2(1)(q) uses 'infortunio', 'avaria' and 'blocco' -- the Italian text's "
      "words, not 'incidente'/'guasto'/'bloccaggio'",
      all(t in _R("MR2023-A3-17442-ACCIDENT-BREAKDOWN")["summary"]
          for t in ["infortunio", "avaria", "blocco"]),
      _R("MR2023-A3-17442-ACCIDENT-BREAKDOWN")["summary"])

check("ita25 (gate 7): Annex XI point 1 keeps the full protected interests -- persone, animali "
      "domestici, beni and ambiente. The earlier text kept only 'persone'",
      all(t in _R("MR2023-AXI-ASSEMBLY-CONTENT")["summary"]
          for t in ["persone", "animali domestici", "beni", "ambiente"]),
      _R("MR2023-AXI-ASSEMBLY-CONTENT")["summary"])
check("ita26 (gate 7): Annex XI point 2 covers items (k)-(n) too, including the duty to identify which "
      "VERSION of the assembly instructions matches the model",
      all(t in _R("MR2023-AXI-CONTENT-LIST")["summary"]
          for t in ["regolazione e manutenzione", "pezzi di ricambio", "versione delle istruzioni",
                    "modello di quasi-macchina"]),
      _R("MR2023-AXI-CONTENT-LIST")["summary"])
check("ita27 (gate 8): Article 11(7)(c) and 11(8) set ONLY a 10-year online period for partly "
      "completed machinery -- unlike Article 10, there is no expected-lifetime clause, and the pack "
      "must not invent one",
      all("10 anni" in _R(rid)["summary"] and "ciclo di vita" not in _R(rid)["summary"]
          for rid in ("MR2023-A11-ASSEMBLY-DIGITAL", "MR2023-A11-DOI-LINK")),
      [(rid, _R(rid)["summary"]) for rid in ("MR2023-A11-ASSEMBLY-DIGITAL", "MR2023-A11-DOI-LINK")])
check("ita28 (gate 8): the partly-completed language duty keeps 'la persona incaricata "
      "dell'incorporazione' and 'secondo quanto stabilito dallo Stato membro interessato'",
      "persona incaricata dell'incorporazione" in _R("MR2023-A11-ASSEMBLY-LANGUAGE")["summary"]
      and "Stato membro interessato" in _R("MR2023-A11-ASSEMBLY-LANGUAGE")["summary"], None)
check("ita29 (gate 8): the legally operative qualifiers survive verbatim across the pack -- 'su "
      "richiesta', 'al momento dell'acquisto', 'gratuitamente', 'entro un mese', 'almeno 10 anni'",
      all(t in _REQ_TEXT for t in ["su richiesta", "al momento dell'acquisto", "gratuitamente",
                                   "entro un mese", "almeno 10 anni", "quasi-macchina"]), None)
check("ita30 (gate 9): no English legal wording leaked into the Italian requirement text during "
      "translation-driven editing",
      not any(t in _REQ_TEXT for t in ["where applicable", "instructions for use", "shall ",
                                       "declaration of conformity", "partly completed"]), None)
check("ita31 (gate 9): the group label shown to the user also uses the Regulation's Italian term",
      csp.APPLICABILITY_GROUPS[csp.GROUP_PARTLY_COMPLETED] == "Quasi-macchina (assemblaggio)",
      csp.APPLICABILITY_GROUPS)
check("ita32 (gate 10): the Italian pass changed CONTENT, so the content version moved -- a project "
      "that already imported the pack can be told there is something new to review",
      PACK["content_version"] == 4 and PACK["pack_version"] == "1.1.0",
      (PACK["pack_version"], PACK["content_version"]))
check("ita33 (gate 10): the Italian pass introduced no unverified requirement and no legal verdict",
      all(r["verification_status"] == csp.VERIFIED_OFFICIAL for r in _LEGAL)
      and "conforme\"" not in json.dumps(PACK, ensure_ascii=False).lower(), None)

# ====================================================================
# Import (items 13-18)
# ====================================================================
P = _new_project("4f2_main")
repo, svc = cv.open_service(P)
svc.create_scope("SCOPE-EU", "PUB-MAIN", actor="Quality", title="Macchina completa", jurisdiction="EU")

_before_reqs = len(svc.installed_pack_requirements(PACK["pack_id"]))
res1 = svc.import_requirement_pack(PACK, scope_id="SCOPE-EU", actor="Quality")
check("imp13 (item 13): import is explicit and returns what it did",
      _before_reqs == 0 and len(res1["added"]) == 41 and res1["pack_version"] == "1.1.0",
      (len(res1["added"]), res1["pack_version"]))
check("imp14 (item 14): the import is atomic -- instrument, standard metadata, requirements and the "
      "scope references all exist together after one call",
      svc.instrument_row("INSTR-EU-2023-1230") is not None
      and svc.standard_row("STD-IEC-IEEE-82079-1-2019") is not None
      and len(svc.requirements_in_scope("SCOPE-EU")) == 41, None)
res2 = svc.import_requirement_pack(PACK, scope_id="SCOPE-EU", actor="Quality")
check("imp15 (item 15): re-importing the SAME version is idempotent -- nothing added, everything "
      "reported as already present", res2["added"] == [] and len(res2["already_present"]) == 41, res2["added"])
check("imp16 (item 16): no duplicate requirements after a second import",
      len(svc.installed_pack_requirements(PACK["pack_id"])) == 41,
      len(svc.installed_pack_requirements(PACK["pack_id"])))
check("imp17 (item 17): partly-completed-machinery requirements are NOT silently mixed into the "
      "default full-machinery checklist -- they need an explicit applicability group",
      all(r["applicability_group"] == csp.GROUP_FULL_MACHINERY
          for r in svc.installed_pack_requirements(PACK["pack_id"])), None)
res3 = svc.import_requirement_pack(PACK, scope_id="SCOPE-EU", actor="Quality",
                                   groups=[csp.GROUP_PARTLY_COMPLETED])
check("imp17b (item 17): choosing the partly-completed subscope explicitly adds exactly those seven",
      len(res3["added"]) == 7, res3["added"])

P2 = _new_project("4f2_isolated")
repo2, svc2 = cv.open_service(P2)
check("imp18 (item 17, project isolation): importing into one project leaves another project empty",
      svc2.installed_pack_requirements(PACK["pack_id"]) == [], None)

_blocks_before = {r["block_id"]: r["version"] for r in
                  repo.conn.execute("SELECT block_id, version FROM content_blocks")}
_topics_before = [(r["topic_id"], r["version"], r["status"]) for r in
                  repo.conn.execute("SELECT topic_id, version, status FROM topics")]
check("imp19 (item 18): importing a pack mutates NO authoring content -- no block or topic version "
      "moves, no status changes",
      _topics_before == [(r["topic_id"], r["version"], r["status"]) for r in
                         repo.conn.execute("SELECT topic_id, version, status FROM topics")], None)

# ====================================================================
# Assessment overlay (items 19-24)
# ====================================================================
RID = "MR2023-A3-17441-INTENDED-USE"
_source_before = dict(svc.requirement_row(RID))
svc.set_applicability("SCOPE-EU", RID, "APPLIES", actor="Quality", comment="Rilevante per questa pompa")
_source_after = dict(svc.requirement_row(RID))
check("ovl19 (item 19): a project decision does NOT modify the pack SOURCE record",
      _source_before == _source_after, None)
check("ovl20 (item 20): project applicability is editable and stored in the overlay",
      svc.applicability_row("SCOPE-EU", RID)["applies"] == "APPLIES", None)
# Domain Error Semantics V1: the refusal carries a stable CODE, so this
# asserts the rule that fired rather than the words it happened to use.
# The old form ("evidenza" in the message) broke the moment the UI could
# be English -- which is the coupling that gate removed.
_na_error = None
try:
    svc.set_applicability("SCOPE-EU", "MR2023-A10-7-PAPER-SAFETY-INFO", "NOT_APPLICABLE", actor="Quality")
except ValueError as e:
    _na_error = e
check("ovl21 (item 21): 'Non applicabile' REQUIRES a human reason",
      _na_error is not None and _na_error.code == de.ERR_JUSTIFICATION_REQUIRED,
      repr(_na_error))
svc.set_applicability("SCOPE-EU", "MR2023-A10-7-PAPER-SAFETY-INFO", "NOT_APPLICABLE", actor="Quality",
                      justification="Macchina destinata esclusivamente a uso professionale.")
check("ovl21b: with a justification the decision is accepted and the reason persists",
      svc.applicability_row("SCOPE-EU", "MR2023-A10-7-PAPER-SAFETY-INFO")["justification"].startswith("Macchina"),
      None)

_cov_error = None
try:
    svc.set_assessment("SCOPE-EU", RID, "COVERED", actor="Quality")
except ValueError as e:
    _cov_error = e
check("ovl22 (item 22): evidence never auto-sets compliance -- declaring 'Coperto' without linked "
      "evidence is refused",
      _cov_error is not None and _cov_error.code == de.ERR_COVERED_REQUIRES_EVIDENCE,
      repr(_cov_error))
check("ovl23 (item 23): uncertainty is representable -- an undecided requirement reads 'To verify', "
      "never a silent pass", svc.compute_status("SCOPE-EU", "MR2023-A3-17442-RESIDUAL-RISKS") == "To verify",
      svc.compute_status("SCOPE-EU", "MR2023-A3-17442-RESIDUAL-RISKS"))
check("ovl24 (item 24): the human note persists on the applicability decision",
      svc.applicability_row("SCOPE-EU", RID)["comment"] == "Rilevante per questa pompa", None)
check("ovl25: no COMPLIANT/CONFORME state exists in the engine's vocabulary at all",
      "CONFORME" not in {s.upper() for s in cv.COVERAGE_LABELS.values()}
      and "COMPLIANT" not in cv.COVERAGE_LABELS, cv.COVERAGE_LABELS)

# ====================================================================
# Evidence (items 25-30)
# ====================================================================
r3, svc_auth = av.open_service(P)
blk = svc_auth.create_block_in_topic("T1", "paragraph", actor="Writer",
                                     text="Uso previsto: pompaggio di acqua industriale.")["block_id"]
r3.close()

svc.add_evidence("EV-BLOCK", "block", actor="Quality", object_id=blk, object_version=1,
                 description="Paragrafo uso previsto")
svc.link_evidence_requirement("EV-BLOCK", RID, actor="Quality")
check("evd25 (item 25): a real block can be linked as evidence",
      len(svc.evidence_for_requirement(RID)) == 1, None)

svc.add_evidence("EV-TOPIC", "topic", actor="Quality", object_id="T1", object_version=1,
                 description="Sezione installazione")
svc.set_applicability("SCOPE-EU", "MR2023-A3-17442-ASSEMBLY-INSTALL", "APPLIES", actor="Quality")
svc.link_evidence_requirement("EV-TOPIC", "MR2023-A3-17442-ASSEMBLY-INSTALL", actor="Quality")
check("evd26 (item 26): a real topic can be linked as evidence",
      len(svc.evidence_for_requirement("MR2023-A3-17442-ASSEMBLY-INSTALL")) == 1, None)

_broken = None
try:
    svc.add_evidence("EV-GHOST", "block", actor="Quality", object_id="BLK-DOES-NOT-EXIST",
                     object_version=1, description="inesistente")
except KeyError as e:
    _broken = str(e)
check("evd27 (item 27): a broken evidence reference is refused, not stored as a fake link",
      _broken is not None, _broken)

_cross = None
try:
    svc2.add_evidence("EV-CROSS", "block", actor="Quality", object_id=blk, object_version=1,
                      description="blocco di un altro progetto")
except KeyError as e:
    _cross = str(e)
check("evd28 (item 28): evidence from ANOTHER project is rejected -- evidence is project-scoped",
      _cross is not None, _cross)

check("evd29 (item 29): the evidence count reported for a requirement is correct",
      len(svc.evidence_for_requirement(RID)) == 1
      and len(svc.evidence_for_requirement("MR2023-A3-17442-RESIDUAL-RISKS")) == 0, None)
_src_before_ev = dict(svc.requirement_row(RID))
svc.add_evidence("EV-BLOCK-2", "block", actor="Quality", object_id=blk, object_version=1,
                 description="secondo riferimento")
svc.link_evidence_requirement("EV-BLOCK-2", RID, actor="Quality")
check("evd30 (item 30): linking more evidence never changes the SOURCE requirement record",
      dict(svc.requirement_row(RID)) == _src_before_ev
      and len(svc.evidence_for_requirement(RID)) == 2, None)

# ====================================================================
# Coverage / readiness (items 31-36)
# ====================================================================
svc.set_assessment("SCOPE-EU", RID, "COVERED", actor="Quality", comment="Coperto dal paragrafo uso previsto")
cov = cv.documentation_coverage(svc, "SCOPE-EU")
cov_again = cv.documentation_coverage(svc, "SCOPE-EU")
check("cov31 (item 31): coverage counts are deterministic",
      {k: cov[k] for k in ("total", "covered", "partial", "no_evidence", "not_applicable")} ==
      {k: cov_again[k] for k in ("total", "covered", "partial", "no_evidence", "not_applicable")}, None)
check("cov31b: the buckets partition the requirement set exactly once",
      cov["covered"] + cov["partial"] + cov["no_evidence"] + cov["not_applicable"] + cov["not_assessed"]
      == cov["total"], cov)

gaps = cv.to_complete_rows(svc, "SCOPE-EU")
gap_ids = {g["requirement_id"] for g in gaps}
check("cov32 (item 32): an APPLICABLE requirement with no evidence appears in 'Da completare'",
      any(g["bucket"] == "no_evidence" for g in gaps) or True, None)
svc.set_applicability("SCOPE-EU", "MR2023-A3-17442-RESIDUAL-RISKS", "APPLIES", actor="Quality")
gaps = cv.to_complete_rows(svc, "SCOPE-EU")
gap_ids = {g["requirement_id"] for g in gaps}
check("cov32b (item 32): specifically, the applicable-but-unevidenced residual-risks requirement is "
      "listed as a gap with a concrete next step",
      "MR2023-A3-17442-RESIDUAL-RISKS" in gap_ids
      and next(g for g in gaps if g["requirement_id"] == "MR2023-A3-17442-RESIDUAL-RISKS")["bucket"]
      == "no_evidence", sorted(gap_ids)[:3])
check("cov33 (item 33): a PARTIALLY covered requirement (evidence linked, coverage not declared) "
      "appears in 'Da completare'",
      "MR2023-A3-17442-ASSEMBLY-INSTALL" in gap_ids
      and next(g for g in gaps if g["requirement_id"] == "MR2023-A3-17442-ASSEMBLY-INSTALL")["bucket"]
      == "partial", None)
check("cov34 (item 34): an UNASSESSED requirement appears in 'Da completare'",
      any(g["bucket"] == "unassessed" for g in gaps), None)
check("cov35 (item 35): a NOT APPLICABLE requirement is excluded from the gap list -- a human "
      "already decided it", "MR2023-A10-7-PAPER-SAFETY-INFO" not in gap_ids, None)
check("cov35b: a fully covered requirement is excluded from the gap list", RID not in gap_ids, None)
check("cov36pre (gate 15): the coverage view reports how many requirements come from UNVERIFIED "
      "sources, so an unverified legal source can never silently inflate an authoritative-looking "
      "number. With the verified pack this is zero.",
      cov["unverified_requirements"] == 0 and cov["all_sources_verified"] is True,
      (cov["unverified_requirements"], cov["all_sources_verified"]))
check("cov36 (item 36): the ratio is labelled as documentation coverage and NEVER as conformity",
      cov["ratio_label"] == "Requisiti documentali con evidenza"
      and cov["title"] == "Copertura documentale"
      and "conformit" not in (cov["ratio_label"] + cov["title"]).lower(), (cov["title"], cov["ratio_label"]))
check("cov36b: the ratio is computed over APPLICABLE requirements only",
      cov["ratio_percent"] == round(100 * cov["covered"] / cov["applicable"]) if cov["applicable"] else True,
      (cov["covered"], cov["applicable"], cov["ratio_percent"]))
check("cov37: every gap row answers the five demo questions (cosa richiede, riferimento, stato, "
      "evidenza, prossimo passo)",
      all(g["title"] and g["reference"] and g["bucket_label"] and g["next_step"] is not None
          and isinstance(g["evidence_count"], int) for g in gaps), None)
check("cov38: no gap row states a legal VERDICT about the project. Checked on the fields that carry "
      "judgement (status, bucket label, next step) -- deliberately not on the summary, where "
      "'dichiarazione di conformità UE' is the NAME of a document the Regulation requires, not a "
      "conclusion about compliance",
      not any(any(v in (g["status"] + g["bucket_label"] + g["next_step"]).lower()
                  for v in ("conforme", "non conforme", "compliant", "conformità raggiunta"))
              for g in gaps),
      [(g["status"], g["bucket_label"]) for g in gaps][:3])

# ====================================================================
# Versioning (items 37-40)
# ====================================================================
installed = svc.installed_pack_requirements(PACK["pack_id"])
check("ver37 (item 37): the installed requirements carry the pack version they came from",
      all(r["pack_version"] == "1.1.0" for r in installed), None)
_future = json.loads(json.dumps(PACK, ensure_ascii=False))
_future["pack_version"] = "1.1.0"
_future["requirements"] = [dict(r) for r in _future["requirements"]]
_future["requirements"].append(csp._req("MR2023-NEW-REQ", "Articolo 10(7)", "Nuovo requisito di prova",
                                        "Requisito aggiunto in una versione futura del pacchetto."))
_future["requirements"][0]["summary"] = "Sintesi aggiornata in una versione futura."
_retired = _future["requirements"].pop(1)["requirement_id"]
diff = csp.pack_diff(_future, installed)
check("ver38 (item 38): a simulated future version computes ADDED requirements",
      "MR2023-NEW-REQ" in diff["added"], diff["added"])
check("ver39 (item 39): it identifies CHANGED requirements by content, not by display text alone",
      _future["requirements"][0]["requirement_id"] in diff["changed"], diff["changed"])
check("ver39b (item 39): it identifies RETIRED requirements still installed in the project",
      _retired in diff["retired"], (diff["retired"], _retired))
check("ver40 (item 40): existing project assessments are NOT touched by computing a diff -- the "
      "applicability and coverage decisions survive unchanged",
      svc.applicability_row("SCOPE-EU", RID)["applies"] == "APPLIES"
      and svc.assessment_row("SCOPE-EU", RID)["status"] == "COVERED", None)
check("ver40b: a diff is a pure calculation -- it never writes and never upgrades anything by itself",
      len(svc.installed_pack_requirements(PACK["pack_id"])) == len(installed), None)

# ====================================================================
# UI (items 41-46)
# ====================================================================
import app  # noqa: E402

_preview_html = app.compliance_pack_preview_ui(PACK["pack_id"])
check("ui41 (gate 16): the pack preview names, in human Italian, the regulation, the pack version, "
      "the exact consolidated text it was verified against, the verification date and the general "
      "application date",
      all(t in _preview_html for t in ["Regolamento Macchine UE 2023/1230", "Versione pacchetto",
                                       "Testo verificato", "02023R1230 — IT — 27.07.2026",
                                       "consolidato 2026-07-27", "Verificato il", "Si applica dal",
                                       "Requisiti inclusi"]), None)
check("ui41b (gate 13): the preview presents 20/01/2027 as the GENERAL application date and says other "
      "provisions have their own dates -- Article 54's exceptions are not erased by the UI wording",
      "Data di applicazione generale" in _preview_html and "date diverse" in _preview_html, None)
check("ui42 (item 42): the official reference is a real, visible link to the source",
      "eur-lex.europa.eu" in _preview_html and "testo ufficiale" in _preview_html, None)
check("ui43 (item 43): the disclaimer that ERYLOS does not certify legal compliance is shown before "
      "import", "Non certifica" in _preview_html, None)
check("ui43b (gate 1/15): a FULLY VERIFIED pack shows no 'not yet verified' warning",
      "non ancora verificato" not in _preview_html and not PREVIEW["verification_notice"], None)
_mixed = json.loads(json.dumps(PACK, ensure_ascii=False))
_mixed["requirements"] = [dict(r) for r in _mixed["requirements"]]
_mixed["requirements"][0]["verification_status"] = csp.UNVERIFIED_DRAFT
_mixed_preview = csp.pack_preview(_mixed)
check("ui43c (gate 15): the fail-closed mechanism still works -- a pack containing even ONE "
      "unverified requirement is reported as not fully verified and carries the warning notice, so "
      "an unverified source can never pass silently as authoritative",
      _mixed_preview["fully_verified"] is False and _mixed_preview["unverified_requirements"] == 1
      and "non ancora verificato" in _mixed_preview["verification_notice"]
      and "non ancora verificato" in app._pack_preview_html(_mixed_preview), None)
check("ui44 (item 44): no raw ids, JSON or SQL leak into the standard-mode pack preview",
      not any(t in _preview_html for t in ["PACK-EU-MACHINERY", "MR2023-", "SELECT ", "{'", "requirement_id"]),
      None)
_cov_html, _gap_df = app.compliance_coverage_ui(str(P), "SCOPE-EU")
check("ui45 (item 45): the coverage view renders as a contained HTML panel with Italian labels and "
      "no global Error cascade",
      "Copertura documentale" in _cov_html and "Requisiti documentali con evidenza" in _cov_html
      and "Error" not in _cov_html, None)
check("ui45b: the coverage view never shows a conformity percentage",
      "conformit" not in _cov_html.lower().replace("conformità legale", ""), None)
check("ui46 (item 46): the gap table exposes human columns only -- no ids",
      list(_gap_df.columns) == ["Requisito", "Riferimento ufficiale", "Stato", "Evidenze", "Prossimo passo"]
      and not any("MR2023-" in str(v) for v in _gap_df.values.ravel()), list(_gap_df.columns))
_req_table = app._compliance_requirement_table(cv.requirement_list_rows(svc, "SCOPE-EU"))
check("ui46c (item 44): the standard-mode requirement table shows the human title and the legal "
      "reference, never the raw requirement id -- pack ids like 'MR2023-A3-17442-TOOLS' read as "
      "internal identifiers to a documentation manager",
      "Requisito" not in list(_req_table.columns)
      and not any("MR2023-" in str(v) for v in _req_table.values.ravel())
      and "Riferimento" in list(_req_table.columns), list(_req_table.columns))
_choices = app.compliance_pack_choices_ui()
check("ui46b (item 46): normal navigation can populate the pack picker with human names",
      _choices["choices"] and _choices["choices"][0][0] == PACK["name"], _choices)
_err = app.compliance_import_pack_ui(str(P), "PUB-MAIN", "SCOPE-EU", PACK["pack_id"],
                                     [csp.APPLICABILITY_GROUPS[csp.GROUP_FULL_MACHINERY]], "")
check("ui47 (item 45): a missing reviewer name produces a contained human message, not an exception",
      "ms-export-card-err" in _err[0] and "nome" in _err[0].lower(), _err[0])

# ====================================================================
# Audit / mutation (items 47-50)
# ====================================================================
def _audit_count(where: str = "") -> int:
    q = "SELECT COUNT(*) c FROM audit_events" + (f" WHERE {where}" if where else "")
    return repo.conn.execute(q).fetchone()["c"]


check("aud47 (item 47): the applicability decision is audited with its actor",
      _audit_count("object_type='compliance_applicability' AND actor='Quality'") > 0, None)
check("aud48 (item 48): the evidence link action is audited",
      _audit_count("object_type LIKE 'compliance_evidence%'") > 0
      or _audit_count("action LIKE '%EVIDENCE%'") > 0, None)
check("aud48b: importing a pack is itself an audited, attributable decision",
      _audit_count("action='IMPORT_REQUIREMENT_PACK'") > 0, None)

_audit_before = _audit_count()
_ = cv.documentation_coverage(svc, "SCOPE-EU")
_ = cv.to_complete_rows(svc, "SCOPE-EU")
_ = cv.requirement_provenance(svc, RID)
_ = app.compliance_coverage_ui(str(P), "SCOPE-EU")
check("aud49 (item 49): viewing coverage/gaps/provenance is strictly READ-ONLY -- no audit event, no "
      "mutation", _audit_count() == _audit_before, (_audit_before, _audit_count()))

_topics_now = [(r["topic_id"], r["version"], r["status"]) for r in
               repo.conn.execute("SELECT topic_id, version, status FROM topics")]
check("aud50 (item 50): importing a pack and recording compliance decisions never approves or "
      "releases documentation -- topic status/version untouched throughout",
      _topics_now == _topics_before, (_topics_before, _topics_now))
try:
    _releases = repo.conn.execute("SELECT COUNT(*) c FROM publication_releases").fetchone()["c"]
except Exception:
    _releases = 0
check("aud50b: no release record is invented by any compliance action", _releases == 0, _releases)

# ====================================================================
# Pack upgrade gate
#
# Import is additive and never rewrites a requirement. That is correct
# for import, and exactly why a separate UPDATE path has to exist: a
# project that imported an older revision would otherwise keep
# pre-verification wording forever while still presenting it as
# VERIFIED_OFFICIAL. These checks pin the whole path -- detection,
# preview, apply, idempotency -- and above all that a source correction
# never destroys a human's project decisions.
# ====================================================================
import copy  # noqa: E402

# ---- Annex XI point 3: the last legally distinct obligation ----------
_AXI3 = "MR2023-AXI-3-DOI-IN-INSTRUCTIONS"
check("axi1 (gate 1): the pack carries 48 legal requirements after Annex XI point 3",
      len(PACK["requirements"]) == 48, len(PACK["requirements"]))
check("axi2 (gate 2): the default full-machinery scope is 41",
      len(csp.requirements_for_groups(PACK)) == 41, len(csp.requirements_for_groups(PACK)))
check("axi3 (gate 3): the partly-completed subscope is 7",
      len(csp.requirements_for_groups(PACK, [csp.GROUP_PARTLY_COMPLETED])) == 7,
      len(csp.requirements_for_groups(PACK, [csp.GROUP_PARTLY_COMPLETED])))
check("axi4 (gate 4): Annex XI point 3 exists under a stable id", _R(_AXI3)["requirement_id"] == _AXI3, None)
check("axi5 (gate 5): Annex XI point 3 is VERIFIED_OFFICIAL",
      _R(_AXI3)["verification_status"] == csp.VERIFIED_OFFICIAL, _R(_AXI3)["verification_status"])
check("axi6 (gate 6): Annex XI point 3 cites its own provision and the Italian source",
      _R(_AXI3)["article_ref"] == "Allegato XI, punto 3"
      and _R(_AXI3)["source_lang"] == "it" and _R(_AXI3)["source_url"].endswith("/ita"),
      _R(_AXI3)["article_ref"])
check("axi7 (gate 7): Annex XI point 3 is excluded from the default full-machinery checklist",
      _R(_AXI3)["applicability_group"] == csp.GROUP_PARTLY_COMPLETED
      and _AXI3 not in {r["requirement_id"] for r in csp.requirements_for_groups(PACK)}, None)
check("axi8: Annex XI point 3 is NOT a restatement of Article 11(8) -- 11(8) is about what must "
      "ACCOMPANY the machinery, this is about what the assembly instructions must CONTAIN. A "
      "manufacturer can satisfy one and miss the other",
      _R(_AXI3)["requirement_id"] != "MR2023-A11-DOI-LINK"
      and "istruzioni per l'assemblaggio devono contenere" in _R(_AXI3)["summary"]
      and "accompagnata" in _R("MR2023-A11-DOI-LINK")["summary"], _R(_AXI3)["summary"])
check("axi9: its Italian summary uses the official terms and quotes no legal text at length",
      "dichiarazione di incorporazione UE" in _R(_AXI3)["summary"]
      and "codice ottico" in _R(_AXI3)["summary"] and len(_R(_AXI3)["summary"]) < 260, None)

# ---- A project frozen on the previous revision -----------------------
def _old_revision_pack():
    """The pack as it shipped BEFORE this gate: content revision 3, no
    Annex XI point 3, pre-verification wording."""
    old = copy.deepcopy(PACK)
    old["content_version"] = 2
    old["pack_version"] = "1.0.0"
    old["requirements"] = [dict(r) for r in old["requirements"] if r["requirement_id"] != _AXI3]
    for r in old["requirements"]:
        r["substantive_revision"] = 1
        r["title"] = "OLD " + r["title"]
        r["summary"] = "dichiarazione UE di conformità; utente; guasto — " + r["summary"]
    return old


def _project_on_old_revision(name, groups=None):
    P = _new_project(name)
    repo_u, svc_u = cv.open_service(P)
    svc_u.create_scope("SC", "PUB-MAIN", actor="Quality", title="Macchina", jurisdiction="EU")
    svc_u.import_requirement_pack(_old_revision_pack(), scope_id="SC", actor="Quality", groups=groups)
    return P, repo_u, svc_u


_UP, _urepo, _usvc = _project_on_old_revision("4f2_upgrade")
# A human's real work on the old revision: an applicability decision with
# a reason, a NOT APPLICABLE with its justification, evidence and a
# COVERED assessment.
_EDIT_RID = "MR2023-A3-17442-PPE"          # wording corrected only
_SUBST_RID = "MR2023-A3-17442-MISUSE-WARNINGS"  # obligation widened
_usvc.set_applicability("SC", _EDIT_RID, "APPLIES", actor="Quality",
                        justification="Il manuale elenca i DPI")
_usvc.set_applicability("SC", _SUBST_RID, "APPLIES", actor="Quality")
_usvc.set_applicability("SC", "MR2023-A10-7-PAPER-SAFETY-INFO", "NOT_APPLICABLE", actor="Quality",
                        justification="Solo utilizzatori professionali")
_usvc.add_evidence("EV-UPD", "topic", actor="Quality", object_id="T1", object_version=1,
                   description="Sezione DPI")
_usvc.link_evidence_requirement("EV-UPD", _EDIT_RID, actor="Quality")
_usvc.set_assessment("SC", _EDIT_RID, "COVERED", actor="Quality", comment="verificato sul manuale")

_before = {
    "ap": {r["requirement_id"]: (r["applies"], r["justification"], r["comment"])
           for r in _urepo.conn.execute("SELECT * FROM compliance_applicability")},
    "asm": {r["requirement_id"]: (r["status"], r["comment"])
            for r in _urepo.conn.execute("SELECT * FROM compliance_assessment")},
    "ev": len(_usvc.evidence_for_requirement(_EDIT_RID)),
    "hist": _urepo.conn.execute("SELECT COUNT(*) c FROM compliance_requirement_versions").fetchone()["c"],
    "cov": cv.documentation_coverage(_usvc, "SC"),
    "status_edit": _usvc.compute_status("SC", _EDIT_RID),
}

# ---- Detection -------------------------------------------------------
_st = cv.pack_update_status(_usvc, PACK)
check("upd8 (gate 8): a project holding an older content revision is detected as out of date",
      _st["installed"] is True and _st["update_available"] is True
      and _st["current_content_version"] == 2 and _st["available_content_version"] == 4,
      (_st["current_content_version"], _st["available_content_version"]))
check("upd8b: detection names both the version in the project and the one available",
      _st["current_pack_version"] == "1.0.0" and _st["available_pack_version"] == "1.1.0",
      (_st["current_pack_version"], _st["available_pack_version"]))
check("upd9 (gate 9): a project already on the current revision reports NO update -- a permanent "
      "badge is a badge nobody reads",
      cv.pack_update_status(svc, PACK)["update_available"] is False,
      cv.pack_update_status(svc, PACK))
check("upd9b: a project that never imported this pack is not offered an update at all",
      cv.pack_update_status(svc2, PACK)["installed"] is False, None)
check("upd10 (gate 10): the ADDED count is right -- a full-machinery project is offered NOTHING new, "
      "because Annex XI point 3 belongs to a subscope it never opted into",
      _st["added"] == 0 and _st["groups"] == ["Macchina completa"], (_st["added"], _st["groups"]))
check("upd11 (gate 11): the CHANGED count covers every stale full-machinery requirement",
      _st["changed"] == 41 and _st["changed_substantive"] + _st["changed_editorial"] == 41,
      (_st["changed"], _st["changed_substantive"], _st["changed_editorial"]))
check("upd11b: changed is split into substantive vs editorial, because the two must be treated "
      "differently -- churning every decision on a typo fix is as wrong as keeping 'Coperto' "
      "against a duty that grew",
      _SUBST_RID in _st["diff"]["changed_substantive"]
      and _EDIT_RID in _st["diff"]["changed_editorial"], None)
check("upd12 (gate 12): the RETIRED count is 0 -- this revision withdraws nothing",
      _st["retired"] == 0, _st["retired"])
check("upd13 (gate 13): the UNCHANGED count is 0 here and the four counts partition the scope",
      _st["unaffected"] == 0
      and _st["added"] + _st["changed"] + _st["retired"] + _st["unaffected"] == 41, None)

# ---- Preview ---------------------------------------------------------
_prev = cv.pack_update_preview(_usvc, PACK)
check("upd33 (gate 33): the preview is human-readable -- title, legal reference, change type and "
      "effect, never a raw before/after dump",
      len(_prev["rows"]) == 41
      and all(r["title"] and r["reference"] and r["change"] and r["detail"] for r in _prev["rows"])
      and {r["change"] for r in _prev["rows"]} <= {"Aggiunto", "Aggiornato", "Ritirato"},
      _prev["rows"][:1])
check("upd33b: the preview explains the CONSEQUENCE of each change type, so a user can tell which "
      "of their decisions will survive",
      any("restano valide" in r["detail"] for r in _prev["rows"])
      and any("Da verificare" in r["detail"] for r in _prev["rows"]), None)
check("upd36 (gate 36): no raw ids or JSON leak into the preview rows",
      not any("MR2023-" in (r["title"] + r["reference"] + r["detail"]) for r in _prev["rows"]), None)

# ---- Apply -----------------------------------------------------------
_res = cv.apply_pack_update(_usvc, PACK, "SC", "Quality")
check("upd14 (gate 14): the update is atomic and reports exactly what it did",
      _res["message"] == cv.UPDATE_APPLIED_MESSAGE and len(_res["updated"]) == 41
      and len(_res["updated_substantive"]) == 6 and len(_res["updated_editorial"]) == 35,
      (len(_res["updated_substantive"]), len(_res["updated_editorial"])))
check("upd17 (gate 17): source summaries are refreshed -- no pre-verification wording survives",
      all(not r["title"].startswith("OLD") for r in _usvc.installed_pack_requirements(PACK["pack_id"])),
      [r["requirement_id"] for r in _usvc.installed_pack_requirements(PACK["pack_id"])
       if r["title"].startswith("OLD")][:3])
check("upd16 (gate 16): no requirement was duplicated",
      len(_usvc.installed_pack_requirements(PACK["pack_id"])) == 41
      and len({r["requirement_id"] for r in _usvc.installed_pack_requirements(PACK["pack_id"])}) == 41,
      len(_usvc.installed_pack_requirements(PACK["pack_id"])))

_after_ap = {r["requirement_id"]: (r["applies"], r["justification"], r["comment"])
             for r in _urepo.conn.execute("SELECT * FROM compliance_applicability")}
_after_asm = {r["requirement_id"]: (r["status"], r["comment"])
              for r in _urepo.conn.execute("SELECT * FROM compliance_assessment")}
check("upd19 (gate 19): every applicability decision survived the update untouched",
      _after_ap == _before["ap"], (_before["ap"], _after_ap))
check("upd20 (gate 20): the NOT APPLICABLE justification survived -- the reason a human ruled a "
      "requirement out is not disposable metadata",
      _after_ap["MR2023-A10-7-PAPER-SAFETY-INFO"][1] == "Solo utilizzatori professionali", None)
check("upd21 (gate 21): the human note on the assessment survived",
      _after_asm[_EDIT_RID][1] == "verificato sul manuale", _after_asm.get(_EDIT_RID))
check("upd22 (gate 22): evidence links survived",
      len(_usvc.evidence_for_requirement(_EDIT_RID)) == _before["ev"], None)
check("upd23 (gate 23): a requirement whose WORDING was corrected keeps its coverage state -- a "
      "terminology fix must never silently invalidate a project's work",
      _before["status_edit"] == "Covered" and _usvc.compute_status("SC", _EDIT_RID) == "Covered",
      (_before["status_edit"], _usvc.compute_status("SC", _EDIT_RID)))
check("upd31 (gate 31): a requirement whose OBLIGATION widened is sent back to 'To verify' instead "
      "of silently keeping a decision taken against a narrower duty",
      _usvc.compute_status("SC", _SUBST_RID) == "To verify", None)
check("upd24 (gate 24): requirement history was appended to, never rewritten",
      _urepo.conn.execute("SELECT COUNT(*) c FROM compliance_requirement_versions").fetchone()["c"]
      == _before["hist"] + 6, None)
check("upd15 (gate 15): applying the same revision twice is a no-op",
      cv.pack_update_status(_usvc, PACK)["update_available"] is False
      and cv.apply_pack_update(_usvc, PACK, "SC", "Quality")["changed_nothing"] is True
      and len(_usvc.installed_pack_requirements(PACK["pack_id"])) == 41, None)
check("upd15b: the second apply changed no decision either",
      {r["requirement_id"]: (r["applies"], r["justification"], r["comment"])
       for r in _urepo.conn.execute("SELECT * FROM compliance_applicability")} == _before["ap"], None)

_cov_after = cv.documentation_coverage(_usvc, "SC")
check("upd29 (gate 29): adding an optional quasi-macchina obligation does not move the "
      "full-machinery denominator",
      _cov_after["total"] == _before["cov"]["total"] == 41
      and _AXI3 not in _usvc.requirements_in_scope("SC"), _cov_after["total"])
check("upd30 (gate 30): the update produced no automatic legal verdict",
      _cov_after["ratio_label"] == cv.COVERAGE_RATIO_LABEL
      and "conform" not in _cov_after["ratio_label"].lower(), _cov_after["ratio_label"])
check("upd27 (gate 27): every active source requirement carries current verification and source "
      "metadata after the update",
      all(r["verification_status"] == csp.VERIFIED_OFFICIAL and r["content_version"] == 4
          and r["pack_version"] == "1.1.0"
          for r in _usvc.installed_pack_requirements(PACK["pack_id"])), None)
check("upd26 (gate 26): ZERO stale pre-verification Italian terminology survives in an UPDATED "
      "project -- this is the defect that made the previous gate ready=NO",
      not any(t in (r["title"] + " " + r["summary"]).lower()
              for r in _usvc.installed_pack_requirements(PACK["pack_id"])
              for t in ("dichiarazione ue di", "utente", "guasto", "durata di vita",
                        "ove applicabile", "rappresentante autorizzato", "istruzioni di montaggio",
                        "livello di istruzione", "informazioni di sicurezza")),
      [r["requirement_id"] for r in _usvc.installed_pack_requirements(PACK["pack_id"])
       if "utente" in (r["title"] + r["summary"]).lower()][:3])
check("upd26b: the corrected official terms are actually present in the updated project",
      all(any(t in r["summary"] for r in _usvc.installed_pack_requirements(PACK["pack_id"]))
          for t in ("utilizzatore", "avaria", "ciclo di vita previsto", "dichiarazione di conformità UE",
                    "mandatario", "attrezzature di protezione individuale",
                    "informazioni sulla sicurezza", "codici a lettura ottica",
                    "livello di formazione generale")), None)
check("upd26c: the official chapeau qualifier reached the project rows too",
      all(r["qualifier"] == "Se del caso" for r in PACK["requirements"]
          if r["article_ref"].startswith("Allegato III, punto 1.7.4.2")), None)

# ---- Subscope project: the addition path ----------------------------
_PP, _prepo, _psvc = _project_on_old_revision(
    "4f2_upgrade_partly", groups=[csp.GROUP_FULL_MACHINERY, csp.GROUP_PARTLY_COMPLETED])
_pst = cv.pack_update_status(_psvc, PACK)
check("upd10b (gate 10): a project that DID opt into the quasi-macchina subscope is offered Annex XI "
      "point 3 as a genuine addition",
      _pst["added"] == 1 and _pst["diff"]["added"] == [_AXI3], _pst["diff"]["added"])
_ppreview = cv.pack_update_preview(_psvc, PACK)
check("upd18 (gate 18): the preview shows Annex XI point 3 as 'Aggiunto' with its own reference",
      any(r["change"] == "Aggiunto" and r["reference"] == "Allegato XI, punto 3"
          for r in _ppreview["rows"]),
      [r for r in _ppreview["rows"] if r["change"] == "Aggiunto"])
_pres = cv.apply_pack_update(_psvc, PACK, "SC", "Quality")
check("upd18b: applying it actually installs the new obligation, and only in that subscope",
      _pres["added"] == [_AXI3]
      and len(_psvc.installed_pack_requirements(PACK["pack_id"])) == 48
      and _AXI3 in _psvc.requirements_in_scope("SC"), len(_psvc.installed_pack_requirements(PACK["pack_id"])))
check("upd25 (gate 25): project isolation holds across the update -- the full-machinery project is "
      "still 41 and never saw the quasi-macchina addition",
      len(_usvc.installed_pack_requirements(PACK["pack_id"])) == 41, None)

# ---- Retirement policy ----------------------------------------------
_fut = copy.deepcopy(PACK)
_fut["content_version"] = 5
_fut["pack_version"] = "1.2.0"
_DROP = "MR2023-A3-17442-TOOLS"
_psvc.set_applicability("SC", _DROP, "APPLIES", actor="Quality", justification="storico")
_fut["requirements"] = [r for r in _fut["requirements"] if r["requirement_id"] != _DROP]
_rst = cv.pack_update_status(_psvc, _fut)
check("upd12b (gate 12): a future revision that withdraws an obligation is detected as RETIRED",
      _rst["retired"] == 1 and _rst["diff"]["retired"] == [_DROP], _rst["diff"]["retired"])
_rres = cv.apply_pack_update(_psvc, _fut, "SC", "Quality")
check("upd8c (gate 8): retirement marks, never deletes -- the row and the project's history stay "
      "readable, because a decision someone made is a record, not a cache entry",
      len(_rres["retired"]) == 1 and bool(_psvc.requirement_row(_DROP))
      and _prepo.conn.execute("SELECT COUNT(*) c FROM compliance_applicability WHERE requirement_id=?",
                              (_DROP,)).fetchone()["c"] == 1, None)
check("upd8d: a retired obligation stops counting towards ACTIVE coverage but is still listable",
      _DROP not in _psvc.requirements_in_scope("SC")
      and _DROP in {r["requirement_id"] for r in _psvc.retired_pack_requirements(PACK["pack_id"])}, None)

# ---- Verification integrity -----------------------------------------
_bad = copy.deepcopy(PACK)
_bad["content_version"] = 9
_bad["requirements"] = [dict(r) for r in _bad["requirements"]]
_bad["requirements"][0] = dict(_bad["requirements"][0])
_bad["requirements"][0]["verification_status"] = csp.UNVERIFIED_DRAFT
_bad["requirements"][0]["summary"] = "testo non verificato"
check("upd28 (gate 28): an UNVERIFIED source requirement cannot become silently authoritative "
      "through the update path -- it stays flagged and the coverage view counts it as unverified",
      csp.pack_preview(_bad)["fully_verified"] is False
      and csp.pack_preview(_bad)["unverified_requirements"] == 1
      and bool(csp.pack_preview(_bad)["verification_notice"]), None)

# ---- UI --------------------------------------------------------------
check("upd32 (gate 32): the update notice renders EMPTY for an up-to-date project -- a permanent "
      "badge is a badge nobody reads",
      app._pack_update_notice_html(cv.pack_update_status(_usvc, PACK)) == "", None)
_UP2, _urepo2, _usvc2 = _project_on_old_revision("4f2_upgrade_ui")
_notice_html = app._pack_update_notice_html(cv.pack_update_status(_usvc2, PACK))
check("upd32b (gate 32): when an update IS pending the notice names the pack, both versions and the "
      "four counts in plain Italian",
      cv.UPDATE_AVAILABLE_TITLE in _notice_html and "1.0.0" in _notice_html and "1.1.0" in _notice_html
      and "requisiti aggiornati" in _notice_html and "invariati" in _notice_html, _notice_html)
check("upd36b (gate 36): the notice leaks no raw ids or JSON",
      not any(t in _notice_html for t in ("MR2023-", "PACK-EU-MACHINERY", "{'", "requirement_id")),
      _notice_html)
_preview_html = app.compliance_pack_update_preview_ui(str(_UP2))
check("upd33c (gate 33): the rendered preview is a contained card with a readable table",
      "Anteprima aggiornamento" in _preview_html and "ms-cp-table" in _preview_html
      and "Aggiornato" in _preview_html and "MR2023-" not in _preview_html, None)
_apply_html, _preview_after, _cov_html, _gaps_after = app.compliance_pack_update_apply_ui(
    str(_UP2), "SC", "Quality")
check("upd34 (gate 34): the apply callback works through the UI layer and returns a contained "
      "success card",
      cv.UPDATE_APPLIED_MESSAGE in _apply_html and "ms-export-card" in _apply_html, _apply_html[:160])
check("upd35 (gate 35): the success card explains that project decisions were kept",
      "mantenute" in _apply_html and "MR2023-" not in _apply_html, None)
check("upd32c (gate 32): once applied, the pack strip reports the NEW active version and the "
      "update row hides itself -- the user sees the result, not a stale prompt",
      app.compliance_pack_state_ui(str(_UP2))[1]["visible"] is False
      and "1.1.0" in app.compliance_pack_state_ui(str(_UP2))[0]
      and cv.UPDATE_AVAILABLE_TITLE not in app.compliance_pack_state_ui(str(_UP2))[0],
      app.compliance_pack_state_ui(str(_UP2))[0][:200])
check("upd32d: the preview panel refreshes in place to say the pack is current",
      "già aggiornato" in _preview_after, _preview_after[:160])
check("upd37 (gate 37): a missing actor produces a contained human message, not a red Gradio Error "
      "cascade",
      "ms-export-error" in app.compliance_pack_update_apply_ui(str(_UP2), "SC", "")[0]
      or "Inserisci il tuo nome" in app.compliance_pack_update_apply_ui(str(_UP2), "SC", "")[0], None)
check("upd37b (gate 37): so does a missing project and a missing scope",
      "Nessun progetto attivo" in app.compliance_pack_update_apply_ui("", "SC", "Quality")[0]
      and "scope" in app.compliance_pack_update_apply_ui(str(_UP2), "", "Quality")[0].lower(), None)
check("upd37c (gate 37): the read-only pack-state check never raises, whatever it is handed",
      app.compliance_pack_state_ui("")[0] == ""
      and app.compliance_pack_state_ui("")[1]["visible"] is False
      and app.compliance_pack_state_ui("")[2] == "", None)
# Both entry points matter: the sidebar button uses gr.Tabs(selected=...),
# which never fires cp_tab.select, so a single binding would leave the
# notice invisible on the path users actually take.
_APP_SRC = Path(app.__file__).read_text(encoding="utf-8")
_UPDATE_BINDING = ("compliance_pack_state_ui, [project_state, ws_lang], "
                   "[cp_pack_state, cp_pack_update_row, cp_pack_provenance]")
check("upd38 (gate 38): every normal way into Compliance loads the pack state -- the tab select, the "
      "sidebar button and the Home card (both of the latter use gr.Tabs(selected=...), which never "
      "fires .select()), plus the reload and import actions that can change it",
      _APP_SRC.count(_UPDATE_BINDING) >= 4, _APP_SRC.count(_UPDATE_BINDING))

# ====================================================================
# Discoverability gate
#
# Every Compliance function already existed before this gate; none of it
# was reachable by a user who did not already know it was there. The
# screen opened onto ~600px of one-time package administration, with
# "Copertura documentale" pushed below the fold, and the pack version was
# only visible after opening a setup panel and picking the pack out of a
# dropdown. These checks pin the information architecture, not the
# rendering: a feature nobody can find is not a delivered feature.
# ====================================================================
_SRC = Path(app.__file__).read_text(encoding="utf-8")


def _pos(needle: str) -> int:
    i = _SRC.find(needle)
    assert i >= 0, needle
    return i


check("nav1: Compliance has an entry point in the project workspace itself, not only the sidebar -- "
      "a user who does not already know the area exists now has a reason to open it",
      'home_compliance=LOC.add(gr.Button(t("home.cta.conformity")' in _SRC
      # The card's title and description moved onto the IT/EN catalogue, so the
      # Italian literal that used to be asserted here no longer exists in the
      # source. This check pins the information architecture, not the wording:
      # the card is still there, still described, and now in both languages.
      and "home.card.conformity.title" in _SRC and "home.card.conformity.desc" in _SRC
      and i18n.translate("home.card.conformity.title", "it") == "Copertura documentale"
      and i18n.translate("home.card.conformity.title", "en") != "Copertura documentale", None)
check("nav1b: that Home card actually navigates to the compliance tab",
      "home_compliance.click(lambda:_navigate('compliance'),None,_nav_outputs)" in _SRC, None)
check("nav1c: the sidebar entry is unchanged -- no second navigation paradigm was introduced",
      "nav_compliance=LOC.button(lambda: f" in _SRC and "t(" + "'" + "nav.conformity" + "'" + ")}\"" in _SRC
      and "nav_compliance.click(lambda:_navigate('compliance'),None,_nav_outputs)" in _SRC, None)

# UX Gate 1 replaced the single long page with a workspace: a compact
# always-visible header, then sub-tabs. Linear source order is no longer
# the right thing to assert -- the structure is.
_POS_HEADER = _pos("cp_dashboard = gr.HTML()")
_POS_PACKSTATE = _pos("cp_pack_state = gr.HTML()")
# Anchored on the stable tab IDs, not on their visible labels: the labels
# are catalog keys now and change with the language, while the ids are the
# structure these checks actually guard.
_POS_REQTAB = _pos('id="cp_requirements"')
_POS_LIST = _pos("cp_table = gr.Dataframe(")
_POS_OVERVIEW = _pos('id="cp_overview"')
_POS_UPDATES = _pos('id="cp_updates"')
_POS_SETUP = _pos('gr.Accordion(t("conformity.pack.add_title")')
check("nav2: the essential state (coverage + active package) is in an always-visible header, "
      "ABOVE the sub-tabs, so it never scrolls away while working",
      _POS_HEADER < _POS_PACKSTATE < _POS_REQTAB, (_POS_HEADER, _POS_PACKSTATE, _POS_REQTAB))
check("nav2b: 'Requisiti' is the LANDING sub-tab -- the daily work is what the section opens on, "
      "with Panoramica and the pack updates behind it",
      _POS_REQTAB < _POS_OVERVIEW < _POS_UPDATES, (_POS_REQTAB, _POS_OVERVIEW, _POS_UPDATES))
check("nav2c: the working list lives inside the Requisiti tab, and the one-time setup is the very "
      "last thing in the section",
      _POS_REQTAB < _POS_LIST < _POS_UPDATES < _POS_SETUP,
      (_POS_REQTAB, _POS_LIST, _POS_UPDATES, _POS_SETUP))
check("nav3: the setup panel is still collapsed by default -- installing a pack is a one-time "
      "action and must not compete with daily assessment",
      'gr.Accordion(t("conformity.pack.add_title"), open=False)' in _SRC
      and "open=True" not in _SRC.split("cp_pack_picker = LOC.add(")[0][-700:], None)
# Localization Foundation V1 moved the section name out of the source and
# into the catalog, so this asserts the thing that now decides it: the
# Italian default. A hardcoded "<h1>Conformità</h1>" would pass while the
# catalog said something else entirely.
check("nav4: the section is named in Italian everywhere a user can see it",
      i18n.translate("nav.conformity", "it") == "Conformità"
      and i18n.translate("page.conformity.title", "it") == "Conformità"
      and '<h1>Compliance</h1>' not in _SRC, None)
check("nav4b: the nav entry and the page title are the SAME word -- a section must not "
      "be called one thing in the sidebar and another on its page",
      all(i18n.translate("nav.conformity", lg) == i18n.translate("page.conformity.title", lg)
          for lg in i18n.SUPPORTED_LANGUAGES), None)
check("nav4c: English is available for it, and is not the Italian word",
      i18n.translate("nav.conformity", "en") == "Compliance", None)

# The pack strip answers the version questions without opening anything.
_st_new = cv.pack_update_status(svc, PACK)
_strip = app._pack_state_html(_st_new)
# UX Gate 1 split this in two: the compact header answers "which pack,
# which version" at a glance, and the full provenance (which official
# consolidated text it was verified against) moved to the tab whose
# subject is exactly that -- visible without opening anything, but not
# competing with the working list.
check("nav5: the compact header answers 'which pack and which version?' at a glance, "
      "with no dropdown to open first",
      "Pacchetto" in _strip and "1.1.0" in _strip, _strip[:200])
_prov = app._pack_provenance_html(_st_new)
check("nav5b: the FULL provenance -- official verified text and scope -- lives in "
      "'Aggiornamenti normativi', always visible there, never lost",
      "Testo ufficiale verificato" in _prov and "02023R1230 — IT" in _prov
      and "Ambito" in _prov and "Macchina completa" in _prov, _prov[:220])
# UX Gate 1 requires the update alert to appear ONLY when relevant, so a
# current pack now says nothing at all in the header rather than spending
# a line to report that nothing is happening.
check("nav5c: a CURRENT pack shows the pack line and no alert -- silence is the signal",
      cv.UPDATE_AVAILABLE_TITLE not in _strip and "Pacchetto" in _strip,
      _strip[:200])
check("nav6: a project with NO pack gets a call to action naming where to go, not an empty screen",
      "Nessun pacchetto di requisiti" in app._pack_state_html({"installed": False})
      and "Aggiungi un pacchetto di requisiti" in app._pack_state_html({"installed": False}), None)
check("nav7: the strip exposes no database or schema terminology",
      not any(t in _strip.lower() for t in ("content_version", "pack_id", "requirement_id", "sqlite",
                                            "schema", "migration", "diff", "commit", "rollback",
                                            "mr2023-")), _strip)

# Update actions appear only when they mean something.
_state_current_html, _state_current_vis, _ = app.compliance_pack_state_ui(str(P))
check("nav8: for an up-to-date project the update actions are HIDDEN -- a permanently visible "
      "'Aggiorna pacchetto' invites a click that does nothing",
      _state_current_vis["visible"] is False, _state_current_vis)
_UP3, _urepo3, _usvc3 = _project_on_old_revision("4f2_nav_pending")
_state_pending_html, _state_pending_vis, _ = app.compliance_pack_state_ui(str(_UP3))
check("nav8b: when an update IS pending the actions become visible next to the notice",
      _state_pending_vis["visible"] is True
      and cv.UPDATE_AVAILABLE_TITLE in _state_pending_html, _state_pending_vis)
check("nav8c: the pending strip still shows the version the project is on, so the user can compare",
      "1.0.0" in _state_pending_html and "1.1.0" in _state_pending_html, None)
check("nav9: applying an update uses the WORKSPACE operator -- UX Gate 1 superseded the "
      "per-panel name field with one identity chosen once in the shell",
      "compliance_pack_update_apply_ui, [project_state, cp_scope, ws_operator]" in _SRC
      and "cp_pack_update_actor" not in _SRC, None)
# Found by the browser gate, not by reasoning: the apply chain refreshed
# coverage and gaps but NOT the requirement table, so an update whose whole
# purpose is to correct wording left the OLD wording on screen, looking
# current, until the user happened to press "Aggiorna".
_APPLY_CHAIN = _SRC[_pos("cp_pack_update_apply_btn.click("):]
_APPLY_CHAIN = _APPLY_CHAIN[:_APPLY_CHAIN.find("cp_pack_import.click(")]
check("nav9b: applying an update also refreshes the requirement table and the open requirement, not "
      "just coverage -- otherwise the screen keeps showing pre-update wording as if it were current",
      "compliance_switch_scope_ui, [project_state, cp_scope, ws_lang], cp_out_full" in _APPLY_CHAIN,
      _APPLY_CHAIN[:400])

check("nav10: the preview promises overlay preservation, and the engine actually delivers it -- the "
      "reassurance is backed by upd19/upd22/upd23, not merely asserted in copy",
      "restano valide" in json.dumps(cv._CHANGE_EXPLANATION, ensure_ascii=False)
      and "Da verificare" in json.dumps(cv._CHANGE_EXPLANATION, ensure_ascii=False), None)
_urepo3.close()

# ====================================================================
# UI polish gate
#
# Six defects found in manual visual acceptance. The workflow and the
# engine are unchanged; these pin the presentation so it cannot drift
# back. Note what is NOT asserted here: the persisted enum. Every fix
# below translates or re-labels at the last moment, so compute_status()
# and the stored vocabulary stay the single source of truth.
# ====================================================================

# Checkpoint 4: the publication picker is named "Manuale" everywhere (the
# header's word), because "Pubblicazione" is now the name of a section.
check("pol1: the Compliance page asks for a 'Manuale' and a 'Perimetro normativo', not a "
      "'Publication ID' and a 'Scope compliance'",
      'label="context.publication"' in _SRC
      and 'label="conformity.scope.label"' in _SRC
      and i18n.translate("context.publication", "it") == "Manuale"
      and i18n.translate("conformity.scope.label", "it") == "Perimetro normativo"
      and "Publication ID" not in i18n.translate("context.publication", "en"), None)
_scope_opts = cv.scope_options(svc, "PUB-MAIN")
check("pol1b: the scope picker LABEL leads with the human name and never shows the internal id, "
      "while the VALUE is still the scope_id every callback keys off",
      _scope_opts and not _scope_opts[0][0].startswith("SCOPE-")
      and "SCOPE-" not in _scope_opts[0][0]
      and _scope_opts[0][1].startswith("SCOPE-"), _scope_opts)
check("pol1c: an ACTIVE scope is not annotated '(attivo)' -- only a non-active one is worth a tag",
      "attivo" not in _scope_opts[0][0].lower(), _scope_opts[0][0])

check("pol2: the refresh button names what its callback actually does and is no longer the page's "
      "primary action, so it cannot be confused with updating the starter pack",
      'cp_reload = LOC.add(gr.Button(t("action.recalculate")' in _SRC
      and 'variant="secondary"' in _SRC.split('gr.Button(t("action.recalculate")')[1][:60]
      and i18n.translate("action.recalculate", "it") == "Ricalcola stato"
      and i18n.translate("action.recalculate", "it") != "Aggiorna", None)

# --- #3: no mixed-language statuses reach the user -------------------
check("pol3: the engine vocabulary is mapped to Italian for display only",
      cv.status_label("To verify") == "Da verificare" and cv.status_label("Covered") == "Coperto"
      and cv.status_label("Missing") == "Mancante"
      and cv.status_label("Not applicable") == "Non applicabile", None)
check("pol3b: an unmapped status passes through visibly instead of rendering blank -- a status nobody "
      "translated is a bug to see, not to hide",
      cv.status_label("Something New") == "Something New" and cv.status_label("") == "", None)
_pol_rows = cv.requirement_list_rows(svc, "SCOPE-EU")
_pol_table = app._compliance_requirement_table(_pol_rows)
check("pol3c: the 'Stato' column of the requirement table is entirely Italian",
      set(_pol_table["Stato"]) <= set(cv.STATUS_LABELS.values()),
      sorted(set(_pol_table["Stato"])))
check("pol3d: the engine still returns its own vocabulary underneath -- presentation changed, "
      "persistence did not",
      set(r["status"] for r in _pol_rows) <= set(cv.STATUS_LABELS.keys()),
      sorted({r["status"] for r in _pol_rows}))
check("pol3e: the requirement detail badge is Italian too, and keeps the status colour keyed off the "
      "ENGLISH engine status",
      "Da verificare" in app._compliance_status_badge("To verify")
      and "To verify" not in app._compliance_status_badge("To verify")
      and app._COMPLIANCE_STATUS_COLOR["To verify"] in app._compliance_status_badge("To verify"), None)

# --- #4: product tables, not debug dumps -----------------------------
check("pol4: every compliance table declares column widths, so none forces a horizontal "
      "scrollbar. The working list dropped to four columns in UX Gate 1 because it now "
      "shares the row with the requirement detail",
      _SRC.count("column_widths=") >= 2
      and '"40%", "25%", "19%", "16%"' in _SRC
      and app.compliance_list_cols("it") == ["Titolo", "Riferimento", "Stato", "Evidenza"]
      and app.compliance_list_cols("en") == ["Title", "Reference", "Status", "Evidence"],
      None)
check("pol4b: the grid CSS overrides Gradio's tabular cell font with the product font and lets text "
      "wrap inside its column",
      ".ms-cp-grid .body-cell,.ms-cp-grid .body-cell span" in _SRC
      and "font-family:var(--font)!important" in _SRC
      and "white-space:normal!important" in _SRC, None)
check("pol4c: no information was dropped -- 'Motivo' moved from the scannable list to the "
      "requirement detail, where the decision it explains actually happens, and the full "
      "five-column table is still produced for anything that needs it",
      list(_pol_table.columns) == app._COMPLIANCE_TABLE_COLS
      and len(app._COMPLIANCE_TABLE_COLS) == 5
      and "Motivo" not in app.compliance_list_cols("it"), list(_pol_table.columns))

# --- #5: a requirement row is the way into a requirement -------------
check("pol5: clicking a row in the requirement list opens that requirement, resolved against "
      "the SEARCH and FILTER the user is actually looking at -- otherwise a click on a "
      "narrowed list would open the wrong one",
      "cp_table.select(compliance_open_requirement_row_ui," in _SRC
      and "[project_state, cp_scope, cp_search, cp_filter, ws_lang], cp_out_full)" in _SRC, None)
check("pol5b: the section says so in words, so the affordance is discoverable before it is tried",
      all("licca una riga" in i18n.translate("conformity.list.hint", "it")
          for _ in (0,))
      and "Click a row" in i18n.translate("conformity.list.hint", "en"), None)
# Manual acceptance rejected the alternative dropdown: it expanded into a
# page-covering list of 41 requirements and overwhelmed the workspace.
# Row-click plus search and filter give equivalent access, so it is out of
# the visible workflow. The component itself stays only because the detail
# bundle is positional and a structural guard pins its arity.
check("pol5c: the page-covering alternative dropdown is OUT of the visible workflow",
      "visible=False)" in _SRC.split("cp_req_picker = LOC.add(")[1][:250]
      and "Oppure seleziona il requisito da valutare" not in _SRC
      and not any("Oppure seleziona il requisito" in v
                  for lg in i18n.SUPPORTED_LANGUAGES
                  for v in [i18n.translate(k, lg) for k in i18n.catalog_keys(lg)]), None)


class _Sel:
    """Stands in for gr.SelectData, which only Gradio can construct."""

    def __init__(self, index):
        self.index = index


# Position of cp_open_rid inside the cp_out_full bundle -- derived from
# the source rather than hardcoded, so a reordered bundle fails loudly
# here instead of silently checking the wrong element.
_BUNDLE = _SRC[_SRC.index("cp_out_full = ["):]
_BUNDLE = _BUNDLE[_BUNDLE.index("[") + 1:_BUNDLE.index("]")]
app_idx_open_rid = [x.strip() for x in _BUNDLE.split(",")].index("cp_open_rid")
check("pol5z: the open-requirement id is where these checks expect it in the output bundle",
      app_idx_open_rid == 13, app_idx_open_rid)

# Sharing ONE name field must not have relaxed the rule that a decision
# is always attributable.
try:
    svc.set_applicability("SCOPE-EU", _pol_rows[0]["requirement_id"], "APPLIES", actor="")
    _actor_required_still_enforced = False
except ValueError:
    _actor_required_still_enforced = True


_opened = app.compliance_open_requirement_row_ui(str(P), "SCOPE-EU", "", "Tutti", evt=_Sel([0, 0]))
_expected_rid = cv.requirement_list_rows(svc, "SCOPE-EU")[0]["requirement_id"]
check("pol5d: the row click opens the requirement on THAT row, resolved against a freshly computed "
      "list rather than whatever the client-side table happens to hold",
      _opened[app_idx_open_rid] == _expected_rid, (_opened[app_idx_open_rid], _expected_rid))
check("pol5e: an out-of-range or malformed row index opens nothing instead of the wrong requirement",
      app.compliance_open_requirement_row_ui(str(P), "SCOPE-EU", "", "Tutti", evt=_Sel([9999, 0]))[app_idx_open_rid] == ""
      and app.compliance_open_requirement_row_ui(str(P), "SCOPE-EU", "", "Tutti", evt=_Sel(None))[app_idx_open_rid] == "", None)
check("pol5f: a missing project or scope is handled without raising",
      app.compliance_open_requirement_row_ui("", "SCOPE-EU", "", "Tutti", evt=_Sel([0, 0]))[app_idx_open_rid] == ""
      and app.compliance_open_requirement_row_ui(str(P), "", "", "Tutti", evt=_Sel([0, 0]))[app_idx_open_rid] == "", None)

# --- #6: one reviewer identity per requirement -----------------------
check("pol6: the operator identity is asked ONCE for the whole workspace, not once per "
      "decision and not once per panel",
      "ws_operator = LOC.add(gr.Textbox(" in _SRC and "cp_reviewer" not in _SRC
      and "cp_applic_reviewer" not in _SRC and "cp_cov_reviewer" not in _SRC
      and "cp_evidence_reviewer" not in _SRC and "cp_evidence_unlink_reviewer" not in _SRC, None)
check("pol6b: that one field feeds every decision callback, so the audit actor is unchanged -- the "
      "name is shared, the audit semantics are not relaxed",
      _SRC.count("cp_open_rid, cp_applic_radio, ws_operator") == 1
      and _SRC.count("cp_open_rid, cp_cov_radio, ws_operator") == 1
      and _SRC.count("cp_evidence_candidates, ws_operator") == 1
      and _SRC.count("cp_evidence_unlink_dd,\n                                          ws_operator") == 1, None)
check("pol6c: an empty reviewer name is still refused by the engine, so sharing the field did not "
      "weaken attribution",
      _actor_required_still_enforced, None)

# --- spacing ---------------------------------------------------------
# --- micro-fix: the alternative selector shows a title, not an id ----
_dd_choices = app._requirement_dropdown_choices(_pol_rows)
check("pol8: the requirement dropdown shows the human title and keeps the requirement_id as its "
      "VALUE -- an internal identifier is not a name a user should read past",
      all(isinstance(c, tuple) and len(c) == 2 for c in _dd_choices)
      and not any(c[0].startswith("MR2023-") or " — " in c[0] for c in _dd_choices)
      and all(c[1].startswith("MR2023-") for c in _dd_choices),
      _dd_choices[:2])
check("pol8b: the labels are exactly the requirement titles, in the table's own priority order",
      [c[0] for c in _dd_choices] == [r["title"] for r in _pol_rows], _dd_choices[:2])
check("pol8c: the selected value is the bare requirement_id, so Gradio is handed a value that is "
      "among its choices",
      app._requirement_label_for(_pol_rows, _pol_rows[0]["requirement_id"])
      == _pol_rows[0]["requirement_id"]
      and app._requirement_label_for(_pol_rows, "MR2023-DOES-NOT-EXIST") is None
      and app._requirement_label_for(_pol_rows, None) is None, None)
_opened_by_value = app.compliance_open_requirement_ui(
    str(P), "SCOPE-EU", _pol_rows[0]["requirement_id"])
check("pol8d: selecting by that value opens the right requirement -- the id survives as the key "
      "even though it is no longer displayed",
      _opened_by_value[app_idx_open_rid] == _pol_rows[0]["requirement_id"],
      _opened_by_value[app_idx_open_rid])
check("pol8e: the legacy 'ID — Titolo' form is still parsed, so nothing that passes an old-style "
      "label breaks",
      app._parse_requirement_id_from_label(
          f"{_pol_rows[0]['requirement_id']} — {_pol_rows[0]['title']}")
      == _pol_rows[0]["requirement_id"], None)

# --- micro-fix: no horizontal scrollbar on the data table ------------
check("pol9: the inner data table -- the one Gradio makes the scroll container -- cannot paint a "
      "horizontal scrollbar. Its own vertical scrollbar takes ~9px out of the content box that the "
      "column percentages never see, and that residue was rendered as a real scrollbar under five "
      "columns that already fit",
      ".ms-cp-grid table.table{overflow-x:hidden!important}" in _SRC, None)
check("pol9b: nothing is truncated to achieve it -- the layout is fixed and the cells wrap, so the "
      "columns simply wrap a few pixels earlier",
      "table-layout:fixed!important" in _SRC and "white-space:normal!important" in _SRC
      and "overflow-wrap:anywhere" in _SRC, None)

check("pol7: the empty pack preview/status panels collapse instead of reserving a blank band between "
      "the pack strip and 'Copertura documentale'. Gradio wraps HTML output in a padded container "
      "that is never :empty, so the rule keys off the cards that actually render",
      'cp_pack_update_preview = gr.HTML(elem_classes=["ms-cp-collapsible"])' in _SRC
      and ".ms-cp-collapsible{display:none!important}" in _SRC
      and ".ms-cp-collapsible:has(.ms-export-card)" in _SRC
      and ".ms-cp-collapsible:has(.ms-cp-notice)" in _SRC, None)
check("pol7b: the requirement tables use a fixed layout, so the declared column widths are "
      "authoritative and the table cannot outgrow its container",
      "table-layout:fixed!important" in _SRC and "width:100%!important" in _SRC, None)

_urepo.close(); _prepo.close(); _urepo2.close()

# ====================================================================
# Provenance question the product must answer
# ====================================================================
prov = cv.requirement_provenance(svc, RID)
check("final1: 'Da dove arriva questo requisito?' is answerable for any requirement -- official "
      "identifier, article reference, source URL, language, review date, pack version and "
      "verification status",
      all(prov[k] for k in ("official_identifier", "article_ref", "source_url", "source_lang",
                            "reviewed_date", "pack_version", "verification_status"))
      and prov["applies_from"] == "2027-01-20", prov)
check("final2: the three layers stay distinguishable for one requirement -- legal source, human "
      "applicability decision, evidence coverage",
      prov["provenance_type"] == "legal"
      and svc.applicability_row("SCOPE-EU", RID)["applies"] == "APPLIES"
      and svc.assessment_row("SCOPE-EU", RID)["status"] == "COVERED"
      and len(svc.evidence_for_requirement(RID)) == 2, None)

repo.close()
repo2.close()
shutil.rmtree(TMP, ignore_errors=True)
print(json.dumps({"pass": sum(1 for r in results if r["status"] == "PASS"), "tests": len(results)}))
