"""Phase 2 end-to-end: deterministic context across turns (design §11, §22)."""
from hugpy_agent.mct import fake_a


def test_policy_is_always_present_as_governing_instruction(session):
    session.set_policy("Cite evidence. Be terse.")
    r = session.submit("hello", fake_a.answer("hi"))
    roles = [row["role"] for row in r.context_trace["included"]]
    assert "governing_instruction" in roles


def test_memory_accumulates_and_is_retrieved_next_turn(session):
    # Turn 1: operator states a decision; B extracts it deterministically.
    session.submit("DECISION: gpu-02 eviction is caused by preempt on alloc A17",
                   fake_a.answer("noted"))
    facts = session.server.compaction.facts(session.session_id, kind="decision")
    assert any("gpu-02" in f.text for f in facts)

    # Turn 2: a related query surfaces that decision as a candidate fragment.
    r2 = session.submit("why did gpu-02 get evicted?", fake_a.answer("because preempt"))
    included_roles = [row["role"] for row in r2.context_trace["included"]]
    assert "decision_memory" in included_roles


def test_context_trace_explains_inclusion_and_omission(session):
    session.submit("DECISION: alpha is chosen", fake_a.answer("ok"))
    session.submit("PREFER: beta formatting", fake_a.answer("ok"))
    r = session.submit("tell me about alpha", fake_a.answer("alpha info"))
    trace = r.context_trace
    assert "included" in trace and "omitted" in trace
    for row in trace["included"] + trace["omitted"]:
        assert set(row) >= {"object", "role", "score", "tokens", "reason", "components"}


def test_selected_fragment_reconstructs_to_source_without_model(session):
    session.submit("DECISION: cache is advisory only per section twelve",
                   fake_a.answer("ok"))
    r = session.submit("what did we decide about cache?", fake_a.answer("advisory"))
    # find an included decision_memory fragment and walk it back to its origin
    decision_rows = [row for row in r.context_trace["included"] if row["role"] == "decision_memory"]
    assert decision_rows
    graph = session.server.compaction.provenance_graph(session.session_id,
                                                        decision_rows[0]["object"])
    # the fact traces back through sources to an original object — reversible (invariant 8)
    assert graph["is_original"] is False
    assert graph["sources"] and graph["sources"][0]["object_id"]


def test_operator_prompt_stays_verbatim_through_engine(session):
    session.set_policy("policy text")
    seen = {}

    def a(client):
        seen["op"] = client.read_operator_turn()
        client.respond("ok")

    session.submit("EXACT WORDS stay put", a)
    assert seen["op"] == "EXACT WORDS stay put"  # invariant 2 holds through the context engine
