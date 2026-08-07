"""A-mediated pull arbitration (Phase-1 transport subset).

Design ref: §10 (arbitration order, decisions, loop controls), §21 (``pull_broker.py``).
Serves enforcement rows 11–13.

Phase 1 has no external sources, so pulls resolve against objects already
committed in the session plus a session catalog (§11.6). This exercises the full
``exact / reduced / not_found / denied / budget_exhausted`` decision path and the
pull-loop budget controls (§10.3) without the filesystem broker (Phase 4), which
will slot in at "resolve the target" below.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .capabilities import Capabilities
from .errors import AuthorizationError, MctError, NotFoundError
from .ledger import Ledger
from .objects import ObjectStore
from .protocol import parse_pointer, validate_against

POLICY_REVISION = 1  # static policy snapshot for the prototype (§18.2)


@dataclass
class PullBudget:
    max_pulls: int = 8
    max_tokens: int = 12000
    max_source_bytes: int = 64 * 1024 * 1024  # cumulative source-byte limit (§10.3)


@dataclass
class TurnPullState:
    pulls: int = 0
    tokens: int = 0
    source_bytes: int = 0
    seen_queries: list[str] = field(default_factory=list)
    fs_results: dict = field(default_factory=dict)  # query -> steward candidates


def _tokens(data: bytes) -> int:
    return max(1, len(data) // 4)


def _snippet(text: str, terms: list[str], width: int = 200) -> str:
    """A short evidence window around the earliest term hit (slate material)."""
    if not text:
        return ""
    low = text.lower()
    hits = [low.find(t) for t in terms if t in low]
    if not hits:
        return text[:width].strip()
    start = max(0, min(hits) - width // 3)
    return text[start:start + width].strip()


class PullBroker:
    def __init__(self, store: ObjectStore, ledger: Ledger, caps: Capabilities,
                 budget: PullBudget | None = None):
        self.store = store
        self.ledger = ledger
        self.caps = caps
        self.budget = budget or PullBudget()

    def arbitrate(
        self,
        session_id: str,
        turn_id: str,
        epoch: str,
        request: dict,
        catalog: dict[str, str],
        state: TurnPullState,
        fs_search=None,
        structured_search=None,
    ) -> tuple[dict, str]:
        """Return ``(pull_result_payload, pull_result_pointer)``.

        Follows the design §10.1 order, abbreviated to the transport concerns.
        Every outcome is a committed, pointed ``pull_result`` object — including
        denials, so A can reformulate rather than repeat (§10.3).
        """
        # 1. schema (registry row 1 applied to the pull-request object)
        validate_against("pull-request-v1", request)
        request_id = request["request_id"]

        def finish(payload: dict) -> tuple[dict, str]:
            validate_against("pull-result-v1", payload)
            ref = self.store.commit(
                session_id, json.dumps(payload).encode("utf-8"),
                media_type="application/vnd.hugpy.mct-pull-result+json",
                kind="pull_result", provenance={"request_id": request_id},
            )
            self.ledger.record_pull(session_id, turn_id, request_id,
                                    payload["decision"], ref.pointer)
            return payload, ref.pointer

        # 6. budget preflight (§10.3) — fail closed before doing work
        if state.pulls >= self.budget.max_pulls or state.tokens >= self.budget.max_tokens:
            return finish({
                "schema": "mct.pull-result/1", "request_id": request_id,
                "decision": "budget_exhausted",
                "denial_reason": "pull count or token budget exhausted for this turn",
                "policy_revision": POLICY_REVISION,
            })
        state.pulls += 1

        # 3. scope / authorization (rows 11–12)
        target = request["target"]
        try:
            self.caps.authorize_pull_target(session_id, target)
        except (AuthorizationError, MctError) as exc:
            return finish({
                "schema": "mct.pull-result/1", "request_id": request_id,
                "decision": "denied", "denial_reason": exc.code,
                "policy_revision": POLICY_REVISION,
            })

        # 4. resolve the target. Filesystem-backed sources are already snapshotted
        # into the catalog through confined_io before arbitration (§13.4).
        # Catalog queries go through candidate generation: an unambiguous winner
        # resolves directly; a contested slate goes back to A as decision
        # "candidates" — the frontier model chooses, B only ranks (§11.2).
        candidates = None
        if target["kind"] == "search":
            # A directed B: run the directive, then hand back the same slate a
            # catalog query would produce. A single hit resolves straight
            # through — a precise directive that matched once needs no round
            # trip to confirm it.
            hits = (structured_search(target.get("spec") or {}, self.K)
                    if structured_search else [])
            if state is not None:
                state.search_trace = {"tried": [{"strategy": "structured",
                                                 "hits": len(hits)}],
                                      "coverage": []}
            source_pointer = hits[0]["pointer"] if len(hits) == 1 else None
            candidates = hits if len(hits) > 1 else None
        elif target["kind"] == "catalog-query":
            source_pointer, candidates = self._resolve_catalog_query(
                session_id, target, catalog, fs_search, state)
        else:
            source_pointer = self._locate(session_id, target, catalog)
        if candidates:
            slate = json.dumps({
                "schema": "mct.candidates/1",
                "query": target.get("query") or "",
                "note": "Ambiguous query. Choose one candidate, then pull it with "
                        "target {\"kind\":\"object\",\"object\":<pointer>} (or an "
                        "exact-name catalog-query).",
                "candidates": candidates,
            }).encode("utf-8")
            ref = self.store.commit(
                session_id, slate,
                media_type="application/vnd.hugpy.mct-candidates+json",
                kind="candidate_list", provenance={"request_id": request_id})
            state.tokens += _tokens(slate)
            return finish({
                "schema": "mct.pull-result/1", "request_id": request_id,
                "decision": "candidates",
                "objects": [{"object": ref.pointer, "sha256": ref.sha256,
                             "token_estimate": _tokens(slate)}],
                "policy_revision": POLICY_REVISION,
            })
        if source_pointer is None:
            return finish({
                "schema": "mct.pull-result/1", "request_id": request_id,
                "decision": "not_found",
                "denial_reason": "authorized search completed without a match",
                "policy_revision": POLICY_REVISION,
            })

        # 6b. cumulative source-byte budget (§10.3)
        _, src_oid = parse_pointer(source_pointer)
        src_meta = self.ledger.get_object(src_oid)
        if src_meta and state.source_bytes + src_meta["size"] > self.budget.max_source_bytes:
            return finish({
                "schema": "mct.pull-result/1", "request_id": request_id,
                "decision": "budget_exhausted",
                "denial_reason": "cumulative source-byte budget exhausted for this turn",
                "policy_revision": POLICY_REVISION,
            })
        if src_meta:
            state.source_bytes += src_meta["size"]

        # 7. choose the least expansive satisfactory form (§10.1 step 7)
        selector = self._selector_from(request, target)
        if selector:
            excerpt = self.store.resolve(session_id, source_pointer, selector=selector)
            ref = self.store.commit(
                session_id, excerpt, media_type="text/plain", kind="excerpt",
                provenance={"source": source_pointer, "selector": selector},
            )
            state.tokens += _tokens(excerpt)
            obj = {"object": ref.pointer, "selector": selector, "source": source_pointer,
                   "sha256": ref.sha256, "token_estimate": _tokens(excerpt)}
            decision = "reduced"
        else:
            full = self.store.resolve(session_id, source_pointer)
            _, source_obj_id = parse_pointer(source_pointer)
            meta = self.ledger.get_object(source_obj_id)
            state.tokens += _tokens(full)
            obj = {"object": source_pointer, "sha256": meta["digest"],
                   "token_estimate": _tokens(full)}
            decision = "exact"

        return finish({
            "schema": "mct.pull-result/1", "request_id": request_id,
            "decision": decision, "objects": [obj],
            "policy_revision": POLICY_REVISION,
        })

    # --- resolution helpers ------------------------------------------------
    def _locate(self, session_id: str, target: dict, catalog: dict[str, str]) -> str | None:
        kind = target["kind"]
        if kind in ("object", "selector"):
            pointer = target["object"]
            _, object_id = parse_pointer(pointer)
            return pointer if self.ledger.get_object(object_id) else None
        return None

    K = 5                      # max slate size returned to A
    _PEEK_BYTES = 256 * 1024   # bounded content peek for catalog ranking

    def _resolve_catalog_query(self, session_id: str, target: dict,
                               catalog: dict[str, str], fs_search=None,
                               state: TurnPullState | None = None
                               ) -> tuple[str | None, list[dict] | None]:
        """Candidate generation for a catalog query (§11.2).

        Returns ``(pointer, None)`` for an unambiguous winner, ``(None, slate)``
        when the top candidates are contested (A chooses), ``(None, None)`` for
        a miss. Name terms outweigh content terms so a query that names a
        source still beats an incidental mention."""
        query = (target.get("query") or "").lower()
        terms = [t for t in query.split() if t]
        if not terms:
            return None, None
        scored: list[dict] = []
        for name, pointer in catalog.items():
            key = name.lower()
            if all(t in key for t in terms):
                return pointer, None  # named exactly: no arbitration needed
            text = self._peek_text(session_id, pointer)
            low = text.lower()
            score = 3.0 * sum(t in key for t in terms)
            score += float(sum(1 for t in terms if t in low)) if text else 0.0
            if score > 0:
                _, oid = parse_pointer(pointer)
                meta = self.ledger.get_object(oid) or {}
                scored.append({"name": name, "pointer": pointer, "score": score,
                               "snippet": _snippet(text, terms),
                               "token_estimate": max(1, int(meta.get("size") or 0) // 4)})
        if fs_search is not None:
            # Steward trigger on: B may broker against granted roots (still
            # confined + snapshotted; never a host path). Memoized per turn so
            # a repeated query does not re-pay the walk or the finder call.
            fs_hits = state.fs_results.get(query) if state is not None else None
            if fs_hits is None:
                fs_hits = fs_search(target.get("query") or "", self.K)
                if state is not None:
                    state.fs_results[query] = fs_hits
            seen = {c["pointer"] for c in scored}
            scored += [c for c in fs_hits if c["pointer"] not in seen]
        if not scored:
            return None, None
        scored.sort(key=lambda c: (-c["score"], c["name"]))
        top = scored[: self.K]
        if len(top) == 1 or top[0]["score"] >= 2.0 * top[1]["score"]:
            return top[0]["pointer"], None  # clear winner: skip the round-trip
        return None, top

    def _peek_text(self, session_id: str, pointer: str) -> str:
        """Bounded text peek of a committed object ('' when unreadable)."""
        try:
            data = self.store.resolve(session_id, pointer)[: self._PEEK_BYTES]
            return data.decode("utf-8", errors="replace")
        except Exception:
            return ""

    _SELECTOR_VERBS = ("lines ", "bytes ", "json ", "symbol ", "match ")

    @classmethod
    def _selector_from(cls, request: dict, target: dict) -> str | None:
        if target.get("kind") == "selector":
            return target.get("selector")
        if target.get("selector"):
            return target["selector"]
        form = (request.get("preferred_form") or "")
        if form.lower().startswith(cls._SELECTOR_VERBS):
            return form  # any bounded excerpt selector (§7.3): lines/bytes/json/symbol/match
        return None
