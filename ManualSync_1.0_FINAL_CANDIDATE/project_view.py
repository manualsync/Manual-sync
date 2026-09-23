"""Read-only project facts the interface needs to explain itself.

Services own every rule; this module only READS, so the UI can say WHY an
action cannot run and point at the exact things involved, instead of a
bare refusal. No function here writes, and none re-decides a rule: where a
fact mirrors an engine gate (which content blocks a translation package),
a regression test pins it to that gate so the two can never drift apart.

The second part renders Settings › Project: the project's details and its
translation languages, with the rule that a language may leave the list
only when nothing in the project was produced for it. The last part holds
the Release notices about the publishing profile preview and export need.
"""
from __future__ import annotations

import html
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import i18n
import ux_components as ux
from ccms_industrial import IndustrialRepository
from language_codes import canonical_lang, canonical_langs

# The engine's approval set (translation_impact.analyze_translation_impact):
# only these statuses may feed a translation package.
APPROVED_STATUSES = frozenset({"Approved", "Published"})


@dataclass(frozen=True)
class BlockingItem:
    kind: str            # "topic" | "block" | "table" | "figure" | "publication"
    object_id: str
    label: str           # human text: topic title or a preview of the block
    status: str          # raw engine status ("Draft", "In Review", "MISSING", "EMPTY", ...)
    archived: bool = False

    @property
    def openable(self) -> bool:
        """Whether the editor can open it directly (topics and blocks)."""
        return self.kind in ("topic", "block")

    @property
    def open_value(self) -> str:
        return f"{self.kind}:{self.object_id}"


def ccms_db(project_dir: str | Path) -> Path:
    base = Path(project_dir)
    for name in ("ManualSync_CCMS.sqlite", "ManualSync_CCMS_v18.sqlite"):
        if (base / name).exists():
            return base / name
    raise FileNotFoundError("Repository CCMS non trovato nel progetto")


def _preview(text: Any, limit: int = 70) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def _block_label(row: Any) -> str:
    text = _preview(row["text_content"])
    return text or str(row["block_id"])


def unapproved_for_packages(repo: IndustrialRepository, kind: str, object_id: str) -> list[BlockingItem]:
    """Everything that stops a translation package from being generated for
    (kind, object_id) because it is not approved.

    Mirrors the engine gate exactly -- empty if and only if the engine would
    accept the object:
      * publication: every linked topic and every block of those topics,
        archived ones included (_publication_status does not skip them);
        a publication with no topics is itself blocking ("EMPTY");
      * topic / block / table / figure: the object's own status;
      * component: never gated.
    Unlike the engine, which stops at the first offender, this lists them
    all, so the user can see and open each one. Raises KeyError when the
    object does not exist, exactly as the engine does.
    """
    kind = str(kind or "").strip().lower()
    oid = str(object_id or "").strip()
    conn = repo.conn
    if kind == "component":
        return []
    if kind == "publication":
        pub = conn.execute("SELECT publication_id, title FROM publications WHERE publication_id=?", (oid,)).fetchone()
        if not pub:
            raise KeyError(oid)
        links = list(conn.execute(
            "SELECT topic_id FROM publication_topics WHERE publication_id=? ORDER BY position", (oid,)))
        if not links:
            return [BlockingItem("publication", oid, str(pub["title"] or oid), "EMPTY")]
        out: list[BlockingItem] = []
        for link in links:
            tid = str(link["topic_id"])
            t = conn.execute("SELECT topic_id, title, status, archived_at FROM topics WHERE topic_id=?", (tid,)).fetchone()
            if not t:
                out.append(BlockingItem("topic", tid, tid, "MISSING"))
                continue
            if str(t["status"] or "") not in APPROVED_STATUSES:
                out.append(BlockingItem("topic", tid, str(t["title"] or tid), str(t["status"] or ""),
                                        bool(t["archived_at"])))
            for b in conn.execute(
                    "SELECT block_id, text_content, status, archived_at FROM content_blocks WHERE topic_id=? "
                    "ORDER BY position, block_id", (tid,)):
                if str(b["status"] or "") not in APPROVED_STATUSES:
                    out.append(BlockingItem("block", str(b["block_id"]), _block_label(b), str(b["status"] or "Draft"),
                                            bool(b["archived_at"])))
        return out
    table = {"topic": ("topics", "topic_id", "title"), "block": ("content_blocks", "block_id", "text_content"),
             "table": ("structured_tables", "table_id", "title"), "figure": ("figures", "figure_id", "title")}.get(kind)
    if table is None:
        raise ValueError(f"kind non supportato: {kind}")
    name, key, label_col = table
    row = conn.execute(f"SELECT * FROM {name} WHERE {key}=?", (oid,)).fetchone()
    if not row:
        raise KeyError(oid)
    status = str(row["status"] or "")
    if status in APPROVED_STATUSES:
        return []
    archived = bool(row["archived_at"]) if "archived_at" in row.keys() else False
    return [BlockingItem(kind, oid, _preview(row[label_col]) or oid, status or "Draft", archived)]


# ======================================================================
# Settings › Project -- the project's translation languages
# ======================================================================
# Offered in the pickers; any other well-formed code can still be typed.
# The EU's official languages (English in both common variants) plus the
# two further languages machinery manuals most often ship in.
LANGUAGE_CODES = (
    "bg-BG", "cs-CZ", "da-DK", "de-DE", "el-GR", "en-GB", "en-US", "es-ES", "et-EE", "fi-FI",
    "fr-FR", "ga-IE", "hr-HR", "hu-HU", "it-IT", "lt-LT", "lv-LV", "mt-MT", "nl-NL", "no-NO",
    "pl-PL", "pt-PT", "ro-RO", "sk-SK", "sl-SI", "sv-SE", "tr-TR",
)

# language[-Script][-REGION] once canonical_lang has run: de-DE, pt-BR,
# zh-Hant-TW, es-419. Anything else a user types is refused, not stored.
_LANG_CODE = re.compile(r"^[a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-(?:[A-Z]{2}|[0-9]{3}))?$")

# Where a target language leaves data behind: the CCMS translation tables
# and releases, the workflow's jobs, and every translation memory in the
# project (tm_entries, wherever the file sits).
_TRANSLATION_TABLES = ("topic_translations", "block_translations", "component_translations",
                       "structured_table_translations", "figure_translations")


def _t(key: str, **params: Any) -> str:
    return i18n.translate(key, None, **params)


def _tn(key: str, n: int, **params: Any) -> str:
    """Catalog text with the `.one` singular variant when n == 1."""
    if n == 1 and i18n.has_key(f"{key}.one"):
        return _t(f"{key}.one", n=n, **params)
    return _t(key, n=n, **params)


def language_code(value: Any) -> str:
    """The canonical code for a typed or picked value, or "" if it is not one."""
    code = canonical_lang(str(value or "").strip())
    return code if _LANG_CODE.match(code) else ""


def same_language(a: Any, b: Any) -> bool:
    return bool(canonical_lang(a)) and canonical_lang(a).casefold() == canonical_lang(b).casefold()


def language_label(code: str) -> str:
    """'Tedesco (de-DE)'. A language the catalogue does not name reads as its code."""
    code = canonical_lang(code)
    if not code:
        return ""
    key = f"lang.noun.{code.split('-')[0].lower()}"
    name = _t(key)
    return code if name == key else f"{name} ({code})"


def language_choices(exclude: Iterable[str] = ()) -> list[tuple[str, str]]:
    """(label, code) pairs for a picker, alphabetical in the current language."""
    gone = {canonical_lang(x).casefold() for x in exclude}
    return sorted(((language_label(c), c) for c in LANGUAGE_CODES if c.casefold() not in gone),
                  key=lambda pair: pair[0].casefold())


def _as_values(requested: Any) -> list[str]:
    if requested is None:
        return []
    if isinstance(requested, str):
        return requested.replace(";", ",").split(",")
    return [str(x) for x in requested]


@dataclass(frozen=True)
class LanguageAddition:
    add: tuple[str, ...] = ()        # new translation languages, canonical
    already: tuple[str, ...] = ()    # already in the project: ignored
    source: tuple[str, ...] = ()     # the document's own language: refused
    invalid: tuple[str, ...] = ()    # not a language code, as typed


def plan_language_addition(requested: Any, source_lang: str, current: Iterable[str]) -> LanguageAddition:
    """Sort what the user asked to add. Each value lands in exactly one
    bucket, and only `add` is ever saved."""
    have = {c.casefold() for c in canonical_langs(list(current or []))}
    add: list[str] = []
    already: list[str] = []
    source: list[str] = []
    invalid: list[str] = []
    for raw in _as_values(requested):
        text = raw.strip()
        if not text:
            continue
        code = language_code(text)
        if not code:
            if text not in invalid:
                invalid.append(text)
        elif same_language(code, source_lang):
            if code not in source:
                source.append(code)
        elif code.casefold() in have:
            if code not in already:
                already.append(code)
        elif code.casefold() not in {a.casefold() for a in add}:
            add.append(code)
    return LanguageAddition(tuple(add), tuple(already), tuple(source), tuple(invalid))


@dataclass(frozen=True)
class LanguageUsage:
    language: str
    translations: int = 0
    jobs: int = 0
    releases: int = 0
    memory: int = 0

    @property
    def in_use(self) -> bool:
        return any((self.translations, self.jobs, self.releases, self.memory))


def _count(conn: sqlite3.Connection, table: str, code: str) -> int:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return 0
    return int(conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE REPLACE(target_lang,'_','-')=? COLLATE NOCASE", (code,)).fetchone()[0])


def _count_in(db: Path, tables: Iterable[str], code: str) -> int:
    # SELECTs only. A plain connection rather than mode=ro: a read-only
    # connection cannot tidy a WAL database's side files on close and would
    # leave them behind in the project folder.
    conn = sqlite3.connect(str(db), timeout=10)
    try:
        return sum(_count(conn, t, code) for t in tables)
    finally:
        conn.close()


def language_usage(project_dir: str | Path, language: str) -> LanguageUsage:
    """Everything the project holds for `language` as a translation target.

    A table that does not exist yet counts zero. Any other failure is
    raised, never turned into a zero: the caller must then refuse the
    removal, because "could not check" is not "nothing there".
    """
    base = Path(project_dir)
    code = canonical_lang(language)
    translations = releases = jobs = memory = 0
    try:
        db = ccms_db(base)
    except FileNotFoundError:
        db = None
    if db is not None:
        translations = _count_in(db, _TRANSLATION_TABLES, code)
        releases = _count_in(db, ("publication_releases",), code)
    workflow = base / "ManualSync_Workflow.sqlite"
    if workflow.exists():
        jobs = _count_in(workflow, ("translation_jobs",), code)
    for tm in sorted(base.rglob("*.sqlite")):
        memory += _count_in(tm, ("tm_entries",), code)
    return LanguageUsage(code, translations, jobs, releases, memory)


def usage_lines(usage: LanguageUsage) -> list[str]:
    return [_tn(f"ux.proj.usage.{field}", getattr(usage, field))
            for field in ("translations", "jobs", "releases", "memory") if getattr(usage, field)]


# ---------------------------------------------------------------- rendering
_PROJECT_FIELDS = ("client", "product", "manual_code", "revision", "owner")


def project_facts_html(meta: dict | None) -> str:
    """The project's details, read-only, as label/value pairs."""
    if not meta:
        return ux.notice(ux.INFO, _t("ux.no_project.title"), _t("ux.no_project.body"))
    rows = [(_t("ux.proj.field.name"), html.escape(str(meta.get("project_name") or "")))]
    for field in _PROJECT_FIELDS:
        value = str(meta.get(field) or "").strip()
        if value:
            rows.append((_t(f"ux.proj.field.{field}"), html.escape(value)))
    source = str(meta.get("source_lang") or "").strip()
    rows.append((_t("ux.proj.field.source"),
                 html.escape(language_label(source) or "—")
                 + f"<span class='ms-ux-facts-note'>{html.escape(_t('ux.proj.source.fixed'))}</span>"))
    targets = canonical_langs(meta.get("target_langs") or [])
    rows.append((_t("ux.proj.field.targets"),
                 html.escape(", ".join(language_label(c) for c in targets) or _t("ux.proj.targets.none"))))
    body = "".join(f"<dt>{html.escape(k)}</dt><dd>{v}</dd>" for k, v in rows)
    return f"<dl class='ms-ux-facts'>{body}</dl>"


def add_result_notice(plan: LanguageAddition) -> str:
    """What was added, or why nothing was."""
    if plan.invalid:
        return ux.notice(ux.WARNING, _t("ux.proj.add.invalid.title"), _t("ux.proj.add.invalid.body"),
                         items=list(plan.invalid), action=_t("ux.proj.add.invalid.todo"))
    skipped = ([_t("ux.proj.skip.source", language=language_label(c)) for c in plan.source]
               + [_t("ux.proj.skip.present", language=language_label(c)) for c in plan.already])
    if not plan.add and not skipped:
        return ux.notice(ux.WARNING, _t("ux.proj.add.none.title"), _t("ux.proj.add.none.body"))
    if not plan.add:
        return ux.notice(ux.INFO, _t("ux.proj.add.nothing.title"), items=skipped)
    done = ux.notice(ux.SUCCESS, _tn("ux.proj.add.done.title", len(plan.add)), _t("ux.proj.add.done.body"),
                     items=[language_label(c) for c in plan.add])
    return done + (ux.notice(ux.INFO, _t("ux.proj.skip.title"), items=skipped) if skipped else "")


def remove_none_notice() -> str:
    return ux.notice(ux.WARNING, _t("ux.proj.remove.none.title"), _t("ux.proj.remove.none.body"))


def remove_not_target_notice(code: str) -> str:
    return ux.notice(ux.WARNING, _t("ux.proj.remove.not_target.title"),
                     _t("ux.proj.remove.not_target.body", language=language_label(code) or str(code)))


def remove_blocked_notice(usage: LanguageUsage) -> str:
    return ux.notice(ux.BLOCKED, _t("ux.proj.remove.blocked.title", language=language_label(usage.language)),
                     _t("ux.proj.remove.blocked.body"), items=usage_lines(usage),
                     action=_t("ux.proj.remove.blocked.todo"))


def remove_confirm_notice(code: str) -> str:
    return ux.notice(ux.WARNING, _t("ux.proj.remove.confirm.title", language=language_label(code)),
                     f"{_t('ux.proj.remove.confirm.body')} {_t('ux.proj.remove.confirm.todo')}")


def remove_done_notice(code: str) -> str:
    return ux.notice(ux.SUCCESS, _t("ux.proj.remove.done.title"),
                     _t("ux.proj.remove.done.body", language=language_label(code)))


def check_error_notice(code: str, message: str) -> str:
    return ux.notice(ux.ERROR, _t("ux.proj.check_error.title"),
                     _t("ux.proj.check_error.body", language=language_label(code)), details=message)


def save_error_notice(message: str) -> str:
    return ux.notice(ux.ERROR, _t("ux.proj.save_error.title"), _t("ux.proj.save_error.body"), details=message)


# ======================================================================
# Release -- the publishing profile preview and export need
# ======================================================================
def profile_id_for(name: str, existing: Iterable[str]) -> str:
    """A readable, unused profile id from the name the user typed:
    'Standard' -> PROF-STANDARD, then PROF-STANDARD-2, ... The user never
    has to invent or see it."""
    ascii_name = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    base = "PROF-" + (re.sub(r"[^A-Za-z0-9]+", "-", ascii_name).strip("-").upper() or "PROFILE")
    taken = {str(x) for x in existing}
    pid, n = base, 2
    while pid in taken:
        pid, n = f"{base}-{n}", n + 1
    return pid


def no_profile_notice() -> str:
    return ux.notice(ux.WARNING, _t("ux.prof.none.title"), _t("ux.prof.none.body"), action=_t("ux.prof.none.todo"))


def profile_form_notice(dimension_names: list[str], hidden_names: list[str]) -> str:
    body = _t("ux.prof.form.dims") if dimension_names or hidden_names else _t("ux.prof.form.nodims")
    if hidden_names:
        body = f"{body} {_t('ux.prof.form.more', names=', '.join(hidden_names))}"
    return ux.notice(ux.INFO, _t("ux.prof.form.title"), body)


def profile_no_actor_notice(field: str) -> str:
    return ux.notice(ux.WARNING, _t("ux.prof.no_actor.title"), _t("ux.prof.no_actor.body", field=field))


def profile_no_name_notice() -> str:
    return ux.notice(ux.WARNING, _t("ux.prof.no_name.title"), _t("ux.prof.no_name.body"))


def profile_error_notice(message: str) -> str:
    return ux.notice(ux.ERROR, _t("ux.prof.error.title"), _t("ux.prof.error.body"), details=message)


def profile_done_notice(name: str) -> str:
    return ux.notice(ux.SUCCESS, _t("ux.prof.done.title", name=name), _t("ux.prof.done.body"))
