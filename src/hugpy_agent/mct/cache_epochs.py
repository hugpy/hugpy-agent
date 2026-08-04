"""Epochs and adapter read-receipts.

Design ref: §12 (three caches, receipts, epoch handshake), §21 (``cache_epochs.py``).
Serves enforcement rows 6 (epoch identity) and the receipt half of row 7.

Key doctrine: *delivery is not residency* (invariant 7). B knows exactly what the
adapter opened (receipts), but must not assume those bytes remain in A's context
after an epoch change. Receipts are recorded by the adapter transport, never by
A's prose (§12.2).
"""
from __future__ import annotations

import json

from .ledger import Ledger


class EpochManager:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def active(self, session_id: str) -> str:
        return self.ledger.active_epoch(session_id)

    def change(self, session_id: str, reason: str) -> str:
        """Bump the epoch and, by doing so, invalidate prior residency
        assumptions (§12.3). Durable read history is retained for audit."""
        return self.ledger.new_epoch(session_id, reason)


def seal_receipt_bytes(receipts: list[dict]) -> bytes:
    """Serialize the adapter-recorded reads into a ``context_receipt`` body (§7.3)."""
    payload = {
        "schema": "mct.context-receipt/1",
        "opened": [
            {
                "object": r["object_id"],
                "digest": r["digest"],
                "selector": r["selector"],
                "placed_in_input": bool(r["placed_in_input"]),
                "purpose": r["purpose"],
                "timestamp": r["timestamp"],
                "manifest_sha256": r["manifest_sha256"],
            }
            for r in receipts
        ],
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
