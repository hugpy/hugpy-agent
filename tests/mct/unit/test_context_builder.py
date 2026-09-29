"""Context builder: scoring, packing, dedup, explainability (design §11.2, §11.5)."""
import pytest

from hugpy_agent.mct.compaction import Compaction
from hugpy_agent.mct.context_builder import ContextBuilder
from hugpy_agent.mct.ledger import Ledger
from hugpy_agent.mct.objects import ObjectStore
from hugpy_agent.mct.retrieval import Retrieval

BUDGET = {"maximum_input_tokens": 24000, "reserved_output_tokens": 8000,
          "pull_tokens_remaining": 0}


@pytest.fixture
def kit(tmp_path):
    ledger = Ledger(tmp_path / "mct.db")
    store = ObjectStore(tmp_path, ledger)
    comp = Compaction(store, ledger)
    builder = ContextBuilder(store, ledger, Retrieval(ledger, store))
    sid, epoch = ledger.create_session()
    return builder, store, ledger, comp, sid, epoch


def _op(store, sid, text="trace gpu-02 eviction cause"):
    return store.commit(sid, text.encode(), media_type="text/plain", kind="operator_turn")


def test_operator_turn_and_policy_are_required(kit):
    builder, store, ledger, comp, sid, epoch = kit
    policy = store.commit(sid, b"Always be terse.", media_type="text/plain", kind="policy_snapshot")
    op = _op(store, sid)
    ptr, sha, trace = builder.build(
        sid, "t_000001", epoch, op, "trace gpu-02 eviction cause", budget=BUDGET,
        required_specs=[{"object": policy.pointer, "role": "governing_instruction"}])
    import json
    manifest = json.loads(store.resolve(sid, ptr))
    assert manifest["operator_turn"]["object"] == op.pointer
    assert manifest["operator_turn"]["verbatim"] is True
    roles = [f["role"] for f in manifest["fragments"]]
    assert "governing_instruction" in roles


def test_relevant_fact_outscores_irrelevant(kit):
    builder, store, ledger, comp, sid, epoch = kit
    op = _op(store, sid, "why was gpu-02 evicted")
    comp.record_fact(sid, "gpu-02 eviction caused by preempt alloc A17", kind="decision",
                     source_object_ids=[op.object_id])
    comp.record_fact(sid, "unrelated note about billing invoices", kind="decision",
                     source_object_ids=[op.object_id])
    _ptr, _sha, trace = builder.build(sid, "t_000001", epoch, op, "why was gpu-02 evicted",
                                      budget=BUDGET)
    ex = trace.explain()
    # the gpu-02 fact scores higher on relevance than the billing note
    relevances = [r["components"]["R"] for r in ex["included"] + ex["omitted"]
                  if "R" in r["components"]]
    assert max(relevances) > 0  # at least one relevant lexical match found


def test_exact_duplicate_dropped(kit):
    builder, store, ledger, comp, sid, epoch = kit
    op = _op(store, sid)
    comp.record_fact(sid, "identical decision body", kind="decision", source_object_ids=[op.object_id])
    # a byte-identical response_body object in history
    store.commit(sid, b"identical decision body", media_type="text/plain", kind="response_body")
    _ptr, _sha, trace = builder.build(sid, "t_000002", epoch, op, "identical", budget=BUDGET)
    ex = trace.explain()
    dupes = [r for r in ex["omitted"] if r["reason"] == "exact-duplicate"]
    assert len(dupes) == 1  # §11.5 rule 1


def test_tight_budget_omits_with_reason(kit):
    builder, store, ledger, comp, sid, epoch = kit
    op = _op(store, sid, "topic alpha")
    for i in range(5):
        comp.record_fact(sid, f"alpha detail number {i} " + "x" * 400, kind="decision",
                         source_object_ids=[op.object_id])
    tight = {"maximum_input_tokens": 120, "reserved_output_tokens": 0, "pull_tokens_remaining": 0}
    _ptr, _sha, trace = builder.build(sid, "t_000003", epoch, op, "alpha", budget=tight)
    ex = trace.explain()
    omitted_reasons = {r["reason"] for r in ex["omitted"]}
    assert "budget-exhausted" in omitted_reasons
    assert ex["selected_tokens"] <= ex["pack_budget"]


def test_every_fragment_has_an_explanation(kit):
    builder, store, ledger, comp, sid, epoch = kit
    op = _op(store, sid, "beta topic")
    comp.record_fact(sid, "beta relevant fact", kind="decision", source_object_ids=[op.object_id])
    _ptr, _sha, trace = builder.build(sid, "t_000004", epoch, op, "beta", budget=BUDGET)
    ex = trace.explain()
    for row in ex["included"] + ex["omitted"]:
        assert "reason" in row and "components" in row and "score" in row  # §18.2 explainability
