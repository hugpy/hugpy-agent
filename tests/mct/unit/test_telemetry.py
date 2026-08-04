"""Observability: metrics recorded, traces content-safe (design §18)."""
from hugpy_agent.mct import fake_a


def test_turn_metric_recorded(session):
    r = session.submit("what is the status?", fake_a.answer("all good"))
    m = session.server.ledger.get_metric(session.session_id, r.turn_id)
    assert m is not None
    assert m["operator_bytes"] == len("what is the status?")
    assert m["rendered"] == 1
    assert m["latency_ms"] >= 0


def test_turn_report_is_content_safe(session):
    # a prompt with a distinctive secret string must never appear in telemetry
    secret = "SECRET-swordfish-42"
    r = session.submit(f"deploy key is {secret}", fake_a.answer("noted"))
    report = session.server.telemetry.turn_report(session.session_id, r.turn_id)
    assert session.server.telemetry.is_content_safe(report)
    assert secret not in str(report)          # §18.3 — no bodies/secrets in traces


def test_session_summary_aggregates(session):
    session.submit("one", fake_a.answer("a"))
    session.submit("two", fake_a.answer("b"))
    s = session.server.telemetry.session_summary(session.session_id)
    assert s["turns"] == 2 and s["avg_latency_ms"] >= 0


def test_trace_answers_what_a_opened(session):
    session.register_source("doc", "hello\nworld\n")

    def a(client):
        client.read_operator_turn()
        out = client.submit_pull(need="d", target={"kind": "catalog-query", "query": "doc"})
        client.respond("done")

    r = session.submit("open doc", a)
    report = session.server.telemetry.turn_report(session.session_id, r.turn_id)
    types = {e["type"] for e in report["events"]}
    assert "pull.requested" in types                    # trace shows the pull happened
    assert report["pull_decisions"]                     # ...and its decision
    assert session.server.telemetry.is_content_safe(report)
