"""Publishing Preflight v1 — OUTPUT-INTEGRITY preflight for production
export. The smallest coherent subset of Prompt 10 needed to make export
fail closed.

## What this is, stated narrowly

This is an output-integrity check, NOT a general-purpose overset
detector and NOT a layout engine. It does not reflow, paginate, resolve
cross-references, build a TOC or validate a template. It asks one
question about an export about to be handed to a customer: *did a
SAMPLE of the authored content actually survive into the finished file?*

Being explicit about the sampling matters, because the difference
between "we verify the document" and "we verify representative points in
the document" is the difference between a claim we can defend and one we
cannot. `CONTENT_LOSS` is DETERMINISTIC for the cases instrumented by
its source-derived probes, and says nothing about anything else. Up to 24
probes are derived from the output model: the publication title, each
section heading, one distinctive word per content block, and per table
either a repeated token with the exact number of times it must appear, or
a token unique to the last column, or both.

Not covered, and not claimed: any block whose longest word is under five
characters or carries internal punctuation (so no probe can be built),
anything past the probe budget on a very large publication, loss inside
an element that keeps its probe, and any table whose vocabulary yields
neither a repeated token nor a last-column token. Detection of every
possible content-loss shape is NOT guaranteed.

What the sampling IS good for: the renderer's real failure mode here is
not losing a word, it is discarding a whole element. A clipped table
takes all of its cells with it, a dropped block takes all of its text.
Against that failure mode a probe per element is sensitive, and the
last-column probe is aimed squarely at the one case measured in this
build.

## Why the rules are deterministic rather than heuristic

The obvious way to "detect overset" is to guess -- count characters,
count table columns, assume a threshold. That would be a lie dressed as a
check: it would fire on documents that lay out perfectly and stay silent
on documents that do not, because the only thing that knows whether
content fits is the layout engine.

So every rule here reads the ACTUAL engine instead:

* `fitz.Story.place(frame)` returns `(more, filled)`. `more` is the
  engine saying "there is content I could not place in this frame".
* The finished PDF is re-opened and its extracted text layer is searched
  for the probes that must have survived.

Both are observations, not estimates. Measured against this MuPDF build
(1.26.12):

* an over-wide table is CLIPPED: 36 of 40 columns simply vanish from the
  text layer while `more` reports 0 and `filled` stays inside the frame.
  Geometry alone cannot see this; the text-layer probe can. This is the
  one real, reachable overset condition in the current pipeline, and it
  is horizontal.

## What this build does NOT let us detect, and why there is no rule for it

Two geometric rules were written for this gate and then REMOVED, because
measuring them proved they could not work. Recording why, so nobody
reintroduces them:

* **"the layout stopped advancing"** -- keyed on consecutive placements
  returning the same height while content remained unplaced. It fires on
  every document longer than two pages: a full page fills the frame to
  exactly the same height every time, so "stuck" and "paginating" are
  indistinguishable from `(more, filled_height)` alone.
* **"an element is taller than the printable frame"** -- keyed on
  `filled.height > frame.height`. Measured: a perfectly normal full page
  comes back 12pt past the frame (the last line's leading extends beyond
  it), on every full page of every document. Meanwhile a 6000px figure,
  the obvious candidate for vertical overset, does NOT overflow at all --
  MuPDF scales it down to fit and reports `more = 0`. So the two ends are
  12pt and 0pt, and any tolerance separating "normal overshoot" from
  "unplaceable element" would be a number invented to make a rule look
  like it works.

The conclusion is that in THIS export architecture vertical overset is
not a meaningful condition: ordinary content paginates, and an oversized
figure is scaled down to fit rather than overflowing. There is therefore
nothing for a vertical rule to detect, and a rule that fired anyway would
only produce false refusals.

This is a property of the current renderer, not a permanent one. Fixed
frames, multi-column layout, or a template with rigid regions would make
vertical overset reachable, and that work will need its own GEOMETRIC
preflight — measured against whatever engine produces it, not carried
over from here on the assumption that it still applies.

## Severity

BLOCKING means the artifact would misrepresent the source: content the
author wrote is not in the file. Production export refuses.

WARNING means the artifact is faithful but something deserves a human
eye. Export proceeds -- deliberately, because turning today's successful
exports into failures is a product decision, not a preflight decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------

BLOCKING = "blocking"
WARNING = "warning"
SEVERITIES = (BLOCKING, WARNING)

_SEVERITY_LABEL = {BLOCKING: "Bloccante", WARNING: "Avviso"}

# ---------------------------------------------------------------------
# Rule identifiers
#
# Stable ids so a report, a manifest and a test can all refer to the same
# rule. They are NOT shown to the user -- every rule carries its own
# Italian sentence.
# ---------------------------------------------------------------------

RULE_EMPTY_PUBLICATION = "EMPTY_PUBLICATION"
RULE_OVERSET_PAGE_LIMIT = "OVERSET_PAGE_LIMIT"
RULE_CONTENT_LOSS = "CONTENT_LOSS"
RULE_ASSET_MISSING = "ASSET_MISSING"
RULE_EMPTY_TOPIC = "EMPTY_TOPIC"

BLOCKING_RULES = frozenset({
    RULE_EMPTY_PUBLICATION,
    RULE_OVERSET_PAGE_LIMIT,
    RULE_CONTENT_LOSS,
})
WARNING_RULES = frozenset({RULE_ASSET_MISSING, RULE_EMPTY_TOPIC})
ALL_RULES = BLOCKING_RULES | WARNING_RULES

# Rules that can only be evaluated where there IS a fixed page frame.
# An HTML5 package reflows to the reader's window: it has no frame, so it
# cannot have overset, and claiming to check for it there would be
# theatre.
PAGE_LAYOUT_RULES = frozenset({RULE_OVERSET_PAGE_LIMIT, RULE_CONTENT_LOSS})


@dataclass(frozen=True)
class Finding:
    """One preflight result.

    `where` is the human location ("Sezione «Installazione» — tabella 2"),
    never an internal id. `fix` is what the author must actually change;
    a finding a user cannot act on is a complaint, not a check."""
    rule: str
    severity: str
    what: str
    where: str = ""
    fix: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def is_blocking(self) -> bool:
        return self.severity == BLOCKING

    def as_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "severity": self.severity, "what": self.what,
                "where": self.where, "fix": self.fix, "detail": dict(self.detail)}


@dataclass
class PreflightReport:
    """Everything the export decision and the user-facing message need."""
    kind: str                       # "pdf" | "html5"
    findings: list[Finding] = field(default_factory=list)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.is_blocking]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if not f.is_blocking]

    @property
    def ok(self) -> bool:
        """True when production export may proceed. Warnings never block."""
        return not self.blocking

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "ok": self.ok,
                "blocking": len(self.blocking), "warnings": len(self.warnings),
                "findings": [f.as_dict() for f in self.findings]}


# ---------------------------------------------------------------------
# Layout telemetry
# ---------------------------------------------------------------------

@dataclass
class LayoutTrace:
    """What the layout engine actually did, recorded while it did it.

    `placements` is one (more, filled_height) pair per page, in order.
    `frame_height` is the printable height those placements had to fit
    inside. `page_limit_hit` says the render loop stopped because of its
    own hard cap rather than because the story ended."""
    frame_height: float = 0.0
    frame_width: float = 0.0
    placements: list[tuple[int, float]] = field(default_factory=list)
    page_limit_hit: bool = False
    page_limit: int = 0
    unplaced_at_end: bool = False

    @property
    def pages(self) -> int:
        return len(self.placements)



# ---------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------

def check_model(model: dict[str, Any], *, kind: str) -> PreflightReport:
    """Rules that need only the output model -- they apply to every
    export kind, because they are about the CONTENT being complete rather
    than about how it lays out."""
    report = PreflightReport(kind=kind)
    topics = model.get("topics") or []
    if not topics:
        report.add(Finding(
            rule=RULE_EMPTY_PUBLICATION, severity=BLOCKING,
            what="Nessun contenuto incluso per questo profilo.",
            where=f"Pubblicazione «{model.get('publication_title', '')}»",
            fix="Verifica il profilo di variante selezionato: con le condizioni attuali "
                "non viene incluso alcun argomento."))
        return report

    empty = [t for t in topics if not (t.get("blocks") or [])]
    if len(empty) == len(topics):
        # Every included section is empty. The renderer would happily
        # produce a PDF of nothing but headings, which is a deliverable
        # only in the sense that the file opens. Blocking, and phrased so
        # the user looks at the profile rather than at the document.
        report.add(Finding(
            rule=RULE_EMPTY_PUBLICATION, severity=BLOCKING,
            what="Nessun contenuto incluso per questo profilo: il documento conterrebbe "
                 "solo i titoli delle sezioni.",
            where=f"Pubblicazione «{model.get('publication_title', '')}» — "
                  f"profilo «{model.get('profile_name', '')}»",
            fix="Verifica il profilo di variante selezionato: con le condizioni attuali "
                "nessun blocco di contenuto viene incluso.",
            detail={"topics": len(topics)}))
        return report

    for topic in empty:
        report.add(Finding(
            rule=RULE_EMPTY_TOPIC, severity=WARNING,
            what="Sezione inclusa ma senza contenuto.",
            where=f"Sezione «{topic.get('title', '')}»",
            fix="Aggiungi contenuto alla sezione oppure escludila dal profilo: "
                "nel documento finale comparirà solo il titolo.",
            detail={"topic_title": topic.get("title", "")}))
    return report


def check_assets(report: PreflightReport, asset_warnings: list[str]) -> None:
    """Figures whose file could not be materialised.

    WARNING, not blocking, and deliberately so: the current export already
    renders a visible placeholder and records the problem in the manifest,
    so nothing is lost silently. Promoting this to blocking would turn
    exports that succeed today into failures -- a product decision that
    belongs to a later gate, not to the preflight that introduces the
    mechanism."""
    for message in asset_warnings or []:
        report.add(Finding(
            rule=RULE_ASSET_MISSING, severity=WARNING,
            what="Immagine non disponibile: nel documento compare un segnaposto.",
            where=str(message),
            fix="Ricollega il file dell'immagine alla figura e riesporta.",
            detail={"asset_warning": str(message)}))


def check_layout(report: PreflightReport, trace: LayoutTrace) -> None:
    """Rules read straight off the layout engine. PDF path only."""
    if trace.page_limit_hit and trace.unplaced_at_end:
        report.add(Finding(
            rule=RULE_OVERSET_PAGE_LIMIT, severity=BLOCKING,
            what=(f"Il contenuto non è stato impaginato per intero: il motore ha raggiunto "
                  f"il limite di {trace.page_limit} pagine con del contenuto ancora da "
                  f"collocare."),
            where=f"Oltre pagina {trace.pages}",
            fix="Riduci o suddividi il contenuto della pubblicazione: così com'è, "
                "una parte non entrerebbe nel documento finale.",
            detail={"pages": trace.pages, "page_limit": trace.page_limit}))



def check_content_survived(report: PreflightReport, missing: list[dict[str, Any]]) -> None:
    """Content the author wrote that is NOT in the finished file.

    This is the rule that catches the classic overset case in this MuPDF
    build: a table wider than the frame is clipped, its columns disappear
    from the text layer, and nothing in the geometry says so. Each entry
    carries where it came from, so the report can point at a section
    instead of at a missing string."""
    if not missing:
        return
    where = missing[0].get("where") or ""
    extra = len(missing) - 1
    report.add(Finding(
        rule=RULE_CONTENT_LOSS, severity=BLOCKING,
        what=("Parte del contenuto non è arrivata nel PDF: il motore di impaginazione "
              "l'ha scartata durante il rendering."),
        where=where + (f" (e altri {extra} punti)" if extra > 0 else ""),
        fix="La causa tipica è una tabella troppo larga per la pagina: riduci il numero "
            "di colonne o accorcia le intestazioni, poi riesporta.",
        detail={"lost": missing[:12], "lost_count": len(missing)}))


# ---------------------------------------------------------------------
# Human-readable report
# ---------------------------------------------------------------------

def report_lines(report: PreflightReport, *, show_severity: bool = True) -> list[str]:
    """Plain-text lines. One place builds the wording, so the export
    exception, the UI card and the manifest cannot drift apart."""
    lines: list[str] = []
    for finding in report.blocking + report.warnings:
        # A refusal card is red, headed "Export bloccato", and contains
        # nothing but blocking findings: tagging every line "[Bloccante]"
        # there only adds a label that reads like machine output to the
        # person trying to fix the document.
        head = (f"[{_SEVERITY_LABEL[finding.severity]}] {finding.what}"
                if show_severity else finding.what)
        lines.append(head)
        if finding.where:
            lines.append(f"    Dove: {finding.where}")
        if finding.fix:
            lines.append(f"    Cosa fare: {finding.fix}")
    return lines


def blocking_message(report: PreflightReport) -> str:
    """The message a refused export shows.

    States all four things a blocked user needs: that nothing was saved,
    what failed, where, and what to change. A refusal that does not say
    what to fix is just a wall."""
    if report.ok:
        return ""
    kind = "PDF" if report.kind == "pdf" else "pacchetto HTML"
    head = (f"Export bloccato: il {kind} non è stato salvato perché il documento "
            f"risulterebbe incompleto.")
    _blocking_only = PreflightReport(kind=report.kind, findings=report.blocking)
    body = "\n".join(report_lines(_blocking_only, show_severity=False))
    tail = ("Il contenuto del progetto non è stato modificato: correggi i punti indicati "
            "e riprova l'export.")
    return f"{head}\n\n{body}\n\n{tail}"


def manifest_entries(report: PreflightReport) -> list[str]:
    """Preflight lines for the artifact manifest, so a delivered export
    carries the warnings it was produced with.

    No severity prefix: this list joins the export's existing `warnings`,
    which the result card already introduces with its own "Avviso:" /
    "N avvisi:" heading. Prefixing here produced "Avviso: Avviso: ..."
    on screen -- caught in browser acceptance, not by the unit checks,
    because both halves were individually correct."""
    return [f.what + (f" — {f.where}" if f.where else "")
            for f in report.warnings
            # ASSET_MISSING is DERIVED from the export's own asset warning,
            # which is already in that list verbatim. Adding it again put
            # the same problem in the delivered manifest twice, once raw
            # and once wrapped -- noise in an artifact a customer may read.
            if f.rule != RULE_ASSET_MISSING]
