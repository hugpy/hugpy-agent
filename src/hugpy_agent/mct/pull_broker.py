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


def _tokens(data: bytes) -> int:
    return max(1, len(data) // 4)


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
        source_pointer = self._locate(session_id, target, catalog)
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
        if kind == "catalog-query":
            query = (target.get("query") or "").lower()
            terms = [t for t in query.split() if t]
            best = None
            for name, pointer in catalog.items():
                key = name.lower()
                if terms and all(t in key for t in terms):
                    return pointer
                if any(t in key for t in terms):
                    best = best or pointer
            return best
        return None

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
