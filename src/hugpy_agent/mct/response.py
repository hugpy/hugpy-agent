"""Response validation (complete-body mode).

Design ref: §16.1 (baseline complete sealed body), §16.3 (output validation),
§21 (``response.py``). Serves enforcement row 7 (validate before render).

Streaming (§16.2) is deliberately out of scope for Phase 1 — the baseline is a
single sealed body, which is the smallest reliable prototype (decision §26).
"""
from __future__ import annotations

import json

from .errors import IntegrityError, ProtocolError, StateError
from .ledger import Ledger
from .objects import ObjectStore
from .protocol import validate_against

MAX_BODY_BYTES = 1 * 1024 * 1024
MAX_RENDERED_LINES = 20000


class ResponseValidator:
    def __init__(self, store: ObjectStore, ledger: Ledger):
        self.store = store
        self.ledger = ledger

    def validate(
        self,
        session_id: str,
        turn_id: str,
        epoch: str,
        manifest_pointer: str,
        *,
        cancelled: bool,
        allow_terminal_escapes: bool = False,
    ) -> tuple[str, dict]:
        """Return ``(body_text, manifest)`` or raise. Never guesses missing content."""
        manifest = json.loads(self.store.resolve(session_id, manifest_pointer))
        validate_against("response-v1", manifest)

        # turn / epoch / lease binding (rejects stale or cross-turn responses, §17)
        if manifest["session_id"] != session_id:
            raise StateError("response session mismatch")
        if manifest["turn_id"] != turn_id:
            raise StateError("response is for a different turn")
        if manifest["epoch"] != epoch:
            raise StateError("response epoch mismatch — stale, will not render")
        if cancelled:
            raise StateError("turn is cancelled — response will not render")

        body_bytes = self.store.resolve(session_id, manifest["body"])
        if len(body_bytes) > MAX_BODY_BYTES:
            raise ProtocolError("response body exceeds size limit")

        import hashlib
        if hashlib.sha256(body_bytes).hexdigest() != manifest["body_sha256"]:
            raise IntegrityError("response body digest mismatch")

        try:
            body = body_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(f"response body is not valid UTF-8: {exc}") from exc

        if body.count("\n") + 1 > MAX_RENDERED_LINES:
            raise ProtocolError("response exceeds rendered-line limit")

        if not allow_terminal_escapes and ("\x1b" in body or "\x9b" in body):
            # No terminal-control escape injection unless explicitly supported (§16.3,
            # adversarial case 9).
            raise ProtocolError("response contains terminal control escapes")

        return body, manifest
