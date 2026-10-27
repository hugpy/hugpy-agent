"""Crash / restart / resume: durable state survives, no double render (design §17)."""
from hugpy_agent.mct import fake_a, recovery
from hugpy_agent.mct.a_adapter import AAdapterClient
from hugpy_agent.mct.session import BrokerServer


def _crashing(text):
    def program(client: AAdapterClient) -> None:
        client.read_operator_turn()
        client.respond(text)
        program.resend = (client.turn_id, client.epoch, client._b.response_manifest,
                         f"{client.session_id}:{client.turn_id}:response:1")
        raise RuntimeError("crash after render")
    return program


def test_crash_then_resume_renders_exactly_once(tmp_path):
    printed = []
    server = BrokerServer(tmp_path, sink=printed.append)
    sid = server.open_session("t")
    sess = server.session(sid)

    prog = _crashing("emitted before crash")
    try:
        sess.submit("go", prog)
    except RuntimeError:
        pass
    assert len(printed) == 1                        # rendered once pre-crash
    turn_id, epoch, manifest_ptr, key = prog.resend
    server.close()

    # restart over the same durable root
    server2 = BrokerServer(tmp_path, sink=printed.append)
    report = recovery.reconcile(server2)
    assert report.chains_ok
    assert any(t["turn_id"] == turn_id for t in report.unfinished_turns)

    sess2 = server2.session(sid)
    result = sess2.on_response_ready(turn_id, epoch, manifest_ptr, key)
    assert result["already_rendered"] and not result["rendered"]
    assert len(printed) == 1                         # still exactly one line
    assert server2.ledger.get_turn(sid, turn_id)["state"] == "Committed"
    server2.close()


def test_reconcile_cleans_orphan_temp(tmp_path):
    server = BrokerServer(tmp_path, sink=lambda *_: None)
    (server.store.tmp_dir / "obj-orphan.tmp").write_bytes(b"junk")
    report = recovery.reconcile(server)
    assert report.removed_temp_objects == 1
    assert not list(server.store.tmp_dir.glob("*"))
    server.close()
