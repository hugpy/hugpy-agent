"""Startup reconciliation.

Design ref: §17.1 (authoritative state = object store + append-only ledger;
runtime buffers are disposable), §17.2 (GC), §21 (``recovery.py``).

The durable tables (idempotency, renders, events) already make resend/replay safe
after a crash. Reconciliation's job is therefore narrow: drop orphaned temp
writes (which never received a pointer, §7.5), verify the hash chain, and report
turns that were interrupted mid-flight so an operator/adapter can resume them.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ReconcileReport:
    removed_temp_objects: int = 0
    unfinished_turns: list[dict] = field(default_factory=list)
    chains_ok: bool = True
    sessions_checked: int = 0


def reconcile(server) -> ReconcileReport:
    report = ReconcileReport()
    report.removed_temp_objects = server.store.gc_temp()
    report.unfinished_turns = server.ledger.unfinished_turns()

    # Verify tamper-evidence chain per session touched by an unfinished turn,
    # plus any session with events (cheap for the prototype).
    sessions = {t["session_id"] for t in report.unfinished_turns}
    rows = server.ledger._db.execute("SELECT DISTINCT session_id FROM events").fetchall()
    sessions.update(r["session_id"] for r in rows)
    for sid in sessions:
        report.sessions_checked += 1
        if not server.ledger.verify_chain(sid):
            report.chains_ok = False
    return report
