"""Milestone 4B.2: view-model adapter between AuthoringService
(authoring_service.py) and the Gradio UI in app.py.

Pure Python -- no gradio import here, so every function is testable without
a UI runtime. Exactly the compliance_view.py contract: this module never
duplicates a business rule. Status/version/archive truth always comes from
IndustrialRepository/AuthoringService; a save/reorder/archive/revert is
always validated by AuthoringService itself, never re-validated here. The
only thing this module adds is presentation: friendly Italian labels for
technical status/type values, an outline flattened for a simple two-level
picker (topics, then their blocks), breadcrumbs, draft/conflict/review
messages, and a small local text search -- assembling read-only getters
into one screen's worth of data.
"""
from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

from ccms_industrial import IndustrialRepository
from authoring_service import AuthoringService, EDITABLE_TEXT_BLOCK_TYPES, _SHARED_OBJECT_MESSAGE

STATUS_LABELS = {"Draft": "Bozza", "In Review": "In revisione", "Approved": "Approvato",
                 "Migrated": "Importato", "Published": "Pubblicato"}

BLOCK_TYPE_LABELS = {
    "paragraph": "Paragrafo", "note": "Nota", "warning": "Avviso", "list_item": "Elemento lista",
    "table": "Tabella", "figure": "Figura", "component_ref": "Componente riusabile", "code": "Codice",
}

# Non-editable-here object types get an explicit, honest label instead of a
# silently disabled control -- the editor must never let a generic textarea
# corrupt a shared table/figure/component object (see 4A architecture gate).
_NOT_YET_EDITABLE_SUFFIX = " — modifica disponibile nel prossimo incremento"

# 4C: table/figure blocks get their OWN dedicated editor (not the generic
# textarea EDITABLE_TEXT_BLOCK_TYPES governs) -- kept as a separate set so
# the text-block guard in AuthoringService.save_block stays exactly as
# strict as it already was; this is a presentation-layer concern only
# (which outline rows should show a real preview instead of the "not yet
# editable" suffix, and which block types the Studio UI should route to a
# dedicated editor rather than the plain text box).
DEDICATED_EDITOR_BLOCK_TYPES = {"table", "figure"}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def evaluate_summary_text(result: dict[str, Any]) -> str:
    """Human-language rendering of AuthoringService.evaluate_element_with_
    profile()'s result for the Studio's 'Valuta contenuto con questo
    profilo' mini-tool. 4E.1 stabilization: the ORIGINAL wording here --
    'Escluso: non soddisfa Mercato.' / 'Incluso (soddisfa Mercato).' --
    was logically correct but read like an evaluator dump (bare dimension
    names, no sense of the actual mismatch) rather than something a
    normal technical author would write. This instead names the profile
    and explains what the content is actually FOR, using result[
    'condition_description'] (from AuthoringService.describe_condition(),
    already resolved to human dimension/value NAMES -- never raw ids,
    never JSON, never operator terminology) that evaluate_element_with_
    profile() now carries alongside the match/fail/unresolved id lists."""
    profile_name = result.get("profile_name") or result.get("profile_id") or "il profilo"
    desc = result.get("condition_description") or {}
    dims_by_id = {d["dimension_id"]: d for d in desc.get("dimensions", [])}

    def _phrase(ids: list[str]) -> str:
        parts = []
        for i in ids:
            d = dims_by_id.get(i)
            if not d:
                continue
            labels = " o ".join(v["label"] for v in d.get("values", []))
            parts.append(f"{d['name']} = {labels}")
        return " · ".join(parts)

    if result.get("unresolved_dimensions"):
        return "⚠️ Non valutabile — la variante usa un criterio o valore non più disponibile."
    if result["included"]:
        matched = _phrase(result.get("matched_dimensions") or [])
        base = f"✅ Incluso nel profilo «{profile_name}»"
        return f"{base} — soddisfa {matched}." if matched else f"{base} — nessuna condizione impostata."
    failed = _phrase(result.get("failed_dimensions") or [])
    base = f"⛔ Non incluso nel profilo «{profile_name}»"
    return f"{base} — questo contenuto è destinato a {failed}." if failed else f"{base}."


def condition_summary_text(describe: dict[str, Any]) -> str:
    """Human, non-technical summary from AuthoringService.describe_condition()
    -- e.g. 'Visibile per: Mercato = EU o UK · Edizione = Service'. Standard
    mode NEVER shows ids/JSON: only dimension/value NAMES, already resolved
    by describe_condition(). A condition too complex for the simplified
    editor to decompile gets an honest "avanzata" message instead of a
    guessed/wrong summary (4E.1's UX acceptance criterion: never require
    understanding syntax in standard mode)."""
    if describe.get("too_complex"):
        return "Condizione avanzata (non gestibile in modalità standard)."
    parts = []
    for dim in describe.get("dimensions", []):
        labels = " o ".join(v["label"] for v in dim.get("values", []))
        if labels:
            parts.append(f"{dim['name']} = {labels}")
    if not parts:
        return "Sempre visibile (nessuna condizione)."
    return "Visibile per: " + " · ".join(parts)


def block_type_label(block_type: str, *, editable: bool) -> str:
    base = BLOCK_TYPE_LABELS.get(block_type, block_type)
    return base if editable else base + _NOT_YET_EDITABLE_SUFFIX


def ccms_db_path(project_dir: str | Path) -> Path:
    base = Path(project_dir)
    for name in ("ManualSync_CCMS.sqlite", "ManualSync_CCMS_v18.sqlite"):
        p = base / name
        if p.exists():
            return p
    raise FileNotFoundError("Repository CCMS non trovato nel progetto")


def open_service(project_dir: str | Path) -> tuple[IndustrialRepository, AuthoringService]:
    repo = IndustrialRepository(ccms_db_path(project_dir))
    return repo, AuthoringService(repo)


def publication_options(repo: IndustrialRepository) -> list[tuple[str, str]]:
    """(label, publication_id) pairs -- same Gradio Dropdown (label, value)
    convention as compliance_view.scope_options(). ManualSync does not have
    a second "open document" concept: this lists the same `publications`
    table the Compliance/Release tabs already read."""
    out = []
    for r in repo.conn.execute("SELECT publication_id,title FROM publications ORDER BY publication_id"):
        out.append((f"{r['publication_id']} — {r['title']}", r["publication_id"]))
    return out


def _block_preview(text: str, *, width: int = 70) -> str:
    flat = " ".join((text or "").split())
    return flat[:width] + ("…" if len(flat) > width else "") if flat else "(vuoto)"


def _table_preview(repo: IndustrialRepository, table_id: str, *, width: int = 70) -> str:
    row = repo.conn.execute("SELECT title,caption FROM structured_tables WHERE table_id=?", (table_id,)).fetchone()
    if not row:
        return "(tabella non trovata)"
    label = row["caption"] or row["title"]
    return _block_preview(label, width=width) if label else "(tabella senza titolo)"


def _figure_preview(repo: IndustrialRepository, figure_id: str, *, width: int = 70) -> str:
    row = repo.conn.execute("SELECT title,caption FROM figures WHERE figure_id=?", (figure_id,)).fetchone()
    if not row:
        return "(figura non trovata)"
    label = row["caption"] or row["title"]
    return _block_preview(label, width=width) if label else "(figura senza didascalia)"


def _reuse_reference_count(repo: IndustrialRepository, block_type: str, object_ref_id: str) -> int:
    """4D: same block-level-precise count as AuthoringService._reference_count
    (duplicated here, not imported, because this is a read-only repo-level
    query used purely for the outline's '· riutilizzata N volte' presentation
    suffix -- authoring_view.py never imports AuthoringService's private
    helpers, and the outline is built from `repo` alone, no open service
    instance)."""
    row = repo.conn.execute(
        "SELECT COUNT(*) AS c FROM content_blocks WHERE block_type=? AND object_ref_id=?",
        (block_type, object_ref_id),
    ).fetchone()
    return int(row["c"])


_OUTLINE_SUFFIX_MAX_LEN = 40


def _compact_condition_suffix(service: AuthoringService | None, condition_json: str | None) -> str:
    """Compact, human, zero-training outline differentiator -- e.g.
    ' · Mercato EU', ' · Mercato EU/UK', ' · Mercato EU · Edizione
    Service'. Two neighboring rows conditioned on different values
    (e.g. an EU paragraph and a US paragraph) used to both show the
    exact same generic ' · variante' suffix, making them look identical
    at a glance -- this shows the actual differentiator instead.

    Falls back to ' · variante' when no service is available to resolve
    dimension/value NAMES (never shows raw ids) or when the condition is
    too complex for this simplified renderer to represent compactly, and
    to a plain count (' · 2 condizioni') when it decompiles fine but has
    more dimensions than fit on one outline row or would make the label
    too long -- the full, unambiguous summary is always one click away
    in the element's own Varianti panel, never lost, only compacted."""
    if condition_json in (None, "", "{}"):
        return ""
    if service is None:
        return " · variante"
    try:
        condition = json.loads(condition_json)
    except (TypeError, ValueError):
        return " · variante"
    desc = service.describe_condition(condition)
    dims = desc.get("dimensions", [])
    if desc.get("too_complex") or not dims:
        return " · variante"
    if len(dims) > 2:
        return f" · {len(dims)} condizioni"
    parts = [f"{d['name']} {'/'.join(v['label'] for v in d['values'])}" for d in dims]
    suffix = " · " + " · ".join(parts)
    if len(suffix) > _OUTLINE_SUFFIX_MAX_LEN:
        return f" · {len(dims)} condizioni"
    return suffix


def outline_rows(repo: IndustrialRepository, publication_id: str, *, include_archived: bool = False,
                 service: AuthoringService | None = None) -> list[dict[str, Any]]:
    """Flat, ordered rows: one per active topic (or archived too, if
    requested), each immediately followed by its own blocks in the same
    order assemble_structured_publication() would read them. This is the
    single source both the outline Dataframe and the 'Apri elemento'
    picker are built from."""
    rows: list[dict[str, Any]] = []
    if include_archived:
        topic_links = repo.conn.execute(
            "SELECT topic_id FROM publication_topics WHERE publication_id=? ORDER BY position", (publication_id,)
        ).fetchall()
    else:
        topic_links = repo.conn.execute(
            """SELECT pt.topic_id FROM publication_topics pt JOIN topics t ON t.topic_id=pt.topic_id
               WHERE pt.publication_id=? AND t.archived_at IS NULL ORDER BY pt.position""",
            (publication_id,),
        ).fetchall()
    for link in topic_links:
        tid = link["topic_id"]
        t = repo.conn.execute("SELECT * FROM topics WHERE topic_id=?", (tid,)).fetchone()
        if not t:
            continue
        archived = bool(t["archived_at"])
        rows.append({
            "kind": "topic", "id": tid, "topic_id": tid, "title": t["title"], "status": t["status"],
            "status_label": status_label(t["status"]), "version": int(t["version"]), "archived": archived,
            # 4E.1: cheap string check (condition_json is canonical JSON, so
            # "no condition" is always exactly "{}") -- avoids a JSON parse
            # per outline row just to know whether ANY condition exists.
            "has_condition": t["condition_json"] not in (None, "", "{}"),
            "condition_suffix": _compact_condition_suffix(service, t["condition_json"]),
        })
        block_sql = "SELECT * FROM content_blocks WHERE topic_id=?"
        if not include_archived:
            block_sql += " AND archived_at IS NULL"
        block_sql += " ORDER BY position,block_id"
        for b in repo.conn.execute(block_sql, (tid,)):
            bt = b["block_type"]
            editable_text = bt in EDITABLE_TEXT_BLOCK_TYPES
            has_dedicated_editor = bt in DEDICATED_EDITOR_BLOCK_TYPES
            editable = editable_text or has_dedicated_editor
            # 4D bug fix (found during browser stabilization): for a table/
            # figure block, content_blocks.version is the WRAPPING block's
            # own structural counter (bumped by e.g. fork_table_local's
            # rewire) -- NOT the table/figure object's real content
            # version that the editor header, table_history/figure_
            # history, and revert all operate on. Showing the block's
            # version here was silently misleading: right after a fork,
            # a freshly-created (content version 1) table could show
            # "Versione 2" in this outline merely because the wrapping
            # block had been rewired once. Look up the OBJECT's own
            # version for table/figure rows so this column always means
            # the same thing everywhere in the UI.
            display_version = int(b["version"])
            if bt == "table":
                preview = _table_preview(repo, b["object_ref_id"])
                rc = _reuse_reference_count(repo, "table", b["object_ref_id"])
                if rc > 1:
                    preview = f"{preview} · riutilizzata {rc} volte"
                trow = repo.conn.execute("SELECT version FROM structured_tables WHERE table_id=?", (b["object_ref_id"],)).fetchone()
                if trow is not None:
                    display_version = int(trow["version"])
            elif bt == "figure":
                preview = _figure_preview(repo, b["object_ref_id"])
                rc = _reuse_reference_count(repo, "figure", b["object_ref_id"])
                if rc > 1:
                    preview = f"{preview} · riutilizzata {rc} volte"
                frow = repo.conn.execute("SELECT version FROM figures WHERE figure_id=?", (b["object_ref_id"],)).fetchone()
                if frow is not None:
                    display_version = int(frow["version"])
            elif editable_text:
                preview = _block_preview(b["text_content"])
            elif bt == "component_ref":
                # 4D Blocker B (component scope decision, Option 1): no
                # Studio editor exists for components, so this NEVER
                # becomes 'editable' -- the "modifica disponibile nel
                # prossimo incremento" suffix stays. But the reuse count
                # IS real and worth surfacing here exactly like table/
                # figure, since it's just as easy for a writer to
                # accidentally treat a shared component_ref as if it were
                # private to one topic.
                preview = block_type_label(bt, editable=False)
                rc = _reuse_reference_count(repo, "component_ref", b["object_ref_id"])
                if rc > 1:
                    preview = f"{preview} · riutilizzata {rc} volte"
            else:
                preview = block_type_label(bt, editable=False)
            has_condition = b["condition_json"] not in (None, "", "{}")
            preview = f"{preview}{_compact_condition_suffix(service, b['condition_json'])}"
            rows.append({
                "kind": "block", "id": b["block_id"], "topic_id": tid, "block_type": bt,
                "object_ref_id": b["object_ref_id"],
                "type_label": block_type_label(bt, editable=editable),
                "preview": preview,
                "status": b["status"], "status_label": status_label(b["status"]), "version": display_version,
                "archived": bool(b["archived_at"]), "editable": editable,
                "has_dedicated_editor": has_dedicated_editor,
                "has_condition": has_condition,
            })
    return rows


def outline_table(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Presentation rows for a read-only overview gr.Dataframe."""
    out = []
    for r in rows:
        if r["kind"] == "topic":
            label = r["title"] + r.get("condition_suffix", "")
            out.append({
                "Elemento": label, "Tipo": "Topic", "Stato": r["status_label"],
                "Versione": r["version"], "Archiviato": "Sì" if r["archived"] else "No",
            })
        else:
            out.append({
                "Elemento": f"    {r['preview']}", "Tipo": r["type_label"], "Stato": r["status_label"],
                "Versione": r["version"], "Archiviato": "Sì" if r["archived"] else "No",
            })
    return out


def open_choices(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(label, 'kind:id') pairs for the manual-structure picker (a single
    click opens the item) -- topics as top-level rows, blocks indented
    beneath with a human type label + short preview, same composite
    'kind:id' value convention app.py's Compliance tab already uses for its
    requirement picker. Status is shown only when it isn't the common
    Draft/'Bozza' case -- a status badge on every single row is noise, not
    signal ("stato solo se utile")."""
    out = []
    for r in rows:
        flag = " (archiviato)" if r["archived"] else ""
        status = f"  · {r['status_label']}" if r["status_label"] != "Bozza" else ""
        if r["kind"] == "topic":
            variant = r.get("condition_suffix", "")
            out.append((f"{r['title']}{variant}{flag}{status}", f"topic:{r['id']}"))
        else:
            out.append((f"    ↳ {r['type_label']}: {r['preview']}{flag}{status}", f"block:{r['id']}"))
    return out


def parse_open_value(value: str | None) -> tuple[str, str] | tuple[None, None]:
    v = str(value or "").strip()
    if ":" not in v:
        return None, None
    kind, _, obj_id = v.partition(":")
    if kind not in ("topic", "block"):
        return None, None
    return kind, obj_id


def topic_ids_in_order(rows: list[dict[str, Any]], *, active_only: bool = True) -> list[str]:
    return [r["id"] for r in rows if r["kind"] == "topic" and (not active_only or not r["archived"])]


def block_ids_in_order(rows: list[dict[str, Any]], topic_id: str, *, active_only: bool = True) -> list[str]:
    return [r["id"] for r in rows if r["kind"] == "block" and r["topic_id"] == topic_id and (not active_only or not r["archived"])]


def topic_detail(service: AuthoringService, topic_id: str) -> dict[str, Any]:
    data = service.load_topic_for_edit(topic_id)
    draft = data.get("draft")
    open_review = service.open_review_for("topic", topic_id)
    condition_desc = service.describe_condition(data.get("condition"))
    return {
        **data,
        "status_label": status_label(data["status"]),
        "has_draft": draft is not None,
        "draft_notice": ("È presente una bozza non salvata." if draft else ""),
        "can_submit_for_review": data["status"] == "Draft" and not data["archived"],
        "open_review": open_review,
        "open_review_notice": (f"Review #{open_review['review_id']} aperta, in attesa di decisione." if open_review else ""),
        "condition_summary": condition_summary_text(condition_desc),
        "condition_dimensions": condition_desc.get("dimensions", []),
        "condition_always_included": condition_desc.get("always_included", True),
    }


def block_detail(service: AuthoringService, block_id: str) -> dict[str, Any]:
    data = service.load_block_for_edit(block_id)
    draft = data.get("draft")
    open_review = service.open_review_for("block", block_id)
    editable = data["block_type"] in EDITABLE_TEXT_BLOCK_TYPES
    condition_desc = service.describe_condition(data.get("condition"))
    return {
        **data,
        "status_label": status_label(data["status"]),
        "type_label": block_type_label(data["block_type"], editable=editable),
        "editable": editable,
        "has_draft": draft is not None,
        "draft_notice": ("È presente una bozza non salvata." if draft else ""),
        "can_submit_for_review": editable and data["status"] == "Draft" and not data["archived"],
        "open_review": open_review,
        "open_review_notice": (f"Review #{open_review['review_id']} aperta, in attesa di decisione." if open_review else ""),
        "condition_summary": condition_summary_text(condition_desc),
        "condition_dimensions": condition_desc.get("dimensions", []),
        "condition_always_included": condition_desc.get("always_included", True),
    }


def table_detail(service: AuthoringService, table_id: str) -> dict[str, Any]:
    data = service.load_table_for_edit(table_id)
    draft = data.get("draft")
    open_review = service.open_review_for("table", table_id)
    impact = service.get_reuse_impact("table", table_id)
    return {
        **data,
        "status_label": status_label(data["status"]),
        "has_draft": draft is not None,
        "draft_notice": ("È presente una bozza non salvata." if draft else ""),
        "can_submit_for_review": data["status"] == "Draft" and not data["archived"] and data["reference_count"] <= 1,
        "open_review": open_review,
        "open_review_notice": (f"Review #{open_review['review_id']} aperta, in attesa di decisione." if open_review else ""),
        "is_shared": data["reference_count"] > 1,
        "shared_notice": (_SHARED_OBJECT_MESSAGE if data["reference_count"] > 1 else ""),
        "reuse_impact": impact,
        "reuse_impact_summary": reuse_impact_summary(impact),
    }


def figure_detail(service: AuthoringService, figure_id: str) -> dict[str, Any]:
    data = service.load_figure_for_edit(figure_id)
    draft = data.get("draft")
    open_review = service.open_review_for("figure", figure_id)
    impact = service.get_reuse_impact("figure", figure_id)
    return {
        **data,
        "status_label": status_label(data["status"]),
        "has_draft": draft is not None,
        "draft_notice": ("È presente una bozza non salvata." if draft else ""),
        "can_submit_for_review": data["status"] == "Draft" and not data["archived"] and data["reference_count"] <= 1,
        "open_review": open_review,
        "open_review_notice": (f"Review #{open_review['review_id']} aperta, in attesa di decisione." if open_review else ""),
        "is_shared": data["reference_count"] > 1,
        "shared_notice": (_SHARED_OBJECT_MESSAGE if data["reference_count"] > 1 else ""),
        "reuse_impact": impact,
        "reuse_impact_summary": reuse_impact_summary(impact),
    }


def table_history_rows(service: AuthoringService, table_id: str) -> list[dict[str, Any]]:
    out = []
    for h in service.table_history(table_id):
        out.append({
            "Versione": h["version"], "Quando": (h["created_at"] or "")[:16].replace("T", " "),
            "Stato al salvataggio": status_label(h["status"]), "Autore": h["actor"] or "—",
            "Anteprima": _block_preview(h["caption"] or h["title"], width=50),
        })
    return out


def figure_history_rows(service: AuthoringService, figure_id: str) -> list[dict[str, Any]]:
    out = []
    for h in service.figure_history(figure_id):
        out.append({
            "Versione": h["version"], "Quando": (h["created_at"] or "")[:16].replace("T", " "),
            "Stato al salvataggio": status_label(h["status"]), "Autore": h["actor"] or "—",
            "Anteprima": _block_preview(h["caption"] or h["title"], width=50),
        })
    return out


def impact_summary(repo: IndustrialRepository, object_type: str, object_id: str) -> list[str]:
    """Advanced-mode-only 'where is this reused' listing (topic titles),
    built from the EXISTING impact_map_industrial() -- never a second
    impact-tracing implementation. Deduplicates by topic title since
    impact_map_industrial() is topic/publication-row granularity (a topic
    in two publications yields two rows) and this is purely informational,
    not the shared-object gate itself (see AuthoringService._reference_count,
    which is block-level and is what actually decides the fail-closed
    guard)."""
    try:
        df = repo.impact_map_industrial(object_id, kind=object_type)
    except Exception:
        return []
    if df is None or df.empty or "topic_title" not in df.columns:
        return []
    seen: list[str] = []
    for v in df["topic_title"].tolist():
        if v and v not in seen:
            seen.append(str(v))
    return seen


def reuse_impact_summary(impact: dict[str, Any]) -> dict[str, Any]:
    """4D: formats AuthoringService.get_reuse_impact()'s raw dict into the
    exact panel text the shared-object decision UX needs -- a usage-count
    line and a readable, deduplicated list of where the object is used.
    Purely presentation over the service's own block-level-precise
    primitive; never a second impact-tracing implementation."""
    count = int(impact.get("reference_count") or 0)
    topics = impact.get("topics") or []
    topic_lines = [f"· {t['title']}" for t in topics]
    archived = int(impact.get("archived_reference_count") or 0)
    usage_line = "Utilizzato in 1 punto." if count == 1 else f"Utilizzato in {count} punti."
    notes = []
    if archived:
        notes.append(
            "1 di questi riferimenti si trova in un blocco archiviato." if archived == 1
            else f"{archived} di questi riferimenti si trovano in blocchi archiviati."
        )
    return {
        "is_shared": bool(impact.get("is_shared")),
        "header": "Questo elemento è utilizzato in più punti." if impact.get("is_shared") else "",
        "usage_line": usage_line,
        "topic_lines": topic_lines,
        "notes": notes,
    }


def diff_text(old: str | None, new: str | None) -> dict[str, Any]:
    """Word-level diff for topic/text-block revision compare, using the
    standard-library difflib.SequenceMatcher -- no new dependency. Returns
    both a segment list (tagged equal/insert/delete, for a highlighted-diff
    UI) and an Italian human-readable summary."""
    old_words = (old or "").split()
    new_words = (new or "").split()
    sm = difflib.SequenceMatcher(a=old_words, b=new_words, autojunk=False)
    added = removed = 0
    segments: list[dict[str, str]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            segments.append({"tag": "equal", "text": " ".join(new_words[j1:j2])})
        elif tag == "insert":
            segments.append({"tag": "insert", "text": " ".join(new_words[j1:j2])})
            added += j2 - j1
        elif tag == "delete":
            segments.append({"tag": "delete", "text": " ".join(old_words[i1:i2])})
            removed += i2 - i1
        elif tag == "replace":
            segments.append({"tag": "delete", "text": " ".join(old_words[i1:i2])})
            segments.append({"tag": "insert", "text": " ".join(new_words[j1:j2])})
            removed += i2 - i1
            added += j2 - j1
    if not added and not removed:
        summary = "Nessuna modifica al testo."
    else:
        parts = []
        if added:
            parts.append(f"{added} parol{'a' if added == 1 else 'e'} aggiunt{'a' if added == 1 else 'e'}")
        if removed:
            parts.append(f"{removed} parol{'a' if removed == 1 else 'e'} rimoss{'a' if removed == 1 else 'e'}")
        summary = (", ".join(parts) + ".")[0].upper() + (", ".join(parts) + ".")[1:]
    return {"changed": bool(added or removed), "segments": segments, "words_added": added,
            "words_removed": removed, "summary": summary}


def diff_table(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Deterministic, list-order-preserving comparison of two table
    versions (as returned by AuthoringService.get_table_version /
    load_table_for_edit): columns added/removed, rows added/removed, and
    how many individual cells changed between rows that align at the same
    position in both versions."""
    old_cols = list(old.get("columns") or [])
    new_cols = list(new.get("columns") or [])
    old_rows = list(old.get("rows") or [])
    new_rows = list(new.get("rows") or [])

    col_sm = difflib.SequenceMatcher(a=old_cols, b=new_cols, autojunk=False)
    columns_added = columns_removed = 0
    for tag, i1, i2, j1, j2 in col_sm.get_opcodes():
        if tag in ("insert", "replace"):
            columns_added += j2 - j1
        if tag in ("delete", "replace"):
            columns_removed += i2 - i1

    old_keys = [json.dumps(r, sort_keys=True, ensure_ascii=False) for r in old_rows]
    new_keys = [json.dumps(r, sort_keys=True, ensure_ascii=False) for r in new_rows]
    row_sm = difflib.SequenceMatcher(a=old_keys, b=new_keys, autojunk=False)
    rows_added = rows_removed = cells_changed = 0
    for tag, i1, i2, j1, j2 in row_sm.get_opcodes():
        if tag == "insert":
            rows_added += j2 - j1
        elif tag == "delete":
            rows_removed += i2 - i1
        elif tag == "replace":
            n = min(i2 - i1, j2 - j1)
            for k in range(n):
                old_r, new_r = old_rows[i1 + k], new_rows[j1 + k]
                for c in range(max(len(old_r), len(new_r))):
                    ov = old_r[c] if c < len(old_r) else None
                    nv = new_r[c] if c < len(new_r) else None
                    if ov != nv:
                        cells_changed += 1
            rows_removed += (i2 - i1) - n
            rows_added += (j2 - j1) - n

    title_changed = (old.get("title") or "") != (new.get("title") or "")
    caption_changed = (old.get("caption") or "") != (new.get("caption") or "")

    parts = []
    if columns_added:
        parts.append(f"{columns_added} colonn{'a aggiunta' if columns_added == 1 else 'e aggiunte'}")
    if columns_removed:
        parts.append(f"{columns_removed} colonn{'a rimossa' if columns_removed == 1 else 'e rimosse'}")
    if rows_added:
        parts.append(f"{rows_added} rig{'a aggiunta' if rows_added == 1 else 'he aggiunte'}")
    if rows_removed:
        parts.append(f"{rows_removed} rig{'a rimossa' if rows_removed == 1 else 'he rimosse'}")
    if cells_changed:
        parts.append(f"{cells_changed} cell{'a modificata' if cells_changed == 1 else 'e modificate'}")
    if title_changed and caption_changed:
        parts.append("titolo e didascalia modificati")
    elif title_changed:
        parts.append("titolo modificato")
    elif caption_changed:
        parts.append("didascalia modificata")
    changed = bool(parts)
    summary = ((", ".join(parts) + ".")[0].upper() + (", ".join(parts) + ".")[1:]) if changed \
        else "Nessuna modifica alla tabella."
    return {"changed": changed, "columns_added": columns_added, "columns_removed": columns_removed,
            "rows_added": rows_added, "rows_removed": rows_removed, "cells_changed": cells_changed,
            "title_changed": title_changed, "caption_changed": caption_changed, "summary": summary}


def diff_figure(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Field-level comparison for a figure revision: 'Immagine sostituita'
    is a far more useful signal for a technical writer than a raw
    file_path string diff, plus title/caption/alt-text changes."""
    image_changed = (old.get("file_path") or "") != (new.get("file_path") or "")
    title_changed = (old.get("title") or "") != (new.get("title") or "")
    caption_changed = (old.get("caption") or "") != (new.get("caption") or "")
    alt_changed = (old.get("alt_text") or "") != (new.get("alt_text") or "")
    parts = []
    if image_changed:
        parts.append("Immagine sostituita")
    if title_changed:
        parts.append("titolo modificato")
    if caption_changed:
        parts.append("didascalia modificata")
    if alt_changed:
        parts.append("testo alternativo modificato")
    changed = bool(parts)
    summary = ((", ".join(parts) + ".")[0].upper() + (", ".join(parts) + ".")[1:]) if changed \
        else "Nessuna modifica alla figura."
    return {"changed": changed, "image_changed": image_changed, "title_changed": title_changed,
            "caption_changed": caption_changed, "alt_text_changed": alt_changed, "summary": summary}


def topic_history_rows(service: AuthoringService, topic_id: str) -> list[dict[str, Any]]:
    out = []
    for h in service.topic_history(topic_id):
        out.append({
            "Versione": h["version"], "Quando": (h["created_at"] or "")[:16].replace("T", " "),
            "Stato al salvataggio": status_label(h["status"]), "Autore": h["actor"] or "—",
            "Anteprima titolo": _block_preview(h["title"], width=50),
        })
    return out


def block_history_rows(service: AuthoringService, block_id: str) -> list[dict[str, Any]]:
    out = []
    for h in service.block_history(block_id):
        out.append({
            "Versione": h["version"], "Quando": (h["created_at"] or "")[:16].replace("T", " "),
            "Stato al salvataggio": status_label(h["status"]), "Autore": h["actor"] or "—",
            "Anteprima testo": _block_preview(h["text"], width=50),
        })
    return out


def breadcrumb(rows: list[dict[str, Any]], kind: str, object_id: str) -> str:
    if kind == "topic":
        t = next((r for r in rows if r["kind"] == "topic" and r["id"] == object_id), None)
        return f"Pubblicazione › {t['title']}" if t else "Pubblicazione"
    b = next((r for r in rows if r["kind"] == "block" and r["id"] == object_id), None)
    if not b:
        return "Pubblicazione"
    t = next((r for r in rows if r["kind"] == "topic" and r["id"] == b["topic_id"]), None)
    tt = t["title"] if t else b["topic_id"]
    return f"Pubblicazione › {tt} › {b['type_label']}"


def next_previous_topic(rows: list[dict[str, Any]], topic_id: str) -> tuple[str | None, str | None]:
    ids = topic_ids_in_order(rows)
    if topic_id not in ids:
        return None, None
    i = ids.index(topic_id)
    prev_id = ids[i - 1] if i > 0 else None
    next_id = ids[i + 1] if i < len(ids) - 1 else None
    return prev_id, next_id


def search(repo: IndustrialRepository, publication_id: str, query: str) -> list[dict[str, Any]]:
    """Simple local, case-insensitive substring search across active topic
    titles and active editable-block text -- adequate for a small/medium
    manual (see 4A architecture note: no FTS5 in this increment). Never
    searches archived content."""
    q = str(query or "").strip().lower()
    if not q:
        return []
    rows = outline_rows(repo, publication_id, include_archived=False)
    out = []
    for r in rows:
        if r["kind"] == "topic" and q in r["title"].lower():
            out.append({"kind": "topic", "id": r["id"], "topic_id": r["id"],
                        "label": r["title"], "context": f"Topic — {status_label(r['status'])}"})
    # Block text is searched against the live, untruncated text_content
    # (not outline_rows()'s already-truncated preview) so a match past the
    # preview boundary is never missed.
    block_sql = """SELECT b.block_id,b.topic_id,b.block_type,b.text_content,b.object_ref_id,b.status,t.title AS topic_title
                   FROM content_blocks b JOIN topics t ON t.topic_id=b.topic_id
                   WHERE b.topic_id IN (SELECT pt.topic_id FROM publication_topics pt JOIN topics tt ON tt.topic_id=pt.topic_id
                                        WHERE pt.publication_id=? AND tt.archived_at IS NULL)
                   AND b.archived_at IS NULL ORDER BY b.topic_id,b.position"""
    for b in repo.conn.execute(block_sql, (publication_id,)):
        bt = b["block_type"]
        if bt in EDITABLE_TEXT_BLOCK_TYPES:
            text = b["text_content"] or ""
            if q in text.lower():
                out.append({"kind": "block", "id": b["block_id"], "topic_id": b["topic_id"],
                            "label": f"{b['topic_title']} — {_block_preview(text)}",
                            "context": f"{block_type_label(bt, editable=True)} — {status_label(b['status'])}"})
        elif bt == "table":
            t = repo.conn.execute(
                "SELECT title,caption,rows_json FROM structured_tables WHERE table_id=?", (b["object_ref_id"],)
            ).fetchone()
            if not t:
                continue
            cells_text = " ".join(str(c) for row in _load_json_safe(t["rows_json"], []) for c in row)
            haystack = f"{t['title']} {t['caption']} {cells_text}".lower()
            if q in haystack:
                out.append({"kind": "block", "id": b["block_id"], "topic_id": b["topic_id"],
                            "label": f"{b['topic_title']} — {_block_preview(t['caption'] or t['title'])}",
                            "context": f"Tabella — {status_label(b['status'])}"})
        elif bt == "figure":
            f = repo.conn.execute(
                "SELECT title,caption,alt_text FROM figures WHERE figure_id=?", (b["object_ref_id"],)
            ).fetchone()
            if not f:
                continue
            haystack = f"{f['title']} {f['caption']} {f['alt_text']}".lower()
            if q in haystack:
                out.append({"kind": "block", "id": b["block_id"], "topic_id": b["topic_id"],
                            "label": f"{b['topic_title']} — {_block_preview(f['caption'] or f['title'])}",
                            "context": f"Figura — {status_label(b['status'])}"})
    return out


def _load_json_safe(value: str | None, default):
    try:
        return json.loads(value or "")
    except Exception:
        return default


def conflict_message(detail: str = "") -> str:
    return (detail or "Questo contenuto è stato modificato da un'altra sessione. "
            "La tua bozza è stata conservata. Ricarica la versione corrente prima di decidere come procedere.")


def archived_message(kind: str, object_id: str) -> str:
    label = "topic" if kind == "topic" else "blocco"
    return f"Questo {label} è archiviato: ripristinalo prima di modificarlo."
