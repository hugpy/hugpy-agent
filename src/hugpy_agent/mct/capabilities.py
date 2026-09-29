"""Deterministic authorization (Phase-1 subset).

Design ref: §13.1 (``Cap_A = Reach_B,OS ∩ Grant_session ∩ Policy_tool ∩ Scope_request``),
§21 (``capabilities.py``). Serves enforcement rows 11–12.

Phase 1 has no filesystem sources yet (that is ``confined_io`` in Phase 4), so the
only reach A has is *within its own session's already-committed objects*. The one
rule enforced here is therefore the load-bearing one for the transport core: **a
pull can only narrow or materialize already-permitted reach; it can never widen
authority or cross a session boundary** (invariant 12, adversarial cases 1 & 5).
The richer per-root/per-selector/size policy (design §20.4) lands in Phase 4 on
top of this same call.
"""
from __future__ import annotations

from .errors import AuthorizationError, IsolationError
from .protocol import parse_pointer


class Capabilities:
    def authorize_pull_target(self, session_id: str, target: dict) -> None:
        """Raise if ``target`` is outside this session's permitted reach.

        Returns ``None`` when the target is admissible for lookup by the broker;
        the broker still decides ``exact``/``reduced``/``not_found`` afterwards.
        """
        kind = target.get("kind")
        if kind in ("object", "selector"):
            pointer = target.get("object")
            if not pointer:
                raise AuthorizationError("object/selector target requires an object pointer")
            ptr_session, _ = parse_pointer(pointer)  # rejects path-shaped input (invariant 5)
            if ptr_session != session_id:
                raise IsolationError("pull references an object outside this session")
        elif kind in ("catalog-query", "search", "browse"):
            # catalog-query searches only this session's catalog; search and
            # browse are executed by B against the granted roots (and not at
            # all when fs requests are off) — all confined on B's side.
            return
        else:
            raise AuthorizationError(f"unsupported pull target kind: {kind!r}")
