"""Phase 3: local model improves ranking but cannot authorize / delete / hide.

Design §22 Phase-3 exit condition, invariant 9.
"""
import pytest

from hugpy_agent.mct import fake_a
from hugpy_agent.mct.local_model import DeterministicLocalModel
from hugpy_agent.mct.session import BrokerConfig, BrokerServer


@pytest.fixture
def msession(tmp_path):
    printed = []
    server = BrokerServer(tmp_path, sink=printed.append,
                          config=BrokerConfig(use_model=True))
    server.printed = printed
    sid = server.open_session("t")
    return server.session(sid)


def test_model_is_the_deterministic_offline_one(msession):
    assert isinstance(msession.server.model, DeterministicLocalModel)


def test_semantic_relevance_boosts_a_synonym_match(msession):
    # store a decision phrased with "eviction"; query with the synonym "removed"
    msession.submit("DECISION: gpu-02 eviction happened due to preempt",
                    fake_a.answer("noted"))
    r = msession.submit("why was gpu-02 removed?", fake_a.answer("preempt"))
    rows = [row for row in r.context_trace["included"] if row["role"] == "decision_memory"]
    assert rows, "the decision should be retrieved"
    # semantic component contributed (R_sem > 0) even though exact overlap is low
    assert any(row["components"].get("R_sem", 0) > 0 for row in rows)


def test_model_extraction_catches_unprefixed_decision(msession):
    msession.submit("The incident was caused by a preempt on alloc A17.",
                    fake_a.answer("ack"))
    decisions = msession.server.compaction.facts(msession.session_id, kind="decision")
    assert any("preempt" in f.text for f in decisions)  # regex alone would miss this


def test_model_draft_is_traceable(msession):
    r = msession.submit("DECISION: cache is advisory only", fake_a.answer("ok"))
    facts = msession.server.compaction.facts(msession.session_id, kind="decision")
    src_id = facts[0].source_objects[0]
    summ = msession.server.compaction.summarize(msession.session_id, [facts[0].object_id])
    assert summ is not None
    graph = msession.server.compaction.provenance_graph(msession.session_id, summ.object_id)
    assert graph["is_original"] is False           # a model draft is never an original
    assert graph["sources"]                         # ...and never untraceable (§11.4)
    # the drafting model is recorded in provenance
    import json
    meta = msession.server.ledger.get_object(summ.object_id)
    assert json.loads(meta["provenance"])["model"] == "deterministic-local/1"


def test_near_duplicate_clustering_never_deletes(msession):
    comp = msession.server.compaction
    sid = msession.session_id
    src = msession.server.store.commit(sid, b"x", media_type="text/plain", kind="operator_turn")
    a = comp.record_fact(sid, "gpu-02 evicted by preempt on alloc A17", kind="decision",
                         source_object_ids=[src.object_id])
    b = comp.record_fact(sid, "gpu-02 was evicted due to preempt, alloc A17", kind="decision",
                         source_object_ids=[src.object_id])
    pairs = comp.cluster_near_duplicates(sid, kind="decision", threshold=0.8)
    assert pairs                                     # detected as near-duplicates
    # both originals still resolve — clustering only annotates (§11.5 rule 3)
    assert msession.server.store.resolve(sid, a.pointer)
    assert msession.server.store.resolve(sid, b.pointer)
    assert msession.server.ledger.near_duplicates(a.object_id)


def test_conflict_detection_retains_both(msession):
    comp = msession.server.compaction
    sid = msession.session_id
    src = msession.server.store.commit(sid, b"x", media_type="text/plain", kind="operator_turn")
    comp.record_fact(sid, "max-gpu keeps the worker resident", kind="decision",
                     source_object_ids=[src.object_id])
    comp.record_fact(sid, "max-gpu bypassed, worker evicted under preempt", kind="decision",
                     source_object_ids=[src.object_id])
    pairs = comp.detect_conflicts(sid, kind="decision")
    assert pairs
    assert len(msession.server.ledger.facts(sid, kind="decision")) == 2  # neither deleted


def test_model_cannot_authorize_a_cross_session_pull(msession):
    # Even though the model ranks/knows about content, authorization is deterministic
    # and never consults it (invariant 9). A cross-session pull is still denied.
    from hugpy_agent.mct.protocol import make_pointer

    def a(client):
        client.read_operator_turn()
        out = client.submit_pull(need="steal", target={"kind": "object",
                                 "object": make_pointer("s_OTHER", "o_X")})
        assert out.decision == "denied"
        client.respond("denied as expected")

    r = msession.submit("try cross-session", a)
    assert r.state == "Committed"


def test_model_failure_degrades_to_lexical(tmp_path):
    # A model that raises on embed must not break context building (design §17).
    class BrokenModel:
        name = "broken/1"
        def embed(self, texts): raise RuntimeError("model down")
        def summarize(self, text, *, max_sentences=2): raise RuntimeError
        def extract(self, text): return []
        def are_conflicting(self, a, b): return False

    server = BrokerServer(tmp_path, sink=lambda *_: None, local_model=BrokenModel())
    sess = server.session(server.open_session("t"))
    sess.submit("DECISION: alpha chosen", fake_a.answer("ok"))
    r = sess.submit("tell me about alpha", fake_a.answer("alpha"))   # must not raise
    assert r.state == "Committed"
    server.close()
