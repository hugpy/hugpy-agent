"""Rolling append-only log written as things happen (normal server behavior)."""
from hugpy_agent.mct import fake_a
from hugpy_agent.mct.session import BrokerConfig, BrokerServer


def test_rolling_log_written_live(tmp_path):
    server = BrokerServer(tmp_path, sink=lambda *_: None)
    log_path = server.ledger.event_log_path
    assert log_path.name == "mct.log"

    sess = server.session(server.open_session("t"))
    sess.register_source("doc", "the answer is 42\n")

    def a(client):
        client.read_operator_turn()
        out = client.submit_pull(need="d", target={"kind": "catalog-query", "query": "doc"})
        client.resolve(out.objects[0]["object"])
        client.respond("42")

    sess.submit("what is the answer?", a)

    text = log_path.read_text()
    # every party's activity is in the one rolling log, as it happened
    assert "turn.ingested" in text
    assert "context.built" in text
    assert "pull.requested" in text
    assert "a.read" in text                     # A's reads (receipts)
    assert "response.rendered" in text
    assert "turn.committed" in text
    # ordered and append-only: ingest precedes commit
    assert text.index("turn.ingested") < text.index("turn.committed")
    server.close()


def test_rolling_log_survives_restart_and_appends(tmp_path):
    s1 = BrokerServer(tmp_path, sink=lambda *_: None)
    sid = s1.open_session("t")
    s1.session(sid).submit("one", fake_a.answer("a"))
    lines_after_1 = s1.ledger.event_log_path.read_text().count("\n")
    s1.close()

    s2 = BrokerServer(tmp_path, sink=lambda *_: None)   # reopen: appends, doesn't truncate
    s2.session(sid).submit("two", fake_a.answer("b"))
    assert s2.ledger.event_log_path.read_text().count("\n") > lines_after_1
    s2.close()


def test_rolling_log_can_be_disabled(tmp_path):
    server = BrokerServer(tmp_path, sink=lambda *_: None, config=BrokerConfig(event_log=False))
    server.session(server.open_session("t")).submit("hi", fake_a.answer("x"))
    assert not server.ledger.event_log_path.exists()
    server.close()


def test_rolling_log_is_content_safe(tmp_path):
    server = BrokerServer(tmp_path, sink=lambda *_: None)
    sess = server.session(server.open_session("t"))
    sess.submit("my password is hunter2 swordfish", fake_a.answer("noted secret-reply"))
    text = server.ledger.event_log_path.read_text()
    assert "hunter2" not in text and "swordfish" not in text    # body-free (§18.3)
    assert "secret-reply" not in text
    server.close()
