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

from pathlib import Path
from typing import Any

from ccms_industrial import IndustrialRepository
from authoring_service import AuthoringService, EDITABLE_TEXT_BLOCK_TYPES

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


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


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


def outline_rows(repo: IndustrialRepository, publication_id: str, *, include_archived: bool = False) -> list[dict[str, Any]]:
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
        })
        block_sql = "SELECT * FROM content_blocks WHERE topic_id=?"
        if not include_archived:
            block_sql += " AND archived_at IS NULL"
        block_sql += " ORDER BY position,block_id"
        for b in repo.conn.execute(block_sql, (tid,)):
            editable = b["block_type"] in EDITABLE_TEXT_BLOCK_TYPES
            rows.append({
                "kind": "block", "id": b["block_id"], "topic_id": tid, "block_type": b["block_type"],
                "type_label": block_type_label(b["block_type"], editable=editable),
                "preview": _block_preview(b["text_content"]) if editable else block_type_label(b["block_type"], editable=editable),
                "status": b["status"], "status_label": status_label(b["status"]), "version": int(b["version"]),
                "archived": bool(b["archived_at"]), "editable": editable,
            })
    return rows


def outline_table(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Presentation rows for a read-only overview gr.Dataframe."""
    out = []
    for r in rows:
        if r["kind"] == "topic":
            out.append({
                "Elemento": r["title"], "Tipo": "Topic", "Stato": r["status_label"],
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
            out.append((f"{r['title']}{flag}{status}", f"topic:{r['id']}"))
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
    return {
        **data,
        "status_label": status_label(data["status"]),
        "has_draft": draft is not None,
        "draft_notice": ("È presente una bozza non salvata." if draft else ""),
        "can_submit_for_review": data["status"] == "Draft" and not data["archived"],
        "open_review": open_review,
        "open_review_notice": (f"Review #{open_review['review_id']} aperta, in attesa di decisione." if open_review else ""),
    }


def block_detail(service: AuthoringService, block_id: str) -> dict[str, Any]:
    data = service.load_block_for_edit(block_id)
    draft = data.get("draft")
    open_review = service.open_review_for("block", block_id)
    editable = data["block_type"] in EDITABLE_TEXT_BLOCK_TYPES
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
    }


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
    block_sql = """SELECT b.block_id,b.topic_id,b.block_type,b.text_content,b.status,t.title AS topic_title
                   FROM content_blocks b JOIN topics t ON t.topic_id=b.topic_id
                   WHERE b.topic_id IN (SELECT pt.topic_id FROM publication_topics pt JOIN topics tt ON tt.topic_id=pt.topic_id
                                        WHERE pt.publication_id=? AND tt.archived_at IS NULL)
                   AND b.archived_at IS NULL ORDER BY b.topic_id,b.position"""
    for b in repo.conn.execute(block_sql, (publication_id,)):
        if b["block_type"] not in EDITABLE_TEXT_BLOCK_TYPES:
            continue
        text = b["text_content"] or ""
        if q in text.lower():
            out.append({"kind": "block", "id": b["block_id"], "topic_id": b["topic_id"],
                        "label": f"{b['topic_title']} — {_block_preview(text)}",
                        "context": f"{block_type_label(b['block_type'], editable=True)} — {status_label(b['status'])}"})
    return out


def conflict_message(detail: str = "") -> str:
    return (detail or "Questo contenuto è stato modificato da un'altra sessione. "
            "La tua bozza è stata conservata. Ricarica la versione corrente prima di decidere come procedere.")


def archived_message(kind: str, object_id: str) -> str:
    label = "topic" if kind == "topic" else "blocco"
    return f"Questo {label} è archiviato: ripristinalo prima di modificarlo."
