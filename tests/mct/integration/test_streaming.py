"""Streaming responses: ordered frames, verified assembly, fail-closed (§16.2-16.3)."""
import pytest

from hugpy_agent.mct import fake_a


def test_streamed_frames_render_in_order(session):
    prog = fake_a.stream_answer(["Hello ", "streamed ", "world."])
    r = session.submit("hi", prog)
    assert r.state == "Committed"
    # frames emitted incrementally, then sealed — one logical answer, three writes
    assert session.server.printed == ["Hello ", "streamed ", "world."]
    assert prog.render.rendered and not prog.render.already_rendered


def test_stream_marks_turn_rendered_once(session):
    prog = fake_a.stream_answer(["a", "b"])
    r = session.submit("hi", prog)
    assert session.server.ledger.is_rendered(session.session_id, r.turn_id)
    # a duplicate seal must not re-render (invariant 14)
    assert prog.render.rendered


def test_out_of_order_frame_rejected(session):
    def bad(client):
        client.read_operator_turn()
        # drive frames manually out of order via the binding-facing broker
        b = client._b
        p0 = b.create_object(b"x", "text/markdown", "response_stream_chunk", {"seq": 0})
        b.handle_stream_frame(p0, 0)
        p2 = b.create_object(b"z", "text/markdown", "response_stream_chunk", {"seq": 2})
        with pytest.raises(Exception):
            b.handle_stream_frame(p2, 2)  # expected seq 1
        client.respond("recovered to non-stream answer")
    r = session.submit("hi", bad)
    assert r.state == "Committed"


def test_tampered_assembly_is_rejected(session, monkeypatch):
    # If the sealed body doesn't match the streamed frames, B refuses (never guesses).
    def evil(client):
        client.read_operator_turn()
        b = client._b
        p0 = b.create_object(b"real frame", "text/markdown", "response_stream_chunk", {"seq": 0})
        b.handle_stream_frame(p0, 0)
        # seal a manifest whose body differs from the streamed frame
        import hashlib, json
        body = b"DIFFERENT body"
        body_ptr = b.create_object(body, "text/markdown", "response_body", None)
        manifest = {"schema": "mct.response/1", "session_id": client.session_id,
                    "turn_id": client.turn_id, "epoch": client.epoch, "body": body_ptr,
                    "format": "text/markdown", "final": True, "proposed_actions": [],
                    "body_sha256": hashlib.sha256(body).hexdigest()}
        mptr = b.create_object(json.dumps(manifest).encode(),
                               "application/vnd.hugpy.mct-response+json", "response_manifest", None)
        evil.result = b.handle_stream_seal(mptr, f"{client.session_id}:{client.turn_id}:response:1")
    session.submit("hi", evil)
    assert evil.result["discarded"] is True  # assembled != sealed body -> rejected
