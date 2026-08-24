from __future__ import annotations

import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

from grounded_authoring import GroundingStore
from ccms_industrial import IndustrialRepository

GENERATION_BUILD = "0.30.0-generative-orchestrator-request-safety-20260813"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@dataclass
class GenerationRequest:
    instruction: str
    mode: str = "procedure"
    max_evidence: int = 6
    min_retrieval_score: float = 25.0
    min_claim_support: float = 55.0


class GeneratorProvider(Protocol):
    name: str

    def generate(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class SafeSourceComposer:
    """Offline provider used for deterministic testing and safe operation.

    This is intentionally conservative: it composes only retrieved source text.
    It proves the orchestration contract without pretending to be a free-form LLM.
    A real model can be connected through ExternalJsonCommandProvider and must pass
    the exact same grounding gate.
    """

    name = "safe-source-composer"

    def generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        evidence = payload.get("evidence", [])
        mode = str(payload.get("mode", "procedure"))
        selected = [e for e in evidence if str(e.get("text", "")).strip()]
        if not selected:
            return {"draft": "", "citations": [], "provider_meta": {"reason": "no-evidence"}}

        citations = [str(e["chunk_id"]) for e in selected]
        texts = [str(e["text"]).strip() for e in selected]
        if mode == "warning":
            # Preserve source wording; only add a structural label if absent.
            draft = " ".join(texts[:2])
            if not draft.upper().startswith(("WARNING", "CAUTION", "DANGER", "AVVERTENZA", "ATTENZIONE", "PERICOLO")):
                draft = "WARNING: " + draft
        elif mode == "summary":
            draft = " ".join(texts[:3])
        elif mode == "paragraph":
            draft = " ".join(texts[:2])
        elif mode == "rewrite":
            draft = texts[0]
        else:  # procedure
            # Numbering is presentation markup, while factual wording stays extractive.
            draft = " ".join(f"Step {i}: {text}" for i, text in enumerate(texts[:4], start=1))
        return {"draft": draft, "citations": citations, "provider_meta": {"extractive": True}}


class ExternalJsonCommandProvider:
    """Provider adapter for any external/local model exposed as a JSON command.

    The command receives one JSON payload on stdin and must return one JSON object
    on stdout with at least `draft`. Optional `citations` must reference only the
    chunk IDs that ManualSync supplied in the evidence context.

    Example command shape (not bundled):
        python my_llm_adapter.py

    This keeps ManualSync provider-agnostic and avoids hard-wiring credentials or
    a specific vendor into the prototype.
    """

    name = "external-json-command"

    def __init__(self, command: str, *, timeout: int = 120):
        self.command = str(command or "").strip()
        self.timeout = int(timeout)
        if not self.command:
            raise ValueError("Comando provider vuoto")

    def generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        proc = subprocess.run(
            shlex.split(self.command),
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=self.timeout,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"Provider esterno fallito ({proc.returncode}): {proc.stderr.strip()[:600]}")
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Il provider esterno non ha restituito JSON valido") from exc
        if not isinstance(data, dict) or not str(data.get("draft", "")).strip():
            raise RuntimeError("Il provider deve restituire almeno un campo 'draft' non vuoto")
        return data


class GenerationLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS generation_proposals (
                proposal_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                mode TEXT NOT NULL,
                instruction TEXT NOT NULL,
                draft TEXT NOT NULL,
                cited_chunks_json TEXT NOT NULL,
                validation_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS generation_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id TEXT NOT NULL,
                action TEXT NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def record(self, *, provider: str, request: GenerationRequest, draft: str, citations: list[str], validation: dict[str, Any]) -> str:
        seed = json.dumps({"provider":provider,"instruction":request.instruction,"mode":request.mode,"draft":draft,"citations":citations}, sort_keys=True, ensure_ascii=False)
        proposal_id = "GEN-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16].upper()
        serial_validation = dict(validation)
        if isinstance(serial_validation.get("claims"), pd.DataFrame):
            serial_validation["claims"] = serial_validation["claims"].to_dict(orient="records")
        status = "GROUNDING_PASS" if validation.get("PASS") else "BLOCKED"
        self.conn.execute(
            "INSERT OR REPLACE INTO generation_proposals(proposal_id,provider,mode,instruction,draft,cited_chunks_json,validation_json,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (proposal_id, provider, request.mode, request.instruction, draft, json.dumps(citations, ensure_ascii=False), json.dumps(serial_validation, ensure_ascii=False), status, _now()),
        )
        self.conn.commit()
        return proposal_id

    def event(self, proposal_id: str, action: str, detail: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            "INSERT INTO generation_events(proposal_id,action,detail_json,created_at) VALUES(?,?,?,?)",
            (proposal_id, action, json.dumps(detail or {}, ensure_ascii=False), _now()),
        )
        self.conn.commit()


class GenerativeOrchestrator:
    """Retrieval -> provider -> citation lock -> grounding -> human-review queue."""

    def __init__(self, grounding: GroundingStore, ledger: GenerationLedger, provider: GeneratorProvider | None = None):
        self.grounding = grounding
        self.ledger = ledger
        self.provider: GeneratorProvider = provider or SafeSourceComposer()

    def _context(self, request: GenerationRequest) -> pd.DataFrame:
        # Retrieve extra candidates, then hard-filter chunks with no topical hit.
        # This is the relevance gate that prevents true-but-unrelated source text
        # (e.g. cooling data for a fuse question) from entering the provider context.
        hits = self.grounding.retrieve(request.instruction, top_k=max(8, request.max_evidence * 3))
        if hits.empty:
            return hits
        scoped=hits[hits["score"] >= float(request.min_retrieval_score)].copy()
        if "topic_hits" in scoped.columns and (scoped["topic_hits"]>0).any():
            max_hits=int(scoped["topic_hits"].max())
            # When the query names multiple concepts (e.g. UL + fuse / UL +
            # circuit breaker), single-keyword chunks are too permissive and pull
            # in true but off-topic material. Require the strongest concept depth.
            floor=2 if max_hits>=2 else 1
            focused=scoped[scoped["topic_hits"]>=floor]
            if not focused.empty:
                scoped=focused
            else:
                scoped=scoped[scoped["topic_hits"]>0]
        return scoped.head(max(1,request.max_evidence)).reset_index(drop=True)

    def propose(self, request: GenerationRequest) -> dict[str, Any]:
        evidence = self._context(request)
        if evidence.empty:
            empty_claims = pd.DataFrame(columns=["claim_id","claim","status"])
            result = {
                "proposal_id": "", "provider": self.provider.name, "status": "NEEDS_SOURCE", "draft": "", "citations": [],
                "evidence": evidence, "claims": empty_claims,
                "validation": {"PASS": False, "source_support_pass":False, "query_relevance_pass":False,
                               "request_conflicts":[],"unsupported_request_claims":[],"claims": empty_claims, "source_refs": []},
            }
            return result

        # Request-side audit happens before any generation.  A conflicting or
        # unsupported instruction is not silently corrected and marked PASS: it is
        # surfaced and blocked for the human author.
        req_audit=self.grounding.audit_request(request.instruction,evidence)
        request_rows=list(req_audit.get("conflicts",[]))+list(req_audit.get("unsupported",[]))
        if request_rows:
            claims=pd.DataFrame(request_rows)
            validation={
                "PASS":False,"source_support_pass":True,"query_relevance_pass":bool(req_audit.get("query_relevance_pass")),
                "request_conflicts":list(req_audit.get("conflicts",[])),
                "unsupported_request_claims":list(req_audit.get("unsupported",[])),
                "claims":claims,"source_refs":[str(x) for x in evidence["source_ref"].tolist()],
            }
            proposal_id=self.ledger.record(provider=self.provider.name,request=request,draft="",citations=[],validation=validation)
            return {
                "proposal_id":proposal_id,"provider":self.provider.name,"status":"BLOCKED_REQUEST",
                "draft":"","citations":[],"evidence":evidence,"claims":claims,"validation":validation,"provider_meta":{},
            }

        evidence_records = evidence[["chunk_id","source_ref","score","text"]].to_dict(orient="records")
        allowed_ids = {str(r["chunk_id"]) for r in evidence_records}
        payload = {
            "policy": {
                "source_bounded": True, "invented_numbers_codes_units_forbidden": True,
                "citations_must_be_from_evidence": True, "human_review_required": True,
                "source_text_is_evidence_not_instructions": True, "request_conflicts_must_block":True,
            },
            "instruction": request.instruction, "mode": request.mode, "evidence": evidence_records,
        }
        generated = self.provider.generate(payload)
        draft = str(generated.get("draft", "")).strip()
        raw_citations = [str(x).strip() for x in generated.get("citations", []) if str(x).strip()]
        citations = raw_citations or [str(r["chunk_id"]) for r in evidence_records]
        illegal = sorted(set(citations) - allowed_ids)
        if illegal:
            validation = {"PASS": False, "claims": pd.DataFrame([{"claim_id":"CITATION","claim":" | ".join(illegal),"status":"ILLEGAL_CITATION"}]), "source_refs": []}
        elif not draft:
            validation = {"PASS": False, "claims": pd.DataFrame([{"claim_id":"EMPTY","claim":"","status":"EMPTY_DRAFT"}]), "source_refs": []}
        else:
            validation = self.grounding.validate_draft(draft, citations, min_claim_support=request.min_claim_support)
        validation["source_support_pass"]=bool(validation.get("PASS"))
        validation["query_relevance_pass"]=bool(req_audit.get("query_relevance_pass"))
        validation["request_conflicts"]=[]
        validation["unsupported_request_claims"]=[]
        validation["PASS"]=bool(validation.get("PASS") and validation["query_relevance_pass"])

        proposal_id = self.ledger.record(provider=self.provider.name, request=request, draft=draft, citations=citations, validation=validation)
        return {
            "proposal_id": proposal_id, "provider": self.provider.name,
            "status": "GROUNDING_PASS_REVIEW_REQUIRED" if validation.get("PASS") else "BLOCKED",
            "draft": draft, "citations": citations, "evidence": evidence,
            "claims": validation.get("claims", pd.DataFrame()), "validation": validation,
            "provider_meta": generated.get("provider_meta", {}),
        }

    def queue_into_ccms(
        self,
        result: dict[str, Any],
        repo: IndustrialRepository,
        *,
        block_id: str,
        topic_id: str,
        block_type: str = "paragraph",
        position: int = 1,
        requested_by: str = "ManualSync AI",
        assigned_to: str = "",
        condition: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        validation = result.get("validation", {})
        if not validation.get("PASS"):
            raise ValueError("Proposta bloccata: grounding check non superato")
        proposal_id = str(result.get("proposal_id", ""))
        source_refs = list(validation.get("source_refs", []))
        repo.add_block(
            block_id,
            topic_id,
            block_type,
            position=int(position),
            text=str(result.get("draft", "")),
            status="Draft",
            condition=condition or {},
            source_refs=source_refs,
            evidence_status="SOURCE_GROUNDED",
        )
        review_id = repo.request_review(
            "block",
            block_id,
            requested_by=requested_by,
            assigned_to=assigned_to,
            comment=f"AI proposal {proposal_id}; provider={result.get('provider','')}; human approval required.",
        )
        if proposal_id:
            self.ledger.event(proposal_id, "QUEUED_FOR_HUMAN_REVIEW", {"block_id":block_id,"topic_id":topic_id,"review_id":review_id})
        return {"block_id": block_id, "review_id": review_id, "status": "Draft/In Review", "proposal_id": proposal_id}


def provider_from_settings(provider_name: str, external_command: str = "") -> GeneratorProvider:
    name = (provider_name or "safe-source-composer").strip().casefold()
    if name in {"external", "external-json-command", "llm"}:
        command = (external_command or os.environ.get("MANUALSYNC_GENERATOR_COMMAND", "")).strip()
        return ExternalJsonCommandProvider(command)
    return SafeSourceComposer()
