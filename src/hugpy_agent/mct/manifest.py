"""``ArtifactManifest``: provenance + lineage for every significant artifact.

Design ref: `hugpy_oracle_video_architecture.md` §3.4 — every artifact carries
content hash and media metadata, artifact type and schema version, parent
artifacts and producing node, original source and acquisition time, model /
adapter / prompt / parameters / seed, rights and consent classification,
evaluation results, disclosure policy (including whether the frontier model A
may access it), an immutable storage pointer, and recipe / run / graph-revision
identifiers. Roadmap task k96.

The doc asks whether the existing ``.hugpy_agent/mct/objects`` object plane can
back this contract. It can, and this module is that backing — no new store:

* A manifest is an ordinary content-addressed object (``kind=
  "artifact_manifest"``, ``media_type="application/json"``) whose bytes are the
  manifest's own deterministic JSON. Therefore **the manifest's sha256 IS the
  artifact's stable id**: ``ManifestRef.digest == sha256(canonical JSON)``, and
  ``ObjectStore.resolve`` re-verifies it (and quarantines on mismatch) on every
  read, for free.
* ``parents`` are *manifest* digests — the stable ids — not content digests, so
  ``lineage()`` is a walk over the same address space it returns.
* Idempotency: the manifest JSON contains ``created_at``, so re-serializing the
  same artifact at a later wall-clock time would otherwise produce different
  bytes. ``ManifestStore.commit`` keys the ledger's existing idempotency table
  on everything *except* ``created_at``; a re-commit of identical fields returns
  the first manifest verbatim (same digest, same ``created_at``) and emits no
  second ledger event.

Honesty rule (project-wide): a field that is not known is ``None``. Nothing here
sniffs, infers or defaults media metadata — the producer states what it knows.

Style: stdlib only, frozen+slotted dataclasses with ``to_dict``/``from_dict``,
closed vocabularies as module constants, structurally-invalid values raised in
``__post_init__`` — the same idiom as ``AH/oracle/contracts.py``. The typed
authority/consent object lands in k97 on the central side; ``rights`` here is a
deliberately coarse classification string, not an authority decision.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import Any, Mapping

from .errors import IntegrityError, NotFoundError, StateError
from .ledger import Ledger, now_iso
from .objects import ObjectRef, ObjectStore, relative_object_path
from .protocol import make_pointer

SCHEMA_VERSION = "mct.artifact_manifest/1"

# --- closed vocabularies ---------------------------------------------------

RIGHTS_UNKNOWN = "unknown"
#: Coarse rights/consent classification. NOT an authority decision: k97 owns the
#: typed ``Authorization``/``RightsManifest`` contract on the central side. This
#: field records what the producer could state about the artifact's provenance.
RIGHTS_CLASSES = frozenset({
    RIGHTS_UNKNOWN,     # never asserted — the default, and never guessed
    "owned",            # produced by us from inputs we own
    "licensed",         # covered by a recorded licence
    "consented",        # identity/voice used with recorded consent
    "public_domain",
    "restricted",       # known-restricted; do not publish or redistribute
})

DISCLOSURE_LOCAL_ONLY = "local_only"
#: What B may disclose to A (the frontier model). B is the broker; A never
#: reads the object plane directly (MCT invariant 3/5).
DISCLOSURE_CLASSES = frozenset({
    DISCLOSURE_LOCAL_ONLY,  # bytes never leave B
    "excerpt_only",         # bounded excerpts may be placed in A's context
    "full",                 # the whole artifact may be placed in A's context
})

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

#: Largest excerpt (bytes) B may place in A's context from an artifact whose
#: disclosure is ``excerpt_only`` (POLICY-rights-consent-disclosure §3.4).
EXCERPT_MAX_BYTES = 4096


# ---------------------------------------------------------------------------
# The B->A disclosure verdict (k113; policy §3). One function, consulted by
# ``a_adapter`` before any artifact bytes or listing entry leave B for A.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DisclosureVerdict:
    """What B may hand A about ONE artifact. ``allowed`` False is a refusal;
    ``limit`` (bytes) is set for an excerpt-bounded read; ``reason`` is the
    operator-readable why, and names no content."""
    allowed: bool
    reason: str
    disclosure: str
    frontier_may_access: bool
    limit: int | None = None
    artifact_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason,
                "disclosure": self.disclosure,
                "frontier_may_access": self.frontier_may_access,
                "limit": self.limit, "artifact_type": self.artifact_type}


def disclosure_verdict(manifest: "ArtifactManifest", *,
                       selector: str | None = None) -> DisclosureVerdict:
    """Policy §3.2-3.4, in order: ``frontier_may_access=False`` refuses
    (whatever ``disclosure`` says); ``local_only`` refuses; ``excerpt_only``
    admits a SELECTOR-bounded read capped at ``EXCERPT_MAX_BYTES`` and refuses
    a whole-object read; ``full`` admits the object."""
    base = dict(disclosure=manifest.disclosure,
                frontier_may_access=manifest.frontier_may_access,
                artifact_type=manifest.artifact_type)
    if not manifest.frontier_may_access:
        return DisclosureVerdict(
            allowed=False, reason="frontier_may_access=False: B does not "
                                  "disclose this artifact to A", **base)
    if manifest.disclosure == DISCLOSURE_LOCAL_ONLY:
        return DisclosureVerdict(
            allowed=False, reason="disclosure=local_only: bytes never leave B",
            **base)
    if manifest.disclosure == "excerpt_only":
        if not selector:
            return DisclosureVerdict(
                allowed=False, reason="disclosure=excerpt_only: a whole-object "
                                      "read is refused; name a selector", **base)
        return DisclosureVerdict(allowed=True, limit=EXCERPT_MAX_BYTES,
                                 reason="excerpt_only: selector-bounded read",
                                 **base)
    return DisclosureVerdict(allowed=True, reason="disclosure=full", **base)


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    """Deterministic JSON: sorted keys, no whitespace, ASCII-escaped."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def normalize_parameters(params: Any) -> tuple[tuple[str, str], ...]:
    """Generation parameters -> canonical sorted ``(key, json-text)`` pairs.

    Tuple-of-pairs keeps the dataclass frozen and hashable (the same trick as
    ``ExecutionReceipt.normalize_request``). Accepts a mapping (the normal call
    site) or an already-normalized pair sequence, whose values must be valid
    JSON text — pass a mapping if you have raw Python values.
    """
    if params is None:
        return ()
    if isinstance(params, Mapping):
        return tuple(
            (str(k), json.dumps(params[k], sort_keys=True, separators=(",", ":")))
            for k in sorted(params, key=str)
        )
    out: list[tuple[str, str]] = []
    for pair in params:
        try:
            key, value = pair
        except (TypeError, ValueError) as exc:
            raise ValueError(f"parameters entry is not a (key, value) pair: {pair!r}") from exc
        if not isinstance(value, str):
            raise ValueError(
                f"parameter {key!r}: pair form takes JSON text, got {type(value).__name__}; "
                "pass a mapping to encode raw values")
        try:
            json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"parameter {key!r} is not valid JSON text: {value!r}") from exc
        out.append((str(key), value))
    return tuple(sorted(out, key=lambda kv: kv[0]))


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArtifactManifest:
    """Immutable provenance record for one artifact (§3.4).

    Required: what the artifact *is* (``artifact_type``), what it *contains*
    (``content_sha256``, ``size_bytes``, ``media_type``). Everything else is
    optional and defaults to "not known" — ``None``, ``()`` or the ``unknown``
    rights class. Nothing is inferred.

    ``storage_pointer`` is the immutable, content-addressed location relative to
    the object-store root (``objects/sha256/ab/cd/<digest>``), derived from
    ``content_sha256`` when left empty. It is deliberately NOT an ``mct://``
    pointer: those embed a per-commit opaque ``object_id``, which would make the
    manifest non-idempotent (a second commit of identical bytes mints a new id).
    """

    # --- identity ---------------------------------------------------------
    artifact_type: str                       # e.g. "audio.master", "video.segment"
    content_sha256: str
    size_bytes: int
    media_type: str
    # --- media metadata (None = not cheaply knowable; never guessed) ------
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    frame_rate: float | None = None
    sample_rate_hz: int | None = None
    # --- lineage ----------------------------------------------------------
    parents: tuple[str, ...] = ()            # PARENT MANIFEST digests
    producer: str | None = None              # producing node / tool name
    run_id: str | None = None
    recipe_id: str | None = None
    graph_revision: str | None = None
    # --- acquisition ------------------------------------------------------
    source_uri: str | None = None            # original source, verbatim
    acquired_at: str | None = None           # ISO-8601 UTC
    # --- generation -------------------------------------------------------
    model: str | None = None
    adapter: str | None = None
    prompt: str | None = None
    parameters: tuple[tuple[str, str], ...] = ()
    seed: int | None = None
    # --- rights + disclosure ---------------------------------------------
    rights: str = RIGHTS_UNKNOWN
    disclosure: str = DISCLOSURE_LOCAL_ONLY
    frontier_may_access: bool = False        # default deny: B is the broker
    # --- evaluation -------------------------------------------------------
    evaluations: tuple[str, ...] = ()        # scorecard object digests / ids
    # --- storage ----------------------------------------------------------
    storage_pointer: str = ""                # derived from content_sha256 if empty
    schema_version: str = SCHEMA_VERSION
    created_at: str | None = None            # stamped by ManifestStore.commit

    def __post_init__(self) -> None:
        _set = object.__setattr__
        if not self.artifact_type or not isinstance(self.artifact_type, str):
            raise ValueError("artifact_type must be a non-empty string")
        if not isinstance(self.content_sha256, str) or not _HEX64.match(self.content_sha256):
            raise ValueError(f"content_sha256 is not a sha256 hex digest: {self.content_sha256!r}")
        if not _is_int(self.size_bytes) or self.size_bytes < 0:
            raise ValueError(f"size_bytes must be a non-negative int: {self.size_bytes!r}")
        if not self.media_type or not isinstance(self.media_type, str):
            raise ValueError("media_type must be a non-empty string")

        for name in ("width", "height", "sample_rate_hz"):
            value = getattr(self, name)
            if value is not None and (not _is_int(value) or value <= 0):
                raise ValueError(f"{name} must be a positive int or None: {value!r}")
        for name in ("duration_s", "frame_rate"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"{name} must be a non-negative number or None: {value!r}")
            _set(self, name, float(value))

        parents = tuple(self.parents)
        for parent in parents:
            if not isinstance(parent, str) or not _HEX64.match(parent):
                raise ValueError(f"parent is not a manifest digest: {parent!r}")
        if len(set(parents)) != len(parents):
            raise ValueError("duplicate parent digests")
        _set(self, "parents", parents)

        evaluations = tuple(self.evaluations)
        for ref in evaluations:
            if not isinstance(ref, str) or not ref:
                raise ValueError(f"evaluation reference must be a non-empty string: {ref!r}")
        _set(self, "evaluations", evaluations)

        _set(self, "parameters", normalize_parameters(self.parameters))
        if self.seed is not None and not _is_int(self.seed):
            raise ValueError(f"seed must be an int or None: {self.seed!r}")

        if self.rights not in RIGHTS_CLASSES:
            raise ValueError(
                f"unknown rights class {self.rights!r}; one of {sorted(RIGHTS_CLASSES)}")
        if self.disclosure not in DISCLOSURE_CLASSES:
            raise ValueError(
                f"unknown disclosure policy {self.disclosure!r}; "
                f"one of {sorted(DISCLOSURE_CLASSES)}")
        if not isinstance(self.frontier_may_access, bool):
            raise ValueError("frontier_may_access must be a bool")
        if self.frontier_may_access and self.disclosure == DISCLOSURE_LOCAL_ONLY:
            raise ValueError(
                "frontier_may_access=True contradicts disclosure='local_only'; "
                "widen disclosure explicitly")

        if not self.storage_pointer:
            _set(self, "storage_pointer", relative_object_path(self.content_sha256))
        if not self.schema_version:
            raise ValueError("schema_version must be non-empty")

    # --- constructors ------------------------------------------------------
    @classmethod
    def for_bytes(cls, data: bytes, *, artifact_type: str, media_type: str,
                  **fields: Any) -> "ArtifactManifest":
        """Manifest for bytes we hold: hashes them and derives size + pointer.

        Media metadata is NOT sniffed here — pass ``width=``/``duration_s=``/…
        if the producer knows them, otherwise they stay ``None``.
        """
        if not isinstance(data, (bytes, bytearray)):
            raise ValueError("artifact data must be bytes")
        return cls(artifact_type=artifact_type, media_type=media_type,
                   content_sha256=hashlib.sha256(data).hexdigest(),
                   size_bytes=len(data), **fields)

    # --- wire shape --------------------------------------------------------
    def parameters_dict(self) -> dict[str, Any]:
        return {k: json.loads(v) for k, v in self.parameters}

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict. Every key is always present, so the serialization is
        positionally stable and the digest depends only on the values."""
        return {
            "schema_version": self.schema_version,
            "artifact_type": self.artifact_type,
            "content_sha256": self.content_sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "width": self.width,
            "height": self.height,
            "duration_s": self.duration_s,
            "frame_rate": self.frame_rate,
            "sample_rate_hz": self.sample_rate_hz,
            "parents": list(self.parents),
            "producer": self.producer,
            "run_id": self.run_id,
            "recipe_id": self.recipe_id,
            "graph_revision": self.graph_revision,
            "source_uri": self.source_uri,
            "acquired_at": self.acquired_at,
            "model": self.model,
            "adapter": self.adapter,
            "prompt": self.prompt,
            "parameters": self.parameters_dict(),
            "seed": self.seed,
            "rights": self.rights,
            "disclosure": self.disclosure,
            "frontier_may_access": self.frontier_may_access,
            "evaluations": list(self.evaluations),
            "storage_pointer": self.storage_pointer,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ArtifactManifest":
        """Strict: unknown keys and unknown schema versions are REJECTED.

        A manifest is content-addressed, so silently dropping a field would
        change the digest of a round-tripped manifest — a lie about identity.
        Better to refuse to read what this version does not understand.
        """
        unknown = sorted(set(d) - set(cls.__dataclass_fields__))
        if unknown:
            raise ValueError(f"unknown ArtifactManifest fields: {', '.join(unknown)}")
        version = d.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported manifest schema_version {version!r} (this build reads "
                f"{SCHEMA_VERSION!r})")
        missing = [k for k in ("artifact_type", "content_sha256", "size_bytes", "media_type")
                   if k not in d]
        if missing:
            raise ValueError(f"missing required manifest fields: {', '.join(missing)}")
        return cls(
            artifact_type=d["artifact_type"],
            content_sha256=d["content_sha256"],
            size_bytes=d["size_bytes"],
            media_type=d["media_type"],
            width=d.get("width"),
            height=d.get("height"),
            duration_s=d.get("duration_s"),
            frame_rate=d.get("frame_rate"),
            sample_rate_hz=d.get("sample_rate_hz"),
            parents=tuple(d.get("parents") or ()),
            producer=d.get("producer"),
            run_id=d.get("run_id"),
            recipe_id=d.get("recipe_id"),
            graph_revision=d.get("graph_revision"),
            source_uri=d.get("source_uri"),
            acquired_at=d.get("acquired_at"),
            model=d.get("model"),
            adapter=d.get("adapter"),
            prompt=d.get("prompt"),
            parameters=d.get("parameters") or {},
            seed=d.get("seed"),
            rights=d.get("rights", RIGHTS_UNKNOWN),
            disclosure=d.get("disclosure", DISCLOSURE_LOCAL_ONLY),
            frontier_may_access=bool(d.get("frontier_may_access", False)),
            evaluations=tuple(d.get("evaluations") or ()),
            storage_pointer=d.get("storage_pointer") or "",
            schema_version=version,
            created_at=d.get("created_at"),
        )

    # --- identity ----------------------------------------------------------
    def canonical_bytes(self) -> bytes:
        """The exact bytes stored in the object plane."""
        return _canonical_json(self.to_dict())

    def digest(self) -> str:
        """sha256 of :meth:`canonical_bytes` — the artifact's stable id.

        Only final once ``created_at`` is stamped (``ManifestStore.commit``
        does that); before then it is the digest of a draft.
        """
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def identity_key(self) -> str:
        """Digest over every field EXCEPT ``created_at`` — the idempotency key.

        Two commits of the same bytes with the same provenance are the same
        artifact even though they happen at different times.
        """
        payload = self.to_dict()
        payload.pop("created_at", None)
        return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Handles
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ManifestRef:
    """A committed manifest: its stable id plus the session-scoped handles.

    ``digest`` is stable and shareable (it is the artifact id used in
    ``parents``). ``object_id``/``pointer`` are opaque, session-scoped handles
    for the manifest JSON; ``content_*`` are the same for the artifact bytes and
    are ``None`` when those bytes are not in this session's object store (a
    manifest may legitimately describe an artifact held elsewhere).
    ``created`` is False when an identical manifest already existed.
    """
    digest: str
    manifest: ArtifactManifest
    object_id: str
    pointer: str
    content_object_id: str | None = None
    content_pointer: str | None = None
    created: bool = True


@dataclass(frozen=True, slots=True)
class LineageNode:
    """One step of a lineage/children walk.

    ``manifest`` is ``None`` when the ancestor's digest is referenced by a child
    but is not present in this session's store — reported, never invented.
    """
    digest: str
    depth: int
    manifest: ArtifactManifest | None = None


# ---------------------------------------------------------------------------
# The manifest plane
# ---------------------------------------------------------------------------


class ManifestStore:
    """Commits and walks :class:`ArtifactManifest` objects on the MCT object plane.

    Mirrors ``compaction.Compaction``: a thin policy layer over
    ``(ObjectStore, Ledger)`` that owns no storage of its own.
    """

    OBJECT_KIND = "artifact_manifest"
    MEDIA_TYPE = "application/json"
    EVENT_TYPE = "artifact.committed"
    ACTOR = "B.manifest"
    IDEMPOTENCY_PREFIX = "mct.manifest"

    def __init__(self, store: ObjectStore, ledger: Ledger):
        self.store = store
        self.ledger = ledger

    # --- commit ------------------------------------------------------------
    def commit_artifact(self, session_id: str, data: bytes, *, artifact_type: str,
                        media_type: str, content_kind: str = "artifact",
                        turn_id: str | None = None, epoch: str | None = None,
                        **fields: Any) -> ManifestRef:
        """Commit artifact bytes AND their manifest. ``**fields`` are manifest
        fields (``parents``, ``producer``, ``model``, ``seed``, ``rights``, …).

        Idempotent end to end: identical bytes + identical manifest fields reuse
        the existing content object and return the first manifest unchanged.
        """
        manifest = ArtifactManifest.for_bytes(
            data, artifact_type=artifact_type, media_type=media_type, **fields)
        self._commit_content(session_id, data, media_type=media_type,
                             kind=content_kind, manifest=manifest)
        return self.commit(session_id, manifest, turn_id=turn_id, epoch=epoch)

    def commit(self, session_id: str, manifest: ArtifactManifest, *,
               turn_id: str | None = None, epoch: str | None = None) -> ManifestRef:
        """Commit a manifest as a content-addressed object + one ledger event.

        The artifact bytes need not be in the store: the manifest is a record
        about them, and ``content_sha256``/``storage_pointer`` stay meaningful
        either way.

        Ordering: object commit -> ledger event -> idempotency record. A crash
        between the last two can replay the event (at-least-once audit) but
        never mints a second artifact id (exactly-once identity).
        """
        key = self._idempotency_key(session_id, manifest)
        recorded = self.ledger.idempotency_get(key)
        if recorded is not None:
            return self._recommit_existing(session_id, manifest, recorded)

        stored = manifest if manifest.created_at else replace(manifest, created_at=now_iso())
        ref = self.store.commit(session_id, stored.canonical_bytes(),
                                media_type=self.MEDIA_TYPE, kind=self.OBJECT_KIND,
                                provenance=self._provenance(stored))
        self.ledger.append_event(
            session_id, turn_id or "", epoch or self._epoch(session_id),
            self.EVENT_TYPE, self.ACTOR,
            input_objects=self._parent_object_ids(session_id, stored),
            output_objects=[ref.object_id], idempotency_key=key,
        )
        self.ledger.idempotency_put(
            key, session_id,
            {"manifest_sha256": ref.sha256, "created_at": stored.created_at})
        return self._ref(session_id, ref.sha256, stored, ref.object_id, ref.pointer,
                         created=True)

    # --- read --------------------------------------------------------------
    def get(self, session_id: str, digest: str) -> ArtifactManifest:
        """Load a manifest by its digest. Raises :class:`NotFoundError` if this
        session has no such manifest; :class:`IntegrityError` (after
        quarantining the bytes) if the stored JSON no longer hashes to its
        recorded digest — the object plane's invariant 4, inherited for free."""
        row = self.ledger.object_by_digest(session_id, digest, kind=self.OBJECT_KIND)
        if row is None:
            raise NotFoundError(f"unknown artifact manifest: {digest}")
        return self._load(session_id, row["object_id"])

    def try_get(self, session_id: str, digest: str) -> ArtifactManifest | None:
        """:meth:`get`, but ``None`` for an absent manifest. Integrity failures
        still raise — a corrupt manifest is not an absent one."""
        try:
            return self.get(session_id, digest)
        except NotFoundError:
            return None

    def lineage(self, session_id: str, digest: str, depth: int = 3) -> list[LineageNode]:
        """Ancestors of ``digest``, breadth-first, up to ``depth`` generations.

        Returns one :class:`LineageNode` per distinct ancestor digest, ordered
        by generation then by the parent order recorded in each manifest. An
        ancestor that is not in this session's store is returned with
        ``manifest=None`` rather than omitted or fabricated.

        Complexity: O(ancestors) manifest reads, each an indexed digest lookup
        plus one verified object read. The root must exist (else NotFoundError).
        """
        if depth < 0:
            raise ValueError(f"depth must be >= 0: {depth}")
        root = self.get(session_id, digest)
        seen = {digest}
        out: list[LineageNode] = []
        level = list(root.parents)
        generation = 1
        while level and generation <= depth:
            following: list[str] = []
            for parent in level:
                if parent in seen:
                    continue
                seen.add(parent)
                found = self.try_get(session_id, parent)
                out.append(LineageNode(digest=parent, depth=generation, manifest=found))
                if found is not None:
                    following.extend(found.parents)
            level = following
            generation += 1
        return out

    def children(self, session_id: str, digest: str) -> list[LineageNode]:
        """Manifests naming ``digest`` as a parent, oldest first.

        Complexity: O(n) verified reads over the session's manifests — there is
        no parent index. That is deliberate for now: the index would be a second
        source of truth for something the manifests already state, and session-
        scale n is small. If this becomes hot, add a ``manifest_parents`` table
        to the ledger and keep this scan as the rebuild path.

        Fails closed: one corrupt manifest anywhere in the session raises
        :class:`IntegrityError` rather than returning a partial answer.
        """
        out: list[LineageNode] = []
        seen: set[str] = set()
        for row in self.ledger.list_objects(session_id, kinds=[self.OBJECT_KIND],
                                            newest_first=False):
            if row["digest"] in seen:
                continue  # a re-materialized manifest: same bytes, second handle
            seen.add(row["digest"])
            child = self._load(session_id, row["object_id"])
            if digest in child.parents:
                out.append(LineageNode(digest=row["digest"], depth=1, manifest=child))
        return out

    def list_manifests(self, session_id: str) -> list[LineageNode]:
        """Every manifest in the session, oldest first (depth 0). O(n) reads."""
        out: list[LineageNode] = []
        seen: set[str] = set()
        for row in self.ledger.list_objects(session_id, kinds=[self.OBJECT_KIND],
                                            newest_first=False):
            if row["digest"] in seen:
                continue
            seen.add(row["digest"])
            out.append(LineageNode(digest=row["digest"], depth=0,
                                   manifest=self._load(session_id, row["object_id"])))
        return out

    def for_content(self, session_id: str, content_sha256: str) -> ArtifactManifest | None:
        """The manifest that describes the artifact whose BYTES hash to
        ``content_sha256`` — how the disclosure gate gets from an object
        pointer (content) to a policy (manifest). ``None`` when no manifest in
        this session claims those bytes; the newest claim wins when several
        do. O(n) reads, like ``list_manifests``."""
        found: ArtifactManifest | None = None
        for node in self.list_manifests(session_id):
            if node.manifest is not None and node.manifest.content_sha256 == content_sha256:
                found = node.manifest
        return found

    # --- internals ---------------------------------------------------------
    def _load(self, session_id: str, object_id: str) -> ArtifactManifest:
        raw = self.store.resolve(session_id, make_pointer(session_id, object_id))
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrityError(f"manifest {object_id} is not valid JSON: {exc}") from exc
        return ArtifactManifest.from_dict(payload)

    def _idempotency_key(self, session_id: str, manifest: ArtifactManifest) -> str:
        # Session-scoped: object handles are session-scoped, and two sessions
        # committing identical bytes must each get their own committed record.
        return f"{self.IDEMPOTENCY_PREFIX}:{session_id}:{manifest.identity_key()}"

    def _recommit_existing(self, session_id: str, manifest: ArtifactManifest,
                           recorded: Mapping[str, Any]) -> ManifestRef:
        """Return the manifest committed the first time, verbatim."""
        stored = replace(manifest, created_at=recorded.get("created_at"))
        digest = stored.digest()
        if digest != recorded.get("manifest_sha256"):
            raise IntegrityError(
                "manifest idempotency record does not match its manifest "
                f"({digest} != {recorded.get('manifest_sha256')})")
        row = self.ledger.object_by_digest(session_id, digest, kind=self.OBJECT_KIND)
        if row is not None and self.store.has_digest(digest):
            object_id = row["object_id"]
            pointer = make_pointer(session_id, object_id)
        else:
            # The bytes were collected or never landed; re-materialize them with
            # the recorded created_at so the digest — the artifact id — is
            # unchanged. No second event: the commit already happened.
            ref = self.store.commit(session_id, stored.canonical_bytes(),
                                    media_type=self.MEDIA_TYPE, kind=self.OBJECT_KIND,
                                    provenance=self._provenance(stored))
            object_id, pointer = ref.object_id, ref.pointer
        return self._ref(session_id, digest, stored, object_id, pointer, created=False)

    def _commit_content(self, session_id: str, data: bytes, *, media_type: str,
                        kind: str, manifest: ArtifactManifest) -> ObjectRef:
        row = self.ledger.object_by_digest(session_id, manifest.content_sha256,
                                           kind=kind, media_type=media_type)
        if row is not None and self.store.has_digest(manifest.content_sha256):
            return ObjectRef(
                object_id=row["object_id"], session_id=session_id,
                pointer=make_pointer(session_id, row["object_id"]),
                sha256=row["digest"], size=row["size"],
                media_type=row["media_type"], kind=row["kind"],
            )
        return self.store.commit(session_id, data, media_type=media_type, kind=kind,
                                 provenance={"artifact_type": manifest.artifact_type})

    def _ref(self, session_id: str, digest: str, manifest: ArtifactManifest,
             object_id: str, pointer: str, *, created: bool) -> ManifestRef:
        content = self.ledger.object_by_digest(session_id, manifest.content_sha256)
        return ManifestRef(
            digest=digest, manifest=manifest, object_id=object_id, pointer=pointer,
            content_object_id=content["object_id"] if content else None,
            content_pointer=(make_pointer(session_id, content["object_id"])
                             if content else None),
            created=created,
        )

    def _provenance(self, manifest: ArtifactManifest) -> dict:
        """Body-free ledger annotation, so log lines are self-describing (§18.3)."""
        return {
            "catalog_name": manifest.artifact_type,
            "artifact_type": manifest.artifact_type,
            "content_sha256": manifest.content_sha256,
            "parents": list(manifest.parents),
            "producer": manifest.producer,
            "run_id": manifest.run_id,
        }

    def _parent_object_ids(self, session_id: str, manifest: ArtifactManifest) -> list[str]:
        """Parent manifest handles that exist here — the event's input objects."""
        ids: list[str] = []
        for parent in manifest.parents:
            row = self.ledger.object_by_digest(session_id, parent, kind=self.OBJECT_KIND)
            if row is not None:
                ids.append(row["object_id"])
        return ids

    def _epoch(self, session_id: str) -> str | None:
        """The session's active epoch, or None when the caller works outside a
        session lifecycle (a manifest is still a fact; the epoch just isn't known)."""
        try:
            return self.ledger.active_epoch(session_id)
        except StateError:
            return None
