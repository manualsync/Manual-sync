from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from rapidfuzz.fuzz import ratio

from cat_tm import split_sentences, technical_signature
from ccms_industrial import IndustrialRepository
import pdf_backend

GROUNDING_BUILD = "0.31.0-grounded-authoring-multilingual-conflict-20260813"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(text: str) -> str:
    text = (text or "").casefold().replace("−", "-").replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-zA-ZÀ-ÿ0-9._/-]{2,}", _norm(text))}


def _technical_payload(text: str) -> dict[str, list[str]]:
    sig = technical_signature(text)
    return {"numbers": list(sig.numbers), "codes": list(sig.codes), "units": list(sig.units)}


# Retrieval and request-safety helpers added in v0.30.  The goal is deliberately
# deterministic: cross-language terminology normalization, relevance filtering,
# request/source conflict detection and unsupported technical-value detection all
# happen before any provider is allowed to compose a draft.
_ALIAS_GROUPS: dict[str, tuple[str, ...]] = {
    "fuse": ("fuse", "fuses", "fusibile", "fusibili"),
    "circuit_breaker": ("circuit breaker", "circuit breakers", "interruttore automatico", "interruttori automatici"),
    "tightening_torque": ("tightening torque", "torque", "coppia di serraggio", "coppia serraggio"),
    "pressure": ("pressure", "pressione"),
    "temperature": ("temperature", "temperatura"),
    "clearance": ("clearance", "distanza libera", "spazio libero"),
    "voltage": ("voltage", "tensione"),
    "current": ("current", "corrente"),
    "frequency": ("frequency", "frequenza"),
    "warning": ("warning", "avvertenza"),
}

_QUERY_STOPWORDS = {
    # English request boilerplate
    "a","an","and","about","all","be","by","do","from","generate","give","in","indexed","information",
    "instruction","instructions","is","are","was","were","it","not","of","only","or","present","provide","request","required","related","used",
    "source","stating","summarize","summary","technical","that","the","to","use","using","with","write",
    "explicitly","supported","add","values","requirements",
    # Italian request boilerplate
    "a","ai","al","alla","alle","anche","che","con","dalla","dalle","dei","del","della","delle","di","e",
    "esclusivamente","fonte","indicizzata","informazioni","istruzioni","modo","nei","nel","nella","non","o",
    "presenti","requisiti","riassumi","scrivi","sia","sui","sul","tecnico","testo","usa","utilizza","valori","relative","relativo","conciso","concisa",
}

_MODAL_WORDS = {"may","can","could","must","shall","should","not","never","without","allowed","required","prohibited","forbidden"}


def _canonical_text(text: str) -> str:
    out = _norm(text)
    # Longest aliases first avoids replacing "circuit breaker" as two unrelated words.
    pairs=[]
    for canonical, aliases in _ALIAS_GROUPS.items():
        for alias in aliases:
            pairs.append((alias.casefold(), canonical))
    for alias, canonical in sorted(pairs, key=lambda x: len(x[0]), reverse=True):
        out = re.sub(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", canonical, out, flags=re.I)
    return out


def _topic_terms(text: str) -> set[str]:
    canon = _canonical_text(text)
    terms = {w for w in re.findall(r"[a-zA-ZÀ-ÿ0-9_+-]{2,}", canon)}
    return {w for w in terms if w not in _QUERY_STOPWORDS and w not in _MODAL_WORDS and not re.fullmatch(r"\d+(?:[.,]\d+)?", w)}


def _technical_pairs(text: str) -> list[str]:
    unit = r"(?:n\s*[·.]?\s*m|nm|bar|psi|kv|mv|v|ma|a|hz|khz|kw|mw|w|mm|cm|kg|°c|°f|%|rpm)"
    out=[]
    for m in re.finditer(rf"(?<![\w.])[-+]?\d+(?:[.,]\d+)?\s*{unit}(?!\w)", text or "", flags=re.I):
        val = re.sub(r"\s+", "", m.group(0)).replace(",", ".").casefold().replace("n·m", "nm").replace("n.m", "nm")
        out.append(val)
    return list(dict.fromkeys(out))


def _polarity(text: str) -> str:
    """Return normative polarity in English or Italian.

    Request/source conflict detection must operate after cross-language retrieval,
    otherwise an Italian request can retrieve the right English evidence but still
    bypass the contradiction gate. Keep this deterministic and conservative.
    """
    t=_canonical_text(text)
    # Explicit prohibitions first: they must win over generic modal matches.
    if re.search(
        r"\b(?:must|shall|may|can)\s+not\b|\b(?:do|does)\s+not\b|\bnever\b|\bprohibited\b|\bforbidden\b"
        r"|\bnon\s+(?:deve|devono|pu[oò]|possono)\b|\b(?:vietato|vietata|vietati|vietate)\b"
        r"|\bnon\s+(?:è|e')\s+consentit[oaie]\b|\bnon\s+sono\s+consentit[ie]\b",
        t, flags=re.I
    ):
        return "PROHIBITION"
    # Permission / possibility. Handles e.g. "possono essere utilizzati senza fusibili".
    if re.search(
        r"\b(?:may|can|is allowed to|are allowed to)\b"
        r"|\b(?:pu[oò]|possono)\s+(?:essere\s+)?(?:usat[oaie]|utilizzat[oaie]|impiegat[oaie])\b"
        r"|\b(?:è|e')\s+consentit[oa]\b|\bsono\s+consentit[ie]\b|\bsi\s+possono\s+(?:usare|utilizzare|impiegare)\b",
        t, flags=re.I
    ):
        return "PERMISSION"
    if re.search(
        r"\b(?:must|shall|required to|is required|are required)\b"
        r"|\b(?:deve|devono)\b|\b(?:è|e')\s+(?:necessario|necessaria|richiesto|richiesta)\b",
        t, flags=re.I
    ):
        return "REQUIREMENT"
    return "NEUTRAL"


def _request_clauses(text: str) -> list[str]:
    raw=re.sub(r"\s+", " ", text or "").strip()
    if not raw:
        return []
    parts=re.split(r"(?<=[.!?;])\s+|\s+(?:and that|e che)\s+", raw, flags=re.I)
    return [p.strip(" .;:") for p in parts if p.strip(" .;:")]


class GroundingStore:
    """Evidence store and deterministic grounding guard.

    v0.19 deliberately does not call a generative model. It builds the safety
    layer first: source ingestion, retrieval, extractive draft creation, claim
    validation, uncertainty flags and safe insertion into the CCMS. A future LLM
    may propose wording, but it must pass the same guard before approval.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._schema()

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS source_documents (
                document_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_path TEXT NOT NULL DEFAULT '',
                sha256 TEXT NOT NULL,
                approved INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_chunks (
                chunk_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                text TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                page INTEGER NOT NULL DEFAULT 0,
                technical_json TEXT NOT NULL,
                approved INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY(document_id) REFERENCES source_documents(document_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS ix_chunks_doc ON source_chunks(document_id,ordinal);
            CREATE TABLE IF NOT EXISTS grounding_audits (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                draft_sha256 TEXT NOT NULL,
                draft_text TEXT NOT NULL,
                cited_chunks_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def ingest_text(self, document_id: str, title: str, text: str, *, source_ref_prefix: str | None = None, approved: bool = True) -> pd.DataFrame:
        content = re.sub(r"\s+", " ", text or "").strip()
        if not content:
            raise ValueError("Fonte vuota")
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        now = _now()
        self.conn.execute(
            "INSERT OR REPLACE INTO source_documents(document_id,title,source_type,source_path,sha256,approved,created_at) VALUES(?,?,?,?,?,?,?)",
            (document_id, title, "text", "", sha, int(bool(approved)), now),
        )
        self.conn.execute("DELETE FROM source_chunks WHERE document_id=?", (document_id,))
        sentences = split_sentences(content)
        if not sentences:
            sentences = [content]
        rows = []
        for i, sentence in enumerate(sentences, start=1):
            cid = f"{document_id}#C{i:04d}"
            ref = f"{source_ref_prefix or document_id}#chunk={i}"
            tech = _technical_payload(sentence)
            self.conn.execute(
                "INSERT INTO source_chunks(chunk_id,document_id,ordinal,text,source_ref,page,technical_json,approved,created_at) VALUES(?,?,?,?,?,0,?,?,?)",
                (cid, document_id, i, sentence, ref, json.dumps(tech, ensure_ascii=False), int(bool(approved)), now),
            )
            rows.append({"chunk_id":cid,"text":sentence,"source_ref":ref,**tech})
        self.conn.commit()
        return pd.DataFrame(rows)

    def ingest_pdf(self, document_id: str, title: str, pdf_path: str | Path, *, approved: bool = True) -> pd.DataFrame:
        pdf_path = Path(pdf_path)
        sha = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        now = _now()
        self.conn.execute(
            "INSERT OR REPLACE INTO source_documents(document_id,title,source_type,source_path,sha256,approved,created_at) VALUES(?,?,?,?,?,?,?)",
            (document_id, title, "pdf", str(pdf_path), sha, int(bool(approved)), now),
        )
        self.conn.execute("DELETE FROM source_chunks WHERE document_id=?", (document_id,))
        pages = pdf_backend.default_reader().iter_page_texts(pdf_path)
        rows=[]; ordinal=0
        try:
            for page_no, raw_text in enumerate(pages, start=1):
                page_text = re.sub(r"\s+", " ", raw_text or "").strip()
                if not page_text:
                    continue
                for sentence in split_sentences(page_text) or [page_text]:
                    if len(sentence.strip()) < 4:
                        continue
                    ordinal += 1
                    cid=f"{document_id}#C{ordinal:04d}"
                    ref=f"{pdf_path.name}#page={page_no}#chunk={ordinal}"
                    tech=_technical_payload(sentence)
                    self.conn.execute(
                        "INSERT INTO source_chunks(chunk_id,document_id,ordinal,text,source_ref,page,technical_json,approved,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (cid,document_id,ordinal,sentence,ref,page_no,json.dumps(tech,ensure_ascii=False),int(bool(approved)),now),
                    )
                    rows.append({"chunk_id":cid,"page":page_no,"text":sentence,"source_ref":ref,**tech})
        finally:
            pages.close()
        self.conn.commit()
        return pd.DataFrame(rows)

    def retrieve(self, query: str, *, top_k: int = 8, approved_only: bool = True) -> pd.DataFrame:
        qn=_canonical_text(query); qw=_topic_terms(query); qtech=technical_signature(query)
        rows=[]
        sql="SELECT c.*,d.title FROM source_chunks c JOIN source_documents d ON d.document_id=c.document_id"
        if approved_only:
            sql += " WHERE c.approved=1 AND d.approved=1"
        for r in self.conn.execute(sql):
            tn=_canonical_text(r["text"]); tw=_topic_terms(r["text"])
            fuzzy=ratio(qn,tn)/100.0 if qn and tn else 0.0
            hits=qw & tw
            # Coverage against the query, not the union, prevents long source chunks
            # from being unfairly penalized and gives bilingual aliases equal weight.
            coverage=len(hits)/max(1,len(qw))
            sig=technical_signature(r["text"])
            tech_bonus=0.0
            if qtech.codes and set(qtech.codes)&set(sig.codes): tech_bonus += 0.20
            if qtech.numbers and set(qtech.numbers)&set(sig.numbers): tech_bonus += 0.12
            if qtech.units and set(qtech.units)&set(sig.units): tech_bonus += 0.05
            phrase_bonus=0.0
            if "circuit_breaker" in qw and "circuit_breaker" in tw: phrase_bonus += 0.12
            if "fuse" in qw and "fuse" in tw: phrase_bonus += 0.10
            score=min(1.0, 0.28*fuzzy + 0.55*coverage + tech_bonus + phrase_bonus)
            rows.append({
                "chunk_id":r["chunk_id"],"document_id":r["document_id"],"title":r["title"],"source_ref":r["source_ref"],
                "page":r["page"],"score":round(score*100,1),"topic_hits":len(hits),
                "query_terms":" | ".join(sorted(qw)),"matched_terms":" | ".join(sorted(hits)),"text":r["text"]
            })
        rows.sort(key=lambda x:(-x["topic_hits"],-x["score"],x["chunk_id"]))
        return pd.DataFrame(rows[:max(1,int(top_k))])

    def audit_request(self, instruction: str, evidence: pd.DataFrame | None = None) -> dict[str, Any]:
        """Validate the *request* before generation.

        A source-bounded generator can still hide a dangerous request by silently
        correcting it. ManualSync instead surfaces request/source conflicts and
        unsupported technical values explicitly and blocks the proposal.
        """
        if evidence is None:
            evidence=self.retrieve(instruction,top_k=12)
        if evidence is None or evidence.empty:
            return {"query_relevance_pass":False,"conflicts":[],"unsupported":[],"topic_terms":sorted(_topic_terms(instruction))}
        relevant=evidence[evidence.get("topic_hits",pd.Series([0]*len(evidence)))>0] if "topic_hits" in evidence.columns else evidence
        if relevant.empty:
            relevant=evidence.head(3)
        conflicts=[]
        for clause in _request_clauses(instruction):
            cp=_polarity(clause)
            if cp not in {"PERMISSION","PROHIBITION","REQUIREMENT"}:
                continue
            ct=_topic_terms(clause)
            if not ct:
                continue
            for _,r in relevant.iterrows():
                st=_topic_terms(str(r["text"]))
                # Conflict matching should compare the technical concepts in the
                # clause, not request boilerplate such as "genera", "affermando",
                # "possono essere utilizzati". This is essential for cross-language
                # requests where lexical overlap is intentionally low.
                known=set(_ALIAS_GROUPS.keys())
                ct_core=ct & known
                st_core=st & known
                if ct_core:
                    overlap=len(ct_core & st_core)/max(1,len(ct_core))
                    if overlap < 0.50:
                        continue
                else:
                    overlap=len(ct&st)/max(1,len(ct))
                    if overlap < 0.50:
                        continue
                sp=_polarity(str(r["text"]))
                opposite=(cp=="PERMISSION" and sp=="PROHIBITION") or (cp=="PROHIBITION" and sp=="PERMISSION")
                if opposite:
                    conflicts.append({
                        "claim_id":f"REQ-CONFLICT-{len(conflicts)+1:02d}","claim":clause,"status":"REQUEST_SOURCE_CONFLICT",
                        "source_ref":str(r["source_ref"]),"evidence":str(r["text"]),"support_score":float(r.get("score",0.0))
                    })
                    break
        unsupported=[]
        ev_text=" ".join(str(x) for x in relevant["text"].tolist())
        ev_norm=re.sub(r"\s+","",ev_text).replace(",",".").casefold().replace("n·m","nm").replace("n.m","nm")
        for pair in _technical_pairs(instruction):
            if pair not in ev_norm:
                unsupported.append({
                    "claim_id":f"REQ-UNSUPPORTED-{len(unsupported)+1:02d}","claim":pair,"status":"UNSUPPORTED_REQUEST_TECHNICAL_CLAIM",
                    "source_ref":"","evidence":"Technical value/unit pair not found in relevant evidence","support_score":0.0
                })
        qsig=technical_signature(instruction)
        ev_sig=technical_signature(ev_text)
        for code in sorted(set(qsig.codes)-set(ev_sig.codes)):
            unsupported.append({
                "claim_id":f"REQ-UNSUPPORTED-{len(unsupported)+1:02d}","claim":code,"status":"UNSUPPORTED_REQUEST_CODE",
                "source_ref":"","evidence":"Model/code not found in relevant evidence","support_score":0.0
            })
        return {
            "query_relevance_pass": bool(len(relevant) and (int(relevant.iloc[0].get("topic_hits",0))>0 or float(relevant.iloc[0].get("score",0))>=35)),
            "conflicts":conflicts,"unsupported":unsupported,"topic_terms":sorted(_topic_terms(instruction)),
        }

    def chunk_texts(self, chunk_ids: Iterable[str]) -> list[sqlite3.Row]:
        out=[]
        for cid in chunk_ids:
            r=self.conn.execute("SELECT * FROM source_chunks WHERE chunk_id=?",(cid,)).fetchone()
            if r: out.append(r)
        return out

    def extractive_draft(self, instruction: str, *, top_k: int = 4, min_score: float = 30.0) -> dict[str, Any]:
        hits=self.retrieve(instruction,top_k=top_k)
        selected=hits[hits["score"]>=float(min_score)] if not hits.empty else hits
        if selected.empty:
            return {"draft":"","citations":[],"status":"NEEDS_SOURCE","retrieval":hits}
        # Deduplicate while preserving retrieved order; no invented wording.
        seen=set(); sentences=[]; citations=[]
        for _,r in selected.iterrows():
            n=_norm(r["text"])
            if n in seen: continue
            seen.add(n); sentences.append(str(r["text"])); citations.append(str(r["chunk_id"]))
        return {"draft":" ".join(sentences),"citations":citations,"status":"EXTRACTIVE_GROUNDED","retrieval":hits}

    def validate_draft(self, draft: str, cited_chunk_ids: Iterable[str], *, min_claim_support: float = 55.0) -> dict[str, Any]:
        cited=self.chunk_texts(cited_chunk_ids)
        cited_text=" ".join(r["text"] for r in cited)
        cited_sig=technical_signature(cited_text)
        cited_numbers=set(cited_sig.numbers); cited_codes=set(cited_sig.codes); cited_units=set(cited_sig.units)
        rows=[]; all_pass=True
        claims=split_sentences(draft) or ([draft.strip()] if draft.strip() else [])
        for i,claim in enumerate(claims,start=1):
            sig=technical_signature(claim)
            missing_numbers=sorted(set(sig.numbers)-cited_numbers)
            missing_codes=sorted(set(sig.codes)-cited_codes)
            # Units may occur without values in natural language; require source support if present.
            missing_units=sorted(set(sig.units)-cited_units)
            best_score=0.0; best_chunk=""; best_ref=""
            for r in cited:
                score=ratio(_norm(claim),_norm(r["text"]))
                # token overlap helps when wording is lightly edited.
                cw=_words(claim); sw=_words(r["text"])
                overlap=100*len(cw&sw)/max(1,len(cw))
                score=max(score,overlap)
                if score>best_score:
                    best_score=score; best_chunk=r["chunk_id"]; best_ref=r["source_ref"]
            tech_ok=not(missing_numbers or missing_codes or missing_units)
            support_ok=best_score>=float(min_claim_support)
            status="PASS" if tech_ok and support_ok else "UNSUPPORTED"
            if status!="PASS": all_pass=False
            rows.append({
                "claim_id":f"CLM-{i:03d}","claim":claim,"best_chunk":best_chunk,"source_ref":best_ref,
                "support_score":round(best_score,1),"technical_ok":tech_ok,"missing_numbers":" | ".join(missing_numbers),
                "missing_codes":" | ".join(missing_codes),"missing_units":" | ".join(missing_units),"status":status,
            })
        frame=pd.DataFrame(rows)
        result={"PASS":bool(all_pass and len(rows)>0),"claims":frame,"cited_chunks":[r["chunk_id"] for r in cited],"source_refs":[r["source_ref"] for r in cited]}
        serial={**result,"claims":frame.to_dict(orient="records")}
        self.conn.execute(
            "INSERT INTO grounding_audits(draft_sha256,draft_text,cited_chunks_json,result_json,created_at) VALUES(?,?,?,?,?)",
            (hashlib.sha256((draft or '').encode('utf-8')).hexdigest(),draft,json.dumps(result["cited_chunks"],ensure_ascii=False),json.dumps(serial,ensure_ascii=False),_now()),
        )
        self.conn.commit()
        return result

    def add_grounded_block(self, repo: IndustrialRepository, *, block_id: str, topic_id: str, block_type: str,
                           position: int, draft: str, cited_chunk_ids: Iterable[str], status: str = "Draft",
                           condition: dict[str, Any] | None = None) -> str:
        check=self.validate_draft(draft,cited_chunk_ids)
        if not check["PASS"]:
            raise ValueError("Contenuto non inserito: grounding check fallito")
        return repo.add_block(block_id,topic_id,block_type,position=position,text=draft,status=status,
                              condition=condition or {},source_refs=check["source_refs"],evidence_status="SOURCE_GROUNDED")

    def uncertainty_report(self, instruction: str, *, top_k: int = 5) -> dict[str, Any]:
        hits=self.retrieve(instruction,top_k=top_k)
        best=float(hits.iloc[0]["score"]) if len(hits) else 0.0
        if best>=70: level="LOW"
        elif best>=45: level="MEDIUM"
        else: level="HIGH"
        return {"uncertainty":level,"best_support_score":best,"candidates":hits}
