"""Compaction: extraction, provenance graph, dedup, conflict (design §11.4-11.5)."""
import pytest

from hugpy_agent.mct.compaction import Compaction
from hugpy_agent.mct.ledger import Ledger
from hugpy_agent.mct.objects import ObjectStore
from hugpy_agent.mct.protocol import parse_pointer


@pytest.fixture
def kit(tmp_path):
    ledger = Ledger(tmp_path / "mct.db")
    store = ObjectStore(tmp_path, ledger)
    sid, _ = ledger.create_session()
    return Compaction(store, ledger), store, ledger, sid


def test_extract_decisions_and_preferences(kit):
    comp, store, ledger, sid = kit
    src = store.commit(sid, b"chatter\nDECISION: use SQLite WAL\nPREFER: terse output\nmore\n",
                       media_type="text/plain", kind="operator_turn")
    facts = comp.extract(sid, src.object_id)
    kinds = {ledger.facts(sid)[i]["kind"] for i in range(len(ledger.facts(sid)))}
    assert kinds == {"decision", "preference"}
    assert len(facts) == 2


def test_provenance_graph_reconstructs_to_original(kit):
    comp, store, ledger, sid = kit
    original = store.commit(sid, b"DECISION: cache is advisory only\n",
                            media_type="text/plain", kind="source_snapshot")
    fact = comp.extract(sid, original.object_id)[0]
    graph = comp.provenance_graph(sid, fact.object_id)
    # derived fact -> its source original
    assert graph["is_original"] is False
    assert len(graph["sources"]) == 1
    assert graph["sources"][0]["object_id"] == original.object_id
    assert graph["sources"][0]["is_original"] is True


def test_exact_duplicate_fact_is_deduped(kit):
    comp, store, ledger, sid = kit
    src = store.commit(sid, b"x", media_type="text/plain", kind="operator_turn")
    a = comp.record_fact(sid, "same text", kind="decision", source_object_ids=[src.object_id])
    b = comp.record_fact(sid, "same text", kind="decision", source_object_ids=[src.object_id])
    assert a.object_id == b.object_id                 # deduped (§11.5 rule 1)
    assert len(ledger.facts(sid)) == 1


def test_conflicting_facts_both_retained_and_marked(kit):
    comp, store, ledger, sid = kit
    src = store.commit(sid, b"x", media_type="text/plain", kind="operator_turn")
    a = comp.record_fact(sid, "max-gpu keeps worker resident", kind="decision",
                         source_object_ids=[src.object_id])
    b = comp.record_fact(sid, "max-gpu bypassed under preempt", kind="decision",
                         source_object_ids=[src.object_id])
    comp.mark_conflict(a.object_id, b.object_id)
    import json
    rows = {f["object_id"]: json.loads(f["conflict_with"]) for f in ledger.facts(sid)}
    assert b.object_id in rows[a.object_id] and a.object_id in rows[b.object_id]
    assert len(ledger.facts(sid)) == 2                # neither deleted (§11.5 rule 3)


def test_supersede_keeps_original(kit):
    comp, store, ledger, sid = kit
    src = store.commit(sid, b"x", media_type="text/plain", kind="operator_turn")
    v1 = comp.record_fact(sid, "rev1", kind="decision", source_object_ids=[src.object_id])
    v2 = comp.record_fact(sid, "rev2", kind="decision", source_object_ids=[src.object_id],
                          supersedes=v1.object_id)
    live = ledger.facts(sid)
    allf = ledger.facts(sid, include_superseded=True)
    assert [f["object_id"] for f in live] == [v2.object_id]     # only current is live
    assert len(allf) == 2                                        # original retained
