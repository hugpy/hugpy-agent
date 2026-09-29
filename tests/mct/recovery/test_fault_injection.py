"""Fault injection at turn transitions; resume is idempotent (design §17, exit cond.)."""
from hugpy_agent.mct import fake_a, gc, recovery
from hugpy_agent.mct.a_adapter import AAdapterClient
from hugpy_agent.mct.session import BrokerServer


def test_crash_before_respond_leaves_no_render_and_is_resumable(tmp_path):
    printed = []
    server = BrokerServer(tmp_path, sink=printed.append)
    sid = server.open_session("t")

    def boom(client):
        client.read_operator_turn()
        raise RuntimeError("crash before respond")
    try:
        server.session(sid).submit("go", boom)
    except RuntimeError:
        pass
    assert printed == []                                  # B invented nothing (invariant 10)
    report = recovery.reconcile(server)
    assert report.chains_ok
    assert report.unfinished_turns                        # the turn is left mid-flight
    server.close()


def test_crash_after_context_built_before_a(tmp_path):
    # Prepare a turn (ingest + context + manifest) but never run A — as if B died
    # right after ContextBuilt. Durable state is consistent and resumable.
    server = BrokerServer(tmp_path, sink=lambda *_: None)
    sid = server.open_session("t")
    sess = server.session(sid)
    turn_id, epoch, op_ref, mptr, sha, trace = sess._prepare_turn("half a turn", None)
    server.close()

    server2 = BrokerServer(tmp_path, sink=lambda *_: None)
    report = recovery.reconcile(server2)
    assert report.chains_ok
    assert any(t["turn_id"] == turn_id for t in report.unfinished_turns)
    # the operator turn and manifest are intact and readable
    assert server2.store.resolve(sid, op_ref.pointer) == b"half a turn"
    server2.close()


def test_crash_after_render_resumes_without_double_render(tmp_path):
    printed = []
    server = BrokerServer(tmp_path, sink=printed.append)
    sid = server.open_session("t")

    def crash_after_render(client):
        client.read_operator_turn()
        client.respond("answer before crash")
        crash_after_render.resend = (client.turn_id, client.epoch, client._b.response_manifest,
                                     f"{client.session_id}:{client.turn_id}:response:1")
        raise RuntimeError("crash after render")
    try:
        server.session(sid).submit("go", crash_after_render)
    except RuntimeError:
        pass
    assert len(printed) == 1
    tid, epoch, mptr, key = crash_after_render.resend
    server.close()

    server2 = BrokerServer(tmp_path, sink=printed.append)
    recovery.reconcile(server2)
    res = server2.session(sid).on_response_ready(tid, epoch, mptr, key)
    assert res["already_rendered"] and not res["rendered"]
    assert len(printed) == 1                              # exactly once across the crash
    assert server2.ledger.get_turn(sid, tid)["state"] == "Committed"
    server2.close()


def test_orphan_bytes_cleaned_after_crash(tmp_path):
    server = BrokerServer(tmp_path, sink=lambda *_: None)
    sid = server.open_session("t")
    server.session(sid).submit("ok", fake_a.answer("done"))
    # simulate a crash that left an unreferenced object file behind
    orphan = server.store._path_for_digest("ef" * 32)
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"leftover")
    report = gc.garbage_collect(server, dry_run=False)
    assert "ef" * 32 in report.collectable
    assert not orphan.exists()
    assert server.ledger.verify_chain(sid)                # ledger unaffected
    server.close()
