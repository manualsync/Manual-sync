from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pandas as pd

from ccms_industrial import IndustrialRepository

IDML_HARDENING_BUILD = "0.21.0-idml-template-hardening-20260810"
SLOT_RE = re.compile(r"\[\[MS:SLOT:([A-Za-z0-9_.:-]+)\]\]")


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def _canon_text(text: str) -> str:
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n")


def _text_of_paragraph(p: ET.Element) -> str:
    return "".join((c.text or "") for c in p.iter("Content"))


def _block_text(row: pd.Series) -> str:
    bt = str(row.get("block_type", "paragraph"))
    content = row.get("content", "")
    if bt == "table" and isinstance(content, dict):
        lines = []
        if content.get("caption"):
            lines.append(str(content["caption"]))
        cols = content.get("columns", []) or []
        if cols:
            lines.append("\t".join(map(str, cols)))
        lines.extend("\t".join(map(str, rr)) for rr in (content.get("rows", []) or []))
        return "\r".join(lines)
    if bt == "figure" and isinstance(content, dict):
        return f"[FIGURE:{content.get('figure_id','')}] {content.get('caption','')}"
    prefix = {"warning":"WARNING: ", "note":"NOTE: ", "list_item":"• "}.get(bt, "")
    return prefix + str(content)


def inspect_idml(idml_path: str | Path) -> dict[str, Any]:
    path = Path(idml_path)
    if not zipfile.is_zipfile(path):
        raise ValueError("IDML non valido: il file non è un package ZIP leggibile")
    with zipfile.ZipFile(path, "r") as zf:
        names = zf.namelist()
        stories = []
        placeholders = []
        paragraph_styles_used = set()
        story_paragraphs = 0
        for name in sorted(n for n in names if n.startswith("Stories/") and n.endswith(".xml")):
            try:
                root = ET.fromstring(zf.read(name))
            except Exception:
                continue
            pcount = 0
            for p in root.iter("ParagraphStyleRange"):
                pcount += 1; story_paragraphs += 1
                paragraph_styles_used.add(p.attrib.get("AppliedParagraphStyle", ""))
                text = _text_of_paragraph(p)
                for m in SLOT_RE.finditer(text):
                    placeholders.append({"slot":m.group(1), "story":name, "paragraph_style":p.attrib.get("AppliedParagraphStyle", ""), "text":text})
            stories.append({"story":name,"paragraphs":pcount})

        styles_defined = set()
        if "Resources/Styles.xml" in names:
            try:
                root = ET.fromstring(zf.read("Resources/Styles.xml"))
                for el in root.iter():
                    self_id = el.attrib.get("Self", "")
                    if self_id.startswith("ParagraphStyle/"):
                        styles_defined.add(self_id)
            except Exception:
                pass
        links = [n for n in names if n.startswith("Links/")]
        spreads = [n for n in names if n.startswith("Spreads/") and n.endswith(".xml")]
        fonts = [n for n in names if "Font" in n or n.startswith("Resources/Fonts")]
    return {
        "path": str(path),
        "package_parts": len(names),
        "stories": stories,
        "story_count": len(stories),
        "story_paragraphs": story_paragraphs,
        "slots": placeholders,
        "slot_count": len(placeholders),
        "paragraph_styles_used": sorted(x for x in paragraph_styles_used if x),
        "paragraph_styles_defined": sorted(styles_defined),
        "link_parts": len(links),
        "spread_count": len(spreads),
        "font_parts": len(fonts),
        "preflight_pass": bool(stories and placeholders),
        "limitations": [
            "v0.21 preserves the supplied IDML template; it does not reconstruct arbitrary InDesign layouts from scratch.",
            "Tables are still injected as structured tab-delimited story content unless a future native-table mapping is configured.",
            "Figures require pre-existing/template placement logic for native InDesign links; v0.21 preserves and reports existing links rather than inventing geometry.",
        ],
    }


def _style_for(block_type: str, slot_style: str, style_map: dict[str, str], styles_defined: set[str]) -> str:
    desired = style_map.get(block_type) or style_map.get("body") or slot_style or "ParagraphStyle/$ID/[Basic Paragraph]"
    if styles_defined and desired not in styles_defined:
        fallback = slot_style if slot_style in styles_defined else "ParagraphStyle/$ID/[Basic Paragraph]"
        return fallback
    return desired


def patch_idml_slots(
    repo: IndustrialRepository,
    publication_id: str,
    template_idml: str | Path,
    out_idml: str | Path,
    *,
    profile_id: str | None = None,
    dimensions: dict[str, Any] | None = None,
    target_lang: str | None = None,
    slot_map: dict[str, str] | None = None,
    style_map: dict[str, str] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Patch one or more named IDML story slots while preserving untouched parts.

    slot_map maps slot name -> topic_id or "*" for whole publication.
    A bridge manifest records exact paragraph/block mappings for a later round-trip
    audit. No automatic Adobe geometry creation is claimed.
    """
    template_idml = Path(template_idml); out_idml = Path(out_idml)
    pre = inspect_idml(template_idml)
    if not pre["preflight_pass"]:
        raise ValueError("IDML preflight fallito: serve almeno uno Story slot [[MS:SLOT:name]]")

    frame, pub_summary = repo.assemble_structured_publication(publication_id, profile_id=profile_id, dimensions=dimensions, target_lang=target_lang)
    slots_available = {x["slot"] for x in pre["slots"]}
    if slot_map is None:
        if len(slots_available) == 1:
            slot_map = {next(iter(slots_available)): "*"}
        else:
            raise ValueError("Template multi-slot: specificare slot_map")
    unknown = set(slot_map) - slots_available
    if unknown:
        raise ValueError("Slot non presenti nel template: " + ", ".join(sorted(unknown)))

    tmp = Path(tempfile.mkdtemp(prefix="manualsync_idml_v21_"))
    with zipfile.ZipFile(template_idml, "r") as zin:
        zin.extractall(tmp)
        original_hashes = {n:_sha_bytes(zin.read(n)) for n in zin.namelist()}

    styles_defined = set(pre["paragraph_styles_defined"])
    style_map = dict(style_map or {})
    changed_stories = set()
    manifest_rows = []

    for story_info in pre["slots"]:
        slot = story_info["slot"]
        if slot not in slot_map:
            continue
        story_name = story_info["story"]
        story_path = tmp / story_name
        tree = ET.parse(story_path); root = tree.getroot()
        target_para = None
        for p in root.iter("ParagraphStyleRange"):
            if f"[[MS:SLOT:{slot}]]" in _text_of_paragraph(p):
                target_para = p; break
        if target_para is None:
            raise ValueError(f"Slot {slot} perso durante il parsing")
        children = list(root)
        if target_para not in children:
            # Fixture and common IDML Stories place ParagraphStyleRange directly under Story.
            # If nested in an unsupported structure, stop rather than corrupt the package.
            raise ValueError(f"Slot {slot} è in una struttura Story non supportata in v0.21")
        insert_at = children.index(target_para)
        slot_style = target_para.attrib.get("AppliedParagraphStyle", "")
        root.remove(target_para)

        topic_filter = slot_map[slot]
        selected = frame if topic_filter == "*" else frame[frame["topic_id"].astype(str).eq(str(topic_filter))]
        if selected.empty:
            # Keep an explicit empty marker to preserve story flow and make the issue visible.
            selected_records = []
            paragraphs = [("[ManualSync: no content for slot]", "empty", "")]
        else:
            selected_records = selected.to_dict(orient="records")
            paragraphs = []
            last_topic = None
            for rec in selected_records:
                if rec["topic_id"] != last_topic:
                    paragraphs.append((str(rec["topic_title"]), "heading", f"TOPIC:{rec['topic_id']}"))
                    last_topic = rec["topic_id"]
                paragraphs.append((_block_text(pd.Series(rec)), str(rec["block_type"]), str(rec["block_id"])))

        for offset, (text, kind, object_id) in enumerate(paragraphs):
            pstyle = _style_for(kind, slot_style, style_map, styles_defined)
            p = ET.Element("ParagraphStyleRange", {"AppliedParagraphStyle": pstyle})
            ch = ET.SubElement(p, "CharacterStyleRange", {"AppliedCharacterStyle":"CharacterStyle/$ID/[No character style]"})
            c = ET.SubElement(ch, "Content"); c.text = text
            root.insert(insert_at + offset, p)
            manifest_rows.append({
                "slot":slot,"story":story_name,"paragraph_offset":offset,"kind":kind,
                "object_id":object_id,"text_sha256":hashlib.sha256(_canon_text(text).encode("utf-8")).hexdigest(),"style":pstyle,
            })
        tree.write(story_path, encoding="UTF-8", xml_declaration=True)
        changed_stories.add(story_name)

    manifest = {
        "build": IDML_HARDENING_BUILD,
        "publication_id": publication_id,
        "profile_id": profile_id or "",
        "target_lang": target_lang or "",
        "slot_map": slot_map,
        "style_map": style_map,
        "preflight": pre,
        "paragraph_map": manifest_rows,
        "changed_stories": sorted(changed_stories),
        "publication_summary": pub_summary,
    }
    (tmp / "ManualSyncBridgeV21.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    out_idml.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_idml, "w") as zout:
        mime = tmp / "mimetype"
        if mime.exists():
            zout.write(mime, "mimetype", compress_type=zipfile.ZIP_STORED)
        for p in sorted(tmp.rglob("*")):
            if not p.is_file() or p == mime:
                continue
            zout.write(p, str(p.relative_to(tmp)).replace(os.sep,"/"), compress_type=zipfile.ZIP_DEFLATED)
    shutil.rmtree(tmp, ignore_errors=True)

    # Integrity audit: untouched original package parts must remain byte-identical.
    with zipfile.ZipFile(out_idml, "r") as zf:
        out_hashes = {n:_sha_bytes(zf.read(n)) for n in zf.namelist()}
    untouched_mismatches = []
    for name, old_hash in original_hashes.items():
        if name in changed_stories:
            continue
        if out_hashes.get(name) != old_hash:
            untouched_mismatches.append(name)
    manifest["untouched_parts_preserved"] = len(untouched_mismatches) == 0
    manifest["untouched_mismatches"] = untouched_mismatches
    return out_idml, manifest


def audit_idml_roundtrip(idml_path: str | Path) -> pd.DataFrame:
    """Compare exported Story text against the ManualSync bridge manifest.

    This does not replace opening the file in Adobe InDesign. It provides a
    deterministic text/mapping integrity audit before and after a human InDesign
    editing round-trip.
    """
    path = Path(idml_path)
    if not zipfile.is_zipfile(path):
        raise ValueError("IDML non valido")
    with zipfile.ZipFile(path, "r") as zf:
        if "ManualSyncBridgeV21.json" not in zf.namelist():
            raise ValueError("Manifest ManualSync v0.21 non presente")
        manifest = json.loads(zf.read("ManualSyncBridgeV21.json"))
        story_cache: dict[str, list[str]] = {}
        rows = []
        for m in manifest.get("paragraph_map", []):
            story = m["story"]
            if story not in story_cache:
                root = ET.fromstring(zf.read(story))
                story_cache[story] = [_text_of_paragraph(p) for p in root.iter("ParagraphStyleRange")]
            texts = story_cache[story]
            # Paragraph offsets are relative to insertion point, not absolute. Find
            # by hash to tolerate template paragraphs before/after the slot.
            matches = [i for i,t in enumerate(texts) if hashlib.sha256(_canon_text(t).encode("utf-8")).hexdigest() == m["text_sha256"]]
            rows.append({
                "slot":m["slot"],"story":story,"kind":m["kind"],"object_id":m["object_id"],
                "expected_sha256":m["text_sha256"],"matching_paragraphs":len(matches),
                "status":"UNCHANGED" if matches else "CHANGED_OR_MISSING",
            })
    return pd.DataFrame(rows)


def create_multislot_fixture(out_path: str | Path) -> Path:
    """Regression fixture only — not a production InDesign template."""
    out_path = Path(out_path); out_path.parent.mkdir(parents=True, exist_ok=True)
    main_story = '''<?xml version="1.0" encoding="UTF-8"?><Story Self="main"><ParagraphStyleRange AppliedParagraphStyle="ParagraphStyle/MS Body"><CharacterStyleRange AppliedCharacterStyle="CharacterStyle/$ID/[No character style]"><Content>[[MS:SLOT:INSTALL]]</Content></CharacterStyleRange></ParagraphStyleRange></Story>'''
    service_story = '''<?xml version="1.0" encoding="UTF-8"?><Story Self="service"><ParagraphStyleRange AppliedParagraphStyle="ParagraphStyle/MS Body"><CharacterStyleRange AppliedCharacterStyle="CharacterStyle/$ID/[No character style]"><Content>[[MS:SLOT:SERVICE]]</Content></CharacterStyleRange></ParagraphStyleRange></Story>'''
    styles = '''<?xml version="1.0" encoding="UTF-8"?><Root><ParagraphStyle Self="ParagraphStyle/$ID/[Basic Paragraph]"/><ParagraphStyle Self="ParagraphStyle/MS Body"/><ParagraphStyle Self="ParagraphStyle/MS Heading"/><ParagraphStyle Self="ParagraphStyle/MS Warning"/><ParagraphStyle Self="ParagraphStyle/MS Note"/></Root>'''
    design = '''<?xml version="1.0" encoding="UTF-8"?><Document xmlns:idPkg="http://ns.adobe.com/AdobeInDesign/idml/1.0/packaging"><idPkg:Story src="Stories/Story_main.xml"/><idPkg:Story src="Stories/Story_service.xml"/></Document>'''
    with zipfile.ZipFile(out_path, "w") as zf:
        zf.writestr("mimetype","application/vnd.adobe.indesign-idml-package",compress_type=zipfile.ZIP_STORED)
        zf.writestr("designmap.xml",design)
        zf.writestr("Resources/Styles.xml",styles)
        zf.writestr("Stories/Story_main.xml",main_story)
        zf.writestr("Stories/Story_service.xml",service_story)
        zf.writestr("Spreads/Spread_u1.xml","<Spread Self=\"u1\"/>")
        zf.writestr("Links/README.txt","fixture link part preserved")
    return out_path
