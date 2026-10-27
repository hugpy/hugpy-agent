"""When Claude answers as final text (not via respond()), B relays it (design §5.2)."""
from hugpy_agent.mct import claude_adapter
from hugpy_agent.mct.claude_adapter import ClaudeCodeAdapter


def test_final_text_is_relayed_as_the_answer(broker):
    sess = broker.session(broker.open_session("t"))
    ad = ClaudeCodeAdapter(broker)
    turn_id, epoch, op_ref, mptr, sha, trace = sess._prepare_turn("did you read files?", None)

    # simulate the adapter's fallback: Claude produced final text, never called respond
    ptr = ad._ingest_text_answer(sess, turn_id, epoch, "No — those were session objects, not files.")
    res = sess.on_response_ready(turn_id, epoch, ptr, f"{sess.session_id}:{turn_id}:response:1")
    assert res["rendered"] and not res["already_rendered"]
    assert "session objects" in broker.printed[-1]


def test_failure_reason_is_reported(broker, monkeypatch):
    sess = broker.session(broker.open_session("t"))
    # A genuinely fails (timeout) -> the reason is surfaced, not a bare "Failed".
    monkeypatch.setattr(claude_adapter.ClaudeCodeAdapter, "run_turn",
                        lambda *a, **k: {"response_manifest": None,
                                         "error": "claude timed out after 240s"})
    r = sess.submit_via_claude("hello", model="sonnet")
    assert r.state == "Failed"
    assert r.error == "claude timed out after 240s"


def test_failure_reason_helper():
    err = ClaudeCodeAdapter._failure_reason({"is_error": True, "subtype": "error_max_turns",
                                             "api_error_status": None}, None)
    assert "error_max_turns" in err
    ok_none = ClaudeCodeAdapter._failure_reason({"is_error": False}, None)
    assert "no answer" in ok_none
