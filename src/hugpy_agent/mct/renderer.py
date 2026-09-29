"""Terminal renderer with idempotent display.

Design ref: §5 (operator experience), §16.3, invariant 14 (display is idempotent),
§21 (``renderer.py``). Serves enforcement rows 7–8.

A response object is rendered **at most once** for a given terminal turn. The
render ledger, not in-memory state, is the source of truth, so a crash-resumed B
(or a replayed ``response.ready``) never double-renders (design §15.2).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable

from .ledger import Ledger


@dataclass
class RenderResult:
    rendered: bool          # bytes were written to the sink this call
    already_rendered: bool  # a prior render was found; this call was a no-op
    body_sha256: str


class Renderer:
    def __init__(self, ledger: Ledger, sink: Callable[[str], None]):
        self.ledger = ledger
        self.sink = sink

    def render(self, session_id: str, turn_id: str, body: str) -> RenderResult:
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if self.ledger.is_rendered(session_id, turn_id):
            return RenderResult(rendered=False, already_rendered=True, body_sha256=digest)
        self.sink(body)
        self.ledger.record_render(session_id, turn_id, digest)
        return RenderResult(rendered=True, already_rendered=False, body_sha256=digest)

    # --- streaming (design §16.2) ------------------------------------------
    def render_frame(self, session_id: str, turn_id: str, text: str) -> bool:
        """Emit one validated stream frame. No-op if the turn was already rendered
        (idempotent display, invariant 14)."""
        if self.ledger.is_rendered(session_id, turn_id):
            return False
        self.sink(text)
        return True

    def seal_stream(self, session_id: str, turn_id: str, body_sha256: str) -> RenderResult:
        """Mark a streamed turn rendered without re-emitting (frames already went
        to the sink); the assembled body was verified before this call."""
        already = self.ledger.is_rendered(session_id, turn_id)
        self.ledger.record_render(session_id, turn_id, body_sha256)
        return RenderResult(rendered=not already, already_rendered=already, body_sha256=body_sha256)
