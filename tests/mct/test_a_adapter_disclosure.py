"""k113 — the B->A disclosure gate on the restricted A adapter
(IDEA_PHASE/POLICY-rights-consent-disclosure.md §3.3-3.5).

Locks:
  [1] an artifact whose manifest says frontier_may_access=False never reaches A:
      a read raises the typed DisclosureRefused and the broker is never asked;
  [2] a pull listing entry for such an artifact is replaced by a redacted stub
      (no pointer, no digest) and the payload names the refusal;
  [3] excerpt_only admits only a selector-bounded read, capped at EXCERPT_MAX_BYTES;
      full admits the object; objects with no manifest pass through;
  [4] every refusal is a typed record, kept B-side and handed to the broker's
      record_disclosure_refusal sink;
  [5] ManifestStore.for_content finds a manifest from content bytes, and the
      disclosure_verdict rule is the single source of truth.
"""
import hashlib

import pytest

from hugpy_agent.mct.a_adapter import (
    REFUSAL_SCHEMA,
    AAdapterClient,
    DisclosureRefusal,
    DisclosureRefused,
)
from hugpy_agent.mct.ledger import Ledger
from hugpy_agent.mct.manifest import (
    EXCERPT_MAX_BYTES,
    ArtifactManifest,
    ManifestStore,
    disclosure_verdict,
)
from hugpy_agent.mct.objects import ObjectStore


def _manifest(data: bytes, **fields) -> ArtifactManifest:
    fields.setdefault("artifact_type", "audio.master")
    fields.setdefault("media_type", "audio/wav")
    return ArtifactManifest.for_bytes(data, **fields)


class _FakeBroker:
    """In-memory stand-in for session._ABinding: pointers -> bytes, plus the
    optional manifest lookup and refusal sink the gate consults."""
    session_id = "s1"
    turn_id = "t1"
    epoch = "e1"
    manifest_pointer = "mct://s1/turn-manifest"

    def __init__(self, objects: dict[str, bytes], manifests: dict[str, ArtifactManifest],
                 pull_objects: list[dict] | None = None):
        self.objects = objects
        self.manifests = manifests
        self.pull_objects = pull_objects or []
        self.resolved: list[str] = []
        self.refusals: list[dict] = []
        self.created: list[tuple[str, str]] = []

    def artifact_manifest_for(self, pointer):
        return self.manifests.get(pointer)

    def record_disclosure_refusal(self, record: dict):
        self.refusals.append(record)

    def resolve(self, pointer, selector, purpose):
        self.resolved.append(pointer)
        return self.objects[pointer]

    def create_object(self, data, media_type, kind, provenance):
        ptr = f"mct://s1/obj-{len(self.created)}"
        self.created.append((kind, ptr))
        self.objects[ptr] = data
        return ptr

    def next_request_id(self):
        return "r1"

    def handle_pull(self, request_pointer):
        return {"decision": "exact", "objects": list(self.pull_objects)}, "mct://s1/result"

    def handle_response(self, manifest_pointer, idempotency_key):
        return {"rendered": True, "already_rendered": False, "body_sha256": ""}


@pytest.fixture
def world():
    secret = b"local-only bytes " * 10
    shared = b"shared bytes " * 10
    big = b"x" * (EXCERPT_MAX_BYTES * 3)
    objects = {"mct://s1/secret": secret, "mct://s1/shared": shared,
               "mct://s1/excerpt": big, "mct://s1/plain": b"no manifest here"}
    manifests = {
        "mct://s1/secret": _manifest(secret),                      # default deny
        "mct://s1/shared": _manifest(shared, disclosure="full", frontier_may_access=True),
        "mct://s1/excerpt": _manifest(big, disclosure="excerpt_only",
                                      frontier_may_access=True, artifact_type="doc.text"),
    }
    pulls = [{"object": "mct://s1/secret", "sha256": hashlib.sha256(secret).hexdigest()},
             {"object": "mct://s1/shared", "sha256": hashlib.sha256(shared).hexdigest()},
             {"object": "mct://s1/plain"}]
    broker = _FakeBroker(objects, manifests, pulls)
    return broker, AAdapterClient(broker)


# [1] ------------------------------------------------------------------------

def test_frontier_may_access_false_never_reaches_a(world):
    broker, client = world
    with pytest.raises(DisclosureRefused) as ei:
        client.resolve("mct://s1/secret")
    assert broker.resolved == []                     # B was never asked for the bytes
    record = ei.value.record
    assert isinstance(record, DisclosureRefusal)
    assert record.verdict.allowed is False
    assert record.verdict.frontier_may_access is False
    assert "frontier_may_access=False" in record.verdict.reason


def test_local_only_disclosure_is_refused_even_if_flag_were_true():
    # The manifest contract forbids the contradictory combination outright (§3.3).
    with pytest.raises(ValueError):
        _manifest(b"x", disclosure="local_only", frontier_may_access=True)


# [2] ------------------------------------------------------------------------

def test_pull_listing_redacts_refused_artifacts_and_names_the_refusal(world):
    broker, client = world
    out = client.submit_pull("need", {"kind": "audio"})
    assert out.satisfied
    assert len(out.objects) == 3
    stub, shared, plain = out.objects
    assert stub == {"redacted": True, "outcome": "refused",
                    "reason": "frontier_may_access=False: B does not disclose "
                              "this artifact to A",
                    "artifact_type": "audio.master"}
    assert "object" not in stub and "sha256" not in stub  # no pointer, no digest
    assert shared["object"] == "mct://s1/shared"
    assert plain == {"object": "mct://s1/plain"}         # no manifest: passes through
    assert [r.pointer for r in out.refused] == ["mct://s1/secret"]
    assert out.payload["disclosure_refusals"][0]["schema"] == REFUSAL_SCHEMA
    assert out.payload["objects"] == out.objects


def test_pull_with_nothing_to_redact_leaves_payload_untouched(world):
    broker, client = world
    broker.pull_objects = [{"object": "mct://s1/shared"}]
    out = client.submit_pull("need", {"kind": "audio"})
    assert out.refused == [] and "disclosure_refusals" not in out.payload


# [3] ------------------------------------------------------------------------

def test_full_disclosure_and_unmanifested_objects_are_readable(world):
    broker, client = world
    assert client.resolve("mct://s1/shared") == broker.objects["mct://s1/shared"]
    assert client.resolve("mct://s1/plain") == b"no manifest here"
    assert client._refusals == []


def test_excerpt_only_needs_a_selector_and_is_capped(world):
    broker, client = world
    with pytest.raises(DisclosureRefused) as ei:
        client.resolve("mct://s1/excerpt")                # whole-object read
    assert "excerpt_only" in ei.value.record.verdict.reason
    data = client.resolve("mct://s1/excerpt", selector="lines:1-10")
    assert len(data) == EXCERPT_MAX_BYTES


# [4] ------------------------------------------------------------------------

def test_refusals_are_typed_records_handed_to_the_broker(world):
    broker, client = world
    with pytest.raises(DisclosureRefused):
        client.resolve("mct://s1/secret", purpose="read")
    client.submit_pull("need", {"kind": "audio"})
    assert len(broker.refusals) == 2 == len(client._refusals)
    rec = broker.refusals[0]
    assert rec["schema"] == REFUSAL_SCHEMA and rec["outcome"] == "refused"
    assert (rec["session_id"], rec["turn_id"], rec["epoch"]) == ("s1", "t1", "e1")
    assert rec["pointer"] == "mct://s1/secret" and rec["purpose"] == "read"
    assert rec["verdict"]["allowed"] is False and rec["at"]
    assert broker.refusals[1]["purpose"] == "pull"
    # the record names the verdict, never the content
    assert "local-only bytes" not in str(rec)


def test_client_without_a_manifest_plane_is_a_plain_passthrough(world):
    broker, _ = world
    del _FakeBroker.artifact_manifest_for
    try:
        client = AAdapterClient(broker)
        assert client.resolve("mct://s1/secret") == broker.objects["mct://s1/secret"]
    finally:
        _FakeBroker.artifact_manifest_for = lambda self, p: self.manifests.get(p)


def test_explicit_lookup_wins_over_the_broker(world):
    broker, _ = world
    deny_all = lambda pointer: _manifest(b"y")  # noqa: E731
    client = AAdapterClient(broker, manifest_lookup=deny_all)
    with pytest.raises(DisclosureRefused):
        client.resolve("mct://s1/shared")


def test_a_surface_gains_no_public_attribute():
    broker = _FakeBroker({}, {})
    public = {a for a in dir(AAdapterClient(broker)) if not a.startswith("_")}
    assert public <= {"resolve", "open_manifest", "read_operator_turn",
                      "submit_pull", "respond", "respond_stream",
                      "session_id", "turn_id", "epoch", "manifest_pointer"}


# [5] ------------------------------------------------------------------------

def test_disclosure_verdict_is_the_single_rule():
    m = _manifest(b"a")
    assert disclosure_verdict(m).allowed is False
    m = _manifest(b"a", disclosure="excerpt_only", frontier_may_access=True)
    assert disclosure_verdict(m).allowed is False
    v = disclosure_verdict(m, selector="p:1")
    assert v.allowed and v.limit == EXCERPT_MAX_BYTES
    m = _manifest(b"a", disclosure="full", frontier_may_access=True)
    v = disclosure_verdict(m)
    assert v.allowed and v.limit is None and v.to_dict()["disclosure"] == "full"


def test_manifest_store_for_content_finds_the_governing_manifest(tmp_path):
    ledger = Ledger(tmp_path / "mct.db")
    store = ObjectStore(tmp_path, ledger)
    session_id, _ = ledger.create_session("test")
    manifests = ManifestStore(store, ledger)
    data = b"artifact bytes"
    assert manifests.for_content(session_id, hashlib.sha256(data).hexdigest()) is None
    ref = manifests.commit_artifact(session_id, data, artifact_type="doc.text",
                                    media_type="text/plain")
    found = manifests.for_content(session_id, ref.manifest.content_sha256)
    assert found is not None and found.frontier_may_access is False
    assert disclosure_verdict(found).allowed is False
    # wired as the gate's lookup through the content pointer's digest
    def lookup(pointer):
        _, oid = pointer.rsplit("/", 1)
        meta = ledger.get_object(oid)
        return manifests.for_content(session_id, meta["digest"])
    broker = _FakeBroker({ref.content_pointer: data}, {})
    client = AAdapterClient(broker, manifest_lookup=lookup)
    with pytest.raises(DisclosureRefused):
        client.resolve(ref.content_pointer)
