"""ArtifactManifest + lineage on the MCT object plane (k96, architecture §3.4).

What these tests hold to account:
  * the manifest is a faithful, content-addressed record (roundtrip, digest);
  * committing the same artifact twice is the same artifact (idempotency);
  * lineage is walkable and honest about what it cannot find;
  * the object plane's integrity guarantee still applies to manifests;
  * every commit leaves an auditable ledger event on the unchanged hash chain;
  * defaults deny: nothing reaches the frontier model unless it is said to.
"""
import hashlib
import json
import os

import pytest

from hugpy_agent.mct.errors import IntegrityError, NotFoundError
from hugpy_agent.mct.ledger import Ledger
from hugpy_agent.mct.manifest import (
    DISCLOSURE_LOCAL_ONLY,
    RIGHTS_UNKNOWN,
    SCHEMA_VERSION,
    ArtifactManifest,
    ManifestStore,
)
from hugpy_agent.mct.objects import ObjectStore, relative_object_path


@pytest.fixture
def plane(tmp_path):
    """(ManifestStore, ObjectStore, Ledger, session_id) on a real store."""
    ledger = Ledger(tmp_path / "mct.db")
    store = ObjectStore(tmp_path, ledger)
    session_id, _epoch = ledger.create_session("test")
    return ManifestStore(store, ledger), store, ledger, session_id


def _events(ledger, session_id, type_):
    rows = ledger._db.execute(
        "SELECT * FROM events WHERE session_id=? AND type=? ORDER BY rowid_global ASC",
        (session_id, type_)).fetchall()
    return [dict(r) for r in rows]


# --- the contract itself ---------------------------------------------------

def test_roundtrip_is_lossless_and_deterministic():
    m = ArtifactManifest.for_bytes(
        b"take-1.wav bytes",
        artifact_type="audio.line",
        media_type="audio/wav",
        duration_s=2.5,
        sample_rate_hz=48000,
        producer="runners.tts_chatterbox",
        run_id="run_7",
        recipe_id="two-characters-three-lines",
        graph_revision="rev_2",
        source_uri="file:///script.txt",
        acquired_at="2026-08-20T10:00:00.000Z",
        model="chatterbox",
        adapter="tts_chatterbox@1",
        prompt="Nice to meet you.",
        parameters={"temperature": 0.7, "exaggeration": 0.5},
        seed=1234,
        rights="consented",
        disclosure="excerpt_only",
        frontier_may_access=True,
        evaluations=("scorecard_9",),
        created_at="2026-08-20T10:00:01.000Z",
    )
    again = ArtifactManifest.from_dict(m.to_dict())
    assert again == m
    assert again.digest() == m.digest()
    assert again.canonical_bytes() == m.canonical_bytes()
    # parameters survive as typed values, not as their JSON text
    assert again.parameters_dict() == {"temperature": 0.7, "exaggeration": 0.5}
    # deterministic JSON: sorted keys, no whitespace
    assert m.canonical_bytes() == json.dumps(
        m.to_dict(), sort_keys=True, separators=(",", ":")).encode()


def test_content_hash_size_and_storage_pointer_are_derived_not_asserted():
    data = b"pixels"
    m = ArtifactManifest.for_bytes(data, artifact_type="image.keyframe",
                                   media_type="image/png")
    assert m.content_sha256 == hashlib.sha256(data).hexdigest()
    assert m.size_bytes == len(data)
    assert m.storage_pointer == relative_object_path(m.content_sha256)
    assert m.schema_version == SCHEMA_VERSION


def test_unknown_media_metadata_stays_none_never_guessed():
    m = ArtifactManifest.for_bytes(b"\x89PNG...", artifact_type="image.keyframe",
                                   media_type="image/png")
    assert (m.width, m.height, m.duration_s, m.frame_rate, m.sample_rate_hz) == (
        None, None, None, None, None)
    assert (m.model, m.prompt, m.seed, m.run_id, m.source_uri) == (
        None, None, None, None, None)
    assert m.parents == () and m.evaluations == ()


def test_defaults_deny_disclosure_to_the_frontier_model():
    m = ArtifactManifest.for_bytes(b"secret", artifact_type="text.note",
                                   media_type="text/plain")
    assert m.frontier_may_access is False          # B is the authority broker
    assert m.disclosure == DISCLOSURE_LOCAL_ONLY
    assert m.rights == RIGHTS_UNKNOWN              # never assumed to be ours
    with pytest.raises(ValueError, match="contradicts disclosure"):
        ArtifactManifest.for_bytes(b"secret", artifact_type="text.note",
                                   media_type="text/plain", frontier_may_access=True)


def test_unknown_fields_are_rejected_not_silently_dropped():
    m = ArtifactManifest.for_bytes(b"x", artifact_type="text.note",
                                   media_type="text/plain")
    payload = m.to_dict()
    payload["tenant"] = "acme"  # a field this build does not understand
    with pytest.raises(ValueError, match="unknown ArtifactManifest fields: tenant"):
        ArtifactManifest.from_dict(payload)
    # ... nor a manifest written by a future schema
    future = m.to_dict()
    future["schema_version"] = "mct.artifact_manifest/2"
    with pytest.raises(ValueError, match="unsupported manifest schema_version"):
        ArtifactManifest.from_dict(future)


def test_structurally_invalid_values_raise():
    good = dict(artifact_type="text.note", media_type="text/plain")
    with pytest.raises(ValueError, match="not a sha256 hex digest"):
        ArtifactManifest(content_sha256="nope", size_bytes=1, **good)
    with pytest.raises(ValueError, match="unknown rights class"):
        ArtifactManifest.for_bytes(b"x", rights="mine-i-swear", **good)
    with pytest.raises(ValueError, match="not a manifest digest"):
        ArtifactManifest.for_bytes(b"x", parents=("o_HANDLE",), **good)
    with pytest.raises(ValueError, match="non-negative"):
        ArtifactManifest.for_bytes(b"x", duration_s=-1.0, **good)


# --- commit / idempotency --------------------------------------------------

def test_commit_stores_the_manifest_as_a_content_addressed_object(plane):
    manifests, store, ledger, sid = plane
    ref = manifests.commit_artifact(sid, b"hello artifact",
                                    artifact_type="text.note", media_type="text/plain")
    assert ref.created is True
    assert ref.digest == ref.manifest.digest()
    # the digest IS the object's digest — resolving the pointer returns the manifest
    raw = store.resolve(sid, ref.pointer)
    assert hashlib.sha256(raw).hexdigest() == ref.digest
    assert ArtifactManifest.from_dict(json.loads(raw)) == ref.manifest
    assert ref.manifest.created_at is not None  # stamped at commit
    # the artifact bytes are in the plane too, at the pointer the manifest names
    assert store.resolve(sid, ref.content_pointer) == b"hello artifact"
    assert (store.root / ref.manifest.storage_pointer).exists()
    assert manifests.get(sid, ref.digest) == ref.manifest


def test_recommit_of_identical_bytes_and_fields_is_idempotent(plane):
    manifests, store, ledger, sid = plane
    kwargs = dict(artifact_type="audio.line", media_type="audio/wav",
                  producer="runners.tts_chatterbox", seed=7)
    first = manifests.commit_artifact(sid, b"waveform", **kwargs)
    second = manifests.commit_artifact(sid, b"waveform", **kwargs)

    assert second.digest == first.digest                  # same stable artifact id
    assert second.created is False
    assert second.manifest == first.manifest              # including created_at
    assert second.object_id == first.object_id            # same handle, no new object
    assert second.content_object_id == first.content_object_id
    # exactly one manifest object and one ledger event, not two
    assert len(ledger.list_objects(sid, kinds=[ManifestStore.OBJECT_KIND])) == 1
    assert len(_events(ledger, sid, ManifestStore.EVENT_TYPE)) == 1
    # a genuinely different provenance IS a different artifact
    other = manifests.commit_artifact(sid, b"waveform", **{**kwargs, "seed": 8})
    assert other.digest != first.digest and other.created is True


def test_commit_emits_one_ledger_event_and_the_chain_still_verifies(plane):
    manifests, store, ledger, sid = plane
    parent = manifests.commit_artifact(sid, b"script", artifact_type="text.screenplay",
                                       media_type="text/plain")
    child = manifests.commit_artifact(sid, b"line audio", artifact_type="audio.line",
                                      media_type="audio/wav", parents=(parent.digest,),
                                      producer="runners.tts_chatterbox")
    events = _events(ledger, sid, ManifestStore.EVENT_TYPE)
    assert len(events) == 2
    assert events[1]["actor"] == ManifestStore.ACTOR
    assert json.loads(events[1]["output_objects"]) == [child.object_id]
    assert json.loads(events[1]["input_objects"]) == [parent.object_id]
    assert events[1]["idempotency_key"].startswith(ManifestStore.IDEMPOTENCY_PREFIX)
    assert ledger.verify_chain(sid) is True  # chain format untouched


def test_commit_without_the_bytes_records_the_artifact_honestly(plane):
    """A manifest may describe an artifact held elsewhere (e.g. on a worker):
    the content handle is None, not invented."""
    manifests, store, ledger, sid = plane
    m = ArtifactManifest.for_bytes(b"a 4GB render we do not hold",
                                   artifact_type="video.segment", media_type="video/mp4")
    ref = manifests.commit(sid, m)
    assert ref.content_object_id is None and ref.content_pointer is None
    assert manifests.get(sid, ref.digest).content_sha256 == m.content_sha256


# --- lineage ---------------------------------------------------------------

def _chain(manifests, sid, generations=3):
    """great-grandparent -> ... -> child, returns oldest-first refs."""
    refs = []
    parents = ()
    for i in range(generations + 1):
        ref = manifests.commit_artifact(
            sid, f"generation-{i}".encode(), artifact_type=f"gen.{i}",
            media_type="text/plain", parents=parents, producer=f"node_{i}")
        refs.append(ref)
        parents = (ref.digest,)
    return refs


def test_lineage_walks_three_generations_of_ancestors(plane):
    manifests, store, ledger, sid = plane
    g0, g1, g2, g3 = _chain(manifests, sid, generations=3)

    walk = manifests.lineage(sid, g3.digest, depth=3)
    assert [n.digest for n in walk] == [g2.digest, g1.digest, g0.digest]
    assert [n.depth for n in walk] == [1, 2, 3]
    assert [n.manifest.producer for n in walk] == ["node_2", "node_1", "node_0"]

    assert [n.digest for n in manifests.lineage(sid, g3.digest, depth=1)] == [g2.digest]
    assert manifests.lineage(sid, g0.digest, depth=3) == []   # the root has no parents
    assert manifests.lineage(sid, g3.digest, depth=0) == []


def test_lineage_reports_an_absent_ancestor_rather_than_inventing_one(plane):
    manifests, store, ledger, sid = plane
    absent = hashlib.sha256(b"a manifest that was never committed here").hexdigest()
    ref = manifests.commit_artifact(sid, b"orphan", artifact_type="text.note",
                                    media_type="text/plain", parents=(absent,))
    walk = manifests.lineage(sid, ref.digest, depth=2)
    assert [(n.digest, n.manifest) for n in walk] == [(absent, None)]
    with pytest.raises(NotFoundError):
        manifests.lineage(sid, absent)


def test_children_finds_the_next_generation(plane):
    manifests, store, ledger, sid = plane
    g0, g1, g2, _g3 = _chain(manifests, sid, generations=3)
    sibling = manifests.commit_artifact(sid, b"sibling", artifact_type="gen.1b",
                                        media_type="text/plain", parents=(g0.digest,))
    kids = manifests.children(sid, g0.digest)
    assert sorted(n.digest for n in kids) == sorted([g1.digest, sibling.digest])
    assert all(n.depth == 1 for n in kids)
    assert [n.digest for n in manifests.children(sid, g1.digest)] == [g2.digest]
    assert manifests.children(sid, sibling.digest) == []


# --- integrity -------------------------------------------------------------

def test_tampered_manifest_is_quarantined_and_refused(plane):
    """Manifests inherit the object plane's invariant 4 unchanged."""
    manifests, store, ledger, sid = plane
    ref = manifests.commit_artifact(sid, b"trustworthy", artifact_type="text.note",
                                    media_type="text/plain")
    path = store._path_for_digest(ref.digest)
    os.chmod(path, 0o640)
    forged = json.loads(path.read_bytes())
    forged["frontier_may_access"] = True          # the interesting lie to try
    path.write_bytes(json.dumps(forged).encode())

    with pytest.raises(IntegrityError):
        manifests.get(sid, ref.digest)
    assert not path.exists()                       # moved to quarantine
    assert (store.quarantine_dir / ref.digest).exists()


def test_tampered_artifact_bytes_are_still_quarantined(plane):
    manifests, store, ledger, sid = plane
    ref = manifests.commit_artifact(sid, b"the real take", artifact_type="audio.line",
                                    media_type="audio/wav")
    path = store._path_for_digest(ref.manifest.content_sha256)
    os.chmod(path, 0o640)
    path.write_bytes(b"a different take")
    with pytest.raises(IntegrityError):
        store.resolve(sid, ref.content_pointer)
    assert not path.exists()
    # the manifest still states what the artifact WAS — provenance outlives bytes
    assert manifests.get(sid, ref.digest).content_sha256 == ref.manifest.content_sha256


def test_get_of_an_unknown_manifest_is_not_found(plane):
    manifests, store, ledger, sid = plane
    missing = hashlib.sha256(b"nothing").hexdigest()
    with pytest.raises(NotFoundError):
        manifests.get(sid, missing)
    assert manifests.try_get(sid, missing) is None


def test_manifests_are_session_scoped(plane):
    manifests, store, ledger, sid = plane
    other_sid, _ = ledger.create_session("other")
    ref = manifests.commit_artifact(sid, b"mine", artifact_type="text.note",
                                    media_type="text/plain")
    assert manifests.try_get(other_sid, ref.digest) is None
    # ... and the same artifact committed in another session gets its own record
    theirs = manifests.commit_artifact(other_sid, b"mine", artifact_type="text.note",
                                       media_type="text/plain")
    assert theirs.created is True and theirs.object_id != ref.object_id
