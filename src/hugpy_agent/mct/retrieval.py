"""Deterministic retrieval: lexical, structured, and dependency search.

Design ref: §11.1 (context layers), §11.2 (candidate generation), §21
(``retrieval.py``). Phase 2 is model-free, so this covers the *deterministic*
retrieval modes only — lexical (term overlap), structured (JSON field), and
dependency (provenance graph). Semantic/embedding retrieval is added on top in
Phase 3 (§22 Phase 3) without changing this interface.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .excerpt import apply_selector
from .ledger import Ledger
from .objects import ObjectStore
from .protocol import make_pointer, parse_pointer

# Object kinds that are legitimate context candidates (not manifests/receipts/etc.)
CANDIDATE_KINDS = ("operator_turn", "response_body", "summary", "excerpt",
                   "source_snapshot", "fact", "decision", "preference")

_WORD = re.compile(r"[a-z0-9_]+")


def tokenize(text: str) -> list[str]:
    return _WORD.findall(text.lower())


@dataclass
class Candidate:
    object_id: str
    pointer: str
    kind: str
    digest: str
    size: int
    created_at: str
    text: str | None
    source_objects: list[str] = field(default_factory=list)
    fact_kind: str | None = None  # for objects backed by a facts-table row


class Retrieval:
    def __init__(self, ledger: Ledger, store: ObjectStore, model=None):
        self.ledger = ledger
        self.store = store
        self.model = model  # optional LocalModel; None -> deterministic lexical only

    # --- candidate loading --------------------------------------------------
    def _candidate(self, session_id: str, meta: dict, fact_kind: str | None = None) -> Candidate:
        pointer = make_pointer(session_id, meta["object_id"])
        provenance = json.loads(meta.get("provenance") or "{}")
        sources = _source_object_ids(provenance)
        text = None
        mt = meta.get("media_type") or ""
        if mt.startswith("text/") or mt.endswith("json") or mt.endswith("+json"):
            try:
                text = self.store.resolve(session_id, pointer).decode("utf-8", errors="replace")
            except Exception:  # unreadable candidate is simply text-less
                text = None
        return Candidate(
            object_id=meta["object_id"], pointer=pointer, kind=meta["kind"],
            digest=meta["digest"], size=meta["size"], created_at=meta["created_at"],
            text=text, source_objects=sources, fact_kind=fact_kind,
        )

    # --- deterministic retrieval modes -------------------------------------
    def recent_turns(self, session_id: str, limit: int = 6,
                     exclude: set[str] | None = None) -> list[Candidate]:
        """Most-recent operator turns and responses (L1/L3), newest first (§11.1)."""
        exclude = exclude or set()
        rows = self.ledger.list_objects(
            session_id, kinds=["operator_turn", "response_body"],
            newest_first=True, limit=limit * 2)
        return [self._candidate(session_id, r) for r in rows if r["object_id"] not in exclude][:limit]

    def facts(self, session_id: str) -> list[Candidate]:
        """Durable memory (L2): decisions, preferences, entities, open questions."""
        out = []
        for f in self.ledger.facts(session_id):
            meta = self.ledger.get_object(f["object_id"])
            if meta:
                out.append(self._candidate(session_id, meta, fact_kind=f["kind"]))
        return out

    def sources(self, session_id: str) -> list[Candidate]:
        rows = self.ledger.list_objects(session_id, kinds=["source_snapshot"], newest_first=False)
        return [self._candidate(session_id, r) for r in rows]

    def lexical_score(self, query_tokens: set[str], cand: Candidate) -> float:
        """Deterministic term-overlap relevance in [0, 1] (stands in for R_i until
        Phase 3 supplies semantic relevance, §11.2)."""
        if not cand.text or not query_tokens:
            return 0.0
        toks = set(tokenize(cand.text))
        if not toks:
            return 0.0
        return len(query_tokens & toks) / len(query_tokens)

    def semantic_scores(self, session_id: str, query: str,
                        candidates: list[Candidate]) -> dict[str, float]:
        """Cosine similarity of ``query`` to each candidate (§11.2 semantic R_i).

        Returns ``{}`` when no model is configured or the model is unavailable, so
        the caller degrades to lexical only (design §17: never bypass, just
        degrade). Embeddings are cached in the ledger, keyed by object + model."""
        if self.model is None:
            return {}
        from .local_model import cosine
        try:
            qvec = self.model.embed([query])[0]
            scores: dict[str, float] = {}
            for c in candidates:
                if not c.text:
                    continue
                vec = self.ledger.get_embedding(c.object_id, self.model.name)
                if vec is None:
                    vec = self.model.embed([c.text])[0]
                    self.ledger.put_embedding(c.object_id, session_id, self.model.name, vec)
                scores[c.object_id] = max(0.0, cosine(qvec, vec))
            return scores
        except Exception:
            return {}  # local model unavailable -> deterministic fallback (§17)

    def structured(self, session_id: str, json_path: str, equals=None) -> list[Candidate]:
        """Structured search: JSON objects whose ``json_path`` exists (== ``equals``)."""
        out = []
        for r in self.ledger.list_objects(session_id, newest_first=False):
            mt = r.get("media_type") or ""
            if not (mt.endswith("json") or mt.endswith("+json")):
                continue
            pointer = make_pointer(session_id, r["object_id"])
            try:
                value = apply_selector(self.store.resolve(session_id, pointer),
                                       f"json {json_path}").decode("utf-8")
            except Exception:
                continue
            if equals is None or json.loads(value) == equals:
                out.append(self._candidate(session_id, r))
        return out

    def dependency(self, session_id: str, seed_object_ids: set[str]) -> list[Candidate]:
        """Objects connected to the seeds through provenance (§11.5 rule 5).

        Returns both directions: sources the seeds were derived from, and objects
        derived from the seeds. Used to complete a fragment's dependency set."""
        out: dict[str, Candidate] = {}
        seeds = set(seed_object_ids)
        # forward: seeds' own sources
        for oid in seeds:
            meta = self.ledger.get_object(oid)
            if not meta:
                continue
            for src in _source_object_ids(json.loads(meta.get("provenance") or "{}")):
                m = self.ledger.get_object(src)
                if m:
                    out[src] = self._candidate(session_id, m)
        # backward: objects whose provenance references a seed
        for r in self.ledger.list_objects(session_id, newest_first=False):
            if r["object_id"] in seeds:
                continue
            srcs = set(_source_object_ids(json.loads(r.get("provenance") or "{}")))
            if srcs & seeds:
                out[r["object_id"]] = self._candidate(session_id, r)
        return list(out.values())


def _source_object_ids(provenance: dict) -> list[str]:
    """Normalize provenance links to a list of source object_ids."""
    ids: list[str] = []
    src = provenance.get("source")
    if isinstance(src, str) and src.startswith("mct://"):
        ids.append(parse_pointer(src)[1])
    for item in provenance.get("source_objects", []) or []:
        if isinstance(item, str):
            ids.append(parse_pointer(item)[1] if item.startswith("mct://") else item)
    return ids
