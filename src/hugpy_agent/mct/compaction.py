"""Compaction: derived products with reversible provenance.

Design ref: §11.4 (compaction products + per-fact metadata), §11.5
(deduplication), §21 (``compaction.py``). Invariants 8 & 15: every derived fact
keeps pointers to its sources, originals are never edited, and a summary can be
walked back to the bytes it came from.

Phase 2 is deterministic: extraction here is regex/structured, not model-driven.
Phase 3 adds model-drafted summaries on top of the same fact schema and the same
provenance discipline (§22 Phase 3).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .ledger import Ledger
from .objects import ObjectRef, ObjectStore
from .protocol import make_pointer

# Deterministic extractors: (compiled pattern, fact kind).
_EXTRACTORS = [
    (re.compile(r"\bDECISION\b\s*[:\-]?\s*(.+)", re.I), "decision"),
    (re.compile(r"\bPREFER(?:ENCE)?\b\s*[:\-]?\s*(.+)", re.I), "preference"),
    (re.compile(r"\b(?:TODO|OPEN QUESTION|OPEN)\b\s*[:\-]?\s*(.+)", re.I), "open_question"),
]


@dataclass
class Fact:
    object_id: str
    pointer: str
    kind: str
    text: str
    source_objects: list[str]
    method: str
    confidence: float
    sensitivity: str
    scope: str


class Compaction:
    def __init__(self, store: ObjectStore, ledger: Ledger, model=None):
        self.store = store
        self.ledger = ledger
        self.model = model  # optional LocalModel for drafting/extraction/conflict

    def record_fact(self, session_id: str, text: str, *, kind: str,
                    source_object_ids: list[str], method: str = "manual",
                    confidence: float = 1.0, sensitivity: str = "normal",
                    scope: str = "session", supersedes: str | None = None,
                    model: str | None = None) -> ObjectRef:
        """Commit a derived fact with full lineage. Exact byte-duplicate facts are
        deduplicated (§11.5 rule 1): an identical, live fact is returned as-is.

        ``model`` records which local model drafted the fact, so a model-derived
        product is never untraceable (Phase-3 exit condition, §11.4)."""
        data = text.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        for existing in self.ledger.facts(session_id, kind=kind):
            meta = self.ledger.get_object(existing["object_id"])
            if meta and meta["digest"] == digest:
                return ObjectRef(existing["object_id"], session_id,
                                 make_pointer(session_id, existing["object_id"]),
                                 digest, meta["size"], meta["media_type"], meta["kind"])

        provenance = {
            "kind": kind, "method": method,
            "source_objects": [make_pointer(session_id, oid) for oid in source_object_ids],
        }
        if model:
            provenance["model"] = model
        ref = self.store.commit(session_id, data, media_type="text/plain",
                                kind="fact", provenance=provenance)
        self.ledger.record_fact_meta(ref.object_id, session_id, kind, method,
                                     confidence, sensitivity, scope, source_object_ids)
        if supersedes:
            self.ledger.supersede_fact(supersedes, ref.object_id)  # original retained (§11.5 rule 3)
        return ref

    # --- model-assisted products (Phase 3) ---------------------------------
    def summarize(self, session_id: str, source_object_ids: list[str], *,
                  max_sentences: int = 2) -> ObjectRef | None:
        """Draft an episodic summary from sources. The model *drafts*; the summary
        keeps pointers to every source (reversible, §11.4). No model -> no summary
        (deterministic caller keeps the originals)."""
        if self.model is None:
            return None
        texts = []
        for oid in source_object_ids:
            try:
                texts.append(self.store.resolve(session_id, make_pointer(session_id, oid))
                             .decode("utf-8", errors="replace"))
            except Exception:
                continue
        draft = self.model.summarize("\n".join(texts), max_sentences=max_sentences)
        if not draft.strip():
            return None
        return self.record_fact(session_id, draft, kind="episodic_summary",
                                source_object_ids=source_object_ids, method="model-draft",
                                confidence=0.7, model=self.model.name)

    def extract_semantic(self, session_id: str, source_object_id: str) -> list[ObjectRef]:
        """Model-assisted extraction: catches decisions/preferences/questions the
        regex extractor misses. Each fact points back to its source."""
        if self.model is None:
            return []
        try:
            text = self.store.resolve(session_id, make_pointer(session_id, source_object_id)) \
                .decode("utf-8", errors="replace")
        except Exception:
            return []
        out = []
        for item in self.model.extract(text):
            out.append(self.record_fact(session_id, item["text"], kind=item["kind"],
                                        source_object_ids=[source_object_id],
                                        method="model-extract", confidence=0.6,
                                        model=self.model.name))
        return out

    def detect_conflicts(self, session_id: str, kind: str = "decision") -> list[tuple[str, str]]:
        """Flag conflicting facts of one kind. Both are retained and marked
        (§11.5 rule 4); nothing is deleted."""
        if self.model is None:
            return []
        facts = self.facts(session_id, kind=kind)
        pairs = []
        for i in range(len(facts)):
            for j in range(i + 1, len(facts)):
                if self.model.are_conflicting(facts[i].text, facts[j].text):
                    self.ledger.mark_conflict(facts[i].object_id, facts[j].object_id)
                    pairs.append((facts[i].object_id, facts[j].object_id))
        return pairs

    def cluster_near_duplicates(self, session_id: str, kind: str = "decision",
                                threshold: float = 0.92) -> list[tuple[str, str, float]]:
        """Semantic near-duplicate clustering. Marks links only — semantic
        similarity NEVER deletes an original (§11.5 rules 2 & 3)."""
        if self.model is None:
            return []
        from .local_model import cosine
        facts = self.facts(session_id, kind=kind)
        vecs = {f.object_id: self.model.embed([f.text])[0] for f in facts}
        pairs = []
        for i in range(len(facts)):
            for j in range(i + 1, len(facts)):
                a, b = facts[i].object_id, facts[j].object_id
                sim = cosine(vecs[a], vecs[b])
                if sim >= threshold:
                    self.ledger.mark_near_duplicate(a, b, session_id, sim, self.model.name)
                    pairs.append((a, b, round(sim, 4)))
        return pairs

    def extract(self, session_id: str, source_object_id: str) -> list[ObjectRef]:
        """Deterministically extract decisions/preferences/questions from a source
        object's text. Each derived fact points back to ``source_object_id``."""
        pointer = make_pointer(session_id, source_object_id)
        try:
            text = self.store.resolve(session_id, pointer).decode("utf-8", errors="replace")
        except Exception:
            return []
        out: list[ObjectRef] = []
        for line in text.splitlines():
            for pattern, kind in _EXTRACTORS:
                m = pattern.search(line)
                if m:
                    out.append(self.record_fact(
                        session_id, m.group(1).strip(), kind=kind,
                        source_object_ids=[source_object_id], method="regex-extract"))
                    break
        return out

    def facts(self, session_id: str, kind: str | None = None) -> list[Fact]:
        result = []
        for f in self.ledger.facts(session_id, kind=kind):
            meta = self.ledger.get_object(f["object_id"])
            text = self.store.resolve(session_id, make_pointer(session_id, f["object_id"])).decode("utf-8")
            import json
            result.append(Fact(
                object_id=f["object_id"], pointer=make_pointer(session_id, f["object_id"]),
                kind=f["kind"], text=text, source_objects=json.loads(f["source_objects"]),
                method=f["method"], confidence=f["confidence"],
                sensitivity=f["sensitivity"], scope=f["scope"]))
        return result

    def mark_conflict(self, a_object_id: str, b_object_id: str) -> None:
        """Retain both facts and flag them as conflicting (§11.5 rule 4)."""
        self.ledger.mark_conflict(a_object_id, b_object_id)

    # --- reversibility: the provenance graph (§11.4, invariant 8) -----------
    def provenance_graph(self, session_id: str, object_id: str,
                         _seen: set[str] | None = None) -> dict:
        """Walk source links back to originals; proves any derived object can be
        reconstructed to the bytes it came from — no model required."""
        import json
        _seen = _seen if _seen is not None else set()
        meta = self.ledger.get_object(object_id)
        if meta is None or object_id in _seen:
            return {"object_id": object_id, "missing_or_cycle": True}
        _seen.add(object_id)
        from .retrieval import _source_object_ids
        sources = _source_object_ids(json.loads(meta.get("provenance") or "{}"))
        return {
            "object_id": object_id,
            "kind": meta["kind"],
            "digest": meta["digest"],
            "is_original": not sources,
            "sources": [self.provenance_graph(session_id, s, _seen) for s in sources],
        }
