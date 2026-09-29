"""A-cache mirror: B durably records everything A received/produced (design §12)."""
import json

from hugpy_agent.mct.mct_mcp_server import TurnTools


def _drive_turn(broker, sess, monkeypatch, prompt="why evicted?"):
    """Run one turn through the real A-facing tools (offline), plus the adapter's
    input/transcript capture, exactly as claude_adapter would."""
    d = None
    sess.register_source("logs.e", "l1 ok\nDECISION evict gpu-02 preempt A17\nl3 ok\n")
    turn_id, epoch, op_ref, mptr, sha, trace = sess._prepare_turn(prompt, None)
    broker.ledger.set_turn_state(sess.session_id, turn_id, "Reasoning", epoch)

    # adapter captures A's exact inputs (system prompt + prompt) as durable objects
    for kind, text in [("a_system_prompt", "You are A. Tools: resolve/submit_pull/respond."),
                       ("a_prompt", f"Handle one turn. Manifest={mptr}")]:
        broker.store.commit(sess.session_id, text.encode(), media_type="text/plain",
                            kind=kind, provenance={"turn_id": turn_id, "epoch": epoch})

    monkeypatch.setenv("MCT_WORKSPACE", str(broker.workspace_root))
    monkeypatch.setenv("MCT_SESSION", sess.session_id)
    monkeypatch.setenv("MCT_TURN", turn_id)
    monkeypatch.setenv("MCT_EPOCH", epoch)
    monkeypatch.setenv("MCT_MANIFEST", mptr)
    tools = TurnTools()
    m = json.loads(tools.resolve({"pointer": mptr}))
    tools.resolve({"pointer": m["operator_turn"]["object"]})
    tools.submit_pull({"need": "evidence", "target": {"kind": "catalog-query", "query": "logs e"},
                       "preferred_form": "match evict ctx 0"})
    tools.respond({"body": "Evicted by preempt (A17)."})
    tools.server.close()

    # adapter persists A's full transcript
    broker.store.commit(sess.session_id, b'{"type":"assistant"}\n{"type":"result"}\n',
                        media_type="application/x-ndjson", kind="a_transcript",
                        provenance={"turn_id": turn_id, "epoch": epoch})
    # commit turn so it's discoverable
    broker.ledger.set_turn_state(sess.session_id, turn_id, "Committed", epoch)
    return turn_id


def test_a_cache_captures_everything_a_received_and_produced(broker, monkeypatch):
    sess = broker.session(broker.open_session("t"))
    turn_id = _drive_turn(broker, sess, monkeypatch)

    rec = broker.a_cache.for_turn(sess.session_id, turn_id)

    # A's exact inputs are documented
    roles_in = {i["role"] for i in rec["inputs"]}
    assert roles_in == {"system_prompt", "prompt"}

    # everything A opened is captured, with reconstructable content (invariant 6)
    kinds_read = {r["kind"] for r in rec["reads"]}
    assert "context_manifest" in kinds_read and "operator_turn" in kinds_read
    op_read = next(r for r in rec["reads"] if r["kind"] == "operator_turn")
    assert op_read["content"] == "why evicted?"          # actual bytes, not just a pointer

    # A's outputs (its pull and its answer) are captured
    roles_out = {o["role"] for o in rec["outputs"]}
    assert "pull_request" in roles_out and "response" in roles_out
    resp = next(o for o in rec["outputs"] if o["role"] == "response")
    assert "preempt" in resp["content"]

    # A's full agent transcript is persisted
    assert rec["transcript"] is not None and rec["transcript"]["bytes"] > 0


def test_a_cache_session_stats_and_dump(broker, monkeypatch):
    sess = broker.session(broker.open_session("t"))
    _drive_turn(broker, sess, monkeypatch)
    stats = broker.a_cache.stats(sess.session_id)
    assert stats["objects_A_opened"] >= 2
    assert stats["pulls_A_issued"] >= 1
    assert stats["transcripts_captured"] == 1
    dump = broker.a_cache.dump(sess.session_id)
    assert "READ" in dump and "OUT" in dump and "TRANSCRIPT" in dump


def test_a_cache_reconstructs_from_durable_state_only(broker, monkeypatch):
    # Reopen a fresh server over the same store; the A-cache is fully rebuildable.
    sess = broker.session(broker.open_session("t"))
    turn_id = _drive_turn(broker, sess, monkeypatch)
    sid = sess.session_id
    from hugpy_agent.mct.session import BrokerServer
    reopened = BrokerServer(broker.workspace_root, sink=lambda *_: None)
    rec = reopened.a_cache.for_turn(sid, turn_id)
    assert rec["reads"] and rec["outputs"] and rec["transcript"]
    reopened.close()
