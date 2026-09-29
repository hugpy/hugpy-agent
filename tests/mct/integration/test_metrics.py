"""Comprehensive metrics dashboard covers every dimension (design §18)."""
from hugpy_agent.mct import fake_a


def test_full_report_covers_all_dimensions(session):
    session.register_source("doc", "the answer is 42\n")

    def a(client):
        client.read_operator_turn()
        out = client.submit_pull(need="d", target={"kind": "catalog-query", "query": "doc"})
        client.resolve(out.objects[0]["object"])
        client.respond("42")

    session.submit("what's the answer?", a)
    r = session.server.metrics.full_report(session.session_id)

    # B's cache (object store) contents + totals
    assert r["cache_store"]["objects"] > 0
    assert r["cache_store"]["total_bytes"] > 0
    assert "operator_turn" in r["cache_store"]["by_kind"]
    # turns / pulls / reads / memory / ledger
    assert r["turns"]["total"] == 1 and r["turns"]["by_state"].get("Committed") == 1
    assert r["pulls"]["total"] >= 1
    assert r["reads"]["total"] >= 1 and "operator_turn" in r["reads"]["by_kind"]
    assert r["ledger"]["chain_verified"] is True
    assert r["ledger"]["events"] > 0 and r["ledger"]["epochs"] >= 1

    text = session.server.metrics.render(session.session_id)
    for section in ("B's cache", "A's cache", "turns / latency", "pulls / reads / memory",
                    "ledger integrity"):
        assert section in text


def test_dedup_saved_is_counted(session):
    # commit the same bytes under two objects -> dedup shows a saving
    session.server.store.commit(session.session_id, b"dup", media_type="text/plain", kind="x")
    session.server.store.commit(session.session_id, b"dup", media_type="text/plain", kind="x")
    r = session.server.metrics.full_report(session.session_id)
    assert r["cache_store"]["dedup_saved_objects"] >= 1
