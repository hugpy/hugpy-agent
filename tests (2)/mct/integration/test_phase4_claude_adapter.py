"""Phase 4 Claude adapter — data path tested offline (no billed claude call).

Drives the MCP child's TurnTools directly (as `claude` would via the tools) and
verifies B renders once. Also checks A-unavailable => B does not answer.
"""
from hugpy_agent.mct import claude_adapter
from hugpy_agent.mct.mct_mcp_server import TurnTools


def _register_log(sess, tmp_path):
    d = tmp_path / "ws"
    d.mkdir()
    (d / "e.log").write_text("l1 ok\nl2 ok\nDECISION evict gpu-02 preempt A17\nl4 ok\n")
    sess.register_root("ws", str(d))
    sess.register_source_file("logs.e", "ws", "e.log")


def test_full_adapter_data_path_offline(broker, tmp_path, monkeypatch):
    sess = broker.session(broker.open_session("t"))
    sess.set_policy("cite evidence")
    _register_log(sess, tmp_path)

    # Parent prepares the turn (ingest + context + manifest), as submit_via_claude does.
    turn_id, epoch, op_ref, manifest_ptr, sha, trace = sess._prepare_turn("why evicted?", None)
    broker.ledger.set_turn_state(sess.session_id, turn_id, "Reasoning", epoch)

    # Drive the MCP tools exactly as the confined `claude` process would.
    monkeypatch.setenv("MCT_WORKSPACE", str(broker.workspace_root))
    monkeypatch.setenv("MCT_SESSION", sess.session_id)
    monkeypatch.setenv("MCT_TURN", turn_id)
    monkeypatch.setenv("MCT_EPOCH", epoch)
    monkeypatch.setenv("MCT_MANIFEST", manifest_ptr)
    tools = TurnTools()

    import json
    manifest = json.loads(tools.resolve({"pointer": manifest_ptr}))
    operator = tools.resolve({"pointer": manifest["operator_turn"]["object"]})
    assert operator == "why evicted?"                      # exact prompt reaches A

    pull = json.loads(tools.submit_pull({"need": "eviction line",
                                         "target": {"kind": "catalog-query", "query": "logs e"},
                                         "preferred_form": "match evict ctx 0"}))
    assert pull["decision"] == "reduced"
    assert "evict gpu-02" in pull["preview"]

    tools.respond({"body": f"Evicted by preempt. Evidence: {pull['preview'].strip()}"})
    tools.server.close()

    # Parent renders exactly once via the resume/response path.
    res = sess.on_response_ready(turn_id, epoch, _latest_response(broker, sess, turn_id),
                                 f"{sess.session_id}:{turn_id}:response:1")
    assert res["rendered"] and not res["already_rendered"]
    assert len(broker.printed) == 1 and "evict gpu-02" in broker.printed[0]
    assert broker.ledger.get_turn(sess.session_id, turn_id)["state"] == "Committed"


def _latest_response(broker, sess, turn_id):
    from hugpy_agent.mct.protocol import make_pointer
    ev = broker.ledger.latest_event(sess.session_id, turn_id, "a.response_ready")
    return make_pointer(sess.session_id, ev["output_objects"][0])


def test_a_unavailable_means_b_does_not_answer(broker, tmp_path, monkeypatch):
    sess = broker.session(broker.open_session("t"))
    # Simulate A unavailable: adapter returns no response manifest (§17, invariant 10).
    monkeypatch.setattr(claude_adapter.ClaudeCodeAdapter, "run_turn",
                        lambda *a, **k: {"response_manifest": None, "error": "unavailable"})
    r = sess.submit_via_claude("hello", model="sonnet")
    assert r.state == "Failed"
    assert not r.rendered
    assert broker.printed == []                            # B invented nothing
