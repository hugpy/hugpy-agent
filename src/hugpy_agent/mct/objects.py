"""Immutable, integrity-addressed object store.

Design ref: §7.1 (object properties), §7.4 (physical layout), §7.5 (atomic
commit), §21 (``objects.py``). Serves enforcement rows 2 (digest-verified
resolve), 3 (opaque-handle mapping — never ``open(path)``), and 4 (atomic commit).

Bytes are content-addressed under ``objects/sha256/ab/cd/<digest>`` and may be
physically deduplicated, but every ``object_id`` is opaque and session-scoped
(§7.4). The ledger holds the ``object_id -> (session, digest, ...)`` mapping;
resolving is a table lookup, never a filesystem path from A.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from . import ids
from .errors import IntegrityError, IsolationError, NotFoundError, QuotaError
from .excerpt import apply_selector
from .ledger import Ledger
from .protocol import make_pointer, parse_pointer


@dataclass(frozen=True)
class ObjectRef:
    object_id: str
    session_id: str
    pointer: str
    sha256: str
    size: int
    media_type: str
    kind: str


class ObjectStore:
    def __init__(self, root: str | Path, ledger: Ledger, *, session_quota_bytes: int = 0):
        self.root = Path(root)
        self.objects_dir = self.root / "objects" / "sha256"
        self.quarantine_dir = self.root / "quarantine"
        self.tmp_dir = self.root / "objects" / "_tmp"
        for d in (self.objects_dir, self.quarantine_dir, self.tmp_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.ledger = ledger
        self.session_quota_bytes = session_quota_bytes  # 0 = unlimited

    def _path_for_digest(self, digest: str) -> Path:
        return self.objects_dir / digest[:2] / digest[2:4] / digest

    def path_for(self, session_id: str, object_id: str) -> Path | None:
        """The physical file backing an object — so 'where is that document?' has
        a concrete answer for every pointer."""
        meta = self.ledger.get_object(object_id)
        if not meta or meta["session_id"] != session_id:
            return None
        return self._path_for_digest(meta["digest"])

    def locate(self, pointer: str) -> Path | None:
        session_id, object_id = parse_pointer(pointer)
        return self.path_for(session_id, object_id)

    # --- commit (design §7.5) ----------------------------------------------
    def commit(
        self,
        session_id: str,
        data: bytes,
        *,
        media_type: str,
        kind: str,
        provenance: dict | None = None,
    ) -> ObjectRef:
        """Atomically commit bytes and return an opaque, session-scoped handle.

        Order (§7.5): temp write on same fs -> verify digest+size -> flush &
        permission -> atomic rename -> flush dir -> ledger record. An interrupted
        write is left in ``_tmp`` for GC and never receives a valid pointer.
        """
        if not isinstance(data, (bytes, bytearray)):
            raise IntegrityError("object data must be bytes")
        # Per-session disk quota (§13.2, §17): fail closed, preserving existing state.
        if self.session_quota_bytes:
            used = self.ledger.session_object_bytes(session_id)
            if used + len(data) > self.session_quota_bytes:
                raise QuotaError(
                    f"session {session_id} quota exhausted: {used}+{len(data)} "
                    f"> {self.session_quota_bytes}")
        digest = hashlib.sha256(data).hexdigest()
        final = self._path_for_digest(digest)
        final.parent.mkdir(parents=True, exist_ok=True)

        if not final.exists():  # physical dedup: identical bytes stored once
            fd, tmp_name = _mkstemp(self.tmp_dir)
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                # verify what actually landed on disk before publishing
                if hashlib.sha256(tmp.read_bytes()).hexdigest() != digest:
                    raise IntegrityError("digest mismatch after write")
                os.chmod(tmp, 0o440)
                os.replace(tmp, final)  # atomic rename
                _fsync_dir(final.parent)
            except BaseException:
                if tmp.exists():
                    tmp.replace(self.quarantine_dir / tmp.name)
                raise

        object_id = ids.new_object_id()
        self.ledger.record_object(
            object_id, session_id, digest, media_type, kind, len(data), provenance
        )
        return ObjectRef(
            object_id=object_id,
            session_id=session_id,
            pointer=make_pointer(session_id, object_id),
            sha256=digest,
            size=len(data),
            media_type=media_type,
            kind=kind,
        )

    # --- resolve (design §7.2, invariants 4 & 5) ---------------------------
    def resolve(self, session_id: str, pointer: str, selector: str | None = None) -> bytes:
        """Return authorized, digest-verified bytes for ``pointer``.

        Enforcement: the pointer is parsed to ``(session, object_id)``; the
        requesting ``session_id`` must match (cross-session access is an
        :class:`IsolationError`, adversarial case 5); the recorded digest is
        recomputed against the stored bytes before any byte is returned
        (invariant 4). A never supplies a host path — only an ``mct://`` handle.
        """
        ptr_session, object_id = parse_pointer(pointer)
        if ptr_session != session_id:
            raise IsolationError("pointer belongs to a different session")
        meta = self.ledger.get_object(object_id)
        if meta is None:
            raise NotFoundError(f"unknown object: {object_id}")
        if meta["session_id"] != session_id:
            raise IsolationError("object is not owned by this session")

        path = self._path_for_digest(meta["digest"])
        if not path.exists():
            raise NotFoundError("object bytes missing from store")
        data = path.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != meta["digest"]:
            # quarantine and refuse (invariant 4)
            path.replace(self.quarantine_dir / meta["digest"])
            raise IntegrityError("stored object failed digest verification")

        if selector is not None:
            data = apply_selector(data, selector)
        return data

    def gc_temp(self) -> int:
        """Remove orphaned temp writes; they never received a pointer (§17.2)."""
        removed = 0
        for p in self.tmp_dir.glob("*"):
            p.unlink()
            removed += 1
        return removed


# --- helpers ---------------------------------------------------------------

def _mkstemp(directory: Path):
    import tempfile
    return tempfile.mkstemp(dir=str(directory), prefix="obj-", suffix=".tmp")


def _fsync_dir(directory: Path) -> None:
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
