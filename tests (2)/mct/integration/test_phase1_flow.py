"""End-to-end mediated turn flow through BrokerServer (design §9, §10, §25)."""
from hugpy_agent.mct import fake_a


def test_normal_turn_completes_and_renders_once(session):
    r = session.submit("hello", fake_a.answer("Present."))
    assert r.state == "Committed" and r.rendered
    assert len(session.server.printed) == 1
    assert "Present." in session.server.printed[0]
    assert r.receipt is not None  # adapter receipt sealed (§12.2)


def test_operator_prompt_is_verbatim(session):
    seen = {}

    def a(client):
        seen["op"] = client.read_operator_turn()
        client.respond("ok")

    session.submit("EXACT operator words", a)
    assert seen["op"] == "EXACT operator words"  # invariant 2


def test_pull_reduced_excerpt_flow(session):
    session.register_source("logs.gpu-02", "l1\nl2\nEVICT gpu-02\nl4\nl5\n")
    r = session.submit(
        "why evicted?",
        fake_a.answer_after_pull(need="eviction line", query="logs gpu-02",
                                 preferred_form="lines 3-3"),
    )
    assert r.state == "Committed"
    assert "EVICT gpu-02" in session.server.printed[-1]
    assert "reduced" in session.server.printed[-1]


def test_pull_not_found_is_graceful(session):
    def a(client):
        client.read_operator_turn()
        out = client.submit_pull(need="missing", target={"kind": "catalog-query",
                                                          "query": "nonexistent zzz"})
        assert out.decision == "not_found"
        client.respond("no evidence found")

    r = session.submit("find nothing", a)
    assert r.state == "Committed"


def test_pull_budget_exhaustion(session):
    session.server.pull_broker.budget.max_pulls = 2
    session.register_source("cat.item", "data\n")

    def a(client):
        client.read_operator_turn()
        decisions = [client.submit_pull(need="x", target={"kind": "catalog-query",
                                                           "query": "cat item"}).decision
                     for _ in range(3)]
        client.respond("done: " + ",".join(decisions))

    session.submit("hammer pulls", a)
    assert "budget_exhausted" in session.server.printed[-1]


def test_receipts_capture_what_a_opened(session):
    session.register_source("cat.doc", "hello world\n")

    def a(client):
        client.read_operator_turn()
        out = client.submit_pull(need="d", target={"kind": "catalog-query", "query": "cat doc"})
        client.resolve(out.objects[0]["object"])
        client.respond("read it")

    r = session.submit("open a doc", a)
    receipts = session.server.ledger.receipts_for_turn(session.session_id, r.turn_id)
    purposes = {rr["purpose"] for rr in receipts}
    assert "operator_turn" in purposes and "read" in purposes  # invariant 6
