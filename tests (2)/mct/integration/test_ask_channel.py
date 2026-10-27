"""The ask channel: A clarifies with the operator WITHOUT ending its turn.

Ending a turn to ask "which of the two did you mean?" is the most expensive way
to get a one-word answer — the next turn rebuilds the context and re-pays for
everything A had already resolved. The operator is sitting right there and is
not an LLM; the round trip should cost a sentence, not a turn.

Cross-process by construction: A's MCP server is a child of ``claude`` with no
tty and cannot read C's terminal, so the question travels through the shared
object store and whichever frontend owns stdin answers it.
"""
import threading
import time

from hugpy_agent.mct.session import BrokerServer


def _sess(broker):
    s = broker.session(broker.open_session("t"))
    s._active_turn = ("t_000001", "e1")
    return s


def test_an_answer_from_another_broker_handle_reaches_a(tmp_path, broker):
    """The two sides are different processes in real use; nothing may depend on
    shared memory."""
    sess = _sess(broker)

    def operator():
        other = BrokerServer(tmp_path, sink=lambda *_: None)
        osess = other.session(sess.session_id)
        try:
            for _ in range(100):
                pending = osess.pending_asks()
                if pending:
                    osess.answer_ask(pending[0]["pointer"], "the second one")
                    return
                time.sleep(0.02)
        finally:
            other.close()

    threading.Thread(target=operator, daemon=True).start()
    res = sess.broker_ask("which feed did you mean?", timeout=10)

    assert res["answered"] is True
    assert res["answer"] == "the second one"


def test_an_unanswered_ask_times_out_instead_of_hanging(broker):
    """A turn must never be able to block forever on an absent operator."""
    sess = _sess(broker)
    t0 = time.time()
    res = sess.broker_ask("anybody there?", timeout=1)
    elapsed = time.time() - t0

    assert res["answered"] is False
    assert 0.5 < elapsed < 5
    # and A is told plainly, so it does not silently invent a choice
    assert "no answer" in res["answer"] and "best assumption" in res["answer"]


def test_pending_clears_once_answered(broker):
    sess = _sess(broker)
    ptr = None

    def operator():
        nonlocal ptr
        for _ in range(100):
            p = sess.pending_asks()
            if p:
                ptr = p[0]["pointer"]
                sess.answer_ask(ptr, "yes")
                return
            time.sleep(0.02)

    threading.Thread(target=operator, daemon=True).start()
    sess.broker_ask("go ahead?", timeout=10)

    assert ptr is not None
    assert sess.pending_asks() == []


def test_several_asks_queue_in_order(broker):
    sess = _sess(broker)
    for q in ("first?", "second?"):
        sess.server.store.commit(
            sess.session_id, f'{{"question": "{q}"}}'.encode(),
            media_type="application/vnd.hugpy.mct-ask+json",
            kind="operator_ask", provenance={})
    assert [a["question"] for a in sess.pending_asks()] == ["first?", "second?"]


def test_the_exchange_is_committed_not_a_side_channel(broker):
    """A clarification that changed the answer has to be part of the record."""
    sess = _sess(broker)
    sess.broker_ask("unanswered on purpose", timeout=1)

    kinds = [o["kind"] for o in broker.ledger.list_objects(sess.session_id)]
    assert "operator_ask" in kinds
    rows = broker.access.entries(sess.session_id)
    assert any((r["actor"], r["verb"]) == ("A->C", "ask") for r in rows)
    assert any(r["verb"] == "unanswered" for r in rows)


def test_answering_records_the_operator_as_the_actor(broker):
    sess = _sess(broker)
    ptr = sess.server.store.commit(
        sess.session_id, b'{"question": "which?"}',
        media_type="application/vnd.hugpy.mct-ask+json",
        kind="operator_ask", provenance={}).pointer

    sess.answer_ask(ptr, "this one")

    assert sess._answer_for(ptr) == "this one"
    import sqlite3
    con = sqlite3.connect(str(broker.root / "mct.db"))
    con.row_factory = sqlite3.Row
    actors = [r["actor"] for r in con.execute(
        "select actor from events where type='ask.answered' and session_id=?",
        (sess.session_id,))]
    assert actors == ["C.operator"]


def test_a_is_told_the_tool_exists_and_told_not_to_burn_a_turn():
    from hugpy_agent.mct import claude_adapter as ca
    from hugpy_agent.mct import mct_mcp_server as mcp

    assert "submit_ask" in [t["name"] for t in mcp.TOOLS]
    assert "mcp__mct__submit_ask" in ca.tool_policy("none")[0]
    assert "submit_ask" in ca._SYSTEM
    assert "do not end the turn to ask" in ca._SYSTEM
