"""Full C / B / A logs reconstructed from durable state (design §5, §14, §12)."""
from hugpy_agent.mct import fake_a
from hugpy_agent.mct.session import BrokerServer


def test_c_log_is_the_operator_conversation(session):
    session.submit("what is 2+2?", fake_a.answer("4"))
    session.submit("and 3+3?", fake_a.answer("6"))
    clog = session.server.logs.c_log(session.session_id)
    assert [e["operator"] for e in clog] == ["what is 2+2?", "and 3+3?"]
    assert all(e["shown"] for e in clog)
    assert "4" in clog[0]["assistant"] and "6" in clog[1]["assistant"]


def test_c_log_marks_unanswered_turns(session):
    session.submit("stay silent", fake_a.silent())      # A produces nothing
    e = session.server.logs.c_log(session.session_id)[0]
    assert e["assistant"] is None and not e["shown"]
    assert "did not answer" in e["notice"]


def test_b_log_is_the_hashchained_ledger(session):
    session.submit("hi", fake_a.answer("hello"))
    blog = session.server.logs.b_log(session.session_id)
    assert blog["chain_verified"] is True
    types = [e["type"] for e in blog["events"]]
    assert "turn.ingested" in types and "context.built" in types and "turn.committed" in types
    assert blog["metrics"]                               # per-turn metrics present


def test_a_log_has_reads_and_outputs(session, monkeypatch):
    session.register_source("doc", "the answer is 42\n")

    def a(client):
        client.read_operator_turn()
        out = client.submit_pull(need="d", target={"kind": "catalog-query", "query": "doc"})
        client.resolve(out.objects[0]["object"])
        client.respond("42")

    session.submit("what is the answer?", a)
    alog = session.server.logs.a_log(session.session_id)
    assert alog and alog[0]["reads"] and alog[0]["outputs"]
    kinds = {r["kind"] for r in alog[0]["reads"]}
    assert "operator_turn" in kinds


def test_write_all_produces_three_files(session, tmp_path):
    session.submit("hi", fake_a.answer("hello"))
    paths = session.server.logs.write_all(session.session_id, tmp_path / "logs")
    assert set(paths) == {"A", "B", "C", "MAP"}
    c_text = (tmp_path / "logs" / "C.log").read_text()
    b_text = (tmp_path / "logs" / "B.log").read_text()
    map_text = (tmp_path / "logs" / "MAP.log").read_text()
    assert "you> hi" in c_text and "hello" in c_text
    assert "chain_verified=True" in b_text
    assert "object store" in map_text and "objects/sha256" in map_text


def test_every_object_has_a_real_file_path(session):
    import os
    session.register_source("doc", "hi\n")

    def a(client):
        client.read_operator_turn()
        out = client.submit_pull(need="d", target={"kind": "catalog-query", "query": "doc"})
        client.resolve(out.objects[0]["object"])
        client.respond("done")

    session.submit("q", a)
    index = session.server.logs.object_index(session.session_id)
    assert index
    for o in index:                                       # nothing opaque — all on disk
        assert o["path"] and os.path.exists(o["path"])
    # /where resolves any pointer to its concrete file
    w = session.server.logs.where(session.session_id, index[0]["pointer"])
    assert w["path"] and os.path.exists(w["path"])
