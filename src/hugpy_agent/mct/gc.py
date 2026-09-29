"""Garbage collection: mark-and-sweep from durable roots.

Design ref: §17.2. The authoritative state is the object store + append-only
ledger (§17.1); everything else is rebuildable. GC never collects an object that
is still referenced (by any ledger object row, a live turn, or a legal hold);
it only reclaims orphaned bytes — e.g. a file left by a commit that renamed into
the store but crashed before recording its ledger row (§7.5).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GcReport:
    scanned_files: int = 0
    collectable: list[str] = field(default_factory=list)  # digests
    retained: int = 0
    freed_bytes: int = 0
    dry_run: bool = True


def garbage_collect(server, *, dry_run: bool = True, legal_hold: set[str] | None = None) -> GcReport:
    """Sweep object files not referenced by any live ledger object row.

    Roots = every digest present in the ``objects`` table (each is referenced by a
    session-scoped object id, and thus by the events/receipts/facts that point to
    it). ``legal_hold`` digests are never collected regardless.
    """
    legal_hold = legal_hold or set()
    live_digests = server.ledger.all_object_digests() | legal_hold
    report = GcReport(dry_run=dry_run)

    for path in server.store.objects_dir.rglob("*"):
        if not path.is_file():
            continue
        report.scanned_files += 1
        digest = path.name
        if digest in live_digests:
            report.retained += 1
            continue
        report.collectable.append(digest)
        report.freed_bytes += path.stat().st_size
        if not dry_run:
            # Quarantine rather than hard-delete: reversible within the window (§17.2).
            path.replace(server.store.quarantine_dir / digest)
    return report
