"""Deterministic context engine: candidate scoring and budget-aware packing.

Design ref: §11.2 (candidate scoring formula), §11.3 (exact prompt handling),
§11.5 (exact dedup), §11.6 (catalog), §21 (``context_builder.py``). Serves the
Phase-2 exit condition: *all selected context can be explained and reconstructed
without a local model.*

The score is the design's formula, computed from deterministic signals only:

    S_i = w_a·A_i + w_r·R_i + w_d·D_i + w_f·F_i + w_c·C_i − w_z·Z_i − w_u·U_i

R_i is lexical overlap (not embeddings) in Phase 2; every other term is structural.
``build`` returns a full trace — per fragment: score, component breakdown, and
include/omit reason — which is the machine-checkable form of "explain why this
fragment was included or omitted" (§18.2).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .protocol import make_pointer, parse_pointer, validate_against
from .retrieval import Candidate, Retrieval, tokenize


@dataclass
class Weights:
    w_a: float = 1.0   # authority / instruction priority
    w_r: float = 1.2   # relevance (lexical in Phase 2)
    w_d: float = 0.8   # dependency importance
    w_f: float = 0.5   # freshness
    w_c: float = 0.4   # continuity with active task
    w_z: float = 0.6   # token-size cost
    w_u: float = 0.9   # redundancy with already-selected


# role -> (authority in [0,1], continuity in [0,1])
_ROLE_AUTHORITY = {
    "governing_instruction": (1.00, 1.0),
    "decision_memory": (0.85, 0.6),
    "operator_preference": (0.80, 0.6),
    "open_question": (0.60, 0.7),
    "entity_index": (0.55, 0.5),
    "episodic_summary": (0.50, 1.0),
    "source_excerpt": (0.50, 0.3),
    "tool_result_abstract": (0.50, 0.4),
    "source_catalog": (0.40, 0.2),
}


def token_estimate(size: int) -> int:
    return max(1, size // 4)


def role_for(cand: Candidate) -> str:
    if cand.kind in ("operator_turn", "response_body"):
        return "episodic_summary"
    if cand.kind == "fact":
        return {"decision": "decision_memory", "preference": "operator_preference",
                "open_question": "open_question", "entity": "entity_index"}.get(
                    cand.fact_kind or "", "decision_memory")
    if cand.kind in ("excerpt", "source_snapshot"):
        return "source_excerpt"
    return "episodic_summary"


@dataclass
class ScoredFragment:
    candidate: Candidate
    role: str
    score: float
    components: dict
    tokens: int
    included: bool = False
    reason: str = ""


@dataclass
class BuildTrace:
    pack_budget: int
    required_tokens: int
    selected_tokens: int
    fragments: list[ScoredFragment] = field(default_factory=list)
    required_rows: list[dict] = field(default_factory=list)

    def explain(self) -> dict:
        return {
            "pack_budget": self.pack_budget,
            "required_tokens": self.required_tokens,
            "selected_tokens": self.selected_tokens,
            # required fragments bypass scoring but are still selected context (§11.2)
            "included": self.required_rows + [_frag_row(f) for f in self.fragments if f.included],
            "omitted": [_frag_row(f) for f in self.fragments if not f.included],
        }


def _frag_row(f: ScoredFragment) -> dict:
    return {"object": f.candidate.object_id, "role": f.role, "score": round(f.score, 4),
            "tokens": f.tokens, "reason": f.reason, "components": {k: round(v, 4)
                                                                   for k, v in f.components.items()}}


class ContextBuilder:
    def __init__(self, store, ledger, retrieval: Retrieval, weights: Weights | None = None,
                 model=None):
        self.store = store
        self.ledger = ledger
        self.retrieval = retrieval
        self.w = weights or Weights()
        self.model = model  # optional; only affects R_i ranking, nothing else

    def build(self, session_id, turn_id, epoch, operator_turn_ref, query, *,
              budget: dict, required_specs=None, catalog=None) -> tuple[str, str, BuildTrace]:
        required_specs = required_specs or []
        catalog = catalog or {}

        # Budget: reserve output + pull space; pack optional context into the rest (§11.2).
        pack_budget = max(0, budget["maximum_input_tokens"]
                          - budget["reserved_output_tokens"]
                          - budget.get("pull_tokens_remaining", 0))

        # --- required fragments (bypass scoring, §11.2) ------------------------
        required_fragments = []
        required_rows = [{"object": operator_turn_ref.object_id, "role": "operator_prompt",
                          "score": None, "tokens": token_estimate(operator_turn_ref.size),
                          "reason": "required-operator-turn", "components": {}}]
        required_digests = {operator_turn_ref.sha256}
        required_source_ids: set[str] = set()
        req_tokens = token_estimate(operator_turn_ref.size)  # operator turn is L0
        for spec in required_specs:
            _, oid = parse_pointer(spec["object"])
            meta = self.ledger.get_object(oid)
            if not meta:
                continue
            tok = token_estimate(meta["size"])
            req_tokens += tok
            required_digests.add(meta["digest"])
            required_source_ids.update(spec.get("source_objects", []))
            role = spec.get("role", "governing_instruction")
            required_fragments.append({
                "object": spec["object"], "role": role,
                "priority": int(spec.get("priority", 100)),
                "token_estimate": tok, "required": True,
                **({"source_objects": spec["source_objects"]} if spec.get("source_objects") else {}),
            })
            required_rows.append({"object": oid, "role": role, "score": None, "tokens": tok,
                                  "reason": "required", "components": {}})

        # --- candidate pool (deterministic retrieval) -------------------------
        query_tokens = set(tokenize(query))
        pool = self.retrieval.recent_turns(session_id, limit=6,
                                           exclude={operator_turn_ref.object_id})
        pool += self.retrieval.facts(session_id)

        # exact dedup by digest (§11.5 rule 1)
        seen_digests = set(required_digests)
        deduped: list[Candidate] = []
        dropped: list[Candidate] = []
        for c in pool:
            (dropped if c.digest in seen_digests else deduped).append(c)
            seen_digests.add(c.digest)

        # freshness ranking over the pool
        order = sorted(deduped, key=lambda c: c.created_at, reverse=True)
        fresh_rank = {c.object_id: (1.0 - i / max(1, len(order) - 1)) for i, c in enumerate(order)}

        # Semantic relevance from the local model (empty dict if no model / unavailable).
        # The model can only *raise* R_i; it never touches authorization, packing, or
        # dedup (invariant 9, Phase-3 exit condition).
        semantic = self.retrieval.semantic_scores(session_id, query, deduped)

        # --- static scores ----------------------------------------------------
        scored: list[ScoredFragment] = []
        for c in deduped:
            role = role_for(c)
            authority, continuity = _ROLE_AUTHORITY.get(role, (0.5, 0.4))
            r_lex = self.retrieval.lexical_score(query_tokens, c)
            r_sem = semantic.get(c.object_id, 0.0)
            relevance = max(r_lex, r_sem)  # model improves ranking, never degrades it
            dependency = 1.0 if c.object_id in required_source_ids else 0.0
            freshness = fresh_rank.get(c.object_id, 0.0)
            tokens = token_estimate(c.size)
            size_cost = min(1.0, tokens / max(1, pack_budget))
            comp = {"A": authority, "R": relevance, "R_lex": r_lex, "R_sem": round(r_sem, 4),
                    "D": dependency, "F": freshness, "C": continuity, "Z": size_cost, "U": 0.0}
            static = (self.w.w_a * authority + self.w.w_r * relevance + self.w.w_d * dependency
                      + self.w.w_f * freshness + self.w.w_c * continuity - self.w.w_z * size_cost)
            scored.append(ScoredFragment(c, role, static, comp, tokens))
        for c in dropped:
            sf = ScoredFragment(c, role_for(c), 0.0, {"exact_duplicate": 1.0},
                                token_estimate(c.size), included=False, reason="exact-duplicate")
            scored.append(sf)

        # --- greedy pack with redundancy penalty (§11.5) ----------------------
        candidates = [s for s in scored if s.reason != "exact-duplicate"]
        candidates.sort(key=lambda s: s.score, reverse=True)
        remaining = pack_budget - req_tokens
        selected_tokens = 0
        selected_tok_sets: list[set[str]] = []
        for sf in candidates:
            toks = set(tokenize(sf.candidate.text or ""))
            redundancy = max((_jaccard(toks, prev) for prev in selected_tok_sets), default=0.0)
            sf.components["U"] = redundancy
            sf.score -= self.w.w_u * redundancy
            if sf.score <= 0:
                sf.reason = "low-score"
                continue
            if sf.tokens > remaining:
                sf.reason = "budget-exhausted"
                continue
            sf.included = True
            sf.reason = "selected"
            remaining -= sf.tokens
            selected_tokens += sf.tokens
            selected_tok_sets.append(toks)

        # --- assemble + commit manifest ---------------------------------------
        fragments = list(required_fragments)
        for sf in candidates:
            if not sf.included:
                continue
            frag = {"object": sf.candidate.pointer, "role": sf.role,
                    "priority": round(_ROLE_AUTHORITY.get(sf.role, (0.5, 0))[0] * 100),
                    "token_estimate": sf.tokens, "required": False}
            if sf.candidate.source_objects:
                frag["source_objects"] = sf.candidate.source_objects
            fragments.append(frag)

        catalog_pointer = self._commit_catalog(session_id, catalog)
        manifest = {
            "schema": "mct.context/1", "session_id": session_id, "turn_id": turn_id,
            "epoch": epoch,
            "operator_turn": {"object": operator_turn_ref.pointer, "required": True, "verbatim": True},
            "fragments": fragments, "budget": budget,
        }
        if catalog_pointer:
            manifest["catalog"] = catalog_pointer
        validate_against("context-v1", manifest)
        ref = self.store.commit(session_id, json.dumps(manifest).encode("utf-8"),
                                media_type="application/vnd.hugpy.mct-context+json",
                                kind="context_manifest", provenance={"turn_id": turn_id})

        trace = BuildTrace(pack_budget, req_tokens, selected_tokens, scored, required_rows)
        return ref.pointer, ref.sha256, trace

    def _commit_catalog(self, session_id: str, catalog: dict[str, str]) -> str | None:
        if not catalog:
            return None
        entries = []
        for name, pointer in sorted(catalog.items()):
            _, oid = parse_pointer(pointer)
            meta = self.ledger.get_object(oid)
            entries.append({"name": name, "kind": meta["kind"] if meta else "unknown",
                            "size": meta["size"] if meta else 0})  # metadata only, no contents (§11.6)
        body = {"schema": "mct.catalog/1", "entries": entries}
        ref = self.store.commit(session_id, json.dumps(body).encode("utf-8"),
                                media_type="application/vnd.hugpy.mct-catalog+json",
                                kind="context_catalog", provenance={})
        return ref.pointer


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
